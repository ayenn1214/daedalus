"""Sketch editing operations on sketch2d geometry: trim, extend, offset, fillet, mirror, patterns.
Pure functions: take a geometry dict, return a new one (the caller solves/commits it)."""
from __future__ import annotations

import copy
import math

import numpy as np

from . import sketch2d as sk
from .sketch2d import SketchError

TOL = 1e-7


def _P(g, pid):
    return np.array(g["points"][pid], float)


def _new_pt(g, xy):
    pid = sk.new_id(g, "p")
    g["points"][pid] = [float(xy[0]), float(xy[1])]
    return pid


def _drop_constraints(g, ids):
    g["constraints"] = [c for c in g["constraints"] if not (set(c["e"]) & set(ids))]


def _ang(v):
    return math.atan2(v[1], v[0])


def _arc_span(g, c):
    C = _P(g, c["c"])
    a0 = _ang(_P(g, c["a"]) - C)
    a1 = _ang(_P(g, c["b"]) - C)
    sw = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    return C, float(np.linalg.norm(_P(g, c["a"]) - C)), a0, sw


# ------------------------------------------------------------ intersections

def _param(g, c, xy):
    """Position of a point along a curve: line t∈[0,1]; arc/circle angle from start (0…sweep)."""
    xy = np.asarray(xy, float)
    if c["type"] == "line":
        A, B = _P(g, c["a"]), _P(g, c["b"])
        d = B - A
        return float(np.dot(xy - A, d) / max(np.dot(d, d), 1e-30))
    if c["type"] == "circle":
        return _ang(xy - _P(g, c["c"])) % (2 * math.pi)
    C, r, a0, sw = _arc_span(g, c)
    return (_ang(xy - C) - a0) % (2 * math.pi)


def _on(g, c, xy, tol=1e-6):
    t = _param(g, c, xy)
    if c["type"] == "line":
        return -tol <= t <= 1 + tol
    if c["type"] == "circle":
        return True
    _, _, _, sw = _arc_span(g, c)
    return t <= sw + tol or t >= 2 * math.pi - tol


def _geo(g, c):
    if c["type"] == "line":
        return ("L", _P(g, c["a"]), _P(g, c["b"]))
    C = _P(g, c["c"])
    r = c["r"] if c["type"] == "circle" else float(np.linalg.norm(_P(g, c["a"]) - C))
    return ("C", C, r)


def _xsect(g1, g2, infinite_first=False):
    """Intersection points of two curve geometries (lines treated as infinite here)."""
    out = []
    if g1[0] == "L" and g2[0] == "L":
        p, r = g1[1], g1[2] - g1[1]
        q, s = g2[1], g2[2] - g2[1]
        den = r[0] * s[1] - r[1] * s[0]
        if abs(den) < 1e-14:
            return []
        t = ((q - p)[0] * s[1] - (q - p)[1] * s[0]) / den
        out.append(p + t * r)
    elif g1[0] == "C" and g2[0] == "C":
        c1, r1, c2, r2 = g1[1], g1[2], g2[1], g2[2]
        d = float(np.linalg.norm(c2 - c1))
        if d < 1e-12 or d > r1 + r2 + 1e-9 or d < abs(r1 - r2) - 1e-9:
            return []
        a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
        h = math.sqrt(max(r1 * r1 - a * a, 0.0))
        m = c1 + a * (c2 - c1) / d
        perp = np.array([-(c2 - c1)[1], (c2 - c1)[0]]) / d
        out += [m + h * perp, m - h * perp] if h > 1e-12 else [m]
    else:
        L, Cg = (g1, g2) if g1[0] == "L" else (g2, g1)
        p, d = L[1], L[2] - L[1]
        f = p - Cg[1]
        A = d @ d
        B = 2 * f @ d
        Cc = f @ f - Cg[2] ** 2
        disc = B * B - 4 * A * Cc
        if disc < 0:
            return []
        sq = math.sqrt(disc)
        for t in ((-B - sq) / (2 * A), (-B + sq) / (2 * A)):
            out.append(p + t * d)
    return out


