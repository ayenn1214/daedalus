"""SolidWorks BOM → DIMS: review every level, number new parts under the shop structure, build the BOMs, write
PartNo / DIMSId back; Icarus drives it (and every dims_* tool) from SolidWorks Link."""
import json
import os

from daedalus.inventory import model as M
from daedalus.inventory.store import InventoryStore
from daedalus.sw import bom_inventory as BI
from daedalus.sw.backend import ASSEMBLY, PART, FakeBackend, SWComponent, SWDoc
from daedalus.sw.link import SWLink


def shop(tmp_path):
    """C30 top assembly: base frame (C10, numbered), 2 × caster sub-assembly (no numbers: plate + 4 screws),
    a hydraulic fitting with a vendor and no number."""
    b = FakeBackend(tmp_path / "sw")
    root = b.root
    top = root / "Cart.SLDASM"
    b.docs = {}
    b.comps = []
    b.active = str(top)
    b.docs[str(top)] = SWDoc(str(top), top.name, ASSEMBLY, properties={"PartNo": "C30-0004",
                                                                         "Description": "ASSY, CART"})

    def part(name, props, mat="", asm=False):
        p = root / f"{name}.{'SLDASM' if asm else 'SLDPRT'}"
        p.write_text("x")
        b.docs[str(p)] = SWDoc(str(p), p.name, ASSEMBLY if asm else PART, properties=dict(props), material=mat)
        return str(p)
    frame = part("Base Frame", {"PartNo": "C10-0007", "Description": "FRAME, BASE", "Revision": "B"}, "A36")
    caster = part("Caster Assy", {"Description": "ASSY, CASTER"}, asm=True)
    plate = part("Caster Plate", {"Description": "PLATE, CASTER", "Finish": "Powder coat"}, "6061-T6")
    screw = part("SHCS M8x25", {"Description": "SCREW, SHCS, M8 x 25"})
    fit = part("Fitting", {"Description": "FITTING, JIC 1/4", "Vendor": "PSI FLUID POWER", "VendorNo": "6-4 F5OX",
                           "Cost": "4.20"})
    comps = [SWComponent("Base Frame-1", frame, properties=b.docs[frame].properties, material="A36")]
    for n in (1, 2):
        ca = f"Caster Assy-{n}"
        comps.append(SWComponent(ca, caster, properties=b.docs[caster].properties, is_assembly=True))
        comps.append(SWComponent(f"Caster Plate-1/{n}", plate, properties=b.docs[plate].properties, parent=ca))
        for k in range(4):
            comps.append(SWComponent(f"SHCS-{k}/{n}", screw, properties=b.docs[screw].properties, parent=ca))
    comps.append(SWComponent("Fitting-1", fit, properties=b.docs[fit].properties))
    b.comps = comps
    st = InventoryStore(tmp_path / "inv.json")
    st.inv.parts = [M.new_part(id="frame", name="FRAME, BASE", partNumber="C10-0007", type="manufactured",
                               revision="A")]
    st.inv.partNumbers = [{"id": "C20-0002", "prefix": "C20", "seq": 2, "status": "assigned"}]
    st.save()
    return b, SWLink(b, store=st), st


def test_review_every_level(tmp_path):
    b, link, st = shop(tmp_path)
    rv = BI.review(link)
    by = {x["description"]: x for x in rv["lines"]}
    assert rv["product_line"] == "C" and rv["counts"]["new"] == 5 and rv["counts"]["changed"] == 1
    assert by["FRAME, BASE"]["status"] == "changed" and "revision A → B" in by["FRAME, BASE"]["changes"]
    assert by["ASSY, CASTER"]["suggested_no"] == "C20-0003" and by["ASSY, CASTER"]["kind"] == "assembly"
    assert by["PLATE, CASTER"]["suggested_no"] == "C10-0008" and by["PLATE, CASTER"]["qty_per_top"] == 2
    assert by["SCREW, SHCS, M8 x 25"]["type"] == "fastener" and by["SCREW, SHCS, M8 x 25"]["qty_per_top"] == 8
    assert by["SCREW, SHCS, M8 x 25"]["suggested_no"] == "P00-0001"
    assert by["FITTING, JIC 1/4"]["type"] == "purchased" and by["FITTING, JIC 1/4"]["suggested_no"] == ""
    assert by["ASSY, CART"]["part_no"] == "C30-0004"


