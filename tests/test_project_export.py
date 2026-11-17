"""Project BOM / parts list, export, and the Reorder buy list."""
import os

import openpyxl

from daedalus.inventory import model as M
from daedalus.inventory import orders as O
from daedalus.inventory import project_export as PX


def shop():
    inv = M.Inventory()
    pid = inv.find_or_create_project("RLC")
    tab = M.new_part(id="tab", name="FLAT, TAB", partNumber="M10-0005", type="manufactured", currentStock=2,
                     material="A36", unitCost=5)
    gus = M.new_part(id="gus", name="GUSSET", partNumber="M10-0006", type="manufactured", material="A36")
    sub = M.new_part(id="sub", name="WLDMT, LEG", partNumber="M20-0001", type="manufactured", isWeldment=True,
                     components=[{"partId": "gus", "qty": 2}])
    bolt = M.new_part(id="bolt", name="3/8 x 1 HHCS", type="fastener", currentStock=10, size="3/8-16", grade="5",
                      unitCost=0.2, suppliers=[{"name": "Acme", "price": 0.2, "partNumber": "91247A"}, {}, {}])
    frame = M.new_part(id="frame", name="WLDMT, FRAME", partNumber="M30-0001", type="manufactured", isWeldment=True,
                       components=[{"partId": "tab", "qty": 4}, {"partId": "sub", "qty": 2},
                                   {"partId": "nut", "qty": 2}],
                       finish={"process": "Powder coat", "vendor": "Coat Co"})
    nut = M.new_part(id="nut", name="3/8 Nyloc", type="fastener", suppliers=[{"name": "Acme", "price": 0.1}, {}, {}])
    caster = M.new_part(id="caster", name="Caster", type="purchased", unitCost=30,
                        suppliers=[{"name": "Roll", "price": 30, "website": "https://roll.example"}, {}, {}])
    rag = M.new_part(id="rag", name="Shop rag")
    rag["type"] = None
    inv.parts = [tab, gus, sub, bolt, nut, frame, caster, rag]
    for p, q in ((frame, 1), (bolt, 8), (caster, 4), (rag, 2)):
        inv.set_allocation(p, pid, q)
    O.confirm(inv, O.save_order(inv, None, "PO-1", "Acme", [{"projectId": pid, "qty": 3}]))
    return inv, pid


def test_hierarchy_and_lists():
    inv, pid = shop()
    d = PX.project_data(inv, pid)
    assert d["units"] == 3
    tree = [(ln["item"], ln["part"]["id"], ln["qty"], ln["perUnit"], ln["total"]) for ln in d["tree"]]
    assert tree == [("1", "frame", 1, 1, 3), ("1.1", "tab", 4, 4, 12), ("1.2", "sub", 2, 2, 6),
                    ("1.2.1", "gus", 2, 4, 12)]
    f = {r["part"]["id"]: r for r in d["lists"]["Fasteners"]}
    assert f["bolt"]["perUnit"] == 8 and f["bolt"]["total"] == 24 and f["bolt"]["short"] == 14
    assert f["nut"]["perUnit"] == 2 and f["nut"]["total"] == 6                 # inside the frame weldment
    assert [r["part"]["id"] for r in d["lists"]["Purchased parts"]] == ["caster"]
    assert [r["part"]["id"] for r in d["lists"]["Other"]] == ["rag"]
    assert d["counts"] == {"MFG parts": 4, "Fasteners": 2, "Purchased parts": 1, "Other": 1}
    cols, rows = PX.tree_rows(inv, d)
    t = PX.tsv(cols, rows)
    assert t.splitlines()[0].startswith("Item\tPart No.\tPart") and "1.2.1\tM10-0006\t        GUSSET" in t
    cols, rows = PX.buy_rows(inv, d)
    assert [r[0] for r in rows] == ["Acme", "Acme", "Roll", "No vendor set"]
    bolt = next(r for r in rows if r[3] == "3/8 x 1 HHCS")
    assert bolt[7] == 14 and bolt[2] == "91247A"


def test_workbook(tmp_path):
    inv, pid = shop()
    p = PX.write(tmp_path / "x.xlsx", inv, [pid], {pid: 2}, "Ankur")
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames == ["BOM", "MFG", "Fasteners", "Purchased", "Other", "Buy List"]
    vals = [[c.value for c in r] for r in wb["BOM"].iter_rows()]
    assert vals[0][0] == "RLC — Bill of Materials"
    secs = [r[0] for r in vals if isinstance(r[0], str) and " · " in r[0] and not r[0].startswith("2 units")]
    assert secs == ["MFG parts · 4", "Fasteners · 2", "Purchased parts · 1", "Other · 1"]
    mfg = [[c.value for c in r] for r in wb["MFG"].iter_rows()]
    assert mfg[0][:3] == ["Item", "Part No.", "Part"] and mfg[4][0] == "1.2.1" and mfg[4][9] == 8
    buy = [[c.value for c in r] for r in wb["Buy List"].iter_rows()]
    assert buy[1][0] == "Acme" and buy[3][12] == "https://roll.example"
    inv.find_or_create_project("FHD")
    wb = openpyxl.load_workbook(PX.write(tmp_path / "y.xlsx", inv, [pid, inv.find_or_create_project("FHD")]))
    assert wb.sheetnames[0] == "Summary" and "RLC · BOM" in wb.sheetnames


def test_bom_window_and_buy_list(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QTreeWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    d = page.project_bom(pid)
    tree = d.tabs.widget(0)
    assert isinstance(tree, QTreeWidget) and tree.topLevelItemCount() == 1
    top = tree.topLevelItem(0)
    assert top.text(0) == "1" and top.childCount() == 2 and top.child(1).child(0).text(0) == "1.2.1"
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == \
        ["MFG parts · 4", "Fasteners · 2", "Purchased parts · 1", "Other · 1", "Buy List · 4"]
    d.copy_tab()
    assert QGuiApplication.clipboard().text().startswith("Item\tPart No.")
    d.units.setValue(1)
    assert "×1" in d.sections[0][1][9]
    out = tmp_path / "rlc.xlsx"
    monkeypatch.setattr(page, "_save_path", lambda *a: str(out))
    d.export()
    assert out.exists()
    d.close()
    page.goto("reorder")
    app.processEvents()
    b = page.buy_list()
    assert b.rows and b.cols[:3] == ["Vendor", "Part No.", "Vendor PN"]
    b.copy_all()
    lines = QGuiApplication.clipboard().text().splitlines()
    assert lines[0].split("\t")[0] == "Vendor" and len(lines) == len(b.rows) + 1
    b.table.selectRow(0)
    b.copy_selected()
    assert len(QGuiApplication.clipboard().text().splitlines()) == 2
    b.close()
    page.close()