def intersections(g, cid, others=None):
    c = sk.curve(g, cid)
    res = []
    for o in g["curves"]:
        if o["id"] == cid or (others is not None and o["id"] not in others):
            continue
        if o["type"] not in ("line", "circle", "arc") or o.get("infinite"):
            continue                                  # (free-form curves don't bound a trim)
        for x in _xsect(_geo(g, c), _geo(g, o)):
            if _on(g, c, x) and _on(g, o, x):
                res.append(x)
    return res


# -------------------------------------------------------------------- trim

def trim(geom, cid, click):
    """Remove the piece of curve cid between its neighbouring intersections around the click point."""
    g = copy.deepcopy(geom)
    c = sk.curve(g, cid)
    xs = intersections(g, cid)
    click = np.asarray(click, float)
    tc = _param(g, c, click)
    if c["type"] == "line":
        ts = sorted({round(_param(g, c, x), 12) for x in xs if 1e-9 < _param(g, c, x) < 1 - 1e-9})
        lo = max([t for t in ts if t < tc], default=0.0)
        hi = min([t for t in ts if t > tc], default=1.0)
        A, B = _P(g, c["a"]), _P(g, c["b"])
        g["curves"] = [x for x in g["curves"] if x["id"] != cid]
        _drop_constraints(g, [cid])
        if lo > 0:
            pm = _new_pt(g, A + lo * (B - A))
            g["curves"].append({"id": sk.new_id(g, "l"), "type": "line", "a": c["a"], "b": pm,
                                **({"construction": True} if c.get("construction") else {})})
        if hi < 1:
            pm = _new_pt(g, A + hi * (B - A))
            g["curves"].append({"id": sk.new_id(g, "l"), "type": "line", "a": pm, "b": c["b"],
                                **({"construction": True} if c.get("construction") else {})})
        return _clean_points(g)
    # circle / arc: angles from the start
    if c["type"] == "circle":
        C = _P(g, c["c"])
        angs = sorted({_ang(x - C) % (2 * math.pi) for x in xs})
        if len(angs) < 2:
            raise SketchError("A circle needs two intersections to trim")
        a = _ang(click - C) % (2 * math.pi)
        hi = min([t for t in angs if t > a], default=angs[0] + 2 * math.pi)
        lo = max([t for t in angs if t < a], default=angs[-1] - 2 * math.pi)
        r = c["r"]
        g["curves"] = [x for x in g["curves"] if x["id"] != cid]
        _drop_constraints(g, [cid])
        pa = _new_pt(g, C + r * np.array([math.cos(hi), math.sin(hi)]))
        pb = _new_pt(g, C + r * np.array([math.cos(lo), math.sin(lo)]))
        g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": c["c"], "a": pa, "b": pb})
        return _clean_points(g)
    C, r, a0, sw = _arc_span(g, c)
    ts = sorted({(_ang(x - C) - a0) % (2 * math.pi) for x in xs})
    ts = [t for t in ts if 1e-9 < t < sw - 1e-9]
    lo = max([t for t in ts if t < tc], default=0.0)
    hi = min([t for t in ts if t > tc], default=sw)
    g["curves"] = [x for x in g["curves"] if x["id"] != cid]
    _drop_constraints(g, [cid])
    if lo > 0:
        pm = _new_pt(g, C + r * np.array([math.cos(a0 + lo), math.sin(a0 + lo)]))
        g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": c["c"], "a": c["a"], "b": pm})
    if hi < sw:
        pm = _new_pt(g, C + r * np.array([math.cos(a0 + hi), math.sin(a0 + hi)]))
        g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": c["c"], "a": pm, "b": c["b"]})
    return _clean_points(g)


def _clean_points(g):
    used = {sk.ORIGIN}
    for c in g["curves"]:
        used |= set(sk.curve_points(c))
    for con in g["constraints"]:
        used |= set(con["e"])
    g["points"] = {k: v for k, v in g["points"].items() if k in used}
    return g


