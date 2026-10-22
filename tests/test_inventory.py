"""Holocron inventory: model rules, local store, xlsx round-trip, CAD sync, copilot tools, UI smoke."""
import json
import os

import pytest

from daedalus.core.document import Document
from daedalus.inventory import cad as IC
from daedalus.inventory import model as M
from daedalus.inventory.store import InventoryStore

from test_core import cart_geom


def box(w=40, h=30, dep=10):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": w, "h": h}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": dep})
    return d


def bolted():
    d = box()
    d.add("hole", {"face": "top", "positions": [[10, 5]], "fastener": "M8", "fit": "normal"})
    d.insert_part("socket head cap screw", "M8", 25, target={"obj": "Object1", "face": {"axis_near": [10, 5, 5]}},
                  washer=True, nut=True)
    return d


def test_rules_and_naming():
    assert M.fastener_name("M8", "Metric Coarse", "Socket Head", "", "Screw", "25").startswith("M8")
    s = IC.spec_from_designation("SHCS M8 x 25 (ISO)")
    assert s["size"] == "M8" and s["length"] == "25" and s["headStyle"] == "Socket Head"
    s = IC.spec_from_designation('Hex bolt 1/4-20 x 1.25" (ASME)')
    assert s["size"] == '1/4"' and s["length"] == "1-1/4" and s["threadStandard"] == "Imperial UNC"
    assert IC.spec_from_designation("Bracket") is None
    p = M.new_part(name="x", currentStock=4, reorderPoint=4, reorderQty=0)
    inv = M.Inventory()
    inv.parts.append(p)
    assert inv.low_stock() == [p]
    assert M.Inventory.suggested_qty(p, 20) == 16          # needed − stock beats 2·rp − stock
    assert M.Inventory.suggested_qty({**p, "reorderQty": 50}, 20) == 50


def test_transactions_undo_and_shortages():
    inv = M.Inventory()
    pid = inv.find_or_create_project("Cart", totalQtyNeeded=10)
    p = M.new_part(name="Wheel", currentStock=12)
    inv.parts.append(p)
    inv.set_allocation(p, pid, 4)
    assert inv.shortages()[0]["short"] == 28
    tx = inv.record(p["id"], "usage", 5)
    assert p["currentStock"] == 7 and inv.transactions[-1]["mode"] == "usage"
    assert inv.undo_transaction(tx) and p["currentStock"] == 12 and not inv.transactions
    with pytest.raises(ValueError):
        inv.record(p["id"], "usage", 0)


def test_weldment_expansion():
    inv = M.Inventory()
    pid = inv.find_or_create_project("Frame job", totalQtyNeeded=2)
    tube = M.new_part(type="manufactured", name="Tube 600", currentStock=0)
    frame = M.new_part(type="manufactured", name="Frame", isWeldment=True, currentStock=1,
                       components=[{"partId": tube["id"], "qty": 4}])
    inv.parts += [tube, frame]
    inv.set_allocation(frame, pid, 1)
    rows = inv.project_rows(pid)
    assert [(r[0]["name"], r[1]) for r in rows] == [("Frame", 0), ("Tube 600", 1)]
    assert rows[1][3] == 8 and rows[1][4] == 4 and rows[1][5]     # needed 2×4, embedded 1×4


def test_store_roundtrip_and_external_change(tmp_path):
    f = tmp_path / "inv.json"
    st = InventoryStore(f)
    st.inv.parts.append(M.new_part(name="Spacer", currentStock=3))
    st.save()
    raw = json.loads(f.read_text())
    assert raw["format"] == "holocron-inventory" and "parts" in raw["values"]
    other = InventoryStore(f)
    assert other.inv.parts[0]["name"] == "Spacer"
    other.inv.parts[0]["currentStock"] = 99
    other.save()
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 2))
    assert st.check_external() and st.inv.parts[0]["currentStock"] == 99


def test_xlsx_export_import(tmp_path):
    inv = M.Inventory()
    pid = inv.find_or_create_project("P1", totalQtyNeeded=3)
    p = M.new_part(type="fastener", category="Screw", headStyle="Socket Head", threadStandard="Metric Coarse",
                   size="M6", length="20", name="M6x20 SHCS", currentStock=40, reorderPoint=10, unitCost=0.12)
    inv.parts.append(p)
    inv.set_allocation(p, pid, 8)
    out = tmp_path / "master.xlsx"
    inv.export_file(out)
    inv2 = M.Inventory()
    inv2.import_file(out)
    q = inv2.parts[0]
    assert q["name"] == "M6x20 SHCS" and M.num(q["currentStock"]) == 40 and q["size"] == "M6"
    assert inv2.projects and inv2.projects[0]["name"] == "P1"


