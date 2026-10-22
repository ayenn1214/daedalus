"""Click-to-shape builders for the 2D sketcher (Qt-free): rectangles, slots, circles, arcs, polygons, ellipses,
conics, splines, points and line variants — each made of ordinary sketch geometry plus the relations that keep
it that shape (SolidWorks-style).

A builder gets the clicks so far — [(snap point id or None, (x, y))] — and returns the new geometry.
`newpt(g, xy, snap_id)` makes (or reuses) a point; the sketcher passes its own so snaps add relations.
"""
from __future__ import annotations

import copy
import math

from . import sketch2d as sk
from .sketch2d import SketchError

# tool key: (label, group, clicks needed (None = until finished), hint per click)
TOOLS = {
    "line": ("Line", "Line", 2, ["start", "end (keeps chaining; Esc ends)"]),
    "centerline": ("Centerline", "Line", 2, ["start", "end"]),
    "midline": ("Midpoint line", "Line", 2, ["middle", "one end (the other mirrors it)"]),
    "infline": ("Infinite line", "Line", 2, ["a point on it", "its direction"]),
    "rect": ("Corner rectangle", "Rectangle", 2, ["first corner", "opposite corner"]),
    "rect_center": ("Center rectangle", "Rectangle", 2, ["centre", "a corner"]),
    "rect_3pt": ("3 point corner rectangle", "Rectangle", 3, ["first corner", "end of the first side", "width"]),
    "rect_3pt_center": ("3 point center rectangle", "Rectangle", 3, ["centre", "middle of a side", "width"]),
    "parallelogram": ("Parallelogram", "Rectangle", 3, ["first corner", "second corner", "third corner"]),
    "slot": ("Straight slot", "Slot", 3, ["first end centre", "second end centre", "width"]),
    "slot_center": ("Centerpoint straight slot", "Slot", 3, ["slot centre", "end centre", "width"]),
    "slot_arc3": ("3 point arc slot", "Slot", 4, ["start centre", "end centre", "a point on the arc", "width"]),
    "slot_arc_center": ("Centerpoint arc slot", "Slot", 4, ["arc centre", "start centre", "end centre", "width"]),
    "circle": ("Circle", "Circle", 2, ["centre", "radius"]),
    "circle_perim": ("Perimeter circle", "Circle", 3, ["a point on it", "second point", "third point"]),
    "arc": ("Centerpoint arc", "Arc", 3, ["centre", "start", "end"]),
    "arc_tangent": ("Tangent arc", "Arc", 2, ["the end of a line or arc", "end"]),
    "arc3": ("3 point arc", "Arc", 3, ["start", "end", "a point on the arc"]),
    "polygon": ("Polygon", "Polygon", 2, ["centre", "a vertex (or side middle)"]),
    "point": ("Point", "Point", 1, ["where"]),
    "ellipse": ("Ellipse", "Ellipse", 3, ["centre", "major axis end", "minor axis"]),
    "ellipse_partial": ("Partial ellipse", "Ellipse", 5, ["centre", "major axis end", "minor axis", "start",
                                                          "end"]),
    "parabola": ("Parabola", "Conic", 3, ["start", "end", "vertex control (tangent meeting point)"]),
    "conic": ("Conic", "Conic", 3, ["start", "end", "top (tangent meeting point)"]),
    "spline": ("Spline", "Spline", None, ["first point", "next point (double-click / Enter finishes)"]),
}
GROUPS = ["Line", "Rectangle", "Slot", "Circle", "Arc", "Polygon", "Ellipse", "Conic", "Spline", "Point"]


def needed(tool):
    return TOOLS[tool][2]


def hint(tool, n):
    h = TOOLS[tool][3]
    return h[min(n, len(h) - 1)]


def _v(a, b):
    return (b[0] - a[0], b[1] - a[1])


def _len(v):
    return math.hypot(v[0], v[1])


def _unit(v):
    L = _len(v)
    if L < 1e-9:
        raise SketchError("Points are too close together")
    return (v[0] / L, v[1] / L)


def _perp(u):
    return (-u[1], u[0])


