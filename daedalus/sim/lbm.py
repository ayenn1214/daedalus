"""The lattice-Boltzmann time loop on the CPU (numba, every core) or on an NVIDIA graphics card (CUDA, numba.cuda).

Both backends offer the same small interface, so cfd.py's loops don't care where they run:
    d = make(f0, solid, nforce, device)      device: 'cpu' | 'gpu' (falls back to the CPU with d.note set)
    d.collide(tau, smag); d.zero_forces(); d.stream(force_on); d.swap(); d.boundaries(u_in, external)
    d.set_ports(pin, pin_nb, pout, pout_nb); d.ports(ux, uy, uz)
    d.forces() → (nforce, 3) host;  d.macro() → (rho, u) host;  d.accumulate();  d.averages() → (u, rho) host
GPU kernels mirror the CPU kernels in cfd.py line for line (D3Q19, BGK + Smagorinsky, half-way bounce-back)."""
from __future__ import annotations

import math

import numpy as np

from .. import compute


class CpuLBM:
    device = "cpu"

    def __init__(self, f0, solid, nforce):
        from .cfd import _kernels
        self.collide_k, self.stream_k, self.macro_k, self.bound_k = _kernels()
        self.f, self.g = f0, f0.copy()
        self.solid = solid
        self.nforce = nforce
        self.facc = np.zeros((nforce, 3))
        self.rho = np.ones(solid.shape, np.float32)
        self.u = np.zeros((3,) + solid.shape, np.float32)
        self.uavg = np.zeros((3,) + solid.shape)
        self.pavg = np.zeros(solid.shape)
        self.navg = 0
        self.note = ""

    def collide(self, tau, smag):
        self.collide_k(self.f, self.solid, tau, smag)

    def zero_forces(self):
        self.facc[:] = 0

    def stream(self, force_on):
        self.stream_k(self.f, self.g, self.solid, bool(force_on), self.facc, self.nforce - 1)

    def swap(self):
        self.f, self.g = self.g, self.f

    def boundaries(self, u_in, external):
        self.bound_k(self.f, self.solid, u_in, bool(external))

    def set_ports(self, pin, pin_nb, pout, pout_nb):
        from .cfd import _port_kernel
        self.ports_k = _port_kernel()
        self._p = (np.ascontiguousarray(pin), np.ascontiguousarray(pin_nb), np.ascontiguousarray(pout),
                   np.ascontiguousarray(pout_nb))

    def ports(self, ux, uy, uz):
        pin, pin_nb, pout, pout_nb = self._p
        self.ports_k(self.f, pin, pin_nb, ux, uy, uz, pout, pout_nb)

    def forces(self):
        return self.facc

    def macro(self):
        self.macro_k(self.f, self.solid, self.rho, self.u)
        return self.rho, self.u

    def accumulate(self):
        self.macro()
        self.uavg += self.u
        self.pavg += self.rho
        self.navg += 1

    def averages(self):
        if not self.navg:
            return self.u, self.rho
        return (self.uavg / self.navg).astype(np.float32), (self.pavg / self.navg).astype(np.float32)


# ------------------------------------------------------------------------------------------------- GPU (CUDA)
_GK = None
cuda = None                     # numba.cuda, imported on first use (a module global so its simulator can test it)


