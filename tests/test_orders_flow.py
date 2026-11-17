"""Orders → confirm → production → purchasing checklist (Yes / No / Not needed) → finish estimate."""
import os

import pytest

from daedalus.inventory import model as M
from daedalus.inventory import orders as O


def shop():
    inv = M.Inventory()
    inv.userName = "Ankur"
    pid = inv.find_or_create_project("RLC")
    inv.find_or_create_project("Old", totalQtyNeeded=5)
    tab = M.new_part(id="tab", name="FLAT, TAB", partNumber="M10-0005", type="manufactured")
    frame = M.new_part(id="frame", name="WLDMT, FRAME", partNumber="M30-0001", type="manufactured", isWeldment=True,
                       components=[{"partId": "tab", "qty": 4}], finish={"process": "Powder coat", "vendor": "Coat Co"})
    bolt = M.new_part(id="bolt", name="3/8 x 1 HHCS", type="fastener", currentStock=10,
                      suppliers=[{"name": "Acme", "price": 0.2}, {}, {}])
    nut = M.new_part(id="nut", name="3/8 Nyloc", type="fastener", suppliers=[{"name": "Acme", "price": 0.1}, {}, {}])
    caster = M.new_part(id="caster", name="Caster", type="purchased", suppliers=[{"name": "Roll", "price": 30}, {}, {}])
    plated = M.new_part(id="pin", name="Pin", type="manufactured", finish="Zinc plating")
    inv.parts = [tab, frame, bolt, nut, caster, plated]
    for p, q in ((frame, 1), (bolt, 8), (nut, 8), (caster, 4), (plated, 2)):
        inv.set_allocation(p, pid, q)
    return inv, pid


def test_projects_describe_one_unit_and_orders_drive_demand():
    inv, pid = shop()
    old = inv.find_or_create_project("Old")
    assert inv.project_qty(pid) == 0                     # no orders, no old plan → no demand
    assert inv.project_qty(old) == 5                     # old plan still counts until its first order
    assert inv.total_needed(inv.part("bolt")) == 0
    o = O.save_order(inv, None, "PO-77", "Acme Cabinets", [{"projectId": pid, "qty": 3}], "Ankur")
    assert O.status(o) == O.DRAFT and inv.project_qty(pid) == 0          # draft: no demand yet
    prods = O.confirm(inv, o, "Ankur")
    assert len(prods) == 1 and prods[0]["qty"] == 3 and prods[0]["poNumber"] == "PO-77"
    assert inv.project_qty(pid) == 3 and inv.total_needed(inv.part("bolt")) == 24
    rows = inv.project_rows(pid, per_unit=True)
    assert next(r for r in rows if r[0]["id"] == "bolt")[3] == 8          # project view: qty for 1 unit
    assert [c["what"] for c in o["changes"]] == ["created", "confirmed"]


def test_change_order_updates_production_and_logs():
    inv, pid = shop()
    other = inv.find_or_create_project("RLCL")
    o = O.save_order(inv, None, "PO-1", "Shop A", [{"projectId": pid, "qty": 2}], "Ankur")
    O.confirm(inv, o, "Ankur")
    p = O.linked_production(inv, o)[0]
    O.mark_read(p, "Ankur")
    assert not O.is_unread(p, "Ankur") and O.is_unread(p, "Dan")
    O.save_order(inv, o, "PO-1A", "Shop A", [{"projectId": pid, "qty": 5}, {"projectId": other, "qty": 1}], "Ankur")
    assert o["rev"] == 1 and o["changes"][-1]["what"] == "change order 1"
    assert any("×2 → ×5" in c for c in o["changes"][-1]["changes"]) and any("PO-1A" in c for c in o["changes"][-1]["changes"])
    prods = {x["projectId"]: x for x in O.linked_production(inv, o)}
    assert prods[pid]["qty"] == 5 and prods[pid]["poNumber"] == "PO-1A" and O.is_unread(prods[pid], "Ankur")
    assert prods[other]["qty"] == 1
    O.save_order(inv, o, "PO-1A", "Shop A", [{"projectId": pid, "qty": 5}], "Ankur")     # RLCL removed
    assert prods[other]["status"] == "Cancelled" and inv.project_qty(other) == 0
    O.set_status(inv, o, O.CANCELLED, "Ankur")
    assert prods[pid]["status"] == "Cancelled" and inv.project_qty(pid) == 0


