"""SolidWorks-style sketch entities and tools: every click tool builds solvable geometry, new curve types make
profiles, and the sketch tools (chamfer, split, move/rotate/scale/copy, segments, belt, convert) work."""
import math
import os

import pytest

from daedalus.core import sketch2d as sk
from daedalus.core import sketch_ops as so
from daedalus.core import sketch_shapes as S

CLICKS = {
    "line": [(0, 0), (20, 0)], "centerline": [(0, 0), (10, 10)], "midline": [(0, 0), (10, 5)],
    "infline": [(0, 0), (10, 5)], "rect": [(0, 0), (40, 20)], "rect_center": [(0, 0), (20, 10)],
    "rect_3pt": [(0, 0), (30, 10), (20, 30)], "rect_3pt_center": [(0, 0), (15, 5), (0, 12)],
    "parallelogram": [(0, 0), (30, 0), (40, 20)], "slot": [(0, 0), (40, 0), (20, 6)],
    "slot_center": [(0, 0), (20, 10), (5, 12)], "slot_arc3": [(30, 0), (0, 30), (21.2, 21.2), (25, 25)],
    "slot_arc_center": [(0, 0), (30, 0), (0, 30), (34, 0)], "circle": [(0, 0), (10, 0)],
    "circle_perim": [(0, 0), (10, 0), (5, 8)], "arc": [(0, 0), (10, 0), (0, 10)], "arc3": [(0, 0), (20, 0), (10, 5)],
    "polygon": [(0, 0), (10, 0)], "point": [(5, 5)], "ellipse": [(0, 0), (20, 0), (0, 8)],
    "ellipse_partial": [(0, 0), (20, 0), (0, 8), (20, 0.1), (0, 8)], "parabola": [(0, 0), (20, 0), (10, 15)],
    "conic": [(0, 0), (20, 0), (10, 15)], "spline": [(0, 0), (10, 8), (20, -3), (30, 4)],
}
CLOSED_AREA = {"rect": 800, "rect_center": 800, "rect_3pt": 700, "parallelogram": 600, "circle": math.pi * 100,
               "ellipse": math.pi * 160, "polygon": 259.8}


@pytest.mark.parametrize("tool", sorted(CLICKS))
def test_every_tool_solves(tool):
    assert tool in S.TOOLS
    g = S.build(tool, sk.empty_geometry(), [(None, p) for p in CLICKS[tool]], opts={"sides": 6, "rho": 0.7})
    r = sk.solve(g)
    assert r.ok, (tool, r.status)
    if tool in CLOSED_AREA:
        a = sum(f.area for f in sk.profile_faces(r.geometry))
        assert abs(a - CLOSED_AREA[tool]) < 1.0, (tool, a)


def test_slots_are_closed_profiles():
    for tool in ("slot", "slot_center", "slot_arc3", "slot_arc_center"):
        g = S.build(tool, sk.empty_geometry(), [(None, p) for p in CLICKS[tool]])
        assert len(sk.profile_faces(sk.solve(g).geometry)) == 1, tool
    g = S.build("slot", sk.empty_geometry(), [(None, p) for p in CLICKS["slot"]])
    expect = 40 * 12 + math.pi * 36
    assert abs(sk.profile_faces(g)[0].area - expect) < 0.5


def test_polygon_options_and_limits():
    g = S.build("polygon", sk.empty_geometry(), [(None, (0, 0)), (None, (10, 0))], opts={"sides": 8,
                                                                                      "circumscribed": True})
    assert len([c for c in g["curves"] if c["type"] == "line"]) == 8
    with pytest.raises(sk.SketchError):
        S.build("polygon", sk.empty_geometry(), [(None, (0, 0)), (None, (10, 0))], opts={"sides": 41})


def test_tangent_arc_from_line_and_arc():
    g = S.build("line", sk.empty_geometry(), [(None, (0, 0)), (None, (20, 0))])
    end = next(k for k, v in g["points"].items() if v == [20.0, 0.0])
    g = S.build("arc_tangent", g, [(end, (20, 0)), (None, (30, 10))])
    r = sk.solve(g)
    assert r.ok and any(k["type"] == "tangent" for k in g["constraints"])
    with pytest.raises(sk.SketchError):
        S.build("arc_tangent", sk.empty_geometry(), [(None, (0, 0)), (None, (5, 5))])


def test_spline_closed_equation_text():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    g = S.build("spline", sk.empty_geometry(), [(None, p) for p in sq])
    assert g["curves"][0]["closed"] and abs(sum(f.area for f in sk.profile_faces(g)) - 160) < 20
    g = S.equation_curve(sk.empty_geometry(), "parametric", "20*cos(t)", "10*sin(t)", 0, 2 * math.pi)
    assert abs(sum(f.area for f in sk.profile_faces(g)) - math.pi * 200) < 2
    g = S.equation_curve(sk.empty_geometry(), "explicit", "", "x^2/10", -10, 10)
    assert len(sk.samples(g, g["curves"][0])) > 10
    with pytest.raises(sk.SketchError):
        sk.eval_expr("__import__('os')")
    g = S.text_entity(sk.empty_geometry(), (0, 0), "P/N {PartNo}", 8)
    assert sk.text_value(g["curves"][0], {"PartNo": "RG-1"}) == "P/N RG-1"
    assert len(sk.profile_faces(g, {"PartNo": "RG-1"})) > 3


