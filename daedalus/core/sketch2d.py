"""Constrained 2D sketch: data model, SolveSpace-based solver, profile builder.

Geometry (stored in the Sketch feature's `geometry` param, plain JSON):

    {
      "points": {"p1": [x, y], ...},                 # plane-local mm. "O" = origin (always fixed)
      "curves": [
        {"id": "l1", "type": "line",   "a": "p1", "b": "p2", "construction": false},
        {"id": "c1", "type": "circle", "c": "p3", "r": 10.0},
        {"id": "a1", "type": "arc",    "c": "p4", "a": "p5", "b": "p6"},     # CCW a → b
        {"id": "e1", "type": "ellipse", "c": "p7", "a": "p8", "b": "p9"},   # a = major-axis end, b = minor end
        {"id": "e2", "type": "ellipse", ..., "s": "p10", "e": "p11"}        # partial ellipse, CCW s → e
        {"id": "k1", "type": "conic", "a": "p1", "b": "p2", "t": "p3", "rho": 0.5}   # rho .5 = parabola
        {"id": "s1", "type": "spline", "pts": ["p1", "p2", "p3"], "closed": false}  # through points
        {"id": "q1", "type": "equation", "c": "p1", "mode": "parametric", "x": "50*cos(t)", "y": "50*sin(t)",
         "t0": 0, "t1": 6.283}                                              # explicit: "y" in x, t = x
        {"id": "t1", "type": "text", "a": "p1", "text": "ABC", "height": 10, "font": "Arial", "angle": 0}
        {"id": "o1", "type": "point", "a": "p1"}                            # a free-standing sketch point
      ],
      Lines may carry "infinite": true (construction lines drawn across the view).
      "constraints": [
        {"type": "horizontal", "e": ["l1"]},
        {"type": "distance", "e": ["p1", "p2"], "value": 50},
        ...
      ]
    }

Constraint types (entities in "e"):
  coincident [pt, pt] · point_on [pt, line|circle|arc] · horizontal [line] or [pt, pt]
  vertical [line] or [pt, pt] · parallel [line, line] · perpendicular [line, line]
  equal [line, line] or [circle|arc, circle|arc] · tangent [arc, line|arc] (sharing an endpoint)
  midpoint [pt, line] · symmetric [pt, pt, line|pt] · fix [pt]
  Dimensions (need "value"): distance [pt, pt] or [pt, line] · hdistance [pt, pt] · vdistance [pt, pt]
  length [line] · radius [circle|arc] · diameter [circle|arc] · angle [line, line] (degrees)
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

ORIGIN = "O"

CONSTRAINT_ARITY = {
    "coincident": 2, "point_on": 2, "horizontal": (1, 2), "vertical": (1, 2), "parallel": 2,
    "perpendicular": 2, "equal": 2, "tangent": 2, "midpoint": 2, "symmetric": 3, "fix": 1,
    "distance": 2, "hdistance": 2, "vdistance": 2, "length": 1, "radius": 1, "diameter": 1,
    "angle": 2,
}
DIMENSIONS = {"distance", "hdistance", "vdistance", "length", "radius", "diameter", "angle"}


class SketchError(ValueError):
    pass


def empty_geometry() -> dict:
    return {"points": {ORIGIN: [0.0, 0.0]}, "curves": [], "constraints": []}


def normalize(geom: dict | None) -> dict:
    g = copy.deepcopy(geom) if geom else empty_geometry()
    g.setdefault("points", {})
    g.setdefault("curves", [])
    g.setdefault("constraints", [])
    g["points"][ORIGIN] = [0.0, 0.0]
    for k, v in g["points"].items():
        g["points"][k] = [float(v[0]), float(v[1])]
    return g


def new_id(geom: dict, prefix: str) -> str:
    used = set(geom["points"]) | {c["id"] for c in geom["curves"]}
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


def curve(geom: dict, cid: str) -> dict:
    for c in geom["curves"]:
        if c["id"] == cid:
            return c
    raise SketchError(f"No curve '{cid}'")


def kind(geom: dict, eid: str) -> str:
    if eid in geom["points"]:
        return "point"
    return curve(geom, eid)["type"]


POINT_KEYS = ("a", "b", "c", "t", "s", "e")
CURVE_TYPES = ("line", "circle", "arc", "ellipse", "conic", "spline", "equation", "text", "point")
RIGID = ("equation", "text")                  # shape fixed by parameters; only the anchor point moves
NOT_PROFILE = ("point",)


def curve_points(c: dict) -> list[str]:
    out = [c[k] for k in POINT_KEYS if k in c]
    out += list(c.get("pts") or [])
    return out


def replace_point(c: dict, old: str, new: str):
    for k in POINT_KEYS:
        if c.get(k) == old:
            c[k] = new
    if "pts" in c:
        c["pts"] = [new if p == old else p for p in c["pts"]]


def remove_entities(geom: dict, ids: set[str]) -> dict:
    """Delete curves/points and every constraint touching them; drop orphan points."""
    g = geom
    ids = set(ids) - {ORIGIN}
    g["curves"] = [c for c in g["curves"] if c["id"] not in ids and not (set(curve_points(c)) & ids)]
    g["constraints"] = [k for k in g["constraints"] if not (set(k["e"]) & ids)]
    used = {ORIGIN} | {p for c in g["curves"] for p in curve_points(c)} | \
           {e for k in g["constraints"] for e in k["e"] if e in g["points"]}
    g["points"] = {k: v for k, v in g["points"].items() if k in used}
    g["constraints"] = [k for k in g["constraints"] if all(
        e in g["points"] or any(c["id"] == e for c in g["curves"]) for e in k["e"])]
    return g


def merge_points(geom: dict, keep: str, drop: str) -> dict:
    """Make two points one point (the implicit-coincidence way)."""
    if keep == drop:
        return geom
    if drop == ORIGIN:
        keep, drop = drop, keep
    for c in geom["curves"]:
        replace_point(c, drop, keep)
    for k in geom["constraints"]:
        k["e"] = [keep if e == drop else e for e in k["e"]]
    geom["constraints"] = [k for k in geom["constraints"]
                           if not (k["type"] == "coincident" and k["e"][0] == k["e"][1])]
    geom["points"].pop(drop, None)
    # zero-length lines collapse
    geom["curves"] = [c for c in geom["curves"] if not (c["type"] == "line" and c["a"] == c["b"])]
    return geom


# ------------------------------------------------------------------ solve ---

@dataclass
class SolveResult:
    geometry: dict
    ok: bool
    dof: int
    status: str                                   # "fully defined" | "under-defined" | "over-defined" | "failed"
    failed: list[int] = field(default_factory=list)   # indices into geometry["constraints"]
    free_points: set[str] = field(default_factory=set)


def validate(geom: dict):
    pts = geom["points"]
    for c in geom["curves"]:
        for p in curve_points(c):
            if p not in pts:
                raise SketchError(f"Curve {c['id']} references missing point {p}")
        if c["type"] == "circle" and float(c.get("r", 0)) <= 0:
            raise SketchError(f"Circle {c['id']} needs r > 0")
        if c["type"] not in CURVE_TYPES:
            raise SketchError(f"Unknown curve type {c['type']}")
        if c["type"] == "spline" and len(c.get("pts") or []) < 2:
            raise SketchError(f"Spline {c['id']} needs at least 2 points")
    for i, k in enumerate(geom["constraints"]):
        t = k.get("type")
        if t not in CONSTRAINT_ARITY:
            raise SketchError(f"Constraint {i}: unknown type '{t}'")
        n = CONSTRAINT_ARITY[t]
        ok = len(k.get("e", [])) in (n if isinstance(n, tuple) else (n,))
        if not ok:
            raise SketchError(f"Constraint {i} ({t}) needs {n} entities, got {k.get('e')}")
        if t in DIMENSIONS and "value" not in k:
            raise SketchError(f"Constraint {i} ({t}) needs a value")


class _System:
    """Builds a SolveSpace system from geometry."""

    def __init__(self, geom: dict, drag: dict[str, tuple[float, float]] | None = None,
                 lock_free: set[str] | None = None):
        from python_solvespace import SolverSystem
        self.g = geom
        self.s = SolverSystem()
        self.s.set_group(1)                       # reference geometry: not solved for
        self.wp = self.s.create_2d_base()
        self.nm = self.s.add_normal_3d(1, 0, 0, 0)
        self.s.set_group(2)
        self.pt, self.cv, self.rad = {}, {}, {}
        self.map: list[list[int]] = []            # our constraint index -> slvs constraint numbers
        drag = drag or {}
        for pid, (x, y) in geom["points"].items():
            if pid in drag:
                x, y = drag[pid]
            self.pt[pid] = self.s.add_point_2d(x, y, self.wp)
        for c in geom["curves"]:
            if c["type"] == "line":
                self.cv[c["id"]] = self.s.add_line_2d(self.pt[c["a"]], self.pt[c["b"]], self.wp)
            elif c["type"] == "circle":
                r = self.s.add_distance(float(c["r"]), self.wp)
                self.rad[c["id"]] = r
                self.cv[c["id"]] = self.s.add_circle(self.nm, self.pt[c["c"]], r, self.wp)
            elif c["type"] == "arc":
                self.cv[c["id"]] = self.s.add_arc(self.nm, self.pt[c["c"]], self.pt[c["a"]], self.pt[c["b"]], self.wp)
            elif c["type"] == "ellipse":             # major and minor half-axes stay square to each other
                major = self.s.add_line_2d(self.pt[c["c"]], self.pt[c["a"]], self.wp)
                minor = self.s.add_line_2d(self.pt[c["c"]], self.pt[c["b"]], self.wp)
                self.s.perpendicular(major, minor, self.wp)
            # conic / spline / equation / text / point: their points are ordinary points
        self._lock(ORIGIN)
        for pid in drag:
            self._lock(pid)
        for pid in lock_free or ():
            self._lock(pid)
        for i, k in enumerate(geom["constraints"]):
            before = self.s.cons_len()
            try:
                self._add(k)
            except (TypeError, ValueError) as ex:
                raise SketchError(f"Constraint #{i} {k['type']} {k['e']}: entities not supported ({ex})") from ex
            self.map.append(list(range(before + 1, self.s.cons_len() + 1)))

    def _lock(self, pid):
        self.s.dragged(self.pt[pid], self.wp)

    def _e(self, eid):
        if eid in self.pt:
            return self.pt[eid]
        if eid in self.cv:
            return self.cv[eid]
        raise SketchError(f"Unknown entity '{eid}'")

    def _helper_line(self, a, b):
        return self.s.add_line_2d(self._e(a), self._e(b), self.wp)

    def _add(self, k):
        s, wp, t, e = self.s, self.wp, k["type"], k["e"]
        v = float(k.get("value", 0))
        g = self.g
        if t == "coincident":
            s.coincident(self._e(e[0]), self._e(e[1]), wp)
        elif t == "point_on":
            s.coincident(self._e(e[0]), self._e(e[1]), wp)
        elif t in ("horizontal", "vertical"):
            line = self._e(e[0]) if len(e) == 1 else self._helper_line(e[0], e[1])
            (s.horizontal if t == "horizontal" else s.vertical)(line, wp)
        elif t == "parallel":
            s.parallel(self._e(e[0]), self._e(e[1]), wp)
        elif t == "perpendicular":
            s.perpendicular(self._e(e[0]), self._e(e[1]), wp)
        elif t == "equal":
            s.equal(self._e(e[0]), self._e(e[1]), wp)
        elif t == "tangent":
            a, b = e
            if kind(g, a) != "arc":
                a, b = b, a
            if kind(g, a) != "arc":
                raise SketchError("Tangent needs an arc and a line/arc that share an endpoint")
            from python_solvespace import Constraint as _C, Entity as _E
            ca, cb = curve(g, a), curve(g, b)
            other = 1 if ca["b"] in curve_points(cb) and ca["a"] not in curve_points(cb) else 0
            if cb["type"] == "line":
                s.add_constraint(_C.ARC_LINE_TANGENT, wp, 0.0, _E.NONE, _E.NONE, self._e(a), self._e(b),
                                 _E.NONE, _E.NONE, other, 0)
            else:
                other2 = 1 if cb["b"] in curve_points(ca) and cb["a"] not in curve_points(ca) else 0
                s.add_constraint(_C.CURVE_CURVE_TANGENT, wp, 0.0, _E.NONE, _E.NONE, self._e(a), self._e(b),
                                 _E.NONE, _E.NONE, other, other2)
        elif t == "midpoint":
            s.midpoint(self._e(e[0]), self._e(e[1]), wp)
        elif t == "symmetric":
            if e[2] in self.pt:      # about a point = that point is the midpoint
                s.midpoint(self._e(e[2]), self._helper_line(e[0], e[1]), wp)
            else:
                s.symmetric(self._e(e[0]), self._e(e[1]), self._e(e[2]), wp)
        elif t == "fix":
            s.dragged(self._e(e[0]), wp)
        elif t == "distance":
            s.distance(self._e(e[0]), self._e(e[1]), v, wp)
        elif t == "length":
            c = curve(g, e[0])
            s.distance(self.pt[c["a"]], self.pt[c["b"]], v, wp)
        elif t in ("hdistance", "vdistance"):
            # helper point h shares y (or x) with A and x (or y) with B; |A-h| = v
            pa, pb = g["points"][e[0]], g["points"][e[1]]
            hx, hy = (pb[0], pa[1]) if t == "hdistance" else (pa[0], pb[1])
            h = s.add_point_2d(hx, hy, wp)
            ln1 = s.add_line_2d(self._e(e[0]), h, wp)
            ln2 = s.add_line_2d(h, self._e(e[1]), wp)
            if t == "hdistance":
                s.horizontal(ln1, wp)
                s.vertical(ln2, wp)
            else:
                s.vertical(ln1, wp)
                s.horizontal(ln2, wp)
            s.distance(self._e(e[0]), h, v, wp)
        elif t in ("radius", "diameter"):
            s.diameter(self._e(e[0]), v * 2 if t == "radius" else v)
        elif t == "angle":
            s.angle(self._e(e[0]), self._e(e[1]), v, wp)

    def read(self) -> dict:
        g = copy.deepcopy(self.g)
        for pid, ent in self.pt.items():
            x, y = self.s.params(ent.params)
            g["points"][pid] = [x, y]
        for cid, r in self.rad.items():
            curve(g, cid)["r"] = abs(self.s.params(r.params)[0])
        project_partial_ellipses(g)
        return g


def solve(geom: dict, drag: dict[str, tuple[float, float]] | None = None,
          analyze: bool = False) -> SolveResult:
    """Solve constraints. `drag` pins points at new positions (interactive drag).
    `analyze` additionally finds which points are still free (per-point DOF)."""
    from python_solvespace import ResultFlag
    g = normalize(geom)
    validate(g)
    sysm = _System(g, drag)
    flag = sysm.s.solve()
    if flag != ResultFlag.OKAY:
        fails = sysm.s.failures()
        bad = sorted({i for i, nums in enumerate(sysm.map) if set(nums) & set(fails)})
        status = "over-defined" if flag == ResultFlag.INCONSISTENT else "failed"
        return SolveResult(g, False, -1, status, bad)
    out = sysm.read()
    dof = sysm.s.dof()
    res = SolveResult(out, True, dof, "fully defined" if dof == 0 else "under-defined")
    if analyze and dof > 0:
        if len(out["points"]) > ANALYZE_LIMIT:      # one solve per point: too slow for big imports
            res.free_points = {p for p in out["points"] if p != ORIGIN}
        else:
            res.free_points = free_points(out)
    return res


ANALYZE_LIMIT = 150


def free_points(geom: dict) -> set[str]:
    """Points that can still move: nudge each one; if the system accepts it, it's free."""
    from python_solvespace import ResultFlag
    free = set()
    for pid, (x, y) in geom["points"].items():
        if pid == ORIGIN:
            continue
        for dx, dy in ((0.731, 0.0), (0.0, 0.613)):
            sysm = _System(geom, {pid: (x + dx, y + dy)})
            if sysm.s.solve() == ResultFlag.OKAY:
                free.add(pid)
                break
    return free


