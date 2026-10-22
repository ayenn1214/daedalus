"""v2.8 features: curves (helix / spiral, curve through XYZ points, projected and composite curves), coil springs,
and surfaces (extrude / revolve / loft / planar / offset / fill / knit / thicken / delete face).

Curves land in ctx.paths (usable as sweep paths and frame layouts) and are shown like sketches. Surfaces land in
ctx.surfaces (object coordinates) and are drawn translucent; Knit can close them into a solid and Thicken turns a
surface into one.
"""
from __future__ import annotations

import math
import re

from build123d import Axis, Compound, Edge, Face, Part, Plane, Vector, Wire

from .features import (BASE_PLANES, Feature, FeatureError, Param, _path_frame, get_paths, get_profile,
                       resolve_plane)
from .selectors import select_face

OPS = ["new", "join", "cut", "intersect"]


def _wires(ctx, name) -> list:
    """Path curves of a sketch / curve feature — or, for a quick-shape sketch, its profile outlines."""
    if ctx.paths.get(name):
        return ctx.paths[name]
    if name in ctx.sketches:
        sk, _pl = ctx.sketches[name]
        out = []
        for f in sk.faces():
            out += [f.outer_wire(), *f.inner_wires()]
        if out:
            return out
    return get_paths(ctx, name)


def _names(s) -> list[str]:
    return [x.strip() for x in re.split(r"[,;|]", s or "") if x.strip()]


def _show(ctx, name, shapes):
    try:
        ctx.sketch_display[name] = (Compound(list(shapes)), Plane.XY)
    except Exception:  # noqa: BLE001
        pass


def _as_part(shape):
    from .features import as_part
    return as_part(shape)


def _apply_solid(ctx, solid, op, owner):
    if op == "new":
        op = "join"
    ctx.apply(_as_part(solid), op, owner)


def _axis_dir(plane: Plane):
    return plane.z_dir


# =================================================================== curves ===

class HelixCurve(Feature):
    TYPE = "helix"
    LABEL = "Helix"
    DESCRIPTION = ("Helix / spiral curve about the normal of a plane, starting at a point on it. Defined by two of "
                   "pitch, height and revolutions (mode). taper > 0 widens it (conical). Used as a sweep path "
                   "(springs, threads, coils).")
    PARAMS = [
        Param("plane", "plane_ref", "XY", "Base plane (axis = its normal)"),
        Param("offset", "float", 0.0, "Plane offset (mm)"),
        Param("center", "json", [0, 0], "Axis position on the plane [x, y]"),
        Param("mode", "choice", "pitch_revolutions", "Which two values define it",
              ["pitch_revolutions", "height_revolutions", "height_pitch"]),
        Param("diameter", "float", 20.0, "Start diameter (mm)", minimum=0.01),
        Param("pitch", "float", 5.0, "Rise per turn (mm)", minimum=0.001),
        Param("revolutions", "float", 4.0, "Number of turns", minimum=0.01),
        Param("height", "float", 20.0, "Total height (mm)", minimum=0.001),
        Param("taper", "float", 0.0, "Taper angle (deg, + = widening)"),
        Param("start_angle", "float", 0.0, "Start angle on the plane (deg from its x axis)"),
        Param("clockwise", "bool", False, "Left-hand (clockwise looking down the axis)"),
        Param("reverse", "bool", False, "Run below the plane"),
    ]

    def summary(self):
        p = self.params
        return f"Ø{p['diameter']:g} · P{self._pitch():g} · {self._height() / self._pitch():g} turns"

    def _pitch(self):
        p = self.params
        if p["mode"] == "height_revolutions":
            return p["height"] / max(p["revolutions"], 1e-9)
        return p["pitch"]

    def _height(self):
        p = self.params
        if p["mode"] == "pitch_revolutions":
            return p["pitch"] * p["revolutions"]
        return p["height"]

    def wire(self, ctx) -> Wire:
        from build123d import Helix
        p = self.params
        pl = resolve_plane(p["plane"], p["offset"], ctx)
        cx, cy = (list(p["center"]) + [0, 0])[:2]
        o = pl.from_local_coords((float(cx), float(cy), 0))
        d = pl.z_dir * (-1 if p["reverse"] else 1)
        pitch, height = self._pitch(), self._height()
        if height / pitch > 500:
            raise FeatureError("At most 500 turns")
        h = Helix(pitch=pitch, height=height, radius=p["diameter"] / 2, center=(0, 0, 0), direction=(0, 0, 1),
                  cone_angle=float(p["taper"]), lefthand=bool(p["clockwise"]))
        local = Plane(origin=o, x_dir=pl.x_dir, z_dir=d)
        if p["start_angle"]:
            local = local.rotated((0, 0, float(p["start_angle"])))
        return Wire([local.location * h]) if not isinstance(h, Wire) else local.location * h

    def build(self, ctx):
        w = self.wire(ctx)
        ctx.paths[self.name] = [w]
        _show(ctx, self.name, w.edges())


