"""Finite-element analysis (scikit-fem, quadratic tetrahedra). Qt-free.

Units: mm, N, MPa, tonne, s, °C, W.  Material data come from core.materials.

Studies (dicts stored in the document, see sim/studies.py):
  static     linear static stress: fixtures, loads, optional thermal strain → displacement,
             von Mises, principal stresses, factor of safety, reaction forces
  frequency  natural frequencies and mode shapes (with or without fixtures)
  buckling   linear buckling load factors and mode shapes (pre-stress from the study's loads)
  thermal    steady or transient conduction: fixed temperature, convection, heat flux, heat power,
             radiation → temperature, heat flux
  harmonic   forced response from modal superposition (loads × sin ωt, damping ratio)
Fatigue lives in sim/fatigue.py (built on a static result).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from .mesh import SimMesh, mesh_solid

FIXTURES = ("fixed", "roller", "pin", "elastic")
LOADS = ("force", "pressure", "torque", "bearing", "gravity", "centrifugal")
THERMAL_BCS = ("temperature", "convection", "heat_flux", "heat_power", "radiation", "heat_volume")
SIGMA_SB = 5.670374419e-8          # W/m²K⁴


class SimError(RuntimeError):
    pass


@dataclass
class FieldResult:
    """Everything the viewer and Claude need. Vertex fields are per mesh node (P1 display)."""
    study: str
    kind: str
    p: np.ndarray                                  # (n, 3) nodes, part coordinates (mm)
    tris: np.ndarray                               # (k, 3) boundary triangles
    scalars: dict = field(default_factory=dict)    # name -> (n,) array
    vectors: dict = field(default_factory=dict)    # name -> (n, 3) (e.g. displacement)
    modes: list = field(default_factory=list)      # [{"label", "value", "shape": (n, 3)}]
    frames: list = field(default_factory=list)     # transient: [{"t", "scalars": {...}}]
    curves: dict = field(default_factory=dict)     # name -> (x list, y list, x label, y label)
    summary: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    units: dict = field(default_factory=dict)      # field name -> unit
    ms: float = 0.0
    extra: dict = field(default_factory=dict)      # solver data reused by fatigue/harmonic


# ------------------------------------------------------------------ helpers ---

class _Model:
    """Mesh + bases + face→facet lookup for one solid."""

    def __init__(self, sm: SimMesh, vector=True, order=2):
        from skfem import Basis, ElementTetP1, ElementTetP2, ElementVector, MeshTet
        self.sm = sm
        self.m = MeshTet(np.ascontiguousarray(sm.p.T), np.ascontiguousarray(sm.t))
        el = ElementTetP2() if order == 2 else ElementTetP1()
        self.e = ElementVector(el) if vector else el
        self.ib = Basis(self.m, self.e, intorder=2)
        self.b1 = self.ib.with_element(ElementTetP1())
        self.key = {tuple(sorted(f)): i for i, f in enumerate(self.m.facets.T)}
        self.lump = None

    def facets(self, faces: list[int]) -> np.ndarray:
        out = []
        for fi in faces:
            tri = self.sm.faces.get(fi)
            if tri is None:
                raise SimError(f"Face {fi} isn't on the meshed body")
            out += [self.key[tuple(sorted(t))] for t in tri if tuple(sorted(t)) in self.key]
        return np.unique(np.array(out, int))

    def fbasis(self, facets):
        from skfem import FacetBasis
        return FacetBasis(self.m, self.e, facets=facets, intorder=3)

    def area(self, facets) -> float:
        from skfem import FacetBasis, Functional
        fb = FacetBasis(self.m, self.b1.elem, facets=facets, intorder=2)
        return float(Functional(lambda w: 1.0 + 0 * w.x[0]).assemble(fb))

    def project(self, q: np.ndarray) -> np.ndarray:
        """Quadrature-point values (nel, nqp) → nodal values: the linear field through each
        element's 4 Gauss points is extrapolated to its corners, then corners are averaged
        (the usual stress-recovery that keeps surface peaks)."""
        if self.lump is None:
            X = self.ib.X                                          # (3, 4) reference Gauss points
            A = np.vstack([np.ones(X.shape[1]), X]).T              # (4 qp, 4)
            Vr = np.array([[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 0, 0, 1.0]])
            self.lump = Vr @ np.linalg.inv(A)                      # corner values = Q @ qp values
            self.count = np.bincount(self.m.t.ravel(), minlength=self.m.p.shape[1]).astype(float)
        corner = q @ self.lump.T                                   # (nel, 4)
        out = np.bincount(self.m.t.T.ravel(), weights=corner.ravel(), minlength=self.m.p.shape[1])
        return out / np.maximum(self.count, 1)

    def surface(self) -> np.ndarray:
        return outward_surface(self.m)


def outward_surface(m) -> np.ndarray:
    """Boundary triangles oriented outward (consistent normals → clean shading and edges)."""
    bf = m.boundary_facets()
    tri = m.facets[:, bf].T.copy()
    el = m.f2t[0, bf]
    P = m.p.T
    a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
    n = np.cross(b - a, c - a)
    centroid = P[m.t[:, el].T].mean(axis=1)
    flip = np.einsum("ij,ij->i", n, a - centroid) < 0
    tri[flip, 1], tri[flip, 2] = tri[flip, 2].copy(), tri[flip, 1].copy()
    return tri


def _mat(material):
    E = material.E * 1000.0                          # MPa
    nu = material.nu
    lam = E * nu / ((1 + nu) * (1 - 2 * nu))
    mu = E / (2 * (1 + nu))
    return E, nu, lam, mu


def _strain_stress(U, lam, mu, dT=None, alpha=0.0):
    g = U.grad                                       # (3, 3, nel, nqp): g[i, j] = du_i/dx_j
    eps = 0.5 * (g + g.transpose(1, 0, 2, 3))
    tr = eps[0, 0] + eps[1, 1] + eps[2, 2]
    sig = 2 * mu * eps
    for i in range(3):
        sig[i, i] = sig[i, i] + lam * tr
    if dT is not None and alpha:
        th = (3 * lam + 2 * mu) * alpha * dT
        for i in range(3):
            sig[i, i] = sig[i, i] - th
    return sig


def _von_mises(s):
    return np.sqrt(0.5 * ((s[0, 0] - s[1, 1]) ** 2 + (s[1, 1] - s[2, 2]) ** 2 + (s[2, 2] - s[0, 0]) ** 2)
                   + 3 * (s[0, 1] ** 2 + s[1, 2] ** 2 + s[0, 2] ** 2))


def _principal(s):
    S = np.moveaxis(s, (0, 1), (-2, -1))             # (nel, nqp, 3, 3)
    w = np.linalg.eigvalsh(S)
    return w[..., 2], w[..., 1], w[..., 0]          # σ1 ≥ σ2 ≥ σ3


def _unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise SimError("Direction can't be zero")
    return v / n


def _face_axis(body, fi):
    """Axis (point, dir) of a face: cylinder/cone axis, or plane normal through its centre."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    f = list(body.faces())[fi]
    gt = f.geom_type.name
    if gt in ("CYLINDER", "CONE"):
        s = BRepAdaptor_Surface(f.wrapped)
        ax = s.Cylinder().Axis() if gt == "CYLINDER" else s.Cone().Axis()
        return (np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()]),
                np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()]))
    n = f.normal_at()
    c = f.center()
    return np.array([c.X, c.Y, c.Z]), np.array([n.X, n.Y, n.Z])