def test_send_bom_and_stock_report():
    d = bolted()
    inv = M.Inventory()
    r = IC.send_bom(inv, d, "Bracket job", 10)
    assert r["units"] == 10 and len(r["added"]) == len(d.bom())
    screw = next(p for p in inv.parts if p["type"] == "fastener" and p["category"] == "Screw")
    assert screw["size"] == "M8" and screw["length"] == "25"
    r2 = IC.send_bom(inv, d, "Bracket job", 10)
    assert not r2["added"] and len(r2["linked"]) == len(d.bom())
    screw["currentStock"] = 4
    rep = {x["part"]: x for x in IC.stock_report(inv, d, 10)}
    row = next(x for k, x in rep.items() if k.startswith("SHCS"))
    assert row["status"] == "short" and row["short"] == 6 and row["in_stock"] == 4
    hit = IC.stock_for_designation(inv, "SHCS M8 x 25 (ISO)")
    assert hit and hit[1] == 4


def test_send_bom_frame_becomes_weldment():
    d = Document()
    d.add("sketch3d", {"geometry": cart_geom()})
    d.add("frame", {"path": "3DSketch1", "profile": "square_tube", "width": 25.4, "thickness": 1.65})
    inv = M.Inventory()
    IC.send_bom(inv, d, "Cart", 1)
    w = next(p for p in inv.parts if p.get("isWeldment"))
    assert sum(c["qty"] for c in w["components"]) == 7
    assert all(inv.part(c["partId"])["category"] == "Frame member" for c in w["components"])


def test_copilot_inventory_tools(tmp_path):
    from daedalus.ai.tools import TOOLS, ToolExecutor
    names = {t["name"] for t in TOOLS}
    assert {"inventory_stock_check", "send_bom_to_inventory", "inventory_search", "inventory_record"} <= names
    d = bolted()
    ex = ToolExecutor(d)
    ex.inventory_store = InventoryStore(tmp_path / "inv.json")
    out, err = ex.run("send_bom_to_inventory", {"project": "Job", "units": 2})
    assert not err and json.loads(out)["ok"]
    out, err = ex.run("inventory_search", {"query": "m8", "shortages": True})
    res = json.loads(out)
    assert res["count"] >= 1 and res["shortages"]
    pname = res["parts"][0]["name"]
    out, err = ex.run("inventory_record", {"part": pname, "mode": "receiving", "qty": 5})
    assert json.loads(out)["stock"] == 5
    out, err = ex.run("inventory_stock_check", {"units": 2})
    rows = json.loads(out)["rows"]
    assert any(r["inventory_part"] == pname and r["in_stock"] == 5 for r in rows)
    assert (tmp_path / "inv.json").exists()


def test_inventory_ui_smoke(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import inventory_dialogs as D
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    IC.send_bom(st.inv, bolted(), "Job", 3)
    st.save()
    page = InventoryPage(st)
    page.resize(1300, 800)
    for i in range(page.tabs.count()):
        page.tabs.setCurrentIndex(i)
        app.processEvents()
    assert "Parts" in page.tabs.tabText(page.TABS.index(("parts", "Parts")))
    p = st.inv.parts[0]
    for dlg in (D.PartDialog(st.inv, p, page), D.MoveDialog(p, "usage", page), D.SendBomDialog(st.inv, "Job", page),
                D.BatchDialog(st.inv, [p], "receiving", page), D.HistoryDialog(st.inv, p["id"], page)):
        dlg.show()
        app.processEvents()
        dlg.close()
    page.close()


def test_legacy_strata_files_and_migration(tmp_path):
    """Files and data saved under the old Strata CAD name keep working."""
    from daedalus import is_native
    from daedalus.core.document import _load_any as open_any
    from daedalus.migrate import migrate_inventory
    d = box()
    d.save(tmp_path / "old.strata")
    assert is_native(tmp_path / "old.strata") and is_native("x.daedalus") and not is_native("x.step")
    assert open_any(tmp_path / "old.strata").result.bodies
    old = tmp_path / "StrataCAD" / "holocron-inventory.json"
    old.parent.mkdir()
    old.write_text('{"format": "holocron-inventory", "values": {"parts": "[]"}}')
    new = tmp_path / "Daedalus" / "holocron-inventory.json"
    assert migrate_inventory(new) and new.exists()
    assert not migrate_inventory(new)          # never overwrites