class SpiralCurve(Feature):
    TYPE = "spiral"
    LABEL = "Spiral"
    DESCRIPTION = "Flat (Archimedean) spiral on a plane: start diameter, growth per turn, number of turns."
    PARAMS = [
        Param("plane", "plane_ref", "XY", "Plane"),
        Param("offset", "float", 0.0, "Plane offset (mm)"),
        Param("center", "json", [0, 0], "Centre [x, y]"),
        Param("diameter", "float", 10.0, "Start diameter (mm)", minimum=0.0),
        Param("pitch", "float", 5.0, "Radius growth per turn (mm)", minimum=0.001),
        Param("revolutions", "float", 3.0, "Turns", minimum=0.05),
        Param("start_angle", "float", 0.0, "Start angle (deg)"),
        Param("clockwise", "bool", False, "Clockwise"),
    ]

    def summary(self):
        return f"{self.params['revolutions']:g} turns · {self.params['pitch']:g} mm/turn"

    def build(self, ctx):
        p = self.params
        pl = resolve_plane(p["plane"], p["offset"], ctx)
        cx, cy = (list(p["center"]) + [0, 0])[:2]
        n = max(24, int(p["revolutions"] * 48))
        s = -1 if p["clockwise"] else 1
        pts = []
        for i in range(n + 1):
            t = p["revolutions"] * i / n
            r = p["diameter"] / 2 + p["pitch"] * t
            a = math.radians(p["start_angle"]) + s * 2 * math.pi * t
            pts.append(pl.from_local_coords((float(cx) + r * math.cos(a), float(cy) + r * math.sin(a), 0)))
        w = Wire([Edge.make_spline(pts)])
        ctx.paths[self.name] = [w]
        _show(ctx, self.name, w.edges())


class CurveXYZ(Feature):
    TYPE = "curve_xyz"
    LABEL = "Curve XYZ"
    DESCRIPTION = "Curve through XYZ points (smooth spline, or straight segments with polyline=true)."
    PARAMS = [
        Param("points", "json", [[0, 0, 0], [20, 10, 5], [40, 0, 10]], "[[x, y, z], ...] (≥ 2)",
              schema={"type": "array", "items": {"type": "array", "items": {"type": "number"},
                                                 "minItems": 3, "maxItems": 3}}),
        Param("closed", "bool", False, "Close the curve"),
        Param("polyline", "bool", False, "Straight segments instead of a spline"),
    ]

    def summary(self):
        return f"{len(self.params['points'])} points" + (" · closed" if self.params["closed"] else "")

    def build(self, ctx):
        p = self.params
        pts = [Vector(*map(float, (list(q) + [0, 0, 0])[:3])) for q in p["points"]]
        if len(pts) < 2:
            raise FeatureError("A curve needs at least 2 points")
        if p["polyline"] or len(pts) == 2:
            w = Wire.make_polygon(pts, close=bool(p["closed"]))
        else:
            w = Wire([Edge.make_spline(pts + ([pts[0]] if p["closed"] else []), periodic=False)])
        ctx.paths[self.name] = [w]
        _show(ctx, self.name, w.edges())


