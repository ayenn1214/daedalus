"""Microsoft 365: Teams alerts and OneDrive / SharePoint workbook sync.

Teams: a channel's Workflows webhook (Teams ▸ channel ▸ ⋯ ▸ Workflows ▸ "Post to a channel when a webhook request is
received") gets an adaptive card when a part drops below its reorder point, a production order is completed, quotes
are requested or a part is released. No sign-in needed — the webhook address is the key.

OneDrive / SharePoint: Daedalus signs in with Microsoft (device code: you type a short code at microsoft.com/devicelogin)
using your organisation's app registration (client ID, delegated Files.ReadWrite.All), then pulls the shared workbook
into DIMS or pushes DIMS out to it — replacing the file only if nobody changed it since it was read. If the OneDrive
app already syncs that folder to this PC, Import / Export on the file there does the same with no sign-in.

Qt-free except settings(); HTTP through urllib (system proxy)."""
from __future__ import annotations

import base64
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPES = "offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All"
EVENTS = [("low_stock", "Part below its reorder point"), ("production_done", "Production order completed"),
          ("rfq", "Quotes requested"), ("released", "Part released (new revision)")]


class M365Error(RuntimeError):
    pass


# ------------------------------------------------------------------ settings
_OVERRIDE: dict = {}


def setting(key, default=""):
    if key in _OVERRIDE:
        return _OVERRIDE[key]
    try:
        from PySide6.QtCore import QSettings
        v = QSettings("Daedalus", "Daedalus").value(key, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def set_setting(key, value):
    if key in _OVERRIDE:
        _OVERRIDE[key] = value
        return
    try:
        from PySide6.QtCore import QSettings
        QSettings("Daedalus", "Daedalus").setValue(key, value)
    except Exception:  # noqa: BLE001
        pass


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")


# ------------------------------------------------------------------ HTTP
def _req(method, url, data=None, headers=None, form=False, raw=False, timeout=60):
    h = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
            return (out, dict(r.headers)) if raw else (json.loads(out.decode() or "{}") if out else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            j = json.loads(txt)
            err = j.get("error")
            msg = (err.get("message") if isinstance(err, dict) else None) or j.get("error_description") or err or txt
            code = (err.get("code") if isinstance(err, dict) else err) or ""
        except ValueError:
            msg, code = txt, ""
        ex = M365Error(f"{e.code} {msg}")
        ex.status, ex.code = e.code, code
        raise ex from None
    except urllib.error.URLError as e:
        raise M365Error(f"Can't reach Microsoft ({e.reason})") from None


# ------------------------------------------------------------------ Teams
def card(title: str, text: str = "", facts: dict | None = None, link: str = "") -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True})
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": str(k), "value": str(v)} for k, v in facts.items()]})
    content = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
               "version": "1.4", "body": body}
    if link:
        content["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": content}]}


def post_teams(webhook: str, title: str, text: str = "", facts: dict | None = None, link: str = ""):
    if not webhook:
        raise M365Error("No Teams webhook set (Settings ▸ Microsoft 365)")
    _req("POST", webhook, card(title, text, facts, link), timeout=20)


_sent: dict = {}
_lock = threading.Lock()


def notify(event: str, title: str, text: str = "", facts: dict | None = None, key: str = "", wait: bool = False,
           repeat_hours: float = 24.0) -> bool:
    """Post to Teams if alerts are on and this event is ticked. key: don't repeat the same alert within
    repeat_hours. Runs in the background unless wait."""
    if not _bool(setting("teams/enabled", False)) or not setting("teams/webhook", ""):
        return False
    on = str(setting("teams/events", ",".join(k for k, _ in EVENTS)) or "").split(",")
    if event not in on:
        return False
    k = f"{event}:{key or title}"
    with _lock:
        if key and time.time() - _sent.get(k, 0) < repeat_hours * 3600:
            return False
        _sent[k] = time.time()
    hook = setting("teams/webhook", "")

    def go():
        try:
            post_teams(hook, title, text, facts)
        except Exception:  # noqa: BLE001
            pass
    if wait:
        go()
    else:
        threading.Thread(target=go, daemon=True).start()
    return True


class InventoryWatcher:
    """Turns DIMS changes into Teams alerts: parts that newly drop below their reorder point, production orders that
    newly become Complete. Call check(inv) after each saved change."""

    def __init__(self):
        self.red: set | None = None
        self.done: set | None = None

    def check(self, inv) -> list[str]:
        from .inventory import model as M
        red = {p["id"] for p in inv.parts if M.num(p.get("reorderPoint")) > 0 and
               M.num(p.get("currentStock")) <= M.num(p.get("reorderPoint")) and not p.get("discontinued")}
        done = {o["id"] for o in inv.productionOrders if o.get("status") == "Complete"}
        sent = []
        if self.red is not None:
            for pid in red - self.red:
                p = inv.part(pid)
                best = inv.best_supplier(p)
                if notify("low_stock", f"Low stock: {p['name']}", "", {
                        "On hand": f"{M.num(p.get('currentStock')):g} {p.get('unit') or 'ea'}",
                        "Reorder point": f"{M.num(p.get('reorderPoint')):g}",
                        "Part #": p.get("partNumber") or "—", "Best supplier": best["name"] if best else "—"},
                        key=pid):
                    sent.append(pid)
            for oid in done - (self.done or set()):
                o = next(x for x in inv.productionOrders if x["id"] == oid)
                name = inv.project_name(o.get("projectId")) or o.get("name") or "Production order"
                if notify("production_done", f"Completed: {name}", "", {
                        "Order": o.get("number") or o.get("name") or oid[:6], "Qty": f"{M.num(o.get('qty'), 1):g}",
                        "By": inv.userName or ""}, key=oid):
                    sent.append(oid)
        self.red, self.done = red, done
        return sent


