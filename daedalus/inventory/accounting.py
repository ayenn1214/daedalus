"""Accounting link (off unless turned on in Settings ▸ Accounting): QuickBooks Online or Xero.

- Push a DIMS purchase order into the accounting system as a purchase order (vendor matched by name, or created;
  lines matched to items by the part's QuickBooks part number / Xero item code, else posted to the expense account).
- Pull vendors (name, email, phone, address) and item purchase costs back into DIMS.

Sign-in is OAuth 2.0 in the browser with your organisation's app (QuickBooks: client ID + secret from
developer.intuit.com; Xero: client ID of an "Auth code with PKCE" app from developer.xero.com), redirected to
http://localhost:<port>/callback on this PC. Tokens stay on this PC. Qt-free."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import model as M

PROVIDERS = [("qbo", "QuickBooks Online"), ("xero", "Xero")]
QBO_AUTH = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API = "https://quickbooks.api.intuit.com"
QBO_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"
QBO_SCOPE = "com.intuit.quickbooks.accounting"
XERO_AUTH = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN = "https://identity.xero.com/connect/token"
XERO_API = "https://api.xero.com"
XERO_SCOPE = "offline_access accounting.transactions accounting.contacts accounting.settings.read"


class AccountingError(RuntimeError):
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


def enabled() -> bool:
    v = setting("accounting/enabled", False)
    return v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")


def provider_key() -> str:
    return str(setting("accounting/provider", "qbo") or "qbo")


def redirect_uri() -> str:
    return f"http://localhost:{int(M.num(setting('accounting/port', 8765)) or 8765)}/callback"


# ------------------------------------------------------------------ HTTP
def _req(method, url, data=None, headers=None, form=False):
    h = {"Accept": "application/json", **(headers or {})}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw.decode() or "{}") if raw else {}
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        msg = txt[:300]
        try:
            j = json.loads(txt)
            f = (j.get("Fault") or {}).get("Error") or []
            msg = (f[0].get("Detail") or f[0].get("Message")) if f else (
                j.get("Detail") or j.get("Message") or j.get("error_description") or j.get("error") or msg)
            if j.get("Elements"):
                errs = [v.get("Message") for el in j["Elements"] for v in el.get("ValidationErrors") or []]
                msg = "; ".join(x for x in errs if x) or msg
        except ValueError:
            pass
        ex = AccountingError(f"{e.code}: {msg}")
        ex.status = e.code
        raise ex from None
    except urllib.error.URLError as e:
        raise AccountingError(f"Can't reach the accounting service ({e.reason})") from None


# ------------------------------------------------------------------ OAuth
def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class Provider:
    key = ""
    name = ""

    def __init__(self, client_id=None, client_secret=None, auth_url=None, token_url=None, api=None):
        self.client_id = client_id if client_id is not None else str(setting(f"{self.key}/client_id", "") or "")
        self.client_secret = client_secret if client_secret is not None else \
            str(setting(f"{self.key}/client_secret", "") or "")
        self.auth_base = auth_url or self.AUTH
        self.token_url = token_url or self.TOKEN
        self.api = api or self.default_api()
        self._access, self._exp = "", 0.0

    def default_api(self):
        return ""

    # sign-in
    def authorize_url(self, state: str) -> str:
        q = {"client_id": self.client_id, "response_type": "code", "scope": self.SCOPE,
             "redirect_uri": redirect_uri(), "state": state}
        if self.key == "xero":
            self._verifier, ch = _pkce()
            q.update(code_challenge=ch, code_challenge_method="S256")
        return self.auth_base + "?" + urllib.parse.urlencode(q)

    def _token(self, data: dict) -> dict:
        h = {}
        if self.client_secret:
            h["Authorization"] = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        else:
            data = {**data, "client_id": self.client_id}
        tok = _req("POST", self.token_url, data, h, form=True)
        self._access = tok["access_token"]
        self._exp = time.time() + float(tok.get("expires_in", 1800)) - 60
        if tok.get("refresh_token"):
            set_setting(f"{self.key}/refresh", tok["refresh_token"])
        return tok

    def exchange(self, code: str, extra: dict | None = None) -> dict:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}
        if self.key == "xero":
            data["code_verifier"] = getattr(self, "_verifier", "")
        tok = self._token(data)
        self.after_sign_in(extra or {})
        return tok

    def after_sign_in(self, extra: dict):
        pass

    def access(self) -> str:
        if self._access and time.time() < self._exp:
            return self._access
        rt = setting(f"{self.key}/refresh", "")
        if not rt:
            raise AccountingError(f"Not connected to {self.name} (Settings ▸ Accounting ▸ Connect)")
        self._token({"grant_type": "refresh_token", "refresh_token": rt})
        return self._access

    def connected(self) -> bool:
        return bool(setting(f"{self.key}/refresh", ""))

    def disconnect(self):
        set_setting(f"{self.key}/refresh", "")
        self._access = ""


class QuickBooks(Provider):
    key, name = "qbo", "QuickBooks Online"
    AUTH, TOKEN, SCOPE = QBO_AUTH, QBO_TOKEN, QBO_SCOPE

    def default_api(self):
        sb = setting("qbo/sandbox", False)
        sb = sb if isinstance(sb, bool) else str(sb).lower() in ("true", "1")
        return QBO_SANDBOX if sb else QBO_API

    def after_sign_in(self, extra):
        if extra.get("realmId"):
            set_setting("qbo/realm", extra["realmId"])

    def _url(self, path):
        realm = setting("qbo/realm", "")
        if not realm:
            raise AccountingError("QuickBooks company not chosen — connect again")
        sep = "&" if "?" in path else "?"
        return f"{self.api}/v3/company/{realm}/{path}{sep}minorversion=75"

    def call(self, method, path, data=None):
        tok = self.access()
        return _req(method, self._url(path), data, {"Authorization": f"Bearer {tok}"})

    def query(self, q: str) -> dict:
        return self.call("GET", "query?query=" + urllib.parse.quote(q)).get("QueryResponse") or {}

    @staticmethod
    def _q(s):
        return str(s).replace("'", "\\'")

    def vendor_id(self, name: str, v: dict | None = None) -> str:
        got = self.query(f"select * from Vendor where DisplayName = '{self._q(name)}'").get("Vendor") or []
        if got:
            return got[0]["Id"]
        body = {"DisplayName": name}
        if v:
            if v.get("email"):
                body["PrimaryEmailAddr"] = {"Address": v["email"]}
            if v.get("phone"):
                body["PrimaryPhone"] = {"FreeFormNumber": v["phone"]}
        return self.call("POST", "vendor", body)["Vendor"]["Id"]

    def item_id(self, name: str) -> str | None:
        if not name:
            return None
        got = self.query(f"select * from Item where Name = '{self._q(name)}'").get("Item") or []
        return got[0]["Id"] if got else None

    def create_po(self, inv, po: dict, vendor: str) -> dict:
        v = next((x for x in inv.vendors if (x.get("name") or "").lower() == vendor.lower()), None)
        vid = self.vendor_id(vendor, v)
        acct = str(setting("qbo/expense_account", "") or "")
        lines = []
        for ln in po.get("lines") or []:
            p = inv.part(ln.get("partId")) if ln.get("partId") else None
            qty, rate = M.num(ln.get("qty")), M.num(ln.get("rate"))
            desc = " — ".join(x for x in (ln.get("mpn"), (ln.get("description") or "").replace("\n", " ")) if x)
            item = self.item_id((p or {}).get("qbPartNumber") or "")
            if item:
                lines.append({"DetailType": "ItemBasedExpenseLineDetail", "Amount": round(qty * rate, 2),
                              "Description": desc, "ItemBasedExpenseLineDetail": {
                                  "ItemRef": {"value": item}, "Qty": qty, "UnitPrice": rate}})
            else:
                if not acct:
                    raise AccountingError(f"“{desc or 'a line'}” has no QuickBooks item — set its QuickBooks part "
                                          "number in DIMS, or an expense account in Settings ▸ Accounting")
                lines.append({"DetailType": "AccountBasedExpenseLineDetail", "Amount": round(qty * rate, 2),
                              "Description": f"{qty:g} × {desc} @ {rate:g}", "AccountBasedExpenseLineDetail": {
                                  "AccountRef": {"value": acct}}})
        body = {"VendorRef": {"value": vid}, "Line": lines, "DocNumber": (po.get("number") or "")[:21],
                "TxnDate": po.get("date") or M.today(), "POStatus": "Open"}
        if po.get("job"):
            body["Memo"] = po["job"]
        r = self.call("POST", "purchaseorder", body)["PurchaseOrder"]
        return {"id": r["Id"], "number": r.get("DocNumber"), "total": r.get("TotalAmt"), "system": self.name}

    def vendors(self) -> list[dict]:
        out = []
        for v in self.query("select * from Vendor maxresults 1000").get("Vendor") or []:
            a = v.get("BillAddr") or {}
            out.append({"name": v.get("DisplayName"), "email": (v.get("PrimaryEmailAddr") or {}).get("Address", ""),
                        "phone": (v.get("PrimaryPhone") or {}).get("FreeFormNumber", ""),
                        "address": a.get("Line1", ""), "city": a.get("City", ""),
                        "province": a.get("CountrySubDivisionCode", ""), "postalCode": a.get("PostalCode", ""),
                        "country": a.get("Country", "")})
        return out

    def item_costs(self) -> dict:
        """{item name: purchase cost}"""
        return {i["Name"]: float(i["PurchaseCost"]) for i in
                self.query("select * from Item maxresults 1000").get("Item") or [] if i.get("PurchaseCost")}


class Xero(Provider):
    key, name = "xero", "Xero"
    AUTH, TOKEN, SCOPE = XERO_AUTH, XERO_TOKEN, XERO_SCOPE

    def default_api(self):
        return XERO_API

    def after_sign_in(self, extra):
        cons = _req("GET", f"{self.api}/connections", headers={"Authorization": f"Bearer {self.access()}"})
        if not cons:
            raise AccountingError("No Xero organisation was connected")
        set_setting("xero/tenant", cons[0]["tenantId"])
        set_setting("xero/org", cons[0].get("tenantName", ""))

    def call(self, method, path, data=None):
        t = setting("xero/tenant", "")
        if not t:
            raise AccountingError("Xero organisation not chosen — connect again")
        return _req(method, f"{self.api}/api.xro/2.0/{path}", data,
                    {"Authorization": f"Bearer {self.access()}", "xero-tenant-id": t})

    def contact_id(self, name: str, v: dict | None = None) -> str:
        w = urllib.parse.quote(f'Name=="{name}"')
        got = self.call("GET", f"Contacts?where={w}").get("Contacts") or []
        if got:
            return got[0]["ContactID"]
        body = {"Name": name, "IsSupplier": True}
        if v and v.get("email"):
            body["EmailAddress"] = v["email"]
        return self.call("PUT", "Contacts", {"Contacts": [body]})["Contacts"][0]["ContactID"]

    def create_po(self, inv, po: dict, vendor: str) -> dict:
        v = next((x for x in inv.vendors if (x.get("name") or "").lower() == vendor.lower()), None)
        cid = self.contact_id(vendor, v)
        acct = str(setting("xero/account_code", "") or "")
        items = []
        for ln in po.get("lines") or []:
            p = inv.part(ln.get("partId")) if ln.get("partId") else None
            li = {"Description": " — ".join(x for x in (ln.get("mpn"), (ln.get("description") or "")
                                                        .replace("\n", " ")) if x) or "Item",
                  "Quantity": M.num(ln.get("qty")), "UnitAmount": M.num(ln.get("rate"))}
            code = (p or {}).get("qbPartNumber") or ""
            if code:
                li["ItemCode"] = code
            if acct:
                li["AccountCode"] = acct
            items.append(li)
        body = {"Contact": {"ContactID": cid}, "Date": po.get("date") or M.today(), "LineItems": items,
                "PurchaseOrderNumber": po.get("number") or "", "Reference": po.get("job") or "",
                "Status": "DRAFT", "LineAmountTypes": "Exclusive"}
        r = self.call("PUT", "PurchaseOrders", {"PurchaseOrders": [body]})["PurchaseOrders"][0]
        return {"id": r["PurchaseOrderID"], "number": r.get("PurchaseOrderNumber"), "total": r.get("Total"),
                "system": self.name}

    def vendors(self) -> list[dict]:
        got = self.call("GET", "Contacts?where=" + urllib.parse.quote("IsSupplier==true")).get("Contacts") or []
        out = []
        for c in got:
            a = next((x for x in c.get("Addresses") or [] if x.get("AddressLine1")), {})
            ph = next((x for x in c.get("Phones") or [] if x.get("PhoneNumber")), {})
            out.append({"name": c.get("Name"), "email": c.get("EmailAddress", ""),
                        "phone": " ".join(x for x in (ph.get("PhoneAreaCode"), ph.get("PhoneNumber")) if x),
                        "address": a.get("AddressLine1", ""), "city": a.get("City", ""),
                        "province": a.get("Region", ""), "postalCode": a.get("PostalCode", ""),
                        "country": a.get("Country", "")})
        return out

    def item_costs(self) -> dict:
        return {i["Code"]: float((i.get("PurchaseDetails") or {}).get("UnitPrice"))
                for i in self.call("GET", "Items").get("Items") or []
                if (i.get("PurchaseDetails") or {}).get("UnitPrice") not in (None, "")}


def provider(key: str | None = None, **kw) -> Provider:
    return {"qbo": QuickBooks, "xero": Xero}[key or provider_key()](**kw)


# ------------------------------------------------------------------ the browser sign-in
def sign_in(prov: Provider, open_url, timeout: float = 300.0) -> dict:
    """Open the provider's sign-in page; catch the redirect on localhost; exchange the code."""
    state = secrets.token_urlsafe(16)
    port = int(urllib.parse.urlparse(redirect_uri()).port)
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q and q.get("state") == state
            self.wfile.write(("<html><body style='font-family:sans-serif'><h3>" +
                              ("Daedalus is connected — you can close this tab." if ok else
                               "Sign-in didn't finish — go back to Daedalus.") + "</h3></body></html>").encode())

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    srv.timeout = 1
    open_url(prov.authorize_url(state))
    end = time.time() + timeout
    try:
        while not got and time.time() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if not got:
        raise AccountingError("Sign-in timed out")
    if got.get("error"):
        raise AccountingError(f"Sign-in refused: {got.get('error_description') or got['error']}")
    if got.get("state") != state:
        raise AccountingError("Sign-in answer didn't match — try again")
    return prov.exchange(got["code"], got)


