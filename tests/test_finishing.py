"""Finishing: zinc plating by weight, powder coating by reused quote."""
import os

import openpyxl

from daedalus.inventory import finishing as F
from daedalus.inventory import model as M
from daedalus.inventory import orders as O


def shop():
    inv = M.Inventory()
    pid = inv.find_or_create_project("RLC")
    br = M.new_part(id="br", name="BRACKET", partNumber="C10-0085", type="manufactured", weightLb=9.143,
                    finish={"process": "Zinc plating", "vendor": "Plateco"})
    tab = M.new_part(id="tab", name="TAB", partNumber="C10-0003", type="manufactured", weightLb=0.685,
                     finish="Zinc plating")
    gus = M.new_part(id="gus", name="GUSSET", partNumber="C10-0024", type="manufactured", finish={"process": "Zinc plating"})
    frame = M.new_part(id="frame", name="WLDMT, FRAME", partNumber="C30-0001", type="manufactured", isWeldment=True,
                       components=[{"partId": "tab", "qty": 2}], finish={"process": "Powder coat", "spec": "RAL 9005"})
    cover = M.new_part(id="cover", name="COVER", partNumber="C20-0008", type="manufactured",
                       finish={"process": "Powder coat"})
    inv.parts = [br, tab, gus, frame, cover]
    for p, q in ((br, 3), (gus, 10), (frame, 1), (cover, 1)):
        inv.set_allocation(p, pid, q)
    return inv, pid


def test_zinc_by_weight_and_rate():
    inv, pid = shop()
    src = {"kind": "project", "id": pid, "units": 2}
    z = F.zinc_rows(inv, src)
    rows = {r["part"]["id"]: r for r in z["rows"]}
    assert rows["br"]["qty"] == 6 and rows["br"]["total"] == round(9.143 * 6, 4) and rows["br"]["cost"] == 66.38
    assert rows["tab"]["qty"] == 4                                          # inside the weldment, × 2 units
    assert rows["gus"]["missing"] and z["missing"] == 1
    assert z["rate"] == 1.21 and z["total"] == round(66.38 + rows["tab"]["cost"], 2)
    F.set_zinc_rate(inv, 1.5)
    assert F.zinc_rate(inv) == 1.5 and F.zinc_rows(inv, src)["rows"][0]["rate"] == 1.5
    # production order as the source
    o = O.confirm(inv, O.save_order(inv, None, "PO-9", "Shop", [{"projectId": pid, "qty": 3}]))[0]
    assert {r["part"]["id"]: r["qty"] for r in F.zinc_rows(inv, {"kind": "production", "id": o["id"]})["rows"]}["br"] == 9


def test_powder_quote_is_reused():
    inv, pid = shop()
    src = {"kind": "project", "id": pid, "units": 4}
    pc = F.powder_rows(inv, src)
    assert pc["needQuote"] == 2 and pc["total"] == 0
    F.add_quote(inv, inv.part("frame"), 85, "Coat Co", "CAD", "Q-1")
    F.add_quote(inv, inv.part("cover"), 10, "Coat Co", "USD")
    pc = F.powder_rows(inv, src)
    rows = {r["part"]["id"]: r for r in pc["rows"]}
    assert rows["frame"]["price"] == 85 and rows["frame"]["cost"] == 340 and rows["frame"]["spec"] == "RAL 9005"
    assert rows["cover"]["price"] == round(10 * inv.exchangeRate, 4) or abs(rows["cover"]["price"] - 13.5) < 1e-6
    F.add_quote(inv, inv.part("frame"), 90)                                 # a new quote replaces the old price
    assert F.powder_rows(inv, src)["rows"][1]["price"] == 90 or {r["part"]["id"]: r for r in
                                                                 F.powder_rows(inv, src)["rows"]}["frame"]["price"] == 90
    assert len(inv.part("frame")["pcQuotes"]) == 2
    F.set_kind(inv.part("cover"), None)
    assert [r["part"]["id"] for r in F.powder_rows(inv, src)["rows"]] == ["frame"]


def test_export_and_tab(tmp_path, monkeypatch):
    inv, pid = shop()
    src = {"kind": "project", "id": pid, "units": 1}
    p = F.write_xlsx(tmp_path / "f.xlsx", "RLC", F.zinc_rows(inv, src), F.powder_rows(inv, src))
    wb = openpyxl.load_workbook(p)
    zs = [[c.value for c in r] for r in wb["Zinc Plating"].iter_rows()]
    assert zs[2][:4] == ["Item No", "Part Number", "Description", "Qty"] and any(r[6] == "Zinc Plating Total" for r in zs)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("finishing")
    app.processEvents()
    fv = page.pages["finishing"].widget()
    assert [fv.tabs.tabText(i) for i in range(fv.tabs.count())] == ["Zinc Plating", "Powder Coating"]
    assert len(fv._zinc["rows"]) == 3
    fv.set_weight(inv.part("gus"), 0.372)
    for _ in range(3):
        app.processEvents()
    assert st.inv.part("gus")["weightLb"] == 0.372
    fv = page.pages["finishing"].widget()
    assert fv._zinc["missing"] == 0
    fv.copy(F.ZINC_COLS, F.zinc_table(fv._zinc), fv._zinc["total"], "Zinc Plating Total")
    assert QGuiApplication.clipboard().text().startswith("Item No\tPart Number")
    page.close()
