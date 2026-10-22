"""Objects, mates and the assembly solver.

An Object is a body with its own slice of the feature history and a pose
(translation + rotation vector, mm / radians) in the assembly. Mates tie
geometric references on two objects together; the solver finds poses that
satisfy every mate, optionally while following a drag target, so dragging one
object moves everything it's connected to through the mates.

References are stored as geometry in the object's own coordinates —
plane (p, n), axis (p, d, r) or point (p) — plus the face index they were
picked from. After a rebuild they're re-resolved to the closest matching face
of the same kind, so mates follow design changes instead of breaking.

Solver: damped least squares (Levenberg–Marquardt) on 6 pose parameters per
free object with a numeric Jacobian. Mate residuals are weighted 100× the
drag residual so the drag only moves what the mates leave free.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

MATE_TYPES = {
    # type: (allowed (a, b) reference kinds, needs value)
    "coincident": ({("plane", "plane"), ("point", "point"), ("point", "plane"), ("plane", "point"),
                    ("axis", "axis"), ("point", "axis"), ("axis", "point")}, False),
    "distance": ({("plane", "plane"), ("point", "point"), ("point", "plane"), ("plane", "point")}, True),
    "parallel": ({("plane", "plane"), ("axis", "axis"), ("plane", "axis"), ("axis", "plane")}, False),
    "perpendicular": ({("plane", "plane"), ("axis", "axis"), ("plane", "axis"), ("axis", "plane")}, False),
    "angle": ({("plane", "plane"), ("axis", "axis")}, True),
    "concentric": ({("axis", "axis")}, False),
    "hinge": ({("axis", "axis")}, False),          # concentric; pair with a coincident face mate to stop sliding
    "gear": ({("axis", "axis")}, True),            # value = ratio: B turns −ratio × A
    "rack": ({("axis", "plane"), ("plane", "axis")}, True),   # value = pinion pitch radius (mm)
    "lock": ({("plane", "plane"), ("axis", "axis"), ("point", "point"), ("plane", "axis"), ("axis", "plane"),
              ("point", "plane"), ("plane", "point"), ("point", "axis"), ("axis", "point")}, False),
    # v2.8
    "tangent": ({("plane", "axis"), ("axis", "plane"), ("axis", "axis")}, False),   # value: extra gap (mm)
    "limit_distance": ({("plane", "plane"), ("point", "point"), ("point", "plane"), ("plane", "point")}, True),
    "limit_angle": ({("plane", "plane"), ("axis", "axis")}, True),                # value = min, value2 = max
    "width": ({("plane", "plane")}, False),                                       # + extra refs a2, b2
    "symmetric": ({("plane", "plane"), ("point", "point"), ("axis", "axis")}, False),   # + extra ref c (plane)
    "path": ({("point", "path"), ("axis", "path"), ("path", "point"), ("path", "axis")}, False),
    "screw": ({("axis", "axis")}, True),                                         # value = lead (mm per turn)
    "cam": ({("profile", "axis"), ("profile", "point"), ("axis", "profile"), ("point", "profile")}, False),
    "slot": ({("slot", "axis"), ("slot", "point"), ("axis", "slot"), ("point", "slot")}, False),
}
EXTRA_REFS = {"width": ("a2", "b2"), "symmetric": ("c",)}
RANGE_MATES = ("limit_distance", "limit_angle")


class AssemblyError(ValueError):
    pass


# --------------------------------------------------------------- objects ---

@dataclass
class Obj:
    name: str
    color: str = "#b4b8bf"
    visible: bool = True
    fixed: bool = False
    pose: list = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])   # tx ty tz rx ry rz
    material: str | None = None          # None: the document's material
    part_no: str | None = None           # library / purchased part designation (BOM)
    unit_cost: float | None = None       # purchased-part price (BOM)
    description: str | None = None       # BOM description (standard parts)
    std: dict | None = None              # standard-library reference {id, kind, vendor, version}
    source: dict | None = None           # where it was imported from {system, id, path, name, linked}
    group: str | None = None             # subassembly it belongs to (v2.8)
    instance_of: str | None = None       # pattern / mirror instance: geometry of this object (v2.8)
    mirrored: bool = False               # instance is the mirror image of its source
    pattern: str | None = None           # component pattern that owns this instance
    suppressed: bool = False             # left out of the model (configurations)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in ("name", "color", "visible", "fixed", "pose", "material", "part_no",
                                         "unit_cost", "description", "std", "source", "group", "instance_of",
                                         "mirrored", "pattern", "suppressed") if k in d})


@dataclass
class Mate:
    name: str
    type: str
    a: dict                 # {"obj": name, "ref": {kind, p, n|d, r?, face?}}
    b: dict
    value: float = 0.0      # mm or degrees
    flip: bool = False      # coincident/distance/parallel planes: aligned instead of opposed
    suppressed: bool = False
    value2: float = 0.0     # limit mates: maximum (value = minimum)
    extra: dict = field(default_factory=dict)   # {"a2"|"b2"|"c": {"obj", "ref"}} and per-type data

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in ("name", "type", "a", "b", "value", "flip", "suppressed", "value2", "extra")
                      if k in d})

    def objects(self):
        return (self.a["obj"], self.b["obj"]) + tuple(self.extra[k]["obj"] for k in EXTRA_REFS.get(self.type, ())
                                                      if k in self.extra)


# ------------------------------------------------------------- rotations ---

def rotmat(rv) -> np.ndarray:
    rv = np.asarray(rv, float)
    th = np.linalg.norm(rv)
    if th < 1e-12:
        return np.eye(3)
    k = rv / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K


def rotvec(R) -> np.ndarray:
    c = max(-1.0, min(1.0, (np.trace(R) - 1) / 2))
    th = math.acos(c)
    if th < 1e-9:
        return np.zeros(3)
    if abs(th - math.pi) < 1e-6:
        # axis from the largest diagonal entry
        i = int(np.argmax(np.diag(R)))
        v = R[:, i] + np.eye(3)[i]
        k = v / np.linalg.norm(v)
        return k * th
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * math.sin(th))
    return w * th


def pose_matrix(pose) -> np.ndarray:
    """4×4 homogeneous transform for a pose [tx ty tz rx ry rz]."""
    M = np.eye(4)
    M[:3, :3] = rotmat(pose[3:6])
    M[:3, 3] = pose[:3]
    return M


def compose(pose_a, pose_b):
    """pose_a ∘ pose_b (apply b, then a)."""
    M = pose_matrix(pose_a) @ pose_matrix(pose_b)
    return list(M[:3, 3]) + list(rotvec(M[:3, :3]))


def location_of(pose):
    """build123d Location for a pose."""
    from build123d import Location
    from OCP.gp import gp_Trsf
    M = pose_matrix(pose)
    t = gp_Trsf()
    t.SetValues(*[float(M[i, j]) for i in range(3) for j in range(4)])
    return Location(t)


# -------------------------------------------------------------- references ---

def _xyz(v):
    if hasattr(v, "X"):
        return [float(v.X), float(v.Y), float(v.Z)]
    return [float(v[0]), float(v[1]), float(v[2])]


def ref_from_face(face, index=None, pick=None) -> dict:
    """Describe a face as plane / axis geometry in its body's coordinates."""
    gt = face.geom_type.name
    if gt == "PLANE":
        n = face.normal_at()
        c = _xyz(pick if pick is not None else face.center())
        return {"kind": "plane", "p": c, "n": _xyz(n), "face": index}
    if gt in ("CYLINDER", "CONE"):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        s = BRepAdaptor_Surface(face.wrapped)
        if gt == "CYLINDER":
            ax = s.Cylinder().Axis()
            r = s.Cylinder().Radius()
        else:
            ax = s.Cone().Axis()
            r = s.Cone().RefRadius()
        loc, d = ax.Location(), ax.Direction()
        p = np.array([loc.X(), loc.Y(), loc.Z()])
        dv = np.array([d.X(), d.Y(), d.Z()])
        # anchor the axis point near the face centre so "point on axis" mates behave
        c = np.array(_xyz(pick if pick is not None else face.center()))
        p = p + dv * float(np.dot(c - p, dv))
        return {"kind": "axis", "p": list(p), "d": list(dv), "r": r, "face": index}
    if gt == "SPHERE":
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        s = BRepAdaptor_Surface(face.wrapped).Sphere()
        loc = s.Location()
        return {"kind": "point", "p": [loc.X(), loc.Y(), loc.Z()], "face": index}
    c = face.center()
    return {"kind": "point", "p": [c.X, c.Y, c.Z], "face": index}


