"""3D sketch: points and curves free in XYZ, with 3D relations and dimensions.

Geometry (Sketch3D feature `geometry` param):

    {
      "points": {"p1": [x, y, z], ...},                 # "O" = origin, always fixed
      "curves": [
        {"id": "l1", "type": "line",   "a": "p1", "b": "p2"},
        {"id": "s1", "type": "spline", "pts": ["p2", "p3", "p4"]},          # through points
        {"id": "a1", "type": "arc",    "a": "p4", "m": "p5", "b": "p6"},    # through 3 points
      ],
      "constraints": [{"type": "along_x", "e": ["l1"]}, {"type": "length", "e": ["l1"], "value": 600}, ...]
    }

Relations: coincident [pt, pt] · point_on [pt, line] · along_x / along_y / along_z [line] or [pt, pt]
           perpendicular [line, line] · equal [line, line] · midpoint [pt, line] · fix [pt]
Dimensions: length [line] · distance [pt, pt] or [pt, line] · dx / dy / dz [pt, pt] (magnitude)
            angle [line, line] (degrees)

Only constraints that reduce to scalar equations are used: SolveSpace's 3D
"parallel" aborts the process, so axis alignment is built from projected
distances instead, and line-to-line parallel isn't offered.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

ORIGIN = "O"
AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}

ARITY = {
    "coincident": 2, "point_on": 2, "along_x": (1, 2), "along_y": (1, 2), "along_z": (1, 2),
    "perpendicular": 2, "equal": 2, "midpoint": 2, "fix": 1,
    "length": 1, "distance": 2, "dx": 2, "dy": 2, "dz": 2, "angle": 2,
}
DIMENSIONS = {"length", "distance", "dx", "dy", "dz", "angle"}


class Sketch3DError(ValueError):
    pass


def empty_geometry() -> dict:
    return {"points": {ORIGIN: [0.0, 0.0, 0.0]}, "curves": [], "constraints": []}


def normalize(geom: dict | None) -> dict:
    g = copy.deepcopy(geom) if geom else empty_geometry()
    for k in ("points", "curves", "constraints"):
        g.setdefault(k, {} if k == "points" else [])
    g["points"][ORIGIN] = [0.0, 0.0, 0.0]
    for k, v in g["points"].items():
        g["points"][k] = [float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0]
    return g


def curve_points(c: dict) -> list[str]:
    if c["type"] == "spline":
        return list(c["pts"])
    return [c[k] for k in ("a", "m", "b") if k in c]


def curve(geom, cid):
    for c in geom["curves"]:
        if c["id"] == cid:
            return c
    raise Sketch3DError(f"No curve '{cid}'")


def kind(geom, eid):
    if eid in geom["points"]:
        return "point"
    return curve(geom, eid)["type"]


def new_id(geom, prefix):
    used = set(geom["points"]) | {c["id"] for c in geom["curves"]}
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


def merge_points(geom, keep, drop):
    if keep == drop:
        return geom
    if drop == ORIGIN:
        keep, drop = drop, keep
    for c in geom["curves"]:
        for k in ("a", "m", "b"):
            if c.get(k) == drop:
                c[k] = keep
        if c["type"] == "spline":
            c["pts"] = [keep if p == drop else p for p in c["pts"]]
    for k in geom["constraints"]:
        k["e"] = [keep if e == drop else e for e in k["e"]]
    geom["constraints"] = [k for k in geom["constraints"]
                           if not (k["type"] == "coincident" and k["e"][0] == k["e"][1])]
    geom["points"].pop(drop, None)
    geom["curves"] = [c for c in geom["curves"] if len(set(curve_points(c))) == len(curve_points(c))]
    return geom


def remove_entities(geom, ids):
    ids = set(ids) - {ORIGIN}
    geom["curves"] = [c for c in geom["curves"] if c["id"] not in ids and not (set(curve_points(c)) & ids)]
    geom["constraints"] = [k for k in geom["constraints"] if not (set(k["e"]) & ids)]
    used = {ORIGIN} | {p for c in geom["curves"] for p in curve_points(c)} | \
           {e for k in geom["constraints"] for e in k["e"] if e in geom["points"]}
    geom["points"] = {k: v for k, v in geom["points"].items() if k in used}
    cids = {c["id"] for c in geom["curves"]}
    geom["constraints"] = [k for k in geom["constraints"] if all(e in geom["points"] or e in cids for e in k["e"])]
    return geom


def validate(geom):
    for c in geom["curves"]:
        if c["type"] not in ("line", "spline", "arc"):
            raise Sketch3DError(f"Unknown curve type {c['type']}")
        for p in curve_points(c):
            if p not in geom["points"]:
                raise Sketch3DError(f"Curve {c['id']} references missing point {p}")
        if c["type"] == "spline" and len(c["pts"]) < 2:
            raise Sketch3DError(f"Spline {c['id']} needs ≥ 2 points")
    lines = {c["id"] for c in geom["curves"] if c["type"] == "line"}
    for i, k in enumerate(geom["constraints"]):
        t = k.get("type")
        if t not in ARITY:
            raise Sketch3DError(f"Constraint {i}: unknown type '{t}'")
        n = ARITY[t]
        if len(k.get("e", [])) not in (n if isinstance(n, tuple) else (n,)):
            raise Sketch3DError(f"Constraint {i} ({t}) needs {n} entities, got {k.get('e')}")
        if t in DIMENSIONS and "value" not in k:
            raise Sketch3DError(f"Constraint {i} ({t}) needs a value")
        for e in k["e"]:
            if e not in geom["points"] and e not in {c["id"] for c in geom["curves"]}:
                raise Sketch3DError(f"Constraint {i} ({t}) references unknown '{e}'")
        # relations that need straight lines
        need_lines = {"perpendicular": [0, 1], "equal": [0, 1], "angle": [0, 1], "length": [0],
                      "point_on": [1], "midpoint": [1]}
        for j in need_lines.get(t, []):
            if k["e"][j] not in lines:
                raise Sketch3DError(f"Constraint {i} ({t}): '{k['e'][j]}' must be a line")
        if t.startswith("along_") and len(k["e"]) == 1 and k["e"][0] not in lines:
            raise Sketch3DError(f"Constraint {i} ({t}): '{k['e'][0]}' must be a line")
        if t == "distance" and not (k["e"][0] in geom["points"] and (k["e"][1] in geom["points"] or k["e"][1] in lines)):
            raise Sketch3DError(f"Constraint {i} (distance) needs [point, point|line]")
        if t in ("dx", "dy", "dz", "coincident") and not all(e in geom["points"] for e in k["e"]):
            raise Sketch3DError(f"Constraint {i} ({t}) needs two points")


class _System:
    def __init__(self, geom, drag=None, skip=frozenset()):
        from python_solvespace import Constraint, Entity, SolverSystem
        self.C, self.E = Constraint, Entity
        self.g = geom
        s = self.s = SolverSystem()
        s.set_group(1)
        o = s.add_point_3d(0, 0, 0)
        self.axis = {a: s.add_line_3d(o, s.add_point_3d(*v)) for a, v in AXES.items()}
        s.set_group(2)
        drag = drag or {}
        self.pt = {}
        for pid, xyz in geom["points"].items():
            self.pt[pid] = s.add_point_3d(*(drag.get(pid, xyz)))
        self.ln = {}
        for c in geom["curves"]:
            if c["type"] == "line":
                self.ln[c["id"]] = s.add_line_3d(self.pt[c["a"]], self.pt[c["b"]])
        s.dragged(self.pt[ORIGIN])
        for pid in drag:
            s.dragged(self.pt[pid])
        self.map = []
        self.skip = skip
        for i, k in enumerate(geom["constraints"]):
            self._i, self._j = i, 0
            before = s.cons_len()
            try:
                self._add(k)
            except (TypeError, ValueError) as ex:
                raise Sketch3DError(f"Constraint #{i} {k['type']}: {ex}") from ex
            self.map.append(list(range(before + 1, s.cons_len() + 1)))

    def _gate(self) -> bool:
        """True if the next sub-constraint of the current relation should be added."""
        ok = (self._i, self._j) not in self.skip
        self._j += 1
        return ok

    def _proj(self, v, p, q, axis):
        # SolveSpace: value = (p − q) · axis
        if self._gate():
                self.s.add_constraint(self.C.PROJ_PT_DISTANCE, self.E.FREE_IN_3D, v, p, q, self.axis[axis], self.E.NONE)

    def _ends(self, e):
        if len(e) == 1:
            c = curve(self.g, e[0])
            return c["a"], c["b"]
        return e[0], e[1]

    def _add(self, k):
        s, t, e = self.s, k["type"], k["e"]
        v = float(k.get("value", 0.0))
        P = self.g["points"]
        if t == "coincident":
            if self._gate():
                s.coincident(self.pt[e[0]], self.pt[e[1]])
        elif t == "point_on":
            if self._gate():
                s.coincident(self.pt[e[0]], self.ln[e[1]])
        elif t.startswith("along_"):
            a, b = self._ends(e)
            for ax in AXES:
                if ax != t[-1]:
                    self._proj(0.0, self.pt[a], self.pt[b], ax)
        elif t == "perpendicular":
            if self._gate():
                s.add_constraint(self.C.PERPENDICULAR, self.E.FREE_IN_3D, 0, self.E.NONE, self.E.NONE,
                                 self.ln[e[0]], self.ln[e[1]])
        elif t == "angle":
            if self._gate():
                s.add_constraint(self.C.ANGLE, self.E.FREE_IN_3D, v, self.E.NONE, self.E.NONE,
                                 self.ln[e[0]], self.ln[e[1]])
        elif t == "equal":
            if self._gate():
                s.equal(self.ln[e[0]], self.ln[e[1]])
        elif t == "midpoint":
            if self._gate():
                s.midpoint(self.pt[e[0]], self.ln[e[1]])
        elif t == "fix":
            if self._gate():
                s.dragged(self.pt[e[0]])
        elif t == "length":
            c = curve(self.g, e[0])
            if self._gate():
                s.distance(self.pt[c["a"]], self.pt[c["b"]], v)
        elif t == "distance":
            if self._gate():
                s.distance(self.pt[e[0]], self.pt[e[1]] if e[1] in self.pt else self.ln[e[1]], v)
        elif t in ("dx", "dy", "dz"):
            ax = t[1]
            i = "xyz".index(ax)
            cur = P[e[0]][i] - P[e[1]][i]
            self._proj(math.copysign(abs(v), cur if abs(cur) > 1e-9 else 1.0), self.pt[e[0]], self.pt[e[1]], ax)

    def read(self):
        g = copy.deepcopy(self.g)
        for pid, ent in self.pt.items():
            g["points"][pid] = list(self.s.params(ent.params))
        return g


@dataclass
class Solve3DResult:
    geometry: dict
    ok: bool
    dof: int
    status: str
    failed: list[int] = field(default_factory=list)
    free_points: set[str] = field(default_factory=set)
    redundant: list[int] = field(default_factory=list)


def _vec(a, b):
    return [b[i] - a[i] for i in range(3)]


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def residual(geom, k, original=None) -> float:
    """How far constraint k is from being satisfied by geom (mm or degrees)."""
    P, t, e = geom["points"], k["type"], k["e"]
    v = float(k.get("value", 0.0))

    def ends(x):
        c = curve(geom, x)
        return P[c["a"]], P[c["b"]]

    if t == "coincident":
        return math.dist(P[e[0]], P[e[1]])
    if t in ("point_on", "midpoint"):
        a, b = ends(e[1])
        if t == "midpoint":
            return math.dist(P[e[0]], [(a[i] + b[i]) / 2 for i in range(3)])
        u, w = _vec(a, b), _vec(a, P[e[0]])
        cr = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]]
        return _norm(cr) / (_norm(u) or 1)
    if t.startswith("along_"):
        a, b = ends(e[0]) if len(e) == 1 else (P[e[0]], P[e[1]])
        d = _vec(a, b)
        i = "xyz".index(t[-1])
        return math.sqrt(sum(d[j] ** 2 for j in range(3) if j != i))
    if t in ("perpendicular", "angle"):
        u, w = _vec(*ends(e[0])), _vec(*ends(e[1]))
        cosv = sum(u[i] * w[i] for i in range(3)) / ((_norm(u) * _norm(w)) or 1)
        ang = math.degrees(math.acos(max(-1, min(1, cosv))))
        target = 90.0 if t == "perpendicular" else v
        return min(abs(ang - target), abs(180 - ang - target))
    if t == "equal":
        return abs(math.dist(*ends(e[0])) - math.dist(*ends(e[1])))
    if t == "fix":
        return math.dist(P[e[0]], original["points"][e[0]]) if original else 0.0
    if t == "length":
        return abs(math.dist(*ends(e[0])) - v)
    if t == "distance":
        if e[1] in P:
            return abs(math.dist(P[e[0]], P[e[1]]) - v)
        return abs(residual(geom, {"type": "point_on", "e": e}) - v)
    if t in ("dx", "dy", "dz"):
        i = "xyz".index(t[1])
        return abs(abs(P[e[0]][i] - P[e[1]][i]) - abs(v))
    return 0.0


def solve(geom, drag=None, analyze=False) -> Solve3DResult:
    """Solve; consistent redundant relations (common in closed 3D loops) are
    tolerated: they're left out of the solve and checked afterwards."""
    from python_solvespace import ResultFlag
    g = normalize(geom)
    validate(g)
    skip: set[tuple[int, int]] = set()
    sysm = _System(g, drag)
    flag = sysm.s.solve()
    if flag != ResultFlag.OKAY:
        # Find redundant/conflicting sub-equations by adding them one at a time.
        subs = [(i, j) for i, nums in enumerate(sysm.map) for j in range(len(nums))]
        for n, sub in enumerate(subs):
            trial = _System(g, drag, frozenset(skip | set(subs[n + 1:])))
            if trial.s.solve() != ResultFlag.OKAY:
                skip.add(sub)
        sysm = _System(g, drag, frozenset(skip))
        if sysm.s.solve() != ResultFlag.OKAY:
            return Solve3DResult(g, False, -1, "failed", sorted({i for i, _ in skip}))
    out = sysm.read()
    touched = sorted({i for i, _ in skip})
    bad = [i for i in touched if residual(out, g["constraints"][i], g) > 1e-5]
    if bad:
        return Solve3DResult(g, False, -1, "over-defined", bad)
    dof = sysm.s.dof()
    res = Solve3DResult(out, True, dof, "fully defined" if dof == 0 else "under-defined",
                        redundant=touched)
    if analyze and dof > 0:
        res.free_points = free_points(out, frozenset(skip))
    return res