class ProjectedCurve(Feature):
    TYPE = "projected_curve"
    LABEL = "Projected Curve"
    DESCRIPTION = "Project a sketch's curves onto the body along the sketch normal (curves on curved faces)."
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Sketch whose curves are projected"),
        Param("reverse", "bool", False, "Project the other way"),
    ]

    def summary(self):
        return f"{self.params['sketch']} onto the body"

    def build(self, ctx):
        body = ctx.require_body()
        wires = _wires(ctx, self.params["sketch"])
        sk = ctx.sketches.get(self.params["sketch"]) or ctx.sketch_display.get(self.params["sketch"])
        if sk is None:
            raise FeatureError("Sketch not found")
        d = sk[1].z_dir * (1 if self.params["reverse"] else -1)
        out = []
        for w in wires:
            try:
                got = w.project_to_shape(body, d)
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Projection failed ({e})") from e
            out += [g for g in got if g is not None]
        if not out:
            raise FeatureError("The curves miss the body in that direction")
        ctx.paths[self.name] = out
        _show(ctx, self.name, [e for w in out for e in w.edges()])


class CompositeCurve(Feature):
    TYPE = "composite_curve"
    LABEL = "Composite Curve"
    DESCRIPTION = "Join the paths of several sketches / curves (end to end) into one path."
    PARAMS = [Param("curves", "text", "", "Comma-separated sketch / curve feature names")]

    def summary(self):
        return self.params["curves"]

    def references(self):
        return _names(self.params["curves"])

    def build(self, ctx):
        edges = [e for n in _names(self.params["curves"]) for w in _wires(ctx, n) for e in w.edges()]
        if not edges:
            raise FeatureError("No curves to join")
        try:
            ws = Wire.combine(edges)
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Curves don't join end to end ({e})") from e
        ctx.paths[self.name] = list(ws)
        _show(ctx, self.name, edges)


