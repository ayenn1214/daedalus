"""2D snapping (Qt-free): endpoints, midpoints, centres, quadrants, intersections, nearest-on-curve,
perpendicular / tangent from the previous point, H/V inference from other points, grid and angle steps.

Works on a neutral item list so the sketcher (sketch geometry) and the drawing editor (sheet primitives)
share it. Each snap carries `refs` the caller can turn into sketch relations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

KINDS = ["endpoint", "midpoint", "center", "quadrant", "intersection", "nearest", "perpendicular", "tangent",
         "inference", "grid", "angle"]
DEFAULTS = {"endpoint": True, "midpoint": True, "center": True, "quadrant": True, "intersection": True,
            "nearest": True, "perpendicular": True, "tangent": True, "inference": True, "grid": False,
            "angle": True}
RANK = {"endpoint": 0, "center": 0, "intersection": 1, "midpoint": 2, "quadrant": 2, "perpendicular": 3,
        "tangent": 3, "nearest": 5, "inference": 6, "grid": 7, "angle": 8}
GLYPHS = {"endpoint": "□", "center": "⊕", "intersection": "✕", "midpoint": "△", "quadrant": "◇",
          "perpendicular": "⊥", "tangent": "◯", "nearest": "⧗", "inference": "┼", "grid": "#", "angle": "∠"}


@dataclass
class Snap:
    x: float
    y: float
    kind: str
    refs: dict = field(default_factory=dict)
    guides: list = field(default_factory=list)       # [((x0, y0), (x1, y1))] dashed helper lines

    @property
    def xy(self):
        return (self.x, self.y)


# ------------------------------------------------------------------ items ---

def sketch_items(geom, underlay=()):
    """Items from sketch geometry (+ reference polylines, e.g. projected body edges)."""
    P = geom["points"]
    items, points = [], []
    for pid, (x, y) in P.items():
        points.append((pid, (x, y)))
    for c in geom["curves"]:
        t = c["type"]
        if t == "line":
            items.append({"id": c["id"], "t": "line", "a": tuple(P[c["a"]]), "b": tuple(P[c["b"]]),
                          "pa": c["a"], "pb": c["b"]})
        elif t == "circle":
            items.append({"id": c["id"], "t": "circle", "c": tuple(P[c["c"]]), "r": float(c["r"]), "pc": c["c"]})
        elif t == "arc":
            cx, cy = P[c["c"]]
            ax, ay = P[c["a"]]
            bx, by = P[c["b"]]
            items.append({"id": c["id"], "t": "arc", "c": (cx, cy), "r": math.hypot(ax - cx, ay - cy),
                          "a0": math.atan2(ay - cy, ax - cx), "a1": math.atan2(by - cy, bx - cx), "pc": c["c"],
                          "pa": c["a"], "pb": c["b"]})
        elif t in ("ellipse", "conic", "spline", "equation"):
            from .sketch2d import samples
            pts = samples(geom, c)
            for i in range(len(pts) - 1):            # nearest / intersection snaps along free-form curves
                items.append({"id": f"_{c['id']}_{i}", "t": "line", "a": pts[i], "b": pts[i + 1], "ref": True,
                              "sampled": True})
    for k, poly in enumerate(underlay):
        for i in range(len(poly) - 1):
            items.append({"id": f"_ref{k}_{i}", "t": "line", "a": tuple(poly[i]), "b": tuple(poly[i + 1]),
                          "ref": True})
        if poly:
            points.append((None, tuple(poly[0])))
            points.append((None, tuple(poly[-1])))
    return items, points


def prim_items(prims):
    """Items from drawing primitives (sheet mm)."""
    items, points = [], []
    for k, p in enumerate(prims):
        t = p["t"]
        if t == "line":
            items.append({"id": k, "t": "line", "a": tuple(p["a"]), "b": tuple(p["b"])})
            points += [(None, tuple(p["a"])), (None, tuple(p["b"]))]
        elif t == "poly" and not p.get("fill"):
            pts = p["pts"] + ([p["pts"][0]] if p.get("closed") else [])
            for i in range(len(pts) - 1):
                items.append({"id": (k, i), "t": "line", "a": tuple(pts[i]), "b": tuple(pts[i + 1])})
            points += [(None, tuple(q)) for q in p["pts"]]
        elif t == "circle":
            items.append({"id": k, "t": "circle", "c": tuple(p["c"]), "r": p["r"]})
        elif t == "arc":
            a0, a1 = math.radians(p["a0"]), math.radians(p["a1"])
            items.append({"id": k, "t": "arc", "c": tuple(p["c"]), "r": p["r"], "a0": a0, "a1": a1})
            for a in (a0, a1):
                points.append((None, (p["c"][0] + p["r"] * math.cos(a), p["c"][1] + p["r"] * math.sin(a))))
    return items, points


# --------------------------------------------------------------- geometry ---

def _in_arc(it, ang):
    if it["t"] != "arc":
        return True
    sweep = (it["a1"] - it["a0"]) % (2 * math.pi)
    return (ang - it["a0"]) % (2 * math.pi) <= sweep + 1e-9


def _closest(it, x, y):
    if it["t"] == "line":
        (ax, ay), (bx, by) = it["a"], it["b"]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-18
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        return ax + t * dx, ay + t * dy
    cx, cy = it["c"]
    ang = math.atan2(y - cy, x - cx)
    if not _in_arc(it, ang):
        ends = [(cx + it["r"] * math.cos(a), cy + it["r"] * math.sin(a)) for a in (it["a0"], it["a1"])]
        return min(ends, key=lambda q: math.hypot(q[0] - x, q[1] - y))
    return cx + it["r"] * math.cos(ang), cy + it["r"] * math.sin(ang)


def _dist(it, x, y):
    qx, qy = _closest(it, x, y)
    return math.hypot(qx - x, qy - y)


def intersections(a, b):
    out = []
    if a["t"] == "line" and b["t"] == "line":
        (x1, y1), (x2, y2) = a["a"], a["b"]
        (x3, y3), (x4, y4) = b["a"], b["b"]
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-12:
            return out
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            out.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
        return out
    if a["t"] != "line" and b["t"] == "line":
        a, b = b, a
    if a["t"] == "line":
        (x1, y1), (x2, y2) = a["a"], a["b"]
        cx, cy = b["c"]
        r = b["r"]
        dx, dy = x2 - x1, y2 - y1
        fx, fy = x1 - cx, y1 - cy
        A = dx * dx + dy * dy
        B = 2 * (fx * dx + fy * dy)
        C = fx * fx + fy * fy - r * r
        disc = B * B - 4 * A * C
        if A < 1e-18 or disc < 0:
            return out
        for s in (-1, 1):
            t = (-B + s * math.sqrt(disc)) / (2 * A)
            if -1e-9 <= t <= 1 + 1e-9:
                px, py = x1 + t * dx, y1 + t * dy
                if _in_arc(b, math.atan2(py - cy, px - cx)):
                    out.append((px, py))
        return out
    (x0, y0), r0 = a["c"], a["r"]
    (x1, y1), r1 = b["c"], b["r"]
    d = math.hypot(x1 - x0, y1 - y0)
    if d < 1e-12 or d > r0 + r1 + 1e-9 or d < abs(r0 - r1) - 1e-9:
        return out
    aa = (r0 * r0 - r1 * r1 + d * d) / (2 * d)
    h = math.sqrt(max(r0 * r0 - aa * aa, 0.0))
    xm, ym = x0 + aa * (x1 - x0) / d, y0 + aa * (y1 - y0) / d
    for s in (-1, 1):
        px, py = xm + s * h * (y1 - y0) / d, ym - s * h * (x1 - x0) / d
        if _in_arc(a, math.atan2(py - y0, px - x0)) and _in_arc(b, math.atan2(py - y1, px - x1)):
            out.append((px, py))
    return out


# ------------------------------------------------------------------- find ---

def find(items, points, x, y, tol, opts=None, from_pt=None, grid=0.0, angle_step=15.0, exclude_points=()):
    """Best snap near (x, y) within tol (world units), or None."""
    o = {**DEFAULTS, **(opts or {})}
    cands = []

    def add(kind, qx, qy, refs=None, guides=None):
        d = math.hypot(qx - x, qy - y)
        if d <= tol:
            cands.append((RANK[kind], d, Snap(qx, qy, kind, refs or {}, guides or [])))

    if o["endpoint"]:
        for pid, (px, py) in points:
            if pid in exclude_points:
                continue
            add("endpoint", px, py, {"point": pid})
    near = [it for it in items if _dist(it, x, y) <= tol * 3]
    for it in near:
        ref = it.get("ref")
        if it["t"] == "line":
            (ax, ay), (bx, by) = it["a"], it["b"]
            if o["midpoint"] and not it.get("sampled"):
                add("midpoint", (ax + bx) / 2, (ay + by) / 2, {} if ref else {"line": it["id"]})
        else:
            cx, cy = it["c"]
            if o["center"]:
                add("center", cx, cy, {"point": it.get("pc")} if it.get("pc") else {})
            if o["quadrant"]:
                for k, ang in enumerate((0.0, math.pi / 2, math.pi, 1.5 * math.pi)):
                    if _in_arc(it, ang):
                        add("quadrant", cx + it["r"] * math.cos(ang), cy + it["r"] * math.sin(ang),
                            {"curve": it["id"], "center": it.get("pc"), "hv": "horizontal" if k % 2 == 0
                             else "vertical"})
    if o["center"]:                                      # centres of circles whose rim is far from the cursor
        for it in items:
            if it["t"] != "line" and math.hypot(it["c"][0] - x, it["c"][1] - y) <= tol:
                add("center", it["c"][0], it["c"][1], {"point": it.get("pc")} if it.get("pc") else {})
    if o["intersection"] and len(near) > 1:
        for i in range(len(near)):
            for j in range(i + 1, len(near)):
                for qx, qy in intersections(near[i], near[j]):
                    refs = {"curves": [c["id"] for c in (near[i], near[j]) if not c.get("ref")]}
                    add("intersection", qx, qy, refs)
    if from_pt is not None:
        fx, fy = from_pt
        for it in near:
            if it["t"] == "line" and o["perpendicular"]:
                (ax, ay), (bx, by) = it["a"], it["b"]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy or 1e-18
                t = ((fx - ax) * dx + (fy - ay) * dy) / L2
                if 0 <= t <= 1:
                    add("perpendicular", ax + t * dx, ay + t * dy, {} if it.get("ref") else
                        {"curve": it["id"], "perp_line": it["id"]})
            elif it["t"] != "line":
                cx, cy = it["c"]
                r = it["r"]
                dd = math.hypot(fx - cx, fy - cy)
                if o["perpendicular"] and dd > 1e-9:
                    for s in (1, -1):
                        qx, qy = cx + s * r * (fx - cx) / dd, cy + s * r * (fy - cy) / dd
                        if _in_arc(it, math.atan2(qy - cy, qx - cx)):
                            add("perpendicular", qx, qy, {"curve": it["id"], "through_center": it.get("pc")})
                if o["tangent"] and dd > r + 1e-9:
                    base = math.atan2(fy - cy, fx - cx)
                    off = math.acos(r / dd)
                    for s in (1, -1):
                        a = base + s * off
                        if _in_arc(it, a):
                            add("tangent", cx + r * math.cos(a), cy + r * math.sin(a), {"curve": it["id"]})
    if o["nearest"]:
        for it in near:
            qx, qy = _closest(it, x, y)
            add("nearest", qx, qy, {} if it.get("ref") else {"curve": it["id"]})
    if cands:
        cands.sort(key=lambda c: (c[0], c[1]))
        return cands[0][2]
    # inference: line up with other points horizontally / vertically
    sx = sy = None
    if o["inference"]:
        bx = by = tol
        for pid, (px, py) in points:
            if pid in exclude_points:
                continue
            if abs(px - x) < bx:
                bx, sx = abs(px - x), (pid, px, py)
            if abs(py - y) < by:
                by, sy = abs(py - y), (pid, px, py)
        if from_pt is not None:
            if abs(from_pt[0] - x) < bx:
                sx = (None, from_pt[0], from_pt[1])
            if abs(from_pt[1] - y) < by:
                sy = (None, from_pt[0], from_pt[1])
    if sx or sy:
        qx = sx[1] if sx else x
        qy = sy[2] if sy else y
        if not sx or not sy:
            g = grid if o["grid"] and grid > 0 else 0
            if g:
                qx = qx if sx else round(qx / g) * g
                qy = qy if sy else round(qy / g) * g
        refs, guides = {}, []
        if sx:
            refs["vertical_with"] = sx[0]
            guides.append(((sx[1], sx[2]), (qx, qy)))
        if sy:
            refs["horizontal_with"] = sy[0]
            guides.append(((sy[1], sy[2]), (qx, qy)))
        return Snap(qx, qy, "inference", refs, guides)
    if o["angle"] and from_pt is not None and angle_step > 0:
        fx, fy = from_pt
        L = math.hypot(x - fx, y - fy)
        if L > tol:
            ang = math.degrees(math.atan2(y - fy, x - fx))
            k = round(ang / angle_step) * angle_step
            if abs(ang - k) < 2.0 and k % 90 != 0:
                a = math.radians(k)
                return Snap(fx + L * math.cos(a), fy + L * math.sin(a), "angle", {"angle": k},
                            [((fx, fy), (fx + L * 1.2 * math.cos(a), fy + L * 1.2 * math.sin(a)))])
    if o["grid"] and grid > 0:
        return Snap(round(x / grid) * grid, round(y / grid) * grid, "grid")
    return None


def relations_for(snap: Snap, pid: str) -> list[dict]:
    """Sketch relations that keep a new point `pid` where it snapped."""
    r = snap.refs
    out = []
    if snap.kind == "midpoint" and r.get("line"):
        out.append({"type": "midpoint", "e": [pid, r["line"]]})
    elif snap.kind in ("nearest", "perpendicular", "tangent") and r.get("curve"):
        out.append({"type": "point_on", "e": [pid, r["curve"]]})
    elif snap.kind == "quadrant" and r.get("curve"):
        out.append({"type": "point_on", "e": [pid, r["curve"]]})
        if r.get("center"):
            out.append({"type": r["hv"], "e": [pid, r["center"]]})
    elif snap.kind == "intersection":
        out += [{"type": "point_on", "e": [pid, c]} for c in r.get("curves", [])]
    elif snap.kind == "inference":
        if r.get("vertical_with"):
            out.append({"type": "vertical", "e": [pid, r["vertical_with"]]})
        if r.get("horizontal_with"):
            out.append({"type": "horizontal", "e": [pid, r["horizontal_with"]]})
    return out
