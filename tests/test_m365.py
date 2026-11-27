"""Microsoft 365: Teams alerts (adaptive card, events, no repeats, the inventory watcher) and OneDrive workbook
sync (device-code sign-in, link → file, pull into DIMS, push with the version check) against a fake Microsoft."""
import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daedalus import m365
from daedalus.inventory import model as M


def fake_ms(workbook: bytes):
    st = {"posts": [], "polls": 0, "file": workbook, "etag": '"v1"', "puts": [], "auth": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, code, d, headers=None):
            b = json.dumps(d).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):  # noqa: N802
            body = self._body()
            if self.path == "/teams":
                st["posts"].append(json.loads(body))
                return self._j(202, {})
            form = dict(x.split("=", 1) for x in body.decode().split("&"))
            if self.path.endswith("/devicecode"):
                return self._j(200, {"device_code": "DC", "user_code": "ABCD-1234", "interval": 0, "expires_in": 60,
                                     "verification_uri": "https://microsoft.com/devicelogin"})
            if self.path.endswith("/token"):
                if form["grant_type"].endswith("device_code"):
                    st["polls"] += 1
                    if st["polls"] < 2:
                        return self._j(400, {"error": "authorization_pending"})
                    return self._j(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
                return self._j(200, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})
            self._j(404, {})

        def do_GET(self):  # noqa: N802
            st["auth"].append(self.headers.get("Authorization"))
            if self.path.startswith("/v1.0/shares/u!"):
                return self._j(200, {"id": "ITEM", "name": "Hardware.xlsx", "eTag": st["etag"], "webUrl": "w",
                                     "parentReference": {"driveId": "DRV"}, "lastModifiedDateTime": "2026-11-20",
                                     "lastModifiedBy": {"user": {"displayName": "Jess"}}})
            if self.path == "/v1.0/drives/DRV/items/ITEM":
                return self._j(200, {"eTag": st["etag"]})
            if self.path == "/v1.0/drives/DRV/items/ITEM/content":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(st["file"])
                return
            self._j(404, {})

        def do_PUT(self):  # noqa: N802
            body = self._body()
            if self.headers.get("If-Match") and self.headers["If-Match"] != st["etag"]:
                return self._j(412, {"error": {"code": "resourceModified", "message": "changed"}})
            st["puts"].append(body)
            st["file"], st["etag"] = body, '"v2"'
            self._j(200, {"eTag": st["etag"]})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", st


@pytest.fixture
def ms(monkeypatch):
    inv = M.Inventory()
    inv.parts = [M.new_part(id="p1", name="M8 nut", type="fastener", currentStock=40, reorderPoint=20)]
    buf = io.BytesIO()
    import tempfile
    import os
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    inv.export_file(tmp)
    wb = open(tmp, "rb").read()
    os.remove(tmp)
    srv, base, st = fake_ms(wb)
    monkeypatch.setattr(m365, "GRAPH", base + "/v1.0")
    monkeypatch.setattr(m365, "LOGIN", base)
    monkeypatch.setattr(m365, "_OVERRIDE", {"teams/enabled": True, "teams/webhook": base + "/teams",
                                           "teams/events": "low_stock,production_done,rfq,released",
                                           "m365/client_id": "CID", "m365/tenant": "organizations",
                                           "m365/refresh": "", "m365/workbook": "https://x.sharepoint.com/:x:/r/H",
                                           "m365/etag": "", "m365/pulled": ""})
    monkeypatch.setattr(m365, "_sent", {})
    m365._token.update(access="", exp=0)
    yield base, st, buf
    srv.shutdown()


def test_teams_card_events_and_repeats(ms):
    base, st, _ = ms
    assert m365.notify("rfq", "Quotes requested: RFQ-0001", "", {"Fastenal": "3 line(s)"}, key="RFQ-0001", wait=True)
    assert not m365.notify("rfq", "Quotes requested: RFQ-0001", key="RFQ-0001", wait=True)     # no repeat
    c = st["posts"][0]["attachments"][0]
    assert c["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert c["content"]["body"][0]["text"] == "Quotes requested: RFQ-0001"
    assert c["content"]["body"][1]["facts"] == [{"title": "Fastenal", "value": "3 line(s)"}]
    m365._OVERRIDE["teams/events"] = "low_stock"
    assert not m365.notify("rfq", "x", wait=True)
    m365._OVERRIDE["teams/enabled"] = False
    assert not m365.notify("low_stock", "x", wait=True)


def test_inventory_watcher(ms, monkeypatch):
    base, st, _ = ms
    calls = []
    monkeypatch.setattr(m365, "notify", lambda *a, **k: calls.append((a, k)) or True)
    inv = M.Inventory()
    inv.parts = [M.new_part(id="p1", name="M8 nut", currentStock=30, reorderPoint=20),
                 M.new_part(id="p2", name="Bearing", currentStock=1, reorderPoint=2)]
    inv.productionOrders = [{"id": "o1", "status": "In Progress", "qty": 2, "name": "RFP 1308"}]
    w = m365.InventoryWatcher()
    assert w.check(inv) == []                                    # first look: no alerts for what's already low
    inv.record("p1", "usage", 12)
    inv.productionOrders[0]["status"] = "Complete"
    assert w.check(inv) == ["p1", "o1"]
    assert calls[0][0][1] == "Low stock: M8 nut" and calls[0][0][3]["On hand"] == "18 ea"
    assert calls[1][0][0] == "production_done"
    assert w.check(inv) == []


def test_sign_in_pull_push(ms):
    base, st, _ = ms
    dc = m365.device_code_start()
    assert dc["user_code"] == "ABCD-1234"
    tok = m365.device_code_poll(dc, sleep=lambda s: None)
    assert tok["access_token"] == "AT1" and m365.signed_in() and st["polls"] == 2
    assert m365.share_id("https://a/b") == "u!" + base64.urlsafe_b64encode(b"https://a/b").decode().rstrip("=")
    inv = M.Inventory()
    r = m365.pull(inv)
    assert r == {"file": "Hardware.xlsx", "added": 1, "updated": 0, "by": "Jess", "modified": "2026-11-20"}
    assert inv.parts[0]["name"] == "M8 nut" and m365.setting("m365/etag") == '"v1"' and st["auth"][0] == "Bearer AT1"
    inv.parts[0]["currentStock"] = 7
    out = m365.push(inv)
    assert out["file"] == "Hardware.xlsx" and st["puts"] and st["etag"] == '"v2"'
    st["etag"] = '"v3"'                                          # a teammate saved it meanwhile
    with pytest.raises(m365.M365Error, match="pull first"):
        m365.push(inv)
    m365._token.update(access="", exp=0)                         # expired → refresh token
    m365.pull(inv)
    assert st["auth"][-1] == "Bearer AT2" and m365.setting("m365/refresh") == "RT2"
    m365.sign_out()
    assert not m365.signed_in()
    with pytest.raises(m365.M365Error, match="Not signed in"):
        m365.pull(inv)


def test_no_client_id(ms):
    m365._OVERRIDE["m365/client_id"] = ""
    with pytest.raises(m365.M365Error, match="client"):
        m365.device_code_start()