# --------------------------------------------------------- stiffness & loads ---

def _assemble_structural(model, material, fixtures, loads, body, dT=None, ref_note=None):
    """K (with penalty fixtures), f, Dirichlet dofs, and per-fixture bookkeeping."""
    from skfem import BilinearForm, LinearForm, asm
    from skfem.models.elasticity import linear_elasticity
    E, nu, lam, mu = _mat(material)
    ib = model.ib
    K = asm(linear_elasticity(lam, mu), ib)
    f = np.zeros(ib.N)
    rho_t = material.density * 1e-9                 # t/mm³
    h = max(model.sm.size, 1e-3)
    kpen = 1e3 * E / h
    D = []
    fix_info = []
    for fx in fixtures:
        fac = model.facets(fx["faces"])
        if fac.size == 0:
            continue
        typ = fx["type"]
        if typ == "fixed":
            dofs = ib.get_dofs(fac).all()
            D.append(dofs)
            fix_info.append((fx, "dirichlet", dofs, None))
            continue
        fb = model.fbasis(fac)
        if typ == "roller":
            n0 = _face_normal_if_flat(fb)
            if n0 is not None and np.max(np.abs(n0)) > 1 - 1e-9:
                comp = int(np.argmax(np.abs(n0)))
                dofs = ib.get_dofs(fac).all(f"u^{comp + 1}")
                D.append(dofs)
                fix_info.append((fx, "dirichlet", dofs, None))
                continue

            @BilinearForm
            def pen(u, v, w):
                n = w.n
                return kpen * (u[0] * n[0] + u[1] * n[1] + u[2] * n[2]) * (v[0] * n[0] + v[1] * n[1] + v[2] * n[2])
            Kp = asm(pen, fb)
        elif typ == "pin":
            p0, d = _face_axis(body, fx["faces"][0])
            slide = bool(fx.get("slide", False))

            @BilinearForm
            def pen(u, v, w):
                n = w.n
                r = kpen * (u[0] * n[0] + u[1] * n[1] + u[2] * n[2]) * (v[0] * n[0] + v[1] * n[1] + v[2] * n[2])
                if not slide:
                    r = r + kpen * (u[0] * d[0] + u[1] * d[1] + u[2] * d[2]) * (v[0] * d[0] + v[1] * d[1] + v[2] * d[2])
                return r
            Kp = asm(pen, fb)
        elif typ == "elastic":
            ks = float(fx.get("stiffness", 10.0))       # N/mm per mm² of face
            normal_only = bool(fx.get("normal_only", False))

            @BilinearForm
            def pen(u, v, w):
                if normal_only:
                    n = w.n
                    return ks * (u[0] * n[0] + u[1] * n[1] + u[2] * n[2]) * (v[0] * n[0] + v[1] * n[1] + v[2] * n[2])
                return ks * (u[0] * v[0] + u[1] * v[1] + u[2] * v[2])
            Kp = asm(pen, fb)
        else:
            raise SimError(f"Unknown fixture '{typ}'. Types: {FIXTURES}")
        K = K + Kp
        fix_info.append((fx, "penalty", None, Kp))

    for ld in loads:
        typ = ld["type"]
        val = float(ld.get("value", 0.0))
        if typ in ("gravity", "centrifugal"):
            if typ == "gravity":
                g = np.asarray(ld.get("direction", [0, 0, -1.0]), float)
                g = _unit(g) * (val or 9.81) * 1000.0     # mm/s²

                @LinearForm
                def bf(v, w):
                    return rho_t * (g[0] * v[0] + g[1] * v[1] + g[2] * v[2])
            else:
                rpm = val
                om = rpm * 2 * math.pi / 60
                a0 = np.asarray(ld.get("point", [0, 0, 0]), float)
                ad = _unit(ld.get("direction", [0, 0, 1.0]))

                @LinearForm
                def bf(v, w):
                    x = w.x
                    r = [x[i] - a0[i] for i in range(3)]
                    dot = r[0] * ad[0] + r[1] * ad[1] + r[2] * ad[2]
                    rp = [r[i] - dot * ad[i] for i in range(3)]
                    return rho_t * om * om * (rp[0] * v[0] + rp[1] * v[1] + rp[2] * v[2])
            f += asm(bf, ib)
            continue
        fac = model.facets(ld["faces"])
        if fac.size == 0:
            continue
        fb = model.fbasis(fac)
        A = model.area(fac)
        if typ == "force":
            dirn = ld.get("direction", "normal")
            if isinstance(dirn, str):
                n0 = _face_normal_if_flat(fb)
                if n0 is None:
                    raise SimError("A force 'normal' to a curved face is ambiguous — give a direction")
                vec = -n0 * val                          # pushes into the face
            else:
                vec = _unit(dirn) * val
            t = vec / A

            @LinearForm
            def lf(v, w):
                return t[0] * v[0] + t[1] * v[1] + t[2] * v[2]
        elif typ == "pressure":
            @LinearForm
            def lf(v, w):
                n = w.n
                return -val * (n[0] * v[0] + n[1] * v[1] + n[2] * v[2])
        elif typ == "torque":
            p0, d = _face_axis(body, ld["faces"][0])
            if ld.get("direction") is not None:
                d = _unit(ld["direction"])
            from skfem import Functional

            def rperp(x):
                r = [x[i] - p0[i] for i in range(3)]
                dot = r[0] * d[0] + r[1] * d[1] + r[2] * d[2]
                return [r[i] - dot * d[i] for i in range(3)]
            fb1 = model.fbasis(fac)
            J = float(Functional(lambda w: sum(c * c for c in rperp(w.x))).assemble(
                __import__("skfem").FacetBasis(model.m, model.b1.elem, facets=fac, intorder=3)))
            Tq = val * 1000.0                             # N·m → N·mm
            c = Tq / max(J, 1e-12)

            @LinearForm
            def lf(v, w):
                rp = rperp(w.x)
                tx = d[1] * rp[2] - d[2] * rp[1]
                ty = d[2] * rp[0] - d[0] * rp[2]
                tz = d[0] * rp[1] - d[1] * rp[0]
                return c * (tx * v[0] + ty * v[1] + tz * v[2])
            fb = fb1
        elif typ == "bearing":
            Fv = _unit(ld.get("direction", [0, 0, -1.0])) * val
            Fh = _unit(Fv)
            from skfem import FacetBasis, Functional
            fb1 = FacetBasis(model.m, model.b1.elem, facets=fac, intorder=3)
            W = float(Functional(lambda w: np.maximum(0.0, -(w.n[0] * Fh[0] + w.n[1] * Fh[1] + w.n[2] * Fh[2])))
                      .assemble(fb1))
            if W <= 1e-12:
                raise SimError("Bearing load: the face doesn't face the load direction (use a hole/bore face)")

            @LinearForm
            def lf(v, w):
                wgt = np.maximum(0.0, -(w.n[0] * Fh[0] + w.n[1] * Fh[1] + w.n[2] * Fh[2])) / W
                return wgt * (Fv[0] * v[0] + Fv[1] * v[1] + Fv[2] * v[2])
        else:
            raise SimError(f"Unknown load '{typ}'. Types: {LOADS}")
        f += asm(lf, fb)

    if dT is not None and material.alpha:
        alpha = material.alpha * 1e-6
        dTq = dT

        @LinearForm
        def th(v, w):
            c = (3 * lam + 2 * mu) * alpha * w.dt
            return c * (v.grad[0, 0] + v.grad[1, 1] + v.grad[2, 2])
        f += asm(th, ib, dt=dTq)

    D = np.unique(np.concatenate(D)) if D else np.zeros(0, int)
    return K, f, D, fix_info


