"""Simulation follow-ups: assembly statics (bonded parts, bolt connectors), drop test (implicit dynamics with
floor contact), topology optimisation (SIMP) and design studies (parameter sweeps). Qt-free."""
from __future__ import annotations

import math
import os
import tempfile
import time

import numpy as np

from .fea import FieldResult, SimError, _mat, _principal, _von_mises, outward_surface
from .mesh import MeshError, SimMesh, _lock, _orient, default_size

G = 9810.0                      # mm/s²


# ---------------------------------------------------------------- meshing ---

def mesh_assembly(bodies: dict, size=None, bonded=True):
    """Mesh several placed solids together. bonded: shared interfaces are fused (gmsh fragment) so the mesh is
    conformal; otherwise each part is meshed on its own. Returns (SimMesh, elem_obj index array, names)."""
    import gmsh
    from build123d import export_brep
    names = list(bodies)
    size = float(size or min(default_size(b) for b in bodies.values()))
    paths = []
    for n in names:
        fd, p = tempfile.mkstemp(suffix=".brep")
        os.close(fd)
        export_brep(bodies[n], p)
        paths.append(p)
    with _lock:
        gmsh.initialize(interruptible=False)
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("General.NumThreads", 1)
            gmsh.model.add("asm")
            owner = {}
            ins = []
            for i, p in enumerate(paths):
                tags = gmsh.model.occ.importShapes(p)
                for d, t in tags:
                    if d == 3:
                        ins.append((3, t))
                        owner[t] = i
            gmsh.model.occ.synchronize()
            if bonded and len(ins) > 1:
                out, omap = gmsh.model.occ.fragment(ins, [])
                gmsh.model.occ.synchronize()
                new_owner = {}
                for (d, t), children in zip(ins, omap):
                    for cd, ct in children:
                        if cd == 3 and ct not in new_owner:
                            new_owner[ct] = owner[t]
                owner = new_owner
            gmsh.option.setNumber("Mesh.MeshSizeMax", size)
            gmsh.option.setNumber("Mesh.MeshSizeMin", size / 8)
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
            gmsh.option.setNumber("Mesh.Algorithm3D", 1)
            gmsh.option.setNumber("Mesh.Optimize", 1)
            try:
                gmsh.model.mesh.generate(3)
            except Exception as e:  # noqa: BLE001
                raise MeshError(f"Meshing failed: {e}") from e
            tags, xyz, _ = gmsh.model.mesh.getNodes()
            idx = np.full(int(tags.max()) + 1, -1, int)
            idx[tags.astype(int)] = np.arange(len(tags))
            P = xyz.reshape(-1, 3)
            tets, eo = [], []
            for _, vt in gmsh.model.getEntities(3):
                et, _, en = gmsh.model.mesh.getElements(3, vt)
                for typ, nodes in zip(et, en):
                    if typ == 4:
                        tt = nodes.reshape(-1, 4)
                        tets.append(tt)
                        eo.append(np.full(len(tt), owner.get(vt, 0)))
            if not tets:
                raise MeshError("No tetrahedra produced")
            T = idx[np.vstack(tets).astype(int)]
            eo = np.concatenate(eo)
        finally:
            gmsh.finalize()
            for p in paths:
                os.unlink(p)
    used = np.unique(T)
    remap = np.full(len(P), -1, int)
    remap[used] = np.arange(len(used))
    T = _orient(P[used], remap[T])
    return SimMesh(P[used], T.T.copy(), {}, size), eo, names


def facets_on_face(m, face, tol):
    """Boundary facets of a skfem mesh lying on a B-rep face (placed coordinates)."""
    from build123d import Vector
    bf = m.boundary_facets()
    P = m.p.T
    tri = m.facets[:, bf].T
    cen = P[tri].mean(axis=1)
    bb = face.bounding_box()
    lo = np.array([bb.min.X, bb.min.Y, bb.min.Z]) - tol
    hi = np.array([bb.max.X, bb.max.Y, bb.max.Z]) + tol
    cand = np.nonzero(np.all((cen >= lo) & (cen <= hi), axis=1))[0]
    keep = []
    for i in cand:
        try:
            if face.distance_to(Vector(*cen[i])) <= tol:
                keep.append(bf[i])
        except Exception:  # noqa: BLE001
            continue
    return np.array(keep, int)


def _elastic_form():
    from skfem import BilinearForm
    from skfem.helpers import ddot, sym_grad, trace

    @BilinearForm
    def elast(u, v, w):
        eu, ev = sym_grad(u), sym_grad(v)
        return w.lam * trace(eu) * trace(ev) + 2.0 * w.mu * ddot(eu, ev)
    return elast


def _fields(ib, per_elem):
    """Per-element values → (nel, nqp) quadrature arrays."""
    return np.repeat(np.asarray(per_elem, float)[:, None], ib.X.shape[1], axis=1)


