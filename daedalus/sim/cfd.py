"""Flow simulation: lattice-Boltzmann (D3Q19) on a voxel grid built from the solids. Qt-free.

External flow: parts sit in a wind/water tunnel (far-field velocity on the tunnel walls) →
  drag & lift forces and coefficients, velocity and pressure fields.
Internal flow: fluid fills the passages of the parts between an inlet plane and an outlet plane
  (both at the ends of the parts' bounding box along the flow axis) → pressure drop, flow rate.

Incompressible, isothermal. Above Re ≈ 1000 the Smagorinsky sub-grid model stands in for turbulence
(large-eddy simulation); coefficients are then averaged over the last part of the run.
Numba compiles the kernels (multi-core); without numba it falls back to numpy (slow).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

FLUIDS = {  # density kg/m³, dynamic viscosity Pa·s  (20 °C)
    "air": (1.204, 1.825e-5), "water": (998.2, 1.002e-3), "oil (SAE 30)": (875.0, 0.29),
    "glycerin": (1260.0, 1.41),
}
AXES = {"+X": (0, 1), "-X": (0, -1), "+Y": (1, 1), "-Y": (1, -1), "+Z": (2, 1), "-Z": (2, -1)}

C = np.array([[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1],
              [1, 1, 0], [-1, -1, 0], [1, -1, 0], [-1, 1, 0], [1, 0, 1], [-1, 0, -1], [1, 0, -1], [-1, 0, 1],
              [0, 1, 1], [0, -1, -1], [0, 1, -1], [0, -1, 1]], np.int64)
W = np.array([1 / 3] + [1 / 18] * 6 + [1 / 36] * 12)
OPP = np.array([0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17], np.int64)


class FlowError(RuntimeError):
    pass


@dataclass
class FlowResult:
    study: str
    origin: np.ndarray            # world position of cell (0,0,0) centre, mm
    spacing: float                # mm
    axes: tuple                   # (perm, sign): grid axis k ↔ world axis perm[k], direction sign[k]
    shape: tuple
    solid: np.ndarray             # (nx, ny, nz) uint8 (0 fluid, 1+ object id)
    velocity: np.ndarray          # (3, nx, ny, nz) float32, m/s, world-axis components
    pressure: np.ndarray          # (nx, ny, nz) float32, Pa (gauge, relative to outlet / far field)
    summary: dict = field(default_factory=dict)
    curves: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    kind: str = "flow"
    ms: float = 0.0
    modes: list = field(default_factory=list)
    scalars: dict = field(default_factory=dict)
    vectors: dict = field(default_factory=dict)
    frames: list = field(default_factory=list)
    units: dict = field(default_factory=dict)

    def world_points(self, idx):
        """grid indices (k, 3) → world mm."""
        return self.grid_to_world(np.asarray(idx, float))

    def grid_to_world(self, g):
        perm, sign = self.axes
        out = np.zeros_like(g, dtype=float)
        for k in range(3):
            out[..., perm[k]] = g[..., k] * self.spacing * sign[k]
        return out + self.origin


# ----------------------------------------------------------------- voxels ---

def _tris_of(body, tol):
    v, t = body.tessellate(tol, 0.3)
    return np.array([[p.X, p.Y, p.Z] for p in v], float), np.array(t, np.int64)


def _voxelize_np(V, T, nx, ny, nz):
    """Parity ray casting along grid x for every (y, z) column. V in grid units (cell centres at ints)."""
    inside = np.zeros((nx, ny, nz), bool)
    hits = [[[] for _ in range(nz)] for _ in range(ny)]
    for a, b, c in T:
        P = V[[a, b, c]]
        ymin, ymax = math.ceil(P[:, 1].min()), math.floor(P[:, 1].max())
        zmin, zmax = math.ceil(P[:, 2].min()), math.floor(P[:, 2].max())
        if ymax < 0 or zmax < 0 or ymin >= ny or zmin >= nz:
            continue
        (y0, z0), (y1, z1), (y2, z2) = P[:, 1:]
        den = (y1 - y0) * (z2 - z0) - (y2 - y0) * (z1 - z0)
        if abs(den) < 1e-14:
            continue
        for y in range(max(ymin, 0), min(ymax, ny - 1) + 1):
            for z in range(max(zmin, 0), min(zmax, nz - 1) + 1):
                yy, zz = y + 1e-7, z + 1.3e-7
                l1 = ((yy - y0) * (z2 - z0) - (y2 - y0) * (zz - z0)) / den
                l2 = ((y1 - y0) * (zz - z0) - (yy - y0) * (z1 - z0)) / den
                if l1 < 0 or l2 < 0 or l1 + l2 > 1:
                    continue
                hits[y][z].append(P[0, 0] + l1 * (P[1, 0] - P[0, 0]) + l2 * (P[2, 0] - P[0, 0]))
    xs = np.arange(nx)
    for y in range(ny):
        for z in range(nz):
            h = sorted(hits[y][z])
            for i in range(0, len(h) - 1, 2):
                inside[(xs >= h[i]) & (xs <= h[i + 1]), y, z] = True
    return inside


def _voxelize(V, T, nx, ny, nz):
    try:
        from numba import njit
    except ImportError:
        return _voxelize_np(V, T, nx, ny, nz)
    global _vox_nb
    if "_vox_nb" not in globals():
        @njit(cache=True)
        def _vox(V, T, nx, ny, nz):
            maxh = 64
            cnt = np.zeros((ny, nz), np.int64)
            hx = np.zeros((ny, nz, maxh))
            for ti in range(T.shape[0]):
                a, b, c = T[ti, 0], T[ti, 1], T[ti, 2]
                y0, z0 = V[a, 1], V[a, 2]
                y1, z1 = V[b, 1], V[b, 2]
                y2, z2 = V[c, 1], V[c, 2]
                den = (y1 - y0) * (z2 - z0) - (y2 - y0) * (z1 - z0)
                if abs(den) < 1e-14:
                    continue
                ylo = max(int(math.ceil(min(y0, y1, y2))), 0)
                yhi = min(int(math.floor(max(y0, y1, y2))), ny - 1)
                zlo = max(int(math.ceil(min(z0, z1, z2))), 0)
                zhi = min(int(math.floor(max(z0, z1, z2))), nz - 1)
                for y in range(ylo, yhi + 1):
                    for z in range(zlo, zhi + 1):
                        yy = y + 1e-7
                        zz = z + 1.3e-7
                        l1 = ((yy - y0) * (z2 - z0) - (y2 - y0) * (zz - z0)) / den
                        l2 = ((y1 - y0) * (zz - z0) - (yy - y0) * (z1 - z0)) / den
                        if l1 < 0 or l2 < 0 or l1 + l2 > 1:
                            continue
                        if cnt[y, z] < maxh:
                            hx[y, z, cnt[y, z]] = V[a, 0] + l1 * (V[b, 0] - V[a, 0]) + l2 * (V[c, 0] - V[a, 0])
                            cnt[y, z] += 1
            inside = np.zeros((nx, ny, nz), np.bool_)
            for y in range(ny):
                for z in range(nz):
                    n = cnt[y, z]
                    if n < 2:
                        continue
                    h = np.sort(hx[y, z, :n])
                    for i in range(0, n - 1, 2):
                        lo = max(int(math.ceil(h[i])), 0)
                        hi = min(int(math.floor(h[i + 1])), nx - 1)
                        for x in range(lo, hi + 1):
                            inside[x, y, z] = True
            return inside
        _vox_nb = _vox
    return _vox_nb(np.ascontiguousarray(V, np.float64), np.ascontiguousarray(T, np.int64), nx, ny, nz)


# ----------------------------------------------------------------- kernels ---

def _kernels():
    """Compile (once) the numba LBM kernels."""
    global _K
    if "_K" in globals():
        return _K
    from numba import njit, prange

    Cx, Cy, Cz = C[:, 0].copy(), C[:, 1].copy(), C[:, 2].copy()
    Wq, Op = W.copy(), OPP.copy()

    @njit(parallel=True, fastmath=True, cache=True)
    def collide(f, solid, tau0, cs2smag):
        nx, ny, nz = solid.shape
        for x in prange(nx):
            feq = np.empty(19)
            for y in range(ny):
                for z in range(nz):
                    if solid[x, y, z]:
                        continue
                    rho = 0.0
                    ux = 0.0
                    uy = 0.0
                    uz = 0.0
                    for i in range(19):
                        fi = f[i, x, y, z]
                        rho += fi
                        ux += fi * Cx[i]
                        uy += fi * Cy[i]
                        uz += fi * Cz[i]
                    ux /= rho
                    uy /= rho
                    uz /= rho
                    usq = 1.5 * (ux * ux + uy * uy + uz * uz)
                    pxx = 0.0
                    pyy = 0.0
                    pzz = 0.0
                    pxy = 0.0
                    pxz = 0.0
                    pyz = 0.0
                    for i in range(19):
                        cu = 3.0 * (Cx[i] * ux + Cy[i] * uy + Cz[i] * uz)
                        feq[i] = Wq[i] * rho * (1.0 + cu + 0.5 * cu * cu - usq)
                        d = f[i, x, y, z] - feq[i]
                        pxx += Cx[i] * Cx[i] * d
                        pyy += Cy[i] * Cy[i] * d
                        pzz += Cz[i] * Cz[i] * d
                        pxy += Cx[i] * Cy[i] * d
                        pxz += Cx[i] * Cz[i] * d
                        pyz += Cy[i] * Cz[i] * d
                    tau = tau0
                    if cs2smag > 0.0:
                        Q = math.sqrt(pxx * pxx + pyy * pyy + pzz * pzz + 2 * (pxy * pxy + pxz * pxz + pyz * pyz))
                        tau = 0.5 * (tau0 + math.sqrt(tau0 * tau0 + 18.0 * cs2smag * Q / rho))
                    om = 1.0 / tau
                    for i in range(19):
                        f[i, x, y, z] += om * (feq[i] - f[i, x, y, z])

    @njit(parallel=True, cache=True)
    def stream(f, g, solid, force_on, fx_acc, nobj):
        nx, ny, nz = solid.shape
        part = np.zeros((nx, nobj + 1, 3))
        for x in prange(nx):
            for y in range(ny):
                for z in range(nz):
                    if solid[x, y, z]:
                        continue
                    for i in range(19):
                        xs = x - Cx[i]
                        ys = y - Cy[i]
                        zs = z - Cz[i]
                        if xs < 0 or xs >= nx or ys < 0 or ys >= ny or zs < 0 or zs >= nz:
                            g[i, x, y, z] = f[i, x, y, z]       # edges: overwritten by BCs
                            continue
                        s = solid[xs, ys, zs]
                        if s:
                            # bounce-back from the wall link (x+c_opp is solid)
                            fo = f[Op[i], x, y, z]
                            g[i, x, y, z] = fo
                            if force_on:
                                part[x, s, 0] += 2.0 * fo * Cx[Op[i]]
                                part[x, s, 1] += 2.0 * fo * Cy[Op[i]]
                                part[x, s, 2] += 2.0 * fo * Cz[Op[i]]
                        else:
                            g[i, x, y, z] = f[i, xs, ys, zs]
        if force_on:
            for x in range(nx):
                for s in range(nobj + 1):
                    for k in range(3):
                        fx_acc[s, k] += part[x, s, k]

    @njit(parallel=True, cache=True)
    def macro(f, solid, rho, u):
        nx, ny, nz = solid.shape
        for x in prange(nx):
            for y in range(ny):
                for z in range(nz):
                    if solid[x, y, z]:
                        rho[x, y, z] = 1.0
                        u[0, x, y, z] = 0.0
                        u[1, x, y, z] = 0.0
                        u[2, x, y, z] = 0.0
                        continue
                    r = 0.0
                    a = 0.0
                    b = 0.0
                    c = 0.0
                    for i in range(19):
                        fi = f[i, x, y, z]
                        r += fi
                        a += fi * Cx[i]
                        b += fi * Cy[i]
                        c += fi * Cz[i]
                    rho[x, y, z] = r
                    u[0, x, y, z] = a / r
                    u[1, x, y, z] = b / r
                    u[2, x, y, z] = c / r

    @njit(cache=True)
    def set_eq(f, x, y, z, rho, ux, uy, uz):
        usq = 1.5 * (ux * ux + uy * uy + uz * uz)
        for i in range(19):
            cu = 3.0 * (Cx[i] * ux + Cy[i] * uy + Cz[i] * uz)
            f[i, x, y, z] = Wq[i] * rho * (1.0 + cu + 0.5 * cu * cu - usq)

    @njit(parallel=True, cache=True)
    def boundaries(f, solid, u_in, external):
        nx, ny, nz = solid.shape
        # inlet x = 0: velocity, density from the neighbour
        for y in prange(ny):
            for z in range(nz):
                if not solid[0, y, z]:
                    r = 0.0
                    for i in range(19):
                        r += f[i, 1, y, z]
                    set_eq(f, 0, y, z, r, u_in, 0.0, 0.0)
                if not solid[nx - 1, y, z]:
                    r = 0.0
                    ux = 0.0
                    uy = 0.0
                    uz = 0.0
                    for i in range(19):
                        fi = f[i, nx - 2, y, z]
                        r += fi
                        ux += fi * Cx[i]
                        uy += fi * Cy[i]
                        uz += fi * Cz[i]
                    set_eq(f, nx - 1, y, z, 1.0, ux / r, uy / r, uz / r)
        if external:
            # tunnel walls: zero-gradient (open far field) — lower blockage than a forced velocity
            for x in prange(1, nx - 1):
                for y in range(ny):
                    for i in range(19):
                        f[i, x, y, 0] = f[i, x, y, 1]
                        f[i, x, y, nz - 1] = f[i, x, y, nz - 2]
                for z in range(nz):
                    for i in range(19):
                        f[i, x, 0, z] = f[i, x, 1, z]
                        f[i, x, ny - 1, z] = f[i, x, ny - 2, z]

    _K = (collide, stream, macro, boundaries)
    return _K


# -------------------------------------------------------------------- run ---

def run(bodies: dict, study: dict, progress=None, cancel=None) -> FlowResult:
    """bodies: {object name: solid placed in world coordinates (mm)}.
    study keys: mode external|internal, fluid, density/viscosity (custom), velocity (m/s),
    direction ('+X'…), resolution (cells along the domain length), flow_throughs, objects."""
    t0 = time.perf_counter()
    try:
        collide, stream, macro, boundaries = _kernels()
    except ImportError as e:
        raise FlowError("Flow simulation needs numba (pip install numba)") from e
    if not bodies:
        raise FlowError("Nothing to put in the flow")
    mode = study.get("mode", "external")
    fluid = study.get("fluid", "air")
    rho_p, mu_p = FLUIDS.get(fluid, (float(study.get("density", 1.2)), float(study.get("viscosity", 1.8e-5))))
    U = float(study.get("velocity", 5.0))
    if U <= 0:
        raise FlowError("Velocity must be > 0")
    ax, sg = AXES[study.get("direction", "+X")]
    perm = [ax] + [a for a in range(3) if a != ax]
    sign = [sg, 1, 1]

    # world → flow frame (grid axis 0 along the flow)
    def to_flow(P):
        Q = np.empty_like(P)
        for k in range(3):
            Q[:, k] = P[:, perm[k]] * sign[k]
        return Q

    names = list(bodies)
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    tris = []
    for n in names:
        b = bodies[n]
        V, T = _tris_of(b, max(0.05, b.bounding_box().diagonal * 0.002))
        Vf = to_flow(V)
        lo, hi = np.minimum(lo, Vf.min(0)), np.maximum(hi, Vf.max(0))
        tris.append((Vf, T))
    size = hi - lo
    across = float(study.get("resolution", 20))               # cells across the body / passage
    if mode == "external":
        Lchar = float(max(size[1], size[2], 1e-6))
        ref = max(size[1:].max(), size[0] * 0.2)
        pad_lo = np.array([1.2, 1.6, 1.6]) * ref
        pad_hi = np.array([2.8, 1.6, 1.6]) * ref
        dlo, dhi = lo - pad_lo, hi + pad_hi
        dx = Lchar / across
    else:
        Lchar = None
        dlo, dhi = lo.copy(), hi.copy()
        dx = float(min(size[1], size[2])) / across
    max_cells = float(study.get("max_cells", 3e6))
    while True:
        dims = np.maximum(np.ceil((dhi - dlo) / dx).astype(int) + 1, 4)
        if mode == "internal":
            dims[1:] += 2
        if np.prod(dims) <= max_cells:
            break
        dx *= 1.15
    if dx > (Lchar or min(size[1], size[2])) / across * 1.01:
        notes_pre = [f"Grid coarsened to {np.prod(dims) / 1e6:.1f} M cells (raise max cells for more detail)"]
    else:
        notes_pre = []
    if mode == "internal":
        dlo[1:] -= dx
        # inlet/outlet planes sit half a cell inside the parts' ends, so they cut through the passage
        dlo[0] = lo[0] + 0.5 * dx
        dims[0] = max(4, int((hi[0] - lo[0] - dx) / dx) + 1)
    nx, ny, nz = map(int, dims)
    solid = np.zeros((nx, ny, nz), np.uint8)
    for k, (Vf, T) in enumerate(tris):
        G = (Vf - dlo) / dx
        ins = _voxelize(G, T, nx, ny, nz)
        solid[ins & (solid == 0)] = k + 1
    if not solid.any():
        raise FlowError("The parts are smaller than one cell — raise the resolution")
    notes = list(notes_pre)
    if mode == "internal":
        fluid_mask = _internal_fluid(solid)
        if not fluid_mask.any():
            raise FlowError("No through-passage along the flow axis — is the part hollow end to end?")
        outside = (solid == 0) & ~fluid_mask
        solid[outside] = 255                                    # blocked (not part of the flow)
        D_h = _hydraulic_diameter(fluid_mask, dx)
        Lchar = D_h
        notes.append(f"Hydraulic diameter {D_h:.2f} mm")
    nu_p = mu_p / rho_p
    Re = U * (Lchar / 1000.0) / nu_p
    L_cells = Lchar / dx
    U_lb = float(study.get("lattice_velocity", 0.08))
    nu_lb = U_lb * L_cells / Re
    tau = 3 * nu_lb + 0.5
    smag = 0.0
    if tau < 0.51:
        smag = 0.17 ** 2
        tau = max(tau, 0.5 + 1e-4)
        notes.append(f"Re = {Re:.3g}: resolved as a large-eddy simulation (Smagorinsky); "
                     "coefficients are time-averaged estimates")
    dt = (dx / 1000.0) * U_lb / U                               # s per step
    flow_throughs = float(study.get("flow_throughs", 2.0))
    steps = int(flow_throughs * nx / U_lb)
    steps = min(steps, int(study.get("max_steps", 40000)))
    nobj = max(len(names), 1)
    f = np.empty((19, nx, ny, nz), np.float32)
    for i in range(19):
        cu = 3.0 * C[i, 0] * U_lb
        f[i] = W[i] * (1 + cu + 0.5 * cu * cu - 1.5 * U_lb * U_lb)
    if mode == "internal":
        f[:, solid > 0] = W[:, None]
    g = f.copy()
    rho = np.ones((nx, ny, nz), np.float32)
    u = np.zeros((3, nx, ny, nz), np.float32)
    blocked = solid == 255
    solid_in = solid.copy()
    solid_in[blocked] = nobj + 1
    facc = np.zeros((nobj + 2, 3))
    hist_t, hist_fx, hist_fy, hist_dp = [], [], [], []
    avg_from = int(steps * 0.6)
    fsum = np.zeros((nobj + 2, 3))
    nsum = 0
    ramp = max(1, int(steps * 0.1))
    fscale = rho_p * (dx / 1000.0) ** 4 / dt ** 2               # lattice force → N
    pscale = rho_p * ((dx / 1000.0) / dt) ** 2 / 3.0             # lattice δρ → Pa (p = c_s² δρ)
    uavg = np.zeros((3, nx, ny, nz), np.float64)
    pavg = np.zeros((nx, ny, nz), np.float64)
    navg = 0
    sample_every = max(1, steps // 200)
    for it in range(steps):
        uin = U_lb * min(1.0, (it + 1) / ramp)
        collide(f, solid_in, tau, smag)
        facc[:] = 0
        record = it >= avg_from
        stream(f, g, solid_in, record or it % sample_every == 0, facc, nobj + 1)
        f, g = g, f
        boundaries(f, solid_in, uin, mode == "external")
        if it % sample_every == 0 or it == steps - 1:
            hist_t.append(it * dt)
            Fn = facc[1:nobj + 1].sum(0) * fscale
            hist_fx.append(float(Fn[0]))
            hist_fy.append(float(np.hypot(Fn[1], Fn[2])))
            if mode == "internal":
                macro(f, solid_in, rho, u)
                hist_dp.append(float((_plane_mean(rho, solid_in, 1) - _plane_mean(rho, solid_in, nx - 2)) * pscale))
            if progress and len(hist_t) % 10 == 0:
                progress(f"Flow: step {it + 1:,} / {steps:,}")
            if cancel and cancel():
                notes.append(f"Stopped at step {it + 1:,} of {steps:,}")
                break
            if not np.isfinite(hist_fx[-1]):
                raise FlowError("Flow solution diverged — lower the velocity or raise the resolution")
        if record:
            fsum += facc
            nsum += 1
            if (it - avg_from) % max(1, (steps - avg_from) // 40) == 0:
                macro(f, solid_in, rho, u)
                uavg += u
                pavg += rho
                navg += 1
    macro(f, solid_in, rho, u)
    if navg:
        u_lb_field = (uavg / navg).astype(np.float32)
        rho_field = (pavg / navg).astype(np.float32)
    else:
        u_lb_field, rho_field = u, rho
    vel = u_lb_field * (U / U_lb)
    p = (rho_field - 1.0) * pscale
    if mode == "external":
        p -= float(np.median(p[-2, :, :]))
    else:
        p -= _plane_mean(p, solid_in, nx - 2)
    p[solid_in > 0] = np.nan
    vel[:, solid_in > 0] = 0.0
    # back to world axes
    velw = np.zeros_like(vel)
    for k in range(3):
        velw[perm[k]] = vel[k] * sign[k]
    Favg = (fsum / max(nsum, 1)) * fscale
    summary = {"mode": mode, "fluid": fluid, "velocity_m_s": U, "direction": study.get("direction", "+X"),
               "reynolds": round(Re, 2), "grid": [nx, ny, nz], "cell_mm": round(dx, 4), "steps": it + 1,
               "simulated_time_s": round((it + 1) * dt, 5), "turbulence_model": "Smagorinsky LES" if smag else "laminar"}
    qdyn = 0.5 * rho_p * U * U
    for k, n in enumerate(names):
        Fg = Favg[k + 1]
        Fw = np.zeros(3)
        for j in range(3):
            Fw[perm[j]] = Fg[j] * sign[j]
        proj = (solid == k + 1).any(axis=0).sum() * (dx / 1000.0) ** 2
        ent = {"force_N": [float(f"{v:.4g}") for v in Fw], "drag_N": float(f"{Fg[0]:.4g}"),
               "lift_side_N": float(f"{math.hypot(Fg[1], Fg[2]):.4g}"), "frontal_area_m2": float(f"{proj:.4g}")}
        if mode == "external" and proj > 0:
            ent["Cd"] = round(float(Fg[0] / (qdyn * proj)), 4)
        summary.setdefault("objects", {})[n] = ent
    if mode == "internal":
        q_area = (solid_in[1] == 0).sum() * (dx / 1000.0) ** 2
        Q = float(np.nansum(vel[0, 1][solid_in[1] == 0]) * (dx / 1000.0) ** 2)
        dp = float(np.nanmean(p[1][solid_in[1] == 0]) - np.nanmean(p[nx - 2][solid_in[nx - 2] == 0]))
        summary.update({"pressure_drop_Pa": round(dp, 4), "flow_rate_m3_s": Q, "flow_rate_L_min": round(Q * 60000, 4),
                        "inlet_area_mm2": round(q_area * 1e6, 3), "mass_flow_kg_s": round(Q * rho_p, 6)})
    summary["max_velocity_m_s"] = round(float(np.linalg.norm(vel, axis=0).max()), 4)
    curves = {"drag force": (hist_t, hist_fx, "time (s)", "N"), "side/lift force": (hist_t, hist_fy, "time (s)", "N")}
    if hist_dp:
        curves["pressure drop"] = (hist_t, hist_dp, "time (s)", "Pa")
    origin = np.zeros(3)
    for k in range(3):
        origin[perm[k]] = dlo[k] * sign[k]
    res = FlowResult(study.get("name", "Flow"), origin, dx, (perm, sign), (nx, ny, nz), solid_in, velw,
                     p.astype(np.float32), summary, curves, notes)
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def _plane_mean(a, solid, x):
    m = solid[x] == 0
    return float(a[x][m].mean()) if m.any() else 0.0


def _internal_fluid(solid):
    """Fluid cells inside the parts: empty cells not connected to the tunnel's side walls."""
    from scipy import ndimage
    empty = solid == 0
    lab, n = ndimage.label(empty)
    side = set(np.unique(np.concatenate([lab[:, 0, :].ravel(), lab[:, -1, :].ravel(),
                                         lab[:, :, 0].ravel(), lab[:, :, -1].ravel()]))) - {0}
    inlet = set(np.unique(lab[0])) - {0}
    outlet = set(np.unique(lab[-1])) - {0}
    keep = (inlet & outlet) - side
    return np.isin(lab, list(keep)) if keep else np.zeros_like(empty)


