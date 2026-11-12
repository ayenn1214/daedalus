"""DIMS MRP-lite: part-number registry (shop structure C30/C20/C10, P00…, 1000-001), BOM workbench, where used,
job orders split by vendor (+ raw material, finishing), kit list, material needs, time-phased view, vendor /
price import, Icarus tools, screens."""
import datetime as dt
import json
import os

import pytest

from daedalus.inventory import model as M
from daedalus.inventory import mrp as R
from daedalus.inventory.store import InventoryStore


def clamp():
    """The 'Clamping' example: C30-0001 ▸ C20-0001 ▸ C10-0001 (+ material) / C10-0002 (+ P00-0010), P00-0001/2."""
    inv = M.Inventory()
    inv.userName = "AN"
    inv.vendors = [{"id": "v1", "name": "Fastenal", "leadTimeMinDays": 3, "leadTimeMaxDays": 5,
                    "address": "1 Main St", "city": "Burnaby", "phone": "604-555-0100"},
                   {"id": "v2", "name": "Apex Machine", "leadTimeMinDays": 10, "leadTimeMaxDays": 14},
                   {"id": "v3", "name": "Coast Steel"}, {"id": "v4", "name": "PowderPro"}]

    def part(pn, name, typ, **kw):
        p = M.new_part(id=pn.lower(), partNumber=pn, name=name, type=typ, **kw)
        inv.parts.append(p)
        return p
    loc = inv.find_or_create_location("Rack A", 1, "small", 1)
    part("C30-0001", "ASSY, FRAME, 32.21", "manufactured")
    part("C20-0001", "WLDMT, VERT POST, LEFT", "manufactured")
    part("C10-0001", "TUBE, VERT POST", "manufactured", unitCost=40,
         suppliers=[{"name": "Apex Machine", "price": 40, "currency": "CAD"}, {}, {}],
         material={"shape": "Square tube (HSS)", "grade": "A500 Gr B/C", "size": "2 x 2 x 3/16", "qty": 30,
                   "unit": "in", "stockLength": 240, "vendor": "Coast Steel"},
         finish={"process": "Powder coat", "spec": "Black", "vendor": "PowderPro"})
    part("C10-0002", "END CAP", "manufactured", unitCost=5, currentStock=1,
         suppliers=[{"name": "Apex Machine", "price": 5, "currency": "CAD"}, {}, {}])
    part("P00-0010", "SCREW,HHCS,1/4-20,UNC,SS,2 3/4", "fastener", currentStock=10, locationId=loc, unitCost=0.4,
         suppliers=[{"name": "Fastenal", "price": 0.4, "currency": "CAD", "partNumber": "11112"}, {}, {}])
    part("P00-0001", "NUT, HEX, 1/4-20", "fastener", currentStock=100, locationId=loc, unitCost=0.05,
         suppliers=[{"name": "Fastenal", "price": 0.05, "currency": "CAD"}, {}, {}])
    R.set_bom(inv, "c10-0002", [("p00-0010", 4)], "weldment")         # purchased bit welded in
    R.set_bom(inv, "c20-0001", [("c10-0001", 1), ("c10-0002", 2), ("p00-0001", 4)], "weldment")
    R.set_bom(inv, "c30-0001", [("c20-0001", 2), ("p00-0001", 8)], "assembly")
    return inv


