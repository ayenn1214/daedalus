"""Standard parts library: save, add files, insert linked/copy, update propagation, BOM/inventory, tools, UI."""
import json
import os
import time

import pytest

from daedalus.core.document import Document
from daedalus.stdparts import LibraryError, StandardLibrary


def plate():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 60, "h": 40}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 8})
    d.add("hole", {"face": "top", "positions": [[20, 10]], "fastener": "M6", "fit": "normal"})
    d.add_object("Pin", checkpoint=False)
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 10}]}, obj="Pin")
    d.add("extrude", {"sketch": "Sketch2", "depth": 30}, obj="Pin")
    return d


@pytest.fixture
def lib(tmp_path):
    return StandardLibrary(tmp_path / "lib")


def test_save_object_keeps_history(lib):
    d = plate()
    a = lib.add_object(d, "Object1", {"name": "Mount plate", "part_number": "RG-1001", "category": "Plates",
                                       "kind": "manufactured"})
    assert a["size_mm"] == [60.0, 40.0, 8.0] and a["version"] == 1
    part = Document.open(a["path"])
    assert [f.TYPE for f in part.features] == ["sketch", "extrude", "hole"]      # editable, not a dumb solid
    assert len(part.objects) == 1 and not part.mates
    with pytest.raises(LibraryError):
        lib.add_object(d, "Object1", {"name": ""})
    assert len(lib.parts()) == 1                                                  # failed save leaves nothing


def test_add_step_search_and_info(lib, tmp_path):
    d = plate()
    d.export(tmp_path / "assy.step")
    c = lib.add_file(tmp_path / "assy.step", {"category": "Imported", "vendor": "Acme", "tags": "motor, nema"})
    assert c["name"] == "assy" and c["size_mm"] == [60.0, 40.0, 30.0] and c["tags"] == ["motor", "nema"]
    assert [p["id"] for p in lib.parts("nema")] == [c["id"]] and lib.categories() == ["Imported"]
    lib.update_info(c["id"], {"name": "Motor bracket", "part_number": "MB-7"})
    assert lib.find("mb-7")["name"] == "Motor bracket" and lib.find("mb-7")["vendor"] == "Acme"
    with pytest.raises(LibraryError):
        lib.add_file(tmp_path / "x.dxf")


def test_insert_bom_link_update_and_versions(lib):
    d = plate()
    a = lib.add_object(d, "Object1", {"name": "Mount plate", "part_number": "RG-1001", "kind": "manufactured",
                                       "description": "Mount plate 60×40"})
    lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002", "kind": "purchased", "unit_cost": 3.5,
                              "vendor": "McMaster-Carr"})
    p = Document()
    n1 = lib.insert(p, "RG-1001")
    n2 = lib.insert(p, "Pin 20", at=[100, 0, 0])
    n3 = lib.insert(p, "RG-2002", link=False)
    assert p.obj(n2).pose[:3] == [100.0, 0.0, 0.0]
    rows = {r["part"]: r for r in p.bom()}
    assert rows["RG-2002"]["qty"] == 2 and rows["RG-2002"]["unit_cost"] == 3.5
    assert rows["RG-1001"]["description"] == "Mount plate 60×40" and rows["RG-1001"]["unit_cost"] > 0
    assert rows["RG-2002"]["standard"]["kind"] == "purchased"
    # edit the library part → linked copies update, embedded copy doesn't
    part = Document.open(a["path"])
    part.edit("Extrude1", {"depth": 20})
    time.sleep(1.05)
    part.save(a["path"])
    assert lib.refresh_from_file(a["id"])["version"] == 2
    assert n1 in p.update_links()
    assert p.result.bodies[n1].bounding_box().size.Z == pytest.approx(20)
    # replacing by part number keeps the id
    r = lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002"}, replace="RG-2002")
    assert r["id"] == "RG-2002" and r["version"] == 2 and len(lib.parts()) == 2
    u = {x["object"]: x for x in lib.usage(p)}
    assert u[n3]["linked"] is False and u[n2]["library_version"] == 2


