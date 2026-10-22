"""Feature definitions.

Each feature type declares its parameters once (PARAMS). That single spec
drives: the Inspector form in the UI, validation, save/load, and the JSON
schema of the tool Claude calls. Add a feature here and it shows up in all
four places.

Units: millimetres and degrees.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from build123d import (
    Axis, BuildSketch, Circle, Compound, Cylinder, Location, Locations, Mode, Part,
    Plane, Polygon, Pos, Rectangle, SlotCenterToCenter, Vector, chamfer,
    extrude, fillet, mirror, offset, revolve,
)

from .selectors import FACE_SELECTORS, select_edges, select_face

try:                                   # build123d ≥ 0.10 renamed add() → insert()
    from build123d import insert as _place
except ImportError:                    # pragma: no cover
    from build123d import add as _place
from .sketch2d import CONSTRAINT_ARITY, SketchError, arc_mid, curve_edges, profile_faces
from . import sketch3d as s3d
from .sketch3d import Sketch3DError, chain_wires, chains
from .sketch2d import solve as solve_sketch


def _tup(v):
    return (float(v.X), float(v.Y), float(v.Z))


# ---------------------------------------------------------------- params ---

@dataclass
class Param:
    name: str
    kind: str                     # float | int | bool | choice | text | sketch_ref | feature_ref | edges | json
    default: Any
    help: str = ""
    choices: list[str] | None = None
    minimum: float | None = None
    schema: dict | None = None        # explicit JSON schema for complex params
    quantity: str | None = None       # units.Q key ('length', 'angle' …, 'none'); inferred when None

    def json_schema(self) -> dict:
        if self.schema:
            return dict(self.schema, description=self.help)
        t = {"float": "number", "int": "integer", "bool": "boolean"}.get(self.kind, "string")
        s: dict[str, Any] = {"type": t, "description": self.help}
        if self.choices:
            s["enum"] = self.choices
        if self.minimum is not None and t in ("number", "integer"):
            s["minimum"] = self.minimum
        if self.kind == "json":
            s = self._json_kind_schema()
        return s

    def _json_kind_schema(self) -> dict:
        if self.name == "entities":
            return {
                "type": "array", "description": self.help,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["rect", "circle", "polygon", "slot"]},
                        "x": {"type": "number"}, "y": {"type": "number"},
                        "w": {"type": "number"}, "h": {"type": "number"},
                        "r": {"type": "number"}, "angle": {"type": "number"},
                        "x2": {"type": "number"}, "y2": {"type": "number"},
                        "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                        "mode": {"type": "string", "enum": ["add", "subtract"]},
                    },
                    "required": ["type"],
                },
            }
        if self.name == "geometry":
            return {
                "type": "object", "description": self.help,
                "properties": {
                    "points": {"type": "object", "description": "id -> [x, y]. 'O' is the fixed origin.",
                               "additionalProperties": {"type": "array", "items": {"type": "number"}}},
                    "curves": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["line", "circle", "arc"]},
                            "a": {"type": "string"}, "b": {"type": "string"}, "c": {"type": "string"},
                            "r": {"type": "number"}, "construction": {"type": "boolean"},
                        }, "required": ["id", "type"]}},
                    "constraints": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": sorted(CONSTRAINT_ARITY)},
                            "e": {"type": "array", "items": {"type": "string"}},
                            "value": {"type": "number"},
                        }, "required": ["type", "e"]}},
                },
            }
        if self.name == "positions":
            return {"type": "array", "description": self.help,
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}}
        return {"description": self.help}


class FeatureError(RuntimeError):
    pass


def as_part(shape) -> Part:
    """Any solid-ish shape as a Part (Part(solid.wrapped) loses the volume in build123d 0.13)."""
    if isinstance(shape, Part):
        return shape
    sols = list(shape.solids()) if hasattr(shape, "solids") else []
    if not sols:
        raise FeatureError("The operation produced no solid")
    return Part(Compound(sols).wrapped)


# ------------------------------------------------------------- context ---

@dataclass
class BuildContext:
    body: Part | None = None
    sketches: dict[str, tuple[Any, Plane]] = field(default_factory=dict)
    sketch_status: dict[str, dict] = field(default_factory=dict)      # name -> {status, dof}
    sketch_issues: dict[str, str] = field(default_factory=dict)       # name -> why it has no profile
    sketch_display: dict[str, tuple[Any, Plane]] = field(default_factory=dict)
    tools: dict[str, tuple[Any, str]] = field(default_factory=dict)   # feature -> (tool solid, op)
    paths: dict[str, list] = field(default_factory=dict)              # sketch -> [chain Wire] (global)
    cutlists: dict[str, list[dict]] = field(default_factory=dict)     # frame feature -> members
    planes: dict[str, Any] = field(default_factory=dict)              # reference plane feature -> Plane
    axes: dict[str, Any] = field(default_factory=dict)                # reference axis feature -> Axis
    threads: dict[str, dict] = field(default_factory=dict)            # cosmetic threads (drawings, BOM)
    flats: dict[str, dict] = field(default_factory=dict)              # sheet-metal flat patterns
    props: dict[str, str] = field(default_factory=dict)               # document properties ({Name} in sketch text)
    surfaces: dict[str, Any] = field(default_factory=dict)            # surface feature -> Face/Shell (v2.8)

    def require_body(self) -> Part:
        if self.body is None or self.body.volume <= 0:
            raise FeatureError("No solid body yet — create an Extrude or Revolve first")
        return self.body

    def apply(self, tool, op: str, owner: str):
        fast = _local_boolean(self.body, tool, op) if op in ("cut", "join") and self.body is not None else None
        if fast is not None:
            self.body = fast
        elif op == "cut":
            self.body = self.require_body() - tool
        elif op == "intersect":
            self.body = self.require_body() & tool
        else:
            self.body = tool if self.body is None else self.body + tool
        self.tools[owner] = (tool, op)


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    BRepBndLib.Add_s(shape.wrapped if hasattr(shape, "wrapped") else shape, b, False)
    return b


def _local_boolean(body, tool, op):
    """Boolean against only the solids the tool can reach: on a body made of many solids (an imported
    assembly as one part, a weldment) the untouched solids are passed through unchanged — far faster, and
    their meshes / validity checks stay cached. None = use the plain boolean."""
    try:
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from build123d import Solid
        ex = TopExp_Explorer(body.wrapped, TopAbs_SOLID)
        sols = []
        while ex.More():
            sols.append(ex.Current())
            ex.Next()
        if len(sols) < 6:
            return None
        tb = _bbox(tool)
        tb.Enlarge(1e-3)
        near, far = [], []
        for sh in sols:
            bb = _bbox(sh)
            (far if bb.IsOut(tb) else near).append(sh)
        if not far:
            return None
        if not near:
            if op == "cut":
                return body                                   # nothing to cut
            parts = [Solid(sh) for sh in far] + list(tool.solids())
            return Part(Compound(parts).wrapped)
        sub = Part(Compound([Solid(sh) for sh in near]).wrapped)
        res = sub - tool if op == "cut" else sub + tool
        parts = [Solid(sh) for sh in far] + list(res.solids())
        return Part(Compound(parts).wrapped)
    except Exception:  # noqa: BLE001
        return None


# -------------------------------------------------------------- planes ---

BASE_PLANES = ["XY", "XZ", "YZ"]
PLANE_CHOICES = BASE_PLANES + FACE_SELECTORS


def resolve_plane(name: str, offset_mm: float, ctx: BuildContext) -> Plane:
    """Base planes, a reference plane feature, or a planar face of the current body (axis-aligned)."""
    if name in BASE_PLANES:
        return getattr(Plane, name).offset(offset_mm)
    if name in ctx.planes:
        return ctx.planes[name].offset(offset_mm)
    if name not in FACE_SELECTORS:
        raise FeatureError(f"No plane '{name}' (XY/XZ/YZ, a face side, or a Plane feature before this one)")
    body = ctx.require_body()
    c = select_face(body, name).center()
    table = {
        "top":    Plane(origin=(0, 0, c.Z), x_dir=(1, 0, 0), z_dir=(0, 0, 1)),
        "bottom": Plane(origin=(0, 0, c.Z), x_dir=(1, 0, 0), z_dir=(0, 0, -1)),
        "front":  Plane(origin=(0, c.Y, 0), x_dir=(1, 0, 0), z_dir=(0, -1, 0)),
        "back":   Plane(origin=(0, c.Y, 0), x_dir=(-1, 0, 0), z_dir=(0, 1, 0)),
        "right":  Plane(origin=(c.X, 0, 0), x_dir=(0, 1, 0), z_dir=(1, 0, 0)),
        "left":   Plane(origin=(c.X, 0, 0), x_dir=(0, -1, 0), z_dir=(-1, 0, 0)),
    }
    return table[name].offset(offset_mm)


def body_extent(ctx: BuildContext) -> float:
    if ctx.body is None:
        return 1000.0
    return max(ctx.body.bounding_box().diagonal * 2, 10.0)


# ------------------------------------------------------------- features ---

class Feature:
    TYPE = "feature"
    LABEL = "Feature"
    DESCRIPTION = ""
    INTERNAL = False                   # True: no add_* tool for Claude
    HIDDEN: set = set()                # params not shown in the Inspector or to Claude
    PARAMS: list[Param] = []

    def __init__(self, name: str, params: dict | None = None, suppressed: bool = False,
                 layer: str = "Default", obj: str = "Object1"):
        self.name = name
        self.suppressed = suppressed
        self.layer = layer
        self.obj = obj
        self.params = {p.name: p.default for p in self.PARAMS}
        if params:
            self.update(params)

    # -- params
    @classmethod
    def param_spec(cls, key: str) -> Param:
        for p in cls.PARAMS:
            if p.name == key:
                return p
        raise KeyError(f"{cls.LABEL} has no parameter '{key}'")

    def update(self, params: dict):
        for k, v in params.items():
            spec = self.param_spec(k)
            self.params[k] = coerce(spec, v)

    def references(self) -> list[str]:
        out = [self.params[p.name] for p in self.PARAMS
               if p.kind in ("sketch_ref", "feature_ref") and self.params.get(p.name)]
        out += [self.params[p.name] for p in self.PARAMS
                if p.kind in ("plane_ref", "axis_ref") and self.params.get(p.name)
                and self.params[p.name] not in PLANE_CHOICES + ["X", "Y", "Z"]]
        return out

    def summary(self) -> str:
        return ""

    def to_dict(self) -> dict:
        return {"type": self.TYPE, "name": self.name, "suppressed": self.suppressed,
                "layer": self.layer, "obj": self.obj, "params": self.params}

    def public_dict(self) -> dict:
        """to_dict without bulky hidden params (what Claude sees)."""
        d = self.to_dict()
        if self.HIDDEN:
            d["params"] = {k: v for k, v in self.params.items() if k not in self.HIDDEN}
        return d

    # -- evaluation
    def build(self, ctx: BuildContext) -> None:
        raise NotImplementedError


def coerce(spec: Param, v: Any) -> Any:
    try:
        if spec.kind == "float":
            v = float(v)
        elif spec.kind == "int":
            v = int(round(float(v)))
        elif spec.kind == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif spec.kind == "json":
            if isinstance(v, str):
                import json
                v = json.loads(v)
        else:
            v = str(v)
    except (TypeError, ValueError) as e:
        raise FeatureError(f"Bad value for '{spec.name}': {v!r} ({e})") from e
    if spec.kind in ("plane_ref", "axis_ref") and not isinstance(v, str):
        raise FeatureError(f"'{spec.name}' must be a name")
    if spec.choices and v not in spec.choices:
        raise FeatureError(f"'{spec.name}' must be one of {spec.choices}, got {v!r}")
    if spec.minimum is not None and isinstance(v, (int, float)) and v < spec.minimum:
        raise FeatureError(f"'{spec.name}' must be ≥ {spec.minimum}, got {v}")
    return v


class Sketch(Feature):
    TYPE = "sketch"
    LABEL = "Sketch"
    DESCRIPTION = ("2D profile on a plane. Either quick closed shapes ('entities') or constrained "
                   "geometry ('geometry': points, lines/arcs/circles, constraints & dimensions) — "
                   "or both. Coordinates are plane-local mm.")
    PARAMS = [
        Param("plane", "plane_ref", "XY", "XY | XZ | YZ, a face side (top/bottom/front/back/left/right) "
              "or a Plane feature"),
        Param("offset", "float", 0.0, "Offset of the sketch plane along its normal (mm)"),
        Param("entities", "json", [],
              "Shapes: rect{x,y,w,h,angle} centred at x,y; circle{x,y,r}; "
              "polygon{points:[[x,y],...]}; slot{x,y,x2,y2,w}. mode add|subtract."),
        Param("geometry", "json", {},
              "Constrained sketch. Lines/arcs share point ids to connect. Closed loops become the "
              "profile; nested loops become holes. Dimensions drive the shape."),
    ]

    def summary(self):
        n = len(self.params["entities"]) + len((self.params.get("geometry") or {}).get("curves", []))
        st = getattr(self, "_status", None)
        tag = f" · {st}" if st else ""
        return f"{self.params['plane']} · {n} item{'s' if n != 1 else ''}{tag}"

    def build(self, ctx):
        plane = resolve_plane(self.params["plane"], self.params["offset"], ctx)
        ents = self.params["entities"]
        geom = self.params.get("geometry") or {}
        faces, display = [], []
        issue = None
        self._status = None
        if geom.get("curves"):
            try:
                res = solve_sketch(geom, analyze=True)
            except SketchError as e:
                raise FeatureError(str(e)) from e
            if not res.ok:
                bad = [f"#{i} {geom['constraints'][i]['type']}" for i in res.failed]
                raise FeatureError(f"Sketch is {res.status}: conflicting constraints {', '.join(bad)}".strip())
            self._status = "defined" if res.dof == 0 else f"{res.dof} DOF"
            ctx.sketch_status[self.name] = {"status": res.status, "dof": res.dof,
                                            **({"free_points": sorted(res.free_points)} if res.free_points else {})}
            display = [plane.location * e for e in curve_edges(res.geometry)]
            ctx.paths[self.name] = [plane.location * w for w in chain_wires(geom2d_to_3d(res.geometry))]
            try:
                faces = profile_faces(res.geometry, getattr(ctx, "props", None))
            except SketchError as e:
                issue = str(e)          # open sketch: fine to keep, can't be extruded
        if ents or faces:
            with BuildSketch(plane) as sk:
                for f in faces:
                    _place(f)
                self._entities(ents)
            if sk.sketch is not None and sk.sketch.area > 0:
                ctx.sketches[self.name] = (sk.sketch, plane)
                if ents:
                    display += list(sk.sketch.edges())
            else:
                issue = issue or "Sketch profile has no area"
        elif not issue:
            issue = "Sketch has no closed profile yet"
        if issue:
            ctx.sketch_issues[self.name] = issue
        if display:
            ctx.sketch_display[self.name] = (Compound(display), plane)

    @staticmethod
    def _entities(ents):
        for e in ents:
            mode = Mode.SUBTRACT if e.get("mode") == "subtract" else Mode.ADD
            t = e.get("type")
            x, y = float(e.get("x", 0)), float(e.get("y", 0))
            if t == "rect":
                with Locations((x, y)):
                    Rectangle(float(e["w"]), float(e["h"]), rotation=float(e.get("angle", 0)), mode=mode)
            elif t == "circle":
                with Locations((x, y)):
                    Circle(float(e["r"]), mode=mode)
            elif t == "polygon":
                pts = [tuple(map(float, p)) for p in e["points"]]
                if len(pts) < 3:
                    raise FeatureError("Polygon needs at least 3 points")
                Polygon(*pts, align=None, mode=mode)
            elif t == "slot":
                x2, y2, w = float(e["x2"]), float(e["y2"]), float(e["w"])
                length = math.hypot(x2 - x, y2 - y)
                ang = math.degrees(math.atan2(y2 - y, x2 - x))
                with Locations(Location(((x + x2) / 2, (y + y2) / 2), ang)):
                    SlotCenterToCenter(length, w, mode=mode)
            else:
                raise FeatureError(f"Unknown entity type '{t}'")


def get_profile(ctx, name):
    if name in ctx.sketches:
        return ctx.sketches[name]
    if name in ctx.sketch_issues:
        raise FeatureError(f"{name}: {ctx.sketch_issues[name]}")
    raise FeatureError(f"Sketch '{name}' not found or failed")


class Extrude(Feature):
    TYPE = "extrude"
    LABEL = "Extrude"
    DESCRIPTION = ("Extrude a sketch. 'join' adds material along the sketch normal; "
                   "'cut' removes material going INTO the part (against the normal) unless flip=true. "
                   "End conditions: blind | through_all | mid_plane | two_directions (depth + depth2) | "
                   "up_to_vertex (target 'x,y,z') | up_to_surface (target face: top/…/at(x,y,z)) | "
                   "offset_from_surface (target face + target_offset) | up_to_next (the next face in the way).")
    ENDS = ["blind", "through_all", "mid_plane", "two_directions", "up_to_vertex", "up_to_surface",
            "offset_from_surface", "up_to_next"]
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Name of the sketch to extrude"),
        Param("operation", "choice", "join", "join | cut | intersect", ["join", "cut", "intersect"]),
        Param("end", "choice", "blind", " | ".join(ENDS), ENDS),
        Param("depth", "float", 10.0, "Depth in mm (blind / mid-plane / direction 1)", minimum=0.001),
        Param("flip", "bool", False, "Reverse direction"),
        Param("draft", "float", 0.0, "Draft angle in degrees (blind only)"),
        Param("depth2", "float", 10.0, "Direction 2 depth (two_directions)", minimum=0.0),
        Param("target", "text", "", "up_to_vertex: x,y,z · up_to_surface / offset_from_surface: a face (top, "
                                   "at(x,y,z), …)"),
        Param("target_offset", "float", 0.0, "Offset from the target surface (mm)"),
    ]

    def summary(self):
        p = self.params
        d = {"through_all": "thru", "up_to_vertex": "to vertex", "up_to_surface": "to surface",
             "offset_from_surface": f"{p['target_offset']:g} mm from surface", "up_to_next": "to next",
             "two_directions": f"{p['depth']:g} + {p['depth2']:g} mm"}.get(p["end"], f"{p['depth']:g} mm")
        return f"{p['operation']} · {d}"

    def build(self, ctx):
        p = self.params
        sk, plane = get_profile(ctx, p["sketch"])
        into = p["operation"] == "cut"
        reverse = into != p["flip"]
        direction = -plane.z_dir if reverse else plane.z_dir
        end = p["end"]
        if end in ("up_to_vertex", "up_to_surface", "offset_from_surface", "up_to_next"):
            tool = self._up_to(ctx, sk, plane, direction, end)
        else:
            depth = body_extent(ctx) if end == "through_all" else p["depth"]
            if end == "mid_plane":
                tool = extrude(sk, depth / 2, both=True)
            elif end == "two_directions":
                tool = extrude(sk, p["depth"], dir=direction)
                if p["depth2"] > 0:
                    tool = tool + extrude(sk, p["depth2"], dir=-direction)
            elif end == "through_all":
                tool = extrude(sk, depth, both=True) if p["operation"] == "cut" else extrude(sk, depth, dir=direction)
            else:
                kw = {"taper": p["draft"]} if p["draft"] else {}
                tool = extrude(sk, depth, dir=direction, **kw)
        ctx.apply(tool, p["operation"], self.name)

    def _up_to(self, ctx, sk, plane, direction, end):
        p = self.params
        if end == "up_to_vertex":
            try:
                xyz = [float(v) for v in re.findall(r"[-+0-9.eE]+", p["target"])][:3]
                v = Vector(*xyz)
            except (TypeError, ValueError) as e:
                raise FeatureError("up_to_vertex: target must be 'x,y,z'") from e
            dist = (v - plane.origin).dot(direction)
            if dist <= 1e-6:
                raise FeatureError("The vertex is behind the sketch in that direction (flip?)")
            return extrude(sk, dist, dir=direction)
        body = ctx.require_body()
        if end == "up_to_next":
            from build123d import Until
            try:
                return extrude(sk, dir=direction, until=Until.NEXT, target=body)
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Nothing in the way to extrude up to ({e})") from e
        if not p["target"]:
            raise FeatureError("Pick the target face (target = top / at(x,y,z) …)")
        from .selectors import select_face
        face = select_face(body, p["target"])
        off = p["target_offset"] if end == "offset_from_surface" else 0.0
        if face.geom_type.name == "PLANE":
            n = face.normal_at()
            fp = face.center()
            if abs(n.dot(direction)) > 1 - 1e-9:                      # parallel: a plain blind depth
                dist = (fp - plane.origin).dot(direction) - off
                if dist <= 1e-6:
                    raise FeatureError("The surface is behind the sketch in that direction (flip?)")
                return extrude(sk, dist, dir=direction)
            cut_pl = Plane(origin=fp - n * off * (1 if n.dot(direction) > 0 else -1), z_dir=n)
            return self._trim_long(ctx, sk, plane, direction, cut_pl)
        if off:
            raise FeatureError("Offset from a curved surface isn't supported — use a planar face")
        return self._trim_long(ctx, sk, plane, direction, face)

    @staticmethod
    def _trim_long(ctx, sk, plane, direction, cutter):
        """Extrude far, split at the target, keep the piece touching the sketch."""
        from build123d import Keep, split
        long = extrude(sk, body_extent(ctx) * 2, dir=direction)
        try:
            pieces = split(long, bisect_by=cutter, keep=Keep.BOTH)
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Couldn't trim at the target surface ({e})") from e
        sols = pieces.solids() if hasattr(pieces, "solids") else [pieces]
        probe_face = sk.faces()[0]
        c = probe_face.center() + direction * 1e-3
        keep = [s_ for s_ in sols if s_.is_inside(c)]
        if not keep:
            raise FeatureError("The target surface doesn't cross the extrusion")
        return as_part(keep[0])


class Revolve(Feature):
    TYPE = "revolve"
    LABEL = "Revolve"
    DESCRIPTION = "Revolve a sketch about a global axis through the origin."
    PARAMS = [
        Param("sketch", "sketch_ref", "", "Name of the sketch to revolve"),
        Param("axis", "axis_ref", "Z", "X | Y | Z or an Axis feature"),
        Param("angle", "float", 360.0, "Sweep angle in degrees", minimum=0.1),
        Param("operation", "choice", "join", "join | cut | intersect", ["join", "cut", "intersect"]),
    ]

    def summary(self):
        return f"{self.params['operation']} · {self.params['angle']:g}° about {self.params['axis']}"

    def build(self, ctx):
        p = self.params
        sk, _ = get_profile(ctx, p["sketch"])
        from .features2 import resolve_axis
        tool = revolve(sk, resolve_axis(p["axis"], ctx), min(p["angle"], 360.0))
        ctx.apply(tool, p["operation"], self.name)


class Fillet(Feature):
    TYPE = "fillet"
    LABEL = "Fillet"
    DESCRIPTION = ("Round edges chosen by an edge selector (e.g. 'vertical', 'top', 'top&circular'). kind: "
                   "constant | variable (radius at the start → radius2 at the end of each edge) | face (between "
                   "faces 'faces' and 'faces2') | full_round (the centre face 'faces' is replaced by a full "
                   "round between its two sides).")
    PARAMS = [
        Param("radius", "float", 2.0, "Radius in mm", minimum=0.001),
        Param("edges", "edges", "vertical", "Edge selector expression"),
        Param("kind", "choice", "constant", "constant | variable | face | full_round",
              ["constant", "variable", "face", "full_round"]),
        Param("radius2", "float", 4.0, "Variable: radius at the end of each edge (mm)", minimum=0.0),
        Param("faces", "text", "", "Face fillet: first face set · full round: the centre face (top, at(x,y,z) …)"),
        Param("faces2", "text", "", "Face fillet: second face set"),
    ]

    def summary(self):
        p = self.params
        k = p.get("kind", "constant")
        if k == "variable":
            return f"R{p['radius']:g}→R{p['radius2']:g} · {p['edges']}"
        if k == "face":
            return f"R{p['radius']:g} · {p['faces']} ↔ {p['faces2']}"
        if k == "full_round":
            return f"full round · {p['faces']}"
        return f"R{p['radius']:g} · {p['edges']}"

    @staticmethod
    def _faces(body, text):
        from .selectors import select_face
        names = [n.strip() for n in re.findall(r"at\([^)]*\)|[^,|]+", text or "") if n.strip()]
        if not names:
            raise FeatureError("Pick faces for the fillet")
        return [select_face(body, n) for n in names]

    def build(self, ctx):
        body = ctx.require_body()
        p = self.params
        k = p.get("kind", "constant")
        if k == "variable":
            from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
            mk = BRepFilletAPI_MakeFillet(body.wrapped)
            for e in select_edges(body, p["edges"]):
                mk.Add(float(p["radius"]), float(max(p["radius2"], 1e-4)), e.wrapped)
            mk.Build()
            if not mk.IsDone():
                raise FeatureError("Variable fillet failed — radii too large for the faces?")
            from build123d import Shape
            ctx.body = as_part(Shape.cast(mk.Shape()))
            return
        if k == "face":
            a, b = self._faces(body, p["faces"]), self._faces(body, p["faces2"])
            edges = [e for fa in a for fb in b for e in fa.edges() if any(e.is_same(g) for g in fb.edges())]
            if not edges:
                raise FeatureError("Those face sets don't share an edge (face fillets here need adjacent faces)")
            ctx.body = fillet(edges, p["radius"])
            return
        if k == "full_round":
            (mid,) = self._faces(body, p["faces"])[:1]
            if mid.geom_type.name != "PLANE":
                raise FeatureError("Full round: pick the flat centre face")
            es = [e for e in mid.edges() if e.geom_type.name == "LINE"]
            if len(es) < 2:
                raise FeatureError("Full round: the centre face needs two long straight edges")
            es.sort(key=lambda e: -e.length)
            e1 = es[0]
            d1 = (e1.position_at(1) - e1.position_at(0)).normalized()
            e2 = next((e for e in es[1:] if abs((e.position_at(1) - e.position_at(0)).normalized().dot(d1)) > 0.999),
                      None)
            if e2 is None:
                raise FeatureError("Full round: the centre face needs two parallel sides")
            w = e2.distance_to(e1.position_at(0.5))
            last = None
            for k_ in (0.4999, 0.499, 0.495, 0.49):
                try:
                    ctx.body = fillet([e1, e2], w * k_)
                    self._full_r = w * k_
                    return
                except Exception as ex:  # noqa: BLE001
                    last = ex
            raise FeatureError(f"Full round failed ({last})")
        ctx.body = fillet(select_edges(body, p["edges"]), p["radius"])


class Chamfer(Feature):
    TYPE = "chamfer"
    LABEL = "Chamfer"
    DESCRIPTION = ("Bevel edges chosen by an edge selector. mode: distance | distance_distance (distance, "
                   "distance2) | distance_angle (distance, angle) | vertex (corners at(x,y,z) in 'vertices', "
                   "cut back 'distance' along each edge).")
    PARAMS = [
        Param("distance", "float", 1.0, "Distance in mm", minimum=0.001),
        Param("edges", "edges", "top", "Edge selector expression"),
        Param("mode", "choice", "distance", "distance | distance_distance | distance_angle | vertex",
              ["distance", "distance_distance", "distance_angle", "vertex"]),
        Param("distance2", "float", 2.0, "Second distance (mm)", minimum=0.001),
        Param("angle", "float", 45.0, "Angle (deg)", minimum=0.1),
        Param("vertices", "text", "", "Vertex chamfer: at(x,y,z)|at(…) corners"),
    ]

    def summary(self):
        p = self.params
        m = p.get("mode", "distance")
        if m == "distance_distance":
            return f"{p['distance']:g} × {p['distance2']:g} mm · {p['edges']}"
        if m == "distance_angle":
            return f"{p['distance']:g} mm × {p['angle']:g}° · {p['edges']}"
        if m == "vertex":
            return f"{p['distance']:g} mm · {p['vertices']}"
        return f"{p['distance']:g} mm · {p['edges']}"

    def build(self, ctx):
        body = ctx.require_body()
        p = self.params
        m = p.get("mode", "distance")
        if m == "vertex":
            ctx.body = _vertex_chamfer(body, p["vertices"], p["distance"])
            return
        edges = select_edges(body, p["edges"])
        if m in ("distance_distance", "distance_angle"):
            # the first distance is measured on a reference face: edges sharing a face go in one call
            kw = {"length2": p["distance2"]} if m == "distance_distance" else {"angle": p["angle"]}
            groups = []
            left = list(edges)
            while left:
                best, members = None, []
                for f in body.faces():
                    mem = [e for e in left if any(e.is_same(g) for g in f.edges())]
                    if len(mem) > len(members):
                        best, members = f, mem
                if not members:
                    raise FeatureError("Chamfer: an edge has no face")
                groups.append((best, members))
                left = [e for e in left if e not in members]
            if len(groups) == 1:
                ctx.body = chamfer(groups[0][1], p["distance"], reference=groups[0][0], **kw)
            else:
                out = body
                for ref, mem in groups:                     # later groups: find their edges again by position
                    cur = []
                    for e in mem:
                        mid = e.position_at(0.5)
                        g = min(out.edges(), key=lambda g_: g_.distance_to(mid))
                        if g.distance_to(mid) < 1e-6:
                            cur.append(g)
                    rf = min(out.faces(), key=lambda f_: f_.distance_to(ref.center()) +
                             (0 if f_.geom_type == ref.geom_type else 1e9))
                    if cur:
                        out = chamfer(cur, p["distance"], reference=rf, **kw)
                ctx.body = out
        else:
            ctx.body = chamfer(edges, p["distance"])


def _vertex_chamfer(body, text, dist):
    """Cut each picked corner off with the plane through the points `dist` along its edges."""
    from .selectors import _AT
    pts = []
    for term in (text or "").split("|"):
        mm = _AT.match(term.strip().lower())
        if mm:
            pts.append(Vector(*map(float, mm.groups())))
    if not pts:
        raise FeatureError("Vertex chamfer: give corners as at(x,y,z)")
    out = body
    for q in pts:
        v = min(body.vertices(), key=lambda v_: (Vector(v_.X, v_.Y, v_.Z) - q).length)
        P = Vector(v.X, v.Y, v.Z)
        es = [e for e in body.edges() if min((e.position_at(0) - P).length, (e.position_at(1) - P).length) < 1e-6]
        if len(es) < 3:
            raise FeatureError("Vertex chamfer: that corner doesn't join three edges")
        ends = []
        for e in es[:3]:
            far = e.position_at(1) if (e.position_at(0) - P).length < 1e-6 else e.position_at(0)
            if (far - P).length <= dist:
                raise FeatureError("Vertex chamfer distance longer than an edge")
            ends.append(P + (far - P).normalized() * dist)
        c = (ends[0] + ends[1] + ends[2]) * (1 / 3)
        apex = P + (P - c) * 0.5                              # past the corner: a clean cut
        out = out - _tetra(apex, *ends)
    return out


def _tetra(a, b, c, d):
    from build123d import Face, Shell, Solid, Wire
    faces = [Face(Wire.make_polygon(list(tri), close=True)) for tri in ((a, b, c), (a, c, d), (a, d, b), (b, d, c))]
    sol = Solid(Shell(faces))
    if sol.volume < 0:
        sol = Solid(sol.wrapped.Reversed())
    return sol


class Shell(Feature):
    TYPE = "shell"
    LABEL = "Shell"
    DESCRIPTION = "Hollow the body to a wall thickness, leaving the listed faces open."
    PARAMS = [
        Param("thickness", "float", 2.0, "Wall thickness in mm", minimum=0.001),
        Param("open_faces", "text", "top", "Faces to remove: top,bottom,front,back,left,right or at(x,y,z) "
                                           "(click faces in the view)"),
        Param("outward", "bool", False, "Add thickness outside instead of inside"),
    ]

    def summary(self):
        return f"{self.params['thickness']:g} mm · open {self.params['open_faces'] or 'none'}"

    def build(self, ctx):
        body = ctx.require_body()
        import re as _re
        names = [n.strip() for n in _re.findall(r"at\([^)]*\)|[^,|]+", self.params["open_faces"] or "")
                 if n.strip()]
        faces = [select_face(body, n) for n in names]
        t = self.params["thickness"]
        try:
            ctx.body = Part(offset(body, amount=t if self.params["outward"] else -t, openings=faces or None).wrapped)
        except Exception as e:  # noqa: BLE001
            raise FeatureError("Shell failed in the kernel. Usually a fillet/chamfer smaller than the wall "
                               "or a small feature near a wall — move Shell earlier in the history "
                               f"(before fillets/holes) or reduce thickness. ({e})") from e


class Hole(Feature):
    TYPE = "hole"
    LABEL = "Hole"
    DESCRIPTION = ("Drill holes into a face. Positions are in the face's sketch-plane coordinates "
                   "(same as sketching on that face). depth=0 means through all.")
    PARAMS = [
        Param("face", "plane_ref", "top", "Face side to drill from (top/bottom/…) or a Plane feature"),
        Param("style", "choice", "simple", "simple | counterbore | countersink",
              ["simple", "counterbore", "countersink"]),
        Param("diameter", "float", 6.6, "Hole diameter in mm", minimum=0.01),
        Param("depth", "float", 0.0, "Depth in mm, 0 = through all", minimum=0.0),
        Param("head_diameter", "float", 11.0, "Counterbore / countersink diameter (mm)", minimum=0.0),
        Param("head_depth", "float", 6.5, "Counterbore depth (mm); countersink uses 90°", minimum=0.0),
        Param("positions", "json", [[0, 0]], "List of [x, y] hole centres on the face"),
        Param("fastener", "text", "", "Hole wizard: fastener size (M3…M24, #4-40…3/4-10) — sets the sizes; "
              "empty = use diameter/head values"),
        Param("fit", "choice", "normal", "Wizard fit: tap (threaded) | close | normal | loose clearance",
              ["tap", "close", "normal", "loose"]),
    ]

    def summary(self):
        p = self.params
        if p.get("fastener"):
            what = f"{p['fastener']} {'tapped' if p['fit'] == 'tap' else p['fit'] + ' clearance'}"
            return f"{what} {p['style'] if p['style'] != 'simple' else ''} ×{len(p['positions'])}".replace("  ", " ")
        return f"Ø{p['diameter']:g} {p['style']} ×{len(p['positions'])}"

    def wizard(self):
        """Diameters from the fastener tables (or the plain values)."""
        p = dict(self.params)
        if p.get("fastener"):
            from . import standards as S
            try:
                f = S.fastener(p["fastener"])
            except KeyError as e:
                raise FeatureError(str(e)) from e
            p["diameter"] = S.hole_size(p["fastener"], p["fit"])
            if p["style"] == "counterbore":
                p["head_diameter"], p["head_depth"] = f.cbore_d, f.cbore_depth
            elif p["style"] == "countersink":
                p["head_diameter"] = f.csk_head + 0.4
        return p

    def build(self, ctx):
        p = self.wizard()
        body = ctx.require_body()
        plane = resolve_plane(p["face"], 0.0, ctx)
        thru = body_extent(ctx)
        depth = p["depth"] or thru
        drill = Plane(origin=plane.origin, x_dir=plane.x_dir, z_dir=-plane.z_dir)
        if not p["positions"]:
            raise FeatureError("No hole positions")
        # one cutter (or cutter set) at the origin, placed by location per hole, and a single boolean cut with
        # all of them — far faster than fusing the cutters one by one
        # cylinder starts slightly above the face so the cut is clean
        proto = [Pos(0, 0, depth / 2 - 0.01) * Cylinder(p["diameter"] / 2, depth + 0.02)]
        if p["style"] == "counterbore" and p["head_diameter"] > p["diameter"]:
            hd = p["head_depth"]
            proto.append(Pos(0, 0, hd / 2 - 0.01) * Cylinder(p["head_diameter"] / 2, hd + 0.02))
        elif p["style"] == "countersink" and p["head_diameter"] > p["diameter"]:
            from build123d import Cone
            h = (p["head_diameter"] - p["diameter"]) / 2   # 90° included
            proto.append(Pos(0, 0, h / 2 - 0.01) * Cone(p["head_diameter"] / 2, p["diameter"] / 2, h + 0.02))
        cutters = []
        for xy in p["positions"]:
            x, y = float(xy[0]), float(xy[1])
            loc = Location(Plane(origin=plane.from_local_coords((x, y, 0)),
                                 x_dir=drill.x_dir, z_dir=drill.z_dir))
            cutters += [loc * c for c in proto]
        from build123d import Compound
        tools = Compound(cutters) if len(cutters) > 1 else cutters[0]
        ctx.body = body.cut(*cutters) if len(cutters) > 1 else body - cutters[0]
        ctx.tools[self.name] = (tools, "cut")
        if p.get("fastener") and p["fit"] == "tap":
            from . import standards as S
            f = S.fastener(p["fastener"])
            for k, xy in enumerate(p["positions"]):
                o = plane.from_local_coords((float(xy[0]), float(xy[1]), 0))
                ctx.threads[f"{self.name}#{k + 1}"] = {
                    "size": p["fastener"] + (f"x{f.pitch:g}" if f.system == "ISO" else " UNC"),
                    "fit": "6H" if f.system == "ISO" else "2B", "radius": p["diameter"] / 2, "major": f.d,
                    "pitch": f.pitch, "depth": p["depth"], "internal": True,
                    "axis": (_tup(o), _tup((-plane.z_dir)))}


class LinearPattern(Feature):
    TYPE = "linear_pattern"
    LABEL = "Linear Pattern"
    DESCRIPTION = "Repeat a previous extrude/revolve/hole along one or two global directions."
    PARAMS = [
        Param("feature", "feature_ref", "", "Feature to repeat (extrude, revolve or hole)"),
        Param("dir1", "choice", "X", "First direction", ["X", "Y", "Z", "-X", "-Y", "-Z"]),
        Param("spacing1", "float", 20.0, "Spacing along dir1 (mm)"),
        Param("count1", "int", 3, "Instances along dir1 incl. original", minimum=1),
        Param("dir2", "choice", "Y", "Second direction", ["X", "Y", "Z", "-X", "-Y", "-Z"]),
        Param("spacing2", "float", 20.0, "Spacing along dir2 (mm)"),
        Param("count2", "int", 1, "Instances along dir2 incl. original (1 = off)", minimum=1),
    ]

    def summary(self):
        p = self.params
        s = f"{p['feature']} · {p['count1']}×{p['dir1']}"
        return s + (f" · {p['count2']}×{p['dir2']}" if p["count2"] > 1 else "")

    @staticmethod
    def _vec(d):
        s = -1 if d.startswith("-") else 1
        return Vector(s * (d[-1] == "X"), s * (d[-1] == "Y"), s * (d[-1] == "Z"))

    def build(self, ctx):
        p = self.params
        if p["feature"] not in ctx.tools:
            raise FeatureError(f"'{p['feature']}' is not a patternable feature or failed")
        tool, op = ctx.tools[p["feature"]]
        v1, v2 = self._vec(p["dir1"]) * p["spacing1"], self._vec(p["dir2"]) * p["spacing2"]
        acc = None
        for i in range(p["count1"]):
            for j in range(p["count2"]):
                if i == 0 and j == 0:
                    continue
                t = Pos(*(v1 * i + v2 * j)) * tool
                acc = t if acc is None else acc + t
        if acc is not None:
            ctx.apply(acc, op, self.name)


class CircularPattern(Feature):
    TYPE = "circular_pattern"
    LABEL = "Circular Pattern"
    DESCRIPTION = "Repeat a previous extrude/revolve/hole around a global axis."
    PARAMS = [
        Param("feature", "feature_ref", "", "Feature to repeat"),
        Param("axis", "axis_ref", "Z", "X | Y | Z (through the origin) or an Axis feature"),
        Param("count", "int", 6, "Total instances incl. original", minimum=2),
        Param("angle", "float", 360.0, "Total angle spread (360 = equal spacing)"),
    ]

    def summary(self):
        p = self.params
        return f"{p['feature']} · {p['count']}× about {p['axis']}"

    def build(self, ctx):
        p = self.params
        if p["feature"] not in ctx.tools:
            raise FeatureError(f"'{p['feature']}' is not a patternable feature or failed")
        tool, op = ctx.tools[p["feature"]]
        n = p["count"]
        step = p["angle"] / n if abs(p["angle"] - 360) < 1e-6 else p["angle"] / max(n - 1, 1)
        from .features2 import resolve_axis
        axis = resolve_axis(p["axis"], ctx)
        acc = None
        for i in range(1, n):
            t = tool.rotate(axis, step * i)
            acc = t if acc is None else acc + t
        ctx.apply(acc, op, self.name)


class Mirror(Feature):
    TYPE = "mirror"
    LABEL = "Mirror"
    DESCRIPTION = "Mirror the whole body about a base plane and join."
    PARAMS = [
        Param("plane", "choice", "YZ", "Mirror plane", BASE_PLANES),
        Param("offset", "float", 0.0, "Plane offset (mm)"),
    ]

    def summary(self):
        return f"about {self.params['plane']}"

    def build(self, ctx):
        body = ctx.require_body()
        pl = getattr(Plane, self.params["plane"]).offset(self.params["offset"])
        ctx.body = body + mirror(body, about=pl)


def geom2d_to_3d(g: dict) -> dict:
    """2D sketch geometry → 3D sketch geometry (z = 0), so chain/path logic is shared."""
    out = {"points": {k: [v[0], v[1], 0.0] for k, v in g["points"].items()}, "curves": [], "constraints": []}
    for c in g["curves"]:
        if c["type"] == "line":
            out["curves"].append(dict(c))
        elif c["type"] == "arc":
            mid = f"_m_{c['id']}"
            mx, my = arc_mid(g, c)
            out["points"][mid] = [mx, my, 0.0]
            out["curves"].append({"id": c["id"], "type": "arc", "a": c["a"], "m": mid, "b": c["b"],
                                  "construction": c.get("construction", False)})
    return out


def _path_frame(wire, rotation_deg=0.0) -> Plane:
    """Plane at the start of a path, normal along its tangent, x-axis level when possible."""
    t = wire.tangent_at(0).normalized()
    up = Vector(0, 0, 1) if abs(t.Z) < 0.99 else Vector(1, 0, 0)
    x = up.cross(t).normalized()
    pl = Plane(origin=wire.position_at(0), x_dir=x, z_dir=t)
    return pl.rotated((0, 0, rotation_deg)) if rotation_deg else pl


def get_paths(ctx, name):
    if name not in ctx.paths or not ctx.paths[name]:
        raise FeatureError(f"'{name}' has no usable path curves")
    return ctx.paths[name]


class Sketch3D(Feature):
    TYPE = "sketch3d"
    LABEL = "3D Sketch"
    DESCRIPTION = ("Lines, splines and 3-point arcs free in XYZ with 3D relations and dimensions. "
                   "Used as sweep paths and frame layouts.")
    PARAMS = [
        Param("geometry", "json", {},
              "points {id:[x,y,z]} ('O' = fixed origin), curves [line(a,b) | spline(pts:[ids]) | "
              "arc(a,m,b through 3 points)], constraints [{type, e, value?}]",
              schema={
                  "type": "object",
                  "properties": {
                      "points": {"type": "object",
                                 "additionalProperties": {"type": "array", "items": {"type": "number"},
                                                          "minItems": 3, "maxItems": 3}},
                      "curves": {"type": "array", "items": {
                          "type": "object",
                          "properties": {
                              "id": {"type": "string"},
                              "type": {"type": "string", "enum": ["line", "spline", "arc"]},
                              "a": {"type": "string"}, "b": {"type": "string"}, "m": {"type": "string"},
                              "pts": {"type": "array", "items": {"type": "string"}},
                              "construction": {"type": "boolean"},
                          }, "required": ["id", "type"]}},
                      "constraints": {"type": "array", "items": {
                          "type": "object",
                          "properties": {
                              "type": {"type": "string", "enum": sorted(s3d.ARITY)},
                              "e": {"type": "array", "items": {"type": "string"}},
                              "value": {"type": "number"},
                          }, "required": ["type", "e"]}},
                  },
              }),
    ]

    def summary(self):
        n = len((self.params.get("geometry") or {}).get("curves", []))
        st = getattr(self, "_status", None)
        return f"{n} curve{'s' if n != 1 else ''}" + (f" · {st}" if st else "")

    def build(self, ctx):
        geom = self.params.get("geometry") or {}
        self._status = None
        if not geom.get("curves"):
            ctx.sketch_issues[self.name] = "3D sketch is empty"
            return
        try:
            res = s3d.solve(geom, analyze=True)
        except Sketch3DError as e:
            raise FeatureError(str(e)) from e
        if not res.ok:
            bad = [f"#{i} {geom['constraints'][i]['type']}" for i in res.failed]
            raise FeatureError(f"3D sketch is {res.status}: conflicting {', '.join(bad)}")
        self._status = "defined" if res.dof == 0 else f"{res.dof} DOF"
        ctx.sketch_status[self.name] = {"status": res.status, "dof": res.dof,
                                        **({"free_points": sorted(res.free_points)} if res.free_points else {}),
                                        **({"redundant": res.redundant} if res.redundant else {})}
        edges = list(s3d.edges_for(res.geometry).values())
        if edges:
            ctx.sketch_display[self.name] = (Compound(edges), Plane.XY)
        ctx.paths[self.name] = chain_wires(res.geometry)
        self._solved = res.geometry


class Sweep(Feature):
    TYPE = "sweep"
    LABEL = "Sweep"
    DESCRIPTION = ("Sweep a closed 2D sketch profile along a path (a 3D sketch or a 2D sketch's curves). "
                   "With align=true the profile's sketch origin is moved to the path start, normal to it.")
    PARAMS = [
        Param("profile", "sketch_ref", "", "2D sketch with the closed profile"),
        Param("path", "sketch_ref", "", "3D sketch or 2D sketch providing the path"),
        Param("chain", "int", 0, "Which connected chain of the path (0 = longest)", minimum=0),
        Param("align", "bool", True, "Move profile to path start, normal to the path"),
        Param("rotation", "float", 0.0, "Profile rotation about the path (deg, when aligned)"),
        Param("transition", "choice", "right", "Corner treatment", ["right", "round", "transformed"]),
        Param("operation", "choice", "join", "join | cut | intersect", ["join", "cut", "intersect"]),
        Param("frenet", "bool", False, "Follow the path's curvature (helices, springs, coils)"),
    ]

    def summary(self):
        return f"{self.params['profile']} along {self.params['path']}"

    def references(self):
        return [r for r in (self.params["profile"], self.params["path"]) if r]

    def build(self, ctx):
        from build123d import Transition, sweep
        p = self.params
        prof, src = get_profile(ctx, p["profile"])
        wires = sorted(get_paths(ctx, p["path"]), key=lambda w: w.length, reverse=True)
        idx = p["chain"]
        if idx >= len(wires):
            raise FeatureError(f"Path has {len(wires)} chain(s); chain {idx} doesn't exist")
        path = wires[idx]
        if p["align"]:
            target = _path_frame(path, p["rotation"])
            prof = target.location * (src.location.inverse() * prof)
        tr = {"right": Transition.RIGHT, "round": Transition.ROUND, "transformed": Transition.TRANSFORMED}
        try:
            tool = sweep(prof, path, transition=tr[p["transition"]], is_frenet=bool(p.get("frenet")))
        except Exception as e:  # noqa: BLE001
            raise FeatureError(f"Sweep failed in the kernel — check the profile is small enough for the "
                               f"path's bends and doesn't self-intersect ({e})") from e
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, p["operation"], self.name)


PROFILES = ["square_tube", "rect_tube", "round_tube", "angle", "channel", "flat_bar", "square_bar", "round_bar"]


def frame_profile(kind: str, w: float, h: float, t: float, r: float = 0.0):
    """Closed profile face centred on its bounding box, in local XY. r: outside corner radius of tubes."""
    from build123d import Circle, Polygon, Rectangle, RectangleRounded
    if kind == "square_tube":
        h = w
    if kind in ("square_tube", "rect_tube"):
        if 2 * t >= min(w, h):
            raise FeatureError("Wall too thick for the tube size")
        if r > 1e-6:
            if r > min(w, h) / 2 - 1e-6:
                raise FeatureError("Corner radius larger than half the tube width")
            ri = r - t
            inner = RectangleRounded(w - 2 * t, h - 2 * t, ri) if ri > 1e-6 else Rectangle(w - 2 * t, h - 2 * t)
            return RectangleRounded(w, h, r) - inner
        return Rectangle(w, h) - Rectangle(w - 2 * t, h - 2 * t)
    if kind == "round_tube":
        if t >= w / 2:
            raise FeatureError("Wall too thick for the tube OD")
        return Circle(w / 2) - Circle(w / 2 - t)
    if kind == "round_bar":
        return Circle(w / 2)
    if kind == "square_bar":
        return Rectangle(w, w)
    if kind == "flat_bar":
        return Rectangle(w, t)
    if kind == "angle":       # legs w (x) and h (y), heel at bottom-left, centred on bbox
        pts = [(0, 0), (w, 0), (w, t), (t, t), (t, h), (0, h)]
        return Polygon(*[(x - w / 2, y - h / 2) for x, y in pts], align=None)
    if kind == "channel":     # web height h (y), flange width w (x), open to +x
        pts = [(0, 0), (w, 0), (w, t), (t, t), (t, h - t), (w, h - t), (w, h), (0, h)]
        return Polygon(*[(x - w / 2, y - h / 2) for x, y in pts], align=None)
    raise FeatureError(f"Unknown profile {kind}")


class Frame(Feature):
    TYPE = "frame"
    LABEL = "Frame"
    DESCRIPTION = ("Structural members (tube, bar, angle, channel) along every path curve of a 3D or 2D "
                   "sketch, centred on the lines. Connected chains get mitred corners; junctions overlap "
                   "and are joined. Produces a cut list.")
    PARAMS = [
        Param("path", "sketch_ref", "", "3D sketch (or 2D sketch) with the frame layout"),
        Param("profile", "choice", "square_tube", "Section type", PROFILES),
        Param("width", "float", 25.4, "Width / OD (mm)", minimum=0.1),
        Param("height", "float", 25.4, "Height (rect tube, angle leg, channel web) (mm)", minimum=0.1),
        Param("thickness", "float", 1.65, "Wall / material thickness (mm)", minimum=0.01),
        Param("rotation", "float", 0.0, "Rotate the section about each member (deg)"),
        Param("corners", "choice", "miter", "miter: chains swept continuously · butt: members cut square",
              ["miter", "butt"]),
        Param("operation", "choice", "join", "join | cut", ["join", "cut"]),
        Param("end_caps", "bool", False, "Close open tube ends with plates"),
        Param("cap_thickness", "float", 3.0, "End cap thickness (mm)", minimum=0.1),
        Param("cope", "bool", True, "Cut list: members ending against another member are cut short to "
              "its face (coped/butted T-joints)"),
        Param("standard", "choice", "custom", "HSS standard: AISC | CSA | EN10219 | EN10210 sizes by designation "
              "(overrides section/size/wall); custom uses the fields above",
              ["custom", "AISC", "CSA", "EN10219", "EN10210"]),
        Param("designation", "text", "", "Standard HSS designation, e.g. HSS4x4x1/4, HSS 102x102x6.4, "
              "SHS 100x100x5, HSS6.625x0.280"),
        Param("corner_radius", "float", 0.0, "Outside corner radius of square/rect tubes (mm, 0 = sharp; "
              "standard sections use their own)", minimum=0.0),
    ]

    def hss(self):
        p = self.params
        if p.get("standard", "custom") != "custom" and p.get("designation"):
            from . import hss as H
            try:
                return H.lookup(p["standard"], p["designation"])
            except KeyError as e:
                raise FeatureError(str(e)) from e
        return None

    def section(self):
        """(kind, w, h, t, r) after resolving a standard designation."""
        p = self.params
        h = self.hss()
        if h is not None:
            kind = {"square": "square_tube", "rect": "rect_tube", "round": "round_tube"}[h.shape]
            return kind, h.B, h.H, h.t, h.ro
        return p["profile"], p["width"], p["height"], p["thickness"], p.get("corner_radius", 0.0) or 0.0

    def kg_per_m(self):
        from . import hss as H
        h = self.hss()
        if h is not None:
            return H.properties(h)["mass_per_m"]
        kind, w, hh, t, r = self.section()
        try:
            return frame_profile(kind, w, hh, t, r).area * 1e-6 * H.STEEL_DENSITY
        except Exception:  # noqa: BLE001
            return None

    def summary(self):
        p = self.params
        if p.get("standard", "custom") != "custom" and p.get("designation"):
            return p["designation"]
        size = f"{p['width']:g}" if p["profile"] in ("square_tube", "round_tube", "round_bar", "square_bar") \
            else f"{p['width']:g}×{p['height']:g}"
        return f"{p['profile'].replace('_', ' ')} {size}×{p['thickness']:g}"

    def build(self, ctx):
        from build123d import Transition, Wire, sweep
        p = self.params
        wires = get_paths(ctx, p["path"])
        kind, sw, sh, st, sr = self.section()
        face = frame_profile(kind, sw, sh, st, sr)
        depth = max(sw, sh)
        members, solids = [], []
        runs = wires if p["corners"] == "miter" else [Wire([e]) for w in wires for e in w.edges()]
        for w in runs:
            pl = _path_frame(w, p["rotation"])
            try:
                solids.append(sweep(pl * face, w, transition=Transition.RIGHT))
            except Exception as e:  # noqa: BLE001
                raise FeatureError(f"Member sweep failed — section may be too big for a bend ({e})") from e
            es = w.edges()
            for i, e in enumerate(es):
                lp = e.length
                if p["corners"] == "miter":   # long-point length at mitred ends
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(es) or (w.is_closed and len(es) > 1):
                            o = es[j % len(es)]
                            ta = e.tangent_at(0 if j < i else 1)
                            tb = o.tangent_at(1 if j < i else 0)
                            dev = math.acos(max(-1, min(1, ta.dot(tb))))
                            lp += depth / 2 * math.tan(dev / 2) if dev > 1e-6 else 0
                members.append({"length": round(e.length, 2), "long_point": round(lp, 2),
                                "straight": e.geom_type.name == "LINE"})
        # T-joints: a run end lying on another run's interior (not at its ends)
        ends_info = []
        for w in runs:
            for t_ in (0.0, 1.0):
                pt = w.position_at(t_)
                hit = False
                for o in runs:
                    if o is w:
                        continue
                    if o.distance_to(pt) < 1e-6 and min((o.position_at(0) - pt).length,
                                                        (o.position_at(1) - pt).length) > 1e-6:
                        hit = True
                ends_info.append((w, t_, pt, hit))
        if p["cope"]:
            k = 0
            for w in runs:
                n_e = len(w.edges())
                for t_ in (0.0, 1.0):
                    info = next(x for x in ends_info if x[0] is w and x[1] == t_)
                    if info[3]:
                        idx = k if t_ == 0.0 else k + n_e - 1
                        members[idx]["length"] = round(members[idx]["length"] - depth / 2, 2)
                        members[idx]["long_point"] = round(members[idx]["long_point"] - depth / 2, 2)
                        members[idx]["coped"] = True
                k += n_e
        if p["end_caps"] and kind in ("square_tube", "rect_tube", "round_tube"):
            from build123d import Face as _F
            f0 = face.faces()[0] if hasattr(face, "faces") else face
            outer = _F(f0.outer_wire())
            connected = {id(w): set() for w in runs}
            for w in runs:
                for t_ in (0.0, 1.0):
                    pt = w.position_at(t_)
                    if any(o is not w and o.distance_to(pt) < 1e-6 for o in runs) or w.is_closed:
                        connected[id(w)].add(t_)
            ncap = 0
            for w in runs:
                for t_ in (0.0, 1.0):
                    if t_ in connected[id(w)]:
                        continue
                    pl = _path_frame(w, p["rotation"]) if t_ == 0.0 else None
                    if pl is None:
                        tan = w.tangent_at(1.0).normalized()
                        up = Vector(0, 0, 1) if abs(tan.Z) < 0.99 else Vector(1, 0, 0)
                        pl = Plane(origin=w.position_at(1.0), x_dir=up.cross(tan).normalized(), z_dir=tan)
                        if p["rotation"]:
                            pl = pl.rotated((0, 0, p["rotation"]))
                        cap = extrude(pl * outer, p["cap_thickness"])
                    else:
                        cap = extrude(pl * outer, -p["cap_thickness"])
                    solids.append(cap)
                    ncap += 1
            if ncap:
                members.append({"length": p["cap_thickness"], "long_point": p["cap_thickness"], "straight": True,
                                "item": "end cap", "qty_caps": ncap})
        tool = solids[0]
        for sld in solids[1:]:
            tool = tool + sld
        ctx.apply(Part(tool.wrapped) if not isinstance(tool, Part) else tool, p["operation"], self.name)
        welds = sum(1 for x in ends_info if x[3])
        kgm = self.kg_per_m()
        ctx.cutlists[self.name] = [dict(m, section=(f"End cap t{p['cap_thickness']:g}" if m.get("item") else
                                                    self.summary()),
                                        kg_per_m=None if m.get("item") else kgm) for m in members
                                   for _ in range(m.get("qty_caps", 1))]
        if ctx.cutlists[self.name]:
            ctx.cutlists[self.name][0]["joints"] = welds


class Imported(Feature):
    """A solid read from a file (STEP/IGES/BREP/mesh/SolidWorks). The geometry is stored in the
    feature itself (compressed BREP), so the .daedalus file stays self-contained. Later features
    (holes, cuts, fillets, sketches on its faces) build on it like any other body."""
    TYPE = "import"
    LABEL = "Imported"
    INTERNAL = True                    # not offered as an add_* tool; created by Import…
    HIDDEN = {"data"}                  # not shown in the Inspector / to Claude
    DESCRIPTION = "Solid imported from a file."
    PARAMS = [
        Param("data", "text", "", "Compressed BREP of the solid"),
        Param("source", "text", "", "Original file name"),
        Param("format", "text", "", "Original format"),
        Param("op", "choice", "join", "Combine with an existing body", ["join", "cut", "intersect"]),
        Param("link", "text", "", "Linked part file (re-read when it changes)"),
        Param("mtime", "float", 0.0, "Linked file time stamp"),
    ]
    HIDDEN = {"data", "mtime"}

    def summary(self) -> str:
        tag = " · linked" if self.params.get("link") else ""
        return f"{self.params['format'].upper()} · {self.params['source']}{tag}"

    def build(self, ctx: BuildContext) -> None:
        if not self.params["data"]:
            raise FeatureError("No imported geometry")
        from ..io.formats import text_to_shape, to_part
        ctx.apply(to_part(text_to_shape(self.params["data"])), self.params["op"], self.name)


FEATURE_TYPES: dict[str, type[Feature]] = {
    c.TYPE: c for c in (Sketch, Sketch3D, Extrude, Revolve, Sweep, Frame, Fillet, Chamfer, Shell, Hole,
                        LinearPattern, CircularPattern, Mirror, Imported)
}

from .features2 import NEW_TYPES  # noqa: E402  (they build on the helpers above)

FEATURE_TYPES.update({c.TYPE: c for c in NEW_TYPES})

from .features3 import NEW_TYPES as _V28_TYPES  # noqa: E402
from .sheetmetal2 import NEW_TYPES as _SM2_TYPES  # noqa: E402

FEATURE_TYPES.update({c.TYPE: c for c in _V28_TYPES + _SM2_TYPES})

from .features_hss import NEW_TYPES as _HSS_TYPES  # noqa: E402

FEATURE_TYPES.update({c.TYPE: c for c in _HSS_TYPES})
