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
    page.goto("engineering")
    assert page.current_key() == "parts"
