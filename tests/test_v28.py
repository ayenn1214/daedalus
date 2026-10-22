"""v2.8: equations + configurations, subassemblies + component patterns, curves / springs / modelled threads,
fillet / chamfer / extrude options, drawing annotations, new mates, sheet-metal tools, surfaces."""
import json
import math
import os

import numpy as np
import pytest

from daedalus.core.document import Document


def box(w=40, h=20, d=10, doc=None, obj=None):
    doc = doc or Document()
    kw = {"obj": obj} if obj else {}
    sk = doc.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": w, "h": h}]}, **kw)
    doc.add("extrude", {"sketch": sk.name, "depth": d}, **kw)
    return doc


def vol(doc, obj="Object1"):
    return doc.result.bodies[obj].volume


# ------------------------------------------------------------ 1 configs/equations
def test_equations_and_configurations(tmp_path):
    d = box()
    d.set_variable("W", "25")
    d.add_equation("Extrude1.depth", "W * 2")
    assert abs(vol(d) - 40 * 20 * 50) < 1e-6
    assert d.driven_params()["Extrude1"]["depth"] == "W * 2"
    d.add_configuration("Short")
    d.set_configuration("Short", variables={"W": "5"}, suppress={})
    assert abs(vol(d) - 40 * 20 * 10) < 1e-6
    d.add_configuration("Thin", activate=False)
    d.set_configuration("Thin", params={"Extrude1.depth": 3})
    d.activate_configuration("Default")
    assert abs(vol(d) - 40 * 20 * 50) < 1e-6
    with pytest.raises(Exception):
        d.add_equation("Extrude1.depth", "W + 1")            # already driven
    with pytest.raises(Exception):
        d.add_equation("Extrude1.draft", "nosuchname * 2")
    # sketch dimension target
    d2 = Document()
    d2.add("sketch", {"plane": "XY", "geometry": {
        "points": {"O": [0, 0], "a": [0, 0], "b": [10, 0], "c": [10, 10], "e": [0, 10]},
        "curves": [{"id": "1", "type": "line", "a": "a", "b": "b"}, {"id": "2", "type": "line", "a": "b", "b": "c"},
                   {"id": "3", "type": "line", "a": "c", "b": "e"}, {"id": "4", "type": "line", "a": "e", "b": "a"}],
        "constraints": [{"type": "coincident", "e": ["a", "O"]}, {"type": "horizontal", "e": ["1"]},
                        {"type": "vertical", "e": ["2"]}, {"type": "horizontal", "e": ["3"]},
                        {"type": "vertical", "e": ["4"]}, {"type": "distance", "e": ["a", "b"], "value": 10},
                        {"type": "distance", "e": ["b", "c"], "value": 10}]}})
    d2.add("extrude", {"sketch": "Sketch1", "depth": 5})
    d2.add_equation("Sketch1.D5", "Sketch1.D6 * 3")
    assert abs(vol(d2) - 30 * 10 * 5) < 1e-3
    # design table round trip
    p = tmp_path / "dt.csv"
    d.export_config_table(p)
    d3 = box()
    d3.set_variable("W", "25")
    d3.add_equation("Extrude1.depth", "W * 2")
    got = d3.read_config_table(p)
    assert set(got) == {"Short", "Thin"}
    d3.activate_configuration("Thin")
    assert abs(vol(d3) - 40 * 20 * 50) < 1e-6 or abs(vol(d3) - 40 * 20 * 3) < 1e-6
    # save / load / undo keep it all
    s = json.dumps(d.to_dict())
    e = Document()
    e._load_dict(json.loads(s))
    e.rebuild()
    assert e.config_names() == ["Default", "Short", "Thin"] and e.equations
    d.undo()