def test_save_load_roundtrip_keeps_std_ref(lib, tmp_path):
    d = plate()
    lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002"})
    p = Document()
    n = lib.insert(p, "RG-2002")
    p.save(tmp_path / "proj.daedalus")
    q = Document.open(tmp_path / "proj.daedalus")
    assert q.obj(n).std["id"] == "RG-2002" and q.obj(n).part_no == "RG-2002"


def test_remove_moves_to_trash(lib):
    lib.add_object(plate(), "Pin", {"name": "Pin 20"})
    lib.remove("Pin-20")
    assert not lib.parts() and any((lib.root / ".trash").iterdir())


def test_inventory_and_copilot_tools(lib, tmp_path):
    from daedalus.ai.tools import TOOLS, ToolExecutor
    from daedalus.inventory import cad as IC
    from daedalus.inventory import model as M
    assert {"std_library_search", "insert_standard_part", "save_standard_part"} <= {t["name"] for t in TOOLS}
    d = plate()
    ex = ToolExecutor(d)
    ex.std_library = lib
    out, err = ex.run("save_standard_part", {"object": "Pin", "name": "Pin 20", "part_number": "RG-2002",
                                             "kind": "purchased", "vendor": "McMaster-Carr", "unit_cost": 3.5})
    assert not err and json.loads(out)["ok"]
    p = Document()
    ex2 = ToolExecutor(p)
    ex2.std_library = lib
    res = json.loads(ex2.run("std_library_search", {"query": "pin"})[0])
    assert res["count"] == 1 and res["parts"][0]["part_number"] == "RG-2002"
    out, err = ex2.run("insert_standard_part", {"part": "RG-2002", "at": [0, 0, 50]})
    assert not err and json.loads(out)["part_no"] == "RG-2002"
    inv = M.Inventory()
    IC.send_bom(inv, p, "Job", 4)
    q = next(x for x in inv.parts if x["partNumber"] == "RG-2002")
    assert q["type"] == "purchased" and q["name"] == "Pin 20" and q["suppliers"][0]["name"] == "McMaster-Carr"


def test_library_ui_smoke(lib):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui.stdlib_ui import SaveStandardDialog, StandardLibraryDialog
    d = plate()
    lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002", "category": "Pins"})
    dlg = StandardLibraryDialog(d, at=[5, 5, 5], lib=lib)
    dlg.show()
    app.processEvents()
    assert dlg.list.count() == 1 and "Pin 20" in dlg.info.text()
    names = []
    dlg.inserted.connect(names.append)
    dlg.insert(True)
    assert names and d.obj(names[0]).pose[:3] == [5.0, 5.0, 5.0]
    s = SaveStandardDialog(lib, d, "Object1")
    s.form.name.setText("Plate")
    s.form.pn.setText("RG-2002")
    assert s.replace_id == "RG-2002"                                 # warns it will replace
    s.form.pn.setText("RG-9")
    s._save()
    assert s.result_info and lib.find("RG-9")["name"] == "Plate"


def _step_with_props(tmp_path, name, product=("Part1", "Motor Mount", "Mount for NEMA 23"), attrs=None):
    import re
    from build123d import Box, Cylinder, export_step
    p = tmp_path / "base.step"
    export_step(Box(30, 20, 10) - Cylinder(4, 10), str(p))
    txt = p.read_text(encoding="latin-1")
    pd = re.search(r"#(\d+)\s*=\s*PRODUCT_DEFINITION\(", txt).group(1)
    items = attrs or {}
    ids = [90010 + i for i in range(len(items))]
    extra = (f"#90001=PROPERTY_DEFINITION('user defined attributes','',#{pd});\n"
             f"#90002=PROPERTY_DEFINITION_REPRESENTATION(#90001,#90003);\n"
             f"#90003=REPRESENTATION('',({','.join(f'#{i}' for i in ids)}),#90009);\n"
             + "".join(f"#{i}=DESCRIPTIVE_REPRESENTATION_ITEM('{k}','{v}');\n" for i, (k, v) in zip(ids, items.items()))
             + "#90009=REPRESENTATION_CONTEXT('','');\n") if items else ""
    txt = re.sub(r"ENDSEC;\s*END-ISO", extra + "ENDSEC;\nEND-ISO", txt)
    a, b, c = product
    txt = re.sub(r"PRODUCT\('([^']*)','([^']*)','([^']*)'", f"PRODUCT('{a}','{b}','{c}'", txt, count=1)
    out = tmp_path / name
    out.write_text(txt, encoding="latin-1")
    return out


