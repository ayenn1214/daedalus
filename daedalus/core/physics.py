"""Rigid-body physics (MuJoCo). Qt-free.

The assembly becomes a MuJoCo model:
  object            → body; mass, centre of mass and inertia tensor from its solid × material density
  fixed object      → static body (ground)
  mates             → joints. For each mated pair the motion the mates still allow is found from the
                      mate equations (null space of their Jacobian) and expressed as slide / hinge /
                      ball joints — so any combination of Daedalus mates works, not a fixed list.
                      Mates that close a loop (four-bar linkages) become equality constraints.
  unmated object    → free body: falls, collides, tumbles
  solids            → collision shapes (convex hull, or convex decomposition for concave parts)
  drives            → on a mate: motor at a speed, torque/force, servo to a position, or a spring

Units in Daedalus: mm, deg, kg, N, s. MuJoCo runs in SI (m, rad); conversion happens here.
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import assembly as asm

DRIVE_KINDS = ("speed", "torque", "position", "spring")
COLLISIONS = ("decompose", "hull", "off")


class PhysicsError(RuntimeError):
    pass


# ---------------------------------------------------------------- settings ---

@dataclass
class Drive:
    mate: str
    kind: str = "speed"           # speed | torque | position | spring
    value: float = 0.0            # speed: deg/s or mm/s · torque: N·m or N · position: deg or mm from start
                                  # spring: stiffness N·m/deg or N/mm
    damping: float = 0.0          # spring damping: N·m·s/deg or N·s/mm
    max: float = 0.0              # motor torque/force limit (0 = unlimited)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in ("mate", "kind", "value", "damping", "max") if k in d})


@dataclass
class PhysicsSettings:
    gravity: list = field(default_factory=lambda: [0.0, 0.0, -9.81])   # m/s²
    duration: float = 2.0             # s
    timestep: float = 0.001           # s
    friction: float = 0.5
    collisions: str = "decompose"     # decompose | hull | off
    fps: int = 60                     # recorded frames per second
    drives: list = field(default_factory=list)                       # [Drive]

    def to_dict(self):
        d = asdict(self)
        d["drives"] = [x.to_dict() if isinstance(x, Drive) else dict(x) for x in self.drives]
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        s = cls(**{k: d[k] for k in ("gravity", "duration", "timestep", "friction", "collisions", "fps") if k in d})
        s.drives = [Drive.from_dict(x) for x in d.get("drives", [])]
        return s


@dataclass
class SimResult:
    times: list
    frames: list                      # [{obj: pose}] (Daedalus poses, mm / rotvec)
    traces: dict                      # name -> [values]  (aligned with times)
    joints: list                      # human-readable joint list
    notes: list
    summary: dict
    ok: bool = True


# ------------------------------------------------------------ mass & shape ---

def mass_props(body, density_gcc: float):
    """(mass kg, centre of mass mm in the body frame, inertia kg·m² at the COM in body axes)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    props = GProp_GProps()
    fn = getattr(BRepGProp, "VolumeProperties_s", None) or BRepGProp.VolumeProperties
    fn(body.wrapped, props)
    vol = props.Mass()                                    # mm³
    rho = density_gcc * 1e-6                              # kg/mm³
    c = props.CentreOfMass()
    Mi = props.MatrixOfInertia()                          # mm⁵, about the COM
    I = np.array([[Mi.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)]) * rho * 1e-6
    return vol * rho, np.array([c.X(), c.Y(), c.Z()]), I


_shape_cache: dict = {}


def _shape_key(body, mode) -> str:
    """Stable fingerprint of a solid's geometry (BREP text isn't: it carries cached meshes)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    props = GProp_GProps()
    fn = getattr(BRepGProp, "VolumeProperties_s", None) or BRepGProp.VolumeProperties
    fn(body.wrapped, props)
    c = props.CentreOfMass()
    Mi = props.MatrixOfInertia()
    bb = body.bounding_box()
    sig = [props.Mass(), body.area, c.X(), c.Y(), c.Z(), bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z,
           *[Mi.Value(i, j) for i in (1, 2, 3) for j in (1, 2, 3)]]
    text = ",".join(f"{v:.6g}" for v in sig) + f"|{len(body.faces())}|{len(body.edges())}|{mode}"
    return hashlib.sha1(text.encode()).hexdigest()


def _hull_volume(V):
    try:
        from scipy.spatial import ConvexHull
        return float(ConvexHull(V).volume)
    except Exception:  # noqa: BLE001
        return None


def _verts(solid, tol):
    verts, tris = solid.tessellate(tol, 0.35)
    return np.array([[p.X, p.Y, p.Z] for p in verts], float), np.array(tris, int)


def _concavity(solid, tol):
    V, _ = _verts(solid, tol)
    hv = _hull_volume(V)
    return 0.0 if hv is None else max(0.0, hv - solid.volume)


def _face_planes(solid, limit=40):
    """Distinct planes of the solid's planar faces, biggest faces first."""
    from build123d import Plane, Vector
    seen, out = set(), []
    for f in sorted((f for f in solid.faces() if f.geom_type.name == "PLANE"), key=lambda f: -f.area):
        n, c = f.normal_at(), f.center()
        d = n.dot(c)
        key = (round(n.X, 4), round(n.Y, 4), round(n.Z, 4), round(d, 3))
        if key in seen or tuple(-k for k in key) in seen:
            continue
        seen.add(key)
        out.append(Plane(origin=Vector(n.X, n.Y, n.Z) * d, z_dir=(n.X, n.Y, n.Z)))
        if len(out) >= limit:
            break
    return out


