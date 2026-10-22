"""Sheet metal: base flange + edge flanges with true cylindrical bends, hems, reliefs, gauge tables,
bend-allowance methods, flange position / length references, and a folded ⇄ flat state.

Cross-sections are built along a centre path (arcs + lines) offset by ±t/2, so bends are exact
cylinders and hems fold back over the inside of the flange.
"""
from __future__ import annotations

import math

import numpy as np
from build123d import Edge, Face, Part, Plane, Vector, Wire, extrude

from .features import Feature, FeatureError, Param, get_profile

SIDES = ["+X", "-X", "+Y", "-Y"]

GAUGES = {  # nominal thickness, mm (Manufacturers' Standard Gauge / aluminium & stainless sheet gauges)
    "Steel 7 ga": 4.554, "Steel 8 ga": 4.176, "Steel 10 ga": 3.416, "Steel 11 ga": 3.038, "Steel 12 ga": 2.657,
    "Steel 14 ga": 1.897, "Steel 16 ga": 1.519, "Steel 18 ga": 1.214, "Steel 20 ga": 0.912, "Steel 22 ga": 0.759,
    "Steel 24 ga": 0.607, "Steel 26 ga": 0.455,
    "Galvanized 10 ga": 3.512, "Galvanized 12 ga": 2.753, "Galvanized 14 ga": 1.994, "Galvanized 16 ga": 1.613,
    "Galvanized 18 ga": 1.311, "Galvanized 20 ga": 1.006, "Galvanized 22 ga": 0.853, "Galvanized 24 ga": 0.701,
    "Stainless 10 ga": 3.571, "Stainless 11 ga": 3.175, "Stainless 12 ga": 2.778, "Stainless 14 ga": 1.984,
    "Stainless 16 ga": 1.588, "Stainless 18 ga": 1.270, "Stainless 20 ga": 0.953, "Stainless 22 ga": 0.794,
    "Stainless 24 ga": 0.635,
    "Aluminum 8 ga": 3.264, "Aluminum 10 ga": 2.588, "Aluminum 12 ga": 2.052, "Aluminum 14 ga": 1.628,
    "Aluminum 16 ga": 1.290, "Aluminum 18 ga": 1.024, "Aluminum 20 ga": 0.813, "Aluminum 22 ga": 0.643,
    "Aluminum 24 ga": 0.511,
    "Metric 0.5 mm": 0.5, "Metric 0.8 mm": 0.8, "Metric 1.0 mm": 1.0, "Metric 1.2 mm": 1.2, "Metric 1.5 mm": 1.5,
    "Metric 2.0 mm": 2.0, "Metric 2.5 mm": 2.5, "Metric 3.0 mm": 3.0, "Metric 4.0 mm": 4.0, "Metric 5.0 mm": 5.0,
    "Metric 6.0 mm": 6.0,
}
K_TYPICAL = {"soft (aluminium, copper)": 0.38, "mild steel": 0.44, "stainless / hard": 0.47, "air bend, R ≥ 2t": 0.5}


def _cap(theta):
    """tan(θ/2) used for set-backs, capped at 1 for bends past 90° (measured to the tangent there)."""
    return math.tan(min(theta, math.pi / 2) / 2)


# ---------------------------------------------------------- strip builder ---

def _path(theta, rc, t, up, L, hem=None, hem_r=0.0, hem_len=0.0):
    """Centre path of a flange in (u, w): u outward from the bend start, w through the plate (0..t)."""
    segs = []
    p = np.array([0.0, t / 2])
    d = np.array([1.0, 0.0])

    def arc(p, d, r, sweep):
        nl = np.array([-d[1], d[0]])                     # left normal
        c = p + nl * r if sweep > 0 else p - nl * r
        a0 = math.atan2(p[1] - c[1], p[0] - c[0])
        segs.append(("arc", c, r, a0, sweep))
        a1 = a0 + sweep
        q = c + r * np.array([math.cos(a1), math.sin(a1)])
        rot = np.array([[math.cos(sweep), -math.sin(sweep)], [math.sin(sweep), math.cos(sweep)]])
        return q, rot @ d

    def line(p, d, L):
        q = p + d * L
        if L > 1e-9:
            segs.append(("line", p.copy(), q))
        return q

    s = theta if up else -theta
    p, d = arc(p, d, rc, s)
    p = line(p, d, L)
    if hem:
        hs = math.pi if up else -math.pi                 # fold back over the inside of the flange
        p, d = arc(p, d, hem_r + t / 2, hs)
        p = line(p, d, hem_len)
    return segs