def test_purchasing_checklist_split_and_finish():
    inv, pid = shop()
    o = O.save_order(inv, None, "PO-9", "Shop B", [{"projectId": pid, "qty": 2}], "Ankur")
    p = O.confirm(inv, o)[0]
    assert O.stage(inv, p) == "Waiting on fab PO"
    O.set_fab_po(p, O.YES, "F-100", "Laser Co", "Ankur")
    cl = O.checklist(inv, p)
    assert list(cl) == ["Fasteners", "Powder coating", "Zinc plating", "Purchased parts"]
    acme = cl["Fasteners"]["Acme"]["lines"]
    bolt = next(l for l in acme if l["partId"] == "bolt")
    assert bolt["qty"] == 16 and bolt["onHand"] == 10 and bolt["toOrder"] == 6
    assert cl["Powder coating"]["Coat Co"]["lines"][0]["partId"] == "frame"
    assert "No finisher set" in cl["Zinc plating"] and cl["Purchased parts"]["Roll"]["lines"][0]["qty"] == 8
    O.move_line(p, "Fasteners", "nut", "Bolt Depot")                        # split the fastener PO
    cl = O.checklist(inv, p)
    assert set(cl["Fasteners"]) == {"Acme", "Bolt Depot"}
    assert O.po_counts(inv, p) == (1, 6)
    O.set_po(p, "Fasteners", "Acme", O.YES, "A-1")
    O.set_po(p, "Fasteners", "Bolt Depot", O.NA)                            # not needed
    O.set_po(p, "Powder coating", "Coat Co", O.YES)
    O.set_po(p, "Zinc plating", "No finisher set", O.NO)
    O.set_po(p, "Purchased parts", "Roll", O.YES)
    assert O.po_counts(inv, p) == (5, 6) and not O.all_pos_done(inv, p) and O.stage(inv, p) == "POs 5/6 done"
    O.set_po(p, "Zinc plating", "No finisher set", O.NA)
    assert O.all_pos_done(inv, p) and "finish estimate" in O.stage(inv, p)
    O.set_finish(p, 12)
    assert p["finishBy"] == M.add_days(M.today(), 12) and p["status"] == "In Fab" and "finish by" in O.stage(inv, p)
    assert O.progress(inv, o)[0]["stage"] == O.stage(inv, p)


def test_fab_not_needed_and_legacy_states():
    inv, pid = shop()
    p = O.confirm(inv, O.save_order(inv, None, "", "X", [{"projectId": pid, "qty": 1}]))[0]
    O.set_fab_po(p, O.NA)
    assert O.fab_state(p) == O.NA and O.stage(inv, p).startswith("POs 1/")
    assert O.fab_state({"fabPoSent": True}) == O.YES and O.status({"status": "Requested"}) == O.DRAFT


def test_save_order_validates():
    inv, pid = shop()
    with pytest.raises(ValueError):
        O.save_order(inv, None, "PO", "", [{"projectId": pid, "qty": 1}])
    with pytest.raises(ValueError):
        O.save_order(inv, None, "PO", "Name", [{"projectId": pid, "qty": 0}])


def test_orders_and_production_pages(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import orders_ui as OU
    from daedalus.ui.inventory_ui import InventoryPage
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1400, 900)
    page.show()
    o = O.save_order(page.inv, None, "PO-5", "Shop C", [{"projectId": pid, "qty": 2}], "Ankur")
    page.confirm_client_order(o)
    for key in ("clients", "production", "projects"):
        page.goto(key)
        app.processEvents()
    assert "Orders" in page.tabs.tabText([k for k, _ in page.TABS].index("clients"))
    p = O.linked_production(page.inv, o)[0]
    d = OU.ProductionDialog(page.inv, p, lambda msg: page.store.save(), page)
    O.set_fab_po(p, O.YES, "F-1")
    d.build()
    d2 = OU.OrderEntryDialog(page.inv, o, page)
    assert d2.windowTitle() == "Change Order" and d2.values()["lines"][0]["qty"] == 2
    OU.OrderHistoryDialog(page.inv, o, page)
    page.order_status(o, O.SHIPPED)
    assert O.status(o) == O.SHIPPED


def test_old_confirmed_order_can_be_sent_to_production():
    inv, pid = shop()
    o = {"id": "old", "status": "Confirmed", "clientId": None, "projectLines": [{"projectId": pid, "qty": 2}]}
    inv.clientOrders.append(o)
    assert O.status(o) == O.CONFIRMED and not O.progress(inv, o)
    O.sync_production(inv, o, "Ankur", "From confirmed order")
    assert O.progress(inv, o)[0]["qty"] == 2 and inv.project_qty(pid) == 2