def convex_split(solid, tol, depth=0, max_depth=6):
    """Split a solid along the planes of its own flat faces until the pieces are convex (exact for
    steps, bosses, slots, L/U shapes). Pieces whose concavity is curved (holes) come back as they are."""
    from build123d import Keep
    from build123d import split as bsplit
    conc = _concavity(solid, tol)
    if conc <= solid.volume * 0.015 or depth >= max_depth:
        return [solid]
    best = None
    for pl in _face_planes(solid):
        try:
            parts = [x for x in bsplit(solid, bisect_by=pl, keep=Keep.BOTH).solids() if x.volume > 1e-6]
        except Exception:  # noqa: BLE001
            continue
        if len(parts) < 2:
            continue
        sc = sum(_concavity(x, tol) for x in parts)
        if best is None or sc < best[0]:
            best = (sc, parts)
    if best is None or best[0] >= conc * 0.98:
        return [solid]
    out = []
    for x in best[1]:
        out += convex_split(x, tol, depth + 1, max_depth)
    return out


def _coacd(V, T, volume):
    """CoACD on a watertight B-rep mesh (no remeshing, so pieces never bulge past the part), tightening
    the concavity threshold until the pieces' hulls overfill the solid by < 0.5 %."""
    import coacd
    coacd.set_log_level("error")
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    mesh_vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0) or volume
    volume = mesh_vol
    import time
    best = None
    t0 = time.perf_counter()
    for th in (0.03, 0.015):
        if best is not None and time.perf_counter() - t0 > 8.0:
            break                                         # good enough; don't stall the user
        res = coacd.run_coacd(coacd.Mesh(V, T), threshold=th, max_convex_hull=64, preprocess_mode="off",
                              resolution=1500, mcts_iterations=60, mcts_max_depth=2, seed=1)
        got = [np.asarray(p[0], float) for p in res if len(p[0]) >= 4]
        excess = sum((_hull_volume(g) or 0.0) for g in got) - volume
        best = got
        if excess < 0.005 * volume:
            break
    return best