def _add(p, v, k=1.0):
    return (p[0] + v[0] * k, p[1] + v[1] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def circumcenter(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        raise SketchError("The three points are in a line")
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy


def _ccw(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) > 0


def _default_newpt(g, xy, sid):
    if sid and sid in g["points"]:
        return sid
    pid = sk.new_id(g, "p")
    g["points"][pid] = [float(xy[0]), float(xy[1])]
    return pid


class _B:
    """Geometry being built."""

    def __init__(self, geom, newpt):
        self.g = copy.deepcopy(geom)
        self.newpt = newpt or _default_newpt

    def pt(self, xy, sid=None):
        return self.newpt(self.g, xy, sid)

    def line(self, a, b, **kw):
        lid = sk.new_id(self.g, "l")
        self.g["curves"].append({"id": lid, "type": "line", "a": a, "b": b, **kw})
        return lid

    def arc(self, c, a, b, **kw):
        aid = sk.new_id(self.g, "a")
        self.g["curves"].append({"id": aid, "type": "arc", "c": c, "a": a, "b": b, **kw})
        return aid

    def circle(self, c, r, **kw):
        cid = sk.new_id(self.g, "c")
        self.g["curves"].append({"id": cid, "type": "circle", "c": c, "r": float(r), **kw})
        return cid

    def con(self, t, *e, value=None):
        k = {"type": t, "e": list(e)}
        if value is not None:
            k["value"] = float(value)
        self.g["constraints"].append(k)

    def xy(self, pid):
        return tuple(self.g["points"][pid])


def build(tool, geom, clicks, newpt=None, opts=None):
    """New geometry for a finished tool (len(clicks) == needed(tool), or ≥ 2 for a spline)."""
    opts = opts or {}
    b = _B(geom, newpt)
    fn = globals().get("_" + tool)
    if fn is None:
        raise SketchError(f"Unknown sketch tool {tool}")
    fn(b, clicks, opts)
    return b.g


# ------------------------------------------------------------------ lines
def _line(b, k, o):
    (s0, p0), (s1, p1) = k
    if _len(_v(p0, p1)) < 1e-9:
        raise SketchError("Zero-length line")
    b.line(b.pt(p0, s0), b.pt(p1, s1))


def _centerline(b, k, o):
    (s0, p0), (s1, p1) = k
    if _len(_v(p0, p1)) < 1e-9:
        raise SketchError("Zero-length line")
    b.line(b.pt(p0, s0), b.pt(p1, s1), construction=True)


def _midline(b, k, o):
    (sm, m), (s1, p1) = k
    p0 = _add(m, _v(p1, m))
    if _len(_v(p0, p1)) < 1e-9:
        raise SketchError("Zero-length line")
    mid = b.pt(m, sm)
    lid = b.line(b.pt(p0), b.pt(p1, s1))
    b.con("midpoint", mid, lid)


def _infline(b, k, o):
    (s0, p0), (_s1, p1) = k
    u = _unit(_v(p0, p1))
    a = b.pt(p0, s0)
    q = b.pt(_add(p0, u, max(10.0, _len(_v(p0, p1)))))
    b.line(a, q, construction=True, infinite=True)


# ------------------------------------------------------------------ rectangles
def _quad(b, P, sids=(None, None, None, None), **kw):
    pts = [b.pt(P[i], sids[i]) for i in range(4)]
    return pts, [b.line(pts[i], pts[(i + 1) % 4], **kw) for i in range(4)]


def _rect(b, k, o):
    (s0, (x0, y0)), (s1, (x1, y1)) = k
    if abs(x1 - x0) < 1e-6 or abs(y1 - y0) < 1e-6:
        raise SketchError("Rectangle needs width and height")
    _, L = _quad(b, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], (s0, None, s1, None))
    b.con("horizontal", L[0])
    b.con("horizontal", L[2])
    b.con("vertical", L[1])
    b.con("vertical", L[3])


def _rect_center(b, k, o):
    (sc, (cx, cy)), (s1, (x1, y1)) = k
    dx, dy = x1 - cx, y1 - cy
    if abs(dx) < 1e-6 or abs(dy) < 1e-6:
        raise SketchError("Rectangle needs width and height")
    pts, L = _quad(b, [(cx - dx, cy - dy), (cx + dx, cy - dy), (cx + dx, cy + dy), (cx - dx, cy + dy)],
                   (None, None, s1, None))
    b.con("horizontal", L[0])
    b.con("horizontal", L[2])
    b.con("vertical", L[1])
    b.con("vertical", L[3])
    d1 = b.line(pts[0], pts[2], construction=True)
    b.line(pts[1], pts[3], construction=True)
    b.con("midpoint", b.pt((cx, cy), sc), d1)


def _rect_3pt(b, k, o):
    (s0, p0), (s1, p1), (_s2, p2) = k
    u = _unit(_v(p0, p1))
    n = _perp(u)
    w = _dot(_v(p1, p2), n)
    if abs(w) < 1e-6:
        raise SketchError("Rectangle needs a width")
    _, L = _quad(b, [p0, p1, _add(p1, n, w), _add(p0, n, w)], (s0, s1, None, None))
    b.con("perpendicular", L[0], L[1])
    b.con("parallel", L[0], L[2])
    b.con("parallel", L[1], L[3])


def _rect_3pt_center(b, k, o):
    (sc, c), (_s1, m), (_s2, p2) = k
    h = _v(c, m)
    u = _unit(h)
    n = _perp(u)
    w = _dot(_v(c, p2), n)
    if abs(w) < 1e-6:
        raise SketchError("Rectangle needs a width")
    P = [_add(_add(c, h, -1), n, -w), _add(_add(c, h, 1), n, -w), _add(_add(c, h, 1), n, w), _add(_add(c, h, -1), n, w)]
    pts, L = _quad(b, P)
    b.con("perpendicular", L[0], L[1])
    b.con("parallel", L[0], L[2])
    b.con("parallel", L[1], L[3])
    d1 = b.line(pts[0], pts[2], construction=True)
    b.line(pts[1], pts[3], construction=True)
    b.con("midpoint", b.pt(c, sc), d1)


def _parallelogram(b, k, o):
    (s0, p0), (s1, p1), (s2, p2) = k
    p3 = _add(p0, _v(p1, p2))
    if abs((p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])) < 1e-9:
        raise SketchError("The three corners are in a line")
    _, L = _quad(b, [p0, p1, p2, p3], (s0, s1, s2, None))
    b.con("parallel", L[0], L[2])
    b.con("parallel", L[1], L[3])