# ------------------------------------------------------ 2 subassemblies/patterns
def test_subassembly_rigid_and_patterns():
    d = box()
    box(10, 10, 10, d, d.add_object("A").name)
    box(10, 10, 10, d, d.add_object("B").name)
    d.place_object("B", translate=(30, 0, 0))
    d.make_subassembly("Sub", ["A", "B"], rigid=True)
    d.solve_assembly(drag=("A", (0, 0, 0), (0, 50, 0)))
    pa, pb = np.array(d.obj("A").pose[:3]), np.array(d.obj("B").pose[:3])
    assert abs(np.linalg.norm(pb - pa) - 30) < 1e-3                 # moved together
    assert d.tree()[0]["kind"] == "subassembly"
    d.set_subassembly("Sub", rigid=False)
    d.dissolve_subassembly("Sub")
    assert d.obj("A").group is None
    p = d.add_component_pattern("linear", ["A"], dir1="X", spacing1=20, count1=3)
    assert len(p["instances"]) == 2
    bom = {r["part"]: r["qty"] for r in d.bom()}
    assert bom["A"] == 3
    d.place_object("A", translate=(0, 0, 5))
    assert abs(d.obj(p["instances"][0]).pose[2] - d.obj("A").pose[2]) < 1e-9      # instances follow
    m = d.add_component_pattern("mirror", ["Object1"], plane={"p": [-60, 0, 0], "n": [1, 0, 0]})
    bb = d.placed_bodies()[m["instances"][0]].bounding_box()
    assert abs(bb.min.X + 140) < 1e-6 and abs(bb.max.X + 100) < 1e-6
    c = d.add_component_pattern("circular", ["B"], axis="Z", count=4)
    assert len(c["instances"]) == 3
    d.set_component_pattern(c["name"], count=6)
    assert len(d.comp_pattern(c["name"])["instances"]) == 5
    d.delete_component_pattern(c["name"])
    d.delete_object("A")                                        # its pattern goes with it
    assert not any(o.instance_of == "A" for o in d.objects)


# ----------------------------------------------------------- 3 curves/threads
def test_helix_spring_thread():
    d = Document()
    d.add("helix", {"diameter": 20, "pitch": 5, "revolutions": 4})
    d.add("sketch", {"plane": "XZ", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 1}]})
    d.add("sweep", {"profile": "Sketch1", "path": "Helix1", "frenet": True})
    L = 4 * math.hypot(math.pi * 20, 5)
    assert abs(vol(d) - math.pi * L) / (math.pi * L) < 0.02
    s = Document()
    s.add("spring", {"od": 20, "wire": 2, "pitch": 6, "active_coils": 5})
    assert not s.result.errors and vol(s) > 800
    t = box(30, 30, 15)
    t.add("hole", {"face": "top", "fastener": "M10", "fit": "tap", "positions": [[0, 0]]})
    v0 = vol(t)
    t.add("thread", {"point": [4.2, 0, 7], "size": "M10", "modelled": True})
    assert not t.result.errors and vol(t) < v0
    sh = Document()
    sh.add("sketch", {"plane": "XY", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 4}]})
    sh.add("extrude", {"sketch": "Sketch1", "depth": 20})
    sh.add("thread", {"point": [4, 0, 10], "size": "M8", "modelled": True})
    assert vol(sh) < math.pi * 16 * 20 - 50
    c = Document()
    c.add("curve_xyz", {"points": [[0, 0, 0], [10, 5, 5], [20, 0, 10]]})
    c.add("spiral", {"revolutions": 2})
    c.add("composite_curve", {"curves": "CurveXYZ1"})
    assert not c.result.errors


# ------------------------------------------------- 4 fillet/chamfer/extrude ends
def test_fillet_chamfer_extrude_options():
    d = box(40, 40, 20)
    v = vol(d)
    d.add("fillet", {"kind": "variable", "edges": "vertical", "radius": 2, "radius2": 6})
    assert not d.result.errors and vol(d) < v
    d.edit("Fillet1", {"kind": "face", "faces": "top", "faces2": "front"})
    assert not d.result.errors
    d.edit("Fillet1", {"kind": "full_round", "faces": "top"})
    assert not d.result.errors
    d.delete("Fillet1")
    d.add("chamfer", {"mode": "vertex", "vertices": "at(20,20,20)", "distance": 5})
    assert abs((v - vol(d)) - 125 / 6) < 1e-3
    d.edit("Chamfer1", {"mode": "distance_distance", "edges": "top", "distance": 2, "distance2": 3})
    assert not d.result.errors and 400 < v - vol(d) < 480
    d.edit("Chamfer1", {"mode": "distance_angle", "edges": "top", "distance": 2, "angle": 30})
    assert not d.result.errors
    e = box(40, 40, 10)
    e.add("sketch", {"plane": "XY", "offset": 30, "entities": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]})
    e.add("extrude", {"sketch": "Sketch2", "end": "up_to_vertex", "target": "0,0,50"})
    assert abs(vol(e) - (16000 + 2000)) < 1e-3
    for end, extra, want in (("up_to_surface", {"target": "top", "flip": True}, 18000),
                             ("offset_from_surface", {"target": "top", "target_offset": 5, "flip": True}, 17500),
                             ("up_to_next", {"flip": True}, 18000),
                             ("two_directions", {"depth": 5, "depth2": 3, "flip": False}, 16800)):
        e.edit("Extrude2", {"end": end, **extra})
        assert not e.result.errors, end
        assert abs(vol(e) - want) < 1e-2, (end, vol(e))