class Spring(Feature):
    TYPE = "spring"
    LABEL = "Spring"
    DESCRIPTION = ("Helical compression / extension spring: wire swept along a helix, optional closed (and ground) "
                   "ends.")
    PARAMS = [
        Param("plane", "plane_ref", "XY", "Base plane (spring axis = its normal)"),
        Param("center", "json", [0, 0], "Axis position [x, y]"),
        Param("od", "float", 20.0, "Outside diameter (mm)", minimum=0.1),
        Param("wire", "float", 2.0, "Wire diameter (mm)", minimum=0.05),
        Param("pitch", "float", 6.0, "Pitch of the active coils (mm)", minimum=0.01),
        Param("active_coils", "float", 5.0, "Active coils", minimum=0.25),
        Param("closed_ends", "bool", True, "One closed coil (pitch = wire) at each end"),
        Param("ground", "bool", True, "Grind the ends flat"),
        Param("clockwise", "bool", False, "Left-hand wind"),
        Param("operation", "choice", "new", "new | join | cut", ["new", "join", "cut"]),
    ]

    def summary(self):
        p = self.params
        return f"OD{p['od']:g} · wire Ø{p['wire']:g} · {p['active_coils']:g} active"

    def build(self, ctx):
        from build123d import Box, Pos
        p = self.params
        if p["wire"] >= p["od"] / 2:
            raise FeatureError("Wire too thick for that diameter")
        if p["pitch"] <= p["wire"] * 1.001:
            raise FeatureError("Pitch must be larger than the wire diameter (coils would touch)")
        r = (p["od"] - p["wire"]) / 2
        pl = resolve_plane(p["plane"], 0.0, ctx)
        cx, cy = (list(p["center"]) + [0, 0])[:2]
        o = pl.from_local_coords((float(cx), float(cy), 0))
        sgn = -1.0 if p["clockwise"] else 1.0
        wd = p["wire"]
        n_end = 1.0 if p["closed_ends"] else 0.0
        turns = p["active_coils"] + 2 * n_end
        close_p = wd * 1.05

        def pitch_at(tn):                                    # pitch of the coil at turn tn (smooth ramps)
            if not n_end:
                return p["pitch"]
            ramp = 0.35
            a_ = min(max((tn - (n_end - ramp)) / (2 * ramp), 0.0), 1.0)
            b_ = min(max(((turns - n_end + ramp) - tn) / (2 * ramp), 0.0), 1.0)
            k = min(a_, b_)
            return close_p + (p["pitch"] - close_p) * k

        per = 36
        npts = int(turns * per) + 1
        pts, z, prev = [], wd / 2, 0.0
        for i in range(npts):
            tn = turns * i / (npts - 1)
            z += pitch_at((tn + prev) / 2) * (tn - prev)
            prev = tn
            a = sgn * 2 * math.pi * tn
            pts.append(Vector(r * math.cos(a), r * math.sin(a), z))
        path = Wire([Edge.make_spline(pts)])
        from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
        from OCP.gp import gp_Dir

        from build123d import Solid
        p0, t0 = path.position_at(0), path.tangent_at(0)
        circ = Wire([Edge.make_circle(wd / 2, Plane(origin=p0, x_dir=Vector(p0.X, p0.Y, 0).normalized(),
                                                    z_dir=t0))])
        mk = BRepOffsetAPI_MakePipeShell(path.wrapped)
        mk.SetMode(gp_Dir(0, 0, 1))
        mk.Add(circ.wrapped, False, False)
        mk.Build()
        if not mk.IsDone():
            raise FeatureError("Spring sweep failed")
        mk.MakeSolid()
        sp = Solid(mk.Shape())
        if sp.volume < 0:
            sp = Solid(sp.wrapped.Reversed())
        total = pts[-1].Z + wd / 2
        if p["ground"] and n_end:
            keep = Pos(0, 0, total / 2) * Box(p["od"] * 2, p["od"] * 2, max(total - wd * 0.6, 0.1))
            try:
                cut = sp & keep
                if cut.volume > sp.volume * 0.5:
                    sp = cut
            except Exception:  # noqa: BLE001
                pass
        local = Plane(origin=o, x_dir=pl.x_dir, z_dir=pl.z_dir)
        sp = local.location * sp
        _apply_solid(ctx, sp, p["operation"], self.name)


# ================================================================= surfaces ===

def _surf(ctx, name):
    if name in ctx.surfaces:
        return ctx.surfaces[name]
    raise FeatureError(f"'{name}' isn't a surface (or it failed)")


def _register(ctx, name, shape):
    ctx.surfaces[name] = shape
    try:
        _show(ctx, name, shape.edges())
    except Exception:  # noqa: BLE001
        pass


def _faces_of(shape):
    return list(shape.faces()) if hasattr(shape, "faces") else [shape]


class SurfaceExtrude(Feature):
    TYPE = "surface_extrude"
    LABEL = "Extruded Surface"
    DESCRIPTION = "Surface from a sketch's curves (open or closed) extruded along the sketch normal."
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Sketch with the curves"),
        Param("depth", "float", 20.0, "Depth (mm)", minimum=0.001),
        Param("both", "bool", False, "Both directions (mid-plane)"),
        Param("reverse", "bool", False, "Reverse"),
    ]

    def summary(self):
        return f"{self.params['sketch']} · {self.params['depth']:g} mm"

    def build(self, ctx):
        from build123d import Location
        p = self.params
        wires = _wires(ctx, p["sketch"])
        pl = (ctx.sketches.get(p["sketch"]) or ctx.sketch_display.get(p["sketch"]) or (None, Plane.XY))[1]
        d = pl.z_dir * (-1 if p["reverse"] else 1)
        faces = []
        for w in wires:
            for e in w.edges():
                if p["both"]:
                    e = e.moved(Location(-d * (p["depth"] / 2)))
                faces.append(Face.extrude(e, d * p["depth"]))
        if not faces:
            raise FeatureError("Nothing to extrude")
        _register(ctx, self.name, _sew(faces))


