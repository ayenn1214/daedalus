import math

import pytest

from daedalus.core import Document


def plate(doc, holes=True):
    doc.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 100}]})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 12})
    if holes:
        doc.add("hole", {"face": "top", "diameter": 9, "positions": [[-85, -35], [85, -35], [85, 35], [-85, 35]]})


def test_plate_with_holes():
    doc = Document()
    plate(doc)
    assert not doc.result.errors
    expected = 200 * 100 * 12 - 4 * math.pi * 4.5 ** 2 * 12
    assert doc.result.body.volume == pytest.approx(expected, rel=1e-4)


def test_parametric_edit_and_undo():
    doc = Document()
    plate(doc, holes=False)
    doc.edit("Extrude1", {"depth": 20})
    assert doc.result.body.volume == pytest.approx(200 * 100 * 20, rel=1e-6)
    doc.undo()
    assert doc.result.body.volume == pytest.approx(200 * 100 * 12, rel=1e-6)
    doc.redo()
    assert doc.mass_properties()["bbox_mm"][2] == pytest.approx(20)


def test_fillet_survives_upstream_edit():
    doc = Document()
    plate(doc)
    doc.add("fillet", {"radius": 5, "edges": "vertical&linear"})
    assert not doc.result.errors
    doc.edit("Extrude1", {"depth": 30})
    assert not doc.result.errors, doc.result.errors


def test_cut_from_top_face_and_pattern():
    doc = Document()
    plate(doc, holes=False)
    doc.add("sketch", {"plane": "top", "entities": [{"type": "rect", "x": -60, "y": 0, "w": 20, "h": 40}]})
    doc.add("extrude", {"sketch": "Sketch2", "operation": "cut", "depth": 5})
    doc.add("linear_pattern", {"feature": "Extrude2", "dir1": "X", "spacing1": 40, "count1": 4})
    assert not doc.result.errors, doc.result.errors
    assert doc.result.body.volume == pytest.approx(200 * 100 * 12 - 4 * 20 * 40 * 5, rel=1e-6)


def test_shell_revolve_mirror_circular():
    doc = Document()
    doc.add("sketch", {"plane": "XY", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 40}]})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 30})
    doc.add("shell", {"thickness": 3, "open_faces": "top"})
    doc.add("hole", {"face": "bottom", "diameter": 5, "positions": [[25, 0]]})
    doc.add("circular_pattern", {"feature": "Hole1", "count": 6})
    assert not doc.result.errors, doc.result.errors
    d2 = Document()
    d2.add("sketch", {"plane": "XZ", "entities": [{"type": "rect", "x": 30, "y": 10, "w": 10, "h": 20}]})
    d2.add("revolve", {"sketch": "Sketch1", "axis": "Z"})
    d2.add("mirror", {"plane": "XY"})
    assert not d2.result.errors, d2.result.errors


def test_error_isolation_and_dependents():
    doc = Document()
    plate(doc, holes=False)
    doc.add("fillet", {"radius": 500, "edges": "vertical"})
    assert "Fillet1" in doc.result.errors
    assert doc.result.body is not None   # body from before the failure survives


def test_save_open_export(tmp_path):
    doc = Document()
    plate(doc)
    doc.save(tmp_path / "p.daedalus")
    d2 = Document.open(tmp_path / "p.daedalus")
    assert d2.result.body.volume == pytest.approx(doc.result.body.volume)
    d2.export(tmp_path / "p.step")
    d2.export(tmp_path / "p.stl")
    assert (tmp_path / "p.step").stat().st_size > 1000


# ---------------------------------------------------------------- sketches
from daedalus.core.sketch2d import empty_geometry, solve, merge_points, remove_entities