def resolve_ref(body, ref: dict) -> dict:
    """Re-find a stored reference on a rebuilt body: the closest face of the same kind
    (planes: same normal; axes: same direction and radius). Returns an updated ref."""
    if body is None or ref.get("kind") == "point" and ref.get("face") is None:
        return ref
    p0 = np.array(ref["p"])
    best, bscore = None, float("inf")
    for i, f in enumerate(body.faces()):
        try:
            r = ref_from_face(f, i)
        except Exception:  # noqa: BLE001
            continue
        if r["kind"] != ref["kind"]:
            continue
        if r["kind"] == "plane":
            n0, n1 = np.array(ref["n"]), np.array(r["n"])
            if np.dot(n0, n1) < 0.999:
                continue
            score = abs(np.dot(np.array(r["p"]) - p0, n0)) * 10 + np.linalg.norm(np.array(r["p"]) - p0) * 0.01
        elif r["kind"] == "axis":
            d0, d1 = np.array(ref["d"]), np.array(r["d"])
            if abs(np.dot(d0, d1)) < 0.999:
                continue
            off = np.cross(np.array(r["p"]) - p0, d0)
            score = np.linalg.norm(off) + abs(r.get("r", 0) - ref.get("r", 0)) * 5
        else:
            score = np.linalg.norm(np.array(r["p"]) - p0)
        if i == ref.get("face"):
            score *= 0.5                       # prefer the face it was picked from
        if score < bscore:
            best, bscore = r, score
    if best is None:
        return ref
    out = dict(best)
    if out["kind"] == "plane":
        # keep the picked spot (projected onto the new plane) so point-type use stays sensible
        n = np.array(out["n"])
        out["p"] = list(p0 - n * float(np.dot(p0 - np.array(out["p"]), n)))
        if np.dot(np.array(ref["n"]), n) < 0:
            out["n"] = list(-n)
    if out["kind"] == "axis" and np.dot(np.array(ref["d"]), np.array(out["d"])) < 0:
        out["d"] = list(-np.array(out["d"]))
    return out


