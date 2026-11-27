"""Supplier catalogs: the McMaster-Carr API client (sign-in, subscribe on first lookup, price tiers, discontinued,
token refresh, CAD), DIMS parts from it, price refresh, the client certificate, and the dims_catalog tool."""
import datetime as dt
import json
import ssl
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daedalus.inventory import catalog as CAT
from daedalus.inventory import model as M

PRODUCTS = {
    "91290A396": {"PartNumber": "91290A396", "ProductStatus": "Active",
                  "FamilyDescription": "Black-Oxide Alloy Steel Socket Head Screw",
                  "DetailDescription": "M8 x 1.25 mm Thread, 25 mm Long",
                  "Specifications": [{"Attribute": "Thread Size", "Values": ["M8"]},
                                     {"Attribute": "Length", "Values": ["25 mm"]}],
                  "Links": [{"Key": "Price", "Value": "/v1/products/91290A396/price"},
                            {"Key": "3-D STEP", "Value": "/v1/cad/library/91290A396.STEP"},
                            {"Key": "2-D DWG", "Value": "/v1/cad/library/91290A396.DWG"}]},
    "6455K16": {"PartNumber": "6455K16", "ProductStatus": "Inactive", "FamilyDescription": "Ball Bearing",
                "DetailDescription": "Sealed, for 25 mm shaft", "Specifications": [],
                "Links": [{"Key": "SuggestedProduct", "Value": "6455K116"}],
                "SuggestedProductDifferences": [{"Attribute": "Seal", "DiscontinuedProductValue": "Rubber",
                                                 "SuggestedProductValue": "Metal"}]},
}
PRICES = {"91290A396": [{"Amount": 12.5, "MinimumQuantity": 1, "UnitOfMeasure": "Pack of 50"},
                        {"Amount": 11.0, "MinimumQuantity": 10, "UnitOfMeasure": "Pack of 50"}], "6455K16": []}


