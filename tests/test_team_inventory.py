"""DIMS team inventory: three-way merge, Supabase store (against a local stand-in for Supabase's auth + REST
API), shared-file merge, offline edits."""
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daedalus.inventory import model as M
from daedalus.inventory.merge import merge_list, merge_record, merge_values
from daedalus.inventory.store import InventoryStore
from daedalus.inventory.supabase import AuthError, SupabaseStore, SyncError

ANON = "anon-key-123"


class FakeSupabase:
    """GoTrue password/refresh grants + PostgREST select / insert / conditional patch on app_storage."""

    def __init__(self, users):
        self.users = users                       # email → password
        self.rows: dict[str, dict] = {}
        self.tokens: dict[str, float] = {}      # access token → expiry
        self.refresh: dict[str, str] = {}       # refresh token → email
        self.lock = threading.Lock()
        self.down = False
        self.writes = 0
        self.token_life = 3600
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _authed(self):
                if self.headers.get("apikey") != ANON:
                    self._send(401, {"message": "bad apikey"})
                    return False
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                if fake.tokens.get(tok, 0) < time.time():
                    self._send(401, {"code": "PGRST301", "message": "JWT expired"})
                    return False
                return True

            def _issue(self, email):
                at, rt = f"at-{time.time_ns()}", f"rt-{time.time_ns()}"
                fake.tokens[at] = time.time() + fake.token_life
                fake.refresh[rt] = email
                return {"access_token": at, "refresh_token": rt, "expires_in": fake.token_life,
                        "user": {"email": email, "user_metadata": {"full_name": email.split("@")[0].title()}}}

            def do_POST(self):                   # noqa: N802
                if fake.down:
                    return self._send(503, {"message": "down"})
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/auth/v1/token":
                    b = self._body()
                    if q["grant_type"][0] == "password":
                        if fake.users.get(b.get("email")) != b.get("password"):
                            return self._send(400, {"error_description": "Invalid login credentials"})
                        return self._send(200, self._issue(b["email"]))
                    email = fake.refresh.pop(b.get("refresh_token"), None)
                    if not email:
                        return self._send(400, {"error_description": "Invalid Refresh Token"})
                    return self._send(200, self._issue(email))
                if u.path == "/auth/v1/logout":
                    return self._send(204)
                if u.path == "/rest/v1/app_storage":
                    if not self._authed():
                        return
                    b = self._body()
                    with fake.lock:
                        if b["key"] in fake.rows:
                            return self._send(409, {"code": "23505", "message": "duplicate key"})
                        fake.rows[b["key"]] = {"key": b["key"], "value": b["value"], "updated_at": b["updated_at"]}
                        fake.writes += 1
                        return self._send(201, [dict(fake.rows[b["key"]])])
                self._send(404)

            def do_GET(self):                    # noqa: N802
                if fake.down:
                    return self._send(503, {"message": "down"})
                if not self._authed():
                    return
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                cols = q.get("select", ["key,value,updated_at"])[0].split(",")
                keys = None
                if "key" in q:
                    m = re.match(r"in\.\((.*)\)", q["key"][0])
                    keys = [k.strip('"') for k in m.group(1).split(",")]
                with fake.lock:
                    rows = [{c: r[c] for c in cols} for k, r in fake.rows.items() if keys is None or k in keys]
                self._send(200, rows)

            def do_PATCH(self):                  # noqa: N802
                if fake.down:
                    return self._send(503, {"message": "down"})
                if not self._authed():
                    return
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                key = q["key"][0][3:]
                ver = q["updated_at"][0][3:]
                b = self._body()
                with fake.lock:
                    r = fake.rows.get(key)
                    if r is None or r["updated_at"] != ver:
                        return self._send(200, [])
                    r.update(value=b["value"], updated_at=b["updated_at"])
                    fake.writes += 1
                    return self._send(200, [dict(r)])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    def value(self, key):
        return json.loads(self.rows[key]["value"])


@pytest.fixture()
def server():
    s = FakeSupabase({"ankur@shop.ca": "pw1", "mike@shop.ca": "pw2"})
    yield s
    s.stop()


