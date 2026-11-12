"""DIMS Supabase backend: the migration on a real Postgres (stock trigger, versions, roles, audit, import) and the
desktop client against it. Skipped when no test Postgres is available (set DIMS_TEST_PG to a libpq DSN)."""
import json
import os
import time
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from daedalus.inventory import model as M  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112000000_dims.sql"
STUB = ROOT / "tests" / "supabase_stub.sql"
ADMIN_DSN = os.environ.get("DIMS_TEST_PG", "host=/tmp/claude-0 port=54329 user=postgres dbname=postgres")

try:
    psycopg.connect(ADMIN_DSN, connect_timeout=2).close()
except Exception:  # noqa: BLE001
    pytest.skip("no test Postgres", allow_module_level=True)

from pg_shim import ANON, Shim, ready_db  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    dsn = ready_db(ADMIN_DSN, "dims_test", str(MIGRATION), str(STUB))
    shim = Shim(dsn)
    ids = {"ankur": shim.add_user("ankur@shop.ca", "pw"), "mike": shim.add_user("mike@shop.ca", "pw"),
           "floor": shim.add_user("floor@shop.ca", "pw"), "view": shim.add_user("view@shop.ca", "pw")}
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute("update dims_profiles set role = 'floor' where email = 'floor@shop.ca'")
        con.execute("update dims_profiles set role = 'viewer' where email = 'view@shop.ca'")
    yield shim, dsn, ids, tmp_path
    shim.stop()


def client(env, who):
    from daedalus.inventory.supabase_rows import SupabaseRowStore
    shim, _dsn, _ids, tmp = env
    st = SupabaseRowStore(shim.url, ANON, cache=tmp / f"{who}.json")
    st.sign_in(f"{who}@shop.ca", "pw")
    st.inv.userName = who.title()
    st.load()
    return st


def sql(dsn, q, *a):
    with psycopg.connect(dsn, autocommit=True) as con:
        cur = con.execute(q, a)
        return cur.fetchall() if cur.description else []


# ----------------------------------------------------------------------- database
def test_roles_from_signup_order(env):
    _shim, dsn, ids, _ = env
    roles = dict(sql(dsn, "select email, role from dims_profiles"))
    assert roles == {"ankur@shop.ca": "admin", "mike@shop.ca": "editor", "floor@shop.ca": "floor",
                     "view@shop.ca": "viewer"}


def test_stock_follows_transactions_in_the_database(env):
    a = client(env, "ankur")
    p = M.new_part(name="M8 bolt", currentStock=100)
    a.inv.parts.append(p)
    a.save()
    _shim, dsn, _ids, _ = env
    a.inv.record(p["id"], "usage", 5)
    a.save()
    data, ver = sql(dsn, "select data, version from dims_records where collection='parts' and id=%s", p["id"])[0]
    assert data["currentStock"] == 95 and ver == 2               # one change: the trigger's
    assert a.inv.part(p["id"])["currentStock"] == 95 and a.pending() == []
    tx = a.inv.transactions[-1]["id"]
    a.inv.undo_transaction(tx)                                   # soft-deletes the transaction → stock back
    a.save()
    assert sql(dsn, "select data->>'currentStock' from dims_records where id=%s", p["id"])[0][0] == "100"
    assert sql(dsn, "select deleted from dims_records where id=%s", tx)[0][0] is True


def test_two_people_take_stock_at_once(env):
    a, b = client(env, "ankur"), client(env, "mike")
    p = M.new_part(name="Caster", currentStock=40)
    a.inv.parts.append(p)
    a.save()
    b.check_external()
    a.inv.record(p["id"], "usage", 3)
    b.inv.record(p["id"], "usage", 4)
    b.inv.part(p["id"])["notes"] = "rack B"
    a.save()
    b.save()
    _shim, dsn, _ids, _ = env
    data = sql(dsn, "select data from dims_records where id=%s", p["id"])[0][0]
    assert data["currentStock"] == 33 and data["notes"] == "rack B"
    assert a.check_external() and a.inv.part(p["id"])["currentStock"] == 33
    assert b.inv.part(p["id"])["currentStock"] == 33


def test_edits_to_different_fields_merge_and_deletes(env):
    a, b = client(env, "ankur"), client(env, "mike")
    v = {"id": "v1", "name": "Fastenal", "category": "Fastener", "phone": ""}
    a.inv.vendors.append(v)
    a.save()
    b.check_external()
    a.inv.vendors[0]["phone"] = "604-555-0100"
    b.inv.vendors[0]["category"] = "Other"
    a.save()
    b.save()
    a.check_external()
    assert a.inv.vendors[0] == {**v, "phone": "604-555-0100", "category": "Other"}
    b.inv.vendors.clear()
    b.save()
    assert a.check_external() and a.inv.vendors == []