# ------------------------------------------------------------ ellipses etc ---

def ellipse_axes(geom, c):
    """(cx, cy, rx, ry, ux, uy): centre, half-axes, unit major direction."""
    P = geom["points"]
    cx, cy = P[c["c"]]
    ax, ay = P[c["a"]]
    bx, by = P[c["b"]]
    rx = math.hypot(ax - cx, ay - cy) or 1e-9
    ux, uy = (ax - cx) / rx, (ay - cy) / rx
    ry = abs(-(bx - cx) * uy + (by - cy) * ux) or abs(math.hypot(bx - cx, by - cy)) or 1e-9
    return cx, cy, rx, ry, ux, uy


def ellipse_param(geom, c, xy):
    """Eccentric angle (radians) of a point (projected onto the ellipse)."""
    cx, cy, rx, ry, ux, uy = ellipse_axes(geom, c)
    dx, dy = xy[0] - cx, xy[1] - cy
    lx, ly = dx * ux + dy * uy, -dx * uy + dy * ux
    return math.atan2(ly / ry, lx / rx)


def ellipse_point(geom, c, t):
    cx, cy, rx, ry, ux, uy = ellipse_axes(geom, c)
    lx, ly = rx * math.cos(t), ry * math.sin(t)
    return cx + lx * ux - ly * uy, cy + lx * uy + ly * ux