# ---------------------------------------------------------------- 5 drawings
def test_drawing_annotations(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from daedalus.drawing import render
    d = Document()
    d.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 20}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    dr = d.new_drawing(views=("front", "top", "iso"))
    sh = dr.sheets[0]
    top = next(v for v in sh.views if v.get("dir") == "top")
    aux = d.add_drawing_view(kind="auxiliary", parent=top["id"], line=[[-100, 10], [100, -10]])
    assert aux["_prims"] and aux["label"].startswith("VIEW")
    d.set_drawing_view(top["id"], breaks=[{"axis": "x", "a": -60, "b": 40}])
    assert top["_break_shift"]["x"] > 80
    assert any(a.get("text") == "200" for a in top["_auto"] if a.get("kind") == "h")
    d.add_drawing_item({"t": "gdt", "p": [50, 50], "symbol": "position", "tol": "0.1", "dia": True, "mod": "M",
                        "datums": ["A", "B", "C"], "leader": [40, 40]})
    d.add_drawing_item({"t": "datum", "p": [60, 80], "at": [60, 95], "name": "A"})
    d.add_drawing_item({"t": "finish", "p": [100, 80], "ra": "1.6", "method": "removal"})
    with pytest.raises(Exception):
        d.add_drawing_item({"t": "gdt", "symbol": "nonsense"})
    d.add_ordinate_dims([100, 100], [[120, 100], [150, 110]], "x")
    d.add_revision("Initial release")
    r = d.add_revision("Slot widened")
    assert r["rev"] == "B"
    prims = render.sheet_prims(d, dr, sh)
    texts = [p["text"] for p in prims if p["t"] == "text"]
    assert "REVISIONS" in texts and "⌖" in texts and "A" in texts and any(t.startswith("Ra") for t in texts)
    unit = 1.0 / (sh.scale[0] / sh.scale[1])
    vert = [p["text"] for p in prims if p["t"] == "text" and p.get("angle") == 90.0]
    assert "0" in vert and f"{20 * unit:g}" in vert and f"{50 * unit:g}" in vert      # ordinate values
    d.export_drawing(tmp_path / "a.pdf")
    d.export_drawing(tmp_path / "a.dxf")
    assert (tmp_path / "a.dxf").stat().st_size > 1000


# ------------------------------------------------------------------- 6 mates
def _two(d, sk2, depth2=20):
    d.add_object("P")
    d.add("sketch", {"plane": "XY", "entities": sk2}, obj="P")
    d.add("extrude", {"sketch": d.features[-1].name, "depth": depth2}, obj="P")