def test_sketch_tools_ops():
    g = S.build("rect", sk.empty_geometry(), [(None, (0, 0)), (None, (40, 20))])
    corner = next(k for k, v in g["points"].items() if v == [40.0, 20.0])
    ch = so.chamfer(g, corner, 5)
    assert sk.solve(ch).ok and abs(sk.profile_faces(ch)[0].area - (800 - 12.5)) < 1e-6
    l0 = g["curves"][0]["id"]
    sp = so.split(g, l0, (10, 0))
    assert sk.solve(sp).ok and len(sp["curves"]) == 5
    ids = [c["id"] for c in g["curves"]]
    mv = so.transform(g, ids, "move", dx=5, dy=5)
    assert sk.solve(mv).ok and min(x for x, _y in mv["points"].values() if (x, _y) != (0.0, 0.0)) == 5
    sc = so.transform(g, ids, "scale", scale=2, about=(0, 0))
    assert abs(sk.profile_faces(sk.solve(sc).geometry)[0].area - 3200) < 1e-6
    rot = so.transform(g, ids, "rotate", angle=90, about=(0, 0))
    assert abs(sk.profile_faces(sk.solve(rot).geometry)[0].area - 800) < 1e-6
    cp = so.transform(g, ids, "move", copy_=True, dx=100)
    assert len(cp["curves"]) == 8 and len(sk.profile_faces(cp)) == 2
    seg = so.segment_points(g, l0, 4)
    assert sum(1 for c in seg["curves"] if c["type"] == "point") == 3 and sk.solve(seg).ok


def test_belt():
    g = sk.empty_geometry()
    for c, r in (((0, 0), 10), ((60, 0), 5), ((30, 40), 7)):
        g = S.build("circle", g, [(None, c), (None, (c[0] + r, c[1]))])
    ids = [c["id"] for c in g["curves"]]
    b = so.belt(g, ids)
    r = sk.solve(b)
    assert r.ok
    belt = [c["id"] for c in b["curves"] if c.get("belt")]
    assert len(belt) == 6
    L = so.belt_length(r.geometry, belt)
    assert 180 < L < 240
    with pytest.raises(sk.SketchError):
        so.belt(g, ids[:1])


def test_mirror_and_pattern_new_types():
    g = S.build("ellipse", sk.empty_geometry(), [(None, (10, 0)), (None, (20, 0)), (None, (10, 4))])
    g = S.build("line", g, [(None, (0, -10)), (None, (0, 10))])
    e, axis = g["curves"][0]["id"], g["curves"][1]["id"]
    m = so.mirror(g, [e, axis], axis)
    assert sum(1 for c in m["curves"] if c["type"] == "ellipse") == 2 and sk.solve(m).ok
    pat = so.linear_pattern(g, [e], 0, 20, 3)
    assert sum(1 for c in pat["curves"] if c["type"] == "ellipse") == 3


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_canvas_clicks_and_toolbar(qapp):
    from PySide6.QtCore import QPointF
    from daedalus.ui.sketcher import SketchEditor
    ed = SketchEditor()
    ed.resize(1200, 700)
    cv = ed.canvas
    cv.load(None)

    def click(tool, pts):
        cv.set_tool(tool)
        for x, y in pts:
            cv.mouse_w, cv.snap, cv.snap_info = (x, y), None, None
            cv._tool_click(cv.w2s(x, y))
    click("slot", [(0, 0), (40, 0), (20, 6)])
    click("polygon", [(100, 0), (110, 0)])
    click("ellipse", [(0, 60), (20, 60), (0, 68)])
    click("spline", [(60, 60), (70, 70), (80, 55)])
    assert cv.finish_open_tool()
    types = [c["type"] for c in cv.geom["curves"]]
    assert types.count("arc") == 2 and "ellipse" in types and "spline" in types
    assert cv.result.ok
    # preview paints without errors for every tool
    from PySide6.QtGui import QImage, QPainter
    img = QImage(1200, 700, QImage.Format_ARGB32)
    for tool, pts in CLICKS.items():
        cv.set_tool(tool)
        cv.pending = [(None, p) for p in pts[:-1]]
        cv.mouse_w = pts[-1]
        p = QPainter(img)
        cv._paint_preview(p)
        p.end()
    cv.set_tool("select")
    cv.render(img)
    # flyout remembers the picked tool
    fl = ed.flyouts["Rectangle"]
    fl.menu().actions()[1].trigger()
    assert cv.tool == "rect_center" and "Center rectangle" in fl.text()
    ed._sync()
    assert fl.isChecked()
    # hit-testing a free-form curve
    sid = next(c["id"] for c in cv.geom["curves"] if c["type"] == "ellipse")
    assert cv._hit_curve(cv.w2s(20, 60)) == sid
