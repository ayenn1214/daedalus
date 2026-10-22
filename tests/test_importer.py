"""Import hub: projects, standard parts, fasteners, inventory parts; links, mates, BOM hand-off."""
import os
import time

import pytest

from daedalus.core import importer
from daedalus.core.document import Document
from daedalus.core.features import FeatureError
from daedalus.inventory.cad import match_part, _templates
from daedalus.inventory.model import Inventory, new_part
from daedalus.stdparts import StandardLibrary


def plate(hole=True):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 60}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    if hole:
        d.add("hole", {"face": "top", "positions": [[20, 10]], "fastener": "M6", "fit": "normal"})
    return d


def block(path, w=20):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": w, "h": 20}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 15})
    d.save(str(path))
    return d


def face(doc, obj, pred):
    return next(i for i, f in enumerate(doc.result.bodies[obj].faces()) if pred(f))


def test_project_import_linked_and_connected(tmp_path):
    p = tmp_path / "Bracket.daedalus"
    block(p)
    items = importer.project_items(dirs=[tmp_path])
    assert [i["name"] for i in items] == ["Bracket"]
    d = plate(hole=False)
    top = face(d, "Object1", lambda f: f.geom_type.name == "PLANE" and f.normal_at().Z > 0.99)
    pick = {"object": "Object1", "index": top, "point": [10, 5, 10], "local": [10, 5, 10]}
    r = importer.import_item(d, items[0], link=True, target=pick)
    n = r["objects"][0]
    assert r["connected"] and len(r["mates"]) == 1
    o = d.obj(n)
    assert o.source["system"] == "project" and o.source["linked"]
    lo = d.object_world_points(n).min(0)
    assert abs(lo[2] - 10) < 1e-3                                   # sitting on the plate's top face
    # source changes → Links picks it up
    time.sleep(0.02)
    block(p, w=40)
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert d.update_links()
    W = d.object_world_points(n)
    assert abs((W.max(0) - W.min(0))[0] - 40) < 1e-3 or abs((W.max(0) - W.min(0))[1] - 40) < 1e-3
    assert d.undo() is True


def test_fastener_into_hole_and_std_part(tmp_path, monkeypatch):
    d = plate()
    hole = face(d, "Object1", lambda f: f.geom_type.name == "CYLINDER")
    shcs = next(i for i in importer.fastener_items("socket head M6") if i["size"] == "M6")
    with pytest.raises(FeatureError):
        importer.import_item(d, shcs)                               # needs a length
    r = importer.import_item(d, shcs, length=20, target={"object": "Object1", "index": hole, "point": [20, 10, 10]})
    assert r["connected"] and len(r["mates"]) == 2
    assert d.obj(r["objects"][0]).source["system"] == "fastener"
    lib = StandardLibrary(tmp_path / "lib")
    b = Document()
    b.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 5}]})
    b.add("extrude", {"sketch": "Sketch1", "depth": 12})
    info = lib.add_object(b, "Object1", {"name": "Dowel 10", "part_number": "DW-10", "kind": "purchased"})
    import daedalus.stdparts as sp
    monkeypatch.setattr(sp, "get_library", lambda: lib)
    items = importer.std_items("dowel")
    assert items and items[0]["id"] == info["id"]
    r2 = importer.import_item(d, items[0], target={"object": "Object1", "index": hole, "point": [20, 10, 10]})
    o = d.obj(r2["objects"][0])
    assert o.std["id"] == info["id"] and o.source["system"] == "std"
    assert r2["connected"]                                          # concentric in the hole


def test_inventory_parts_map_to_cad(tmp_path, monkeypatch):
    inv = Inventory()
    shcs = new_part(name="M6 x 20 SHCS", type="fastener", category="Screw", headStyle="Socket Head",
                    threadStandard="Metric Coarse", size="M6", length="20", partNumber="F-0620", unitCost=0.31)
    inch = new_part(name='1/4-20 x 1" SHCS', type="fastener", category="Screw", headStyle="Socket Head",
                    threadStandard="Imperial UNC", size='1/4"', length="1")
    cadp = tmp_path / "Lift arm.daedalus"
    block(cadp)
    arm = new_part(name="Lift arm", type="manufactured", partNumber="RG-2001", drawingUrl=str(cadp))
    nothing = new_part(name="Grease", type="consumable")
    inv.parts = [shcs, inch, arm, nothing]
    items = {i["name"]: i for i in importer.inventory_items(inv=inv)}
    assert set(items) == {"M6 x 20 SHCS", '1/4-20 x 1" SHCS', "Lift arm"}
    assert items["M6 x 20 SHCS"]["via"] == "fastener" and items["M6 x 20 SHCS"]["length"] == 20
    assert items['1/4-20 x 1" SHCS']["size"] == "1/4-20" and abs(items['1/4-20 x 1" SHCS']["length"] - 25.4) < 1e-9
    assert items["Lift arm"]["via"] == "file"
    d = plate()
    r = importer.import_item(d, items["M6 x 20 SHCS"])
    o = d.obj(r["objects"][0])
    assert o.source == {**o.source, "system": "inventory", "id": shcs["id"]} and o.part_no == "F-0620"
    r = importer.import_item(d, items["Lift arm"])
    assert d.obj(r["objects"][0]).part_no == "RG-2001"
    # Send BOM finds the very same inventory parts again
    for t, _q, row in _templates(d):
        if row.get("inventory_id"):
            assert match_part(inv, t)["id"] == row["inventory_id"]
    ids = {row.get("inventory_id") for _t, _q, row in _templates(d)}
    assert {shcs["id"], arm["id"]} <= ids
    # survives save / open
    out = tmp_path / "assy.daedalus"
    d.save(str(out))
    d2 = Document.open(str(out))
    assert any((o.source or {}).get("id") == arm["id"] for o in d2.objects)


def test_search_and_bad_file(tmp_path):
    block(tmp_path / "A.daedalus")
    got = importer.search("", sources={"project", "fastener"}, dirs=[tmp_path])
    assert any(i["source"] == "project" for i in got) and any(i["source"] == "fastener" for i in got)
    with pytest.raises(FeatureError):
        importer.file_item(str(tmp_path / "notes.txt"))