def free_points(geom, skip=frozenset()) -> set[str]:
    from python_solvespace import ResultFlag
    free = set()
    for pid, (x, y, z) in geom["points"].items():
        if pid == ORIGIN:
            continue
        for d in ((0.731, 0, 0), (0, 0.613, 0), (0, 0, 0.577)):
            if _System(geom, {pid: (x + d[0], y + d[1], z + d[2])}, skip).s.solve() == ResultFlag.OKAY:
                free.add(pid)
                break
    return free


# --------------------------------------------------------------- geometry ---

def edges_for(geom, construction=False):
    """{curve id: build123d Edge}."""
    from build123d import Line, Spline, ThreePointArc
    P = geom["points"]
    out = {}
    for c in geom["curves"]:
        if c.get("construction") and not construction:
            continue
        try:
            if c["type"] == "line":
                a, b = P[c["a"]], P[c["b"]]
                if math.dist(a, b) > 1e-6:
                    out[c["id"]] = Line(tuple(a), tuple(b))
            elif c["type"] == "spline":
                out[c["id"]] = Spline(*[tuple(P[p]) for p in c["pts"]])
            elif c["type"] == "arc":
                out[c["id"]] = ThreePointArc(tuple(P[c["a"]]), tuple(P[c["m"]]), tuple(P[c["b"]]))
        except Exception:  # noqa: BLE001 — degenerate while editing
            continue
    return out