def _client(server, tmp_path, email, pw, name):
    st = SupabaseStore(server.url, ANON, cache=tmp_path / f"{name}.json")
    st.sign_in(email, pw)
    st.inv.userName = name
    st.load()
    return st


# ----------------------------------------------------------------- merge rules
def test_merge_rules():
    base = {"id": "p1", "name": "Bolt", "currentStock": 100, "notes": "", "reorderPoint": 10}
    mine = {**base, "currentStock": 95, "notes": "bin A"}
    theirs = {**base, "currentStock": 90, "reorderPoint": 20}
    out = merge_record(base, mine, theirs)
    assert out == {"id": "p1", "name": "Bolt", "currentStock": 85, "notes": "bin A", "reorderPoint": 20}
    clash = []
    assert merge_record(base, {**base, "name": "A"}, {**base, "name": "B"}, clash)["name"] == "A" and clash
    b = [{"id": "1", "v": 1}, {"id": "2", "v": 2}, {"id": "3", "v": 3}]
    m = [{"id": "1", "v": 1}, {"id": "3", "v": 3}, {"id": "4", "v": 4}]           # deleted 2, added 4
    t = [{"id": "1", "v": 10}, {"id": "2", "v": 2}, {"id": "3", "v": 3}, {"id": "5", "v": 5}]
    assert merge_list(b, m, t) == [{"id": "1", "v": 10}, {"id": "3", "v": 3}, {"id": "5", "v": 5},
                                   {"id": "4", "v": 4}]
    t2 = [{"id": "1", "v": 1}, {"id": "2", "v": 99}, {"id": "3", "v": 3}]          # they edited the one we deleted
    assert {"id": "2", "v": 99} in merge_list(b, m, t2)
    vals = merge_values({"exchange-rate": "1.35"}, {"exchange-rate": "1.40"}, {"exchange-rate": "1.35"})
    assert vals["exchange-rate"] == "1.40"


# ------------------------------------------------------------------- supabase
def test_sign_in_errors(server, tmp_path):
    st = SupabaseStore(server.url, ANON, cache=tmp_path / "c.json")
    with pytest.raises(AuthError, match="Invalid login"):
        st.sign_in("ankur@shop.ca", "wrong")
    with pytest.raises(AuthError):
        st.load()
    st.sign_in("ankur@shop.ca", "pw1")
    assert st.signed_in and st.email == "ankur@shop.ca" and st.inv.userName == "Ankur"


def test_two_users_edit_at_once(server, tmp_path):
    a = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    p = M.new_part(name="M8 bolt", currentStock=100)
    a.inv.parts.append(p)
    a.save()
    b = _client(server, tmp_path, "mike@shop.ca", "pw2", "Mike")
    assert b.inv.part(p["id"])["currentStock"] == 100
    # both take stock at the same time, and each adds a part
    a.inv.record(p["id"], "usage", 5)
    a.inv.parts.append(M.new_part(name="Washer"))
    b.inv.record(p["id"], "usage", 7)
    b.inv.part(p["id"])["notes"] = "moved to rack B"
    b.inv.parts.append(M.new_part(name="Nut"))
    a.save()
    b.save()                                     # b's version is stale → fetch, merge, write
    srv = {x["name"]: x for x in server.value("parts")}
    assert srv["M8 bolt"]["currentStock"] == 88 and srv["M8 bolt"]["notes"] == "moved to rack B"
    assert {"Washer", "Nut"} <= set(srv)
    assert len(server.value("transactions")) == 2
    assert b.inv.part(p["id"])["currentStock"] == 88       # b sees the merged result
    assert a.check_external() and a.inv.part(p["id"])["currentStock"] == 88
    assert not a.check_external()                          # nothing new


def test_only_changed_collections_are_written(server, tmp_path):
    a = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    a.inv.parts.append(M.new_part(name="Pin"))
    a.save()
    n = server.writes
    a.inv.vendors.append({"id": "v1", "name": "Fastenal"})
    a.save()
    assert server.writes == n + 1 and a.pending() == []


