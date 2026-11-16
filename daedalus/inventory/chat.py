"""DIMS team chat: messages, item links and the two backends.

Item links work like linking an item in an MMO chat: Shift-click a part, project or order and it drops into the
message as [label]. In storage a link is a token  {{kind:id|label}}  inside the text, so any backend that keeps text
keeps links. The label is a snapshot; clicking the link opens the live record.

Backends:
* FileChat  — a .chat.jsonl file next to the inventory file (works on a shared folder too; append-only).
* ServerChat — the dims_chat table on the team server (supabase/migrations/*_dims_chat.sql).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from . import model as M

LINK_RE = re.compile(r"\{\{(part|project|production|order|vendor|location):([^|{}]+)\|([^{}]*)\}\}")
KINDS = ("part", "project", "production", "order", "vendor", "location")
MAX_LEN = 4000


def token(kind: str, id_: str, label: str) -> str:
    label = re.sub(r"[{}|\[\]]", "", label or "").strip()[:80] or id_
    return f"{{{{{kind}:{id_}|{label}}}}}"


def parts_of(body: str):
    """[('text', s) | ('link', kind, id, label)] in order."""
    out, i = [], 0
    for m in LINK_RE.finditer(body or ""):
        if m.start() > i:
            out.append(("text", body[i:m.start()]))
        out.append(("link", m.group(1), m.group(2), m.group(3)))
        i = m.end()
    if i < len(body or ""):
        out.append(("text", body[i:]))
    return out


def plain(body: str) -> str:
    """Text with links shown as [label] (notifications, logs, search)."""
    return LINK_RE.sub(lambda m: f"[{m.group(3)}]", body or "")


# ------------------------------------------------------------------ links to records
def link_for(inv: M.Inventory, kind: str, id_: str) -> str | None:
    """A token for a live record (None if it doesn't exist)."""
    if kind == "part":
        p = inv.part(id_)
        return token("part", id_, M.label(p)) if p else None
    if kind == "project":
        n = inv.project_name(id_)
        return token("project", id_, n) if n else None
    if kind == "production":
        o = next((x for x in inv.productionOrders if x.get("id") == id_), None)
        if not o:
            return None
        lab = (o.get("orderName") or inv.project_name(o.get("projectId")) or "Production") + \
              f" ×{M.num(o.get('qty')):g}" + (f" · PO {o['poNumber']}" if o.get("poNumber") else "")
        return token("production", id_, lab)
    if kind == "order":
        o = next((x for x in inv.clientOrders if x.get("id") == id_), None)
        if not o:
            return None
        from . import orders as O
        return token("order", id_, (O.order_name(inv, o) or "Order")
                     + (f" · PO {o['poNumber']}" if o.get("poNumber") else ""))
    if kind == "vendor":
        v = next((x for x in inv.vendors if x.get("id") == id_ or x.get("name") == id_), None)
        return token("vendor", id_, v["name"]) if v else None
    if kind == "location":
        lab = inv.location_label_for(id_)
        return token("location", id_, lab) if lab else None
    return None


def resolve(inv: M.Inventory, texts=(), ids=()):
    """Best record for something clicked that carries no link: ids found on it (part / project / order ids), then
    its text — part number, 'PN — name', project, vendor, bin, order PO, then a unique part name."""
    for v in ids:
        if not isinstance(v, str) or not v:
            continue
        if inv.part(v):
            return "part", v
        if inv.project(v):
            return "project", v
        if any(o.get("id") == v for o in inv.productionOrders):
            return "production", v
        if any(o.get("id") == v for o in inv.clientOrders):
            return "order", v
    by_pn = {(p.get("partNumber") or "").strip().lower(): p for p in inv.parts if p.get("partNumber")}
    by_name: dict = {}
    for p in inv.parts:
        by_name.setdefault(p["name"].strip().lower(), []).append(p)
    projects = {pr["name"].strip().lower(): pr for pr in inv.projects}
    vendors = {v["name"].strip().lower(): v for v in inv.vendors if v.get("name")}
    pos = {(o.get("poNumber") or "").strip().lower(): o for o in inv.clientOrders if o.get("poNumber")}
    for raw in texts:
        for t in [x.strip() for x in re.split(r"\n", raw or "") if x.strip()]:
            t = re.sub(r"^(×\s*\S+\s+)", "", t).strip()                    # BOM lines: "× 4  FLAT, TAB"
            k = t.lower()
            first = k.split(" — ")[0].split()[0] if k.split() else ""
            if k in by_pn:
                return "part", by_pn[k]["id"]
            if first in by_pn and (k == first or k.startswith(first + " ")):
                return "part", by_pn[first]["id"]
            if k in projects:
                return "project", projects[k]["id"]
            if k in vendors:
                v = vendors[k]
                return "vendor", v.get("id") or v["name"]
            if k in pos:
                return "order", pos[k]["id"]
            loc = next((l for l in inv.locations if inv.location_label_for(l.get("id")).lower() == k), None)
            if loc:
                return "location", loc["id"]
            name = re.sub(r"\s+\((weldment|assembly)\)$", "", k)
            if len(by_name.get(name, [])) == 1:
                return "part", by_name[name][0]["id"]
    return None


def quality(inv: M.Inventory, kind: str, id_: str) -> str:
    """Rarity-style colour class for a link (see ui.chat_panel)."""
    if kind == "part":
        p = inv.part(id_)
        if p is None:
            return "gone"
        if p.get("isWeldment") or p.get("isAssembly") or p.get("components"):
            return "epic"
        return {"manufactured": "rare", "purchased": "uncommon", "fastener": "common"}.get(p.get("type"), "common")
    exists = link_for(inv, kind, id_) is not None
    if not exists:
        return "gone"
    return {"project": "legendary", "production": "artifact", "order": "heirloom", "vendor": "poor",
            "location": "poor"}.get(kind, "common")


def tooltip(inv: M.Inventory, kind: str, id_: str) -> str:
    """Hover card for a link: the record as it is now."""
    if kind == "part":
        p = inv.part(id_)
        if p is None:
            return "This part no longer exists."
        lines = [f"<b>{M.label(p)}</b>", M.TYPE_LABEL.get(p.get("type"), "")]
        if p.get("description"):
            lines.append(p["description"])
        lines.append(f"Stock {M.num(p.get('currentStock')):g} {p.get('unit') or ''} · reorder at "
                     f"{M.num(p.get('reorderPoint')):g}")
        chips = inv.project_chips(p)
        if chips:
            lines.append("Projects: " + ", ".join(chips))
        loc = inv.location_label_for(p.get("locationId"))
        if loc:
            lines.append(f"Bin {loc}")
        b = inv.best_supplier(p)
        if b:
            lines.append(f"Vendor {b['name']}")
        return "<br>".join(x for x in lines if x)
    if kind == "project":
        pr = inv.project(id_)
        if pr is None:
            return "This project no longer exists."
        s = inv.project_summary(id_)
        return f"<b>{pr['name']}</b><br>{s['parts']} parts · {M.fmt_money(s['value'])}" + \
               (f"<br>{s['short']} short" if s["short"] else "")
    if kind == "production":
        from . import orders as O
        o = next((x for x in inv.productionOrders if x.get("id") == id_), None)
        if o is None:
            return "This production order no longer exists."
        return (f"<b>{inv.project_name(o.get('projectId')) or '?'} ×{M.num(o.get('qty')):g}</b><br>{O.stage(inv, o)}"
                + (f"<br>Finish by {M.fmt_date(o['finishBy'])}" if o.get("finishBy") else "")
                + (f"<br>Due {M.fmt_date(o['dueDate'])}" if o.get("dueDate") else ""))
    if kind == "order":
        from . import orders as O
        o = next((x for x in inv.clientOrders if x.get("id") == id_), None)
        if o is None:
            return "This order no longer exists."
        lines = [f"<b>{O.order_name(inv, o) or 'Order'}</b>" + (f" · PO {o['poNumber']}" if o.get("poNumber") else ""),
                 O.status(o)]
        for ln in o.get("projectLines") or []:
            lines.append(f"{inv.project_name(ln.get('projectId')) or '?'} ×{M.num(ln.get('qty')):g}")
        return "<br>".join(lines)
    if kind == "vendor":
        return f"<b>{id_}</b><br>Vendor"
    if kind == "location":
        return f"<b>{inv.location_label_for(id_) or id_}</b><br>Bin"
    return ""


def new_message(by: str, body: str, channel: str = "general") -> dict:
    body = (body or "").strip()
    if not body:
        raise ValueError("Empty message")
    if len(body) > MAX_LEN:
        raise ValueError(f"Messages are limited to {MAX_LEN} characters")
    return {"id": str(uuid.uuid4()), "at": M.now_ms(), "by": by or "unknown", "channel": channel, "body": body}


def mentions(body: str, name: str) -> bool:
    return bool(name) and re.search(r"@" + re.escape(name.split()[0]) + r"\b", plain(body), re.I) is not None


# ------------------------------------------------------------------ backends
def ts_ms(text: str) -> int:
    """Server timestamp → epoch ms. Any fraction length, Z or ±hh:mm (Python 3.10's fromisoformat isn't that
    forgiving: Postgres drops trailing zeros, e.g. .38047)."""
    import datetime as dt
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d(?::?\d\d)?)?",
                     (text or "").strip())
    if not m:
        raise ValueError(f"Bad timestamp {text!r}")
    base = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    frac = int(((m.group(3) or "") + "000000")[:6])
    tz = m.group(4) or "Z"
    off = 0
    if tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        hh, mm = int(tz[1:3]), int(tz[-2:]) if len(tz) > 3 else 0
        off = sign * (hh * 60 + mm)
    epoch = dt.datetime(1970, 1, 1)
    return int(((base - epoch).total_seconds() - off * 60) * 1000) + frac // 1000