def _concave_axes(solid):
    """(point, direction) of concave cylinder/cone faces: holes, bores, inside fillets."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    out = []
    for f in solid.faces():
        gt = f.geom_type.name
        if gt not in ("CYLINDER", "CONE"):
            continue
        s = BRepAdaptor_Surface(f.wrapped)
        ax = s.Cylinder().Axis() if gt == "CYLINDER" else s.Cone().Axis()
        p0 = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        d = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        c = f.center()
        c = np.array([c.X, c.Y, c.Z])
        n = f.normal_at(f.center())
        radial = (c - p0) - d * np.dot(c - p0, d)
        if np.dot(radial, [n.X, n.Y, n.Z]) < 0:            # normal points at the axis: material outside
            if not any(np.linalg.norm(np.cross(d, q[1])) < 1e-6 and
                       np.linalg.norm(np.cross(p0 - q[0], d)) < 1e-6 for q in out):
                out.append((p0, d))
    return out


def radial_split(solid, axes, sectors=12):
    """Cut a solid with planes through each concave axis, every 360/sectors degrees, so each piece
    only holds a thin sliver of the curved hollow (nearly convex)."""
    from build123d import Keep, Plane
    from build123d import split as bsplit
    pieces = [solid]
    for p0, d in axes:
        u = np.cross(d, [1, 0, 0] if abs(d[0]) < 0.9 else [0, 1, 0])
        u /= np.linalg.norm(u)
        w = np.cross(d, u)
        for k in range(sectors // 2):
            a = math.pi * k / (sectors // 2)
            n = math.cos(a) * u + math.sin(a) * w
            pl = Plane(origin=tuple(p0), z_dir=tuple(n), x_dir=tuple(d))
            nxt = []
            for pc in pieces:
                try:
                    parts = [x for x in bsplit(pc, bisect_by=pl, keep=Keep.BOTH).solids() if x.volume > 1e-6]
                except Exception:  # noqa: BLE001
                    parts = [pc]
                nxt += parts or [pc]
            pieces = nxt
    return pieces


def collision_pieces(body, mode="decompose", notes=None) -> list[np.ndarray]:
    """Convex pieces (vertex arrays, mm, body frame) for contact. 'decompose': split along the
    solid's flat faces (exact), then CoACD for curved concavities such as holes; 'hull': one
    convex hull per solid. Cached in memory and on disk."""
    if mode == "off":
        return []
    key = _shape_key(body, mode + ":v5")
    if key in _shape_cache:
        return _shape_cache[key]
    cache_dir = Path(os.environ.get("DAEDALUS_CACHE", Path.home() / ".cache" / "daedalus")) / "collision"
    cfile = cache_dir / f"{key}.npz"
    if cfile.exists():
        try:
            z = np.load(cfile)
            pieces = [z[k] for k in sorted(z.files, key=lambda s: int(s[1:]))]
            _shape_cache[key] = pieces
            return pieces
        except Exception:  # noqa: BLE001
            pass
    tol = max(0.05, body.bounding_box().diagonal * 0.003)
    solids = list(body.solids()) or [body]
    pieces = []
    warned = False
    for sol in solids:
        parts = convex_split(sol, tol) if mode == "decompose" else [sol]
        if mode == "decompose":
            refined = []
            for part in parts:
                axes = _concave_axes(part) if _concavity(part, tol) > part.volume * 0.015 else []
                if axes and len(axes) <= 12:
                    for sub in radial_split(part, axes):
                        refined += convex_split(sub, tol, max_depth=3)
                else:
                    refined.append(part)
            parts = refined
        for part in parts:
            V, T = _verts(part, tol)
            hv = _hull_volume(V)
            concave = mode == "decompose" and hv is not None and part.volume < hv * 0.97
            got = None
            if concave:
                try:
                    got = _coacd(V, T, part.volume)
                except ImportError:
                    if notes is not None and not warned:
                        notes.append("Parts with holes/curved pockets collide as convex hulls "
                                     "(pip install coacd for exact shapes)")
                        warned = True
                except Exception as e:  # noqa: BLE001
                    if notes is not None:
                        notes.append(f"Convex decomposition failed ({e}); using the hull")
            pieces += got or [V]
    pieces = [p for p in pieces if len(p) >= 4 and (_hull_volume(p) or 1.0) > 1e-6]
    _shape_cache[key] = pieces
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cfile, **{f"p{i}": p for i, p in enumerate(pieces)})
    except OSError:
        pass
    return pieces


# ----------------------------------------------------- mates → joint motion ---

def _twist_pose(d, eps):
    return list(np.asarray(d[:3]) * eps) + list(np.asarray(d[3:]) * eps)


def allowed_motion(poses, mates, refs, child, scale=100.0):
    """Motions of `child` (its partner held still) that keep `mates` satisfied, as
    joint specs in world coordinates: [{"type": "slide"|"hinge"|"ball", "axis", "pos"}]."""
    P = dict(poses)
    base = np.asarray(P[child], float)

    def resid(d):
        Q = dict(P)
        Q[child] = asm.compose(_twist_pose(d, 1.0), base) if np.any(d) else list(base)
        parts = [asm._eval(m, refs, Q) for m in mates]
        return np.concatenate(parts) if parts else np.zeros(0)

    f0 = resid(np.zeros(6))
    if len(f0) == 0:
        J = np.zeros((1, 6))
    else:
        h = 1e-6
        J = np.zeros((len(f0), 6))
        for j in range(6):
            d = np.zeros(6)
            d[j] = h
            J[:, j] = (resid(d) - f0) / h
    S = np.diag([1, 1, 1, 1 / scale, 1 / scale, 1 / scale])
    Js = J @ S
    _, s, Vt = np.linalg.svd(Js)
    smax = s.max(initial=0.0)
    rank = int(np.sum(s > max(1e-7, 1e-6 * smax)))
    N = (S @ Vt[rank:].T)                                # 6×k null space in (v, ω)
    if N.shape[1] == 0:
        return []
    Vp, W = N[:3], N[3:]
    # translations: null-space combinations with no rotation
    uW, sW, vWt = np.linalg.svd(W)
    rr = int(np.sum(sW > 1e-9 * max(1.0, sW.max(initial=0))))
    kerW = vWt[rr:].T                                     # coefficients giving ω = 0
    T = Vp @ kerW if kerW.size else np.zeros((3, 0))
    slides = []
    if T.size:
        uT, sT, _ = np.linalg.svd(T)
        for i in range(int(np.sum(sT > 1e-9))):
            slides.append(uT[:, i])
    Tb = np.array(slides).T if slides else np.zeros((3, 0))
    joints = [{"type": "slide", "axis": a, "pos": np.asarray(base[:3])} for a in slides]
    rots = []
    for i in range(rr):
        u = uW[:, i]
        c = np.linalg.lstsq(W, u, rcond=None)[0]
        v = Vp @ c
        if Tb.size:
            v = v - Tb @ (Tb.T @ v)
        p = np.cross(u, v)                                # point on the axis: v = p × u
        rots.append((u, p, float(np.dot(u, v)), v))
    if len(rots) == 3 and not slides:                     # rotation about a point: ball joint
        A = np.vstack([-np.array([[0, -u[2], u[1]], [u[2], 0, -u[0]], [-u[1], u[0], 0]]) for u, *_ in rots])
        b = np.concatenate([v for *_, v in rots])
        centre = np.linalg.lstsq(A, b, rcond=None)[0]
        return [{"type": "ball", "axis": np.array([0, 0, 1.0]), "pos": centre}]
    for u, p, pitch, _v in rots:
        joints.append({"type": "hinge", "axis": u, "pos": p, "pitch": pitch})
    return joints


# ------------------------------------------------------------------- build ---

@dataclass
class _Built:
    xml: str
    body_of: dict                     # obj -> mujoco body name
    joints: list                      # [{"name", "obj", "type", "axis_w", "pos_w", "mate"?}]
    drives: list                      # [(Drive, joint name)]
    notes: list
    describe: list


def _quat(R):
    """Rotation matrix → MuJoCo quaternion (w x y z)."""
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    i = int(np.argmax(np.diag(R)))
    if i == 0:
        s = math.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        return [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    if i == 1:
        s = math.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        return [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    s = math.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
    return [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]


def _f(v, k=1.0):
    return " ".join(f"{float(x) * k:.9g}" for x in np.ravel(v))


def _safe(name):
    import re
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _pick_joint(cands, mate, A, B, child_is_b):
    """Joint (of a pair) that a drive on `mate` acts through, and the sign that makes positive
    drive values increase the mate's distance/angle."""
    t = mate.type
    best, score = None, -1.0
    for j in cands:
        ax = j["axis_w"]
        if A["kind"] == "axis" and t in ("concentric", "hinge", "coincident", "parallel", "angle"):
            if j["type"] != "hinge":
                continue
            sc = abs(float(np.dot(ax, A["d"])))
            sign = 1.0 if np.dot(ax, A["d"]) >= 0 else -1.0
        elif t == "angle":
            if j["type"] != "hinge":
                continue
            n = np.cross(_dirv(A), _dirv(B))
            if np.linalg.norm(n) < 1e-9:
                sc, sign = 1.0, 1.0
            else:
                n = n / np.linalg.norm(n)
                sc = abs(float(np.dot(ax, n)))
                sign = 1.0 if np.dot(ax, n) >= 0 else -1.0
        else:                                             # distance-type: slide along the normal
            if j["type"] != "slide":
                continue
            n = A["n"] if A["kind"] == "plane" else (B["n"] if B["kind"] == "plane" else B["p"] - A["p"])
            n = np.asarray(n, float) / (np.linalg.norm(n) or 1.0)
            sc = abs(float(np.dot(ax, n)))
            sign = (1.0 if np.dot(ax, n) >= 0 else -1.0) * (1.0 if child_is_b else -1.0)
        if sc > score:
            best, score = (j, sign), sc
    return best if best and score > 0.5 else None