def test_project_page_groups_parts_by_type(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage, project_group
    assert [project_group({"type": t}) for t in ("manufactured", "purchased", "fastener", None)] == \
        ["MFG parts", "Purchased parts", "Fasteners", "Other"]
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("projects")
    app.processEvents()
    from PySide6.QtWidgets import QTableWidget
    t = next(x for x in page.pages["projects"].widget().findChildren(QTableWidget) if x.rowCount() > 3)
    heads = [t.item(i, 0).text() for i in range(t.rowCount()) if t.columnSpan(i, 0) > 1]
    assert heads == ["MFG parts  ·  2", "Purchased parts  ·  1", "Fasteners  ·  2"]       # one table per project


def test_typed_dates():
    b = "2026-11-16"                                                       # a Monday
    p = M.parse_date_input
    assert p("2026-11-20", b) == "2026-11-20" and p("11/20", b) == "2026-11-20" and p("Nov 20", b) == "2026-11-20"
    assert p("Fri Nov 20, 2026", b) == "2026-11-20" and p("20 nov 2027", b) == "2027-11-20"
    assert p("today", b) == b and p("tomorrow", b) == "2026-11-17" and p("fri", b) == "2026-11-20"
    assert p("mon", b) == "2026-11-23" and p("+10", b) == "2026-11-26" and p("2w", b) == "2026-11-30"
    assert p("1/15", b) == "2027-01-15" and p("", b) == "" and p("abc", b) is None and p("13/40", b) is None


def test_production_dates_inline(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import orders_ui as OU
    from daedalus.ui.date_field import DateField
    inv, pid = shop()
    p = O.confirm(inv, O.save_order(inv, None, "PO-3", "Shop", [{"projectId": pid, "qty": 1}]))[0]
    saved = []
    d = OU.ProductionDialog(inv, p, saved.append)
    d.days_box.setValue(12)
    d.days_box.editingFinished.emit()
    assert p["finishDays"] == 12 and d.finish_field.value() == M.add_days(M.today(), 12)
    d.finish_field.edit.setText("+5")                                      # typed straight into the finish date
    d.finish_field.edit.editingFinished.emit()
    assert p["finishBy"] == M.add_days(M.today(), 5) and p["finishDays"] == 5 and d.days_box.value() == 5
    d.date_fields["dueDate"].edit.setText("+3")
    d.date_fields["dueDate"].edit.editingFinished.emit()
    app.processEvents()
    assert p["dueDate"] == M.add_days(M.today(), 3) and any("Due date" in e["what"] for e in p["events"])
    f = DateField("")
    f.edit.setText("not a date")
    assert f.value() == "" and "border" in f.edit.styleSheet()
    O.set_finish_by(p, "")
    assert "finishBy" not in p


def test_project_weldments_nested_or_flat(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QTableWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    inv, pid = shop()
    flat = inv.project_rows(pid, per_unit=True, nest=False)
    tab = next(r for r in flat if r[0]["id"] == "tab")
    assert tab[1] == 0 and tab[3] == 4 and tab[2] == "in M30-0001"
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("projects")
    app.processEvents()

    def names():
        t = next(x for x in page.pages["projects"].widget().findChildren(QTableWidget) if x.rowCount() > 3)
        return [t.item(i, 1).text() for i in range(t.rowCount()) if t.item(i, 1)]
    n = names()
    assert any("▾ WLDMT, FRAME" in x and "[weldment · 1]" in x for x in n) and any("× 4  FLAT, TAB" in x for x in n)
    page.toggle_weld(pid, "frame")                                         # fold the weldment
    assert any("▸ WLDMT, FRAME" in x for x in names()) and not any("FLAT, TAB" in x for x in names())
    page.toggle_weld(pid, "frame")
    page.toggle_nest(pid)                                                  # flat: tab at the top level
    n = names()
    assert any(x.strip().startswith("FLAT, TAB") and "(in M30-0001)" in x for x in n)
    page.toggle_nest(pid)
    assert any("× 4  FLAT, TAB" in x for x in names())


def test_clicking_a_weldment_folds_it_without_crashing(tmp_path):
    """Real mouse click on the weldment row: the table rebuilds after the click, not inside it (Qt crash)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QTableWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1300, 800)
    page.show()
    page.goto("projects")
    app.processEvents()
    for expect in (True, False, True):
        t = next(x for x in page.pages["projects"].widget().findChildren(QTableWidget) if x.rowCount() > 3)
        r = next(i for i in range(t.rowCount()) if t.item(i, 1) and "WLDMT, FRAME" in t.item(i, 1).text())
        QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, t.visualItemRect(t.item(r, 1)).center())
        for _ in range(3):
            app.processEvents()
        assert (f"{pid}:frame" in page.mrp_state.get("weld_shut", [])) is expect
    page.close()