def _loads_vector(ib, m, loads, faces_of, rho_e, tol):
    from skfem import FacetBasis, LinearForm, asm
    f = np.zeros(ib.N)
    for ld in loads:
        typ = ld["type"]
        if typ == "gravity":
            g = np.asarray(ld.get("direction") or [0, 0, -1], float)
            g = g / np.linalg.norm(g) * float(ld.get("value") or 9.81) * 1000.0
            rho = _fields(ib, rho_e)

            @LinearForm
            def bf(v, w):
                return w.rho * (g[0] * v[0] + g[1] * v[1] + g[2] * v[2])
            f += asm(bf, ib, rho=rho)
            continue
        fac = faces_of(ld)
        if fac.size == 0:
            raise SimError(f"{ld.get('name', typ)}: no mesh facets on its faces")
        fb = FacetBasis(m, ib.elem, facets=fac, intorder=3)
        if typ == "force":
            from skfem import ElementTetP1, Functional
            area = float(Functional(lambda w: 1.0 + 0 * w.x[0]).assemble(
                FacetBasis(m, ElementTetP1(), facets=fac, intorder=2)))
            d = ld.get("direction")
            if d is None or d == "normal":
                raise SimError("Assembly forces need a direction")
            d = np.asarray(d, float)
            tr = d / np.linalg.norm(d) * float(ld["value"]) / max(area, 1e-12)

            @LinearForm
            def lf(v, w):
                return tr[0] * v[0] + tr[1] * v[1] + tr[2] * v[2]
            f += asm(lf, fb)
        elif typ == "pressure":
            p = float(ld["value"])

            @LinearForm
            def lp(v, w):
                return -p * (w.n[0] * v[0] + w.n[1] * v[1] + w.n[2] * v[2])
            f += asm(lp, fb)
        else:
            raise SimError(f"Assembly studies take force, pressure and gravity loads (not {typ})")
    return f


def _comp_vec(ib, fac, comp, n):
    dofs = ib.get_dofs(fac).all(f"u^{comp + 1}")
    c = np.zeros(n)
    c[dofs] = 1.0 / len(dofs)
    return c


# ---------------------------------------------------------- assembly static ---

BOLT_PROOF = {"8.8": 600.0, "10.9": 830.0, "12.9": 970.0, "A2-70": 450.0}


