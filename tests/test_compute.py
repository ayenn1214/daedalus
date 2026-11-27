"""Compute: CPU threads, the CUDA flow kernels (checked against the CPU kernels in numba's CUDA simulator — no
graphics card needed) and the sparse solver choice."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import scipy.sparse as sp

from daedalus import compute

ROOT = Path(__file__).resolve().parents[1]

CHECK = r'''
import sys, numpy as np
sys.path.insert(0, ROOT)
from daedalus.sim import lbm
from daedalus.sim.cfd import W, C
nx, ny, nz = 7, 5, 5
solid = np.zeros((nx, ny, nz), np.uint8); solid[3:5, 2, 2] = 1; solid[:, 0, :] = 3
f0 = np.empty((19, nx, ny, nz), np.float32)
for i in range(19):
    cu = 3.0 * C[i, 0] * 0.05; f0[i] = W[i] * (1 + cu + 0.5 * cu * cu - 1.5 * 0.05 ** 2)
f0[:, solid > 0] = W[:, None]
f0 *= (1 + 0.01 * np.random.default_rng(1).standard_normal(f0.shape)).astype(np.float32)
pin = np.array([[1, 2, 1], [1, 3, 1]]); pout = np.array([[5, 2, 3]])
res = {}
for dev, cls in (("cpu", lbm.CpuLBM), ("gpu", lbm.GpuLBM)):
    d = cls(f0.copy(), solid, 5); d.set_ports(pin, pin + [1, 0, 0], pout, pout - [1, 0, 0])
    F = np.zeros((5, 3))
    for it in range(3):
        d.collide(0.52, 0.17 ** 2 if it % 2 else 0.0); d.zero_forces(); d.stream(True); d.swap()
        d.boundaries(0.05, it % 2 == 0); d.ports(0.01, 0.02, 0.0); F += d.forces()
        if it >= 1: d.accumulate()
    rho, u = d.macro(); ua, ra = d.averages()
    res[dev] = (rho, u, ua, ra, F)
for a, b in zip(res["cpu"], res["gpu"]):
    assert np.allclose(a, b, atol=2e-5), float(np.max(np.abs(a - b)))
print("MATCH")
'''.replace("ROOT", repr(str(ROOT)))


def test_cuda_flow_kernels_match_the_cpu():
    env = {**os.environ, "NUMBA_ENABLE_CUDASIM": "1"}
    r = subprocess.run([sys.executable, "-c", CHECK], capture_output=True, text=True, env=env, timeout=600)
    assert "MATCH" in r.stdout, r.stderr[-2000:]


def test_threads_and_device(monkeypatch):
    compute.configure(device="cpu", threads=1)
    try:
        assert compute.device() == "cpu" and compute.threads() == 1 and os.environ["OMP_NUM_THREADS"] == "1"
        monkeypatch.setenv("DAEDALUS_DEVICE", "gpu")
        assert compute.device() == "gpu"
    finally:
        compute.configure(None, None)
    assert compute.threads() >= 1 and "CPU:" in compute.summary()


def test_lbm_falls_back_to_the_cpu(monkeypatch):
    from daedalus.sim import lbm
    monkeypatch.setattr(compute, "cuda_ok", lambda need=0: (False, "no NVIDIA graphics card"))
    f0 = np.ones((19, 4, 4, 4), np.float32)
    d = lbm.make(f0, np.zeros((4, 4, 4), np.uint8), 3, "gpu")
    assert d.device == "cpu" and "Graphics card not used — no NVIDIA graphics card" in d.note
    assert lbm.make(f0, np.zeros((4, 4, 4), np.uint8), 3, "cpu").note.startswith("Ran on the CPU")


def test_gpu_memory_check(monkeypatch):
    from daedalus.sim import lbm
    monkeypatch.setattr(compute, "info", lambda refresh=False: {"cuda": True, "free_gb": 0.001, "why": ""})
    ok, why = compute.cuda_ok(lbm.gpu_bytes((200, 200, 200)))
    assert not ok and "GB" in why


def test_spsolve_paths(monkeypatch):
    n = 3000
    A = sp.diags([-1, 2.5, -1], [-1, 0, 1], shape=(n, n)).tocsr()
    b = np.ones(n)
    x = compute.spsolve(A, b)
    assert np.allclose(A @ x, b)
    calls = []
    fake = type(sys)("pypardiso")
    fake.spsolve = lambda M, v: calls.append(M.shape) or np.linalg.solve(M.toarray(), v)
    monkeypatch.setitem(sys.modules, "pypardiso", fake)
    monkeypatch.setattr(compute, "_INFO", {**compute.info(), "pardiso": True, "cupy": False})
    assert np.allclose(A @ compute.spsolve(A, b), b) and calls == [(n, n)]
    assert compute.solver_name(n).startswith("PARDISO")