def test_new_mates():
    d = box(100, 100, 10)
    _two(d, [{"type": "circle", "x": 0, "y": 0, "r": 5}], 30)
    d.place_object("P", translate=(20, 30, 40))
    d.place_object("P", axis=(1, 0, 0), angle_deg=80, about=(20, 30, 40))
    m = d.add_mate("tangent", {"obj": "Object1", "ref": {"face": "top"}}, {"obj": "P", "ref": {"axis_near": [5, 0, 15]}})
    assert d.result.assembly.ok and abs(d.obj("P").pose[2] - 15) < 1e-3
    d.delete_mate(m.name)
    m = d.add_mate("limit_distance", {"obj": "Object1", "ref": {"face": "top"}}, {"obj": "P", "ref": {"face": "bottom"}},
                   value=0, value2=5)
    assert d.result.assembly.ok
    d.delete_mate(m.name)
    d.add_mate("screw", {"obj": "Object1", "ref": {"kind": "axis", "p": [0, 0, 0], "d": [0, 0, 1], "r": 5, "face": None}},
               {"obj": "P", "ref": {"axis_near": [5, 0, 15]}}, value=2.0)
    assert d.result.assembly.ok and d.result.assembly.dof == 1
    # slot
    s = Document()
    s.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60},
                                                 {"type": "slot", "x": -20, "y": 0, "x2": 20, "y2": 0, "w": 10,
                                                  "mode": "subtract"}]})
    s.add("extrude", {"sketch": "Sketch1", "depth": 8})
    _two(s, [{"type": "circle", "x": 0, "y": 0, "r": 5}])
    s.place_object("P", translate=(3, 4, -5))
    cyl = [i for i, f in enumerate(s.result.bodies["Object1"].faces()) if f.geom_type.name == "CYLINDER"]
    s.add_mate("slot", {"obj": "Object1", "ref": {"slot_face": cyl[0]}}, {"obj": "P", "ref": {"axis_near": [5, 0, 10]}})
    assert s.result.assembly.ok and abs(s.obj("P").pose[1]) < 1e-4
    # cam
    c = Document()
    c.add("sketch", {"plane": "XY", "entities": [{"type": "circle", "x": 8, "y": 0, "r": 30}]})
    c.add("extrude", {"sketch": "Sketch1", "depth": 10})
    _two(c, [{"type": "circle", "x": 0, "y": 0, "r": 6}], 10)
    c.place_object("P", translate=(60, 5, 0))
    cf = [i for i, f in enumerate(c.result.bodies["Object1"].faces()) if f.geom_type.name == "CYLINDER"][0]
    c.add_mate("cam", {"obj": "Object1", "ref": {"cam_face": cf}}, {"obj": "P", "ref": {"axis_near": [6, 0, 5]}})
    p = np.array(c.obj("P").pose[:2])
    assert abs(np.linalg.norm(p - [8, 0]) - 36) < 0.05
    # path
    pth = Document()
    pth.add("sketch3d", {"geometry": {"points": {"O": [0, 0, 0], "a": [0, 0, 0], "b": [100, 0, 50]},
                                      "curves": [{"id": "l", "type": "line", "a": "a", "b": "b"}], "constraints": []}})
    pth.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]})
    pth.add("extrude", {"sketch": "Sketch1", "depth": 5})
    _two(pth, [{"type": "circle", "x": 0, "y": 0, "r": 3}], 6)
    pth.place_object("P", translate=(40, 30, 0))
    pth.add_mate("path", {"obj": "P", "ref": {"point": [0, 0, 3]}}, {"obj": "Object1", "ref": {"path": "3DSketch1"}})
    from daedalus.core import assembly as asm
    w = asm.world_ref({"kind": "point", "p": [0, 0, 3]}, pth.obj("P").pose)["p"]
    assert abs(w[1]) < 1e-3 and abs(w[2] - w[0] / 2) < 1e-3
    # width + symmetric
    wd = Document()
    wd.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 40},
                                                  {"type": "rect", "x": 0, "y": 0, "w": 30, "h": 50,
                                                   "mode": "subtract"}]})
    wd.add("extrude", {"sketch": "Sketch1", "depth": 10})
    _two(wd, [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}], 10)
    wd.place_object("P", translate=(7, 30, 0))
    m = wd.add_mate("width", {"obj": "Object1", "ref": {"face_near": [-15, 0, 5], "normal": [1, 0, 0]}},
                    {"obj": "P", "ref": {"face_near": [5, 0, 5], "normal": [1, 0, 0]}},
                    extra={"a2": {"obj": "Object1", "ref": {"face_near": [15, 0, 5], "normal": [-1, 0, 0]}},
                           "b2": {"obj": "P", "ref": {"face_near": [-5, 0, 5], "normal": [-1, 0, 0]}}})
    assert abs(wd.obj("P").pose[0]) < 1e-4
    with pytest.raises(Exception):
        wd.add_mate("symmetric", {"obj": "Object1", "ref": {"point": [0, 0, 0]}}, {"obj": "P", "ref": {"point": [0, 0, 0]}})