def assembly_static(doc, st, progress=None):
    """Static stress over several objects: bonded interfaces (or free parts joined only by bolt connectors)."""
    from scipy.sparse import csr_matrix
    from skfem import Basis, ElementTetP1, ElementTetP2, ElementVector, MeshTet, asm, condense
    from ..core import assembly as asmod
    from .studies import resolve_faces
    t0 = time.perf_counter()
    opts = st.get("options", {})
    placed = doc.placed_bodies()
    names = [n.strip() for n in str(opts.get("objects", "")).split(",") if n.strip()] or \
        [o.name for o in doc.objects if o.visible and o.name in placed]
    bodies = {n: placed[n] for n in names if n in placed}
    if len(bodies) < 1:
        raise SimError("No bodies to analyse")
    bonded = opts.get("contact", "bonded") == "bonded"
    if progress:
        progress(f"{st['name']}: meshing {len(bodies)} parts…")
    try:
        sm, eo, order = mesh_assembly(bodies, st.get("mesh_size"), bonded)
    except MeshError as e:
        raise SimError(str(e)) from e
    if progress:
        progress(f"{st['name']}: solving ({sm.n_elems:,} elements)…")
    m = MeshTet(np.ascontiguousarray(sm.p.T), np.ascontiguousarray(sm.t))
    ib = Basis(m, ElementVector(ElementTetP2()), intorder=2)
    mats = [doc.material_of(n) for n in order]
    lam_e = np.array([_mat(mats[i])[2] for i in eo])
    mu_e = np.array([_mat(mats[i])[3] for i in eo])
    rho_e = np.array([mats[i].density * 1e-9 for i in eo])
    K = asm(_elastic_form(), ib, lam=_fields(ib, lam_e), mu=_fields(ib, mu_e))
    tol = max(1e-3, sm.size * 0.02)

    def faces_of(item):
        obj = item.get("object") or st["object"]
        if obj not in bodies:
            raise SimError(f"{item.get('name', item['type'])}: {obj} isn't in this study")
        fis = resolve_faces(doc.result.bodies[obj], item["refs"])
        pf = list(bodies[obj].faces())
        out = [facets_on_face(m, pf[i], tol) for i in fis]
        return np.unique(np.concatenate(out)) if out else np.array([], int)

    D = []
    for fx in st.get("fixtures", []):
        if fx.get("suppressed"):
            continue
        if fx["type"] != "fixed":
            raise SimError("Assembly studies use Fixed fixtures")
        fac = faces_of(fx)
        if fac.size:
            D.append(ib.get_dofs(fac).all())
    if not D:
        raise SimError("No fixtures — fix a face on one of the parts")
    D = np.unique(np.concatenate(D))
    loads = [ld for ld in st.get("loads", []) if not ld.get("suppressed")]
    for ld in loads:                                     # world directions as given (placed coordinates)
        pass
    f = _loads_vector(ib, m, loads, faces_of, rho_e, tol)
    # bolts: axial + shear springs between the mean displacements of the two hole surfaces
    bolts = []
    E_bolt = 205000.0
    for b in st.get("connectors", []):
        if b.get("suppressed"):
            continue
        fa = faces_of({"object": b["a_object"], "refs": b["a_refs"], "type": "bolt"})
        fb_ = faces_of({"object": b["b_object"], "refs": b["b_refs"], "type": "bolt"})
        if not fa.size or not fb_.size:
            raise SimError(f"{b['name']}: hole faces not found")
        ra = asmod.resolve_ref(doc.result.bodies[b["a_object"]], b["a_refs"][0])
        Ma = asmod.pose_matrix(doc.obj(b["a_object"]).pose)
        ax = Ma[:3, :3] @ np.asarray(ra.get("d", [0, 0, 1]), float)
        ax = ax / np.linalg.norm(ax)
        d = float(b.get("diameter") or 2 * float(ra.get("r", 4.0)) * 0.95)
        L = float(b.get("grip") or 20.0)
        A = math.pi * d * d / 4
        k_ax = E_bolt * A / L
        k_sh = 0.5 * k_ax
        cs = [_comp_vec(ib, fa, c, ib.N) - _comp_vec(ib, fb_, c, ib.N) for c in range(3)]
        c_ax = sum(ax[c] * cs[c] for c in range(3))
        rows = []
        for vec, k in [(c_ax, k_ax)] + [(cs[c], k_sh) for c in range(3)]:
            nz = np.nonzero(vec)[0]
            v = vec[nz]
            rows.append(csr_matrix((k * np.outer(v, v).ravel(), (np.repeat(nz, len(nz)), np.tile(nz, len(nz)))),
                                   shape=K.shape))
        Kb = rows[0] + rows[1] + rows[2] + rows[3] - csr_matrix(
            (k_sh * np.outer(c_ax[np.nonzero(c_ax)[0]], c_ax[np.nonzero(c_ax)[0]]).ravel(),
             (np.repeat(np.nonzero(c_ax)[0], len(np.nonzero(c_ax)[0])),
              np.tile(np.nonzero(c_ax)[0], len(np.nonzero(c_ax)[0])))), shape=K.shape)
        K = K + Kb
        bolts.append((b, cs, c_ax, ax, k_ax, k_sh, d))
    from scipy.sparse.linalg import spsolve
    Kc, fc, x, I = condense(K.tocsr(), f, D=D)
    try:
        xi = spsolve(Kc.tocsc(), fc)
    except Exception as e:  # noqa: BLE001
        raise SimError(f"Solver failed: {e}") from e
    if not np.all(np.isfinite(xi)) or np.abs(xi).max() > 1e6:
        raise SimError("A part isn't held — bond it, bolt it or fix it")
    u = np.zeros(ib.N)
    u[I] = xi
    U = ib.interpolate(u)
    sig = _strain_stress_fields(U, _fields(ib, lam_e), _fields(ib, mu_e))
    vm_q = _von_mises(sig)
    s1, _, _ = _principal(sig)
    vm = np.maximum(_project(ib, m, vm_q), 0)
    nd = ib.nodal_dofs
    disp = np.stack([u[nd[0]], u[nd[1]], u[nd[2]]], axis=1)
    dmag = np.linalg.norm(disp, axis=1)
    strength_e = np.array([mats[i].yield_ if mats[i].ductile else mats[i].uts for i in eo])
    strength_n = np.full(m.p.shape[1], np.inf)
    for k in range(4):
        np.minimum.at(strength_n, m.t[k], strength_e)
    fos = strength_n / np.maximum(vm, 1e-9)
    per = {}
    vm_e = vm_q.max(axis=1)
    for i, n in enumerate(order):
        sel = eo == i
        if sel.any():
            per[n] = {"material": mats[i].name, "max_von_mises_MPa": round(float(vm_e[sel].max()), 3),
                      "min_factor_of_safety": round(float((mats[i].yield_ if mats[i].ductile else mats[i].uts)
                                                          / max(vm_e[sel].max(), 1e-9)), 3)}
    bolt_out = []
    for b, cs, c_ax, ax, k_ax, k_sh, d in bolts:
        g = np.array([cs[c] @ u for c in range(3)])
        ga = float(c_ax @ u)
        shear = np.linalg.norm(g - ga * ax) * k_sh
        axial = -ga * k_ax                                   # positive = tension (holes pulled apart)
        P = float(b.get("preload") or 0.0)
        As = 0.7854 * (d - 0.9382 * max(d * 0.15, 0.5)) ** 2
        grade = b.get("grade", "8.8")
        proof = BOLT_PROOF.get(grade, 600.0)
        tension = P + max(axial, 0.0) * (0.2 if P > 0 else 1.0)   # joint stiffness ratio ~0.2 when preloaded
        sf = proof * As / max(math.hypot(tension, 1.7 * shear), 1e-9)
        bolt_out.append({"name": b["name"], "axial_N": round(axial, 2), "shear_N": round(float(shear), 2),
                         "preload_N": P, "bolt_tension_N": round(tension, 2), "grade": grade,
                         "factor_of_safety": round(sf, 3)})
    res = FieldResult(st["name"], "static", sm.p, outward_surface(m))
    res.scalars = {"von Mises stress": vm, "displacement": dmag, "factor of safety": np.minimum(fos, 1e6),
                   "max principal stress": np.maximum(_project(ib, m, s1), -1e9),
                   "part": eo_nodes(m, eo).astype(float)}
    res.units = {"von Mises stress": "MPa", "displacement": "mm", "factor of safety": "",
                 "max principal stress": "MPa", "part": ""}
    res.vectors = {"displacement": disp}
    res.summary = {"nodes": sm.n_nodes, "elements": sm.n_elems, "parts": order, "contact": "bonded" if bonded
                   else "free (bolts only)", "max_von_mises_MPa": round(float(vm.max()), 4),
                   "max_displacement_mm": round(float(dmag.max()), 6),
                   "min_factor_of_safety": round(float(np.min(fos)), 3), "per_part": per, "bolts": bolt_out,
                   "material": ", ".join(sorted({m_.name for m_ in mats}))}
    res.notes.append("Assembly study: parts bonded at touching faces" if bonded else
                     "Parts are joined only by the bolt connectors (no contact)")
    if bolts:
        res.notes.append("Bolts are axial/shear springs between hole surfaces; tension = preload + ~20 % of the "
                         "external separating load")
    res.ms = (time.perf_counter() - t0) * 1000
    res.extra = {"world": True}
    return res


