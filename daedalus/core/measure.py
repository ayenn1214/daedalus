"""Measure: points, vertices, edges, faces and whole bodies — one entity or several.

Refs are dicts: {"obj": name, "kind": "vertex"|"edge"|"face"|"body"|"point", "index": i (into body.vertices() /
edges() / faces()), "p": [x, y, z] (point kind, world)} — optionally "shape" (a build123d shape in the object's
own coordinates, as the viewport holds it). Results are rows (label, base value, quantity) so they can be shown in
any unit system; points are [x, y, z] world mm.
"""
from __future__ import annotations

import math

import numpy as np

from . import assembly as asm
from . import units as U
from .features import FeatureError

G = 9.80665


def _v(p):
    return np.array([float(p.X), float(p.Y), float(p.Z)]) if hasattr(p, "X") else np.asarray(p, float)


def shape_of(doc, ref):
    """World-space build123d shape for a ref."""
    from build123d import Vertex
    kind = ref.get("kind", "point")
    if kind == "point":
        return Vertex(*map(float, ref["p"]))
    name = ref.get("obj") or doc.active_object
    body = doc.result.bodies.get(name)
    if body is None:
        raise FeatureError(f"{name} has no body")
    if ref.get("shape") is not None:
        s = ref["shape"]
    elif kind == "body":
        s = body
    else:
        pool = {"vertex": body.vertices, "edge": body.edges, "face": body.faces}[kind]()
        i = int(ref.get("index", 0))
        if not 0 <= i < len(pool):
            raise FeatureError(f"{name} has {len(pool)} {kind}s (0 … {len(pool) - 1})")
        s = pool[i]
    o = doc.obj(name) if doc.has_obj(name) else None
    if o is not None and any(o.pose):
        s = s.moved(asm.location_of(o.pose))
    return s


def _kind(s):
    t = type(s).__name__.lower()
    for k in ("vertex", "edge", "wire", "face", "shell", "solid", "compound", "part"):
        if k in t:
            return {"wire": "edge", "shell": "face", "compound": "body", "part": "body", "solid": "body"}.get(k, k)
    return "body"


def _center(s, kind):
    from build123d import CenterOf
    if kind == "vertex":
        return _v(s)
    if kind == "edge":
        if s.geom_type.name == "CIRCLE":
            return _v(s.arc_center)
        return _v(s.center())
    if kind == "face":
        try:
            return _v(s.center(CenterOf.MASS))
        except Exception:  # noqa: BLE001
            return _v(s.center())
    try:
        return _v(s.center(CenterOf.MASS))
    except Exception:  # noqa: BLE001
        return _v(s.bounding_box().center())