# ------------------------------------------------------------------ DIMS ⇄ accounting
def push_po(inv, po: dict, vendor: str, prov: Provider | None = None) -> dict:
    prov = prov or provider()
    r = prov.create_po(inv, po, vendor)
    r["date"] = M.today()
    meta = inv.syncMeta if isinstance(inv.syncMeta, dict) else {}
    meta.setdefault("accountingPOs", {})[po.get("number") or r["id"]] = {**r, "vendor": vendor}
    inv.syncMeta = meta
    return r


def pull_vendors(inv, prov: Provider | None = None) -> dict:
    prov = prov or provider()
    added = updated = 0
    for x in prov.vendors():
        if not x.get("name"):
            continue
        before = len(inv.vendors)
        v = inv.ensure_vendor(x["name"])
        added += len(inv.vendors) > before
        changed = False
        for k in ("email", "phone", "address", "city", "province", "postalCode", "country"):
            if x.get(k) and not v.get(k):
                v[k] = x[k]
                changed = True
        updated += changed and len(inv.vendors) == before
    return {"added": added, "updated": updated}


def pull_costs(inv, prov: Provider | None = None) -> dict:
    """Unit costs from the accounting items, matched by the part's QuickBooks part number / Xero item code."""
    prov = prov or provider()
    costs = prov.item_costs()
    n = 0
    for p in inv.parts:
        k = p.get("qbPartNumber")
        if k and k in costs:
            p["unitCost"], p["unitCostCurrency"] = costs[k], str(setting("accounting/currency", "CAD") or "CAD")
            p["updatedAt"] = M.now_ms()
            n += 1
    return {"updated": n, "items": len(costs)}