def eo_nodes(m, eo):
    out = np.zeros(m.p.shape[1])
    for k in range(4):
        out[m.t[k]] = eo
    return out


def _strain_stress_fields(U, lam, mu):
    g = U.grad
    eps = 0.5 * (g + g.transpose(1, 0, 2, 3))
    tr = eps[0, 0] + eps[1, 1] + eps[2, 2]
    sig = 2 * mu * eps
    for i in range(3):
        sig[i, i] = sig[i, i] + lam * tr
    return sig


def _project(ib, m, q):
    X = ib.X
    A = np.vstack([np.ones(X.shape[1]), X]).T
    Vr = np.array([[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 0, 0, 1.0]])
    lump = Vr @ np.linalg.inv(A)
    corner = q @ lump.T
    cnt = np.bincount(m.t.ravel(), minlength=m.p.shape[1]).astype(float)
    out = np.bincount(m.t.T.ravel(), weights=corner.ravel(), minlength=m.p.shape[1])
    return out / np.maximum(cnt, 1)


# --------------------------------------------------------------- drop test ---

def drop_test(body, material, opts, progress=None, cancel=None):
    """Body dropped from a height onto a rigid floor (world -Z). Implicit Newmark (average acceleration),
    P1 tetrahedra, lumped mass, penalty contact with an active set per step."""
    from scipy.sparse import diags
    from scipy.sparse.linalg import splu
    from skfem import Basis, BilinearForm, ElementTetP1, ElementVector, MeshTet, asm
    from skfem.models.elasticity import linear_elasticity
    from .mesh import mesh_solid
    t0 = time.perf_counter()
    h_drop = float(opts.get("height", 1000.0))
    v0 = math.sqrt(2 * G * h_drop)
    bbd = body.bounding_box()
    sm = mesh_solid(body, opts.get("mesh_size") or max(default_size(body) * 1.3,
                                                        max(bbd.size.X, bbd.size.Y, bbd.size.Z) / 14))
    m = MeshTet(np.ascontiguousarray(sm.p.T), np.ascontiguousarray(sm.t))
    ib = Basis(m, ElementVector(ElementTetP1()), intorder=1)
    E, nu, lam, mu = _mat(material)
    rho = material.density * 1e-9
    K = asm(linear_elasticity(lam, mu), ib).tocsc()

    @BilinearForm
    def mass(u, v, w):
        return rho * (u[0] * v[0] + u[1] * v[1] + u[2] * v[2])
    Mc = asm(mass, ib)
    Md = np.asarray(Mc.sum(axis=1)).ravel()
    M = diags(Md)
    nd = ib.nodal_dofs
    P = m.p.T
    zf = P[:, 2].min()
    c = math.sqrt(E / rho)                                   # mm/s
    hmin = sm.size
    # contact penalty ~ local stiffness; time step resolves the contact pulse
    kc = E * hmin * 4.0
    surf_nodes = np.unique(outward_surface(m))
    low = surf_nodes[P[surf_nodes, 2] < zf + 0.25 * hmin + 1e-9]
    cand = surf_nodes[P[surf_nodes, 2] < zf + max(2 * hmin, 0.02 * (P[:, 2].max() - zf))]
    # time scale: contact ≈ π√(m / k_total)
    mass_tot = Md[nd[2]].sum()
    tc = math.pi * math.sqrt(mass_tot / (kc * max(len(low), 1)))
    H = P[:, 2].max() - zf
    t_wave = 2 * H / c
    T = float(opts.get("duration") or 0) or max(3 * tc, 4 * t_wave)
    steps = int(opts.get("steps", 200))
    dt = T / steps
    a0 = 4.0 / dt ** 2
    n = ib.N
    u = np.zeros(n)
    v = np.zeros(n)
    v[nd[2]] = -v0
    a = np.zeros(n)
    a[nd[2]] = -G
    fg = np.zeros(n)
    fg[nd[2]] = -G * Md[nd[2]]
    zdof = nd[2]
    base = (K + a0 * M).tocsc()
    if n > 300000:
        raise SimError("Mesh too fine for the drop test — use a larger element size")
    lu = splu(base, permc_spec="MMD_AT_PLUS_A")
    cz = zdof[cand]
    E_sel = np.zeros((n, len(cz)))
    E_sel[cz, np.arange(len(cz))] = 1.0
    Z = lu.solve(E_sel)                                  # A⁻¹ U for every candidate contact dof
    del E_sel
    ZU = Z[cz, :]                                        # Uᵀ A⁻¹ U
    vm_max = np.zeros(m.p.shape[1])
    frames = []
    force_hist, t_hist, acc_hist = [], [], []
    every = max(1, steps // 40)
    contact_on = None
    contact_off = None

    def vm_nodes(uu):
        U = ib.interpolate(uu)
        g = U.grad
        eps = 0.5 * (g + g.transpose(1, 0, 2, 3))
        tr = eps[0, 0] + eps[1, 1] + eps[2, 2]
        sig = 2 * mu * eps
        for i in range(3):
            sig[i, i] = sig[i, i] + lam * tr
        vme = _von_mises(sig).mean(axis=1)
        out = np.zeros(m.p.shape[1])
        cnt = np.zeros(m.p.shape[1])
        for k in range(4):
            np.add.at(out, m.t[k], vme)
            np.add.at(cnt, m.t[k], 1)
        return out / np.maximum(cnt, 1)

    def solve(rhs, act):
        """(A + kc Σ_active e eᵀ) x = rhs by Woodbury on the active contact dofs."""
        x0 = lu.solve(rhs)
        idx = np.nonzero(act)[0]
        if not len(idx):
            return x0
        S = np.eye(len(idx)) / kc + ZU[np.ix_(idx, idx)]
        y = np.linalg.solve(S, x0[cz[idx]])
        return x0 - Z[:, idx] @ y

    active = np.zeros(len(cand), bool)
    for step in range(1, steps + 1):
        if cancel and cancel():
            raise SimError("Cancelled")
        rhs_base = fg + M @ (a0 * u + 4.0 / dt * v + a)
        for _it in range(10):
            rhs = rhs_base.copy()
            rhs[cz[active]] += kc * (zf - P[cand[active], 2])
            un = solve(rhs, active)
            gap = P[cand, 2] + un[cz] - zf
            new = gap < 0
            if np.array_equal(new, active):
                break
            active = new
        an = a0 * (un - u) - 4.0 / dt * v - a
        v = v + dt / 2 * (a + an)
        u, a = un, an
        Fc = float(kc * np.clip(-(P[cand, 2] + u[zdof[cand]] - zf), 0, None).sum())
        t = step * dt
        if Fc > 0 and contact_on is None:
            contact_on = t
        if Fc == 0 and contact_on is not None and contact_off is None:
            contact_off = t
        force_hist.append(Fc)
        t_hist.append(t * 1000)
        vz = float((v[zdof] * Md[zdof]).sum() / mass_tot)
        acc_hist.append(float((a[zdof] * Md[zdof]).sum() / mass_tot) / G)
        if step % every == 0 or Fc > 0:
            vmn = vm_nodes(u)
            vm_max = np.maximum(vm_max, vmn)
            if step % every == 0:
                disp = np.stack([u[nd[0]], u[nd[1]], u[nd[2]]], axis=1)
                frames.append({"t": t, "scalars": {"von Mises stress": vmn,
                                                   "displacement": np.linalg.norm(disp - disp.mean(0), axis=1)}})
        if progress and step % 20 == 0:
            progress(f"Drop test: step {step}/{steps}")
        if contact_off is not None and vz > 0 and step > steps // 4:
            break
    strength = material.yield_ if material.ductile else material.uts
    res = FieldResult(opts.get("name", "Drop"), "static", sm.p, outward_surface(m))
    res.scalars = {"von Mises stress": vm_max, "factor of safety": np.minimum(strength / np.maximum(vm_max, 1e-9),
                                                                             1e6)}
    res.units = {"von Mises stress": "MPa", "factor of safety": "", "displacement": "mm"}
    res.frames = frames
    res.curves = {"floor force": (t_hist, force_hist, "time (ms)", "force (N)"),
                  "acceleration": (t_hist, acc_hist, "time (ms)", "acceleration (g)")}
    peak = float(max(force_hist) if force_hist else 0)
    res.summary = {"material": material.name, "drop_height_mm": h_drop, "impact_velocity_m_s": round(v0 / 1000, 3),
                   "nodes": sm.n_nodes, "elements": sm.n_elems, "time_step_us": round(float(dt) * 1e6, 3),
                   "peak_floor_force_N": round(peak, 2),
                   "peak_deceleration_g": round(float(max(acc_hist)) if acc_hist else 0.0, 1),
                   "contact_time_ms": round(float((contact_off or t_hist[-1] / 1000) - (contact_on or 0)) * 1000, 4),
                   "max_von_mises_MPa": round(float(vm_max.max()), 3),
                   "min_factor_of_safety": round(float(strength / max(vm_max.max(), 1e-9)), 3),
                   "rebound_velocity_m_s": round(float((v[zdof] * Md[zdof]).sum() / mass_tot) / 1000, 3)}
    res.notes.append("Linear-elastic impact on a rigid floor (penalty contact); peaks above yield mean permanent "
                     "deformation — treat as a severity indicator, not a plastic simulation")
    if res.summary["min_factor_of_safety"] < 1:
        res.notes.append("Stress exceeds the strength: expect denting or cracking at the impact corner")
    res.ms = (time.perf_counter() - t0) * 1000
    res.kind = "drop"
    return res


# ---------------------------------------------------- topology optimisation ---

def topology(body, material, study, opts, progress=None, cancel=None):
    """SIMP compliance minimisation for a volume fraction, density filter, optimality-criteria update.
    Faces with fixtures or loads stay solid."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve
    from scipy.spatial import cKDTree
    from skfem import Basis, ElementTetP1, ElementVector, Functional, MeshTet, asm, condense
    from skfem.helpers import ddot, sym_grad, trace
    from skfem.models.elasticity import linear_elasticity
    from .fea import _Model, _assemble_structural
    from .mesh import mesh_solid
    t0 = time.perf_counter()
    vf = float(opts.get("volume_fraction", 0.4))
    pen = float(opts.get("penalty", 3.0))
    iters = int(opts.get("iterations", 40))
    sm = mesh_solid(body, study.get("mesh_size") or default_size(body) * 0.8)
    model = _Model(sm, vector=True, order=1)
    m, ib = model.m, model.ib
    E, nu, lam, mu = _mat(material)
    K0 = asm(linear_elasticity(lam, mu), ib)
    Kfull, f, D, _fix = _assemble_structural(model, material, study.get("fixtures", []), study.get("loads", []), body)
    Kpen = (Kfull - K0).tocsr()
    if isinstance(D, (list, tuple)):
        D = np.unique(np.concatenate([np.atleast_1d(np.asarray(x, int)) for x in D])) if len(D) else np.array([], int)
    else:
        D = np.unique(np.asarray(D, int))
    if len(D) == 0 and abs(Kpen).sum() < 1e-9:
        raise SimError("Add fixtures to the study first")
    if not np.any(f):
        raise SimError("Add loads to the study first")
    nel = m.t.shape[1]
    # passive solid: elements touching fixture/load faces
    passive = np.zeros(nel, bool)
    keep_nodes = set()
    for it in list(study.get("fixtures", [])) + list(study.get("loads", [])):
        for fi in it.get("faces", []):
            tri = sm.faces.get(fi)
            if tri is not None:
                keep_nodes.update(np.unique(tri).tolist())
    if keep_nodes:
        kn = np.zeros(m.p.shape[1], bool)
        kn[list(keep_nodes)] = True
        passive = kn[m.t].any(axis=0)
    cen = m.p[:, m.t].mean(axis=1).T
    vol_e = np.abs(np.einsum("ij,ij->i", np.cross((m.p[:, m.t[1]] - m.p[:, m.t[0]]).T,
                                                   (m.p[:, m.t[2]] - m.p[:, m.t[0]]).T),
                             (m.p[:, m.t[3]] - m.p[:, m.t[0]]).T)) / 6
    rmin = float(opts.get("filter_radius") or 1.6 * sm.size)
    tree = cKDTree(cen)
    pairs = tree.query_pairs(rmin, output_type="ndarray")
    ii = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(nel)])
    jj = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(nel)])
    w = rmin - np.linalg.norm(cen[ii] - cen[jj], axis=1)
    Hf = coo_matrix((w, (ii, jj)), shape=(nel, nel)).tocsr()
    Hs = np.asarray(Hf.sum(axis=1)).ravel()
    x = np.full(nel, vf)
    x[passive] = 1.0
    xphys = x.copy()
    elast = _elastic_form()
    nq = ib.X.shape[1]
    history = []
    emin = 1e-9
    for it in range(iters):
        if cancel and cancel():
            raise SimError("Cancelled")
        scale = emin + xphys ** pen * (1 - emin)
        K = asm(elast, ib, lam=np.repeat((lam * scale)[:, None], nq, 1), mu=np.repeat((mu * scale)[:, None], nq, 1))
        K = (K + Kpen).tocsr()
        if len(D):
            Kc, fc, _, I = condense(K, f, D=D)
            u = np.zeros(ib.N)
            u[I] = spsolve(Kc.tocsc(), fc)
        else:
            u = spsolve(K.tocsc(), f)
        U = ib.interpolate(u)

        def sed(w):
            e = sym_grad(w["u"])
            return lam * trace(e) ** 2 + 2 * mu * ddot(e, e)
        ce = Functional(sed).elemental(ib, u=U)                # element energy at unit density (×2)
        comp = float(f @ u)
        dc = -pen * xphys ** (pen - 1) * (1 - emin) * ce
        dv = vol_e.copy()
        dc = np.asarray(Hf @ (dc / Hs))
        dv = np.asarray(Hf @ (dv / Hs))
        l1, l2, move = 1e-12, 1e12, 0.2
        target = vf * vol_e.sum()
        while (l2 - l1) / (l1 + l2) > 1e-4:
            lmid = 0.5 * (l1 + l2)
            xn = np.clip(x * np.sqrt(np.maximum(-dc, 0) / (dv * lmid)), np.maximum(0, x - move), np.minimum(1, x + move))
            xn[passive] = 1.0
            xp = np.asarray(Hf @ (xn * 1.0)) / Hs
            xp[passive] = 1.0
            if (xp * vol_e).sum() > target:
                l1 = lmid
            else:
                l2 = lmid
        change = float(np.abs(xn - x).max())
        x, xphys = xn, xp
        history.append(comp)
        if progress:
            progress(f"Topology: iteration {it + 1}/{iters}, compliance {comp:.4g} N·mm, change {change:.3f}")
        if it > 10 and change < 0.01:
            break
    rho_n = np.zeros(m.p.shape[1])
    cnt = np.zeros(m.p.shape[1])
    for k in range(4):
        np.add.at(rho_n, m.t[k], xphys)
        np.add.at(cnt, m.t[k], 1)
    rho_n /= np.maximum(cnt, 1)
    res = FieldResult(study["name"], "topology", sm.p, outward_surface(m))
    res.scalars = {"density": rho_n}
    res.units = {"density": ""}
    res.curves = {"compliance": (list(range(1, len(history) + 1)), history, "iteration", "compliance (N·mm)")}
    kept = float((xphys > 0.5).astype(float) @ vol_e / vol_e.sum())
    res.summary = {"material": material.name, "elements": int(nel), "iterations": len(history),
                   "volume_fraction_target": vf, "volume_fraction_kept": round(kept, 3),
                   "compliance_start": round(history[0], 4), "compliance_end": round(history[-1], 4),
                   "mass_saving_pct": round(100 * (1 - kept), 1)}
    res.notes.append("Iso-surface at density 0.5 — use 'Create body' to turn it into a solid for redesign")
    res.extra = {"elem_density": xphys, "mesh": (m.p.T.copy(), m.t.T.copy())}
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def topology_surface(res, level=0.5, smooth=20):
    """Closed triangle surface of the kept material: (vertices, triangles)."""
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
    P, T = res.extra["mesh"]
    rho_n = res.scalars["density"]
    ug = vtk.vtkUnstructuredGrid()
    pts = vtk.vtkPoints()
    pts.SetData(numpy_to_vtk(np.ascontiguousarray(P), deep=True))
    ug.SetPoints(pts)
    cells = np.hstack([np.full((len(T), 1), 4, np.int64), T.astype(np.int64)]).ravel()
    ca = vtk.vtkCellArray()
    ca.SetCells(len(T), numpy_to_vtkIdTypeArray(cells, deep=True))
    ug.SetCells(vtk.VTK_TETRA, ca)
    arr = numpy_to_vtk(np.ascontiguousarray(rho_n), deep=True)
    arr.SetName("rho")
    ug.GetPointData().SetScalars(arr)
    clip = vtk.vtkClipDataSet()
    clip.SetInputData(ug)
    clip.SetValue(level)
    clip.InsideOutOff()
    surf = vtk.vtkDataSetSurfaceFilter()
    surf.SetInputConnection(clip.GetOutputPort())
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(surf.GetOutputPort())
    clean = vtk.vtkCleanPolyData()
    clean.SetInputConnection(tri.GetOutputPort())
    sm = vtk.vtkWindowedSincPolyDataFilter()
    sm.SetInputConnection(clean.GetOutputPort())
    sm.SetNumberOfIterations(smooth)
    sm.BoundarySmoothingOff()
    sm.FeatureEdgeSmoothingOff()
    sm.SetPassBand(0.1)
    sm.NonManifoldSmoothingOn()
    sm.NormalizeCoordinatesOn()
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputConnection(sm.GetOutputPort())
    conn.SetExtractionModeToLargestRegion()
    dec = vtk.vtkQuadricDecimation()
    dec.SetInputConnection(conn.GetOutputPort())
    dec.SetTargetReduction(0.0)
    conn.Update()
    pd = conn.GetOutput()
    n_tri = pd.GetNumberOfPolys()
    if n_tri > 30000:
        dec.SetTargetReduction(1 - 30000 / n_tri)
        dec.Update()
        pd = dec.GetOutput()
    cl = vtk.vtkCleanPolyData()
    cl.SetInputData(pd)
    cl.Update()
    pd = cl.GetOutput()
    V = vtk_to_numpy(pd.GetPoints().GetData()).astype(float)
    F = vtk_to_numpy(pd.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    return V, F


# ------------------------------------------------------------ design study ---

METRICS = ["mass_kg", "volume_mm3", "max_von_mises_MPa", "max_displacement_mm", "min_factor_of_safety",
           "first_frequency_Hz", "first_buckling_factor", "max_temperature_C", "drag_N", "pressure_drop_Pa"]


def _metric(summary, metric):
    if metric in summary:
        return summary[metric]
    if metric == "first_frequency_Hz" and summary.get("frequencies_Hz"):
        return summary["frequencies_Hz"][0]
    if metric == "first_buckling_factor" and summary.get("load_factors"):
        return summary["load_factors"][0]
    if metric == "drag_N" and summary.get("objects"):
        return sum(o.get("drag_N", 0) for o in summary["objects"].values())
    return None


def design_study(doc, feature, param, values, study=None, metrics=None, progress=None, cancel=None):
    """Sweep a feature parameter; rebuild and (optionally) run a study at each value. The model is restored."""
    from . import studies as S
    f = doc.get(feature)
    if param not in f.params:
        raise SimError(f"{feature} has no parameter '{param}'. Parameters: {list(f.params)}")
    old = f.params[param]
    metrics = metrics or (["mass_kg"] + (["max_von_mises_MPa", "max_displacement_mm", "min_factor_of_safety"]
                                         if study and doc.study(study)["type"] == "static" else []))
    rows = []
    try:
        for i, val in enumerate(values):
            if cancel and cancel():
                raise SimError("Cancelled")
            if progress:
                progress(f"Design study: {param} = {val} ({i + 1}/{len(values)})")
            f.update({param: val})
            doc.rebuild()
            row = {"value": val}
            if doc.result.errors:
                row["error"] = "; ".join(str(e) for e in doc.result.errors)[:200]
                rows.append(row)
                continue
            mp = doc.mass_properties()
            summ = {"mass_kg": mp.get("mass_kg"), "volume_mm3": mp.get("volume_mm3")}
            if study:
                try:
                    r = S.run(doc, study)
                    summ.update(r.summary)
                except Exception as e:  # noqa: BLE001
                    row["error"] = str(e)[:200]
            for mt in metrics:
                row[mt] = _metric(summ, mt)
            rows.append(row)
    finally:
        f.update({param: old})
        doc.rebuild()
    best = {}
    for mt in metrics:
        vals = [(r[mt], r["value"]) for r in rows if r.get(mt) is not None]
        if vals:
            lo, hi = min(vals), max(vals)
            best[mt] = {"min": {"value": lo[1], "metric": lo[0]}, "max": {"value": hi[1], "metric": hi[0]}}
    return {"feature": feature, "parameter": param, "study": study, "metrics": metrics, "rows": rows, "range": best}