# ------------------------------------------------------------------ extend

def extend(geom, cid, click):
    """Extend the end of line cid nearest the click to the next curve it meets."""
    g = copy.deepcopy(geom)
    c = sk.curve(g, cid)
    if c["type"] != "line":
        raise SketchError("Extend works on lines")
    A, B = _P(g, c["a"]), _P(g, c["b"])
    end = "b" if np.linalg.norm(np.asarray(click) - B) < np.linalg.norm(np.asarray(click) - A) else "a"
    best, bt = None, math.inf
    for o in g["curves"]:
        if o["id"] == cid:
            continue
        for x in _xsect(_geo(g, c), _geo(g, o)):
            if not _on(g, o, x):
                continue
            t = _param(g, c, x)
            if end == "b" and t > 1 + 1e-9 and t < bt:
                best, bt = x, t
            if end == "a" and t < -1e-9 and -t < bt:
                best, bt = x, -t
    if best is None:
        raise SketchError("Nothing to extend to in that direction")
    pid = c[end]
    if sum(1 for o in g["curves"] for q in sk.curve_points(o) if q == pid) > 1:
        pid = _new_pt(g, best)
        c[end] = pid
    else:
        g["points"][pid] = [float(best[0]), float(best[1])]
    return g


# ------------------------------------------------------------------ offset

def offset(geom, ids, dist):
    """Offset lines/arcs/circles by dist (positive = to the left of a line's direction / outward for
    circles and arcs). Connected lines are re-joined at their offset corners."""
    g = copy.deepcopy(geom)
    curves = [sk.curve(g, i) for i in ids if sk.kind(g, i) in ("line", "circle", "arc")]
    if not curves:
        raise SketchError("Select lines, arcs or circles to offset")
    newpts = {}

    def shifted(c):
        if c["type"] == "line":
            A, B = _P(g, c["a"]), _P(g, c["b"])
            d = (B - A) / max(np.linalg.norm(B - A), 1e-30)
            n = np.array([-d[1], d[0]])
            return ("L", A + n * dist, B + n * dist)
        C = _P(g, c["c"])
        r = c["r"] if c["type"] == "circle" else float(np.linalg.norm(_P(g, c["a"]) - C))
        return ("C", C, r + dist)

    sh = {c["id"]: shifted(c) for c in curves}
    # endpoints: shared points between selected curves become the intersection of the offsets
    shared = {}
    for c in curves:
        for k in ("a", "b"):
            if c.get(k):
                shared.setdefault(c[k], []).append(c["id"])
    for pid, owners in shared.items():
        if len(owners) == 2:
            xs = _xsect(sh[owners[0]], sh[owners[1]])
            if xs:
                P = _P(g, pid)
                newpts[pid] = min(xs, key=lambda x: np.linalg.norm(x - P))
    for c in curves:
        s = sh[c["id"]]
        if c["type"] == "circle":
            if s[2] <= 0:
                raise SketchError("Offset larger than the circle")
            cp = _new_pt(g, s[1])
            g["curves"].append({"id": sk.new_id(g, "c"), "type": "circle", "c": cp, "r": float(s[2])})
            continue

        def endpoint(k):
            pid = c[k]
            if pid in newpts:
                if isinstance(newpts[pid], str):
                    return newpts[pid]
                q = _new_pt(g, newpts[pid])
                newpts[pid] = q
                return q
            P = _P(g, pid)
            if s[0] == "L":
                return _new_pt(g, s[1] if k == "a" else s[2])
            C = s[1]
            return _new_pt(g, C + (P - C) / max(np.linalg.norm(P - C), 1e-30) * s[2])

        a, b = endpoint("a"), endpoint("b")
        if c["type"] == "line":
            g["curves"].append({"id": sk.new_id(g, "l"), "type": "line", "a": a, "b": b})
        else:
            if s[2] <= 0:
                raise SketchError("Offset larger than the arc radius")
            cp = _new_pt(g, s[1])
            g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": cp, "a": a, "b": b})
    return g