def test_reads_step_product_and_custom_properties(lib, tmp_path):
    from daedalus.stdparts.metadata import read
    f = _step_with_props(tmp_path, "MotorMount.STEP", attrs={
        "PartNo": "RG-3105", "Description": "Motor mount plate, 10 mm", "Material": "6061-T6 (SS)",
        "Revision": "C", "Vendor": "Acme Machining"})
    m = read(f)
    assert m["part_number"] == "RG-3105" and m["name"] == "Motor Mount" and m["notes"] == "Rev C"
    assert "vendor_part_number" not in m                       # own vendor property: not a vendor's file
    a = lib.add_file(f)
    assert a["material"] == "Aluminum 6061-T6" and a["description"] == "Motor mount plate, 10 mm"
    assert a["mass_kg"] == pytest.approx((6000 - 3.14159 * 16 * 10) * 2.7e-6, rel=0.02)   # density from material
    b = lib.add_file(f, {"name": "Override", "part_number": "X-1"})                     # user fields win
    assert b["name"] == "Override" and b["part_number"] == "X-1" and b["vendor"] == "Acme Machining"


def test_vendor_names_generic_ids_and_iges(lib, tmp_path):
    from daedalus.stdparts.metadata import match_material, read
    f = _step_with_props(tmp_path, "91251A542_Black-Oxide Alloy Steel Socket Head Screw.STEP",
                         product=("COMPOUND", "COMPOUND", ""))
    m = read(f)
    assert (m["part_number"], m["vendor"], m["vendor_part_number"], m["kind"]) == \
        ("91251A542", "McMaster-Carr", "91251A542", "purchased")
    assert m["name"] == "Black-Oxide Alloy Steel Socket Head Screw"
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 5})
    d.export(tmp_path / "blk.igs")
    assert read(tmp_path / "blk.igs")["name"] == "blk"                    # processor junk ignored
    from daedalus.core import materials as matlib
    names = [x.name for x in matlib.LIBRARY]
    assert "6061" in match_material("Aluminium 6061-T6", names)
    assert "304" in match_material("AISI 304 stainless", names)
    assert match_material("unobtainium", names) is None


def test_copilot_add_file_reads_metadata(lib, tmp_path):
    from daedalus.ai.tools import ToolExecutor
    f = _step_with_props(tmp_path, "mm.step", attrs={"Part Number": "RG-77", "Description": "Bracket"})
    ex = ToolExecutor(Document())
    ex.std_library = lib
    out, err = ex.run("add_standard_part_file", {"path": str(f), "category": "Brackets"})
    r = json.loads(out)
    assert not err and r["part"]["part_number"] == "RG-77" and r["part"]["description"] == "Bracket"
    assert "STEP product data" in r["read_from_file"]


def _bracket(w=60, hole=3.3):
    from build123d import Box, Cylinder, Pos
    return (Box(w, 40, 5) + Pos(0, -17.5, 22.5) * Box(w, 5, 40) - Pos(-18, 5, 0) * Cylinder(hole, 10)
            - Pos(18, 5, 0) * Cylinder(hole, 10))