def _face_normal_if_flat(fb):
    n = fb.normals.reshape(3, -1)
    m = n.mean(axis=1)
    nm = np.linalg.norm(m)
    if nm < 1e-9:
        return None
    m = m / nm
    if np.min(m @ n) > 1 - 1e-6:
        return m
    return None


def _condense2(A, B, D):
    I = np.setdiff1d(np.arange(A.shape[0]), D)
    A, B = A.tocsr(), B.tocsr()
    return A[I][:, I], B[I][:, I], I


def _solve_linear(K, f, D):
    from scipy.sparse.linalg import spsolve
    from skfem import condense
    if K.shape[0] > 450000:
        raise SimError("Mesh too fine for the direct solver — use a larger element size")
    A, b, x, I = condense(K, f, D=D)
    try:
        xi = spsolve(A.tocsc(), b)
    except Exception as e:  # noqa: BLE001
        raise SimError(f"Solver failed: {e}") from e
    if not np.all(np.isfinite(xi)):
        raise SimError("The part isn't held: add fixtures so it can't move as a rigid body")
    x = np.zeros(K.shape[0])
    x[I] = xi
    return x


def _rigid_check(model, D, fix_info):
    if len(D) == 0 and not any(k == "penalty" for _, k, _, _ in fix_info):
        raise SimError("No fixtures — a static study needs the part held somewhere (Fixed face, roller, pin…)")