def fake_mcmaster():
    st = {"subs": set(), "logins": 0, "calls": [], "expire_next": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, code, d=None):
            b = json.dumps(d).encode() if d is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)

        def _auth(self):
            if st["expire_next"]:
                st["expire_next"] = False
                self._j(403, {"ErrorCode": 403, "ErrorMessage": "EXPIRED_AUTHORIZATION_TOKEN"})
                return False
            if self.headers.get("Authorization") != f"Bearer T{st['logins']}":
                self._j(403, {"ErrorCode": 403, "ErrorMessage": "EXPIRED_AUTHORIZATION_TOKEN"})
                return False
            return True

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/v1/login":
                if body.get("Password") != "pw":
                    return self._j(401, {"ErrorCode": 401, "ErrorMessage": "UNAUTHORIZED",
                                         "ErrorDescription": "Bad user name or password"})
                st["logins"] += 1
                return self._j(200, {"AuthToken": f"T{st['logins']}", "ExpirationTS": "x"})
            self._j(204)

        def do_PUT(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            if not self._auth():
                return
            pn = body["URL"].rsplit("/", 1)[-1]
            st["subs"].add(pn)
            self._j(201, PRODUCTS[pn])

        def do_GET(self):  # noqa: N802
            st["calls"].append(self.path)
            if not self._auth():
                return
            if self.path.startswith("/v1/cad/"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ISO-10303-21;")
                return
            parts = self.path.split("/")
            pn = parts[3]
            if pn not in st["subs"]:
                return self._j(403, {"ErrorCode": 403, "ErrorMessage": "NOT_SUBSCRIBED_TO_PRODUCT"})
            if self.path.endswith("/price"):
                return self._j(200, PRICES[pn])
            self._j(200, PRODUCTS[pn])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prov = CAT.McMaster("", "", "me@rangate.example", "pw", base=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                        ctx=ssl.create_default_context())
    return srv, prov, st


@pytest.fixture
def mc():
    srv, prov, st = fake_mcmaster()
    yield prov, st
    srv.shutdown()


def test_lookup_subscribe_price_tiers(mc):
    prov, st = mc
    i = prov.product("91290A396")
    assert "91290A396" in st["subs"] and st["logins"] == 1
    assert i["description"] == "M8 x 1.25 mm Thread, 25 mm Long" and i["specs"]["Thread Size"] == "M8"
    assert i["tiers"] == [{"min": 1, "price": 12.5, "unit": "Pack of 50"}, {"min": 10, "price": 11.0,
                                                                          "unit": "Pack of 50"}]
    assert CAT.unit_price(i, 1) == 12.5 and CAT.unit_price(i, 25) == 11.0 and CAT.part_type(i) == "fastener"
    assert i["step"].endswith(".STEP") and i["active"] and i["url"] == "https://www.mcmaster.com/91290A396/"
    st["expire_next"] = True                                     # token expired → signs in again, once
    prov.product("91290A396")
    assert st["logins"] == 2
    d = prov.product("6455K16")
    assert not d["active"] and d["suggested"] == "6455K116" and d["tiers"] == []
    assert prov.file(i["step"]) == b"ISO-10303-21;"
    bad = CAT.McMaster("", "", "me", "nope", base=prov.base, ctx=ssl.create_default_context())
    with pytest.raises(CAT.CatalogError, match="Bad user name"):
        bad.product("91290A396")


def test_into_dims_and_refresh(mc):
    prov, st = mc
    inv = M.Inventory()
    p = CAT.new_part(inv, prov.product("91290A396"))
    s = p["suppliers"][0]
    assert p["type"] == "fastener" and s["name"] == "McMaster-Carr" and s["price"] == 12.5 and s["currency"] == "USD"
    assert inv.vendors[0]["name"] == "McMaster-Carr" and "Thread Size: M8" in p["notes"]
    old = M.new_part(id="b1", name="Bearing", reorderQty=4,
                     suppliers=[{"name": "McMaster-Carr", "partNumber": "6455K16", "price": 20}])
    inv.parts.append(old)
    p["reorderQty"] = 20
    r = CAT.refresh_prices(inv, prov)
    assert r["updated"] == 2 and r["discontinued"] == [{"part": "Bearing", "partNumber": "6455K16",
                                                         "suggested": "6455K116"}]
    assert p["suppliers"][0]["price"] == 11.0                     # 20 per reorder → the 10+ tier
    assert old["suppliers"][0]["discontinued"] and old["suppliers"][0]["suggested"] == "6455K116"
    assert CAT.search_url("Misumi", "SFJ 10-100") == "https://us.misumi-ec.com/vona2/result/?Keyword=SFJ%2010-100"


def test_client_certificate_pfx(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rangate")])
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
           .serial_number(1).not_valid_before(dt.datetime(2026, 1, 1)).not_valid_after(dt.datetime(2030, 1, 1))
           .sign(key, hashes.SHA256()))
    pfx = tmp_path / "mcmaster.pfx"
    pfx.write_bytes(pkcs12.serialize_key_and_certificates(b"r", key, crt, None,
                                                          serialization.BestAvailableEncryption(b"secret")))
    assert isinstance(CAT._ssl_context(str(pfx), "secret"), ssl.SSLContext)
    with pytest.raises(CAT.CatalogError, match="password"):
        CAT._ssl_context(str(pfx), "wrong")
    with pytest.raises(CAT.CatalogError, match="not found"):
        CAT._ssl_context(str(tmp_path / "nope.pfx"), "")


def test_dims_catalog_tool(mc, monkeypatch):
    from daedalus.ai.dims_tools import DimsToolExecutor
    prov, st = mc
    inv = M.Inventory()
    store = types.SimpleNamespace(inv=inv, save=lambda: None)
    ex = DimsToolExecutor(lambda: store)
    out = json.loads(ex.run("dims_catalog", {"vendor_part_number": "91290A396"})[0])
    assert out["ok"] is False and "mcmaster.com/91290A396" in out["note"]
    ex.catalog_provider = prov
    out = json.loads(ex.run("dims_catalog", {"vendor_part_number": "91290A396", "action": "add"})[0])
    assert out["ok"] and inv.parts[0]["suppliers"][0]["partNumber"] == "91290A396"
    out, err = ex.run("dims_catalog", {"action": "refresh"})
    assert not err and json.loads(out)["updated"] == 1


def test_catalog_dialog(mc, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.ui import catalog_ui
    prov, st = mc
    inv = M.Inventory()
    commits = []
    page = types.SimpleNamespace(inv=inv, commit=lambda m, undo=None: commits.append(m))
    host = QWidget()
    d = catalog_ui.CatalogDialog(page, parent=host, provider=prov)
    d.pn.setText("91290A396")
    d.lookup()
    assert "91290A396" in d.title.text() and d.table.rowCount() == 4 and d.b_add.isEnabled() and d.b_lib.isEnabled()
    d.add_part()
    assert commits and inv.parts and "Already" not in d.note.text()
    d.lookup()
    assert d.note.text().startswith("Already in DIMS")