def test_similarity_scores():
    from build123d import Box, Cylinder, Plane, Pos, Rot, mirror
    from daedalus.stdparts.similarity import fingerprint, geometry_score
    a = fingerprint(_bracket())
    assert geometry_score(a, fingerprint(Pos(100, 50, 7) * Rot(30, 60, 10) * _bracket()))[0] > 0.999
    assert geometry_score(a, fingerprint(mirror(Pos(10, 0, 0) * _bracket(), Plane.YZ)))[0] > 0.999
    assert 0.95 < geometry_score(a, fingerprint(_bracket(62)))[0] < 0.985          # very similar, not identical
    assert 0.8 < geometry_score(a, fingerprint(_bracket(hole=4.5)))[0] < 0.92        # similar
    assert geometry_score(a, fingerprint(Cylinder(15, 12) - Cylinder(6, 12)))[0] < 0.6


def test_library_finds_similar_parts_and_duplicates(lib, tmp_path):
    from build123d import Pos, Rot, export_step
    for name, shape in (("br60", _bracket()), ("br62", _bracket(62)), ("other", _bracket(80, 6))):
        export_step(shape, str(tmp_path / f"{name}.step"))
        lib.add_file(tmp_path / f"{name}.step", {"name": name, "part_number": name.upper()})
    export_step(Pos(5, 5, 5) * Rot(0, 0, 90) * _bracket(), str(tmp_path / "copy.step"))
    hits = lib.similar_to_file(tmp_path / "copy.step", {"name": "new bracket"})
    assert hits[0]["id"] == "BR60" and hits[0]["match"]["level"] == "identical"
    assert any(h["id"] == "BR62" and h["match"]["level"] in ("very similar", "similar") for h in hits)
    # info alone: same vendor part number
    lib.update_info("BR62", {"vendor": "Acme", "vendor_part_number": "AC-62"})
    hits = lib.similar(None, {"name": "x", "vendor": "acme", "vendor_part_number": "ac-62"})
    assert hits and hits[0]["id"] == "BR62" and "same vendor part number" in hits[0]["match"]["reasons"]
    # parts saved before fingerprints get one on demand
    info = lib.get("OTHER")
    meta = json.loads((lib.root / "OTHER" / "info.json").read_text())
    meta.pop("signature")
    (lib.root / "OTHER" / "info.json").write_text(json.dumps(meta))
    assert lib.signature(lib.get("OTHER"))["volume"] > 0 and "signature" in lib.get("OTHER")
    lib.add_file(tmp_path / "copy.step", {"name": "dupe", "part_number": "DUPE"})
    groups = lib.duplicates()
    assert len(groups) == 1 and {p["id"] for p in groups[0]} == {"BR60", "DUPE"}
    del info


def test_object_check_and_copilot_guard(lib):
    from daedalus.ai.tools import ToolExecutor
    d = plate()
    lib.add_object(d, "Object1", {"name": "Mount plate", "part_number": "RG-1001"})
    d2 = plate()                                             # same geometry, different project
    hits = lib.similar_to_object(d2, "Object1", {"name": "Plate copy"})
    assert hits and hits[0]["id"] == "RG-1001" and hits[0]["match"]["level"] == "identical"
    ex = ToolExecutor(d2)
    ex.std_library = lib
    r = json.loads(ex.run("save_standard_part", {"object": "Object1", "name": "Plate copy"})[0])
    assert r["ok"] is False and r["similar"][0]["id"] == "RG-1001" and len(lib.parts()) == 1
    r = json.loads(ex.run("save_standard_part", {"object": "Object1", "name": "Plate copy",
                                                 "replace": "RG-1001"})[0])
    assert r["ok"] and r["replaced"] and r["version"] == 2 and len(lib.parts()) == 1
    r = json.loads(ex.run("save_standard_part", {"object": "Object1", "name": "Plate copy",
                                                 "allow_duplicate": True})[0])
    assert r["ok"] and len(lib.parts()) == 2