# ------------------------------------------------------------------ slots
def _straight_slot(b, c1, c2, r, sid1=None, sid2=None, center=None):
    u = _unit(_v(c1, c2))
    n = _perp(u)
    p1, p2 = b.pt(c1, sid1), b.pt(c2, sid2)
    A1, B1 = b.pt(_add(c1, n, r)), b.pt(_add(c1, n, -r))
    A2, B2 = b.pt(_add(c2, n, r)), b.pt(_add(c2, n, -r))
    la = b.line(A1, A2)
    lb = b.line(B1, B2)
    a1 = b.arc(p1, A1, B1)
    a2 = b.arc(p2, B2, A2)
    cl = b.line(p1, p2, construction=True)
    for a in (a1, a2):
        b.con("tangent", a, la)
        b.con("tangent", a, lb)
    b.con("equal", a1, a2)
    if center is not None:
        b.con("midpoint", center, cl)
    return cl


def _slot_width(c1, c2, p):
    u = _unit(_v(c1, c2))
    r = abs(_dot(_v(c1, p), _perp(u)))
    if r < 1e-6:
        raise SketchError("Slot needs a width")
    return r


def _slot(b, k, o):
    (s0, c1), (s1, c2), (_s2, p) = k
    _straight_slot(b, c1, c2, _slot_width(c1, c2, p), s0, s1)


def _slot_center(b, k, o):
    (sc, c), (s1, c2), (_s2, p) = k
    c1 = _add(c, _v(c2, c))
    mid = b.pt(c, sc)
    _straight_slot(b, c1, c2, _slot_width(c1, c2, p), None, s1, center=mid)


def _arc_slot(b, O, s, e, r, so=None, ss=None, se=None):
    R = _len(_v(O, s))
    if r >= R - 1e-6:
        raise SketchError("Slot width is bigger than the arc radius")
    e = _add(O, _unit(_v(O, e)), R)                       # end centre on the centreline arc
    o = b.pt(O, so)
    ps, pe = b.pt(s, ss), b.pt(e, se)
    us, ue = _unit(_v(O, s)), _unit(_v(O, e))
    So, Si = b.pt(_add(s, us, r)), b.pt(_add(s, us, -r))
    Eo, Ei = b.pt(_add(e, ue, r)), b.pt(_add(e, ue, -r))
    outer = b.arc(o, So, Eo)
    inner = b.arc(o, Si, Ei)
    cap_s = b.arc(ps, Si, So)
    cap_e = b.arc(pe, Eo, Ei)
    for cap in (cap_s, cap_e):
        b.con("tangent", cap, outer)
        b.con("tangent", cap, inner)


