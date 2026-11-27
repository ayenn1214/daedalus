"""Accounting link: browser sign-in (localhost redirect, PKCE for Xero, realm for QuickBooks), token refresh,
purchase orders pushed (vendor found or created, item vs expense-account lines), vendors and item costs pulled."""
import json
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daedalus.inventory import accounting as ACC
from daedalus.inventory import model as M


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fake_server():
    st = {"tokens": [], "pos": [], "vendors": {"Fastenal": "55"}, "contacts": {}, "created": [], "auth": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, d, code=200):
            b = json.dumps(d).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):  # noqa: N802
            body = self._body()
            if self.path == "/token":
                f = dict(urllib.parse.parse_qsl(body.decode()))
                st["tokens"].append({**f, "auth": self.headers.get("Authorization")})
                n = len(st["tokens"])
                return self._j({"access_token": f"A{n}", "refresh_token": f"R{n}", "expires_in": 3600})
            st["auth"].append(self.headers.get("Authorization"))
            j = json.loads(body)
            if "/vendor" in self.path:
                st["created"].append(j["DisplayName"])
                return self._j({"Vendor": {"Id": "77", "DisplayName": j["DisplayName"]}})
            if "/purchaseorder" in self.path:
                st["pos"].append(j)
                return self._j({"PurchaseOrder": {"Id": "900", "DocNumber": j["DocNumber"], "TotalAmt": 42.0}})
            self._j({}, 404)

        def do_PUT(self):  # noqa: N802
            j = json.loads(self._body())
            st["auth"].append((self.headers.get("Authorization"), self.headers.get("xero-tenant-id")))
            if self.path.endswith("/Contacts"):
                st["created"].append(j["Contacts"][0]["Name"])
                return self._j({"Contacts": [{"ContactID": "C-new"}]})
            if self.path.endswith("/PurchaseOrders"):
                st["pos"].append(j)
                po = j["PurchaseOrders"][0]
                return self._j({"PurchaseOrders": [{"PurchaseOrderID": "PO-1", "Total": 10,
                                                    "PurchaseOrderNumber": po["PurchaseOrderNumber"]}]})
            self._j({}, 404)

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            st["auth"].append(self.headers.get("Authorization"))
            if u.path == "/connections":
                return self._j([{"tenantId": "T1", "tenantName": "Rangate Ltd"}])
            if u.path.endswith("/query"):
                sql = q["query"]
                if "from Vendor where" in sql:
                    name = sql.split("'")[1]
                    return self._j({"QueryResponse": {"Vendor": [{"Id": st["vendors"][name]}]}
                                    if name in st["vendors"] else {}})
                if "from Item where" in sql:
                    return self._j({"QueryResponse": {"Item": [{"Id": "I8"}]} if "F-0825" in sql else {}})
                if "from Vendor maxresults" in sql:
                    return self._j({"QueryResponse": {"Vendor": [
                        {"DisplayName": "Fastenal", "PrimaryEmailAddr": {"Address": "chris@fastenal.example"},
                         "BillAddr": {"Line1": "1 Main", "City": "Burnaby", "CountrySubDivisionCode": "BC"}},
                        {"DisplayName": "Uline"}]}})
                if "from Item maxresults" in sql:
                    return self._j({"QueryResponse": {"Item": [{"Name": "F-0825", "PurchaseCost": 0.19},
                                                                {"Name": "X", "PurchaseCost": 0}]}})
            if u.path.endswith("/Contacts"):
                return self._j({"Contacts": []})
            if u.path.endswith("/Items"):
                return self._j({"Items": [{"Code": "F-0825", "PurchaseDetails": {"UnitPrice": 0.18}}]})
            self._j({}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", st


@pytest.fixture
def acc(monkeypatch):
    srv, base, st = fake_server()
    monkeypatch.setattr(ACC, "_OVERRIDE", {"accounting/enabled": True, "accounting/port": free_port(),
                                          "qbo/client_id": "QC", "qbo/client_secret": "QS", "qbo/refresh": "",
                                          "qbo/realm": "", "qbo/sandbox": False, "qbo/expense_account": "",
                                          "xero/client_id": "XC", "xero/client_secret": "", "xero/refresh": "",
                                          "xero/tenant": "", "xero/org": "", "xero/account_code": "300",
                                          "accounting/currency": "CAD"})
    inv = M.Inventory()
    inv.vendors = [{"id": "v", "name": "Fastenal", "email": ""}, {"id": "w", "name": "New Co", "email": "a@b.c"}]
    inv.parts = [M.new_part(id="p1", name="M8 SHCS", qbPartNumber="F-0825"), M.new_part(id="p2", name="Gas spring")]
    po = {"number": "PO-1001", "date": "2026-11-26", "job": "RFP 1308", "lines": [
        {"partId": "p1", "mpn": "11112233", "description": "F-0825\nM8 SHCS", "qty": 200, "rate": 0.21},
        {"partId": "p2", "mpn": "", "description": "Gas spring", "qty": 2, "rate": 18.5}]}
    yield base, st, inv, po
    srv.shutdown()


def _browser(provider_base):
    """Stands in for the browser: follows the authorize URL straight to the localhost redirect."""
    def open_url(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        cb = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "CODE", "state": q["state"],
                                                                "realmId": "R123"})
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=10).read(), daemon=True).start()
        open_url.q = q
    return open_url