def _hydraulic_diameter(fluid, dx):
    """4A/P of the largest cross-section; staircase edge count × π/4 approximates the true perimeter."""
    areas = fluid.sum(axis=(1, 2)).astype(float)
    sec = fluid[int(np.argmax(areas))].astype(np.int8)
    A = sec.sum()
    edges = np.abs(np.diff(np.pad(sec, 1), axis=0)).sum() + np.abs(np.diff(np.pad(sec, 1), axis=1)).sum()
    return float(4 * A / max(edges * math.pi / 4, 1) * dx)


def slice_plane(res: FlowResult, axis: int, frac: float):
    """World-axis slice of |velocity| and pressure: returns (positions (h, w, 3) world mm, speed, pressure)."""
    perm, sign = res.axes
    gk = perm.index(axis)
    n = res.shape[gk]
    i = int(np.clip(round(frac * (n - 1)), 0, n - 1))
    sl = [slice(None)] * 3
    sl[gk] = i
    sp = np.linalg.norm(res.velocity[(slice(None), *sl)], axis=0)
    pr = res.pressure[tuple(sl)]
    a, b = [k for k in range(3) if k != gk]
    ga, gb = np.meshgrid(np.arange(res.shape[a]), np.arange(res.shape[b]), indexing="ij")
    G = np.zeros(ga.shape + (3,))
    G[..., gk] = i
    G[..., a] = ga
    G[..., b] = gb
    return res.grid_to_world(G), sp, pr