class SurfaceRevolve(Feature):
    TYPE = "surface_revolve"
    LABEL = "Revolved Surface"
    DESCRIPTION = "Surface from a sketch's curves revolved about an axis."
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Sketch with the curves"),
        Param("axis", "axis_ref", "Z", "X | Y | Z or an Axis feature"),
        Param("angle", "float", 360.0, "Angle (deg)", minimum=0.1),
    ]

    def summary(self):
        return f"{self.params['sketch']} · {self.params['angle']:g}°"

    def build(self, ctx):
        from .features2 import resolve_axis
        p = self.params
        ax = resolve_axis(p["axis"], ctx)
        faces = []
        for w in _wires(ctx, p["sketch"]):
            for e in w.edges():
                try:
                    faces.append(Face.revolve(e, min(p["angle"], 360.0), ax))
                except Exception:  # noqa: BLE001 — an edge lying on the axis gives no face
                    continue
        if not faces:
            raise FeatureError("Nothing to revolve")
        _register(ctx, self.name, _sew(faces))


class SurfaceLoft(Feature):
    TYPE = "surface_loft"
    LABEL = "Lofted Surface"
    DESCRIPTION = "Surface through the curves of two or more sketches / curve features, in order."
    PARAMS = [
        Param("sections", "text", "", "Comma-separated sketches / curves, in order"),
        Param("ruled", "bool", False, "Straight between sections"),
    ]

    def summary(self):
        return self.params["sections"]

    def references(self):
        return _names(self.params["sections"])

    def build(self, ctx):
        from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections

        from build123d import Shape
        names = _names(self.params["sections"])
        if len(names) < 2:
            raise FeatureError("A lofted surface needs at least two sections")
        mk = BRepOffsetAPI_ThruSections(False, bool(self.params["ruled"]))
        for n in names:
            w = max(_wires(ctx, n), key=lambda w_: w_.length)
            mk.AddWire(w.wrapped)
        try:
            mk.Build()
            shp = Shape.cast(mk.Shape())
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Loft failed — sections need matching open/closed curves ({e})") from e
        _register(ctx, self.name, shp)


class PlanarSurface(Feature):
    TYPE = "surface_planar"
    LABEL = "Planar Surface"
    DESCRIPTION = "Flat surface bounded by a closed sketch profile."
    PARAMS = [Param("sketch", "sketch_ref", "", "Closed sketch")]

    def summary(self):
        return self.params["sketch"]

    def build(self, ctx):
        sk, _pl = get_profile(ctx, self.params["sketch"])
        _register(ctx, self.name, _sew(_faces_of(sk)))


class OffsetSurface(Feature):
    TYPE = "surface_offset"
    LABEL = "Offset Surface"
    DESCRIPTION = ("Copy faces of the body (or a surface) offset by a distance (0 = a copy in place). faces: "
                   "top/bottom/…, at(x,y,z) (click faces), 'all'.")
    PARAMS = [
        Param("faces", "text", "top", "Faces: top,bottom,…, at(x,y,z), all"),
        Param("source", "text", "", "Surface to offset (empty = the body)"),
        Param("distance", "float", 5.0, "Offset (mm, − = inward)"),
    ]

    def summary(self):
        return f"{self.params['faces']} · {self.params['distance']:g} mm"

    def build(self, ctx):
        p = self.params
        src = _surf(ctx, p["source"]) if p["source"] else ctx.require_body()
        if (p["faces"] or "").strip().lower() == "all":
            faces = _faces_of(src)
        else:
            names = [n.strip() for n in re.findall(r"at\([^)]*\)|[^,|]+", p["faces"] or "") if n.strip()]
            faces = [select_face(src, n) for n in names]
        if not faces:
            raise FeatureError("Pick faces to offset")
        out = []
        for f in faces:
            if abs(p["distance"]) < 1e-12:
                out.append(f)
                continue
            try:
                out += _faces_of(f.offset(p["distance"])) if f.geom_type.name == "PLANE" else \
                    _faces_of(_offset_face(f, p["distance"]))
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Offset failed (curvature smaller than the distance?) ({e})") from e
        _register(ctx, self.name, _sew(out))