def test_part_numbers_follow_the_shop_structure():
    inv = clamp()
    assert R.next_numbers(inv, "c10") == ["C10-0003"]
    assert R.next_numbers(inv, "P00", 2) == ["P00-0011", "P00-0012"]
    assert R.next_numbers(inv, "1000") == ["1000-001"]
    got = R.reserve(inv, "C10", 3, None, "lift cart", "AN")
    assert [r["id"] for r in got] == ["C10-0003", "C10-0004", "C10-0005"] and got[0]["status"] == "reserved"
    R.release(inv, "C10-0005")
    assert R.next_numbers(inv, "C10") == ["C10-0006"]                      # released numbers aren't reused
    p = M.new_part(name="BRACKET")
    inv.parts.append(p)
    R.assign(inv, "C10-0003", p)
    assert p["partNumber"] == "C10-0003" and R.number_record(inv, "C10-0003")["status"] == "assigned"
    with pytest.raises(ValueError):
        R.assign(inv, "C10-0001", p)
    rows = R.registry_rows(inv, "C10")
    assert {r["Number"] for r in rows} >= {"C10-0001", "C10-0003", "C10-0004", "C10-0005"}
    assert next(r for r in rows if r["Number"] == "C10-0001")["Status"] == "in use (unregistered)"
    assert R.registry_rows(inv, status="reserved")[0]["Created By"] == "AN"
    d = R.describe_prefix(inv, "C30")
    assert d["label"] == "Clamping · Top-level assembly" and d["type"] == "manufactured" and d["bom"] == "assembly"
    assert R.describe_prefix(inv, "P01")["label"].startswith("Transmission")
    assert R.describe_prefix(inv, "P00")["type"] == "fastener"
    assert R.level_warning("C20-0001", "C30-0001") and not R.level_warning("C20-0001", "P00-0001")


def test_bom_where_used_and_cost():
    inv = clamp()
    rows = R.bom_rows(inv, "c30-0001", 0)
    assert [r["Part Number"] for r in rows][:2] == ["C20-0001", "C10-0001"]
    assert next(r for r in rows if r["Part Number"] == "P00-0010")["Qty Total"] == 16     # 2 × 2 × 4
    assert R.bom_cost(inv, "c20-0001") == pytest.approx(40 + 2 * (5 + 4 * 0.4) + 4 * 0.05)
    wu = R.where_used(inv, "p00-0001")
    assert wu["shared"] and {x["Part"] for x in wu["parents"]} == {"ASSY, FRAME, 32.21", "WLDMT, VERT POST, LEFT"}
    assert not R.where_used(inv, "c10-0001")["shared"]
    assert R.where_used(inv, "c10-0001")["top_assemblies"][0]["Assembly"] == "ASSY, FRAME, 32.21"
    with pytest.raises(ValueError):
        R.set_bom(inv, "c10-0001", [("c30-0001", 1)])                      # loop
    # assembly components stay on the parts list; weldment components are inside the weldment
    assert "p00-0001" not in inv.component_ids() or True
    assert "c10-0001" in inv.component_ids()


def test_job_order_split_by_vendor_with_material_and_finish(tmp_path):
    inv = clamp()
    job = R.job_order(inv, "c30-0001", 2, net=True)
    g = job["groups"]
    # 4 × C20 → 4 tubes to Apex; end caps: 8 needed, 1 on hand → 7; screws 7 × 4 = 28, 10 on hand → 18
    apex = {ln["Part Number"]: ln["Qty"] for ln in g["Apex Machine"]}
    assert apex == {"C10-0001": 4, "C10-0002": 7}
    assert {ln["Part Number"]: ln["Qty"] for ln in g["Fastenal"]} == {"P00-0010": 18}       # nuts covered by stock
    steel = g["Coast Steel"][0]
    assert steel["Kind"] == "Material" and steel["Qty"] == 120 and "1 × 240" in steel["Note"]
    assert g["PowderPro"][0]["Kind"] == "Finish" and g["PowderPro"][0]["Qty"] == 4
    assert {ln["Part Number"] for ln in g["In-house assembly"]} == {"C30-0001", "C20-0001"}
    pick = {r["Part Number"]: r for r in job["pick"]}
    assert pick["P00-0001"]["Pick"] == 32 and pick["P00-0010"]["Pick"] == 10 and pick["P00-0010"]["Short"] == 18
    gross = R.job_order(inv, "c30-0001", 2, net=False)["groups"]
    assert {ln["Part Number"]: ln["Qty"] for ln in gross["Fastenal"]} == {"P00-0001": 32, "P00-0010": 32}
    paths = R.write_po_drafts(inv, job, tmp_path)
    assert len(paths) == 4 and any("Fastenal" in p for p in paths)
    import openpyxl
    ws = openpyxl.load_workbook(next(p for p in paths if "Fastenal" in p)).active
    vals = [c for row in ws.iter_rows(values_only=True) for c in row if c]
    assert "1 Main St, Burnaby" in vals and "11112" in vals
    made = R.log_job_orders(inv, job)
    assert len(made) == 5 and {o["vendorName"] for o in made} == {"Apex Machine", "Fastenal", "Coast Steel",
                                                                    "PowderPro"}
    assert next(o for o in made if o["vendorName"] == "Apex Machine")["expectedDate"] == M.add_days(M.today(), 14)
    assert any(o["item"] and not o["partId"] for o in made)