def _offset(seg, h):
    """Left offset (+h) of a segment."""
    if seg[0] == "line":
        _, a, b = seg
        d = (b - a) / (np.linalg.norm(b - a) or 1)
        nl = np.array([-d[1], d[0]])
        return ("line", a + nl * h, b + nl * h)
    _, c, r, a0, sw = seg
    return ("arc", c, r - h if sw > 0 else r + h, a0, sw)


def _ends(seg):
    if seg[0] == "line":
        return seg[1], seg[2]
    _, c, r, a0, sw = seg
    return (c + r * np.array([math.cos(a0), math.sin(a0)]),
            c + r * np.array([math.cos(a0 + sw), math.sin(a0 + sw)]))


def _edges3(seg, pl, reverse=False):
    a, b = _ends(seg)
    if reverse:
        a, b = b, a
    P = lambda q: pl.from_local_coords((float(q[0]), float(q[1]), 0.0))  # noqa: E731
    if np.linalg.norm(b - a) < 1e-9 and (seg[0] == "line" or seg[2] < 1e-9):
        return []
    if seg[0] == "line":
        return [Edge.make_line(P(a), P(b))]
    _, c, r, a0, sw = seg
    if r < 1e-9:
        return []
    am = a0 + sw / 2
    m = c + r * np.array([math.cos(am), math.sin(am)])
    if abs(sw) > math.pi * 0.999:                        # split long arcs for robustness
        q1 = c + r * np.array([math.cos(a0 + sw / 4), math.sin(a0 + sw / 4)])
        q3 = c + r * np.array([math.cos(a0 + 3 * sw / 4), math.sin(a0 + 3 * sw / 4)])
        pts = [a, q1, m, q3, b] if not reverse else [a, q3, m, q1, b]
        return [Edge.make_three_point_arc(P(pts[0]), P(pts[1]), P(pts[2])),
                Edge.make_three_point_arc(P(pts[2]), P(pts[3]), P(pts[4]))]
    return [Edge.make_three_point_arc(P(a), P(m), P(b))]


def strip_face(segs, t, pl: Plane) -> Face:
    """Cross-section face of thickness t around a centre path."""
    left = [_offset(s, t / 2) for s in segs]
    right = [_offset(s, -t / 2) for s in segs]
    edges = []
    for s in left:
        edges += _edges3(s, pl)
    P = lambda q: pl.from_local_coords((float(q[0]), float(q[1]), 0.0))  # noqa: E731
    le, re = _ends(left[-1])[1], _ends(right[-1])[1]
    edges.append(Edge.make_line(P(le), P(re)))
    for s in reversed(right):
        edges += _edges3(s, pl, reverse=True)
    ls, rs = _ends(left[0])[0], _ends(right[0])[0]
    edges.append(Edge.make_line(P(rs), P(ls)))
    edges = [e for e in edges if e.length > 1e-9]
    return Face(Wire(edges))


# ------------------------------------------------------------------ feature ---

def _poly(face, plane, tol=0.05):
    """Face (in 3D on plane) → shapely polygon in sketch coordinates."""
    from shapely.geometry import Polygon

    def ring(w):
        n = max(8, int(w.length / max(tol * 4, 0.2)))
        pts = [plane.to_local_coords(w.position_at(i / n)) for i in range(n)]
        return [(p.X, p.Y) for p in pts]
    return Polygon(ring(face.outer_wire()), [ring(w) for w in face.inner_wires()])


