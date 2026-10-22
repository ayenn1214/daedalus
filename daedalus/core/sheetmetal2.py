"""v2.8 sheet metal: sketched bends, convert a thin-walled solid to sheet metal (with an unfolded flat pattern),
and forming tools (dimple, rib, louver, extruded hole). Miter-flange corners live in sheetmetal.SheetMetal.
"""
from __future__ import annotations

import math

import numpy as np
from build123d import Box, Compound, Edge, Face, Part, Plane, Pos, Vector, Wire, extrude

from .features import Feature, FeatureError, Param, get_paths, resolve_plane
from .sheetmetal import _cap


def _part(s):
    from .features import as_part
    return as_part(s)


def _halfspace(origin: Vector, normal: Vector, size: float):
    """A big box filling the side of the plane the normal points to."""
    pl = Plane(origin=origin, z_dir=normal)
    return pl.location * Pos(0, 0, size / 2) * Box(size, size, size)


def _sheet_info(ctx, sheet: str):
    fl = ctx.flats.get(sheet)
    if fl is None:
        raise FeatureError(f"'{sheet}' isn't a built sheet-metal feature before this one")
    return fl


# ================================================================ sketched bend ===

class SketchedBend(Feature):
    TYPE = "sketched_bend"
    LABEL = "Sketched Bend"
    DESCRIPTION = ("Bend a flat sheet-metal plate along a sketched line (sketch on the plate's plane). The side "
                   "without 'fixed' moves; bend allowance from the sheet's K-factor, so the flat pattern stays "
                   "true.")
    PARAMS = [
        Param("sheet", "feature_ref", "", "Sheet-metal feature (thickness, radius, K-factor)"),
        Param("sketch", "sketch_ref", "", "Sketch with the bend line"),
        Param("angle", "float", 90.0, "Bend angle (deg)", minimum=0.1),
        Param("down", "bool", False, "Bend down (away from the sheet's top)"),
        Param("flip", "bool", False, "Move the other side of the line"),
        Param("position", "choice", "centerline", "Where the line sits: centerline | material_inside | "
              "material_outside | bend_outside", ["centerline", "material_inside", "material_outside",
                                                  "bend_outside"]),
        Param("radius", "float", 0.0, "Inside radius (mm, 0 = the sheet's)", minimum=0.0),
    ]

    def summary(self):
        p = self.params
        return f"{p['angle']:g}° {'down' if p['down'] else 'up'} on {p['sketch']}"

    def build(self, ctx):
        p = self.params
        fl = _sheet_info(ctx, p["sheet"])
        if fl.get("state") == "flat":
            return                                           # flattened: nothing to bend
        body = ctx.require_body()
        t, K = float(fl["thickness"]), float(fl["k_factor"])
        R = float(p["radius"] or fl["bend_radius"])
        pl: Plane = fl["plane"]
        wires = get_paths(ctx, p["sketch"])
        lines = [e for w in wires for e in w.edges() if e.geom_type.name == "LINE"]
        if not lines:
            raise FeatureError("The bend sketch needs a straight line")
        e = max(lines, key=lambda e_: e_.length)
        a = pl.to_local_coords(e.position_at(0))
        b = pl.to_local_coords(e.position_at(1))
        L0 = np.array([a.X, a.Y])
        u2 = np.array([b.X - a.X, b.Y - a.Y])
        u2 /= np.linalg.norm(u2)
        w2 = np.array([-u2[1], u2[0]]) * (-1 if p["flip"] else 1)
        th = math.radians(p["angle"])
        BA = th * (R + K * t)
        shift = {"centerline": 0.0, "bend_outside": BA / 2, "material_inside": BA / 2 - (R + t) * _cap(th),
                 "material_outside": BA / 2 - R * _cap(th)}[p["position"]]
        L0 = L0 + w2 * shift
        P3 = lambda x, y, z=0.0: pl.from_local_coords((float(x), float(y), float(z)))  # noqa: E731
        o3 = P3(*L0)
        u3 = (P3(*(L0 + u2)) - o3).normalized()
        w3 = (P3(*(L0 + w2)) - o3).normalized()
        n3 = pl.z_dir
        size = body.bounding_box().diagonal * 3 + 20
        fixed = body - _halfspace(o3 - w3 * (BA / 2), w3, size)
        moving = body & _halfspace(o3 + w3 * (BA / 2), w3, size)
        if moving.volume < 1e-9:
            raise FeatureError("Nothing on the moving side of the line (flip?)")
        zone = body & _halfspace(o3 - w3 * (BA / 2), w3, size) & _halfspace(o3 + w3 * (BA / 2), -w3, size)
        us = [(v - o3).dot(u3) for s_ in zone.solids() for v in [Vector(q.X, q.Y, q.Z) for q in s_.vertices()]]
        if not us:
            raise FeatureError("The line doesn't cross the plate")
        umin, umax = min(us), max(us)
        # bend axis and the material's start direction from it
        if p["down"]:
            axis_pt = o3 - w3 * (BA / 2) - n3 * R
            radial = n3
        else:
            axis_pt = o3 - w3 * (BA / 2) + n3 * (t + R)
            radial = -n3
        sgn = 1.0 if (u3.cross(radial)).dot(w3) > 0 else -1.0
        from build123d import Axis, revolve
        c0 = axis_pt + u3 * umin
        rect = Face(Wire.make_polygon([c0 + radial * R, c0 + radial * (R + t),
                                       c0 + radial * (R + t) + u3 * (umax - umin),
                                       c0 + radial * R + u3 * (umax - umin)], close=True))
        ax = Axis(axis_pt, u3)
        bend = revolve(rect, ax, sgn * p["angle"])
        from build123d import Location
        moved = moving.moved(Location(-w3 * BA)).rotate(ax, sgn * p["angle"])
        out = fixed + bend + moved
        ctx.body = _part(out)
        ctx.tools[self.name] = (bend, "join")
        # flat pattern: the bend line (the plate stays flat there)
        q0 = pl.to_local_coords(o3 + u3 * umin)
        q1 = pl.to_local_coords(o3 + u3 * umax)
        bl = ((q0.X, q0.Y), (q1.X, q1.Y))
        fl.setdefault("bends", []).append({"edge": -1, "angle": float(p["angle"]), "radius": R,
                                          "direction": "down" if p["down"] else "up", "line": bl,
                                          "allowance": BA, "k_factor": K, "sketched": self.name})