def iso_utc(ms: int) -> str:
    """Epoch ms → ISO UTC (no fromtimestamp: Windows rejects times before 1970)."""
    import datetime as dt
    t = dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=max(0, int(ms)))
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class FileChat:
    """Append-only JSON lines next to the inventory file."""

    kind = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, since_ms: int = 0) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("deleted"):
                    out = [x for x in out if x["id"] != m["id"]]
                    continue
                if m.get("at", 0) > since_ms:
                    out.append(m)
        return out

    def post(self, msg: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return msg

    def delete(self, msg_id: str):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": msg_id, "deleted": True}) + "\n")


class ServerChat:
    """The dims_chat table (needs the chat update on the server: dims_schema_version() >= 3)."""

    kind = "server"

    def __init__(self, store):
        self.store = store

    @property
    def ready(self) -> bool:
        return getattr(self.store, "schema", 0) >= 3

    @staticmethod
    def _row(r: dict) -> dict:
        return {"id": r["id"], "at": ts_ms(r["at"]), "by": r.get("by_name") or "?",
                "channel": r.get("channel") or "general", "body": r.get("body") or "", "userId": r.get("user_id")}

    def fetch(self, since_ms: int = 0) -> list[dict]:
        since = iso_utc(since_ms)
        from urllib.parse import quote
        code, rows = self.store._rest("GET", f"/rest/v1/dims_chat?select=*&at=gt.{quote(since)}&order=at.asc"
                                             "&limit=500")
        if code != 200 or not isinstance(rows, list):
            from .supabase import SyncError
            raise SyncError(f"Chat: server answered {code}")
        return [self._row(r) for r in rows]

    def post(self, msg: dict) -> dict:
        code, res = self.store._rest("POST", "/rest/v1/dims_chat",
                                     {"id": msg["id"], "by_name": msg["by"][:80], "channel": msg["channel"],
                                      "body": msg["body"]}, {"Prefer": "return=representation"})
        self.store._check_write(code, res, "chat")
        return self._row(res[0]) if isinstance(res, list) and res else msg

    def delete(self, msg_id: str):
        code, res = self.store._rest("DELETE", f"/rest/v1/dims_chat?id=eq.{msg_id}")
        self.store._check_write(code, res, "chat")


def chat_for(store):
    """The chat backend for an inventory store (None when it can't have one)."""
    if getattr(store, "kind", "file") == "file" and getattr(store, "path", None):
        p = Path(store.path)
        return FileChat(p.with_name(p.stem + ".chat.jsonl"))
    if hasattr(store, "_check_write"):
        return ServerChat(store)
    return None