def _slot_arc3(b, k, o):
    (s0, s), (s1, e), (_s2, m), (_s3, p) = k
    O = circumcenter(s, m, e)
    if not _ccw(s, m, e):
        s, e, s0, s1 = e, s, s1, s0
    R = _len(_v(O, s))
    r = abs(_len(_v(O, p)) - R)
    if r < 1e-6:
        raise SketchError("Slot needs a width")
    _arc_slot(b, O, s, e, r, None, s0, s1)


def _slot_arc_center(b, k, o):
    (so, O), (s1, s), (s2, e), (_s3, p) = k
    R = _len(_v(O, s))
    r = abs(_len(_v(O, p)) - R)
    if r < 1e-6:
        raise SketchError("Slot needs a width")
    _arc_slot(b, O, s, e, r, so, s1, s2)


# ------------------------------------------------------------------ circles / arcs
def _circle(b, k, o):
    (sc, c), (_s1, p) = k
    r = _len(_v(c, p))
    if r < 1e-6:
        raise SketchError("Circle needs a radius")
    b.circle(b.pt(c, sc), r)


def _circle_perim(b, k, o):
    (s0, p0), (s1, p1), (s2, p2) = k
    c = circumcenter(p0, p1, p2)
    cid = b.circle(b.pt(c), _len(_v(c, p0)))
    for sid in (s0, s1, s2):                              # passes through picked existing points
        if sid:
            b.con("point_on", sid, cid)


def _arc(b, k, o):
    (sc, c), (sa, a), (sb, e) = k
    r = _len(_v(c, a))
    if r < 1e-6:
        raise SketchError("Arc needs a radius")
    ang = math.atan2(e[1] - c[1], e[0] - c[0])
    e = (c[0] + r * math.cos(ang), c[1] + r * math.sin(ang))
    pc, pa, pb = b.pt(c, sc), b.pt(a, sa), b.pt(e, sb)
    if (a[0] - c[0]) * (e[1] - c[1]) - (a[1] - c[1]) * (e[0] - c[0]) < 0:
        pa, pb = pb, pa
    b.arc(pc, pa, pb)


def _arc3(b, k, o):
    (s0, s), (s1, e), (_s2, m) = k
    c = circumcenter(s, m, e)
    ps, pe = b.pt(s, s0), b.pt(e, s1)
    pc = b.pt(c)
    if _ccw(s, m, e):
        b.arc(pc, ps, pe)
    else:
        b.arc(pc, pe, ps)


def _arc_tangent(b, k, o):
    (s0, s), (s1, e) = k
    g = b.g
    if not s0 or s0 not in g["points"]:
        raise SketchError("Start a tangent arc on the end of a line or arc")
    host = None
    for c in g["curves"]:
        if c["type"] == "line" and s0 in (c["a"], c["b"]) and not c.get("construction"):
            other = c["b"] if c["a"] == s0 else c["a"]
            d = _unit(_v(b.xy(other), s))
            host = c
            break
        if c["type"] == "arc" and s0 in (c["a"], c["b"]):
            cc = b.xy(c["c"])
            rad = _unit(_v(cc, s))
            d = _perp(rad) if s0 == c["b"] else (rad[1], -rad[0])     # leaving the arc's direction of travel
            host = c
            break
    if host is None:
        raise SketchError("Start a tangent arc on the end of a line or arc")
    n = _perp(d)
    se = _v(s, e)
    den = 2 * _dot(n, se)
    if abs(den) < 1e-9:
        raise SketchError("End point is straight ahead — that's a line, not an arc")
    t = _dot(se, se) / den
    c = _add(s, n, t)
    pc, pe = b.pt(c), b.pt(e, s1)
    aid = b.arc(pc, s0, pe) if t > 0 else b.arc(pc, pe, s0)
    b.con("tangent", aid, host["id"])