# ======================================================= convert to sheet metal ===

class ConvertSheetMetal(Feature):
    TYPE = "convert_sheet_metal"
    LABEL = "Convert to Sheet Metal"
    DESCRIPTION = ("Treat a thin-walled solid (e.g. a shelled box, flat walls of one thickness, sharp corners) as "
                   "sheet metal: picks the outside skin from the fixed face, rips the corners, unfolds every wall "
                   "into a flat pattern with K-factor bend deductions. state=flat shows the flat pattern.")
    PARAMS = [
        Param("fixed_face", "text", "bottom", "Outside face that stays put (bottom, at(x,y,z) …)"),
        Param("thickness", "float", 0.0, "Wall thickness (mm, 0 = measure it)", minimum=0.0),
        Param("bend_radius", "float", 1.0, "Inside bend radius for the flat pattern (mm)", minimum=0.0),
        Param("k_factor", "float", 0.44, "K-factor", minimum=0.0),
        Param("round_bends", "bool", False, "Also round the bend edges on the model (R inside, R + t outside)"),
        Param("state", "choice", "folded", "folded | flat", ["folded", "flat"]),
    ]

    def summary(self):
        p = self.params
        return f"from {p['fixed_face']} · R{p['bend_radius']:g} K{p['k_factor']:g}" + \
            (" · FLAT" if p["state"] == "flat" else "")

    def build(self, ctx):
        from shapely.geometry import Polygon as SPoly
        from shapely.ops import unary_union

        from .selectors import select_face
        p = self.params
        body = ctx.require_body()
        F = select_face(body, p["fixed_face"])
        if F.geom_type.name != "PLANE":
            raise FeatureError("The fixed face must be flat")
        nF = F.normal_at()
        faces = list(body.faces())
        fi = next(i for i, g in enumerate(faces) if g.is_same(F))
        t = float(p["thickness"]) or _measure_thickness(faces, F)
        if t <= 0:
            raise FeatureError("Couldn't measure the wall thickness — enter it")
        R, K = float(p["bend_radius"]), min(max(float(p["k_factor"]), 0.0), 1.0)
        planar = [g.geom_type.name == "PLANE" for g in faces]
        rim = [planar[i] and _is_rim(g, t) for i, g in enumerate(faces)]
        # adjacency on shared straight edges
        edge_of = {}
        for i, g in enumerate(faces):
            if not planar[i] or rim[i]:
                continue
            for e in g.edges():
                if e.geom_type.name != "LINE":
                    continue
                edge_of.setdefault(_ekey(e), []).append((i, e))
        T = {fi: np.eye(4)}
        order, parent_edge = [fi], {}
        q = [fi]
        while q:
            i = q.pop(0)
            for e in faces[i].edges():
                if e.geom_type.name != "LINE":
                    continue
                for j, e2 in edge_of.get(_ekey(e), []):
                    if j == i or j in T:
                        continue
                    ni, nj = _n(faces[i]), _n(faces[j])
                    if abs(float(ni @ nj)) > 0.9999:
                        continue
                    if float(ni @ nj) < -0.0001 and abs(float(ni @ nj) + 1) < 1e-4:
                        continue
                    T[j] = self._unfold(T[i], faces[i], faces[j], e, t, R, K)
                    parent_edge[j] = (i, e)
                    order.append(j)
                    q.append(j)
        # flat polygons in F's plane
        fp = Plane(origin=F.center(), z_dir=nF)
        polys, bends = [], []
        for i in order:
            loops = []
            for w in [faces[i].outer_wire(), *faces[i].inner_wires()]:
                pts = []
                for e in w.edges():
                    n = 1 if e.geom_type.name == "LINE" else 24
                    pts += [e.position_at(k / n) for k in range(n)]
                loops.append([_to2(fp, _xf(T[i], q_)) for q_ in pts])
            try:
                pg = SPoly(loops[0], loops[1:]).buffer(0)
                polys.append(pg)
            except Exception:  # noqa: BLE001
                continue
        for j, (i, e) in parent_edge.items():
            beta = math.acos(max(-1.0, min(1.0, float(_n(faces[i]) @ _n(faces[j])))))
            ossb = (R + t) * math.tan(beta / 2)
            BA = beta * (R + K * t)
            a3, b3 = _xf(T[i], e.position_at(0)), _xf(T[i], e.position_at(1))
            inward = _inward(fp, T[i], faces[i], e)
            off = (ossb - BA / 2)
            a2 = np.array(_to2(fp, a3)) + inward * off
            b2 = np.array(_to2(fp, b3)) + inward * off
            up = float(np.cross(_n(faces[i]), _n(faces[j])) @ _edge_dir(e)) >= 0
            bends.append({"edge": j, "angle": round(math.degrees(beta), 4), "radius": R,
                          "direction": "up" if up else "down", "line": (tuple(a2), tuple(b2)),
                          "allowance": BA, "k_factor": K})
        if not polys:
            raise FeatureError("Nothing to unfold")
        flat = unary_union([pg.buffer(1e-6) for pg in polys]).buffer(-1e-6)
        parts = list(getattr(flat, "geoms", [flat]))
        outline = [{"outer": [tuple(map(float, c)) for c in pg.exterior.coords[:-1]],
                    "holes": [[tuple(map(float, c)) for c in r.coords[:-1]] for r in pg.interiors]}
                   for pg in parts if pg.area > 1e-9]
        bb = flat.bounds
        ctx.flats[self.name] = {"outline": outline, "polygons": [o["outer"] for o in outline], "bends": bends,
                                "thickness": t, "k_factor": K, "bend_radius": R,
                                "size": (round(bb[2] - bb[0], 3), round(bb[3] - bb[1], 3)), "plane": fp,
                                "edges": [], "state": p["state"], "walls": len(order)}
        if p["state"] == "flat":
            from .sheetmetal import flat_pattern_face
            solid = None
            for f_ in flat_pattern_face(ctx.flats[self.name], fp):
                s_ = extrude(f_, t, dir=-nF)
                solid = s_ if solid is None else solid + s_
            ctx.body = _part(solid)
            return
        if p["round_bends"] and parent_edge:
            from build123d import fillet
            try:
                outer = [e for _j, (_i, e) in parent_edge.items()]
                b2 = fillet(outer, R + t)
                ctx.body = _part(b2)
            except Exception:  # noqa: BLE001
                pass                                         # sharp model; the flat pattern has the bends

    @staticmethod
    def _unfold(Tp, fp_, fc, e, t, R, K):
        P = np.array(_v3(e.position_at(0)))
        u = _edge_dir(e)
        npar, nch = _n(fp_), _n(fc)
        # angle that turns the child's normal onto the parent's about the edge
        s_ = float(np.cross(nch, npar) @ u)
        c_ = float(nch @ npar)
        phi = math.atan2(s_, c_)
        Rm = _rotm(u, phi)
        M = np.eye(4)
        M[:3, :3] = Rm
        M[:3, 3] = P - Rm @ P
        # bend deduction: pull the unfolded child toward the parent
        beta = math.acos(max(-1.0, min(1.0, c_)))
        ossb = (R + t) * math.tan(beta / 2)
        BD = 2 * ossb - beta * (R + K * t)
        cc = np.array(_v3(fc.center()))
        cc_r = (M @ np.append(cc, 1))[:3]
        out = cc_r - P - u * float((cc_r - P) @ u)
        out = out / (np.linalg.norm(out) or 1.0)
        S = np.eye(4)
        S[:3, 3] = -out * BD
        return Tp @ S @ M