# ------------------------------------------------------------ 7 sheet metal
def test_sheet_metal_tools():
    d = Document()
    d.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60}]})
    fl = [{"edge": e, "length": 20} for e in ("+X", "-X", "+Y", "-Y")]
    d.add("sheet_metal", {"sketch": "Sketch1", "thickness": 2, "bend_radius": 2, "flanges": fl})
    v_open = vol(d)
    d.edit("SheetMetal1", {"corners": "miter"})
    b = d.result.bodies["Object1"]
    assert not d.result.errors and b.is_valid and len(b.solids()) == 1 and vol(d) > v_open
    d.edit("SheetMetal1", {"corners": "open", "flanges": []})
    d.add("sketch", {"plane": "XY", "geometry": {"points": {"O": [0, 0], "a": [10, -40], "b": [10, 40]},
                                                 "curves": [{"id": "l", "type": "line", "a": "a", "b": "b"}],
                                                 "constraints": []}})
    d.add("sketched_bend", {"sheet": "SheetMetal1", "sketch": "Sketch2", "angle": 90})
    assert not d.result.errors and d.result.bodies["Object1"].bounding_box().max.Z > 55
    assert d.result.flats["SheetMetal1"]["bends"][-1]["sketched"] == "SketchedBend1"
    for tool, kw in (("dimple", {"size": 10, "height": 3}), ("louver", {"size": 30, "width": 8, "height": 4}),
                     ("rib", {"size": 30, "width": 6, "height": 3}), ("extruded_hole", {"size": 10, "height": 4})):
        if not any(f.TYPE == "form_tool" for f in d.features):
            d.add("form_tool", {"sheet": "SheetMetal1", "face": "top", "tool": tool, "positions": [[-30, 0]], **kw})
        else:
            d.edit("FormingTool1", {"tool": tool, **kw})
        assert not d.result.errors, tool
    c = box(100, 60, 30)
    c.add("shell", {"thickness": 2, "open_faces": "top"})
    c.add("convert_sheet_metal", {"fixed_face": "bottom", "bend_radius": 2, "k_factor": 0.44})
    fl = c.result.flats["ConverttoSheetMetal1"]
    bd = 2 * 4 - math.pi / 2 * (2 + 0.44 * 2)
    assert fl["walls"] == 5 and len(fl["bends"]) == 4
    assert abs(fl["size"][0] - (100 + 2 * (30 - bd))) < 0.01
    c.set_sheet_metal_state("flat")
    assert abs(vol(c) - 2 * (6000 + 2 * 100 * (30 - bd) + 2 * 60 * (30 - bd))) < 2


# ---------------------------------------------------------------- 8 surfaces
def test_surfaces():
    d = Document()
    d.add("sketch", {"plane": "XY", "geometry": {"points": {"O": [0, 0], "a": [0, 0], "b": [40, 0], "c": [40, 30]},
                                                 "curves": [{"id": "1", "type": "line", "a": "a", "b": "b"},
                                                            {"id": "2", "type": "line", "a": "b", "b": "c"}],
                                                 "constraints": []}})
    d.add("surface_extrude", {"sketch": "Sketch1", "depth": 20})
    assert abs(d.result.surfaces["ExtrudedSurface1"][0].area - 1400) < 1e-6
    d.add("thicken", {"surface": "ExtrudedSurface1", "thickness": 2})
    assert vol(d) > 2000
    k = box(20, 20, 10)
    k.add("delete_face", {"faces": "top", "patch": False})
    k.add("surface_fill", {"boundary": "at(0,10,10)|at(10,0,10)|at(0,-10,10)|at(-10,0,10)", "source": "DeleteFace1"})
    k.add("knit", {"surfaces": "DeleteFace1, FilledSurface1", "solid": True})
    assert abs(vol(k) - 4000) < 1e-3
    lo = Document()
    lo.add("sketch", {"plane": "XY", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 10}]})
    lo.add("sketch", {"plane": "XY", "offset": 20, "entities": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]})
    lo.add("surface_loft", {"sections": "Sketch1, Sketch2"})
    lo.add("surface_planar", {"sketch": "Sketch1"})
    lo.add("surface_revolve", {"sketch": "Sketch2", "axis": "Y", "angle": 90})
    assert not lo.result.errors and len(lo.result.surfaces) == 3
    h = box(20, 20, 10)
    h.add("fillet", {"edges": "top", "radius": 2})
    v = vol(h)
    h.add("delete_face", {"faces": "at(9.4,0,9.4)", "patch": True})
    assert vol(h) > v
    o = box(20, 20, 10)
    o.add("surface_offset", {"faces": "top", "distance": 5})
    assert abs(o.result.surfaces["OffsetSurface1"][0].center().Z - 15) < 1e-6


