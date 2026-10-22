"""v1.1: sheet-metal options, folded/flat state, 2D/3D snapping, auto relations, new sketch relations."""
import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from daedalus.core import snap2d, snap3d  # noqa: E402
from daedalus.core.document import Document  # noqa: E402
from daedalus.core.features import FeatureError  # noqa: E402


def sm(flanges, w=100, h=60, hole=True, **kw):
    d = Document()
    ents = [{"type": "rect", "x": 0, "y": 0, "w": w, "h": h}]
    if hole:
        ents.append({"type": "circle", "x": 0, "y": 0, "r": 8, "mode": "subtract"})
    d.add("sketch", {"entities": ents})
    d.add("sheet_metal", {"sketch": "Sketch1", "thickness": 2, "bend_radius": 2, "flanges": flanges, **kw})
    assert not d.result.errors, d.result.errors
    return d


T, R, K = 2.0, 2.0, 0.44
BA90 = math.pi / 2 * (R + K * T)


def test_material_inside_outer_length():
    d = sm([{"edge": "+X", "length": 30}])
    bb = d.result.bodies["Object1"].bounding_box()
    assert (bb.size.X, bb.size.Y, bb.size.Z) == pytest.approx((100, 60, 30))     # outside flush + outer height
    fl = d.result.flats["SheetMetal1"]
    assert fl["size"][0] == pytest.approx(100 - (R + T) + BA90 + 30 - (R + T), abs=1e-3)
    assert d.result.bodies["Object1"].is_valid


@pytest.mark.parametrize("pos,ref,width,flat", [
    ("bend_outside", "tangent", 100 + R + T, 100 + BA90 + 30),
    ("material_outside", "inner", 100 + T, 100 - R + BA90 + 30 - R),
])
def test_position_and_length_ref(pos, ref, width, flat):
    d = sm([{"edge": "+X", "length": 30}], position=pos, length_ref=ref)
    assert d.result.bodies["Object1"].bounding_box().size.X == pytest.approx(width, abs=1e-6)
    assert d.result.flats["SheetMetal1"]["size"][0] == pytest.approx(flat, abs=1e-3)


def test_hem_relief_corner_and_flat_state():
    fls = [{"edge": "+X", "length": 30, "hem": "closed", "hem_length": 8},
           {"edge": "-X", "length": 20, "angle": 120, "down": True},
           {"edge": "+Y", "length": 15, "start_offset": 20, "end_offset": 20}, {"edge": "-Y", "length": 15}]
    d = sm(fls, corner_relief="circular", relief="obround")
    b = d.result.bodies["Object1"]
    assert b.is_valid and len(b.solids()) == 1
    fl = d.result.flats["SheetMetal1"]
    assert len(fl["bends"]) == 5 and any(bd.get("hem") == "closed" for bd in fl["bends"])
    assert fl["outline"][0]["holes"]                                  # the base hole is in the flat
    vol_folded = b.volume
    d.set_sheet_metal_state("flat")
    flat = d.result.bodies["Object1"]
    assert flat.bounding_box().size.Z == pytest.approx(T)
    area = sum(f.area for f in __import__("daedalus.core.sheetmetal", fromlist=["x"]).flat_pattern_face(fl))
    assert flat.volume == pytest.approx(area * T, rel=1e-6)
    assert flat.volume == pytest.approx(vol_folded, rel=0.03)          # neutral-axis length is conserved
    d.set_sheet_metal_state("folded")
    assert d.result.bodies["Object1"].volume == pytest.approx(vol_folded)


def test_gauge_and_bend_deduction():
    d = sm([{"edge": "+X", "length": 30}], gauge="Steel 16 ga", method="bend_deduction", bend_value=3.5)
    fl = d.result.flats["SheetMetal1"]
    assert fl["thickness"] == pytest.approx(1.519)
    t = 1.519
    ba90 = 2 * (R + t) - 3.5
    assert fl["bends"][0]["allowance"] == pytest.approx(ba90, abs=1e-6)


def test_add_flange_from_point():
    d = sm([], hole=False)
    fl = d.add_flange("SheetMetal1", [0, 30, 1], 25, 90)              # side face at +Y
    assert not d.result.errors
    bb = d.result.bodies["Object1"].bounding_box()
    assert bb.size.Z == pytest.approx(25) and bb.size.Y == pytest.approx(60)
    with pytest.raises(FeatureError):
        d.add_flange("SheetMetal1", [0, 30, 1], 25, 90, bogus=1)