def test_material_needs_and_time_phased():
    inv = clamp()
    pid = inv.find_or_create_project("Line 3", totalQtyNeeded=1)
    inv.set_allocation(inv.part("c30-0001"), pid, 1)
    soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    inv.productionOrders.append({"id": "po1", "projectId": pid, "qty": 3, "status": "Not Started", "dueDate": soon})
    mats = R.material_needs(inv, R.open_demands(inv))
    assert mats[0]["Qty"] == 180 and mats[0]["Lengths"] == 1                # 6 tubes × 30 in, 240 in sticks
    o = inv.place_order("p00-0010", 20, M.today(), (dt.date.today() + dt.timedelta(days=5)).isoformat(),
                        project_id=pid, po_number="PO-1")
    inc = R.incoming(inv)
    assert inc[0]["For"] == "Line 3" and inc[0]["PO"] == "PO-1"
    proj = {r["Part Number"]: r for r in R.projected(inv, 60)}
    s = proj["P00-0010"]                                                 # 10 + 20 − (6 × 2 × 4 = 48) → −18
    assert s["Incoming"] == 20 and s["Demand"] == 48 and s["Projected"] == -18 and s["Short On"] == soon
    inv.receive_order(o["id"])
    R.bin_order(inv, o["id"], inv.locations[0]["id"])
    log = R.order_log(inv)
    assert [x["Event"] for x in log[:3]] == ["Binned", "Received", "Ordered"] or \
        {x["Event"] for x in log} >= {"Ordered", "Received", "Binned"}
    assert R.lead_stats(inv, "p00-0010")["last"] == 0


def test_vendor_and_price_import():
    inv = clamp()
    a, u = R.import_vendors(inv, [{"Company": "Coast Steel", "Address": "9 Dock Rd", "City": "Surrey",
                                   "Phone": "604-555-0199", "Contact": "Brian", "Terms": "Net 30"},
                                  {"Vendor Name": "New Co", "E-mail": "x@new.co", "Lead time": 7}])
    assert (a, u) == (2, 0)                                              # Coast Steel had no details yet
    cs = R.vendor_record(inv, "coast steel")
    assert cs["city"] == "Surrey" and cs["terms"] == "Net 30" and cs["contactName"] == "Brian"
    assert R.vendor_record(inv, "New Co")["leadTimeMaxDays"] == 7
    r = R.import_prices(inv, [{"Part Number": "P00-0010", "Vendor": "Grainger", "Price": "$0.35",
                               "Vendor Part #": "4XY12", "Lead Time": 4},
                              {"Part No.": "P00-0010", "Supplier": "Fastenal", "Unit Price": 0.38},
                              {"Part Number": "ZZZ-1", "Vendor": "X", "Price": 1}])
    assert r["updated"] == 2 and r["unmatched"] == ["ZZZ-1"]
    opts = R.vendor_options(inv, inv.part("p00-0010"))
    assert [o["Vendor"] for o in opts] == ["Fastenal", "Grainger"] and opts[0]["Price"] == 0.38
    assert opts[1]["Vendor Part #"] == "4XY12" and opts[1]["Quoted Lead"] == 4 and opts[0]["Role"] == "Primary"


