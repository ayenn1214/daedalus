"""Parts + Engineering in one tab, editable profile pane, received-qty lock, fastener grade / material / finish."""
import os

import pytest

from daedalus.inventory import model as M


def inv_with_order():
    inv = M.Inventory()
    inv.userName = "Ankur"
    bolt = M.new_part(id="b", name="3/8 HHCS", type="fastener", currentStock=10)
    inv.parts = [bolt]
    o = inv.place_order("b", 50, "2026-11-01", "2026-11-10", "Acme", po_number="PO-1")
    return inv, bolt, o


def test_receive_confirms_qty_and_locks():
    inv, bolt, o = inv_with_order()
    inv.receive_order(o["id"], 48)
    assert bolt["currentStock"] == 58 and o["receivedQty"] == 48 and o["receivedLocked"]
    assert "48 of 50" in inv.transactions[-1]["note"]
    tx = inv.change_received(o["id"], 50, "short count")
    assert tx and bolt["currentStock"] == 60 and inv.received_qty(o) == 50
    assert o["receivedChanges"][-1] == {**o["receivedChanges"][-1], "from": 48, "to": 50, "reason": "short count"}
    inv.change_received(o["id"], 45, "damaged")
    assert bolt["currentStock"] == 55 and inv.transactions[-1]["mode"] == "usage"
    assert inv.change_received(o["id"], 45) is None              # no change, nothing logged
    assert len(o["receivedChanges"]) == 2


def test_receive_default_and_errors():
    inv, bolt, o = inv_with_order()
    with pytest.raises(ValueError):
        inv.change_received(o["id"], 5)                           # not received yet
    inv.receive_order(o["id"])
    assert bolt["currentStock"] == 60 and inv.received_qty(o) == 50