def _dirv(r):
    return np.asarray(r["n"] if r["kind"] == "plane" else r.get("d", [0, 0, 1]), float)


def build(doc, settings: PhysicsSettings, progress=None) -> _Built:
    notes, describe = [], []
    objs = [o for o in doc.objects if doc.result.bodies.get(o.name) is not None]
    if not objs:
        raise PhysicsError("Nothing to simulate — no solid objects")
    names = {o.name for o in objs}
    poses = {o.name: list(o.pose) for o in doc.objects}
    drives = [d if isinstance(d, Drive) else Drive.from_dict(d) for d in settings.drives]
    drive_by_mate = {}
    for d in drives:
        if d.kind not in DRIVE_KINDS:
            raise PhysicsError(f"Drive kind must be one of {DRIVE_KINDS}")
        doc.mate(d.mate)                                   # raises if missing
        drive_by_mate[d.mate] = d
    mates = [m for m in doc.mates if not m.suppressed and set(m.objects()) <= names]
    couplings = [m for m in mates if m.type in ("gear", "rack")]
    skip = [m for m in mates if m.type in ("limit_distance", "limit_angle", "path", "cam", "symmetric")
            or len(set(m.objects())) != 2]
    mates = [m for m in mates if m.type not in ("gear", "rack") and m not in skip]
    refs = doc._mate_refs()
    released = {m.name for m in mates if m.name in drive_by_mate and m.type in ("distance", "angle")}
    for m in skip:
        notes_skip = f"{m.name}: {m.type.replace('_', ' ')} mates aren't simulated (contact handles limits)"
        notes.append(notes_skip)

    # --- spanning tree from the ground (fixed objects) through mated pairs
    pairs: dict[frozenset, list] = {}
    for m in mates:
        pairs.setdefault(frozenset(m.objects()), []).append(m)
    adj = {n: set() for n in names}
    for pr in pairs:
        a, b = tuple(pr)
        adj[a].add(b)
        adj[b].add(a)
    fixed = [o.name for o in objs if o.fixed]
    parent: dict[str, str | None] = {}
    order = []
    for root_set in ([fixed] if fixed else []) + [[o.name] for o in objs]:
        for r in root_set:
            if r in parent:
                continue
            parent[r] = None
            order.append(r)
            q = [r]
            while q:
                x = q.pop(0)
                for y in sorted(adj[x]):
                    if y not in parent and not (y in fixed):
                        parent[y] = x
                        order.append(y)
                        q.append(y)
    tree_pairs = {frozenset((c, p)) for c, p in parent.items() if p is not None}
    loop_pairs = [pr for pr in pairs if pr not in tree_pairs and not (set(pr) <= set(fixed))]

    # --- joints per tree pair (in world coordinates at the start pose)
    joint_specs: dict[str, list] = {}
    for c in order:
        p = parent[c]
        if p is None:
            continue
        ms = [m for m in pairs[frozenset((c, p))] if m.name not in released]
        L = max(10.0, doc.result.bodies[c].bounding_box().diagonal)
        js = allowed_motion(poses, ms, refs, c, scale=L)
        dof = sum(3 if j["type"] == "ball" else 1 for j in js)
        if dof >= 6:
            parent[c] = None                               # mates leave it free: a free body
            notes.append(f"{c}: its mates don't restrict it — simulated as a free body")
            continue
        joint_specs[c] = js
        if any(abs(j.get("pitch", 0.0)) > 1e-6 for j in js):
            notes.append(f"{c}: helical motion approximated by a hinge")

    # --- XML
    M = {n: asm.pose_matrix(poses[n]) for n in names}
    assets, worldbody, children = [], [], {n: [] for n in names}
    for c, p in parent.items():
        if p is not None:
            children[p].append(c)
    body_of = {n: _safe(n) for n in names}
    joints_out, jcount = [], 0

    def body_xml(n, indent):
        nonlocal jcount
        o = doc.obj(n)
        b = doc.result.bodies[n]
        mass, com, I = mass_props(b, doc.material_of(n).density)
        if mass <= 0:
            raise PhysicsError(f"{n} has no volume")
        p = parent[n]
        Mrel = (np.linalg.inv(M[p]) @ M[n]) if p is not None else M[n]
        pad = "  " * indent
        out = [f'{pad}<body name="{body_of[n]}" pos="{_f(Mrel[:3, 3], 1e-3)}" quat="{_f(_quat(Mrel[:3, :3]))}">']
        out.append(f'{pad}  <inertial pos="{_f(com, 1e-3)}" mass="{mass:.9g}" '
                   f'fullinertia="{_f([I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]])}"/>')
        Rn = M[n][:3, :3]
        tn = M[n][:3, 3]
        if p is None and not o.fixed:
            out.append(f'{pad}  <freejoint name="free_{body_of[n]}"/>')
            describe.append(f"{n}: free body")
        for j in joint_specs.get(n, []):
            jcount += 1
            jn = f"j{jcount}_{body_of[n]}"
            ax = Rn.T @ np.asarray(j["axis"], float)
            pos = Rn.T @ (np.asarray(j["pos"], float) - tn)
            kind = j["type"]
            out.append(f'{pad}  <joint name="{jn}" type="{kind}" axis="{_f(ax)}" pos="{_f(pos, 1e-3)}" '
                       f'armature="1e-6" damping="1e-5"/>')
            joints_out.append({"name": jn, "obj": n, "type": kind, "axis_w": np.asarray(j["axis"], float),
                               "pos_w": np.asarray(j["pos"], float), "parent": parent[n]})
        if joint_specs.get(n) is not None:
            kinds = [j["type"] for j in joint_specs[n]]
            what = ("locked to" if not kinds else
                    f"{'+'.join(sorted(set(kinds)))} ({len(kinds)} DOF) on")
            describe.append(f"{n}: {what} {parent[n]}")
        if progress:
            progress(f"Collision shapes: {n}")
        pieces = collision_pieces(b, settings.collisions, notes)
        for k, V in enumerate(pieces):
            mn = f"m_{body_of[n]}_{k}"
            assets.append(f'    <mesh name="{mn}" vertex="{_f(V, 1e-3)}" inertia="shell"/>')
            out.append(f'{pad}  <geom type="mesh" mesh="{mn}" friction="{settings.friction:.4g} 0.005 0.0001" '
                       f'margin="0" contype="1" conaffinity="1" group="3"/>')
        if not pieces:
            out.append(f'{pad}  <geom type="sphere" size="0.0001" contype="0" conaffinity="0" group="4"/>')
        for c in children[n]:
            out += body_xml(c, indent + 1)
        out.append(f"{pad}</body>")
        return out

    for n in order:
        if parent.get(n) is None:
            worldbody += body_xml(n, 2)

    # --- loop closures as equality constraints (stiffer than MuJoCo's default)
    equality = []
    EQ = f' solref="{max(0.004, 4 * settings.timestep):.4g} 1"'
    for pr in loop_pairs:
        a, b = sorted(pr)
        ms = [m for m in pairs[pr] if m.name not in released]
        L = max(10.0, doc.result.bodies[b].bounding_box().diagonal)
        js = allowed_motion(poses, ms, refs, b, scale=L)
        kinds = sorted(j["type"] for j in js)
        Ra, ta = M[a][:3, :3], M[a][:3, 3]
        loc = lambda w: Ra.T @ (np.asarray(w, float) - ta)  # noqa: E731
        if not js:
            equality.append(f'    <weld body1="{body_of[a]}" body2="{body_of[b]}"{EQ}/>')
        elif kinds == ["hinge"] or (kinds == ["hinge", "slide"] and abs(float(np.dot(
                next(x for x in js if x["type"] == "hinge")["axis"],
                next(x for x in js if x["type"] == "slide")["axis"]))) > 0.999):
            j = next(x for x in js if x["type"] == "hinge")
            if len(js) > 1:
                notes.append(f"{b}–{a}: loop closes as a hinge (sliding along the pin is held where it is)")
            for s in (-0.25, 0.25):
                pt = np.asarray(j["pos"], float) + np.asarray(j["axis"], float) * s * L
                equality.append(f'    <connect body1="{body_of[a]}" body2="{body_of[b]}" anchor="{_f(loc(pt), 1e-3)}"{EQ}/>')
        elif kinds == ["ball"]:
            equality.append(f'    <connect body1="{body_of[a]}" body2="{body_of[b]}" '
                            f'anchor="{_f(loc(js[0]["pos"]), 1e-3)}"{EQ}/>')
        else:
            notes.append(f"Loop mates between {a} and {b} ({', '.join(m.name for m in ms)}) allow "
                         f"{'+'.join(kinds)} — not representable as a loop closure; ignored")
            continue
        describe.append(f"{b}–{a}: loop closure ({'+'.join(kinds) or 'rigid'})")

    # gear / rack couplings between joints
    for m in couplings:
        a, b = m.objects()
        A = asm.world_ref(refs[(m.name, "a")], poses[a])
        B = asm.world_ref(refs[(m.name, "b")], poses[b])

        def find(obj, kind, direction):
            best, sc = None, 0.9
            for j in joints_out:
                if j["obj"] != obj or j["type"] != kind:
                    continue
                c = abs(float(np.dot(j["axis_w"], direction)))
                if c > sc:
                    best, sc = j, c
            return best
        if m.type == "gear":
            ja, jb = find(a, "hinge", A["d"]), find(b, "hinge", B["d"])
            if ja and jb:
                sa = np.sign(np.dot(ja["axis_w"], A["d"]))
                sb = np.sign(np.dot(jb["axis_w"], B["d"]))
                k = -m.value * sa * sb
                equality.append(f'    <joint joint1="{jb["name"]}" joint2="{ja["name"]}" polycoef="0 {k:.9g} 0 0 0"/>')
                describe.append(f"{a}–{b}: gear ratio {m.value:g}")
                continue
        else:
            pin, oth, P_, O_ = (a, b, A, B) if A["kind"] == "axis" else (b, a, B, A)
            u = np.cross(P_["d"], O_["n"])
            u = u / (np.linalg.norm(u) or 1.0)
            jp, js = find(pin, "hinge", P_["d"]), find(oth, "slide", u)
            if jp and js:
                sp = np.sign(np.dot(jp["axis_w"], P_["d"]))
                ss = np.sign(np.dot(js["axis_w"], u))
                k = (m.value / 1000.0) * sp * ss
                equality.append(f'    <joint joint1="{js["name"]}" joint2="{jp["name"]}" polycoef="0 {k:.9g} 0 0 0"/>')
                describe.append(f"{pin}–{oth}: rack & pinion R{m.value:g}")
                continue
        notes.append(f"{m.name}: the parts need their own hinge/slide mates for the {m.type} to drive them")

    excludes = [f'    <exclude body1="{body_of[a]}" body2="{body_of[b]}"/>' for a, b in (sorted(pr) for pr in pairs)
                if not (a in fixed and b in fixed)]

    # --- drives → actuators / springs
    drives_out, actuators = [], []
    for d in drives:
        m = doc.mate(d.mate)
        a, b = m.objects()
        child = b if parent.get(b) == a else a if parent.get(a) == b else None
        if child is None:
            notes.append(f"Drive on {d.mate} skipped: it closes a loop (drive a mate in the main chain)")
            continue
        A = asm.world_ref(refs[(m.name, "a")], poses[a])
        B = asm.world_ref(refs[(m.name, "b")], poses[b])
        cands = [j for j in joints_out if j["obj"] == child]
        pick = _pick_joint(cands, m, A, B, child_is_b=(child == b))
        if pick is None:
            notes.append(f"Drive on {d.mate} skipped: that mate leaves no matching free motion")
            continue
        j, sign = pick
        drives_out.append((d, j["name"], sign, j["type"]))
        rot = j["type"] == "hinge"
        if d.kind == "spring":
            continue
        lim = f' forcelimited="true" forcerange="{-d.max:.6g} {d.max:.6g}"' if d.max > 0 else ""
        an = f"a_{_safe(d.mate)}"
        if d.kind == "torque":
            actuators.append(f'    <motor name="{an}" joint="{j["name"]}" gear="{sign:.0f}"{lim}/>')
        elif d.kind == "speed":
            actuators.append(f'    <velocity name="{an}" joint="{j["name"]}" kv="1"{lim}/>')
        else:
            actuators.append(f'    <position name="{an}" joint="{j["name"]}" kp="1" kv="0.1"{lim}/>')

    g = settings.gravity
    xml = "\n".join([
        '<mujoco model="daedalus">',
        '  <compiler inertiafromgeom="false" angle="radian" autolimits="true"/>',
        f'  <option timestep="{settings.timestep:.6g}" gravity="{_f(g)}" integrator="implicitfast">',
        '    <flag energy="enable"/>',
        '  </option>',
        '  <asset>', *assets, '  </asset>',
        '  <worldbody>', *worldbody, '  </worldbody>',
        *(["  <equality>", *equality, "  </equality>"] if equality else []),
        *(["  <contact>", *excludes, "  </contact>"] if excludes else []),
        *(["  <actuator>", *actuators, "  </actuator>"] if actuators else []),
        "</mujoco>",
    ])
    return _Built(xml, body_of, joints_out, drives_out, notes, describe)