# --------------------------------------------------------------- studies ---

def _surface_vectors(model, u):
    nd = model.ib.nodal_dofs
    return np.stack([u[nd[0]], u[nd[1]], u[nd[2]]], axis=1)


def static(body, material, study, sm=None, dT_nodal=None) -> FieldResult:
    t0 = time.perf_counter()
    sm = sm or mesh_solid(body, study.get("mesh_size"))
    model = _Model(sm)
    E, nu, lam, mu = _mat(material)
    dT = None
    if dT_nodal is not None:
        dT = model.b1.interpolate(np.asarray(dT_nodal, float)).value
    K, f, D, fix = _assemble_structural(model, material, study.get("fixtures", []), study.get("loads", []), body,
                                        dT=dT)
    _rigid_check(model, D, fix)
    if not np.any(f) and dT is None:
        raise SimError("No loads — add a force, pressure, torque, gravity or temperature")
    u = _solve_linear(K, f, D)
    U = model.ib.interpolate(u)
    sig = _strain_stress(U, lam, mu, dT, material.alpha * 1e-6 if dT is not None else 0.0)
    vm_q = _von_mises(sig)
    s1, s2, s3 = _principal(sig)
    vm = np.maximum(model.project(vm_q), 0.0)          # corner extrapolation can dip below zero
    p1, p3 = model.project(s1), model.project(s3)
    disp = _surface_vectors(model, u)
    dmag = np.linalg.norm(disp, axis=1)
    strength = material.yield_ if material.ductile else material.uts
    if material.ductile:
        fos = strength / np.maximum(vm, 1e-9)
    else:
        fos = material.uts / np.maximum(p1, 1e-9)
    # reactions
    r = K @ u - f
    reactions = []
    for fx, kind, dofs, Kp in fix:
        if kind == "dirichlet":
            rr = np.zeros(model.ib.N)
            rr[dofs] = r[dofs]
        else:
            rr = Kp @ u
        R = np.array([rr[_comp_dofs(model, i)].sum() for i in range(3)])
        reactions.append({"fixture": fx.get("name", fx["type"]), "Fx": round(float(R[0]), 4),
                          "Fy": round(float(R[1]), 4), "Fz": round(float(R[2]), 4),
                          "resultant_N": round(float(np.linalg.norm(R)), 4)})
    i_max = int(np.argmax(vm))
    mass = body.volume * material.density * 1e-6
    res = FieldResult(study["name"], "static", sm.p, model.surface())
    res.scalars = {"von Mises stress": vm, "displacement": dmag, "factor of safety": np.minimum(fos, 1e6),
                   "max principal stress": p1, "min principal stress": p3}
    res.units = {"von Mises stress": "MPa", "displacement": "mm", "factor of safety": "",
                 "max principal stress": "MPa", "min principal stress": "MPa"}
    res.vectors = {"displacement": disp}
    vm_peak = float(vm_q.max())
    res.summary = {
        "material": material.name, "nodes": sm.n_nodes, "elements": sm.n_elems, "dofs": int(model.ib.N),
        "max_von_mises_MPa": round(float(vm.max()), 4), "max_von_mises_at": [round(float(c), 3) for c in sm.p[i_max]],
        "peak_von_mises_gauss_MPa": round(vm_peak, 4),
        "max_displacement_mm": round(float(dmag.max()), 6),
        "min_factor_of_safety": round(float(strength / max(vm.max(), 1e-9)) if material.ductile else
                                      float(material.uts / max(p1.max(), 1e-9)), 3),
        "strength_used_MPa": strength, "criterion": "von Mises vs yield" if material.ductile else
        "max principal vs UTS (brittle)",
        "reactions": reactions, "mass_kg": round(mass, 5),
    }
    if res.summary["min_factor_of_safety"] < 1:
        res.notes.append("Factor of safety below 1: the part yields under this load")
    if dmag.max() > 0.05 * body.bounding_box().diagonal:
        res.notes.append("Large displacement (>5 % of size): linear results are approximate")
    if material.note:
        res.notes.append(material.note)
    res.extra = {"model": model, "u": u, "sig_q": sig, "vm_q": vm_q, "K": K, "f": f, "D": D, "lam": lam, "mu": mu}
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def _comp_dofs(model, comp):
    """All dofs (vertex + mid-edge) of displacement component comp."""
    if not hasattr(model, "_comp"):
        model._comp = [model.ib.get_dofs().all(f"u^{i + 1}") for i in range(3)]
    return model._comp[comp]