def rect_geom(w=120, h=70):
    g = empty_geometry()
    g["points"].update({"p1": [-50, -30], "p2": [48, -31], "p3": [52, 29], "p4": [-49, 31]})
    g["curves"] = [{"id": f"l{i}", "type": "line", "a": f"p{i}", "b": f"p{i % 4 + 1}"} for i in range(1, 5)]
    g["constraints"] = [{"type": "horizontal", "e": ["l1"]}, {"type": "horizontal", "e": ["l3"]},
                        {"type": "vertical", "e": ["l2"]}, {"type": "vertical", "e": ["l4"]},
                        {"type": "length", "e": ["l1"], "value": w}, {"type": "length", "e": ["l2"], "value": h},
                        {"type": "symmetric", "e": ["p1", "p3", "O"]}]
    return g


def test_solver_dof_and_conflicts():
    g = rect_geom()
    r = solve(g, analyze=True)
    assert r.ok and r.dof == 0 and not r.free_points
    assert r.geometry["points"]["p3"] == pytest.approx([60, 35])
    g["constraints"].append({"type": "length", "e": ["l3"], "value": 99})
    bad = solve(g)
    assert not bad.ok and 7 in bad.failed
    g["constraints"].pop()
    g["constraints"].pop()          # drop symmetric → 2 DOF, all corners free
    r = solve(g, analyze=True)
    assert r.dof == 2 and r.free_points == {"p1", "p2", "p3", "p4"}


def test_constrained_sketch_drives_part_and_holes():
    g = rect_geom()
    g["points"]["c"] = [5, 3]
    g["curves"].append({"id": "c1", "type": "circle", "c": "c", "r": 7})
    g["constraints"] += [{"type": "coincident", "e": ["c", "O"]}, {"type": "diameter", "e": ["c1"], "value": 20}]
    doc = Document()
    doc.add("sketch", {"geometry": g})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 10})
    assert not doc.result.errors
    assert doc.result.sketch_status["Sketch1"]["dof"] == 0
    assert doc.result.body.volume == pytest.approx(10 * (120 * 70 - math.pi * 100), rel=1e-6)
    g2 = dict(g, constraints=[dict(k) for k in g["constraints"]])
    g2["constraints"][4]["value"] = 200
    doc.edit("Sketch1", {"geometry": g2})
    assert doc.mass_properties()["bbox_mm"][0] == pytest.approx(200)


def test_arc_profile_and_open_sketch():
    g = empty_geometry()
    g["points"].update({"a": [-20, 0], "b": [20, 0], "c": [0, 0]})
    g["curves"] = [{"id": "l1", "type": "line", "a": "a", "b": "b"},
                   {"id": "a1", "type": "arc", "c": "c", "a": "b", "b": "a"}]
    doc = Document()
    doc.add("sketch", {"geometry": g})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 5})
    assert not doc.result.errors
    assert doc.result.body.volume == pytest.approx(5 * math.pi * 400 / 2, rel=1e-6)
    g["curves"] = g["curves"][:1]
    doc.edit("Sketch1", {"geometry": g})
    assert "Sketch1" not in doc.result.errors           # open sketch itself is fine
    assert "Open profile" in doc.result.errors["Extrude1"]


def test_merge_and_remove():
    g = rect_geom()
    g["points"]["q"] = [100, 100]
    g["curves"].append({"id": "l9", "type": "line", "a": "p3", "b": "q"})
    merge_points(g, "p3", "q")                 # collapses l9 (zero length)
    assert not any(c["id"] == "l9" for c in g["curves"]) and "q" not in g["points"]
    remove_entities(g, {"l1"})
    assert all("l1" not in k["e"] for k in g["constraints"])


# ---------------------------------------------------------------- 3D
from daedalus.core import sketch3d as s3