def project_partial_ellipses(geom):
    """Keep a partial ellipse's end points on its ellipse (the solver has no ellipse entity)."""
    for c in geom["curves"]:
        if c["type"] == "ellipse" and "s" in c and "e" in c:
            for k in ("s", "e"):
                geom["points"][c[k]] = list(ellipse_point(geom, c, ellipse_param(geom, c, geom["points"][c[k]])))


_SAFE = {k: getattr(math, k) for k in ("sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh",
                                       "tanh", "exp", "log", "log10", "sqrt", "pi", "e", "radians", "degrees",
                                       "floor", "ceil", "hypot")}
_SAFE["abs"] = abs


def eval_expr(expr: str, **vars_) -> float:
    """A math expression in t / x (radians, ^ = power). Only math functions are allowed."""
    src = str(expr).replace("^", "**")
    code = compile(src, "<equation>", "eval")
    for name in code.co_names:
        if name not in _SAFE and name not in vars_:
            raise SketchError(f"Unknown name '{name}' in equation (use t, x, sin, cos, sqrt, pi, …)")
    return float(eval(code, {"__builtins__": {}}, {**_SAFE, **vars_}))  # noqa: S307 — names checked above


def equation_points(geom, c, n=None):
    """Sampled points of an equation-driven curve, offset by its anchor point."""
    ox, oy = geom["points"][c["c"]]
    t0, t1 = float(c.get("t0", 0)), float(c.get("t1", 1))
    n = int(n or c.get("n", 64))
    out = []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        if c.get("mode", "parametric") == "explicit":
            x, y = t, eval_expr(c["y"], x=t, t=t)
        else:
            x, y = eval_expr(c["x"], t=t), eval_expr(c["y"], t=t)
        out.append((ox + x, oy + y))
    return out


