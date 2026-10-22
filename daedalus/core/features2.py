"""More features: reference geometry, loft, rib, draft, cosmetic thread, sheet metal, gusset.

Registered into FEATURE_TYPES by features.py (so the Inspector, file format and Claude tools
pick them up like every other feature).
"""
from __future__ import annotations

import math

import numpy as np
from build123d import (Axis, Compound, Edge, Face, Location, Part, Plane, Polyline, Vector, Wire, extrude,
                       loft)

from .features import (BASE_PLANES, BuildContext, Feature, FeatureError, Param, body_extent, get_paths,
                       get_profile, resolve_plane)

def _tup(v):
    return (float(v.X), float(v.Y), float(v.Z))


SIDES = ["+X", "-X", "+Y", "-Y"]


def _v(p):
    return Vector(float(p[0]), float(p[1]), float(p[2]))


def _plane_outline(pl: Plane, size: float):
    h = size / 2
    pts = [pl.from_local_coords((x, y, 0)) for x, y in ((-h, -h), (h, -h), (h, h), (-h, h), (-h, -h))]
    return Compound([Edge.make_line(a, b) for a, b in zip(pts, pts[1:])])


# ---------------------------------------------------------------- planes ---

class RefPlane(Feature):
    TYPE = "plane"
    LABEL = "Plane"
    DESCRIPTION = ("Reference plane to sketch on or drill from. mode offset: base plane moved along its normal; "
                   "angle: base plane tilted about its own x or y axis; face: the planar face of the body "
                   "nearest 'point'; points: through three points.")
    PARAMS = [
        Param("mode", "choice", "offset", "offset | angle | face | points", ["offset", "angle", "face", "points"]),
        Param("base", "plane_ref", "XY", "Base plane (XY/XZ/YZ, a face side, or another plane)"),
        Param("offset", "float", 10.0, "Offset along the normal (mm)"),
        Param("angle", "float", 45.0, "Tilt angle (deg, mode angle)"),
        Param("about", "choice", "x", "Tilt about the base plane's x or y axis", ["x", "y"]),
        Param("point", "json", [0, 0, 0], "Point on/near the face (mode face) [x, y, z]"),
        Param("points", "json", [[0, 0, 0], [10, 0, 0], [0, 10, 0]], "Three points (mode points)"),
    ]

    def summary(self):
        p = self.params
        if p["mode"] == "offset":
            return f"{p['base']} + {p['offset']:g} mm"
        if p["mode"] == "angle":
            return f"{p['base']} tilted {p['angle']:g}° about {p['about']}"
        return p["mode"]

    def build(self, ctx):
        p = self.params
        m = p["mode"]
        if m in ("offset", "angle"):
            base = resolve_plane(p["base"], 0.0, ctx)
            if m == "angle":
                base = base.rotated((p["angle"], 0, 0) if p["about"] == "x" else (0, p["angle"], 0))
            pl = base.offset(p["offset"]) if m == "offset" else base.offset(p["offset"] if p["offset"] else 0)
        elif m == "face":
            body = ctx.require_body()
            pt = _v(p["point"])
            faces = [f for f in body.faces() if f.geom_type.name == "PLANE"]
            if not faces:
                raise FeatureError("The body has no planar faces")
            f = min(faces, key=lambda f: f.distance_to(pt))
            pl = Plane(f)
            pl = pl.offset(p["offset"]) if p["offset"] else pl
        else:
            pts = [np.asarray(q, float) for q in p["points"]]
            if len(pts) != 3:
                raise FeatureError("Three points needed")
            n = np.cross(pts[1] - pts[0], pts[2] - pts[0])
            if np.linalg.norm(n) < 1e-9:
                raise FeatureError("The three points are in a line")
            pl = Plane(origin=tuple(pts[0]), x_dir=tuple(pts[1] - pts[0]), z_dir=tuple(n))
        ctx.planes[self.name] = pl
        ctx.sketch_display[self.name] = (_plane_outline(pl, body_extent(ctx) / 2.5 if ctx.body else 80), pl)