def _v3(v):
    return [float(v.X), float(v.Y), float(v.Z)]


def _n(face):
    n = face.normal_at()
    return np.array([n.X, n.Y, n.Z])


def _edge_dir(e):
    a, b = e.position_at(0), e.position_at(1)
    d = np.array([b.X - a.X, b.Y - a.Y, b.Z - a.Z])
    return d / (np.linalg.norm(d) or 1.0)


def _ekey(e):
    a, b = e.position_at(0), e.position_at(1)
    k1 = (round(a.X, 4), round(a.Y, 4), round(a.Z, 4))
    k2 = (round(b.X, 4), round(b.Y, 4), round(b.Z, 4))
    return tuple(sorted([k1, k2]))


def _rotm(u, phi):
    K = np.array([[0, -u[2], u[1]], [u[2], 0, -u[0]], [-u[1], u[0], 0]])
    return np.eye(3) + math.sin(phi) * K + (1 - math.cos(phi)) * K @ K


def _xf(T, v):
    p = np.array(_v3(v) if hasattr(v, "X") else v, float)
    return (T @ np.append(p, 1.0))[:3]


def _to2(pl: Plane, p3):
    q = pl.to_local_coords(Vector(*map(float, p3)))
    return (float(q.X), float(q.Y))


def _inward(fp, T, face, e):
    """2D unit vector in the flat, perpendicular to the edge, pointing into `face`."""
    a = np.array(_to2(fp, _xf(T, e.position_at(0))))
    b = np.array(_to2(fp, _xf(T, e.position_at(1))))
    d = (b - a) / (np.linalg.norm(b - a) or 1.0)
    n = np.array([-d[1], d[0]])
    c = np.array(_to2(fp, _xf(T, face.center())))
    return n if float((c - a) @ n) > 0 else -n