# ------------------------------------------------------------------ fillet

def fillet(geom, pid, radius):
    """Round the corner at point pid where two lines meet with a tangent arc of the given radius."""
    g = copy.deepcopy(geom)
    lines = [c for c in g["curves"] if c["type"] == "line" and pid in (c["a"], c["b"])]
    if len(lines) != 2:
        raise SketchError("Pick a corner point where exactly two lines meet")
    P = _P(g, pid)
    dirs, others = [], []
    for c in lines:
        o = c["b"] if c["a"] == pid else c["a"]
        Q = _P(g, o)
        L = np.linalg.norm(Q - P)
        dirs.append((Q - P) / L)
        others.append((c, L))
    cos_t = float(np.clip(dirs[0] @ dirs[1], -1, 1))
    th = math.acos(cos_t)
    if th < 1e-3 or th > math.pi - 1e-3:
        raise SketchError("Lines are parallel — nothing to round")
    d = radius / math.tan(th / 2)
    if any(d >= L - 1e-9 for _, L in others):
        raise SketchError("Radius too big for these lines")
    bis = (dirs[0] + dirs[1]) / np.linalg.norm(dirs[0] + dirs[1])
    C = P + bis * radius / math.sin(th / 2)
    T = [P + dirs[i] * d for i in range(2)]
    tp = [_new_pt(g, T[0]), _new_pt(g, T[1])]
    for (c, _), t in zip(others, tp):
        if c["a"] == pid:
            c["a"] = t
        else:
            c["b"] = t
    cp = _new_pt(g, C)
    # CCW from one tangent point to the other
    a0 = _ang(T[0] - C)
    a1 = _ang(T[1] - C)
    sweep = (a1 - a0) % (2 * math.pi)
    a, b = (tp[0], tp[1]) if sweep < math.pi else (tp[1], tp[0])
    aid = sk.new_id(g, "a")
    g["curves"].append({"id": aid, "type": "arc", "c": cp, "a": a, "b": b})
    g["constraints"] = [k for k in g["constraints"] if pid not in k["e"]]
    for c, _ in others:
        g["constraints"].append({"type": "tangent", "e": [aid, c["id"]]})
    g["constraints"].append({"type": "radius", "e": [aid], "value": float(radius)})
    return _clean_points(g)


# ------------------------------------------------------------ copy / mirror

def _copy(g, ids, xf):
    """Copy curves through point transform xf(xy) → xy. Returns {old point id: new id}, new curve ids."""
    pm, new_ids = {}, []

    def mp(pid):
        if pid not in pm:
            pm[pid] = _new_pt(g, xf(_P(g, pid)))
        return pm[pid]

    for i in ids:
        c = sk.curve(g, i)
        n = {k: (list(v) if isinstance(v, list) else v) for k, v in c.items() if k != "id"}
        for k in sk.POINT_KEYS:
            if k in n:
                n[k] = mp(n[k])
        if "pts" in n:
            n["pts"] = [mp(q) for q in n["pts"]]
        n["id"] = sk.new_id(g, PREFIX.get(c["type"], "x"))
        g["curves"].append(n)
        new_ids.append(n["id"])
    return pm, new_ids


PREFIX = {"line": "l", "circle": "c", "arc": "a", "ellipse": "e", "conic": "k", "spline": "s", "equation": "q",
          "text": "t", "point": "o"}
MOVABLE = tuple(PREFIX)