def world_ref(ref: dict, pose) -> dict:
    R = rotmat(pose[3:6])
    t = np.asarray(pose[:3], float)
    out = {"kind": ref["kind"], "p": R @ np.asarray(ref["p"], float) + t}
    if "n" in ref:
        out["n"] = R @ np.asarray(ref["n"], float)
    if "d" in ref:
        out["d"] = R @ np.asarray(ref["d"], float)
    if "pts" in ref:
        out["pts"] = np.asarray(ref["pts"], float) @ R.T + t
        out["closed"] = ref.get("closed", False)
    if "p2" in ref:
        out["p2"] = R @ np.asarray(ref["p2"], float) + t
    if "r" in ref:
        out["r"] = ref["r"]
    return out


# ------------------------------------------------------------ polylines ---

def closest_on_polyline(P, pts, closed=False):
    """(closest point, arc length there, total length) of point P on a polyline."""
    P = np.asarray(P, float)
    Q = np.asarray(pts, float)
    if closed:
        Q = np.vstack([Q, Q[:1]])
    A, B = Q[:-1], Q[1:]
    AB = B - A
    L2 = np.einsum("ij,ij->i", AB, AB)
    L2[L2 < 1e-18] = 1e-18
    t = np.clip(np.einsum("ij,ij->i", P - A, AB) / L2, 0.0, 1.0)
    C = A + AB * t[:, None]
    dd = np.linalg.norm(C - P, axis=1)
    i = int(np.argmin(dd))
    seg = np.sqrt(L2)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    return C[i], float(cum[i] + seg[i] * t[i]), float(cum[-1])


def _rot_about(R0, pose, d):
    """Rotation angle (rad) of a body about unit axis d since its stored orientation R0."""
    return float(rotvec(rotmat(pose[3:6]) @ np.asarray(R0).T) @ d)


# --------------------------------------------------------------- residuals ---

def _dirv(r):
    return r["n"] if r["kind"] == "plane" else r["d"]