# ------------------------------------------------------------------ sign-in (device code)
def client_id() -> str:
    return str(setting("m365/client_id", "") or "").strip()


def tenant() -> str:
    return str(setting("m365/tenant", "organizations") or "organizations").strip()


def device_code_start() -> dict:
    """{'user_code', 'verification_uri', 'message', 'device_code', 'interval', 'expires_in'}"""
    if not client_id():
        raise M365Error("Set your organisation's app (client) ID first — see Settings ▸ Microsoft 365")
    return _req("POST", f"{LOGIN}/{tenant()}/oauth2/v2.0/devicecode", {"client_id": client_id(), "scope": SCOPES},
                form=True)


def device_code_poll(dc: dict, timeout: float | None = None, sleep=time.sleep) -> dict:
    """Wait until the code is entered; stores the refresh token. Returns the token response."""
    end = time.time() + (timeout or float(dc.get("expires_in", 900)))
    interval = float(dc.get("interval", 5))
    while time.time() < end:
        try:
            tok = _req("POST", f"{LOGIN}/{tenant()}/oauth2/v2.0/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id(),
                "device_code": dc["device_code"]}, form=True)
            _store(tok)
            return tok
        except M365Error as e:
            code = getattr(e, "code", "")
            if code == "authorization_pending":
                sleep(interval)
                continue
            if code == "slow_down":
                interval += 5
                sleep(interval)
                continue
            raise
    raise M365Error("The sign-in code expired — start again")


_token = {"access": "", "exp": 0.0}


def _store(tok: dict):
    _token["access"] = tok.get("access_token", "")
    _token["exp"] = time.time() + float(tok.get("expires_in", 3600)) - 60
    if tok.get("refresh_token"):
        set_setting("m365/refresh", tok["refresh_token"])


def signed_in() -> bool:
    return bool(setting("m365/refresh", ""))


def sign_out():
    set_setting("m365/refresh", "")
    _token.update(access="", exp=0.0)


def token() -> str:
    if _token["access"] and time.time() < _token["exp"]:
        return _token["access"]
    rt = setting("m365/refresh", "")
    if not rt:
        raise M365Error("Not signed in to Microsoft 365 (Settings ▸ Microsoft 365 ▸ Sign in)")
    tok = _req("POST", f"{LOGIN}/{tenant()}/oauth2/v2.0/token", {
        "grant_type": "refresh_token", "client_id": client_id(), "refresh_token": rt, "scope": SCOPES}, form=True)
    _store(tok)
    return _token["access"]


def graph(method, path, data=None, raw=False, headers=None):
    h = {"Authorization": f"Bearer {token()}", **(headers or {})}
    return _req(method, path if path.startswith("http") else GRAPH + path, data, h, raw=raw, timeout=120)


def me() -> dict:
    return graph("GET", "/me")


# ------------------------------------------------------------------ the workbook
def share_id(link: str) -> str:
    """Graph's id for a sharing link: 'u!' + unpadded base64url."""
    return "u!" + base64.urlsafe_b64encode(link.strip().encode()).decode().rstrip("=")


def resolve(link: str) -> dict:
    """A OneDrive / SharePoint link (Share ▸ Copy link, or the address bar) → the file."""
    it = graph("GET", f"/shares/{share_id(link)}/driveItem")
    ref = it.get("parentReference") or {}
    return {"drive": ref.get("driveId"), "item": it.get("id"), "name": it.get("name"), "web": it.get("webUrl"),
            "etag": it.get("eTag"), "modified": it.get("lastModifiedDateTime"),
            "by": ((it.get("lastModifiedBy") or {}).get("user") or {}).get("displayName", "")}


def download(f: dict) -> tuple[bytes, str]:
    meta = graph("GET", f"/drives/{f['drive']}/items/{f['item']}")
    data, _h = graph("GET", f"/drives/{f['drive']}/items/{f['item']}/content", raw=True)
    return data, meta.get("eTag", "")


def upload(f: dict, data: bytes, etag: str = "") -> dict:
    h = {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    if etag:
        h["If-Match"] = etag
    try:
        return graph("PUT", f"/drives/{f['drive']}/items/{f['item']}/content", data, headers=h)
    except M365Error as e:
        if getattr(e, "status", 0) == 412:
            raise M365Error("Someone changed the workbook since Daedalus read it — pull first, then push") from None
        if getattr(e, "status", 0) == 423:
            raise M365Error("The workbook is open for editing in Excel — close it there (or wait), then push") \
                from None
        raise


def pull(inv, link: str | None = None) -> dict:
    """Workbook → DIMS (the same import as Import… on a local file). Returns counts and the file's version."""
    import os
    import tempfile
    f = resolve(link or setting("m365/workbook", ""))
    data, etag = download(f)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    try:
        os.write(fd, data)
        os.close(fd)
        added, updated = inv.import_file(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    set_setting("m365/etag", etag)
    set_setting("m365/pulled", dt.datetime.now().isoformat(timespec="seconds"))
    return {"file": f["name"], "added": added, "updated": updated, "by": f["by"], "modified": f["modified"]}


def push(inv, link: str | None = None, force: bool = False) -> dict:
    """DIMS → workbook (the same export as Export…), only if unchanged since the last pull unless force."""
    import os
    import tempfile
    f = resolve(link or setting("m365/workbook", ""))
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        inv.export_file(tmp)
        data = open(tmp, "rb").read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = upload(f, data, "" if force else str(setting("m365/etag", "") or ""))
    set_setting("m365/etag", out.get("eTag", ""))
    return {"file": f["name"], "bytes": len(data)}