def _direction(s, kind):
    """Unit direction of a straight edge / normal of a plane face / axis of a cylinder/circle (or None)."""
    try:
        if kind == "edge":
            gt = s.geom_type.name
            if gt == "LINE":
                d = _v(s.position_at(1)) - _v(s.position_at(0))
                return d / np.linalg.norm(d), "line"
            if gt == "CIRCLE":
                return _v(s.normal()), "axis"
        if kind == "face":
            gt = s.geom_type.name
            if gt == "PLANE":
                return _v(s.normal_at()), "plane"
            if gt in ("CYLINDER", "CONE"):
                ax = s.axis_of_rotation if hasattr(s, "axis_of_rotation") else None
                if ax is not None:
                    return _v(ax.direction), "axis"
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                ad = BRepAdaptor_Surface(s.wrapped)
                surf = ad.Cylinder() if gt == "CYLINDER" else ad.Cone()
                d = surf.Axis().Direction()
                return np.array([d.X(), d.Y(), d.Z()]), "axis"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _surface_info(f):
    """(kind, radius, axis point, axis dir, half-angle) for analytic faces."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Sphere, GeomAbs_Torus
    ad = BRepAdaptor_Surface(f.wrapped)
    t = ad.GetType()

    def P(p):
        return np.array([p.X(), p.Y(), p.Z()])
    if t == GeomAbs_Cylinder:
        c = ad.Cylinder()
        return "cylinder", c.Radius(), P(c.Location()), P(c.Axis().Direction()), None
    if t == GeomAbs_Sphere:
        s = ad.Sphere()
        return "sphere", s.Radius(), P(s.Location()), None, None
    if t == GeomAbs_Cone:
        c = ad.Cone()
        return "cone", c.RefRadius(), P(c.Location()), P(c.Axis().Direction()), math.degrees(c.SemiAngle())
    if t == GeomAbs_Torus:
        tt = ad.Torus()
        return "torus", tt.MinorRadius(), P(tt.Location()), P(tt.Axis().Direction()), tt.MajorRadius()
    return None, None, None, None, None


def _min_dist(a, b):
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    ex = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
    ex.Perform()
    if not ex.IsDone() or ex.NbSolution() < 1:
        return None, None, None
    pa, pb = ex.PointOnShape1(1), ex.PointOnShape2(1)
    return ex.Value(), np.array([pa.X(), pa.Y(), pa.Z()]), np.array([pb.X(), pb.Y(), pb.Z()])


def _samples(s, kind, n=40):
    if kind == "vertex":
        return np.array([_v(s)])
    pts = [_v(v) for v in s.vertices()]
    try:
        for e in s.edges():
            pts += [_v(e.position_at(i / n)) for i in range(n + 1)]
    except Exception:  # noqa: BLE001
        pass
    return np.array(pts) if pts else np.array([_center(s, kind)])


def _angle(d1, d2, unsigned_lines=True):
    c = float(np.clip(abs(d1 @ d2) if unsigned_lines else d1 @ d2, -1, 1))
    return math.degrees(math.acos(c))


def _slope_rows(d, rows, prefix=""):
    horiz = math.hypot(d[0], d[1])
    if horiz < 1e-9 and abs(d[2]) < 1e-9:
        return
    ang = math.degrees(math.atan2(abs(d[2]), horiz))
    rows.append((f"{prefix}Plan (horizontal) length", horiz, "length"))
    rows.append((f"{prefix}Rise (vertical)", float(d[2]), "length"))
    if horiz > 1e-9:
        rows.append((f"{prefix}Slope / pitch", ang, "slope"))
        rows.append((f"{prefix}Slope angle", ang, "angle"))


def measure(doc, refs: list[dict], mode: str = "center") -> dict:
    """Everything measurable about the refs. mode (two entities with circles/arcs): center | min | max."""
    if not refs:
        raise FeatureError("Pick something to measure")
    shapes = [shape_of(doc, r) for r in refs]
    kinds = [("vertex" if r.get("kind") == "point" else r.get("kind") or _kind(s)) for r, s in zip(refs, shapes)]
    rows: list[tuple] = []
    pts: list = []
    if len(shapes) == 1:
        s, k, r = shapes[0], kinds[0], refs[0]
        if k == "vertex":
            p = _v(s)
            rows += [("X", p[0], "length"), ("Y", p[1], "length"), ("Z", p[2], "length")]
            pts.append(p.tolist())
        elif k == "edge":
            gt = s.geom_type.name
            rows.append(("Length", s.length, "length"))
            if gt == "LINE":
                a, b = _v(s.position_at(0)), _v(s.position_at(1))
                d = b - a
                rows += [("ΔX", d[0], "length"), ("ΔY", d[1], "length"), ("ΔZ", d[2], "length")]
                _slope_rows(d, rows)
                pts += [a.tolist(), b.tolist()]
            elif gt == "CIRCLE":
                c = _v(s.arc_center)
                rows += [("Radius", s.radius, "length"), ("Diameter", 2 * s.radius, "length")]
                if not s.is_closed:
                    rows.append(("Arc angle", math.degrees(s.length / s.radius), "angle"))
                    rows.append(("Chord", float(np.linalg.norm(_v(s.position_at(1)) - _v(s.position_at(0)))),
                                 "length"))
                rows += [("Center X", c[0], "length"), ("Center Y", c[1], "length"), ("Center Z", c[2], "length")]
                pts.append(c.tolist())
        elif k == "face":
            rows.append(("Area", s.area, "area"))
            try:
                rows.append(("Perimeter", sum(e.length for e in s.outer_wire().edges()), "length"))
                inner = s.inner_wires()
                if inner:
                    rows.append(("Holes", len(inner), None))
            except Exception:  # noqa: BLE001
                pass
            gt = s.geom_type.name
            if gt == "PLANE":
                n = _v(s.normal_at())
                rows += [("Normal", f"({n[0]:.4f}, {n[1]:.4f}, {n[2]:.4f})", "text")]
                tilt = math.degrees(math.acos(min(1.0, abs(n[2]))))
                rows.append(("Tilt from horizontal", tilt, "angle"))
                if 0.01 < tilt < 89.99:
                    rows.append(("Slope / pitch", tilt, "slope"))
            sk, rad, loc, ax, extra = _surface_info(s)
            if sk in ("cylinder", "sphere"):
                rows += [("Radius", rad, "length"), ("Diameter", 2 * rad, "length")]
            elif sk == "cone":
                rows += [("Cone half-angle", abs(extra), "angle"), ("Reference radius", rad, "length")]
            elif sk == "torus":
                rows += [("Tube radius", rad, "length"), ("Major radius", extra, "length")]
            if sk:
                rows.append(("Surface", sk, "text"))
            pts.append(_center(s, k).tolist())
        else:
            name = r.get("obj") or doc.active_object
            vol = s.volume
            mat = doc.material_of(name)
            mass = vol * mat.density / 1e6
            bb = s.bounding_box()
            com = _center(s, k)
            rows += [("Volume", vol, "volume"), ("Surface area", s.area, "area"), ("Material", mat.name, "text"),
                     ("Density", mat.density, "density"), ("Mass", mass, "mass"), ("Weight", mass * G, "force"),
                     ("Bounding box X", bb.size.X, "length"), ("Bounding box Y", bb.size.Y, "length"),
                     ("Bounding box Z", bb.size.Z, "length"), ("Center of mass X", com[0], "length"),
                     ("Center of mass Y", com[1], "length"), ("Center of mass Z", com[2], "length")]
            if vol > 0:
                rows.append(("Board feet (lumber)", vol, "volume_bf"))
            pts.append(com.tolist())
    elif len(shapes) == 2:
        (a, b), (ka, kb) = shapes, kinds
        ca, cb = _center(a, ka), _center(b, kb)
        dmin, pa, pb = _min_dist(a, b)
        dc = cb - ca
        da, ta = _direction(a, ka)
        db, tb = _direction(b, kb)
        both_points = ka == kb == "vertex"
        if both_points:
            rows += [("Distance", float(np.linalg.norm(dc)), "length"), ("ΔX", dc[0], "length"),
                     ("ΔY", dc[1], "length"), ("ΔZ", dc[2], "length")]
            _slope_rows(dc, rows)
            pts += [ca.tolist(), cb.tolist()]
        else:
            if dmin is not None:
                rows.append(("Minimum distance", dmin, "length"))
                d = pb - pa
                rows += [("ΔX (min)", d[0], "length"), ("ΔY (min)", d[1], "length"), ("ΔZ (min)", d[2], "length")]
            rows.append(("Center distance", float(np.linalg.norm(dc)), "length"))
            rows += [("ΔX (centers)", dc[0], "length"), ("ΔY (centers)", dc[1], "length"),
                     ("ΔZ (centers)", dc[2], "length")]
            Sa, Sb = _samples(a, ka), _samples(b, kb)
            if len(Sa) * len(Sb) <= 4e6:
                D = np.linalg.norm(Sa[:, None, :] - Sb[None, :, :], axis=2)
                rows.append(("Maximum distance", float(D.max()), "length"))
            if ta == "plane" and tb == "plane" and abs(abs(da @ db) - 1) < 1e-6:
                rows.append(("Normal distance (parallel planes)", abs(float((cb - ca) @ da)), "length"))
            elif ta == "plane" and kb == "vertex":
                rows.append(("Normal distance", abs(float((cb - ca) @ da)), "length"))
            elif tb == "plane" and ka == "vertex":
                rows.append(("Normal distance", abs(float((ca - cb) @ db)), "length"))
            if ta == tb == "axis" and abs(abs(da @ db) - 1) < 1e-6:
                w = cb - ca
                rows.append(("Axis-to-axis distance", float(np.linalg.norm(w - (w @ da) * da)), "length"))
            if da is not None and db is not None:
                if ta == "line" and tb == "line":
                    rows.append(("Angle", _angle(da, db), "angle"))
                    rows.append(("Included angle", 180 - _angle(da, db), "angle"))
                elif ta == "plane" and tb == "plane":
                    ang = _angle(da, db, unsigned_lines=False)
                    rows.append(("Angle between faces", 180 - ang, "angle"))
                    rows.append(("Angle between normals", ang, "angle"))
                elif {ta, tb} == {"plane", "line"}:
                    n, ld = (da, db) if ta == "plane" else (db, da)
                    rows.append(("Angle line ↔ face", 90 - _angle(n, ld), "angle"))
                elif "axis" in (ta, tb):
                    rows.append(("Angle between axes / directions", _angle(da, db), "angle"))
            pts += ([pa.tolist(), pb.tolist()] if dmin is not None and mode == "min" else [ca.tolist(), cb.tolist()])
    else:
        tot_len = sum(s.length for s, k in zip(shapes, kinds) if k == "edge")
        tot_area = sum(s.area for s, k in zip(shapes, kinds) if k == "face")
        tot_vol = sum(s.volume for s, k in zip(shapes, kinds) if k == "body")
        rows.append(("Items", len(shapes), None))
        if tot_len:
            rows.append(("Total length", tot_len, "length"))
        if tot_area:
            rows.append(("Total area", tot_area, "area"))
        if tot_vol:
            rows.append(("Total volume", tot_vol, "volume"))
        vs = [_center(s, k) for s, k in zip(shapes, kinds) if k == "vertex"]
        if len(vs) >= 3:
            P = np.array(vs)
            path = float(sum(np.linalg.norm(P[i + 1] - P[i]) for i in range(len(P) - 1)))
            rows.append(("Path length (in pick order)", path, "length"))
            # area of the polygon through the points (projected on their best-fit plane)
            c = P.mean(0)
            _u, _s, vt = np.linalg.svd(P - c)
            Q2 = (P - c) @ vt[:2].T
            area = 0.5 * abs(sum(Q2[i, 0] * Q2[(i + 1) % len(Q2), 1] - Q2[(i + 1) % len(Q2), 0] * Q2[i, 1]
                                 for i in range(len(Q2))))
            rows.append(("Enclosed area (polygon)", area, "area"))
        pts += [_center(s, k).tolist() for s, k in zip(shapes, kinds)]
    return {"rows": [r for r in rows if r[1] is not None], "points": pts, "kinds": kinds}


def format_rows(res: dict, system=None) -> list[tuple[str, str]]:
    """[(label, text)] in the active (or given) unit system."""
    out = []
    for label, v, q in res["rows"]:
        if q == "text" or isinstance(v, str):
            out.append((label, str(v)))
        elif q is None:
            out.append((label, f"{v:g}"))
        elif q == "volume_bf":
            out.append((label, U.fmt(v, "volume", system, unit="bf", places=2)))
        else:
            out.append((label, U.fmt(v, q, system)))
    return out


def points(doc, a, b) -> dict:
    """Point-to-point (world mm) — the classic two-click measure — as rows."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = b - a
    rows = [("Distance", float(np.linalg.norm(d)), "length"), ("ΔX", d[0], "length"), ("ΔY", d[1], "length"),
            ("ΔZ", d[2], "length")]
    _slope_rows(d, rows)
    return {"rows": rows, "points": [a.tolist(), b.tolist()], "kinds": ["vertex", "vertex"]}