def mate_residual(m: Mate, A: dict, B: dict, pa=None, pb=None, X: dict | None = None) -> np.ndarray:
    """Residual vector (zero when satisfied). A/B are world refs.
    pa/pb are the poses, needed for 'lock'."""
    t = m.type
    ka, kb = A["kind"], B["kind"]
    s = -1.0 if not m.flip else 1.0          # planes default to facing each other
    if t in ("coincident", "distance") and ka == kb == "plane":
        n1, n2 = A["n"], B["n"]
        off = float(np.dot(n1, B["p"] - A["p"]))
        target = m.value if t == "distance" else 0.0
        return np.concatenate([n1 - s * n2, [off - (target if s < 0 else target)]])
    if t in ("coincident", "distance") and {ka, kb} == {"point", "plane"}:
        P, Q = (A, B) if ka == "plane" else (B, A)
        off = float(np.dot(P["n"], Q["p"] - P["p"]))
        return np.array([off - (m.value if t == "distance" else 0.0)])
    if t in ("coincident", "distance") and ka == kb == "point":
        d = B["p"] - A["p"]
        if t == "distance":
            return np.array([np.linalg.norm(d) - m.value])
        return d
    if t in ("coincident", "concentric", "hinge") and ka == kb == "axis":
        d1, d2 = A["d"], B["d"]
        return np.concatenate([np.cross(d1, d2), np.cross(B["p"] - A["p"], d1)])
    if t == "coincident" and {ka, kb} == {"point", "axis"}:
        X, P = (A, B) if ka == "axis" else (B, A)
        return np.cross(P["p"] - X["p"], X["d"])
    if t == "parallel":
        v1, v2 = _dirv(A), _dirv(B)
        if (ka == "plane") != (kb == "plane"):          # plane ∥ axis: axis ⟂ normal
            return np.array([float(np.dot(v1, v2))])
        return np.cross(v1, v2)
    if t == "perpendicular":
        v1, v2 = _dirv(A), _dirv(B)
        if (ka == "plane") != (kb == "plane"):          # plane ⟂ axis: axis ∥ normal
            return np.cross(v1, v2)
        return np.array([float(np.dot(v1, v2))])
    if t == "angle":
        v1, v2 = _dirv(A), _dirv(B)
        return np.array([float(np.dot(v1, v2)) - math.cos(math.radians(m.value))])
    if t in ("gear", "rack"):
        if pa is None or pb is None or "R0" not in m.a:
            return np.zeros(0)
        ax = A if ka == "axis" else B
        pinion, other, Rp0, pp = (A, B, m.a["R0"], pa) if ka == "axis" else (B, A, m.b["R0"], pb)
        d = np.asarray(ax["d"], float)
        d = d / (np.linalg.norm(d) or 1.0)
        th_p = float(rotvec(rotmat(pp[3:6]) @ np.asarray(Rp0).T) @ d)
        if t == "gear":
            Rb0 = m.b["R0"]
            db = np.asarray(B["d"], float)
            db = db / (np.linalg.norm(db) or 1.0)
            th_b = float(rotvec(rotmat(pb[3:6]) @ np.asarray(Rb0).T) @ db)
            r = th_b + m.value * th_p
            return np.array([math.atan2(math.sin(r), math.cos(r)) * 10.0])
        # rack: the plane-side part slides along (pinion axis × plane normal)
        n = np.asarray(other["n"], float)
        u = np.cross(d, n)
        u = u / (np.linalg.norm(u) or 1.0)
        po = pb if ka == "axis" else pa
        t0 = np.asarray(m.b["t0"] if ka == "axis" else m.a["t0"], float)
        s_ = float((np.asarray(po[:3], float) - t0) @ u)
        return np.array([s_ - m.value * th_p])
    if t == "lock":
        # relative pose of B in A's frame must stay what it was when locked (stored in m.value as -1 sentinel)
        rel = m.a.get("rel")
        if rel is None or pa is None or pb is None:
            return np.zeros(0)
        Ma, Mb = pose_matrix(pa), pose_matrix(pb)
        cur = np.linalg.inv(Ma) @ Mb
        Rt = np.asarray(rel)
        return np.concatenate([(cur[:3, :3] - Rt[:3, :3]).ravel(), (cur[:3, 3] - Rt[:3, 3]) * 0.1])
    return _residual_v28(m, A, B, pa, pb, X or {})


def _clamp_res(v, lo, hi):
    lo, hi = min(lo, hi), max(lo, hi)
    return v - min(max(v, lo), hi)