def _gpu_kernels():
    global _GK, cuda
    if _GK is not None:
        return _GK
    from numba import cuda as _cuda
    cuda = _cuda
    from .cfd import C, OPP, W
    Cx = tuple(int(v) for v in C[:, 0])
    Cy = tuple(int(v) for v in C[:, 1])
    Cz = tuple(int(v) for v in C[:, 2])
    Wq = tuple(float(v) for v in W)
    Op = tuple(int(v) for v in OPP)

    @cuda.jit(device=True)
    def set_eq(f, x, y, z, rho, ux, uy, uz):
        usq = 1.5 * (ux * ux + uy * uy + uz * uz)
        for i in range(19):
            cu = 3.0 * (Cx[i] * ux + Cy[i] * uy + Cz[i] * uz)
            f[i, x, y, z] = Wq[i] * rho * (1.0 + cu + 0.5 * cu * cu - usq)

    @cuda.jit
    def collide(f, solid, tau0, cs2smag):
        nx, ny, nz = solid.shape
        n = cuda.grid(1)
        if n >= nx * ny * nz:
            return
        x = n // (ny * nz)
        y = (n // nz) % ny
        z = n % nz
        if solid[x, y, z]:
            return
        feq = cuda.local.array(19, dtype=np.float64)
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

    @cuda.jit
    def stream(f, g, solid, force_on, part):
        # part: (nx, nforce, 3) per-slab force sums (spreads the atomic adds over nx addresses)
        nx, ny, nz = solid.shape
        n = cuda.grid(1)
        if n >= nx * ny * nz:
            return
        x = n // (ny * nz)
        y = (n // nz) % ny
        z = n % nz
        if solid[x, y, z]:
            return
        for i in range(19):
            xs = x - Cx[i]
            ys = y - Cy[i]
            zs = z - Cz[i]
            if xs < 0 or xs >= nx or ys < 0 or ys >= ny or zs < 0 or zs >= nz:
                g[i, x, y, z] = f[i, x, y, z]
                continue
            s = solid[xs, ys, zs]
            if s:
                fo = f[Op[i], x, y, z]
                g[i, x, y, z] = fo
                if force_on:
                    cuda.atomic.add(part, (x, s, 0), 2.0 * fo * Cx[Op[i]])
                    cuda.atomic.add(part, (x, s, 1), 2.0 * fo * Cy[Op[i]])
                    cuda.atomic.add(part, (x, s, 2), 2.0 * fo * Cz[Op[i]])
            else:
                g[i, x, y, z] = f[i, xs, ys, zs]

    @cuda.jit
    def macro(f, solid, rho, u):
        nx, ny, nz = solid.shape
        n = cuda.grid(1)
        if n >= nx * ny * nz:
            return
        x = n // (ny * nz)
        y = (n // nz) % ny
        z = n % nz
        if solid[x, y, z]:
            rho[x, y, z] = 1.0
            u[0, x, y, z] = 0.0
            u[1, x, y, z] = 0.0
            u[2, x, y, z] = 0.0
            return
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

    @cuda.jit
    def inlet_outlet(f, solid, u_in):
        nx, ny, nz = solid.shape
        n = cuda.grid(1)
        if n >= ny * nz:
            return
        y = n // nz
        z = n % nz
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

    @cuda.jit
    def walls_z(f, nx, ny, nz):
        n = cuda.grid(1)
        if n >= (nx - 2) * ny:
            return
        x = 1 + n // ny
        y = n % ny
        for i in range(19):
            f[i, x, y, 0] = f[i, x, y, 1]
            f[i, x, y, nz - 1] = f[i, x, y, nz - 2]

    @cuda.jit
    def walls_y(f, nx, ny, nz):
        n = cuda.grid(1)
        if n >= (nx - 2) * nz:
            return
        x = 1 + n // nz
        z = n % nz
        for i in range(19):
            f[i, x, 0, z] = f[i, x, 1, z]
            f[i, x, ny - 1, z] = f[i, x, ny - 2, z]

    @cuda.jit
    def ports_in(f, pin, pin_nb, ux, uy, uz):
        k = cuda.grid(1)
        if k >= pin.shape[0]:
            return
        r = 0.0
        for i in range(19):
            r += f[i, pin_nb[k, 0], pin_nb[k, 1], pin_nb[k, 2]]
        set_eq(f, pin[k, 0], pin[k, 1], pin[k, 2], r, ux, uy, uz)

    @cuda.jit
    def ports_out(f, pout, pout_nb):
        k = cuda.grid(1)
        if k >= pout.shape[0]:
            return
        a, b, c = pout_nb[k, 0], pout_nb[k, 1], pout_nb[k, 2]
        r = 0.0
        vx = 0.0
        vy = 0.0
        vz = 0.0
        for i in range(19):
            fi = f[i, a, b, c]
            r += fi
            vx += fi * Cx[i]
            vy += fi * Cy[i]
            vz += fi * Cz[i]
        set_eq(f, pout[k, 0], pout[k, 1], pout[k, 2], 1.0, vx / r, vy / r, vz / r)

    @cuda.jit
    def accumulate(u, rho, uavg, pavg):
        nx, ny, nz = rho.shape
        n = cuda.grid(1)
        if n >= nx * ny * nz:
            return
        x = n // (ny * nz)
        y = (n // nz) % ny
        z = n % nz
        pavg[x, y, z] += rho[x, y, z]
        for k in range(3):
            uavg[k, x, y, z] += u[k, x, y, z]

    _GK = dict(collide=collide, stream=stream, macro=macro, inlet_outlet=inlet_outlet, walls_z=walls_z,
               walls_y=walls_y, ports_in=ports_in, ports_out=ports_out, accumulate=accumulate)
    return _GK


def gpu_bytes(shape, nforce=4) -> int:
    n = int(np.prod(shape))
    return n * (2 * 19 * 4 + 1 + 16 + 32) + shape[0] * nforce * 24


class GpuLBM:
    device = "gpu"
    TPB = 128

    def __init__(self, f0, solid, nforce):
        from numba import cuda
        self.cuda = cuda
        self.K = _gpu_kernels()
        self.shape = solid.shape
        self.N = int(np.prod(solid.shape))
        self.blocks = (self.N + self.TPB - 1) // self.TPB
        self.f = cuda.to_device(np.ascontiguousarray(f0))
        self.g = cuda.to_device(np.ascontiguousarray(f0))
        self.solid = cuda.to_device(np.ascontiguousarray(solid))
        self.nforce = nforce
        self.part = cuda.to_device(np.zeros((solid.shape[0], nforce, 3)))
        self.rho = cuda.to_device(np.ones(solid.shape, np.float32))
        self.u = cuda.to_device(np.zeros((3,) + solid.shape, np.float32))
        self.uavg = None
        self.pavg = None
        self.navg = 0
        self.note = ""
        self._zero_part = np.zeros((solid.shape[0], nforce, 3))

    def _launch(self, k, n, *args):
        b = max(1, (n + self.TPB - 1) // self.TPB)
        k[b, self.TPB](*args)

    def collide(self, tau, smag):
        self._launch(self.K["collide"], self.N, self.f, self.solid, float(tau), float(smag))

    def zero_forces(self):
        self.part.copy_to_device(self._zero_part)

    def stream(self, force_on):
        self._launch(self.K["stream"], self.N, self.f, self.g, self.solid, bool(force_on), self.part)

    def swap(self):
        self.f, self.g = self.g, self.f

    def boundaries(self, u_in, external):
        nx, ny, nz = self.shape
        self._launch(self.K["inlet_outlet"], ny * nz, self.f, self.solid, float(u_in))
        if external:
            self._launch(self.K["walls_z"], (nx - 2) * ny, self.f, nx, ny, nz)
            self._launch(self.K["walls_y"], (nx - 2) * nz, self.f, nx, ny, nz)

    def set_ports(self, pin, pin_nb, pout, pout_nb):
        c = self.cuda
        self._p = tuple(c.to_device(np.ascontiguousarray(a, np.int64)) for a in (pin, pin_nb, pout, pout_nb))

    def ports(self, ux, uy, uz):
        pin, pin_nb, pout, pout_nb = self._p
        self._launch(self.K["ports_in"], pin.shape[0], self.f, pin, pin_nb, float(ux), float(uy), float(uz))
        self._launch(self.K["ports_out"], pout.shape[0], self.f, pout, pout_nb)

    def forces(self):
        return self.part.copy_to_host().sum(0)

    def macro(self):
        self._launch(self.K["macro"], self.N, self.f, self.solid, self.rho, self.u)
        return self.rho.copy_to_host(), self.u.copy_to_host()

    def accumulate(self):
        if self.uavg is None:
            self.uavg = self.cuda.to_device(np.zeros((3,) + self.shape))
            self.pavg = self.cuda.to_device(np.zeros(self.shape))
        self._launch(self.K["macro"], self.N, self.f, self.solid, self.rho, self.u)
        self._launch(self.K["accumulate"], self.N, self.u, self.rho, self.uavg, self.pavg)
        self.navg += 1

    def averages(self):
        if not self.navg:
            rho, u = self.macro()
            return u, rho
        return ((self.uavg.copy_to_host() / self.navg).astype(np.float32),
                (self.pavg.copy_to_host() / self.navg).astype(np.float32))


def make(f0, solid, nforce, device: str | None = None):
    """The time-loop backend. device None → the Settings choice (auto: the graphics card when CUDA works and the
    grid fits in its memory)."""
    want = device or compute.device()
    note = ""
    if want in ("gpu", "auto"):
        ok, why = compute.cuda_ok(gpu_bytes(solid.shape, nforce))
        if ok:
            try:
                d = GpuLBM(f0, solid, nforce)
                d.note = f"Ran on the graphics card ({compute.gpu_name()})"
                return d
            except Exception as e:  # noqa: BLE001
                why = f"CUDA error: {e}"
        if want == "gpu":
            note = f"Graphics card not used — {why}; ran on the CPU"
    d = CpuLBM(f0, solid, nforce)
    d.note = note or f"Ran on the CPU ({compute.threads()} threads)"
    return d