class RefAxis(Feature):
    TYPE = "axis"
    LABEL = "Axis"
    DESCRIPTION = ("Reference axis for revolves and circular patterns. mode points: through two points; "
                   "cylinder: axis of the hole/shaft face nearest 'point'; planes: where two planes meet.")
    PARAMS = [
        Param("mode", "choice", "cylinder", "points | cylinder | planes", ["points", "cylinder", "planes"]),
        Param("point", "json", [0, 0, 0], "Point near the cylindrical face (mode cylinder)"),
        Param("points", "json", [[0, 0, 0], [0, 0, 10]], "Two points (mode points)"),
        Param("plane1", "plane_ref", "XZ", "First plane (mode planes)"),
        Param("plane2", "plane_ref", "YZ", "Second plane (mode planes)"),
    ]

    def summary(self):
        return self.params["mode"]

    def build(self, ctx):
        p = self.params
        if p["mode"] == "points":
            a, b = (np.asarray(q, float) for q in p["points"])
            if np.linalg.norm(b - a) < 1e-9:
                raise FeatureError("The two points coincide")
            ax = Axis(tuple(a), tuple(b - a))
        elif p["mode"] == "cylinder":
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            body = ctx.require_body()
            pt = _v(p["point"])
            cyl = [f for f in body.faces() if f.geom_type.name in ("CYLINDER", "CONE")]
            if not cyl:
                raise FeatureError("No cylindrical faces on the body")
            f = min(cyl, key=lambda f: f.distance_to(pt))
            s = BRepAdaptor_Surface(f.wrapped)
            g = s.Cylinder().Axis() if f.geom_type.name == "CYLINDER" else s.Cone().Axis()
            ax = Axis((g.Location().X(), g.Location().Y(), g.Location().Z()),
                      (g.Direction().X(), g.Direction().Y(), g.Direction().Z()))
        else:
            p1 = resolve_plane(p["plane1"], 0, ctx)
            p2 = resolve_plane(p["plane2"], 0, ctx)
            n1, n2 = np.array(_tup(p1.z_dir)), np.array(_tup(p2.z_dir))
            d = np.cross(n1, n2)
            if np.linalg.norm(d) < 1e-9:
                raise FeatureError("The planes are parallel")
            A = np.vstack([n1, n2, d])
            b = np.array([n1 @ np.array(_tup(p1.origin)), n2 @ np.array(_tup(p2.origin)), 0.0])
            ax = Axis(tuple(np.linalg.solve(A, b)), tuple(d))
        ctx.axes[self.name] = ax
        L = body_extent(ctx) / 3 if ctx.body else 60
        o, d = ax.position, ax.direction
        ctx.sketch_display[self.name] = (Compound([Edge.make_line(o - d * L, o + d * L)]), Plane.XY)


def resolve_axis(name: str, ctx) -> Axis:
    if name in ("X", "Y", "Z"):
        return getattr(Axis, name)
    if name in ctx.axes:
        return ctx.axes[name]
    raise FeatureError(f"No axis '{name}' (X, Y, Z or an Axis feature)")


# ------------------------------------------------------------------ loft ---

class Loft(Feature):
    TYPE = "loft"
    LABEL = "Loft"
    DESCRIPTION = "Solid blending through two or more closed sketch profiles, in order."
    PARAMS = [
        Param("sections", "text", "", "Sketch names in order, comma-separated (e.g. 'Sketch1, Sketch2')"),
        Param("ruled", "bool", False, "Straight (ruled) faces between sections"),
        Param("operation", "choice", "join", "join | cut | intersect", ["join", "cut", "intersect"]),
    ]

    def names(self):
        return [n.strip() for n in self.params["sections"].split(",") if n.strip()]

    def references(self):
        return self.names()

    def summary(self):
        return " → ".join(self.names())

    def build(self, ctx):
        names = self.names()
        if len(names) < 2:
            raise FeatureError("A loft needs at least two sketches")
        faces = []
        for n in names:
            sk, _ = get_profile(ctx, n)
            fs = sk.faces()
            if len(fs) != 1:
                raise FeatureError(f"{n}: a loft section must be a single closed profile")
            faces.append(fs[0])
        try:
            tool = loft(faces, ruled=self.params["ruled"])
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Loft failed — sections should face the same way and not cross ({e})") from e
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, self.params["operation"], self.name)