def _measure_thickness(faces, F):
    nF = _n(F)
    c = np.array(_v3(F.center()))
    best = None
    for g in faces:
        if g.geom_type.name != "PLANE":
            continue
        ng = _n(g)
        if float(ng @ nF) > -0.9999:
            continue
        d = float((c - np.array(_v3(g.center()))) @ nF)
        if d > 1e-6 and (best is None or d < best):
            best = d
    return best or 0.0


def _is_rim(face, t):
    """A face one thickness wide (a free edge of the sheet): its area over half its perimeter ≈ t."""
    try:
        per = sum(e.length for e in face.edges())
        return per > 0 and face.area / (per / 2) <= t * 1.05
    except Exception:  # noqa: BLE001
        return False


# ================================================================== form tools ===

FORMS = ["dimple", "rib", "louver", "extruded_hole"]


class FormTool(Feature):
    TYPE = "form_tool"
    LABEL = "Forming Tool"
    DESCRIPTION = ("Stamp forms into a sheet-metal face: dimple (round cup), rib (stiffening bead), louver (vent), "
                   "extruded_hole (flanged hole). positions = [[x, y], ...] in the face's sketch coordinates; "
                   "direction 'up' = out of the face.")
    PARAMS = [
        Param("sheet", "feature_ref", "", "Sheet-metal feature (for the thickness; or set thickness)"),
        Param("thickness", "float", 0.0, "Sheet thickness (mm, 0 = the sheet's)", minimum=0.0),
        Param("face", "plane_ref", "top", "Face side (top/bottom/…) or a Plane feature"),
        Param("tool", "choice", "dimple", " | ".join(FORMS), FORMS),
        Param("positions", "json", [[0, 0]], "[[x, y], ...] form centres on the face"),
        Param("size", "float", 20.0, "Diameter (dimple, extruded hole) / length (rib, louver) (mm)", minimum=0.5),
        Param("width", "float", 8.0, "Width (rib, louver) (mm)", minimum=0.2),
        Param("height", "float", 4.0, "Height (mm)", minimum=0.05),
        Param("draft", "float", 30.0, "Wall angle from vertical (dimple / rib, deg)", minimum=0.0),
        Param("angle", "float", 0.0, "Rotation on the face (rib / louver, deg)"),
        Param("down", "bool", False, "Form into the face instead of out of it"),
        Param("punch_id", "text", "", "Punch ID (flat-pattern note)"),
    ]

    def summary(self):
        p = self.params
        return f"{p['tool'].replace('_', ' ')} ×{len(p['positions'])}"

    def build(self, ctx):
        from build123d import Circle, Cone, Cylinder, Location, Polygon, SlotCenterToCenter
        p = self.params
        body = ctx.require_body()
        t = float(p["thickness"])
        if not t and p["sheet"]:
            t = float(_sheet_info(ctx, p["sheet"])["thickness"])
        if t <= 0:
            raise FeatureError("Give the sheet (or the thickness)")
        pl = resolve_plane(p["face"], 0.0, ctx)
        if p["down"]:
            pl = Plane(origin=pl.origin, x_dir=pl.x_dir, z_dir=-pl.z_dir)
        h, D, W = float(p["height"]), float(p["size"]), float(p["width"])
        if h <= t * 0.5 and p["tool"] != "extruded_hole":
            raise FeatureError("Form height should be more than half the thickness")
        dr = math.tan(math.radians(min(p["draft"], 75.0)))
        add, cut = [], []
        tool = p["tool"]
        if tool == "dimple":
            r_top = D / 2
            r_bot = r_top + h * dr
            outer = Pos(0, 0, h / 2) * Cone(r_bot, r_top, h)
            add.append(outer)
            cut.append(Pos(0, 0, h / 2 - t) * Cone(r_bot, r_top, h))
        elif tool == "rib":
            if D <= W:
                raise FeatureError("A rib needs length > width")
            prof = SlotCenterToCenter(D - W, W + 2 * h * dr)
            outer = extrude(prof, h, taper=p["draft"]) if p["draft"] else extrude(prof, h)
            add.append(outer)
            cut.append(Pos(0, 0, -t) * outer)
        elif tool == "louver":
            tri = Polygon((-W / 2, 0), (W / 2, 0), (W / 2, h), align=None)
            outer = extrude(Plane.YZ.offset(-D / 2) * tri, D)
            inner_tri = Polygon((-W / 2, -t), (W / 2 + 0.01, -t), (W / 2 + 0.01, h - t), align=None)
            inner = extrude(Plane.YZ.offset(-(D / 2 - t)) * inner_tri, D - 2 * t)
            add.append(outer)
            cut.append(inner)
        else:                                               # extruded (flanged) hole
            r = D / 2
            cut.append(Pos(0, 0, 0) * Cylinder(r, 4 * (t + h)))
            ring = Pos(0, 0, h / 2 - t) * (Cylinder(r + t, h + t) - Cylinder(r, h + t + 1))
            add.append(ring)
        protos_add, protos_cut = add, cut
        adds, cuts = [], []
        for xy in p["positions"]:
            x, y = float(xy[0]), float(xy[1])
            loc = Location(Plane(origin=pl.from_local_coords((x, y, 0)), x_dir=pl.x_dir, z_dir=pl.z_dir)
                           .rotated((0, 0, float(p["angle"]))))
            adds += [loc * s_ for s_ in protos_add]
            cuts += [loc * s_ for s_ in protos_cut]
        out = body
        for s_ in adds:
            out = out + s_
        for s_ in cuts:
            out = out - s_
        ctx.body = _part(out)
        ctx.tools[self.name] = (Compound(adds) if adds else Compound(cuts), "join")
        if p["sheet"] and p["sheet"] in ctx.flats:
            ctx.flats[p["sheet"]].setdefault("forms", []).append(
                {"tool": tool, "positions": [list(map(float, q)) for q in p["positions"]],
                 "punch_id": p["punch_id"], "feature": self.name})


NEW_TYPES = (SketchedBend, ConvertSheetMetal, FormTool)