def mirror(geom, ids, axis_line):
    g = copy.deepcopy(geom)
    ax = sk.curve(g, axis_line)
    if ax["type"] != "line":
        raise SketchError("Mirror about a line")
    A, B = _P(g, ax["a"]), _P(g, ax["b"])
    d = (B - A) / np.linalg.norm(B - A)

    def refl(p):
        v = p - A
        return A + 2 * (v @ d) * d - v

    ids = [i for i in ids if i != axis_line and sk.kind(g, i) in MOVABLE]
    if not ids:
        raise SketchError("Select curves to mirror, then the mirror line last")
    pm, new = _copy(g, ids, refl)
    for c in [sk.curve(g, i) for i in new]:
        if c["type"] == "arc":                       # mirroring reverses arc direction
            c["a"], c["b"] = c["b"], c["a"]
        elif c["type"] == "ellipse" and "s" in c:
            c["s"], c["e"] = c["e"], c["s"]
    minor = {c["b"] for c in (sk.curve(g, i) for i in new) if c["type"] == "ellipse"}
    for old, nw in pm.items():
        if nw in minor and np.linalg.norm(_P(g, old) - _P(g, nw)) >= 1e-9:
            continue                     # an ellipse's minor point is already held square by its own relation
        if np.linalg.norm(_P(g, old) - _P(g, nw)) < 1e-9:      # on the axis: merge
            for c in g["curves"]:
                sk.replace_point(c, nw, old)
        else:
            g["constraints"].append({"type": "symmetric", "e": [old, nw, axis_line]})
    return _clean_points(g)


def linear_pattern(geom, ids, dx, dy, count):
    g = copy.deepcopy(geom)
    for k in range(1, int(count)):
        _copy(g, ids, lambda p, k=k: p + np.array([dx * k, dy * k]))
    return g


def circular_pattern(geom, ids, center, count, angle=360.0):
    g = copy.deepcopy(geom)
    n = int(count)
    step = math.radians(angle) / (n if abs(angle - 360) < 1e-9 else max(n - 1, 1))
    C = np.asarray(center, float)
    for k in range(1, n):
        a = step * k
        R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        _copy(g, ids, lambda p, R=R: C + R @ (p - C))
    return g


# ------------------------------------------------------------------ chamfer

def chamfer(geom, pid, d1, d2=None):
    """Cut the corner at point pid (two lines meet) with a straight line, distances d1 / d2 along the lines."""
    g = copy.deepcopy(geom)
    d2 = d1 if d2 is None else d2
    lines = [c for c in g["curves"] if c["type"] == "line" and pid in (c["a"], c["b"])]
    if len(lines) != 2:
        raise SketchError("Pick a corner point where exactly two lines meet")
    P = _P(g, pid)
    new = []
    for c, d in zip(lines, (d1, d2)):
        o = c["b"] if c["a"] == pid else c["a"]
        Q = _P(g, o)
        L = float(np.linalg.norm(Q - P))
        if d >= L - 1e-9:
            raise SketchError("Chamfer too big for these lines")
        t = _new_pt(g, P + (Q - P) / L * d)
        if c["a"] == pid:
            c["a"] = t
        else:
            c["b"] = t
        new.append(t)
    lid = sk.new_id(g, "l")
    g["curves"].append({"id": lid, "type": "line", "a": new[0], "b": new[1]})
    g["constraints"] = [k for k in g["constraints"] if pid not in k["e"]]
    return _clean_points(g)


# ------------------------------------------------------------------ split

def split(geom, cid, click):
    """Split a line or arc at the point nearest the click (two curves sharing a new point)."""
    g = copy.deepcopy(geom)
    c = sk.curve(g, cid)
    X = np.asarray(click, float)
    if c["type"] == "line":
        A, B = _P(g, c["a"]), _P(g, c["b"])
        d = B - A
        t = float(np.clip((X - A) @ d / (d @ d or 1e-12), 0.02, 0.98))
        m = _new_pt(g, A + d * t)
        nid = sk.new_id(g, "l")
        g["curves"].append({"id": nid, "type": "line", "a": m, "b": c["b"],
                            "construction": c.get("construction", False)})
        c["b"] = m
        hv = [k["type"] for k in g["constraints"] if k["e"] == [cid] and k["type"] in ("horizontal", "vertical")]
        for t in hv:
            g["constraints"].append({"type": t, "e": [nid]})
        if not hv:
            g["constraints"].append({"type": "parallel", "e": [cid, nid]})
        g["constraints"] = [k for k in g["constraints"] if not (cid in k["e"] and k["type"] == "length")]
        return g
    if c["type"] == "arc":
        C = _P(g, c["c"])
        r = float(np.linalg.norm(_P(g, c["a"]) - C))
        a0, sw = _arc_span(g, c)
        ang = (_ang(X - C) - a0) % (2 * math.pi)
        ang = min(max(ang, sw * 0.02), sw * 0.98)
        m = _new_pt(g, C + r * np.array([math.cos(a0 + ang), math.sin(a0 + ang)]))
        nid = sk.new_id(g, "a")
        g["curves"].append({"id": nid, "type": "arc", "c": c["c"], "a": m, "b": c["b"],
                            "construction": c.get("construction", False)})
        c["b"] = m
        g["constraints"].append({"type": "equal", "e": [cid, nid]})
        return g
    raise SketchError("Split works on lines and arcs")


