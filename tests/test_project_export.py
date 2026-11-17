"""Project export → Excel."""
import openpyxl

from daedalus.inventory import model as M
from daedalus.inventory import orders as O
from daedalus.inventory import project_export as PX


def shop():
    inv = M.Inventory()
    pid = inv.find_or_create_project("RLC")
    tab = M.new_part(id="tab", name="FLAT, TAB", partNumber="M10-0005", type="manufactured", currentStock=2,
                     material="A36", unitCost=5)
    frame = M.new_part(id="frame", name="WLDMT, FRAME", partNumber="M30-0001", type="manufactured", isWeldment=True,
                       components=[{"partId": "tab", "qty": 4}], finish={"process": "Powder coat", "vendor": "Coat Co"})
    bolt = M.new_part(id="bolt", name="3/8 x 1 HHCS", type="fastener", currentStock=10, size="3/8-16", grade="5",
                      unitCost=0.2, suppliers=[{"name": "Acme", "price": 0.2, "partNumber": "91247A"}, {}, {}])
    caster = M.new_part(id="caster", name="Caster", type="purchased", unitCost=30,
                        suppliers=[{"name": "Roll", "price": 30, "website": "https://roll.example"}, {}, {}])
    rag = M.new_part(id="rag", name="Shop rag")
    rag["type"] = None
    inv.parts = [tab, frame, bolt, caster, rag]
    for p, q in ((frame, 1), (bolt, 8), (caster, 4), (rag, 2)):
        inv.set_allocation(p, pid, q)
    O.confirm(inv, O.save_order(inv, None, "PO-1", "Acme", [{"projectId": pid, "qty": 3}]))
    return inv, pid


def test_project_data():
    inv, pid = shop()
    d = PX.project_data(inv, pid)
    assert d["units"] == 3 and d["onOrder"] == 3
    assert d["counts"] == {"MFG parts": 1, "Purchased parts": 1, "Fasteners": 1, "Other": 1}
    assert [r["group"] for r in d["bom"]][:2] == ["MFG parts", "MFG parts"]          # frame + its tab line
    b = {r["part"]["id"]: r for r in d["build"]}
    assert b["tab"]["qty"] == 12 and b["tab"]["short"] == 10 and b["bolt"]["qty"] == 24 and b["bolt"]["short"] == 14
    assert d["perUnitCost"]["MFG parts"] == 20 and d["perUnitCost"]["Fasteners"] == 1.6
    assert PX.project_data(inv, pid, 1)["units"] == 1


def test_workbook(tmp_path):
    inv, pid = shop()
    other = inv.find_or_create_project("FHD")
    inv.set_allocation(inv.part("bolt"), other, 2)
    p = PX.write(tmp_path / "x.xlsx", inv, [pid, other], {pid: 2}, "Ankur")
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames[0] == "Summary" and "RLC · BOM" in wb.sheetnames and "FHD · Purchasing" in wb.sheetnames
    bom = wb["RLC · BOM"]
    vals = [[c.value for c in r] for r in bom.iter_rows()]
    assert vals[0][:2] == ["Part No.", "Part"] and vals[0][5] == "For 2 units"
    assert vals[1][0].startswith("MFG parts") and vals[2][0] == "M30-0001"
    assert any(r[0] and str(r[0]).startswith("Fasteners") for r in vals)
    pur = [[c.value for c in r] for r in wb["RLC · Purchasing"].iter_rows()]
    assert any(r[0] == "Acme" and r[4] == "91247A" and r[5] == 16 and r[7] == 6 for r in pur)
    web = [c for r in wb["RLC · Purchasing"].iter_rows() for c in r if c.value == "https://roll.example"]
    assert web and web[0].hyperlink
    mfg = [[c.value for c in r] for r in wb["RLC · MFG"].iter_rows()]
    assert ["M10-0005", "FLAT, TAB", "Part", "A36", None, None, 8, 2, 6] in mfg
    assert any(r[1] == "WLDMT, FRAME" and r[4] == "Powder coat" and r[5] == "Coat Co" for r in mfg)
    summ = [[c.value for c in r] for r in wb["Summary"].iter_rows()]
    assert any(r[0] == "RLC" and r[1] == 2 for r in summ)


def test_export_dialog(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog
    QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    out = tmp_path / "rlc.xlsx"
    monkeypatch.setattr(page, "_save_path", lambda *a: str(out))
    page.export_projects([pid])
    assert out.exists() and "BOM" in openpyxl.load_workbook(out).sheetnames