def test_fastener_grade_material_finish(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.ui import inventory_dialogs as D
    inv = M.Inventory()
    p = M.new_part(id="b", name="3/8 HHCS", type="fastener", grade="Grade 8", fastenerMaterial="Alloy Steel",
                   fastenerFinish="Yellow Zinc")
    inv.parts = [p]
    d = D.PartDialog(inv, p)
    vals = d.values()
    assert (vals["grade"], vals["fastenerMaterial"], vals["fastenerFinish"]) == ("Grade 8", "Alloy Steel", "Yellow Zinc")
    assert "Grade 8" in inv.spec_line(p)


def test_parts_tab_browser(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    tab = M.new_part(id="tab", name="FLAT, TAB", partNumber="M10-0005", description="Laser", type="manufactured")
    w = M.new_part(id="w", name="WLDMT", partNumber="M20-0001", type="manufactured", isWeldment=True,
                   components=[{"partId": "tab", "qty": 4}])
    st.inv.parts = [tab, w, M.new_part(id="n", name="Nut")]
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1500, 900)
    page.show()
    page.goto("parts")
    app.processEvents()
    br = page._browser
    t = br.table
    rows = {t.item(r, 1).text(): (t.item(r, 2).text(), t.item(r, 3).text(), t.item(r, 5).text())
            for r in range(t.rowCount())}
    assert rows["M10-0005"] == ("FLAT, TAB", "Laser", "M20-0001")          # component listed on its own
    assert "M20-0001" in rows and "" in rows                               # the nut has no number
    assert "Parts  3" in page.tabs.tabText([k for k, _ in page.TABS].index("parts"))
    r = next(r for r in range(t.rowCount()) if t.item(r, 1).text() == "M10-0005")
    t.setCurrentCell(r, 2)
    app.processEvents()
    assert page.parts_sel == "tab" and br.editor is not None and not br.dirty()
    ev = page.pages["parts"].widget()
    assert [ev.tabs.tabText(i) for i in range(2)] == ["Components", "Part Profile"]
    assert ev.tabs.currentIndex() == 0                                     # click loads, stays on the list
    t.cellDoubleClicked.emit(r, 2)
    assert ev.tabs.currentWidget() is br.profile_page                      # double-click opens the profile tab
    pk = br.picker                                                         # Part Profile: compact list on the left
    assert [pk.horizontalHeaderItem(i).text() for i in range(4)] == ["Part No.", "Description", "Project", "Used In"]
    prow = {pk.item(i, 0).text(): (pk.item(i, 1).text(), pk.item(i, 3).text()) for i in range(pk.rowCount())}
    assert prow["M10-0005"] == ("Laser", "M20-0001") and prow["M20-0001"][0] == "WLDMT"
    prow_of = lambda pn: next(i for i in range(pk.rowCount()) if pk.item(i, 0).text() == pn)  # noqa: E731
    pr = prow_of("M10-0005")
    assert pk.currentRow() == pr
    w_row = prow_of("M20-0001")
    pk.setCurrentCell(w_row, 1)
    assert page.parts_sel == "w" and br.editor.part["id"] == "w"
    assert t.item(t.currentRow(), 1).text() == "M20-0001"                   # both lists follow
    br.find.setText("m10")
    assert not pk.isRowHidden(pr) and pk.isRowHidden(w_row)
    br.find.clear()
    visible = lambda: {pk.item(i, 0).text() for i in range(pk.rowCount()) if not pk.isRowHidden(i)}  # noqa: E731
    br.pf["kind"].setCurrentIndex(br.pf["kind"].findData("component"))
    assert visible() == {"M10-0005"}
    br.pf["kind"].setCurrentIndex(br.pf["kind"].findData("bom"))
    assert visible() == {"M20-0001"}
    br._clear_picker_filters()
    br.pf["prefix"].setCurrentIndex(br.pf["prefix"].findData("none"))
    assert visible() == {""}
    br.pf["prefix"].setCurrentIndex(br.pf["prefix"].findData("M10"))
    assert visible() == {"M10-0005"} and "1 of 3" in br.pick_count.text()
    br._clear_picker_filters()
    br.pf["type"].setCurrentIndex(br.pf["type"].findData("fastener"))
    assert visible() == {""}
    br._clear_picker_filters()
    assert len(visible()) == 3
    pk.setCurrentCell(pr, 1)
    app.processEvents()
    ev.tabs.setCurrentIndex(0)
    br.editor.name.setText("FLAT, TAB, 3/16")
    assert br.dirty()
    page.refresh_current()                                                 # unsaved edits survive a refresh
    assert page._browser is br and br.editor.name.text() == "FLAT, TAB, 3/16"
    br._save(br.editor, st.inv.part("tab"))
    app.processEvents()
    assert st.inv.part("tab")["name"] == "FLAT, TAB, 3/16" and not page._browser.dirty()
    page._browser.all_cb.setChecked(True)                                  # select all
    app.processEvents()
    assert page.sel_parts == {"tab", "w", "n"}
    page.add_part_inline()
    app.processEvents()
    assert page._browser.editor is not None and page._browser.editor.part is None
    assert page.pages["parts"].widget().tabs.currentWidget() is page._browser.profile_page
    assert page.pages["parts"].widget().tabs.tabText(2) == "BOM Workbench"      # Part Numbers merged into the profile
    br = page._browser
    assert [br.left_tabs.tabText(i) for i in range(br.left_tabs.count())] == ["Parts", "Part Numbers"]
    eng = page.pages["parts"].widget()
    eng.new_prefix.setCurrentIndex(eng.new_prefix.findData("M10"))
    eng.new_btn.click()                                                    # reserve next M10 + open a blank profile
    app.processEvents()
    ed = page._browser.editor
    assert ed.part is None and ed.pn.text().startswith("M10-")
    num = ed.pn.text()
    assert any(r["id"] == num and r.get("status") == "reserved" for r in st.inv.partNumbers)
    ed.name.setText("FLAT, NEW")
    page._browser._save(ed, None)
    app.processEvents()
    assert next(p for p in st.inv.parts if p.get("partNumber") == num)["name"] == "FLAT, NEW"
    page.goto("engineering")
    assert page.current_key() == "parts"


def test_components_sort_box_and_header_sort(tmp_path):
    """The Sort box rebuilds the list from its own signal (that used to delete the box under itself and crash);
    clicking a header sorts by that column and clicking a row still opens the right part."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QComboBox
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory import model as M
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    inv = M.Inventory()
    inv.parts = [M.new_part(id="a", name="Zeta", partNumber="P-3", currentStock=5),
                 M.new_part(id="b", name="Alpha", partNumber="P-1", currentStock=50),
                 M.new_part(id="c", name="Mid", partNumber="P-2", currentStock=12)]
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.show()
    page.goto("parts")
    app.processEvents()

    def order():
        t = page._browser.table
        return [t.item(r, 2).text() for r in range(t.rowCount())]
    assert order() == ["Alpha", "Mid", "Zeta"]                         # Sort: Name
    for key, want in (("partno", ["Alpha", "Mid", "Zeta"]), ("stock", ["Zeta", "Mid", "Alpha"]),
                      ("name", ["Alpha", "Mid", "Zeta"])):
        box = [c for c in page.pages["parts"].widget().findChildren(QComboBox) if c.findData("partno") >= 0][0]
        box.setCurrentIndex(box.findData(key))                         # rebuilds from inside the box's signal
        app.processEvents()
        assert page.parts_filter["sort"] == key
        if key != "stock":
            assert order() == want
    t = page._browser.table
    t.sortItems(8, Qt.DescendingOrder)                                 # Stock column, numbers by value
    assert order() == ["Alpha", "Mid", "Zeta"]
    t.sortItems(8, Qt.AscendingOrder)
    assert order() == ["Zeta", "Mid", "Alpha"]
    t.setCurrentCell(0, 2)                                             # row 0 is now Zeta
    app.processEvents()
    assert page.parts_sel == "a"
    t.sortItems(2, Qt.AscendingOrder)
    page._browser._mark_row("a")
    assert t.currentRow() == 2 or t.selectedItems()[0].row() == 2
