"""Project customizer: pick-one options (length) and add-ons (extra beam) feed demand, orders, kits, BOM export."""
import os

from daedalus.inventory import configs as C
from daedalus.inventory import model as M
from daedalus.inventory import mrp as R
from daedalus.inventory import orders as O
from daedalus.inventory import project_export as PX


def rfp():
    inv = M.Inventory()
    pid = inv.find_or_create_project("RFP")
    common = M.new_part(id="post", name="POST", partNumber="C10-0001", type="manufactured")
    b1308 = M.new_part(id="b1308", name="BEAM 1308", partNumber="C10-0130", type="manufactured")
    b1006 = M.new_part(id="b1006", name="BEAM 1006", partNumber="C10-0100", type="manufactured")
    bolt = M.new_part(id="bolt", name="1/2 HHCS", type="fastener")
    inv.parts = [common, b1308, b1006, bolt]
    inv.set_allocation(common, pid, 4)
    inv.set_allocation(bolt, pid, 16)
    pr = inv.project(pid)
    L, X = C.new_group("Length", C.ONE), C.new_group("Add-ons", C.ANY)
    std, long_ = C.new_choice("1006", default=True), C.new_choice("1308")
    std["lines"] = [{"partId": "b1006", "qty": 3}]
    long_["lines"] = [{"partId": "b1308", "qty": 3}]
    beam = C.new_choice("4th beam")
    beam["lines"] = [{"partId": "b1308", "qty": 1}, {"partId": "bolt", "qty": 4}]
    L["choices"], X["choices"] = [std, long_], [beam]
    pr["options"] = [L, X]
    return inv, pid, L, X, std, long_, beam


def test_configs_and_labels():
    inv, pid, L, X, std, long_, beam = rfp()
    pr = inv.project(pid)
    assert C.default_config(pr) == {L["id"]: [std["id"]], X["id"]: []}
    cfg = {L["id"]: [long_["id"]], X["id"]: [beam["id"]]}
    assert C.label(pr, cfg) == "1308 · 4th beam" and C.label(pr, None) == "1006"
    assert C.lines(pr, cfg) == {"b1308": 4, "bolt": 4}
    assert C.key(pr, cfg) != C.key(pr, None)
    assert C.normalize(pr, {L["id"]: ["nope"]})[L["id"]] == [std["id"]]          # bad choice → default


def test_orders_demand_and_kits_follow_the_configuration():
    inv, pid, L, X, std, long_, beam = rfp()
    long4 = {L["id"]: [long_["id"]], X["id"]: [beam["id"]]}
    o = O.save_order(inv, None, "PO-1", "Acme", [{"projectId": pid, "qty": 2, "config": long4},
                                                  {"projectId": pid, "qty": 3}], "Ankur")
    assert len(o["projectLines"]) == 2                                           # two configurations, two lines
    prods = O.confirm(inv, o)
    assert sorted(p["qty"] for p in prods) == [2, 3] and any(p.get("config") == long4 for p in prods)
    assert inv.project_qty(pid) == 5
    assert inv.total_needed(inv.part("b1308")) == 2 * 4 and inv.total_needed(inv.part("b1006")) == 3 * 3
    assert inv.total_needed(inv.part("bolt")) == 5 * 16 + 2 * 4
    assert inv.total_needed(inv.part("post")) == 20
    p_long = next(p for p in prods if p.get("config"))
    req = O.requirements(inv, p_long)
    assert req["b1308"] == 8 and "b1006" not in req and req["bolt"] == 2 * 16 + 8
    assert "RFP (1308 · 4th beam)" in O.progress(inv, o)[0]["project"] + O.progress(inv, o)[1]["project"]
    dem = dict(R.project_demands(inv, pid, 1, long4))
    assert dem["b1308"] == 4 and "b1006" not in dem
    d = PX.project_data(inv, pid, 1, long4)
    assert any(ln["part"]["id"] == "b1308" and ln["perUnit"] == 4 for ln in d["tree"])
    assert d["configLabel"] == "Length 1308 · Add-ons 4th beam"
    # change order: switch the 3 standard units to 1308
    O.save_order(inv, o, "PO-1", "Acme", [{"projectId": pid, "qty": 5, "config": long4}], "Ankur")
    live = [p for p in O.linked_production(inv, o) if p["status"] != "Cancelled"]
    assert len(live) == 1 and live[0]["qty"] == 5 and inv.total_needed(inv.part("b1006")) == 0
    assert inv.project_chips(inv.part("b1308")) == ["RFP"]


def test_customizer_and_order_ui(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QTableWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import orders_ui as OU
    from daedalus.ui.inventory_ui import InventoryPage
    from daedalus.ui.project_custom_ui import ConfigPicker, ProjectCustomizerDialog
    inv, pid, L, X, std, long_, beam = rfp()
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("projects")
    app.processEvents()
    texts = [t.item(i, 0).text() for t in page.pages["projects"].widget().findChildren(QTableWidget)
             for i in range(t.rowCount()) if t.item(i, 0)]
    assert any(x.startswith("Option · Length") for x in texts) and any(x.startswith("1006") for x in texts)
    d = ProjectCustomizerDialog(inv, pid, page)
    d.add_group()
    g = d.opts[-1]
    d.add_choice()
    d.opts[-1]["choices"][0]["lines"].append({"partId": "bolt", "qty": 2})
    out = d.options()
    assert len(out) == 3 and out[-1]["choices"][0]["lines"] == [{"partId": "bolt", "qty": 2}]
    pk = ConfigPicker(inv.project(pid))
    pk.combos[L["id"]].setCurrentIndex(1)
    pk.boxes[(X["id"], beam["id"])].setChecked(True)
    assert C.label(inv.project(pid), pk.value()) == "1308 · 4th beam"
    e = OU.OrderEntryDialog(inv, None, page)
    e._line(pid, 2, pk.value())
    e._line(pid, 1)
    e._line(pid, 1, pk.value())                                                  # same config: merged
    vals = e.values()["lines"]
    assert [ln["qty"] for ln in vals] == [3, 1] and vals[0]["config"] == pk.value()
    bom = page.project_bom(pid)
    bom.picker.combos[L["id"]].setCurrentIndex(1)
    app.processEvents()
    assert any(ln["part"]["id"] == "b1308" for ln in bom.d["tree"])
    bom.close()
    page.close()


def test_collapse_all_and_icon_row_actions(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton, QToolButton
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    inv, pid, *_ = rfp()
    inv.find_or_create_project("Other")
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("projects")
    app.processEvents()
    w = page.pages["projects"].widget()
    tips = {b.toolTip() for b in w.findChildren(QToolButton)}
    assert "Use — take stock out" in tips and "Unassign from this project" in tips
    page.collapse_all(True)
    assert page.collapsed == {pr["id"] for pr in inv.active_projects()}
    w = page.pages["projects"].widget()
    assert any(b.text() == "Expand all" for b in w.findChildren(QPushButton))
    page.collapse_all(False)
    assert not page.collapsed
    page.close()