def _residual_v28(m, A, B, pa, pb, X):
    t = m.type
    ka, kb = A["kind"], B["kind"]
    if t == "tangent":
        if {ka, kb} == {"plane", "axis"}:
            P, Ax = (A, B) if ka == "plane" else (B, A)
            n = P["n"] / (np.linalg.norm(P["n"]) or 1.0)
            off = float(np.dot(n, Ax["p"] - P["p"]))
            side = 1.0 if not m.flip else -1.0
            return np.array([float(np.dot(n, Ax["d"])), off - side * (Ax.get("r", 0.0) + m.value)])
        d1 = A["d"] / (np.linalg.norm(A["d"]) or 1.0)
        dist = float(np.linalg.norm(np.cross(B["p"] - A["p"], d1)))
        ra, rb = A.get("r", 0.0), B.get("r", 0.0)
        target = (abs(ra - rb) if m.flip else ra + rb) + m.value
        return np.concatenate([np.cross(A["d"], B["d"]), [dist - target]])
    if t == "limit_distance":
        lo, hi = m.value, m.value2
        if ka == kb == "plane":
            s = -1.0 if not m.flip else 1.0
            off = float(np.dot(A["n"], B["p"] - A["p"]))
            return np.concatenate([A["n"] - s * B["n"], [_clamp_res(off, lo, hi)]])
        if ka == kb == "point":
            return np.array([_clamp_res(float(np.linalg.norm(B["p"] - A["p"])), lo, hi)])
        P, Q = (A, B) if ka == "plane" else (B, A)
        return np.array([_clamp_res(float(np.dot(P["n"], Q["p"] - P["p"])), lo, hi)])
    if t == "limit_angle":
        v1, v2 = _dirv(A), _dirv(B)
        c = float(np.dot(v1, v2)) / ((np.linalg.norm(v1) * np.linalg.norm(v2)) or 1.0)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, c))))
        return np.array([math.radians(_clamp_res(ang, m.value, m.value2)) * 10.0])
    if t == "width":
        A2, B2 = X.get("a2"), X.get("b2")
        if A2 is None or B2 is None:
            return np.zeros(0)
        n = A["n"] / (np.linalg.norm(A["n"]) or 1.0)
        mid_a = (A["p"] + A2["p"]) / 2
        mid_b = (B["p"] + B2["p"]) / 2
        return np.concatenate([np.cross(n, B["n"]), [float(np.dot(n, mid_b - mid_a))]])
    if t == "symmetric":
        C = X.get("c")
        if C is None:
            return np.zeros(0)
        nc = C["n"] / (np.linalg.norm(C["n"]) or 1.0)
        H = np.eye(3) - 2 * np.outer(nc, nc)
        if ka == kb == "plane":
            return np.concatenate([H @ A["n"] - B["n"],
                                   [float(np.dot(nc, A["p"] - C["p"]) + np.dot(nc, B["p"] - C["p"]))]])
        mid = (A["p"] + B["p"]) / 2
        out = [[float(np.dot(nc, mid - C["p"]))], np.cross(B["p"] - A["p"], nc)]
        if ka == kb == "axis":
            out.append(np.cross(H @ A["d"], B["d"]))
        return np.concatenate(out)
    if t == "path":
        F, Pth = (A, B) if kb == "path" else (B, A)
        q, s_, L = closest_on_polyline(F["p"], Pth["pts"], Pth.get("closed", False))
        out = [F["p"] - q]
        pct = m.extra.get("percent")
        if pct is not None and L > 0:
            out.append([(s_ - float(pct) / 100.0 * L) * 0.2])
        return np.concatenate(out)
    if t == "screw":
        if pa is None or pb is None or "R0" not in m.a:
            return np.zeros(0)
        d = A["d"] / (np.linalg.norm(A["d"]) or 1.0)
        th = _rot_about(m.b["R0"], pb, d) - _rot_about(m.a["R0"], pa, d)
        s_ = float(((np.asarray(pb[:3]) - np.asarray(m.b["t0"])) - (np.asarray(pa[:3]) - np.asarray(m.a["t0"]))) @ d)
        lead = m.value * (-1.0 if m.flip else 1.0)
        return np.concatenate([np.cross(A["d"], B["d"]), np.cross(B["p"] - A["p"], A["d"]),
                               [s_ - lead * th / (2 * math.pi)]])
    if t == "cam":
        Cm, F = (A, B) if ka == "profile" else (B, A)
        d = Cm["d"] / (np.linalg.norm(Cm["d"]) or 1.0)
        prj = lambda X_: X_ - np.outer((X_ - Cm["p"]) @ d, d) if X_.ndim == 2 else X_ - d * float((X_ - Cm["p"]) @ d)  # noqa: E731
        q, _s, _L = closest_on_polyline(prj(F["p"]), prj(Cm["pts"]), True)
        dist = float(np.linalg.norm(prj(F["p"]) - q))
        out = [[dist - (F.get("r", 0.0) if F["kind"] == "axis" else 0.0)]]
        if F["kind"] == "axis":
            out.append(np.cross(d, F["d"]))
        return np.concatenate(out)
    if t == "slot":
        S, F = (A, B) if ka == "slot" else (B, A)
        p1, p2 = S["p"], S["p2"]
        L = float(np.linalg.norm(p2 - p1)) or 1.0
        u = (p2 - p1) / L
        d = S["d"] / (np.linalg.norm(S["d"]) or 1.0)
        v = np.cross(u, d)
        q = F["p"]
        tt = float(np.dot(q - p1, u)) / L
        out = [[float(np.dot(q - p1, v))], [(max(0.0, -tt) + max(0.0, tt - 1.0)) * L]]
        along = m.extra.get("along", "free")
        if along == "center":
            out.append([(tt - 0.5) * L])
        elif along == "percent":
            out.append([(tt - float(m.extra.get("percent", 50.0)) / 100.0) * L])
        if F["kind"] == "axis":
            out.append(np.cross(d, F["d"]))
        return np.concatenate(out)
    raise AssemblyError(f"Mate {m.name}: '{t}' doesn't apply to {ka} + {kb}")