# ------------------------------------------------------------------- rib ---

class Rib(Feature):
    TYPE = "rib"
    LABEL = "Rib"
    DESCRIPTION = ("Thin wall from an open sketch line: the area between the line and the body (in the sketch "
                   "plane) is filled and thickened symmetrically about the sketch plane — e.g. a gusset rib in "
                   "an inside corner. flip chooses the other side of the line.")
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Sketch with the open rib line(s)"),
        Param("thickness", "float", 3.0, "Rib thickness (mm)", minimum=0.01),
        Param("flip", "bool", False, "Fill on the other side of the line"),
    ]

    def summary(self):
        return f"{self.params['thickness']:g} mm from {self.params['sketch']}"

    def build(self, ctx):
        p = self.params
        body = ctx.require_body()
        if p["sketch"] not in ctx.sketch_display and p["sketch"] not in ctx.sketches:
            raise FeatureError(f"No sketch '{p['sketch']}'")
        plane = (ctx.sketch_display.get(p["sketch"]) or ctx.sketches[p["sketch"]])[1]
        wires = get_paths(ctx, p["sketch"])
        D = body_extent(ctx)
        big = Face.make_rect(D * 4, D * 4, Plane(origin=plane.origin, x_dir=plane.x_dir, z_dir=plane.z_dir))
        try:
            sect = body & big
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Couldn't cut the body with the sketch plane ({e})") from e
        if not sect.faces():
            raise FeatureError("The sketch plane doesn't pass through the body")
        n = plane.z_dir
        tools = []
        for w in wires:
            a, b = w.position_at(0), w.position_at(1)
            options = []
            for sgn in (1, -1):
                side = n.cross(b - a).normalized() * sgn
                poly = [w.position_at(i / 24) for i in range(25)] + [b + side * D * 2, a + side * D * 2]
                strip = Face(Wire.make_polygon(poly, close=True))
                cut = strip - sect
                pieces = cut.faces() if hasattr(cut, "faces") else []
                near = [f for f in pieces if f.distance_to(w) < 1e-3 * max(1.0, D)]
                # a real rib closes off a pocket: it must not reach the far edge of the strip
                bounded = [f for f in near if f.bounding_box().diagonal < D * 1.5]
                if bounded:
                    options.append((sum(f.area for f in bounded), bounded))
            if not options:
                raise FeatureError("The rib line doesn't close off an area against the body")
            options.sort(key=lambda o: o[0])
            pick = options[-1] if (p["flip"] and len(options) > 1) else options[0]
            for f in pick[1]:
                tools.append(extrude(f, p["thickness"] / 2, dir=n, both=True))
        tool = tools[0]
        for t in tools[1:]:
            tool = tool + t
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, "join", self.name)


# ----------------------------------------------------------------- draft ---