# ------------------------------------------------------------ move / copy / rotate / scale

def transform(geom, ids, kind, copy_=False, dx=0.0, dy=0.0, angle=0.0, scale=1.0, about=(0.0, 0.0)):
    """Move / rotate / scale selected curves (and loose points), or copy them. Points held by relations to
    other geometry are moved too; relations to unselected geometry are dropped for moved points."""
    g = copy.deepcopy(geom)
    C = np.asarray(about, float)
    a = math.radians(angle)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])

    def xf(p):
        p = np.asarray(p, float)
        if kind == "move":
            return p + np.array([dx, dy])
        if kind == "rotate":
            return C + R @ (p - C)
        if kind == "scale":
            return C + (p - C) * scale
        raise SketchError(f"Unknown transform {kind}")
    curves = [i for i in ids if i in {c["id"] for c in g["curves"]}]
    loose = [i for i in ids if i in g["points"] and i != sk.ORIGIN]
    if copy_:
        _, new = _copy(g, curves, xf)
        for c in [sk.curve(g, i) for i in new]:
            if c["type"] == "circle" and kind == "scale":
                c["r"] = float(c["r"]) * scale
            if c["type"] == "text":
                c["angle"] = float(c.get("angle", 0)) + (angle if kind == "rotate" else 0)
                if kind == "scale":
                    c["height"] = float(c.get("height", 10)) * scale
        return g
    pids = set(loose)
    for i in curves:
        pids |= set(sk.curve_points(sk.curve(g, i)))
    pids.discard(sk.ORIGIN)
    for pid in pids:
        g["points"][pid] = list(map(float, xf(g["points"][pid])))
    for i in curves:
        c = sk.curve(g, i)
        if c["type"] == "circle" and kind == "scale":
            c["r"] = float(c["r"]) * scale
        if c["type"] == "text":
            if kind == "rotate":
                c["angle"] = float(c.get("angle", 0)) + angle
            if kind == "scale":
                c["height"] = float(c.get("height", 10)) * scale
    moved = pids | set(curves)
    keep = []
    for k in g["constraints"]:
        if k["type"] == "fix" and set(k["e"]) & moved:
            continue
        if kind != "move" and k["type"] in ("horizontal", "vertical") and set(k["e"]) & moved and kind == "rotate":
            continue
        if kind == "scale" and k["type"] in sk.DIMENSIONS - {"angle"} and set(k["e"]) & moved:
            k = dict(k, value=float(k["value"]) * scale)
        if set(k["e"]) & moved and not set(k["e"]) <= moved:
            continue                                 # tie to unselected geometry: drop (it moved away)
        keep.append(k)
    g["constraints"] = keep
    return g


# ------------------------------------------------------------ segments / equidistant points