def test_preview_then_add(tmp_path):
    b, link, st = shop(tmp_path)
    before = json.dumps(st.inv.parts)
    rep = BI.apply(link, preview=True)
    assert len(rep["created"]) == 5 and json.dumps(st.inv.parts) == before and not b.log[-1:] == ["props"]
    fit_item = next(x["item"] for x in BI.review(link)["lines"] if x["description"].startswith("FITTING"))
    rep = BI.apply(link, project="RLC", units=10, overrides={fit_item: {"part_no": "P02-0001"}})
    inv = InventoryStore(tmp_path / "inv.json").inv                       # saved
    by = {p["name"]: p for p in inv.parts}
    assert by["ASSY, CASTER"]["partNumber"] == "C20-0003" and by["PLATE, CASTER"]["partNumber"] == "C10-0008"
    assert by["FITTING, JIC 1/4"]["partNumber"] == "P02-0001" and by["FITTING, JIC 1/4"]["suppliers"][0][
        "partNumber"] == "6-4 F5OX" and by["FITTING, JIC 1/4"]["unitCost"] == 4.2
    assert by["PLATE, CASTER"]["finish"]["process"] == "Powder coat" and by["PLATE, CASTER"]["cadFile"].endswith(
        "Caster Plate.SLDPRT")
    assert by["FRAME, BASE"]["revision"] == "B"
    caster = by["ASSY, CASTER"]
    assert caster["isAssembly"] and {(inv.part(c["partId"])["name"], c["qty"]) for c in caster["components"]} == \
        {("PLATE, CASTER", 1), ("SCREW, SHCS, M8 x 25", 4)}
    top = by["ASSY, CART"]
    assert {inv.part(c["partId"])["name"] for c in top["components"]} == {"FRAME, BASE", "ASSY, CASTER",
                                                                          "FITTING, JIC 1/4"}
    pr = next(x for x in inv.projects if x["name"] == "RLC")
    assert inv.allocation(by["SCREW, SHCS, M8 x 25"], pr["id"])["qty"] == 8 and inv.project_qty(pr["id"]) == 10
    assert {r["id"] for r in inv.partNumbers if r.get("status") == "assigned"} >= {"C20-0003", "C10-0008",
                                                                                     "P02-0001"}
    plate_doc = b.docs[str(b.root / "Caster Plate.SLDPRT")]
    assert plate_doc.properties["PartNo"] == "C10-0008" and plate_doc.properties["DIMSId"] == by["PLATE, CASTER"]["id"]
    assert rep["written_back"] == 6 and not rep["write_failed"]
    again = BI.review(link)                                               # second pass: everything linked
    assert {x["status"] for x in again["lines"]} == {"linked"}


def test_icarus_in_sw_link(tmp_path):
    from daedalus.sw.tools import SW_TOOLS, SWToolExecutor
    b, link, st = shop(tmp_path)
    ex = SWToolExecutor(link)
    names = {t["name"] for t in SW_TOOLS}
    assert {"sw_bom_review", "sw_bom_to_inventory", "dims_reorder_list", "dims_import_file"} <= names
    out, err = ex.run("sw_bom_review", {})
    rv = json.loads(out)
    assert not err and rv["counts"]["new"] == 5 and "_structure" not in rv
    out, err = ex.run("sw_bom_to_inventory", {"preview": True})
    assert not err and json.loads(out)["preview"] and len(st.inv.parts) == 1
    out, err = ex.run("sw_bom_to_inventory", {"preview": False, "project": "RLC"})
    assert not err and len(st.inv.parts) == 6
    out, err = ex.run("dims_search_parts", {"query": "caster"})                  # DIMS tools from SolidWorks Link
    assert not err and json.loads(out)["count"] == 2


def test_review_dialog(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.sw.bom_review_ui import BomReviewDialog
    b, link, st = shop(tmp_path)
    d = BomReviewDialog(link)
    fit = next(x["item"] for x in d.rv["lines"] if x["description"].startswith("FITTING"))
    d.pns[fit].setText("p02-0005")
    d.project.setText("RLC")
    d.run(True)
    assert "Would create 5" in d.note.text() and len(st.inv.parts) == 1
    d.run(False)
    assert d.result() == 1 and any(p.get("partNumber") == "P02-0005" for p in st.inv.parts)