def frequency(body, material, study, sm=None) -> FieldResult:
    from scipy.sparse.linalg import eigsh
    from skfem import BilinearForm, asm, condense
    t0 = time.perf_counter()
    sm = sm or mesh_solid(body, study.get("mesh_size"))
    model = _Model(sm)
    K, _, D, fix = _assemble_structural(model, material, study.get("fixtures", []), [], body)
    rho_t = material.density * 1e-9

    @BilinearForm
    def mass(u, v, w):
        return rho_t * (u[0] * v[0] + u[1] * v[1] + u[2] * v[2])
    M = asm(mass, model.ib)
    n = int(study.get("modes", 6))
    free = len(D) == 0 and not any(k == "penalty" for _, k, _, _ in fix)
    if free:
        n_req = n + 6
    else:
        n_req = n
    Kc, Mc, I = _condense2(K, M, D)
    try:
        w2, V = eigsh(Kc.tocsc(), k=min(n_req, Kc.shape[0] - 2), M=Mc.tocsc(), sigma=-1.0, which="LM")
    except Exception as e:  # noqa: BLE001
        raise SimError(f"Eigen solver failed: {e}") from e
    order = np.argsort(w2)
    w2, V = w2[order], V[:, order]
    if free:
        keep = np.where(w2 > (2 * math.pi * 0.5) ** 2 * 1e-0)[0]
        w2, V = w2[keep][:n], V[:, keep][:, :n]
    freqs = np.sqrt(np.maximum(w2, 0)) / (2 * math.pi)
    res = FieldResult(study["name"], "frequency", sm.p, model.surface())
    total_mass = body.volume * material.density * 1e-6
    for i, fHz in enumerate(freqs):
        full = np.zeros(K.shape[0])
        full[I] = V[:, i]
        shape = _surface_vectors(model, full)
        shape = shape / max(np.abs(shape).max(), 1e-30)
        res.modes.append({"label": f"Mode {i + 1}: {fHz:.2f} Hz", "value": float(fHz), "shape": shape})
    res.summary = {"material": material.name, "nodes": sm.n_nodes, "elements": sm.n_elems,
                   "frequencies_Hz": [round(float(x), 3) for x in freqs], "free_free": free,
                   "mass_kg": round(total_mass, 5)}
    if free:
        res.notes.append("No fixtures: free-free modes (6 rigid-body modes skipped)")
    if res.modes:
        res.scalars = {"mode 1 amplitude": np.linalg.norm(res.modes[0]["shape"], axis=1)}
        res.vectors = {"mode 1": res.modes[0]["shape"]}
    res.extra = {"model": model, "K": K, "M": M, "D": D, "w2": w2, "V": V, "I": I}
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def buckling(body, material, study, sm=None) -> FieldResult:
    from scipy.sparse.linalg import eigsh
    from skfem import BilinearForm, asm, condense
    t0 = time.perf_counter()
    st = static(body, material, study, sm)
    model = st.extra["model"]
    S = st.extra["sig_q"]

    @BilinearForm
    def kg(u, v, w):
        Sg = w.S
        acc = 0.0
        for i in range(3):
            for j in range(3):
                acc = acc + Sg[i, j] * (u.grad[0, i] * v.grad[0, j] + u.grad[1, i] * v.grad[1, j]
                                        + u.grad[2, i] * v.grad[2, j])
        return acc
    KG = asm(kg, model.ib, S=S)
    K, D = st.extra["K"], st.extra["D"]
    Kc, KGc, I = _condense2(K, KG, D)
    n = int(study.get("modes", 4))
    try:
        mu_, V = eigsh(KGc.tocsc(), k=min(max(2 * n, 6), Kc.shape[0] - 2), M=Kc.tocsc(), which="LM")
    except Exception as e:  # noqa: BLE001
        raise SimError(f"Buckling eigen solve failed: {e}") from e
    lam_ = -1.0 / mu_
    order = np.argsort(np.where(lam_ > 0, lam_, np.inf))
    res = FieldResult(study["name"], "buckling", st.p, st.tris)
    for i in order[:n]:
        if not np.isfinite(lam_[i]) or lam_[i] <= 0:
            continue
        full = np.zeros(K.shape[0])
        full[I] = V[:, i]
        shape = _surface_vectors(model, full)
        shape = shape / max(np.abs(shape).max(), 1e-30)
        res.modes.append({"label": f"Mode {len(res.modes) + 1}: load factor {lam_[i]:.3f}", "value": float(lam_[i]),
                          "shape": shape})
    if not res.modes:
        res.notes.append("No buckling under these loads (only tension) — reverse the load to check compression")
    res.summary = {"material": material.name, "load_factors": [round(m["value"], 4) for m in res.modes],
                   "nodes": st.summary["nodes"], "elements": st.summary["elements"],
                   "static_max_von_mises_MPa": st.summary["max_von_mises_MPa"]}
    if res.modes:
        lf = res.modes[0]["value"]
        res.summary["buckling_safety"] = ("buckles at %.3g × the applied load" % lf)
        if lf < 1:
            res.notes.append("Load factor < 1: buckles before the applied load is reached")
        res.vectors = {"mode 1": res.modes[0]["shape"]}
        res.scalars = {"mode 1 amplitude": np.linalg.norm(res.modes[0]["shape"], axis=1)}
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def thermal(body, material, study, sm=None) -> FieldResult:
    from scipy.sparse.linalg import spsolve
    from skfem import (Basis, BilinearForm, ElementTetP2, FacetBasis, LinearForm, MeshTet, asm, condense)
    t0 = time.perf_counter()
    sm = sm or mesh_solid(body, study.get("mesh_size"))
    m = MeshTet(np.ascontiguousarray(sm.p.T), np.ascontiguousarray(sm.t))
    e = ElementTetP2()
    ib = Basis(m, e, intorder=2)
    helper = _Model.__new__(_Model)
    helper.sm, helper.m, helper.e, helper.ib = sm, m, e, ib
    from skfem import ElementTetP1
    helper.b1 = ib.with_element(ElementTetP1())
    helper.key = {tuple(sorted(f)): i for i, f in enumerate(m.facets.T)}
    helper.lump = None
    k = material.k * 1e-3                            # W/(mm·K)
    rc = material.density * 1e-6 * material.cp       # J/(mm³·K)

    @BilinearForm
    def cond(u, v, w):
        return k * (u.grad[0] * v.grad[0] + u.grad[1] * v.grad[1] + u.grad[2] * v.grad[2])
    Kt = asm(cond, ib)
    f = np.zeros(ib.N)
    D, Dval = [], []
    rad = []
    bcs = study.get("thermal", [])
    if not bcs:
        raise SimError("Add a thermal boundary condition (temperature, convection, heat flux, heat power)")
    V = body.volume
    for bc in bcs:
        typ = bc["type"]
        val = float(bc.get("value", 0.0))
        if typ == "heat_volume":
            q = val / V                               # W/mm³

            @LinearForm
            def src(v, w):
                return q * v
            f += asm(src, ib)
            continue
        fac = helper.facets(bc["faces"])
        if fac.size == 0:
            continue
        fb = FacetBasis(m, e, facets=fac, intorder=3)
        if typ == "temperature":
            dofs = ib.get_dofs(fac).all()
            D.append(dofs)
            Dval.append(np.full(len(dofs), val))
        elif typ == "convection":
            hh = float(bc.get("h", 25.0)) * 1e-6       # W/(mm²K)
            Tinf = float(bc.get("ambient", 20.0))

            @BilinearForm
            def cv(u, v, w):
                return hh * u * v

            @LinearForm
            def cvf(v, w):
                return hh * Tinf * v
            Kt = Kt + asm(cv, fb)
            f += asm(cvf, fb)
        elif typ in ("heat_flux", "heat_power"):
            q = val * 1e-6 if typ == "heat_flux" else val / helper.area(fac)

            @LinearForm
            def qf(v, w):
                return q * v
            f += asm(qf, fb)
        elif typ == "radiation":
            rad.append((fb, float(bc.get("emissivity", 0.9)), float(bc.get("ambient", 20.0))))
        else:
            raise SimError(f"Unknown thermal condition '{typ}'. Types: {THERMAL_BCS}")
    Dall = np.concatenate(D) if D else np.zeros(0, int)
    xD = np.zeros(ib.N)
    if D:
        xD[Dall] = np.concatenate(Dval)
    if not D and not any(b["type"] in ("convection", "radiation") for b in bcs):
        raise SimError("Heat has nowhere to go: add a fixed temperature, convection or radiation")

    def steady(Kx, fx):
        if len(Dall):
            A, b, x, I = condense(Kx, fx, x=xD, D=Dall)
            T = xD.copy()
            T[I] = spsolve(A.tocsc(), b)
            return T
        return spsolve(Kx.tocsc(), fx)

    def rad_terms(T):
        Kr = None
        fr = np.zeros(ib.N)
        for fb, eps, Ta in rad:
            Tq = fb.interpolate(T)
            TK = Tq.value + 273.15
            TaK = Ta + 273.15
            hr = eps * SIGMA_SB * (TK ** 2 + TaK ** 2) * (TK + TaK) * 1e-6

            @BilinearForm
            def rk(u, v, w):
                return w.hr * u * v

            @LinearForm
            def rf(v, w):
                return w.hr * Ta * v
            A = asm(rk, fb, hr=hr)
            Kr = A if Kr is None else Kr + A
            fr += asm(rf, fb, hr=hr)
        return Kr, fr

    res = FieldResult(study["name"], "thermal", sm.p, helper.surface())
    tr = study.get("transient")
    if tr:
        @BilinearForm
        def cap(u, v, w):
            return rc * u * v
        C = asm(cap, ib)
        dur = float(tr.get("duration", 60.0))
        steps = int(tr.get("steps", 60))
        dt = dur / steps
        T = np.full(ib.N, float(tr.get("initial", 20.0)))
        if len(Dall):
            T[Dall] = xD[Dall]
        nd = ib.nodal_dofs[0]
        times, tmax, tmin, tavg = [0.0], [T[nd].max()], [T[nd].min()], [T[nd].mean()]
        res.frames.append({"t": 0.0, "scalars": {"temperature": T[nd].copy()}})
        for s in range(1, steps + 1):
            Kx, fx = Kt, f
            if rad:
                Kr, fr = rad_terms(T)
                Kx, fx = Kt + Kr, f + fr
            A = C / dt + Kx
            b = C @ T / dt + fx
            T = steady(A, b)
            times.append(s * dt)
            tmax.append(T[nd].max())
            tmin.append(T[nd].min())
            tavg.append(T[nd].mean())
            if s % max(1, steps // 30) == 0 or s == steps:
                res.frames.append({"t": s * dt, "scalars": {"temperature": T[nd].copy()}})
        res.curves = {"max temperature": (times, tmax, "time (s)", "°C"),
                      "min temperature": (times, tmin, "time (s)", "°C"),
                      "average temperature": (times, tavg, "time (s)", "°C")}
    else:
        T = steady(Kt, f)
        for _ in range(25 if rad else 0):
            Kr, fr = rad_terms(T)
            Tn = steady(Kt + Kr, f + fr)
            if np.max(np.abs(Tn - T)) < 1e-4:
                T = Tn
                break
            T = Tn
    nd = ib.nodal_dofs[0]
    G = ib.interpolate(T).grad
    qmag = k * np.sqrt(G[0] ** 2 + G[1] ** 2 + G[2] ** 2) * 1e6       # W/m²
    res.scalars = {"temperature": T[nd], "heat flux": helper.project(qmag)}
    res.units = {"temperature": "°C", "heat flux": "W/m²"}
    res.summary = {"material": material.name, "nodes": sm.n_nodes, "elements": sm.n_elems,
                   "max_temperature_C": round(float(T[nd].max()), 3), "min_temperature_C": round(float(T[nd].min()), 3),
                   "max_heat_flux_W_m2": round(float(res.scalars["heat flux"].max()), 2),
                   "transient": bool(tr)}
    res.extra = {"T_nodal": T[nd]}
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def harmonic(body, material, study, sm=None) -> FieldResult:
    """Steady-state response to loads × sin(2πft) by modal superposition (damping ratio ζ)."""
    t0 = time.perf_counter()
    nmod = int(study.get("modes", 12))
    fr = frequency(body, material, {**study, "modes": nmod}, sm)
    model = fr.extra["model"]
    K, M, D, I = fr.extra["K"], fr.extra["M"], fr.extra["D"], fr.extra["I"]
    _, f, _, _ = _assemble_structural(model, material, [], study.get("loads", []), body)
    if not np.any(f):
        raise SimError("Harmonic study needs loads (their amplitudes)")
    w2, V = fr.extra["w2"], fr.extra["V"]
    Phi = np.zeros((K.shape[0], V.shape[1]))
    Phi[I] = V
    # mass-normalise
    for i in range(Phi.shape[1]):
        Phi[:, i] /= math.sqrt(max(Phi[:, i] @ (M @ Phi[:, i]), 1e-300))
    q = Phi.T @ f
    zeta = float(study.get("damping", 0.02))
    fmin, fmax = float(study.get("fmin", 1.0)), float(study.get("fmax", max(fr.summary["frequencies_Hz"] or [100]) * 1.3))
    freqs = np.linspace(fmin, fmax, int(study.get("points", 300)))
    wn = np.sqrt(np.maximum(w2, 0))
    nd = model.ib.nodal_dofs
    # track the node with the biggest static-like response
    amp = []
    for fq in freqs:
        w = 2 * math.pi * fq
        H = q / (wn ** 2 - w ** 2 + 2j * zeta * wn * w)
        u = Phi @ H
        U = np.sqrt(np.abs(u[nd[0]]) ** 2 + np.abs(u[nd[1]]) ** 2 + np.abs(u[nd[2]]) ** 2)
        amp.append(float(U.max()))
    ip = int(np.argmax(amp))
    w = 2 * math.pi * freqs[ip]
    H = q / (wn ** 2 - w ** 2 + 2j * zeta * wn * w)
    u = Phi @ H
    E, nu, lam, mu = _mat(material)
    vre = _von_mises(_strain_stress(model.ib.interpolate(u.real), lam, mu))
    vim = _von_mises(_strain_stress(model.ib.interpolate(u.imag), lam, mu))
    vm = np.maximum(model.project(np.sqrt(vre ** 2 + vim ** 2)), 0.0)
    disp = np.abs(np.stack([u[nd[0]], u[nd[1]], u[nd[2]]], axis=1))
    res = FieldResult(study["name"], "harmonic", fr.p, fr.tris)
    res.scalars = {"stress amplitude at peak": vm, "displacement amplitude at peak": np.linalg.norm(disp, axis=1)}
    res.units = {"stress amplitude at peak": "MPa", "displacement amplitude at peak": "mm"}
    res.vectors = {"displacement": np.real(np.stack([u[nd[0]], u[nd[1]], u[nd[2]]], axis=1))}
    res.curves = {"max displacement amplitude": (list(map(float, freqs)), amp, "frequency (Hz)", "mm")}
    res.modes = fr.modes
    res.summary = {"material": material.name, "natural_frequencies_Hz": fr.summary["frequencies_Hz"],
                   "damping_ratio": zeta, "peak_frequency_Hz": round(float(freqs[ip]), 3),
                   "peak_displacement_mm": round(amp[ip], 6), "peak_stress_MPa": round(float(vm.max()), 4)}
    res.ms = (time.perf_counter() - t0) * 1000
    return res