def conic_points(geom, c, n=48):
    """Rational quadratic Bézier a → b with control t and weight rho / (1 - rho)."""
    P = geom["points"]
    a, t, b = P[c["a"]], P[c["t"]], P[c["b"]]
    rho = min(max(float(c.get("rho", 0.5)), 0.01), 0.99)
    w = rho / (1 - rho)
    out = []
    for i in range(n + 1):
        u = i / n
        b0, b1, b2 = (1 - u) ** 2, 2 * u * (1 - u) * w, u * u
        d = b0 + b1 + b2
        out.append(((b0 * a[0] + b1 * t[0] + b2 * b[0]) / d, (b0 * a[1] + b1 * t[1] + b2 * b[1]) / d))
    return out


def spline_points(geom, c, n=24):
    """Display samples of a through-point spline (Catmull-Rom; the solid uses an exact B-spline)."""
    P = geom["points"]
    pts = [tuple(P[p]) for p in c["pts"]]
    closed = bool(c.get("closed"))
    if len(pts) < 3:
        return pts + ([pts[0]] if closed and pts else [])
    seq = pts + ([pts[0]] if closed else [])
    out = []
    m = len(seq)
    for i in range(m - 1):
        p0 = seq[i - 1] if i > 0 else (seq[-2] if closed else seq[0])
        p1, p2 = seq[i], seq[i + 1]
        p3 = seq[i + 2] if i + 2 < m else (seq[1] if closed else seq[-1])
        for k in range(n):
            u = k / n
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * u + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * u * u
                                    + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * u ** 3) for j in (0, 1)))
    out.append(seq[-1])
    return out