def cart_geom():
    return {
        "points": {"a": [0, 0, 0], "b": [590, 5, 0], "c": [600, 410, 3], "d": [-5, 400, 0],
                   "e": [600, 400, 290], "f": [0, 400, 300]},
        "curves": [{"id": "l1", "type": "line", "a": "a", "b": "b"}, {"id": "l2", "type": "line", "a": "b", "b": "c"},
                   {"id": "l3", "type": "line", "a": "c", "b": "d"}, {"id": "l4", "type": "line", "a": "d", "b": "a"},
                   {"id": "l5", "type": "line", "a": "c", "b": "e"}, {"id": "l6", "type": "line", "a": "d", "b": "f"},
                   {"id": "l7", "type": "line", "a": "f", "b": "e"}],
        "constraints": [{"type": "coincident", "e": ["a", "O"]}, {"type": "along_x", "e": ["l1"]},
                        {"type": "along_y", "e": ["l2"]}, {"type": "along_x", "e": ["l3"]},
                        {"type": "along_y", "e": ["l4"]}, {"type": "along_z", "e": ["l5"]},
                        {"type": "along_z", "e": ["l6"]}, {"type": "length", "e": ["l1"], "value": 600},
                        {"type": "length", "e": ["l2"], "value": 400}, {"type": "length", "e": ["l5"], "value": 300},
                        {"type": "equal", "e": ["l5", "l6"]}],
    }


def test_3d_solver_redundancy_and_conflict():
    r = s3.solve(cart_geom(), analyze=True)
    assert r.ok and r.dof == 0 and r.redundant
    assert r.geometry["points"]["f"] == pytest.approx([0, 400, 300])
    g = cart_geom()
    g["constraints"].append({"type": "dz", "e": ["e", "a"], "value": 250})
    bad = s3.solve(g)
    assert not bad.ok and len(g["constraints"]) - 1 in bad.failed
    g["constraints"][-1]["value"] = 300
    assert s3.solve(g).ok


def test_3d_chains_split_at_junctions():
    r = s3.solve(cart_geom())
    ch = s3.chains(r.geometry)
    assert sum(len(seq) for seq, _ in ch) == 7
    assert all(len(seq) >= 1 for seq, _ in ch)


def test_frame_volume_mitres_and_cutlist():
    doc = Document()
    doc.add("sketch3d", {"geometry": cart_geom()})
    doc.add("frame", {"path": "3DSketch1", "profile": "square_tube", "width": 25.4, "thickness": 1.65})
    assert not doc.result.errors
    area = 25.4 ** 2 - (25.4 - 3.3) ** 2
    centerline = 600 * 3 + 400 * 2 + 300 * 2
    # junction overlaps make the real volume a little less than area × centerline
    assert doc.result.body.volume == pytest.approx(area * centerline, rel=0.03)
    rows = doc.cut_list()
    assert sum(r["qty"] for r in rows) == 7
    assert sum(r["qty"] * r["length"] for r in rows) == pytest.approx(centerline)


def test_closed_loop_frame_is_exact():
    g = cart_geom()
    g["curves"] = g["curves"][:4]
    g["constraints"] = [k for k in g["constraints"] if set(k["e"]) <= {"a", "O", "l1", "l2", "l3", "l4"}]
    doc = Document()
    doc.add("sketch3d", {"geometry": g})
    doc.add("frame", {"path": "3DSketch1", "profile": "rect_tube", "width": 50.8, "height": 25.4, "thickness": 3})
    area = 50.8 * 25.4 - (50.8 - 6) * (25.4 - 6)
    assert doc.result.body.volume == pytest.approx(area * 2000, rel=1e-6)


def test_sweep_along_3d_path():
    g = {"points": {"p1": [0, 0, 0], "p2": [200, 0, 0], "p3": [200, 0, 150]},
         "curves": [{"id": "l1", "type": "line", "a": "p1", "b": "p2"},
                    {"id": "l2", "type": "line", "a": "p2", "b": "p3"}], "constraints": []}
    doc = Document()
    doc.add("sketch3d", {"geometry": g})
    doc.add("sketch", {"plane": "YZ", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 10}]})
    doc.add("sweep", {"profile": "Sketch1", "path": "3DSketch1"})
    assert not doc.result.errors, doc.result.errors
    assert doc.result.body.volume == pytest.approx(math.pi * 100 * 350, rel=0.02)


def test_3d_tool_schema():
    from daedalus.ai.tools import TOOLS
    t = next(t for t in TOOLS if t["name"] == "add_sketch3d")
    enum = t["input_schema"]["properties"]["geometry"]["properties"]["constraints"]["items"]["properties"]["type"]["enum"]
    assert "along_z" in enum and "horizontal" not in enum