class SheetMetal(Feature):
    TYPE = "sheet_metal"
    LABEL = "Sheet Metal"
    DESCRIPTION = ("Sheet-metal part: base flange from a closed sketch (holes allowed) plus edge flanges. "
                   "flanges: [{edge: '+X'|'-X'|'+Y'|'-Y' or edge index, length, angle (deg, default 90), down, "
                   "start_offset, end_offset (mm in from the edge ends), hem: none|closed|open, hem_length, "
                   "hem_gap, length_ref / position (per-flange overrides)}]. length_ref: outer (outside virtual "
                   "sharp) | inner | tangent. position: material_inside (flange outside flush with the edge) | "
                   "material_outside | bend_outside | bend_centerline. method: k_factor | bend_allowance | "
                   "bend_deduction (bend_value = BA or BD for a 90° bend). gauge sets the thickness from a "
                   "table. relief: rectangular | obround | none at flange ends; corner_relief: square | "
                   "circular | none. state: folded | flat (shows the flat pattern).")
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Closed sketch for the base flange"),
        Param("gauge", "choice", "custom", "Gauge table entry (sets thickness) or custom", ["custom", *GAUGES]),
        Param("thickness", "float", 1.5, "Material thickness (mm) — used when gauge is custom", minimum=0.05),
        Param("bend_radius", "float", 1.5, "Inside bend radius (mm)", minimum=0.0),
        Param("method", "choice", "k_factor", "Bend allowance method",
              ["k_factor", "bend_allowance", "bend_deduction"]),
        Param("k_factor", "float", 0.44, "K-factor (neutral axis position, 0…0.5)", minimum=0.0),
        Param("bend_value", "float", 0.0, "BA or BD for a 90° bend (methods bend_allowance / bend_deduction)"),
        Param("length_ref", "choice", "outer", "Flange length measured to: outer | inner virtual sharp | tangent",
              ["outer", "inner", "tangent"]),
        Param("position", "choice", "material_inside", "Flange position relative to the sketch edge",
              ["material_inside", "material_outside", "bend_outside", "bend_centerline"]),
        Param("relief", "choice", "rectangular", "Bend relief where a flange is shorter than its edge",
              ["rectangular", "obround", "none"]),
        Param("relief_width", "float", 0.0, "Relief width (mm, 0 = thickness)", minimum=0.0),
        Param("relief_depth", "float", 0.0, "Relief depth past the bend (mm, 0 = ½ thickness)", minimum=0.0),
        Param("corner_relief", "choice", "square", "Relief where two flanges meet",
              ["square", "circular", "none"]),
        Param("corner_size", "float", 0.0, "Circular corner relief radius (mm, 0 = R + t)", minimum=0.0),
        Param("state", "choice", "folded", "folded | flat (show the flat pattern)", ["folded", "flat"]),
        Param("corners", "choice", "open", "Where two flanges meet: open (relieved) | miter (flanges run into a "
              "mitred corner — a miter flange)", ["open", "miter"]),
        Param("miter_gap", "float", 0.2, "Gap left in mitred corners (mm)", minimum=0.0),
        Param("flanges", "json", [], "Edge flanges [{edge, length, angle, down, start_offset, end_offset, hem, "
                                     "hem_length, hem_gap, length_ref, position}]",
              schema={"type": "array", "items": {"type": "object", "properties": {
                  "edge": {"description": "'+X' | '-X' | '+Y' | '-Y' or edge index"},
                  "length": {"type": "number"}, "angle": {"type": "number"}, "down": {"type": "boolean"},
                  "start_offset": {"type": "number"}, "end_offset": {"type": "number"},
                  "hem": {"type": "string", "enum": ["none", "closed", "open"]},
                  "hem_length": {"type": "number"}, "hem_gap": {"type": "number"},
                  "length_ref": {"type": "string"}, "position": {"type": "string"}},
                  "required": ["edge", "length"]}}),
    ]

    def summary(self):
        p = self.params
        t = self.thickness()
        hems = sum(1 for f in p["flanges"] if f.get("hem", "none") not in ("none", None, ""))
        g = "" if p.get("gauge", "custom") == "custom" else f"{p['gauge']} · "
        return (f"{g}t{t:g} R{p['bend_radius']:g} · {len(p['flanges'])} flange(s)"
                + (f" · {hems} hem(s)" if hems else "") + (" · FLAT" if p.get("state") == "flat" else ""))

    def thickness(self):
        g = self.params.get("gauge", "custom")
        return float(GAUGES.get(g, self.params["thickness"]))

    def k_eff(self, t, R):
        """Effective K-factor for the chosen method (BA/BD given for 90° bends)."""
        p = self.params
        m = p.get("method", "k_factor")
        if m == "k_factor" or not p.get("bend_value"):
            return min(max(p["k_factor"], 0.0), 0.5)
        v = float(p["bend_value"])
        ba90 = v if m == "bend_allowance" else 2 * (R + t) - v
        return min(max((ba90 / (math.pi / 2) - R) / t, 0.0), 1.0)

    # ---------------------------------------------------------- geometry
    def _outline(self, ctx):
        sk, plane = get_profile(ctx, self.params["sketch"])
        fs = sk.faces()
        if len(fs) != 1:
            raise FeatureError("Base flange: one closed profile (holes inside it are fine)")
        face = fs[0]
        edges = []
        for e in face.outer_wire().edges():
            a, b = plane.to_local_coords(e.position_at(0)), plane.to_local_coords(e.position_at(1))
            edges.append({"line": e.geom_type.name == "LINE", "a": (a.X, a.Y), "b": (b.X, b.Y)})
        area = sum(e["a"][0] * e["b"][1] - e["b"][0] * e["a"][1] for e in edges)
        return face, plane, edges, area > 0

    def _pick_edge(self, edges, key):
        lines = [i for i, e in enumerate(edges) if e["line"]]
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            i = int(key)
            if i not in lines:
                raise FeatureError(f"Edge {i} isn't a straight edge of the outline")
            return i
        if key not in SIDES:
            raise FeatureError(f"Flange edge must be one of {SIDES} or an edge index")
        ax = 0 if key[1] == "X" else 1
        sgn = 1 if key[0] == "+" else -1
        return max(lines, key=lambda i: sgn * (edges[i]["a"][ax] + edges[i]["b"][ax]) / 2
                   - 1e-3 * abs(edges[i]["a"][ax] - edges[i]["b"][ax]))

    @staticmethod
    def _corner2(f, g, end):
        """2D corner where flange f's shifted edge meets neighbour g's (end 'a' or 'b' of f)."""
        base = f["b"] if end == "b" else f["a"]
        return base - f["out"] * f["shift"] - g["out"] * g["shift"]

    def edge_near(self, ctx_face_plane, xy):
        """Index of the straight outline edge nearest a sketch-plane point."""
        face, plane, edges, _ = ctx_face_plane
        best, bd = None, float("inf")
        for i, e in enumerate(edges):
            if not e["line"]:
                continue
            a, b = np.array(e["a"]), np.array(e["b"])
            ab = b - a
            tt = max(0, min(1, float(np.dot(np.array(xy) - a, ab) / max(np.dot(ab, ab), 1e-12))))
            dd = float(np.linalg.norm(a + ab * tt - np.array(xy)))
            if dd < bd:
                best, bd = i, dd
        return best

    def build(self, ctx):
        from shapely.geometry import Point, Polygon
        from shapely.ops import unary_union
        p = self.params
        t = self.thickness()
        R = float(p["bend_radius"])
        K = self.k_eff(t, R)
        face, plane, edges, ccw = self._outline(ctx)
        n_e = len(edges)
        picked = {}
        for fl in p["flanges"]:
            i = self._pick_edge(edges, fl.get("edge", "+X"))
            if i in picked:
                raise FeatureError(f"Two flanges on edge {i}")
            picked[i] = fl
        n3 = plane.z_dir
        rc = R + t / 2

        def info(i, fl):
            e = edges[i]
            a2, b2 = np.array(e["a"], float), np.array(e["b"], float)
            if not ccw:
                a2, b2 = b2, a2
            L_edge = float(np.linalg.norm(b2 - a2))
            ed = (b2 - a2) / L_edge
            out = np.array([ed[1], -ed[0]])
            theta = math.radians(float(fl.get("angle", 90.0)))
            if not 0 < theta <= math.radians(180):
                raise FeatureError("Flange angle must be in (0, 180]°")
            pos = fl.get("position") or p.get("position", "material_inside")
            BA = theta * (R + K * t)
            shift = {"material_inside": (R + t) * _cap(theta), "material_outside": R * _cap(theta),
                     "bend_outside": 0.0, "bend_centerline": BA / 2}.get(pos)
            if shift is None:
                raise FeatureError(f"position: material_inside | material_outside | bend_outside | bend_centerline")
            ref = fl.get("length_ref") or p.get("length_ref", "outer")
            L = float(fl.get("length", 10.0))
            Lt = {"outer": L - (R + t) * _cap(theta), "inner": L - R * _cap(theta), "tangent": L}.get(ref)
            if Lt is None:
                raise FeatureError("length_ref: outer | inner | tangent")
            if Lt < -1e-9:
                raise FeatureError(f"Flange on edge {i} is shorter than its bend ({L:g} mm)")
            return {"a": a2, "b": b2, "L_edge": L_edge, "ed": ed, "out": out, "theta": theta, "shift": shift,
                    "Lt": max(Lt, 0.0), "BA": BA, "down": bool(fl.get("down", False)), "fl": fl}

        F = {i: info(i, fl) for i, fl in picked.items()}

        # base flange: sketch face clipped back to each shifted edge
        base_poly = _poly(face, plane)
        big = max(base_poly.bounds[2] - base_poly.bounds[0], base_poly.bounds[3] - base_poly.bounds[1]) * 4 + 100
        cuts3 = []
        clip = []
        for i, f in F.items():
            if f["shift"] > 1e-9:
                q0 = f["a"] - f["ed"] * big - f["out"] * f["shift"]
                q1 = f["b"] + f["ed"] * big - f["out"] * f["shift"]
                clip.append(Polygon([tuple(q0), tuple(q1), tuple(q1 + f["out"] * big), tuple(q0 + f["out"] * big)]))
        # relief slots and corner reliefs (cut from the base in 2D and 3D)
        relief = p.get("relief", "rectangular")
        rw = float(p.get("relief_width") or 0) or t
        rd = float(p.get("relief_depth") or 0) or 0.5 * t
        neigh = {}
        miter = p.get("corners", "open") == "miter"
        gap = float(p.get("miter_gap", 0.2) or 0.0)

        def _mitre_ok(f, g):
            return miter and f["down"] == g["down"] and abs(f["theta"] - g["theta"]) < 1e-9
        ext = R + 2 * t + 1.0
        for i, f in F.items():
            prev_i, next_i = ((i - 1) % n_e, (i + 1) % n_e) if ccw else ((i + 1) % n_e, (i - 1) % n_e)
            f["prev"], f["next"] = (prev_i if prev_i in F else None), (next_i if next_i in F else None)
            f["mit_a"] = f["prev"] is not None and _mitre_ok(f, F[prev_i])
            f["mit_b"] = f["next"] is not None and _mitre_ok(f, F[next_i])
            trim_a = (-ext if f["mit_a"] else (R + t)) if prev_i in F else 0.0
            trim_b = (-ext if f["mit_b"] else (R + t)) if next_i in F else 0.0
            so = float(f["fl"].get("start_offset", 0.0) or 0.0)
            eo = float(f["fl"].get("end_offset", 0.0) or 0.0)
            f["s0"], f["s1"] = trim_a + so, f["L_edge"] - trim_b - eo
            if f["s1"] - f["s0"] <= 1e-6:
                raise FeatureError(f"Flange on edge {i} has no width left after offsets / neighbouring flanges")
            neigh[i] = (prev_i in F, next_i in F)
            if relief != "none":
                for end, s_at, has_nb, sign in ((0, f["s0"], neigh[i][0], -1), (1, f["s1"], neigh[i][1], 1)):
                    off = so if end == 0 else eo
                    if off <= 1e-9:                     # runs to the corner: corner relief handles it
                        continue
                    base_pt = f["a"] + f["ed"] * s_at - f["out"] * f["shift"]
                    d_in = rd + (R + t if f["shift"] < 1e-9 else 0.0)
                    a_ = base_pt
                    b_ = base_pt + f["ed"] * sign * rw
                    poly = Polygon([tuple(a_ - f["out"] * d_in), tuple(b_ - f["out"] * d_in),
                                    tuple(b_ + f["out"] * (big)), tuple(a_ + f["out"] * big)])
                    if relief == "obround":
                        c = (a_ + b_) / 2 - f["out"] * d_in
                        poly = poly.union(Point(*c).buffer(rw / 2, quad_segs=16))
                    cuts3.append(poly)
        corner = p.get("corner_relief", "square")
        if corner == "circular" and not miter:
            rcorner = float(p.get("corner_size") or 0) or (R + t)
            for i, f in F.items():
                if neigh[i][1]:
                    j = ((i + 1) % n_e) if ccw else ((i - 1) % n_e)
                    g = F[j]
                    ci = f["b"] - f["out"] * f["shift"] - f["ed"] * 0
                    # corner of the two shifted edges
                    cpt = ci - g["out"] * g["shift"]
                    cuts3.append(Point(*cpt).buffer(rcorner, quad_segs=24))
        base_flat = base_poly
        for c in clip + cuts3:
            base_flat = base_flat.difference(c)
        if base_flat.is_empty:
            raise FeatureError("Nothing left of the base flange")

        # flat pattern strips
        strips, bends = [], []
        for i, f in F.items():
            o_ = f["out"]
            q0 = f["a"] + f["ed"] * f["s0"] - o_ * f["shift"]
            q1 = f["a"] + f["ed"] * f["s1"] - o_ * f["shift"]
            hem = f["fl"].get("hem", "none") or "none"
            hem_len = float(f["fl"].get("hem_length", 0) or 0) or (4 * t if hem != "none" else 0.0)
            hem_gap = 0.0 if hem == "closed" else (float(f["fl"].get("hem_gap", 0) or 0) or (t if hem == "open"
                                                                                              else 0.0))
            hem_BA = math.pi * (hem_gap / 2 + K * t) if hem != "none" else 0.0
            total = f["BA"] + f["Lt"] + (hem_BA + hem_len if hem != "none" else 0.0)
            strip = Polygon([tuple(q0), tuple(q1), tuple(q1 + o_ * total), tuple(q0 + o_ * total)])
            for end in ("a", "b"):
                if not f[f"mit_{end}"]:
                    continue
                g = F[f["prev"] if end == "a" else f["next"]]
                c2 = self._corner2(f, g, end)
                ed_out = f["ed"] if end == "b" else -f["ed"]
                cosang = float(np.dot(-ed_out, g["ed"] if end == "b" else -g["ed"]))
                half = math.acos(max(-1.0, min(1.0, cosang))) / 2
                w_eff = (R + K * t) / max(math.tan(half), 1e-6)
                s_end = c2 + ed_out * (w_eff - gap / 2)
                keep = Polygon([tuple(s_end - o_ * big), tuple(s_end + o_ * big), tuple(s_end + o_ * big - ed_out * big),
                                tuple(s_end - o_ * big - ed_out * big)])
                strip = strip.intersection(keep)
            strips.append(strip)
            bends.append({"edge": i, "angle": math.degrees(f["theta"]), "radius": R,
                          "direction": "down" if f["down"] else "up",
                          "line": (tuple(q0 + o_ * f["BA"] / 2), tuple(q1 + o_ * f["BA"] / 2)),
                          "allowance": f["BA"], "k_factor": K})
            if hem != "none":
                off = f["BA"] + f["Lt"] + hem_BA / 2
                bends.append({"edge": i, "angle": 180.0, "radius": hem_gap / 2, "direction": "down" if not f["down"]
                              else "up", "line": (tuple(q0 + o_ * off), tuple(q1 + o_ * off)), "allowance": hem_BA,
                              "hem": hem})
            f.update(q0=q0, q1=q1, hem=hem, hem_len=hem_len, hem_gap=hem_gap)
        flat = unary_union([base_flat, *strips])
        polys = list(getattr(flat, "geoms", [flat]))
        outline = [{"outer": [tuple(map(float, q)) for q in pg.exterior.coords[:-1]],
                    "holes": [[tuple(map(float, q)) for q in r.coords[:-1]] for r in pg.interiors]}
                   for pg in polys if pg.area > 1e-9]
        b = flat.bounds
        ctx.flats[self.name] = {"outline": outline, "polygons": [o["outer"] for o in outline], "bends": bends,
                                "state": p.get("state", "folded"),
                                "thickness": t, "k_factor": K, "bend_radius": R,
                                "size": (round(b[2] - b[0], 3), round(b[3] - b[1], 3)),
                                "plane": plane, "edges": [(e["a"], e["b"], e["line"]) for e in edges]}

        if p.get("state", "folded") == "flat":
            solid = None
            for f_ in flat_pattern_face(ctx.flats[self.name], plane):
                s = extrude(f_, t, dir=n3)
                solid = s if solid is None else solid + s
            ctx.apply(Part(solid.wrapped) if not isinstance(solid, Part) else solid, "join", self.name)
            try:
                from build123d import Compound
                lines = [Edge.make_line(plane.from_local_coords((*bd["line"][0], t + 0.01)),
                                        plane.from_local_coords((*bd["line"][1], t + 0.01))) for bd in bends]
                if lines:
                    ctx.sketch_display[self.name] = (Compound(lines), Plane.XY)
            except Exception:  # noqa: BLE001
                pass
            return

        # folded: base plate (true sketch geometry, clipped) + bends
        base3 = face
        for c in clip + cuts3:
            pts = [plane.from_local_coords((x, y, 0)) for x, y in c.exterior.coords[:-1]]
            try:
                base3 = base3 - Face(Wire.make_polygon(pts, close=True))
            except Exception:  # noqa: BLE001
                pass
        fs = base3.faces() if hasattr(base3, "faces") else [base3]
        parts = [extrude(f_, t, dir=n3) for f_ in fs]
        for i, f in F.items():
            up = not f["down"]
            segs = _path(f["theta"], rc, t, up, f["Lt"], hem=f["hem"] != "none", hem_r=f["hem_gap"] / 2,
                         hem_len=f["hem_len"])
            o3 = plane.from_local_coords((f["out"][0], f["out"][1], 0)) - plane.origin
            e3 = plane.from_local_coords((f["ed"][0], f["ed"][1], 0)) - plane.origin
            start = plane.from_local_coords((f["q0"][0], f["q0"][1], 0))
            sec_pl = Plane(origin=start, x_dir=o3, z_dir=o3.cross(n3))
            try:
                sec = strip_face(segs, t, sec_pl)
                width = f["s1"] - f["s0"]
                body = extrude(sec, width, dir=e3)
                for end in ("a", "b"):
                    if not f[f"mit_{end}"]:
                        continue
                    g = F[f["prev"] if end == "a" else f["next"]]
                    c2 = self._corner2(f, g, end)
                    b2 = f["out"] + g["out"]
                    b2 = b2 / (np.linalg.norm(b2) or 1.0)
                    m2 = np.array([-b2[1], b2[0]])
                    if np.dot(m2, f["ed"] if end == "b" else -f["ed"]) < 0:
                        m2 = -m2
                    c3 = plane.from_local_coords((float(c2[0] - m2[0] * gap / 2), float(c2[1] - m2[1] * gap / 2), 0))
                    m3 = plane.from_local_coords((float(m2[0]), float(m2[1]), 0)) - plane.origin
                    size = (abs(f["Lt"]) + R + 3 * t + width) * 4 + 50
                    pl_c = Plane(origin=c3, z_dir=m3)
                    from build123d import Box, Pos
                    body = body - pl_c.location * Pos(0, 0, size / 2) * Box(size, size, size)
                parts.append(body)
            except Exception as ex:  # noqa: BLE001
                raise FeatureError(f"Flange on edge {i} failed ({ex})") from ex
        tool = parts[0]
        for s in parts[1:]:
            tool = tool + s
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, "join", self.name)


def flat_pattern_face(flat, plane: Plane | None = None):
    """Flat-pattern faces (sketch coordinates, or placed on plane)."""
    P = (lambda x, y: plane.from_local_coords((x, y, 0))) if plane is not None else \
        (lambda x, y: Vector(x, y, 0))
    if flat.get("outline"):
        out = []
        for o in flat["outline"]:
            f = Face(Wire.make_polygon([P(x, y) for x, y in o["outer"]], close=True))
            for h in o["holes"]:
                try:
                    f = f - Face(Wire.make_polygon([P(x, y) for x, y in h], close=True))
                except Exception:  # noqa: BLE001
                    pass
            out += f.faces() if hasattr(f, "faces") else [f]
        return out
    from build123d import Shape
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    faces = [Face(Wire.make_polygon([P(x, y) for x, y in poly], close=True)) for poly in flat["polygons"]]
    out = faces[0]
    for f in faces[1:]:
        out = out.fuse(f)
    u = ShapeUpgrade_UnifySameDomain(out.wrapped, True, True, True)
    u.Build()
    return Shape.cast(u.Shape()).faces()