# ------------------------------------------------------------------- snapping

def test_snap2d_kinds_and_relations():
    g = {"points": {"O": [0, 0], "a": [-20, 0], "b": [40, 0], "c": [60, 30]},
         "curves": [{"id": "l1", "type": "line", "a": "a", "b": "b"},
                    {"id": "c1", "type": "circle", "c": "c", "r": 10},
                    {"id": "l2", "type": "line", "a": "O", "b": "c"}], "constraints": []}
    items, pts = snap2d.sketch_items(g)
    s = snap2d.find(items, pts, 10.2, 0.3, 1.0)
    assert s.kind == "midpoint" and s.xy == pytest.approx((10, 0))
    assert snap2d.relations_for(s, "p9") == [{"type": "midpoint", "e": ["p9", "l1"]}]
    s = snap2d.find(items, pts, 60.2, 39.8, 1.0)
    assert s.kind == "quadrant" and s.xy == pytest.approx((60, 40))
    # line O→c crosses the circle: intersection beats nearest
    ix = 60 - 10 * 60 / math.hypot(60, 30)
    iy = 30 - 10 * 30 / math.hypot(60, 30)
    s = snap2d.find(items, pts, ix + 0.3, iy - 0.2, 1.0)
    assert s.kind == "intersection" and s.xy == pytest.approx((ix, iy), abs=1e-9)
    s = snap2d.find(items, pts, 0.2, 25, 0.5, from_pt=(0.0, 25.0))
    assert s.kind == "inference"
    s = snap2d.find(items, pts, 13.0, 0.4, 1.0, from_pt=(13.0, 20.0))
    assert s.kind in ("perpendicular", "nearest") and s.xy == pytest.approx((13, 0))
    s = snap2d.find(items, pts, 37.0, 21.0, 0.5, opts={"inference": False}, from_pt=(0.0, 0.0), angle_step=30)
    assert s is not None and s.kind == "angle" and s.refs["angle"] == 30


def test_sketcher_auto_relations():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QPointF
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.ui.sketcher import SketchEditor
    ed = SketchEditor()
    c = ed.canvas
    c.resize(800, 600)
    c.load({"points": {"O": [0, 0], "a": [-20, 0], "b": [40, 0]},
            "curves": [{"id": "l1", "type": "line", "a": "a", "b": "b"}], "constraints": []})
    c.snap_opts.update(snap2d.DEFAULTS)
    c.auto_relations = True
    c.scale, c.off = 5.0, QPointF(400, 300)
    c.set_tool("line")
    for x, y in ((10.2, 0.2), (10.3, 30.0)):
        pos = c.w2s(x, y)
        c._update_snap(pos)
        c._tool_click(pos)
    types = sorted(k["type"] for k in c.geom["constraints"])
    assert "midpoint" in types and "vertical" in types
    assert c.result.ok
    # concentric / collinear relations
    c.load({"points": {"O": [0, 0], "c1": [0, 0.5], "c2": [3, 1], "a": [0, 10], "b": [10, 10], "d": [12, 11],
                       "e": [20, 11]},
            "curves": [{"id": "k1", "type": "circle", "c": "c1", "r": 5}, {"id": "k2", "type": "circle", "c": "c2",
                                                                         "r": 3},
                       {"id": "l1", "type": "line", "a": "a", "b": "b"}, {"id": "l2", "type": "line", "a": "d",
                                                                         "b": "e"}], "constraints": []})
    c.sel = {"k1", "k2"}
    assert "concentric" in c.available_relations()
    c.apply_relation("coradial")
    k = {cc["id"]: cc for cc in c.geom["curves"]}
    assert k["k1"]["c"] == k["k2"]["c"] and k["k1"]["r"] == pytest.approx(k["k2"]["r"])
    c.sel = {"l1", "l2"}
    c.apply_relation("collinear")
    P = c.geom["points"]
    ab = np.subtract(P["b"], P["a"])
    for q in ("d", "e"):
        aq = np.subtract(P[q], P["a"])
        assert abs(ab[0] * aq[1] - ab[1] * aq[0]) < 1e-6 * np.linalg.norm(ab) * max(np.linalg.norm(aq), 1)