# ------------------------------------------------------------------ solver ---

@dataclass
class SolveResult:
    poses: dict
    ok: bool
    dof: int
    residuals: dict              # mate name -> max abs residual
    failed: list
    iterations: int = 0


def solve(objects: list[Obj], mates: list[Mate], refs: dict, drag=None,
          max_iter=60, tol=1e-6, damping=1e-3) -> SolveResult:
    """refs: {(mate name, 'a'|'b'): local ref dict} (already re-resolved).
    drag: (obj name, local point (3,), target) — target is a world point or {"ray": (origin, dir)};
    pulled softly, so mates always win."""
    names = [o.name for o in objects]
    poses = {o.name: np.asarray(o.pose, float).copy() for o in objects}
    active = [m for m in mates if not m.suppressed]
    involved = {n for m in active for n in m.objects()}
    if drag:
        involved.add(drag[0])
    free = [o.name for o in objects if not o.fixed and o.name in involved]
    if not free:
        res = {m.name: 0.0 for m in active}
        bad = []
        for m in active:
            r = _eval(m, refs, poses)
            res[m.name] = float(np.max(np.abs(r))) if len(r) else 0.0
            if res[m.name] > 1e-4:
                bad.append(m.name)
        return SolveResult({k: list(v) for k, v in poses.items()}, not bad, 0, res, bad)
    idx = {n: i for i, n in enumerate(free)}
    W_MATE, W_DRAG = 100.0, 1.0

    def unpack(x):
        P = dict(poses)
        for n, i in idx.items():
            P[n] = x[6 * i: 6 * i + 6]
        return P

    def resid(x, with_drag=True):
        P = unpack(x)
        parts = [W_MATE * _eval(m, refs, P) for m in active]
        if with_drag and drag:
            o, lp, tgt = drag
            M = pose_matrix(P[o])
            w = M[:3, :3] @ np.asarray(lp, float) + M[:3, 3]
            if isinstance(tgt, dict) and "ray" in tgt:
                # follow the cursor: distance from the grabbed point to the pick ray (screen-space feel)
                ro, rd = (np.asarray(v, float) for v in tgt["ray"])
                rd = rd / (np.linalg.norm(rd) or 1.0)
                parts.append(W_DRAG * np.cross(w - ro, rd) * 0.05)
            else:
                parts.append(W_DRAG * (w - np.asarray(tgt, float)) * 0.05)
        return np.concatenate(parts) if parts else np.zeros(0)

    x = _wrap_rotations(np.concatenate([poses[n] for n in free]))
    lam = damping
    f = resid(x)
    cost = float(f @ f)
    it = 0
    for it in range(1, max_iter + 1):
        if cost < tol ** 2:
            break                                   # already satisfied: don't wander along free DOF
        J = _jac(resid, x, f)
        g = J.T @ f
        H = J.T @ J
        improved = False
        # Gauss-Newton first (minimum-norm: directions the mates leave free don't move); fall back to
        # damped steps when it doesn't reduce the error
        try:
            gn = np.linalg.lstsq(J, -f, rcond=1e-10)[0]
            xn = x + gn
            fn = resid(xn)
            cn = float(fn @ fn)
            if cn < cost and np.all(np.isfinite(gn)) and np.linalg.norm(gn) < 1e6:
                x = _wrap_rotations(xn)
                f = resid(x)
                cost = float(f @ f)
                lam = max(lam / 3, 1e-9)
                step = gn
                if np.linalg.norm(step) < 1e-10 or cost < tol ** 2:
                    break
                continue
        except np.linalg.LinAlgError:
            pass
        for _ in range(12):
            # damping + a small identity term so directions the mates leave free don't drift
            step = np.linalg.solve(H + lam * np.diag(np.diag(H)) + (lam + 1e-4) * np.eye(len(x)), -g)
            xn = x + step
            fn = resid(xn)
            cn = float(fn @ fn)
            if cn < cost:
                x = _wrap_rotations(xn)
                f = resid(x)
                cost = float(f @ f)
                lam = max(lam / 3, 1e-9)
                improved = True
                break
            lam *= 4
        if not improved or np.linalg.norm(step) < 1e-10 or cost < tol ** 2:
            break
    P = unpack(x)
    res, bad = {}, []
    for m in active:
        r = _eval(m, refs, P)
        res[m.name] = float(np.max(np.abs(r))) if len(r) else 0.0
        if res[m.name] > 1e-3:
            bad.append(m.name)
    # DOF from the constraint Jacobian rank
    Jc = _jac(lambda xx: resid(xx, with_drag=False), x, resid(x, with_drag=False))
    rank = int(np.linalg.matrix_rank(Jc, tol=1e-6 * max(1.0, np.abs(Jc).max(initial=0)))) if Jc.size else 0
    dof = 6 * len(free) - rank
    # objects that aren't mated or fixed keep their own 6 DOF
    dof += 6 * len([o for o in objects if not o.fixed and o.name not in involved])
    return SolveResult({k: [float(v) for v in vv] for k, vv in P.items()}, not bad, dof, res, bad, it)