def _ends(c):
    pts = curve_points(c)
    return (pts[0], pts[-1]) if c["type"] != "arc" else (c["a"], c["b"])


def chains(geom, construction=False):
    """Split the curve graph into ordered chains between junctions/ends.
    Returns [(list of (curve id, reversed?), closed?)]."""
    curves = [c for c in geom["curves"] if construction or not c.get("construction")]
    adj: dict[str, list[str]] = {}
    for c in curves:
        a, b = _ends(c)
        adj.setdefault(a, []).append(c["id"])
        adj.setdefault(b, []).append(c["id"])
    by_id = {c["id"]: c for c in curves}
    used: set[str] = set()
    out = []

    def walk(start_pt, cid):
        seq = []
        pt = start_pt
        while cid and cid not in used:
            used.add(cid)
            a, b = _ends(by_id[cid])
            rev = a != pt
            seq.append((cid, rev))
            pt = a if rev else b
            nxt = [x for x in adj[pt] if x not in used]
            cid = nxt[0] if len(adj[pt]) == 2 and nxt else None
        return seq, pt

    for pt, cids in adj.items():                       # open chains start at ends/junctions
        if len(cids) != 2:
            for cid in cids:
                if cid not in used:
                    seq, _ = walk(pt, cid)
                    out.append((seq, False))
    for c in curves:                                   # remaining = closed loops
        if c["id"] not in used:
            a, _ = _ends(c)
            seq, end = walk(a, c["id"])
            out.append((seq, end == a))
    return out


def chain_wires(geom, construction=False):
    """build123d Wires, one per chain (ordered, oriented)."""
    from build123d import Wire
    edges = edges_for(geom, construction)
    wires = []
    for seq, _closed in chains(geom, construction):
        es = []
        for cid, rev in seq:
            if cid in edges:
                es.append(edges[cid].reversed() if rev else edges[cid])
        if es:
            try:
                wires.append(Wire(es))
            except Exception:  # noqa: BLE001
                wires.extend(Wire([e]) for e in es)
    return wires