def ellipse_samples(geom, c, n=96):
    if "s" in c and "e" in c:
        t0 = ellipse_param(geom, c, geom["points"][c["s"]])
        t1 = ellipse_param(geom, c, geom["points"][c["e"]])
        sweep = (t1 - t0) % (2 * math.pi) or 2 * math.pi
        return [ellipse_point(geom, c, t0 + sweep * i / n) for i in range(n + 1)]
    return [ellipse_point(geom, c, 2 * math.pi * i / n) for i in range(n + 1)]


def text_value(c, props=None) -> str:
    """Sketch text with {Property} links filled from document properties."""
    import re
    txt = str(c.get("text", ""))
    props = props or {}
    return re.sub(r"\{([^{}]+)\}", lambda m: str(props.get(m.group(1), m.group(0))), txt)


def samples(geom, c):
    """Polyline (plane-local) for drawing / picking any curve type; [] when it has no line to draw."""
    t = c["type"]
    P = geom["points"]
    try:
        if t == "line":
            return [tuple(P[c["a"]]), tuple(P[c["b"]])]
        if t == "circle":
            cx, cy = P[c["c"]]
            r = float(c["r"])
            return [(cx + r * math.cos(2 * math.pi * i / 72), cy + r * math.sin(2 * math.pi * i / 72)) for i in range(73)]
        if t == "arc":
            cx, cy = P[c["c"]]
            ax, ay = P[c["a"]]
            bx, by = P[c["b"]]
            r = math.hypot(ax - cx, ay - cy)
            a0, a1 = math.atan2(ay - cy, ax - cx), math.atan2(by - cy, bx - cx)
            sw = (a1 - a0) % (2 * math.pi) or 2 * math.pi
            return [(cx + r * math.cos(a0 + sw * i / 48), cy + r * math.sin(a0 + sw * i / 48)) for i in range(49)]
        if t == "ellipse":
            return ellipse_samples(geom, c)
        if t == "conic":
            return conic_points(geom, c)
        if t == "spline":
            return spline_points(geom, c)
        if t == "equation":
            return equation_points(geom, c)
    except (KeyError, SketchError, ValueError, ZeroDivisionError, OverflowError):
        return []
    return []