# ------------------------------------------------------------------- tools/UI
def test_tools_and_ui(qtbot=None):
    from daedalus.ai.tools import TOOLS, ToolExecutor
    names = {t["name"] for t in TOOLS}
    assert {"add_helix", "add_knit", "add_form_tool", "configuration", "subassembly", "component_pattern",
            "add_revision"} <= names
    d = box()
    ex = ToolExecutor(d)
    out, err = ex.run("set_variable", {"name": "W", "expr": "15"})
    assert not err
    out, err = ex.run("add_equation", {"target": "Extrude1.depth", "expr": "W"})
    assert not err and abs(vol(d) - 40 * 20 * 15) < 1e-6
    out, err = ex.run("configuration", {"action": "add", "name": "Big"})
    out, err = ex.run("configuration", {"action": "set", "name": "Big", "variables": {"W": "30"}})
    assert not err and abs(vol(d) - 40 * 20 * 30) < 1e-6
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui.v28_ui import ConfigDialog, PatternDialog
    dlg = ConfigDialog(d)
    assert dlg.grid.columnCount() == 2 and dlg.eq_table.rowCount() == 1
    assert PatternDialog("circular").params()["count"] == 4


# ------------------------------------------------------------- design data
def test_design_calcs_and_reference(tmp_path, monkeypatch):
    from daedalus.design import calcs as C
    from daedalus.design import reference as REF
    for cid, c in C.REGISTRY.items():
        r = c.run()
        assert r and all("value" in v for v in r.values()), cid
    # hand checks
    r = C.run("principal_stress", {"sx": 80, "sy": -20, "txy": 30})
    assert abs(r["σ1"]["value"] - (30 + math.hypot(50, 30))) < 1e-9
    r = C.run("shaft_asme", {"power": 0, "T": 1000, "M": 0, "keyway": False, "su": 600, "sy": 400,
                             "service": "stationary, gradual"})
    tau = min(0.3 * 400, 0.18 * 600)
    assert abs(r["Required diameter"]["value"] - (16 * 1e6 / (math.pi * tau)) ** (1 / 3)) < 1e-6
    r = C.run("column_buckling", {"P": 1000})
    assert r["Critical load used"]["value"] > 0
    r = C.run("band_brake", {"mu": 0.3, "theta": 180})
    assert abs(r["Tension ratio e^μθ"]["value"] - math.exp(0.3 * math.pi)) < 1e-9
    assert C.run("parallel_key", {"d": 40})["Key b × h"]["value"] == "12 × 8"
    with pytest.raises(C.CalcError):
        C.run("thick_cylinder", {"p": 500, "s": 100})
    with pytest.raises(C.CalcError):
        C.run("spur_gear", {"nosuch": 1})
    from daedalus.core import materials as M
    pre = C.prefill_from_material("fatigue", M.get(M.DEFAULT))
    assert pre["su"] > pre["sy"] > 0
    # reference library
    monkeypatch.setattr(REF, "root", lambda: tmp_path)
    txt = tmp_path / "h.txt"
    txt.write_text("##### PDF page 1\nStress analysis Mohr circle\n##### PDF page 2\nBand brake tension ratio e^mu theta\n")
    assert REF.add_document(txt, "Handbook")["pages"] == 2
    hits = REF.search("band brake")
    assert hits[0]["page"] == 2 and "Band brake" in REF.page_text("Handbook", 2)
    from daedalus.ai.tools import ToolExecutor
    d = box()
    ex = ToolExecutor(d)
    out, err = ex.run("design_calc", {"calc": "shaft_asme", "use_material": True})
    assert not err and "Standard diameter" in out
    out, err = ex.run("search_reference", {"query": "brake"})
    assert not err and '"page": 2' in out
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui.design_ui import DesignDataDialog
    dlg = DesignDataDialog(d)
    for cid in list(C.REGISTRY)[:5]:
        dlg.calc.show_calc(cid)
        assert dlg.calc.out.rowCount() > 0
    dlg.calc.from_material()
    dlg.ref.q.setText("brake")
    dlg.ref.search()
    assert dlg.ref.hits.count() == 1