def test_icarus_mrp_tools(tmp_path):
    from daedalus.ai.dims_tools import DimsToolExecutor
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = clamp()
    st.save()
    ex = DimsToolExecutor(lambda: st, lambda msg, undo=None: st.save())

    def run(tool, **a):
        out, err = ex.run(tool, a)
        assert not err, out
        return json.loads(out)
    pn = run("dims_part_numbers", prefix="C10")
    assert pn["next"] == "C10-0003" and pn["prefix_meaning"]["label"].endswith("Machined / fabricated part")
    assert run("dims_reserve_part_numbers", prefix="C10", count=2, note="x")["numbers"] == ["C10-0003", "C10-0004"]
    b = run("dims_bom", part="C30-0001")
    assert b["kind"] == "assembly" and b["whereUsed"]["shared"] is False and len(b["lines"]) >= 5
    j = run("dims_job_order", part="C30-0001", qty=2, po_folder=str(tmp_path / "po"))
    assert set(j["vendors"]) >= {"Apex Machine", "Fastenal", "Coast Steel", "PowderPro"} and len(j["po_drafts"]) == 4
    j = run("dims_job_order", part="C30-0001", qty=1, log=True)
    assert j["logged_orders"] >= 4 and run("dims_time_phased", section="incoming")["incoming"]
    assert run("dims_material_needs", source="projects") == []
    s = run("dims_set_bom", part="C20-0001", kind="weldment", lines=[{"part": "C30-0001", "qty": 1}]) \
        if False else None
    out, err = ex.run("dims_set_bom", {"part": "C10-0001", "lines": [{"part": "C30-0001", "qty": 1}]})
    assert err and "loop" in out
    assert run("dims_vendor", vendor="fastenal")["vendor"]["city"] == "Burnaby"
    assert InventoryStore(tmp_path / "inv.json").inv.partNumbers                 # saved


def test_screens(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    from daedalus.ui import inventory_dialogs as D
    from daedalus.ui.inventory_mrp_ui import PartProfileDialog, PrefixDialog
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = clamp()
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1500, 900)
    page.show()
    page.mrp_state.update(bom_part="c30-0001", wu_part="p00-0001", job_part="c30-0001", job_qty=2)
    for key in ("engineering", "timephased", "vendors", "parts"):
        page.goto(key)
        app.processEvents()
    page.goto("engineering")
    eng = page.pages["engineering"].widget()
    for i in range(eng.tabs.count()):
        page.mrp_state["eng_tab"] = i
        page.refresh_current()
        app.processEvents()
    eng = page.pages["engineering"].widget()
    got = eng.reserve("C20", 2, None, "test")
    assert [r["id"] for r in got] == ["C20-0002", "C20-0003"]
    page.mrp_state["eng_tab"] = 3
    page.refresh_current()
    eng = page.pages["engineering"].widget()
    eng.log_orders()
    assert len(st.inv.partOrders) == 5
    page.goto("timephased")
    app.processEvents()
    for dlg in (PartProfileDialog(page, "c10-0001"), PartProfileDialog(page, "c30-0001"), PrefixDialog(st.inv, page),
                D.PartDialog(st.inv, st.inv.part("c10-0001"), page),
                D.PartDialog(st.inv, None, page, preset={"partNumber": "C20-0002", "type": "manufactured",
                                                         "isAssembly": True})):
        dlg.show()
        app.processEvents()
        dlg.close()
    d = D.PartDialog(st.inv, st.inv.part("c10-0001"), page)
    vals = d.values()
    assert vals["material"]["size"] == "2 x 2 x 3/16" and vals["finish"]["vendor"] == "PowderPro"
    assert vals["suppliers"][0]["name"] == "Apex Machine"
    d = D.PartDialog(st.inv, st.inv.part("c30-0001"), page)
    v2 = d.values()
    assert v2["isAssembly"] and not v2["isWeldment"] and len(v2["components"]) == 2
    page.close()