def test_snap3d_and_free_drag():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 40},
                                  {"type": "circle", "x": 0, "y": 0, "r": 5, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    cand = snap3d.candidates(d.result.bodies["Object1"])
    assert {"endpoint", "midpoint", "center", "face center", "quadrant"} <= set(cand["kinds"])
    proj = lambda W: np.asarray(W)[:, :2] * 10.0    # top view, 10 px/mm  # noqa: E731
    hit = snap3d.find([("Object1", cand, None)], proj, 0.3, 0.2, tol=8, origin=False)
    assert hit["kind"] in ("center", "face center") and hit["p"][:2] == pytest.approx([0, 0])
    hit = snap3d.find([("Object1", cand, None)], proj, 200.4, 52.0, tol=8)
    assert hit["kind"] == "on edge" and hit["p"][0] == pytest.approx(20) and hit["p"][1] == pytest.approx(5.2)
    d.add_object("B", pose=[60, 0, 0, 0, 0, 0])
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}]}, obj="B")
    d.add("extrude", {"sketch": "Sketch2", "depth": 5}, obj="B")
    d.solve_assembly(drag=("B", [5, 5, 5], np.array([20.0, 20.0, 10.0])))
    assert d.obj("B").pose[:3] == pytest.approx([15, 15, 5])


def _top(W):
    return np.asarray(W, float).reshape(-1, 3)[:, :2] * 10.0       # top view, 10 px/mm


def _ray(x, y):
    return (np.array([x / 10.0, y / 10.0, 100.0]), np.array([0, 0, -1.0]))


def test_snap3d_inference_kinds():
    pts = {"a": [-10, 0, 0], "b": [10, 0, 0], "c": [3, -10, 0], "d": [3, 20, 0], "e": [20, 20, 0], "f": [30, 30, 0]}
    sk = snap3d.sketch_candidates(pts, [("l1", "a", "b"), ("l2", "c", "d"), ("l3", "e", "f")])
    sets = [("sketch", sk, None)]
    h = snap3d.find(sets, _top, 31.0, 1.5, 8, origin=False, ray=_ray(31.0, 1.5))
    assert h["kind"] == "intersection" and h["p"] == pytest.approx([3, 0, 0])
    h = snap3d.find(sets, _top, 50.0, 1.0, 8, anchor=[5, 6, 0], origin=False, ray=_ray(50, 1))
    assert h["kind"] == "perpendicular" and h["p"] == pytest.approx([5, 0, 0]) and h["refs"]["line"] == "l1"
    h = snap3d.find(sets, _top, 150.0, 0.5, 8, origin=False, ray=_ray(150, 0.5),
                    opts={"on edge": False})
    assert h["kind"] == "extension" and h["p"] == pytest.approx([15, 0, 0])
    h = snap3d.find(sets, _top, 700.0, -150.3, 8, anchor=[40, -15, 0], origin=False, ray=_ray(700, -150.3))
    assert h["kind"] in ("along axis", "aligned") and h["p"][1] == pytest.approx(-15)
    h = snap3d.find(sets, _top, 500.0, -50.0, 8, anchor=[40, -15, 0], origin=False, ray=_ray(500, -50),
                    opts={"along axis": False, "aligned": False})
    assert h["kind"] == "parallel" and h["p"][0] - 40 == pytest.approx(h["p"][1] + 15)
    p, note = snap3d.increments([0, 0, 0], [7.2, 3.9, 0], 5.0, 15.0, [0, 0, 1])
    assert np.linalg.norm(p) == pytest.approx(10.0) and math.degrees(math.atan2(p[1], p[0])) == pytest.approx(30)


def test_snap3d_body_tangent_and_face():
    d = Document()
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 10}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 5})
    cand = snap3d.candidates(d.result.bodies["Object1"])
    sets = [("Object1", cand, None)]
    tx, ty = 10 * math.cos(math.acos(10 / 30)) * 10, 10 * math.sin(math.acos(10 / 30)) * 10   # px
    h = snap3d.find(sets, _top, tx, ty, 6, anchor=[30, 0, 5], origin=False, ray=_ray(tx, ty),
                    opts={"on edge": False, "quadrant": False})
    assert h["kind"] == "tangent"
    assert np.linalg.norm(np.subtract(h["p"][:2], [0, 0])) == pytest.approx(10)
    h = snap3d.find(sets, _top, 40.0, 30.0, 6, origin=False, ray=_ray(40, 30))
    assert h["kind"] == "on face" and h["p"][2] == pytest.approx(5) and h["normal"] == pytest.approx([0, 0, 1])
    assert h["face"] is not None