def _edge_of(geom, c):
    """build123d edge for the new curve types (None for text / point)."""
    from build123d import Edge, Plane
    P = geom["points"]
    t = c["type"]
    if t == "ellipse":
        cx, cy, rx, ry, ux, uy = ellipse_axes(geom, c)
        if ry > rx:                                     # build123d wants x_radius ≥ y_radius
            pl = Plane(origin=(cx, cy, 0), x_dir=(-uy, ux, 0), z_dir=(0, 0, 1))
            rx, ry, shift = ry, rx, -90.0
        else:
            pl = Plane(origin=(cx, cy, 0), x_dir=(ux, uy, 0), z_dir=(0, 0, 1))
            shift = 0.0
        if "s" in c and "e" in c:
            t0 = math.degrees(ellipse_param(geom, c, P[c["s"]])) + shift
            t1 = math.degrees(ellipse_param(geom, c, P[c["e"]])) + shift
            if (t1 - t0) % 360 < 1e-6:
                t1 = t0 + 360
            return Edge.make_ellipse(rx, ry, pl, t0 % 360, t1 % 360 if (t1 % 360) > (t0 % 360) else t1 % 360 + 360)
        return Edge.make_ellipse(rx, ry, pl, 360, 360)
    if t == "conic":
        rho = min(max(float(c.get("rho", 0.5)), 0.01), 0.99)
        w = rho / (1 - rho)
        a, tt, b = P[c["a"]], P[c["t"]], P[c["b"]]
        return Edge.make_bezier((a[0], a[1], 0), (tt[0], tt[1], 0), (b[0], b[1], 0), weights=[1.0, w, 1.0])
    if t == "spline":
        pts = [(P[p][0], P[p][1], 0) for p in c["pts"]]
        if len(pts) == 2:
            from build123d import Line
            return Line(pts[0], pts[1])
        return Edge.make_spline(pts, periodic=bool(c.get("closed")))
    if t == "equation":
        pts = equation_points(geom, c)
        closed = math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6
        if closed:
            pts = pts[:-1]
        return Edge.make_spline([(x, y, 0) for x, y in pts], periodic=closed)
    return None


def is_closed_curve(geom, c) -> bool:
    t = c["type"]
    if t == "ellipse":
        return not ("s" in c and "e" in c)
    if t == "spline":
        return bool(c.get("closed"))
    if t == "equation":
        pts = equation_points(geom, c, n=8)
        return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6
    return False