def test_save_dialog_similarity_panel_and_swap(lib):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from daedalus.ui.stdlib_ui import SaveStandardDialog, swap_for_library_part
    d = plate()
    lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002"})
    s = SaveStandardDialog(lib, d, "Pin")
    s.show()
    app.processEvents()
    assert s.sim.count() == 1 and s.matches[0]["match"]["level"] == "identical"
    assert s.b_use.isEnabled()
    got = []
    s.use_existing.connect(lambda pid, obj: got.append((pid, obj)))
    s._use_existing()
    assert got == [("RG-2002", "Pin")]
    d.obj("Pin").pose = [10.0, 20.0, 0.0, 0.0, 0.0, 0.0]
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        n = swap_for_library_part(lib, d, "RG-2002", "Pin")
    finally:
        QMessageBox.question = orig
    assert not d.has_obj("Pin") and d.obj(n).pose[:2] == [10.0, 20.0] and d.obj(n).std["id"] == "RG-2002"
    s2 = SaveStandardDialog(lib, d, "Object1")
    s2.show()
    app.processEvents()
    assert s2.sim.count() == 0 and "No similar" in s2.sim_info.text()


def _inventory_with_production(lib, tmp_path):
    from daedalus.inventory import model as M
    from daedalus.inventory.store import InventoryStore
    d = plate()
    pin = lib.add_object(d, "Pin", {"name": "Pin 20", "part_number": "RG-2002", "kind": "purchased",
                                    "vendor": "McMaster-Carr", "vendor_part_number": "98381A520", "unit_cost": 3.5})
    st = InventoryStore(tmp_path / "inv.json")
    inv = st.inv
    lift = inv.find_or_create_project("Lift cart", totalQtyNeeded=6)
    press = inv.find_or_create_project("Frame press", totalQtyNeeded=2)
    old = inv.find_or_create_project("Old jig", totalQtyNeeded=3)
    inv.projects[-1]["archived"] = True
    return d, pin, st, inv, (lift, press, old), M


def test_inventory_link_where_used_and_production(lib, tmp_path):
    from daedalus.stdparts.usage import add_to_inventory, inventory_parts_for, std_status, summary_line, sync_to_inventory
    d, pin, st, inv, (lift, press, old), M = _inventory_with_production(lib, tmp_path)
    assert std_status(inv, pin)["parts"] == []
    p = add_to_inventory(inv, pin)
    assert p["stdPartId"] == "RG-2002" and p["type"] == "purchased" and p["suppliers"][0]["partNumber"] == "98381A520"
    assert add_to_inventory(inv, pin) is p                                      # no second copy
    p["currentStock"] = 10
    inv.set_allocation(p, lift, 4)                                              # direct: 4 × 6 = 24
    frame = M.new_part(type="manufactured", name="Press frame", isWeldment=True,
                       components=[{"partId": p["id"], "qty": 2}])
    inv.parts.append(frame)
    inv.set_allocation(frame, press, 1)                                         # via weldment: 2 × 1 × 2 = 4
    inv.set_allocation(p, old, 1)                                               # archived project
    inv.productionOrders += [
        {"id": "po1", "projectId": lift, "qty": 6, "status": "In Fab", "dueDate": "2026-11-01"},
        {"id": "po2", "projectId": press, "qty": 2, "status": "Not Started", "dueDate": "2026-12-01"},
        {"id": "po3", "projectId": old, "qty": 3, "status": "Complete", "dueDate": "2026-01-01"}]
    s = std_status(inv, pin)
    byp = {x["project"]: x for x in s["projects"]}
    assert byp["Lift cart"]["needed"] == 24 and byp["Frame press"]["needed"] == 4
    assert byp["Frame press"]["via"] == "Press frame" and byp["Old jig"]["archived"]
    assert [x["project"] for x in s["in_production"]] == ["Lift cart"]
    assert [x["project"] for x in s["scheduled"]] == ["Frame press"]
    assert not byp["Old jig"]["production"]                                     # completed orders ignored
    assert s["needed"] == 28 and s["short"] == 18                               # archived project excluded
    assert summary_line(s) == "10 in stock · 3 projects · 1 in production"
    # matching without a link: part number, then vendor part number
    p.pop("stdPartId")
    assert inventory_parts_for(inv, pin) == [p]
    p["partNumber"] = "OTHER"
    assert inventory_parts_for(inv, pin) == [p]                                 # vendor + vendor PN
    lib.update_info("RG-2002", {"name": "Pin Ø20 × 30", "unit_cost": 3.9})
    assert sync_to_inventory(inv, lib.get("RG-2002")) == ["Pin Ø20 × 30"]
    assert p["unitCost"] == 3.9 and p["partNumber"] == "RG-2002" and p["stdPartId"] == "RG-2002"