# ------------------------------------------------------------------ polygon / point
def _polygon(b, k, o):
    (sc, c), (_s1, p) = k
    n = int(o.get("sides", 6))
    if not 3 <= n <= 40:
        raise SketchError("A polygon has 3 to 40 sides")
    d = _v(c, p)
    r = _len(d)
    if r < 1e-6:
        raise SketchError("Polygon needs a size")
    a0 = math.atan2(d[1], d[0])
    if o.get("circumscribed"):                           # the click is the middle of a side
        r = r / math.cos(math.pi / n)
        a0 += math.pi / n
    pc = b.pt(c, sc)
    circ = b.circle(pc, r, construction=True)
    pts = [b.pt((c[0] + r * math.cos(a0 + 2 * math.pi * i / n), c[1] + r * math.sin(a0 + 2 * math.pi * i / n)))
           for i in range(n)]
    lines = [b.line(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for q in pts:
        b.con("point_on", q, circ)
    for i in range(1, n):
        b.con("equal", lines[0], lines[i])


def _point(b, k, o):
    (s0, p) = k[0]
    pid = b.pt(p, s0)
    b.g["curves"].append({"id": sk.new_id(b.g, "o"), "type": "point", "a": pid})


# ------------------------------------------------------------------ ellipse / conic / spline
def _ellipse_common(b, k):
    (sc, c), (s1, a), (_s2, m) = k[:3]
    u = _unit(_v(c, a))
    ry = abs(_dot(_v(c, m), _perp(u)))
    if ry < 1e-6:
        raise SketchError("Ellipse needs a minor axis")
    return b.pt(c, sc), b.pt(a, s1), b.pt(_add(c, _perp(u), ry))


def _ellipse(b, k, o):
    pc, pa, pb = _ellipse_common(b, k)
    b.g["curves"].append({"id": sk.new_id(b.g, "e"), "type": "ellipse", "c": pc, "a": pa, "b": pb})


def _ellipse_partial(b, k, o):
    pc, pa, pb = _ellipse_common(b, k)
    (s3, s), (s4, e) = k[3], k[4]
    cid = sk.new_id(b.g, "e")
    c = {"id": cid, "type": "ellipse", "c": pc, "a": pa, "b": pb, "s": b.pt(s, s3), "e": b.pt(e, s4),
         "construction": False}
    b.g["curves"].append(c)
    sk.project_partial_ellipses(b.g)


def _conic_common(b, k, rho):
    (s0, a), (s1, e), (_s2, t) = k
    if abs((e[0] - a[0]) * (t[1] - a[1]) - (e[1] - a[1]) * (t[0] - a[0])) < 1e-9:
        raise SketchError("The top point must be off the line between the ends")
    b.g["curves"].append({"id": sk.new_id(b.g, "k"), "type": "conic", "a": b.pt(a, s0), "b": b.pt(e, s1),
                          "t": b.pt(t), "rho": float(rho)})


def _parabola(b, k, o):
    _conic_common(b, k, 0.5)


def _conic(b, k, o):
    rho = float(o.get("rho", 0.5))
    if not 0.0 < rho < 1.0:
        raise SketchError("Rho is between 0 and 1 (below 0.5 elliptical, 0.5 parabolic, above hyperbolic)")
    _conic_common(b, k, rho)


def _spline(b, k, o):
    if len(k) < 2:
        raise SketchError("A spline needs at least 2 points")
    closed = len(k) > 3 and (k[-1][0] is not None and k[-1][0] == k[0][0] or
                             _len(_v(k[0][1], k[-1][1])) < 1e-9)
    clicks = k[:-1] if closed else k
    pts = []
    for sid, xy in clicks:
        pid = b.pt(xy, sid)
        if not pts or pts[-1] != pid:
            pts.append(pid)
    if len(pts) < 2:
        raise SketchError("A spline needs at least 2 points")
    b.g["curves"].append({"id": sk.new_id(b.g, "s"), "type": "spline", "pts": pts, "closed": bool(closed)})


# ------------------------------------------------------------------ dialogs (no clicks)
def equation_curve(geom, mode, x_expr, y_expr, t0, t1, anchor=sk.ORIGIN, samples=64):
    """Equation-driven curve: mode 'explicit' (y in x, x from t0 to t1) or 'parametric' (x(t), y(t))."""
    g = copy.deepcopy(geom)
    c = {"id": sk.new_id(g, "q"), "type": "equation", "c": anchor, "mode": mode, "x": x_expr, "y": y_expr,
         "t0": float(t0), "t1": float(t1), "n": int(samples)}
    sk.equation_points(g, c)                              # raises on a bad expression
    g["curves"].append(c)
    return g


def text_entity(geom, xy, text, height=10.0, font="Arial", angle=0.0, bold=False, italic=False, sid=None):
    g = copy.deepcopy(geom)
    pid = _default_newpt(g, xy, sid)
    g["curves"].append({"id": sk.new_id(g, "t"), "type": "text", "a": pid, "text": str(text),
                        "height": float(height), "font": font, "angle": float(angle), "bold": bool(bold),
                        "italic": bool(italic)})
    return g