def test_token_refresh_and_expiry(server, tmp_path):
    server.token_life = 61                       # client refreshes when <60 s are left
    a = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    old = a.session["access_token"]
    a.session["expires_at"] = time.time() - 1
    a.inv.parts.append(M.new_part(name="Spring"))
    a.save()
    assert a.session["access_token"] != old
    a.session = {"access_token": "dead", "refresh_token": "bogus", "expires_at": time.time() + 999}
    with pytest.raises(AuthError):
        a.save() if a.pending() else (a.inv.parts.append(M.new_part(name="X")), a.save())


def test_offline_edits_are_kept_and_sent_later(server, tmp_path):
    a = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    server.down = True
    a.inv.parts.append(M.new_part(name="Offline part"))
    with pytest.raises(SyncError):
        a.save()
    assert a.pending() == ["parts"] and not a.check_external()
    # restart while offline: the cache brings the edit back
    a2 = SupabaseStore(server.url, ANON, session=a.session, cache=tmp_path / "Ankur.json")
    assert any(p["name"] == "Offline part" for p in a2.inv.parts) and a2.pending() == ["parts"]
    server.down = False
    assert a2.check_external()                   # back online: sends the waiting edit
    assert any(p["name"] == "Offline part" for p in server.value("parts")) and a2.pending() == []


def test_upload_existing_local_inventory(server, tmp_path):
    local = InventoryStore(tmp_path / "local.json")
    local.inv.parts.append(M.new_part(name="From file", currentStock=3))
    local.save()
    team = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    team.upload_all(local.inv.dump_values())
    assert server.value("parts")[0]["name"] == "From file"


# ------------------------------------------------------------------- shared file
def test_shared_file_merges_instead_of_overwriting(tmp_path):
    f = tmp_path / "shared.json"
    a = InventoryStore(f)
    p = M.new_part(name="Bracket", currentStock=20)
    a.inv.parts.append(p)
    a.save()
    b = InventoryStore(f)
    time.sleep(0.02)
    a.inv.record(p["id"], "usage", 2)
    a.save()
    b.inv.record(p["id"], "receiving", 10)       # b hasn't seen a's save
    time.sleep(0.02)
    b.save()
    c = InventoryStore(f)
    assert c.inv.part(p["id"])["currentStock"] == 28 and len(c.inv.transactions) == 2


# ------------------------------------------------------------------- UI
def test_inventory_page_in_team_mode(server, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    from daedalus.ui.inventory_ui import InventoryPage
    a = _client(server, tmp_path, "ankur@shop.ca", "pw1", "Ankur")
    page = InventoryPage(store=a, lite=True)
    page.inv.parts.append(M.new_part(name="Gusset", currentStock=4))
    page.commit("Added")
    assert server.value("parts")[0]["name"] == "Gusset"
    assert "Team" in page.sync_lab.text() and "synced" in page.sync_lab.text()
    # a teammate adds a part; the background check brings it in
    b = _client(server, tmp_path, "mike@shop.ca", "pw2", "Mike")
    b.inv.parts.append(M.new_part(name="Caster"))
    b.save()
    page._on_fetched(("ok", a, a.fetch_changes()))
    assert {p["name"] for p in page.inv.parts} == {"Gusset", "Caster"}
    # offline: commit keeps the edit, the label says so, and it goes up when the server is back
    server.down = True
    page.inv.parts.append(M.new_part(name="Offline"))
    page.commit("Added")
    assert "offline" in page.sync_lab.text()
    server.down = False
    page._on_fetched(("ok", a, a.fetch_changes()))
    assert "Offline" in {p["name"] for p in server.value("parts")} and "synced" in page.sync_lab.text()
    page.goto("sync")
    page.refresh_current()
    s = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    from daedalus.inventory import backends as B
    B.save_team_config(s, server.url, ANON)
    st = B.open_team(s)
    st.sign_in("mike@shop.ca", "pw2")
    assert "rt-" in s.value("inventory/supabase_session") and "pw2" not in str(s.value("inventory/supabase_session"))
    s.setValue("inventory/backend", "supabase")
    from daedalus.inventory.store import set_store
    set_store(None)
    st2 = B.open_store(s)                          # next start: signed in from the saved refresh token
    assert st2.kind == "supabase" and {"Gusset", "Caster", "Offline"} <= {p["name"] for p in st2.inv.parts}
    set_store(None)