class Draft(Feature):
    TYPE = "draft"
    LABEL = "Draft"
    DESCRIPTION = ("Taper faces for moulding/casting. faces: comma-separated sides (front, back, left, right, "
                   "top, bottom) or 'sides' (every face parallel to the pull direction). The neutral face keeps "
                   "its size; faces taper inward away from it.")
    PARAMS = [
        Param("faces", "text", "sides", "Faces to draft, or 'sides'"),
        Param("angle", "float", 2.0, "Draft angle (deg)", minimum=0.01),
        Param("neutral", "choice", "bottom", "Neutral face", ["bottom", "top", "front", "back", "left", "right"]),
    ]

    def summary(self):
        return f"{self.params['angle']:g}° · {self.params['faces']} from {self.params['neutral']}"

    def build(self, ctx):
        from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
        from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
        from .selectors import select_face
        p = self.params
        body = ctx.require_body()
        nf = select_face(body, p["neutral"])
        nn = nf.normal_at()
        c = nf.center()
        pull = -nn                                     # away from the neutral face into the part
        if p["faces"].strip() == "sides":
            faces = [f for f in body.faces() if f.geom_type.name in ("PLANE", "CYLINDER", "CONE", "BSPLINE")
                     and abs(_normal(f).dot(pull)) < 1e-3]
        else:
            faces = [select_face(body, s.strip()) for s in p["faces"].split(",") if s.strip()]
        if not faces:
            raise FeatureError("No faces to draft")
        dr = BRepOffsetAPI_DraftAngle(body.wrapped)
        pln = gp_Pln(gp_Pnt(c.X, c.Y, c.Z), gp_Dir(nn.X, nn.Y, nn.Z))
        d = gp_Dir(pull.X, pull.Y, pull.Z)
        for f in faces:
            dr.Add(f.wrapped, d, math.radians(p["angle"]), pln)
            if not dr.AddDone():
                raise FeatureError("A face can't take that draft (try fewer faces or a smaller angle)")
        dr.Build()
        if not dr.IsDone():
            raise FeatureError("Draft failed in the kernel")
        from build123d import Shape
        new = Shape.cast(dr.Shape())
        from ..io.formats import to_part
        out = to_part(new)
        if out.volume > body.volume + 1e-6:            # wrong sense: taper the other way
            dr = BRepOffsetAPI_DraftAngle(body.wrapped)
            for f in faces:
                dr.Add(f.wrapped, d, -math.radians(p["angle"]), pln)
            dr.Build()
            out = to_part(Shape.cast(dr.Shape()))
        ctx.body = out


def _normal(f):
    try:
        return f.normal_at()
    except Exception:  # noqa: BLE001
        return Vector(0, 0, 0)


# ---------------------------------------------------------------- thread ---