def _offset_face(face, dist):
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape

    from build123d import Shape
    mk = BRepOffsetAPI_MakeOffsetShape()
    mk.PerformBySimple(face.wrapped, dist)
    return Shape.cast(mk.Shape())


class FillSurface(Feature):
    TYPE = "surface_fill"
    LABEL = "Filled Surface"
    DESCRIPTION = ("Patch a closed boundary: a closed sketch / 3D sketch / curve, or body edges picked with "
                   "at(x,y,z) points (a hole in a surface or an opening in a part). Optional points it passes "
                   "through.")
    PARAMS = [
        Param("boundary", "text", "", "Sketch / curve name, or edge selector at(x,y,z)|at(...)"),
        Param("through", "json", [], "Extra points [[x, y, z], ...] the patch passes through"),
        Param("source", "text", "", "Surface whose edges at(...) refers to (empty = the body)"),
    ]

    def summary(self):
        return self.params["boundary"]

    def build(self, ctx):
        from build123d import Vector as V
        p = self.params
        b = (p["boundary"] or "").strip()
        if not b:
            raise FeatureError("Give the boundary (a sketch / curve, or at(x,y,z) edges)")
        if b.lower().startswith("at("):
            from .selectors import select_edges
            src = _surf(ctx, p["source"]) if p["source"] else ctx.require_body()
            edges = select_edges(src, b)
            try:
                wire = Wire.combine(edges)[0]
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Those edges don't form a loop ({e})") from e
        else:
            wire = max(_wires(ctx, b), key=lambda w_: w_.length)
        if not wire.is_closed:
            raise FeatureError("The boundary must be closed")
        pts = [V(*map(float, q)) for q in (p["through"] or [])]
        try:
            f = Face.make_surface(wire, surface_points=pts or None)
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Fill failed ({e})") from e
        _register(ctx, self.name, f)


class Knit(Feature):
    TYPE = "knit"
    LABEL = "Knit Surface"
    DESCRIPTION = ("Sew surfaces together along shared edges. solid=true turns a closed result into a solid "
                   "(added with 'operation').")
    PARAMS = [
        Param("surfaces", "text", "", "Comma-separated surface feature names"),
        Param("tolerance", "float", 0.01, "Gap allowed between edges (mm)", minimum=1e-6),
        Param("solid", "bool", False, "Make a solid if closed"),
        Param("operation", "choice", "new", "new | join | cut | intersect (solid only)", OPS),
    ]

    def summary(self):
        return self.params["surfaces"] + (" → solid" if self.params["solid"] else "")

    def references(self):
        return _names(self.params["surfaces"])

    def build(self, ctx):
        names = _names(self.params["surfaces"])
        if not names:
            raise FeatureError("List the surfaces to knit")
        faces = [f for n in names for f in _faces_of(_surf(ctx, n))]
        sewn = _sew(faces, self.params["tolerance"])
        if self.params["solid"]:
            from build123d import Solid
            shells = sewn.shells() if hasattr(sewn, "shells") else []
            if not shells:
                raise FeatureError("Knit didn't produce a shell")
            sol = Solid(max(shells, key=lambda sh: sh.area))
            try:
                from OCP.ShapeFix import ShapeFix_Solid
                fx = ShapeFix_Solid(sol.wrapped)
                fx.Perform()
                sol = Solid(fx.Solid())
            except Exception:  # noqa: BLE001
                pass
            if sol.volume < 0:
                sol = Solid(sol.wrapped.Reversed())
            if not sol.is_valid or abs(sol.volume) < 1e-9:
                raise FeatureError("The knit surfaces aren't closed — no solid (check for gaps)")
            _apply_solid(ctx, sol, self.params["operation"], self.name)
            return
        _register(ctx, self.name, sewn)