def segment_points(geom, cid, count):
    """`count` equal segments along a line / arc / circle / spline → count-1 (or count, closed) sketch points
    on the curve."""
    g = copy.deepcopy(geom)
    c = sk.curve(g, cid)
    n = int(count)
    if n < 2:
        raise SketchError("Use at least 2 segments")
    pts = sk.samples(g, c)
    if len(pts) < 2:
        raise SketchError("This curve can't be segmented")
    closed = c["type"] == "circle" or sk.is_closed_curve(g, c)
    seg = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    total = sum(seg)
    marks = [total * k / n for k in range(1 if not closed else 0, n)]
    made = []
    for m in marks:
        acc = 0.0
        for i, L in enumerate(seg):
            if acc + L >= m - 1e-12:
                t = (m - acc) / (L or 1)
                x = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
                y = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
                pid = _new_pt(g, np.array([x, y]))
                oid = sk.new_id(g, "o")
                g["curves"].append({"id": oid, "type": "point", "a": pid, "segment_of": cid})
                if c["type"] in ("line", "circle", "arc"):
                    g["constraints"].append({"type": "point_on", "e": [pid, cid]})
                made.append(pid)
                break
            acc += L
    if c["type"] == "line" and n == 2 and made:
        g["constraints"] = [k for k in g["constraints"] if not (k["type"] == "point_on" and k["e"] == [made[0], cid])]
        g["constraints"].append({"type": "midpoint", "e": [made[0], cid]})
    return g


# ------------------------------------------------------------------ belt / chain

def belt(geom, circle_ids, construction=True):
    """A belt / chain around pulleys (circles, in travel order): external tangent lines and wrap arcs on each
    pulley, tangent to the lines, concentric with and equal to the pulley."""
    g = copy.deepcopy(geom)
    circles = [sk.curve(g, i) for i in circle_ids]
    if len(circles) < 2 or any(c["type"] != "circle" for c in circles):
        raise SketchError("Select two or more circles (pulleys / sprockets) in belt order")
    C = [np.asarray(g["points"][c["c"]], float) for c in circles]
    R = [float(c["r"]) for c in circles]
    n = len(C)
    if n > 2:                                       # travel counter-clockwise around the pulleys
        area = sum(C[i][0] * C[(i + 1) % n][1] - C[(i + 1) % n][0] * C[i][1] for i in range(n))
        if area < 0:
            circles, C, R = circles[::-1], C[::-1], R[::-1]
    spans = []                                      # (point on i, point on j) per span i → i+1
    for i in range(n):
        j = (i + 1) % n
        d = C[j] - C[i]
        L = float(np.linalg.norm(d))
        if L <= abs(R[i] - R[j]) + 1e-9:
            raise SketchError("Pulleys overlap — space them apart")
        u = d / L
        right = np.array([u[1], -u[0]])
        k = (R[i] - R[j]) / L
        m = u * k + right * math.sqrt(max(0.0, 1 - k * k))
        spans.append((C[i] + R[i] * m, C[j] + R[j] * m))
    out_pt, in_pt = [], []
    for i in range(n):
        out_pt.append(_new_pt(g, spans[i][0]))
        in_pt.append(_new_pt(g, spans[i - 1][1]))
    lines, arcs = [], []
    for i in range(n):
        lid = sk.new_id(g, "l")
        g["curves"].append({"id": lid, "type": "line", "a": out_pt[i], "b": in_pt[(i + 1) % n],
                            "construction": construction, "belt": True})
        lines.append(lid)
    for i, c in enumerate(circles):
        aid = sk.new_id(g, "a")
        g["curves"].append({"id": aid, "type": "arc", "c": c["c"], "a": in_pt[i], "b": out_pt[i],
                            "construction": construction, "belt": True})
        arcs.append(aid)
        g["constraints"].append({"type": "equal", "e": [aid, c["id"]]})
        g["constraints"].append({"type": "tangent", "e": [aid, lines[i]]})
        g["constraints"].append({"type": "tangent", "e": [aid, lines[i - 1]]})
    return g


def belt_length(geom, ids) -> float:
    total = 0.0
    for i in ids:
        c = sk.curve(geom, i)
        pts = sk.samples(geom, c)
        total += sum(math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]) for k in range(len(pts) - 1))
    return total