class Thread(Feature):
    TYPE = "thread"
    LABEL = "Thread"
    DESCRIPTION = ("Cosmetic thread on a hole or shaft (nearest cylindrical face to 'point'): carries the "
                   "spec (e.g. M8x1.25, 1/4-20 UNC) to drawings and the BOM; geometry is unchanged.")
    PARAMS = [
        Param("point", "json", [0, 0, 0], "Point near the hole/shaft face [x, y, z]"),
        Param("size", "text", "M8", "Thread size (M3…M24, #4-40…3/4-10)"),
        Param("depth", "float", 0.0, "Thread depth (mm), 0 = full length", minimum=0.0),
        Param("fit", "text", "6H", "Tolerance class (6H/6g, 2B/2A)"),
        Param("modelled", "bool", False, "Cut the real helical thread form (60° V) into the face"),
        Param("left_hand", "bool", False, "Left-hand thread (modelled)"),
    ]

    def summary(self):
        return f"{self.params['size']} {self.params['fit']}" + (" · modelled" if self.params.get("modelled") else "")

    def build(self, ctx):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from . import standards as S
        p = self.params
        body = ctx.require_body()
        pt = _v(p["point"])
        cyl = [(i, f) for i, f in enumerate(body.faces()) if f.geom_type.name == "CYLINDER"]
        if not cyl:
            raise FeatureError("No cylindrical faces to thread")
        i, f = min(cyl, key=lambda t: t[1].distance_to(pt))
        s = BRepAdaptor_Surface(f.wrapped).Cylinder()
        r = s.Radius()
        ax = s.Axis()
        o = Vector(ax.Location().X(), ax.Location().Y(), ax.Location().Z())
        d = Vector(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        try:
            spec = S.fastener(p["size"].split("x")[0])
            major = spec.d
            pitch = spec.pitch
        except KeyError:
            major, pitch = 2 * r, 0.0
        internal = _normal(f).dot((f.center() - o) - d * (f.center() - o).dot(d)) < 0
        ctx.threads[self.name] = {"size": p["size"], "fit": p["fit"], "face": i, "radius": r, "major": major,
                                  "pitch": pitch, "depth": p["depth"], "internal": bool(internal),
                                  "axis": (_tup(o), _tup(d))}
        c = f.center()
        h = (c - o).dot(d)
        circ = Edge.make_circle(major / 2, Plane(origin=o + d * h, z_dir=d))
        ctx.sketch_display[self.name] = (Compound([circ]), Plane.XY)
        if p.get("modelled"):
            if pitch <= 0:
                raise FeatureError(f"No pitch known for '{p['size']}' — use a standard size for a modelled thread")
            ctx.body = _model_thread(body, f, o, d, r, pitch, float(p["depth"]), bool(internal),
                                     bool(p.get("left_hand")))
            ctx.threads[self.name]["modelled"] = True


def _model_thread(body, face, o, d, r, pitch, depth, internal, left):
    """Cut a 60° V thread along a helix on the cylinder: a groove into a shaft, a ridge out of a hole.
    OCCT booleans with helical sweeps are touchy where they cross the end faces, so a few equivalent set-ups
    are tried and the first that removes the expected amount wins."""
    from build123d import Box, Helix, Location, Solid
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    from OCP.gp import gp_Dir
    d = d.normalized()
    hs = [(Vector(v.X, v.Y, v.Z) - o).dot(d) for v in face.vertices()]
    if not hs:
        raise FeatureError("Can't measure the cylinder for the thread")
    h0, h1 = min(hs), max(hs)
    L = min(depth, h1 - h0) if depth > 0 else h1 - h0
    if L < pitch:
        raise FeatureError("The cylinder is shorter than one thread pitch")
    lo, hi = h1 - L, h1                                   # threaded span along the axis
    t30 = math.tan(math.radians(30))
    if internal:
        h, back = 0.5413 * pitch, 0.08 * pitch
        tri = [(-back, -(h + back) * t30), (-back, (h + back) * t30), (h, 0.0)]
    else:
        h, back = 0.6134 * pitch, 0.08 * pitch
        tri = [(back, -(h + back) * t30), (back, (h + back) * t30), (-h, 0.0)]
    area = abs(sum(tri[i][0] * tri[(i + 1) % 3][1] - tri[(i + 1) % 3][0] * tri[i][1] for i in range(3))) / 2
    inside_frac = (h / (h + back)) ** 2                   # part of the V inside the material
    expect = area * inside_frac * 2 * math.pi * r * L / pitch
    v0 = body.volume

    def groove(sign, ext):
        ax = d * sign
        start = (lo - ext) if sign > 0 else (hi + ext)
        hx = Helix(pitch=pitch, height=L + 2 * ext, radius=r, center=tuple(o + d * start), direction=tuple(ax),
                   lefthand=left)
        p0 = hx.position_at(0)
        rad = (p0 - o - d * (p0 - o).dot(d)).normalized()
        w = Wire.make_polygon([p0 + rad * x + d * y for x, y in tri], close=True)
        mk = BRepOffsetAPI_MakePipeShell(Wire(hx.edges()).wrapped)
        mk.SetMode(gp_Dir(ax.X, ax.Y, ax.Z))
        mk.Add(w.wrapped, False, False)
        mk.Build()
        mk.MakeSolid()
        return Solid(mk.Shape())

    size = body.bounding_box().diagonal * 2 + 10
    clip_pl = Plane(origin=o + d * ((lo + hi) / 2), z_dir=d)
    clip = Location(clip_pl) * Box(size, size, max(L - 0.004, 1e-3))
    tries = [(1, 0.0, False), (-1, 0.0, False), (1, pitch, True), (-1, pitch, True), (1, pitch, False),
             (-1, pitch, False)]
    last = None
    for sign, ext, clipped in tries:
        try:
            g = groove(sign, ext)
            if clipped:
                g = g & clip
            out = body - g
            removed = v0 - out.volume
            if 0.3 * expect < removed < 2.0 * expect and out.volume > 0:
                return out
            last = f"removed {removed:.3g} mm³, expected ≈{expect:.3g}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
    raise FeatureError(f"Modelled thread failed in the kernel ({last}) — use a cosmetic thread here")


# ----------------------------------------------------------- sheet metal ---

from .sheetmetal import GAUGES, SheetMetal, flat_pattern_face  # noqa: E402,F401


# ---------------------------------------------------------------- gusset ---

class Gusset(Feature):
    TYPE = "gusset"
    LABEL = "Gusset"
    DESCRIPTION = ("Triangular plates in the corners of a frame layout (3D/2D sketch): at every corner where "
                   "two straight members meet at less than 170°, or at the listed corner indices.")
    PARAMS = [
        Param("path", "sketch_ref", "", "Sketch with the frame layout"),
        Param("leg", "float", 60.0, "Leg length from the corner (mm)", minimum=1.0),
        Param("thickness", "float", 5.0, "Plate thickness (mm)", minimum=0.1),
        Param("offset", "float", 12.7, "Start the plate this far from the corner (half the member width)",
              minimum=0.0),
        Param("corners", "text", "all", "'all' or comma-separated corner numbers"),
    ]

    def summary(self):
        return f"{self.params['leg']:g} mm × t{self.params['thickness']:g}"

    def build(self, ctx):
        p = self.params
        wires = get_paths(ctx, p["path"])
        # corners: shared endpoints of straight edges
        ends = []
        for w in wires:
            for e in w.edges():
                if e.geom_type.name == "LINE":
                    ends.append((e.position_at(0), e.position_at(1)))
        corners = []
        for i, (a1, b1) in enumerate(ends):
            for j, (a2, b2) in enumerate(ends):
                if j <= i:
                    continue
                for p1, q1 in ((a1, b1), (b1, a1)):
                    for p2, q2 in ((a2, b2), (b2, a2)):
                        if (p1 - p2).length < 1e-6:
                            u1, u2 = (q1 - p1).normalized(), (q2 - p2).normalized()
                            ang = math.degrees(math.acos(max(-1, min(1, u1.dot(u2)))))
                            if 5 < ang < 170:
                                corners.append((p1, u1, u2))
        if p["corners"].strip() != "all":
            idx = [int(s) for s in p["corners"].split(",") if s.strip().isdigit()]
            corners = [c for k, c in enumerate(corners) if k in idx]
        if not corners:
            raise FeatureError("No corners between straight members")
        plates = []
        for P, u1, u2 in corners:
            off = p["offset"]
            A = P + u1 * (off + p["leg"]) + u2 * off
            B = P + u2 * (off + p["leg"]) + u1 * off
            Cc = P + (u1 + u2) * off
            n = u1.cross(u2).normalized()
            f = Face(Wire.make_polygon([Cc, A, B], close=True))
            plates.append(extrude(f, p["thickness"] / 2, dir=n, both=True))
        tool = plates[0]
        for s in plates[1:]:
            tool = tool + s
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, "join", self.name)
        ctx.cutlists[self.name] = [{"section": f"Gusset plate t{p['thickness']:g}, legs {p['leg']:g}",
                                    "length": round(p["leg"], 2), "long_point": round(p["leg"], 2), "straight": True,
                                    "weld_mm": round(2 * p["leg"], 1)} for _ in corners]


NEW_TYPES = (RefPlane, RefAxis, Loft, Rib, Draft, Thread, SheetMetal, Gusset)