class Thicken(Feature):
    TYPE = "thicken"
    LABEL = "Thicken"
    DESCRIPTION = "Give a surface thickness: a solid (side: one / other / both)."
    PARAMS = [
        Param("surface", "feature_ref", "", "Surface feature"),
        Param("thickness", "float", 2.0, "Thickness (mm)", minimum=0.001),
        Param("side", "choice", "one", "one | other | both", ["one", "other", "both"]),
        Param("operation", "choice", "new", "new | join | cut | intersect", OPS),
    ]

    def summary(self):
        return f"{self.params['surface']} · {self.params['thickness']:g} mm"

    def build(self, ctx):
        from build123d import Solid
        p = self.params
        surf = _surf(ctx, p["surface"])
        t = p["thickness"]
        parts = []
        for f in _faces_of(surf):
            try:
                if p["side"] == "both":
                    parts.append(Solid.thicken(f, t / 2))
                    parts.append(Solid.thicken(f, -t / 2))
                else:
                    parts.append(Solid.thicken(f, t if p["side"] == "one" else -t))
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Thicken failed ({e})") from e
        sol = parts[0]
        for s in parts[1:]:
            sol = sol.fuse(s)
        sol = sol.clean() if hasattr(sol, "clean") else sol
        _apply_solid(ctx, sol, p["operation"], self.name)


class DeleteFace(Feature):
    TYPE = "delete_face"
    LABEL = "Delete Face"
    DESCRIPTION = ("Remove faces from the body: the rest becomes a surface (patch=false), or the gap is healed by "
                   "extending the neighbours (patch=true, e.g. remove a fillet or a boss).")
    PARAMS = [
        Param("faces", "text", "top", "Faces: top,bottom,…, at(x,y,z)"),
        Param("patch", "bool", True, "Heal the body (true) or leave a surface (false)"),
    ]

    def summary(self):
        return f"{self.params['faces']} · {'heal' if self.params['patch'] else 'to surface'}"

    def build(self, ctx):
        body = ctx.require_body()
        names = [n.strip() for n in re.findall(r"at\([^)]*\)|[^,|]+", self.params["faces"] or "") if n.strip()]
        faces = [select_face(body, n) for n in names]
        if not faces:
            raise FeatureError("Pick faces to delete")
        if self.params["patch"]:
            from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing

            from build123d import Shape
            lst = _shape_list()
            for f in faces:
                lst.Append(f.wrapped)
            df = BRepAlgoAPI_Defeaturing()
            df.SetShape(body.wrapped)
            df.AddFacesToRemove(lst)
            df.SetRunParallel(True)
            df.Build()
            if not df.IsDone():
                raise FeatureError("Couldn't heal the body without those faces")
            ctx.body = _as_part(Shape.cast(df.Shape()))
            return
        keep = [f for f in body.faces() if not any(f.is_same(g) for g in faces)]
        _register(ctx, self.name, _sew(keep))
        ctx.body = None


def _shape_list():
    try:
        from OCP.TopTools import TopTools_ListOfShape
        return TopTools_ListOfShape()
    except ImportError:
        from OCP.collections import List_TopoDS_Shape
        return List_TopoDS_Shape()


def _sew(faces, tol=1e-3):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    from build123d import Shape
    if len(faces) == 1:
        return faces[0]
    sw = BRepBuilderAPI_Sewing(tol)
    for f in faces:
        sw.Add(f.wrapped)
    sw.Perform()
    return Shape.cast(sw.SewedShape())


CURVE_TYPES = (HelixCurve, SpiralCurve, CurveXYZ, ProjectedCurve, CompositeCurve, Spring)
SURFACE_TYPES = (SurfaceExtrude, SurfaceRevolve, SurfaceLoft, PlanarSurface, OffsetSurface, FillSurface, Knit,
                 Thicken, DeleteFace)
NEW_TYPES = CURVE_TYPES + SURFACE_TYPES