def test_send_bom_links_standard_parts_and_file_usage(lib, tmp_path):
    from daedalus.inventory import cad as IC
    from daedalus.stdparts.usage import files_using, record_doc_usage
    d, pin, st, inv, _p, M = _inventory_with_production(lib, tmp_path)
    proj = Document()
    lib.insert(proj, "RG-2002")
    lib.insert(proj, "RG-2002", link=False)
    IC.send_bom(inv, proj, "Lift cart", 6)
    q = next(x for x in inv.parts if x.get("stdPartId") == "RG-2002")
    assert inv.allocation(q, inv.projects[0]["id"])["qty"] == 2
    q["partNumber"] = "renamed"                                                 # link survives renumbering
    IC.send_bom(inv, proj, "Lift cart", 6)
    assert sum(1 for x in inv.parts if x.get("stdPartId") == "RG-2002") == 1
    f = tmp_path / "cart.daedalus"
    proj.save(f)
    assert record_doc_usage(lib, proj, f) == 1
    u = files_using(lib, "RG-2002")
    assert u[0]["count"] == 2 and u[0]["linked"] and u[0]["exists"]
    for o in [o.name for o in proj.objects if o.std]:
        proj.delete_object(o, checkpoint=False)
    record_doc_usage(lib, proj, f)
    assert files_using(lib, "RG-2002") == []


def test_usage_tool_and_browser_tabs(lib, tmp_path):
    from daedalus.ai.tools import ToolExecutor
    from daedalus.stdparts.usage import add_to_inventory, record_doc_usage
    d, pin, st, inv, (lift, press, old), M = _inventory_with_production(lib, tmp_path)
    p = add_to_inventory(inv, pin)
    inv.set_allocation(p, lift, 4)
    inv.productionOrders.append({"id": "po1", "projectId": lift, "qty": 6, "status": "In Finishing",
                                 "dueDate": "2026-11-01"})
    st.save()
    proj = Document()
    lib.insert(proj, "RG-2002")
    proj.save(tmp_path / "cart.daedalus")
    record_doc_usage(lib, proj, tmp_path / "cart.daedalus")
    ex = ToolExecutor(proj)
    ex.std_library, ex.inventory_store = lib, st
    r = json.loads(ex.run("std_part_usage", {"part": "Pin 20"})[0])
    assert r["in_production"][0]["project"] == "Lift cart" and r["in_production"][0]["orders"][0]["status"] == "In Finishing"
    assert r["files"][0]["name"] == "cart" and r["short"] == 24
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui.stdlib_ui import SaveStandardDialog, StandardLibraryDialog
    dlg = StandardLibraryDialog(proj, lib=lib, store=st)
    dlg.show()
    app.processEvents()
    assert "1 in production" in dlg.list.item(0).text()
    assert dlg.tabs.tabText(1) == "Used in · 2" and dlg.tabs.tabText(2) == "Inventory · 0"
    top = [dlg.used.topLevelItem(i).text(0) for i in range(dlg.used.topLevelItemCount())]
    assert top == ["In production (1)", "Daedalus files (1)"]
    dlg.only_prod.setChecked(True)
    assert dlg.list.count() == 1
    targets = []
    dlg.open_inventory.connect(targets.append)
    dlg.inv_open()
    assert targets == [p["id"]]
    s = SaveStandardDialog(lib, d, "Pin")
    s._store = st
    s.form.pn.setText("RG-2002")
    assert "in production" in s.replace_lab.text() and "Lift cart" in s.replace_lab.text()