def _wrap_rotations(x):
    """Keep every rotation vector at angle ≤ π (same rotation, well-conditioned numbers)."""
    x = x.copy()
    for k in range(0, len(x), 6):
        x[k + 3: k + 6] = rotvec(rotmat(x[k + 3: k + 6]))
    return x


def _eval(m, refs, P):
    A = world_ref(refs[(m.name, "a")], P[m.a["obj"]])
    B = world_ref(refs[(m.name, "b")], P[m.b["obj"]])
    X = {}
    for k in EXTRA_REFS.get(m.type, ()):
        if k in m.extra and (m.name, k) in refs:
            X[k] = world_ref(refs[(m.name, k)], P[m.extra[k]["obj"]])
    return mate_residual(m, A, B, P[m.a["obj"]], P[m.b["obj"]], X)


def _jac(fun, x, f0, h=1e-6):
    J = np.zeros((len(f0), len(x)))
    for j in range(len(x)):
        xp = x.copy()
        xp[j] += h
        J[:, j] = (fun(xp) - f0) / h
    if J.size:                       # finite-difference noise in exact-zero columns couples free DOF spuriously
        J[np.abs(J) < 1e-8 * max(1.0, float(np.abs(J).max()))] = 0.0
    return J


def check_mate_kinds(t: str, ka: str, kb: str):
    if t not in MATE_TYPES:
        raise AssemblyError(f"Unknown mate type '{t}'. Types: {list(MATE_TYPES)}")
    if (ka, kb) not in MATE_TYPES[t][0]:
        raise AssemblyError(f"A {t} mate can't join a {ka} to a {kb}")