# ---------------------------------------------------------------- simulate ---

def _rotvec_from_quat(q):
    w, x, y, z = q
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    return asm.rotvec(R)


def simulate(doc, settings: PhysicsSettings | None = None, progress=None, cancel=None) -> SimResult:
    """Run the simulation from the current poses. progress(msg) / cancel() → bool are optional."""
    import mujoco
    settings = settings or getattr(doc, "physics", None) or PhysicsSettings()
    built = build(doc, settings, progress)
    try:
        model = mujoco.MjModel.from_xml_string(built.xml)
    except Exception as e:  # noqa: BLE001
        raise PhysicsError(f"Physics model didn't compile: {e}") from e
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)

    # drive gains from the effective inertia at each joint (stiff but stable)
    dt = settings.timestep
    for d, jn, sign, jtype in built.drives:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, jn)
        dof = model.jnt_dofadr[jid]
        Mfull = np.zeros((model.nv, model.nv))
        try:
            mujoco.mj_fullM(model, data, Mfull)                     # MuJoCo ≥ 3.4
        except TypeError:
            mujoco.mj_fullM(model, Mfull, data.qM)
        Ieff = max(float(Mfull[dof, dof]), 1e-9)
        rot = jtype == "hinge"
        if d.kind == "spring":
            k = d.value * (180 / math.pi if rot else 1000.0)
            c = d.damping * (180 / math.pi if rot else 1000.0)
            model.jnt_stiffness[jid] = k
            model.dof_damping[dof] = max(model.dof_damping[dof], c)
            continue
        aid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, f"a_{_safe(d.mate)}")
        if d.kind == "speed":
            kv = 2 * Ieff / dt          # firm speed servo; much stiffer tears loop closures apart
            model.actuator_gainprm[aid, 0] = kv
            model.actuator_biasprm[aid, 2] = -kv
        elif d.kind == "position":
            wn = min(2 * math.pi * 25, 0.2 / dt)
            kp, kv = Ieff * wn * wn, 2 * 0.9 * Ieff * wn
            model.actuator_gainprm[aid, 0] = kp
            model.actuator_biasprm[aid, 1] = -kp
            model.actuator_biasprm[aid, 2] = -kv

    def ctrl_value(d, sign, jtype):
        rot = jtype == "hinge"
        if d.kind == "speed":
            return sign * (math.radians(d.value) if rot else d.value / 1000.0)
        if d.kind == "position":
            return sign * (math.radians(d.value) if rot else d.value / 1000.0)
        return d.value                                     # torque N·m / force N (gear carries the sign)

    ramp_t = 0.15                     # motors ease in (a step command jolts linkages)
    ctrls = []
    for d, jn, sign, jtype in built.drives:
        if d.kind == "spring":
            continue
        aid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, f"a_{_safe(d.mate)}")
        ctrls.append((aid, ctrl_value(d, sign, jtype)))

    def set_ctrl(t):
        x = min(1.0, t / ramp_t)
        k = x * x * (3 - 2 * x)
        for aid, target in ctrls:
            data.ctrl[aid] = target * k

    bids = {n: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, b) for n, b in built.body_of.items()}
    every = max(1, int(round(1.0 / (settings.fps * dt))))
    steps = int(round(settings.duration / dt))
    times, frames = [], []
    traces = {"kinetic energy (J)": [], "contacts": []}
    jinfo = []
    for d, jn, sign, jtype in built.drives:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, jn)
        u = ("deg", "deg/s", "N·m") if jtype == "hinge" else ("mm", "mm/s", "N")
        jinfo.append((d, jid, sign, jtype, u))
        traces[f"{d.mate} position ({u[0]})"] = []
        traces[f"{d.mate} speed ({u[1]})"] = []
        traces[f"{d.mate} {'torque' if jtype == 'hinge' else 'force'} ({u[2]})"] = []
    moving = [n for n in bids if not doc.obj(n).fixed]
    for n in moving:
        traces[f"{n} speed (mm/s)"] = []
    start = {n: np.array(data.xpos[bids[n]]) for n in bids}
    max_speed = {n: 0.0 for n in moving}
    touching = set()
    notes = list(built.notes)
    unstable = False
    vel = np.zeros(6)

    def record(t):
        times.append(round(t, 6))
        frames.append({n: [*(data.xpos[b] * 1000.0), *_rotvec_from_quat(data.xquat[b])] for n, b in bids.items()})
        traces["kinetic energy (J)"].append(float(data.energy[1]))
        traces["contacts"].append(int(data.ncon))
        for d, jid, sign, jtype, u in jinfo:
            q = float(data.qpos[model.jnt_qposadr[jid]]) * sign
            v = float(data.qvel[model.jnt_dofadr[jid]]) * sign
            k = (180 / math.pi) if jtype == "hinge" else 1000.0
            traces[f"{d.mate} position ({u[0]})"].append(q * k)
            traces[f"{d.mate} speed ({u[1]})"].append(v * k)
            f = float(data.qfrc_actuator[model.jnt_dofadr[jid]] + data.qfrc_passive[model.jnt_dofadr[jid]]) * sign
            traces[f"{d.mate} {'torque' if jtype == 'hinge' else 'force'} ({u[2]})"].append(f)
        for n in moving:
            mujoco.mj_objectVelocity(model, data, mujoco.mjtObj.mjOBJ_BODY, bids[n], vel, 0)
            sp = float(np.linalg.norm(vel[3:])) * 1000.0
            traces[f"{n} speed (mm/s)"].append(sp)
            max_speed[n] = max(max_speed[n], sp)

    record(0.0)
    inv = {b: n for n, b in bids.items()}
    for i in range(1, steps + 1):
        set_ctrl(data.time)
        mujoco.mj_step(model, data)
        if not np.all(np.isfinite(data.qpos)) or np.abs(data.qvel).max(initial=0) > 1e4:
            unstable = True
            notes.append(f"Simulation became unstable at t = {data.time:.3f} s (overlapping parts or "
                         f"extreme speeds) — stopped")
            break
        for c in data.contact[: data.ncon]:
            b1 = model.geom_bodyid[c.geom1]
            b2 = model.geom_bodyid[c.geom2]
            if b1 in inv and b2 in inv:
                touching.add(tuple(sorted((inv[b1], inv[b2]))))
        if i % every == 0:
            record(data.time)
            if cancel and cancel():
                notes.append(f"Stopped at {data.time:.2f} s")
                break
            if progress and len(times) % 30 == 0:
                progress(f"Simulating… {data.time:.2f} / {settings.duration:.2f} s")
    if times[-1] < data.time - 1e-9 and not unstable:
        record(data.time)
    end = frames[-1]
    moved = {n: round(float(np.linalg.norm(np.array(end[n][:3]) / 1000.0 - start[n])) * 1000.0, 3) for n in moving}
    summary = {
        "duration_s": round(times[-1], 4),
        "joints": built.describe,
        "moved_mm": moved,
        "max_speed_mm_s": {n: round(v, 2) for n, v in max_speed.items()},
        "settled": bool(all((traces[f"{n} speed (mm/s)"] or [0])[-1] < 1.0 for n in moving)),
        "contacts_between": sorted(f"{a} ↔ {b}" for a, b in touching),
        "end_poses": {n: [round(float(v), 4) for v in end[n]] for n in bids},
    }
    for d, jid, sign, jtype, u in jinfo:
        key = f"{d.mate} {'torque' if jtype == 'hinge' else 'force'} ({u[2]})"
        pos = traces[f"{d.mate} position ({u[0]})"]
        summary[f"{d.mate}"] = {"kind": d.kind, "travel": f"{pos[-1] - pos[0]:.3f} {u[0]}",
                                "peak_{0}".format("torque" if jtype == "hinge" else "force"):
                                    f"{max(map(abs, traces[key]), default=0):.4g} {u[2]}"}
    return SimResult(times, frames, traces, built.describe, notes, summary, ok=not unstable)
