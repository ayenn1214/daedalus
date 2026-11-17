"""Part profile updates: logged per save (file) or read from the audit trail (server); highlighted in Parts;
listed under Activity Log ▸ Updates."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from daedalus.inventory import model as M  # noqa: E402
from daedalus.inventory import updates as U  # noqa: E402


def test_diff_ignores_stock_and_housekeeping():
    inv = M.Inventory()
    a = M.new_part(name="Bracket", type="manufactured", revision="A", weightLb=1.0)
    b = dict(a, revision="B", weightLb=1.5, currentStock=9, updatedAt=123, revisionFiles={"x": 1},
             suppliers=[{"name": "Protech", "partNumber": "", "price": ""}, {}, {}])
    ch = {c["field"]: (c["old"], c["new"]) for c in U.diff(a, b, inv)}
    assert ch == {"revision": ("A", "B"), "weightLb": ("1", "1.5"), "suppliers": ("—", "Protech")}
    assert U.diff(a, dict(a, currentStock=5), inv) == []


def test_file_store_logs_my_part_changes(tmp_path):
    from daedalus.inventory.store import InventoryStore
    st = InventoryStore(tmp_path / "inv.json")
    st.inv.userName = "Ankur"
    p = M.new_part(name="Bracket", type="manufactured")
    st.inv.parts.append(p)
    st.save()
    p["revision"] = "A"
    p["currentStock"] = 4                                   # stock only: not an update
    st.save()
    p["currentStock"] = 5
    st.save()
    st.inv.parts.remove(p)
    st.save()
    e = U.read_file(st)
    assert [x["action"] for x in e] == ["deleted", "updated", "added"]
    assert e[1]["changes"] == [{"field": "revision", "label": "Revision", "old": "—", "new": "A"}]
    assert e[1]["by"] == "Ankur" and U.log_path(st).name == "inv.updates.jsonl"


def test_server_reads_the_audit_trail():
    inv = M.Inventory()

    class Store:
        kind = "supabase"

        def _rest(self, method, path, body=None, headers=None):
            if "dims_profiles" in path:
                return 200, [{"user_id": "u2", "display_name": "Daniel"}]
            assert "dims_audit" in path and "collection=eq.parts" in path
            return 200, [
                {"at": "2026-11-17T22:00:00Z", "user_id": "u2", "record_id": "p1", "action": "update",
                 "old": {"name": "Bracket", "revision": "A", "currentStock": 1},
                 "new": {"name": "Bracket", "revision": "B", "currentStock": 1}},
                {"at": "2026-11-17T21:00:00Z", "user_id": "u2", "record_id": "p1", "action": "update",
                 "old": {"name": "Bracket", "currentStock": 1}, "new": {"name": "Bracket", "currentStock": 3}},
                {"at": "2026-11-17T20:00:00Z", "user_id": "u2", "record_id": "p2", "action": "insert",
                 "old": None, "new": {"name": "Pin", "partNumber": "C10-0002"}}]
    e = U.read(Store(), inv)
    assert [(x["partId"], x["action"], x["by"]) for x in e] == [("p1", "updated", "Daniel"), ("p2", "added", "Daniel")]
    assert e[0]["changes"][0]["new"] == "B"


def test_parts_are_highlighted_until_opened_and_listed_in_updates(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QTableWidget, QTabWidget
    QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    QSettings("Daedalus", "Daedalus").setValue("dims/seen_updates", "{}")
    st = InventoryStore(tmp_path / "inv.json")
    p = M.new_part(name="Bracket", type="manufactured", partNumber="C10-0001")
    st.inv.parts.append(p)
    st.inv.userName = "Daniel"
    st.save()
    p["revision"] = "B"
    st.save()                                               # Daniel's change, logged
    page = InventoryPage(st, lite=True)
    page.inv.userName = "Ankur"
    marks = page.update_marks()
    assert p["id"] in marks and marks[p["id"]][1] is True and "Revision: — → B" in page.update_tip(marks[p["id"]][0])
    page.show()
    page.goto("parts")
    tbl = next(t for t in page.pages["parts"].widget().findChildren(QTableWidget) if t.rowCount()
               and any(t.horizontalHeaderItem(c).text() == "Part No." for c in range(t.columnCount())))
    row = next(r for r in range(tbl.rowCount()) for c in range(tbl.columnCount())
               if tbl.item(r, c) and tbl.item(r, c).text() == "C10-0001")
    assert any(tbl.item(row, c) and tbl.item(row, c).background().color().alpha() == 70
               for c in range(tbl.columnCount()))
    page.mark_update_seen(p["id"])
    assert page.update_marks()[p["id"]][1] is False
    page.goto("log")
    tabs = page.pages["log"].widget().findChildren(QTabWidget)[0]
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Stock moves", "Updates"]
    ut = tabs.widget(1).findChildren(QTableWidget)[0]
    assert ut.rowCount() == 2 and "Revision: — → B" in ut.item(0, 3).text() and ut.item(0, 2).text() == "Daniel"
    QSettings("Daedalus", "Daedalus").setValue("dims/seen_updates", "{}")
    page.close()