def test_quickbooks(acc):
    base, st, inv, po = acc
    qb = ACC.QuickBooks(auth_url=base + "/auth", token_url=base + "/token", api=base)
    b = _browser(base)
    tok = ACC.sign_in(qb, b, timeout=20)
    assert tok["access_token"] == "A1" and ACC.setting("qbo/realm") == "R123" and ACC.setting("qbo/refresh") == "R1"
    assert b.q["scope"] == ACC.QBO_SCOPE and st["tokens"][0]["auth"].startswith("Basic ")
    with pytest.raises(ACC.AccountingError, match="expense account"):
        ACC.push_po(inv, po, "Fastenal", qb)
    ACC._OVERRIDE["qbo/expense_account"] = "80"
    r = ACC.push_po(inv, po, "Fastenal", qb)
    assert r == {"id": "900", "number": "PO-1001", "total": 42.0, "system": "QuickBooks Online", "date": M.today()}
    body = st["pos"][-1]
    assert body["VendorRef"] == {"value": "55"} and body["DocNumber"] == "PO-1001" and body["Memo"] == "RFP 1308"
    l1, l2 = body["Line"]
    assert l1["DetailType"] == "ItemBasedExpenseLineDetail" and l1["ItemBasedExpenseLineDetail"] == {
        "ItemRef": {"value": "I8"}, "Qty": 200, "UnitPrice": 0.21} and l1["Amount"] == 42.0
    assert l2["DetailType"] == "AccountBasedExpenseLineDetail" and l2["AccountBasedExpenseLineDetail"] == {
        "AccountRef": {"value": "80"}} and l2["Amount"] == 37.0
    assert inv.syncMeta["accountingPOs"]["PO-1001"]["vendor"] == "Fastenal"
    ACC.push_po(inv, {**po, "number": "PO-1002"}, "New Co", qb)
    assert st["created"] == ["New Co"]
    assert ACC.pull_vendors(inv, qb) == {"added": 1, "updated": 1}
    f = next(v for v in inv.vendors if v["name"] == "Fastenal")
    assert f["email"] == "chris@fastenal.example" and f["city"] == "Burnaby"
    assert ACC.pull_costs(inv, qb) == {"updated": 1, "items": 1} and inv.part("p1")["unitCost"] == 0.19
    qb._access, qb._exp = "", 0                                  # expired → refresh token
    ACC.pull_costs(inv, qb)
    assert st["tokens"][-1]["grant_type"] == "refresh_token" and st["tokens"][-1]["refresh_token"] == "R1"


def test_xero(acc):
    base, st, inv, po = acc
    xe = ACC.Xero(auth_url=base + "/auth", token_url=base + "/token", api=base)
    b = _browser(base)
    ACC.sign_in(xe, b, timeout=20)
    assert b.q["code_challenge_method"] == "S256" and ACC.setting("xero/tenant") == "T1"
    t = st["tokens"][0]
    assert t["client_id"] == "XC" and t["code_verifier"] and t["auth"] is None       # PKCE, no secret
    r = ACC.push_po(inv, po, "Fastenal", xe)
    assert r["system"] == "Xero" and r["number"] == "PO-1001" and st["created"] == ["Fastenal"]
    p = st["pos"][-1]["PurchaseOrders"][0]
    assert p["Contact"] == {"ContactID": "C-new"} and p["Status"] == "DRAFT" and p["Reference"] == "RFP 1308"
    assert p["LineItems"][0] == {"Description": "11112233 — F-0825 M8 SHCS", "Quantity": 200, "UnitAmount": 0.21,
                                 "ItemCode": "F-0825", "AccountCode": "300"}
    assert "ItemCode" not in p["LineItems"][1]
    assert st["auth"][-1][1] == "T1"
    assert ACC.pull_costs(inv, xe)["updated"] == 1 and inv.part("p1")["unitCost"] == 0.18


def test_not_connected(acc):
    base, st, inv, po = acc
    with pytest.raises(ACC.AccountingError, match="Not connected"):
        ACC.push_po(inv, po, "Fastenal", ACC.QuickBooks(api=base))
    assert ACC.enabled() and ACC.provider_key() == "qbo" and ACC.redirect_uri().startswith("http://localhost:")


def test_po_dialog_pushes_to_accounting(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import po_ui as PU
    from daedalus.ui.inventory_ui import InventoryPage
    from tests.test_purchase_order import sample
    monkeypatch.setattr(ACC, "_OVERRIDE", {"accounting/enabled": True, "accounting/provider": "xero",
                                          "accounting/auto_po": True, "xero/refresh": "R", "xero/client_id": "X",
                                          "xero/client_secret": ""})
    pushed = []
    monkeypatch.setattr(ACC, "push_po", lambda inv, po, vend, prov=None: pushed.append((po["number"], vend)) or
                        {"system": "Xero", "number": po["number"], "id": "1"})
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = sample()
    st.save()
    page = InventoryPage(st, lite=True)
    p0 = page.inv.parts[0]
    d = PU.PurchaseOrderDialog(page, {"Acme Fab": [(p0, 2)]}, "Acme Fab")
    assert d.acc.isVisible() is False or d.acc.text() == "Also create it in Xero"
    assert d.acc.text() == "Also create it in Xero" and d.acc.isChecked()
    d.save(str(tmp_path / "po.pdf"))
    assert pushed == [("PO-1001", "Acme Fab")]
    ACC._OVERRIDE["accounting/enabled"] = False
    d2 = PU.PurchaseOrderDialog(page, {"Acme Fab": [(p0, 2)]}, "Acme Fab")
    assert d2.acc.text() == ""