def text_faces(geom, c, props=None):
    """Sketch text as faces (plane-local), anchored at its point, rotated by its angle."""
    from build123d import Location, Text
    txt = text_value(c, props)
    if not txt.strip():
        return []
    ax, ay = geom["points"][c["a"]]
    kw = {"font": c.get("font") or "Arial"}
    style = {(False, False): "REGULAR", (True, False): "BOLD", (False, True): "ITALIC", (True, True): "BOLDITALIC"}
    try:
        from build123d import FontStyle
        kw["font_style"] = getattr(FontStyle, style[(bool(c.get("bold")), bool(c.get("italic")))])
    except Exception:  # noqa: BLE001
        pass
    from build123d import Align
    t = Text(txt, float(c.get("height", 10.0)), align=(Align.MIN, Align.MIN), **kw)
    t = Location((ax, ay, 0), (0, 0, 1), float(c.get("angle", 0.0))) * t
    return list(t.faces())


# --------------------------------------------------------------- profile ---

def arc_mid(geom: dict, c: dict) -> tuple[float, float]:
    cx, cy = geom["points"][c["c"]]
    ax, ay = geom["points"][c["a"]]
    bx, by = geom["points"][c["b"]]
    r = math.hypot(ax - cx, ay - cy)
    a0 = math.atan2(ay - cy, ax - cx)
    a1 = math.atan2(by - cy, bx - cx)
    sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    m = a0 + sweep / 2
    return cx + r * math.cos(m), cy + r * math.sin(m)


def curve_edges(geom: dict, construction: bool = False):
    """build123d edges for drawing (plane-local, z=0)."""
    from build123d import Edge, Line, Pos, ThreePointArc
    pts = geom["points"]
    out = []
    for c in geom["curves"]:
        if c.get("construction") and not construction:
            continue
        try:
            if c["type"] == "line":
                a, b = pts[c["a"]], pts[c["b"]]
                if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6:
                    out.append(Line((a[0], a[1]), (b[0], b[1])))
            elif c["type"] == "arc":
                out.append(ThreePointArc(tuple(pts[c["a"]]), arc_mid(geom, c), tuple(pts[c["b"]])))
            elif c["type"] == "circle":
                cx, cy = pts[c["c"]]
                out.append(Pos(cx, cy) * Edge.make_circle(float(c["r"])))
            elif c["type"] == "text":
                for f in text_faces(geom, c):
                    out += list(f.edges())
            elif c["type"] == "line" and c.get("infinite"):
                continue
            else:
                e = _edge_of(geom, c)
                if e is not None:
                    out.append(e)
        except Exception:  # noqa: BLE001 — degenerate curve while editing
            continue
    return out


def profile_faces(geom: dict, props: dict | None = None):
    """Closed loops of non-construction curves → list of build123d Faces in
    plane-local XY (z=0), with even-odd nesting (inner loops become holes). Sketch text adds its letters;
    props fills {Property} links in text."""
    from build123d import Edge, Face, Line, Pos, ThreePointArc, Wire

    pts = geom["points"]
    edges = []
    faces = []
    for c in geom["curves"]:
        if c.get("construction"):
            continue
        if c["type"] == "line":
            a, b = pts[c["a"]], pts[c["b"]]
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6:
                edges.append(Line((a[0], a[1]), (b[0], b[1])))
        elif c["type"] == "arc":
            a, b = pts[c["a"]], pts[c["b"]]
            m = arc_mid(geom, c)
            edges.append(ThreePointArc((a[0], a[1]), m, (b[0], b[1])))
        elif c["type"] == "circle":
            cx, cy = pts[c["c"]]
            faces.append(Pos(cx, cy) * Face(Wire([Edge.make_circle(float(c["r"]))])))
        elif c["type"] == "text":
            text_faces_list = text_faces(geom, c, props)
            faces.extend(text_faces_list)
        elif c["type"] in ("ellipse", "conic", "spline", "equation"):
            try:
                e = _edge_of(geom, c)
            except Exception as ex:  # noqa: BLE001
                raise SketchError(f"{c['type'].title()} {c['id']} can't be built ({ex})") from ex
            if e is None:
                continue
            if is_closed_curve(geom, c):
                faces.append(Face(Wire([e])))
            else:
                edges.append(e)
    if edges:
        for w in Wire.combine(edges):
            if not w.is_closed:
                ends = w.vertices()
                where = f"near ({ends[0].X:.1f}, {ends[0].Y:.1f})" if ends else ""
                raise SketchError(f"Open profile {where} — close the loop or mark lines as construction")
            faces.append(Face(w))
    if not faces:
        return []
    faces.sort(key=lambda f: f.area, reverse=True)
    region = None
    for f in faces:
        if region is not None and region.is_inside(f.center()):
            region = region - f
        else:
            region = f if region is None else region + f
    return list(region.faces()) if region is not None else []