def test_floor_role_can_only_record_stock(env):
    from daedalus.inventory.supabase_rows import PermissionDenied
    a = client(env, "ankur")
    p = M.new_part(name="Spring", currentStock=10)
    a.inv.parts.append(p)
    a.save()
    f = client(env, "floor")
    assert f.role == "floor"
    f.inv.record(p["id"], "usage", 2)
    f.save()                                                   # allowed; the database moves the stock
    assert a.check_external() and a.inv.part(p["id"])["currentStock"] == 8
    f.inv.part(p["id"])["reorderPoint"] = 99
    with pytest.raises(PermissionDenied):
        f.save()
    f.discard_pending()
    assert f.inv.part(p["id"])["reorderPoint"] == 0 and f.pending() == []
    v = client(env, "view")
    v.inv.parts.append(M.new_part(name="nope"))
    with pytest.raises(Exception, match="view-only"):
        v.save()
    _shim, dsn, _ids, _ = env
    # the database itself refuses, whatever the client does
    with psycopg.connect(dsn) as con:
        con.execute("set role authenticated")
        con.execute("select set_config('request.jwt.claims', %s, false)", (json.dumps({"sub": env[2]["view"]}),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            con.execute("insert into dims_records (collection, id, data) values ('parts', 'x', '{}')")


def test_weekly_backup_marker_by_anyone_and_audit(env):
    from daedalus.inventory import backup as BK
    a = client(env, "ankur")
    f = client(env, "floor")
    BK.mark_done(f.inv, "DIMS-weekly-backup-2026-11-13.xlsx")
    f.save()                                                   # floor may record the backup
    assert a.check_external() and not BK.is_due(a.inv.syncMeta)
    a.inv.exchangeRate = 1.4
    a.save()
    assert f.check_external() and f.inv.exchangeRate == 1.4
    _shim, dsn, ids, _ = env
    acts = sql(dsn, "select action, collection, user_id::text from dims_audit order by id")
    assert ("insert", "settings", ids["floor"]) in acts
    a.inv.exchangeRate = 1.5                                   # second save = UPDATE of an existing settings row
    a.save()
    BK.mark_done(a.inv, "DIMS-weekly-backup-2026-11-20.xlsx")
    a.save()
    assert sql(dsn, "select version from dims_settings where key = 'exchange-rate'")[0][0] == 2
    assert f.check_external() and f.inv.exchangeRate == 1.5


def test_import_old_web_app_data(env):
    _shim, dsn, _ids, _ = env
    parts = [{"id": "p1", "name": "Old bolt", "currentStock": 7}, {"id": "p2", "name": "Old nut", "currentStock": 3}]
    txs = [{"id": "t1", "partId": "p1", "mode": "usage", "qty": 1}]
    sql(dsn, "create table app_storage (key text primary key, value text, updated_at timestamptz default now())")
    for k, v in (("parts", json.dumps(parts)), ("transactions", json.dumps(txs)), ("exchange-rate", "1.37")):
        sql(dsn, "insert into app_storage (key, value) values (%s, %s)", k, v)
    with pytest.raises(Exception, match="admin"):
        client(env, "mike").import_old_data()
    a = client(env, "ankur")
    got = a.import_old_data()
    assert got["parts"] == 2 and got["transactions"] == 1
    assert a.inv.part("p1")["currentStock"] == 7               # imported history doesn't move stock again
    assert a.inv.exchangeRate == 1.37
    assert a.import_old_data()["parts"] == 0                   # second run: nothing new


def test_offline_then_back(env):
    shim = env[0]
    a = client(env, "ankur")
    p = M.new_part(name="Gusset", currentStock=5)
    a.inv.parts.append(p)
    a.save()
    shim.down = True
    a.inv.record(p["id"], "receiving", 10)
    with pytest.raises(Exception):
        a.save()
    from daedalus.inventory.supabase_rows import SupabaseRowStore
    a2 = SupabaseRowStore(shim.url, ANON, session=a.session, cache=env[3] / "ankur.json")
    assert a2.inv.part(p["id"])["currentStock"] == 15 and "transactions" in a2.pending()
    shim.down = False
    a2.check_external()
    assert sql(env[1], "select data->>'currentStock' from dims_records where id=%s", p["id"])[0][0] == "15"
    assert a2.pending() == []


def test_incremental_fetch_and_report_view(env):
    shim = env[0]
    a, b = client(env, "ankur"), client(env, "mike")
    for i in range(30):
        a.inv.parts.append(M.new_part(name=f"P{i}", currentStock=i, reorderPoint=5))
    a.save()
    b.check_external()
    n = shim.requests
    a.inv.parts[0]["notes"] = "x"
    a.save()
    payload = b.fetch_changes()
    assert len(payload["rows"]) <= 3                           # only what changed since the last check
    b.apply_changes(payload)
    assert b.inv.parts[0]["notes"] == "x" and shim.requests - n < 10
    low = sql(env[1], "select count(*) from dims_parts_report where needs_reorder")[0][0]
    assert low == 6                                            # P0…P5 at or below 5


def test_people_and_roles(env):
    a = client(env, "ankur")
    people = {p["email"]: p for p in a.people()}
    a.set_role(people["view@shop.ca"]["user_id"], "editor")
    assert {p["email"]: p["role"] for p in a.people()}["view@shop.ca"] == "editor"
    from daedalus.inventory.supabase_rows import PermissionDenied
    with pytest.raises(PermissionDenied):
        client(env, "mike").set_role(people["floor@shop.ca"]["user_id"], "admin")
    time.sleep(0)


def test_connect_detects_layout_and_ui(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from daedalus.inventory import backends as B
    shim, dsn, _ids, tmp = env
    s = QSettings(str(tmp / "s.ini"), QSettings.IniFormat)
    st = B.connect_team(s, shim.url, ANON, "ankur@shop.ca", "pw")
    assert st.layout == "rows" and s.value("inventory/supabase_layout") == "rows" and st.role == "admin"
    assert "dims_records" in B.setup_sql()
    from daedalus.ui.inventory_ui import InventoryPage
    from daedalus.ui.inventory_team_ui import PeopleDialog
    page = InventoryPage(store=st, lite=True)
    page.goto("sync")
    page.refresh_current()
    assert PeopleDialog(st).windowTitle().startswith("DIMS")
    page.inv.parts.append(M.new_part(name="From UI"))
    page.commit("Added")
    assert sql(dsn, "select count(*) from dims_records where data->>'name' = 'From UI'")[0][0] == 1
    # a project without the DIMS tables falls back to the original layout
    sql(dsn, "drop table dims_records cascade")
    sql(dsn, "create table app_storage (key text primary key, value text, updated_at timestamptz default now())")
    sql(dsn, "grant select, insert, update on app_storage to authenticated")
    st2 = B.connect_team(s, shim.url, ANON, "mike@shop.ca", "pw")
    assert st2.layout == "blobs" and s.value("inventory/supabase_layout") == "blobs"
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))


def test_production_completion_dates_from_audit(env):
    import datetime as dt
    a = client(env, "ankur")
    pid = a.inv.find_or_create_project("Cart")
    a.inv.productionOrders.append({"id": "po1", "projectId": pid, "qty": 2, "status": "In Fab"})
    a.save()
    next(o for o in a.inv.productionOrders if o["id"] == "po1")["status"] = "Complete"
    a.save()
    done = a.status_history("production-orders", "status", "Complete")
    assert done == {"po1": dt.date.today().isoformat()}
    f = client(env, "floor")                                   # floor can't read the audit trail: no dates, no error
    assert f.status_history("production-orders", "status", "Complete") == {}


def test_part_numbers_handed_out_by_the_database(env):
    import threading
    a, m = client(env, "ankur"), client(env, "mike")
    assert a.schema == 2
    a.inv.parts.append({"id": "x1", "name": "TUBE", "type": "manufactured", "partNumber": "C10-0007"})
    a.save()
    got = a.reserve_part_numbers("c10", 2, None, "post")
    assert [r["id"] for r in got] == ["C10-0008", "C10-0009"] and a.inv.partNumbers[0]["createdBy"]
    assert not a.pending()                                    # came from the server: nothing left to send
    res = {}

    def grab(st, key):
        res[key] = [r["id"] for r in st.reserve_part_numbers("C10", 5)]
    ts = [threading.Thread(target=grab, args=(s, k)) for s, k in ((a, "a"), (m, "m"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    allnums = res["a"] + res["m"]
    assert len(set(allnums)) == 10 and min(allnums) == "C10-0010" and max(allnums) == "C10-0019"
    m.check_external()
    rec = next(r for r in m.inv.partNumbers if r["id"] == "C10-0008")
    rec["status"] = "released"                                # editors update registry rows like any record
    m.save()
    assert a.check_external() and next(r for r in a.inv.partNumbers if r["id"] == "C10-0008")["status"] == "released"
    from daedalus.inventory.supabase_rows import PermissionDenied
    import pytest as _pt
    with _pt.raises(PermissionDenied):
        client(env, "view").reserve_part_numbers("C10", 1)
    with _pt.raises(PermissionDenied):
        client(env, "floor").reserve_part_numbers("P00", 1)
