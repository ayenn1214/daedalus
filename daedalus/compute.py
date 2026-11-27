"""Where heavy computation runs: every CPU core (numba threads, multithreaded sparse solvers) or an NVIDIA
graphics card (CUDA). Settings ▸ Compute picks auto / CPU / graphics card and the number of CPU threads.

- Flow (lattice-Boltzmann): numba on all cores, or CUDA kernels on the graphics card (sim/lbm.py).
- Structural / thermal solves: the GPU sparse solver (CuPy) when chosen and installed, else Intel MKL PARDISO
  (pypardiso, multithreaded) when installed, else SciPy.
Nothing here needs Qt. Optional installs: numba-cuda[cu12] (CUDA flow), cupy-cuda12x (GPU solves), pypardiso."""
from __future__ import annotations

import os

_SETTINGS = {"device": None, "threads": None}
_INFO: dict | None = None


def _qsettings(key, default):
    if _SETTINGS.get(key.split("/")[-1]) is not None:
        return _SETTINGS[key.split("/")[-1]]
    try:
        from PySide6.QtCore import QSettings
        return QSettings("Daedalus", "Daedalus").value(key, default)
    except Exception:  # noqa: BLE001
        return default


def configure(device: str | None = None, threads: int | None = None):
    """Override the Settings (tests, scripts); None = use Settings."""
    _SETTINGS["device"], _SETTINGS["threads"] = device, threads
    apply_threads()


def device() -> str:
    """'auto' | 'cpu' | 'gpu' (DAEDALUS_DEVICE wins)."""
    d = os.environ.get("DAEDALUS_DEVICE") or _qsettings("compute/device", "auto") or "auto"
    return d if d in ("auto", "cpu", "gpu") else "auto"


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def threads() -> int:
    """CPU threads to use (Settings; 0 = all)."""
    try:
        n = int(_qsettings("compute/threads", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return max(1, min(n, cpu_count())) if n > 0 else cpu_count()


def apply_threads():
    """Tell numba and the math libraries how many threads to use."""
    n = threads()
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[k] = str(n)
    try:
        import numba
        numba.set_num_threads(min(n, numba.config.NUMBA_NUM_THREADS))
    except Exception:  # noqa: BLE001
        pass
    return n


# ------------------------------------------------------------------ the graphics card
def _probe() -> dict:
    info = {"cuda": False, "gpu": "", "memory_gb": 0.0, "free_gb": 0.0, "compute": "", "why": "", "cupy": False,
            "pardiso": False, "numba": False}
    try:
        import numba  # noqa: F401
        info["numba"] = True
    except ImportError:
        info["why"] = "numba isn't installed"
    try:
        from numba import cuda
        if cuda.is_available():
            dev = cuda.get_current_device()
            name = dev.name.decode() if isinstance(dev.name, bytes) else str(dev.name)
            free, total = cuda.current_context().get_memory_info()
            info.update(cuda=True, gpu=name, memory_gb=round(total / 1e9, 1), free_gb=round(free / 1e9, 1),
                        compute=".".join(map(str, dev.compute_capability)))
        else:
            info["why"] = info["why"] or ("no NVIDIA graphics card with a working driver and CUDA libraries "
                                          "(pip install numba-cuda[cu12])")
    except Exception as e:  # noqa: BLE001
        info["why"] = info["why"] or f"CUDA not available ({e.__class__.__name__}: {e})"
    try:
        import cupy
        info["cupy"] = bool(cupy.cuda.runtime.getDeviceCount())
    except Exception:  # noqa: BLE001
        pass
    try:
        import pypardiso  # noqa: F401
        info["pardiso"] = True
    except Exception:  # noqa: BLE001
        pass
    return info


def info(refresh: bool = False) -> dict:
    global _INFO
    if _INFO is None or refresh:
        _INFO = _probe()
        _INFO["threads"], _INFO["cores"] = threads(), cpu_count()
    return _INFO


def gpu_name() -> str:
    return info().get("gpu") or "graphics card"


def cuda_ok(need_bytes: int = 0) -> tuple[bool, str]:
    i = info()
    if not i["cuda"]:
        return False, i["why"] or "CUDA not available"
    if need_bytes and need_bytes > i["free_gb"] * 1e9 * 0.9:
        return False, (f"the grid needs {need_bytes / 1e9:.1f} GB and the graphics card has {i['free_gb']:.1f} GB "
                       "free — lower max cells")
    return True, ""


def use_gpu() -> bool:
    return device() in ("auto", "gpu") and info()["cuda"]


# ------------------------------------------------------------------ sparse linear solves (FEA, thermal)
def spsolve(A, b):
    """A x = b for the structural / thermal solvers: GPU (CuPy) → PARDISO (all cores) → SciPy."""
    import numpy as np
    from scipy.sparse.linalg import spsolve as sp
    b = np.asarray(b)
    if device() in ("auto", "gpu") and info()["cupy"] and A.shape[0] > 20000:
        try:
            import cupy
            import cupyx.scipy.sparse as cps
            import cupyx.scipy.sparse.linalg as cpl
            x = cpl.spsolve(cps.csr_matrix(A.tocsr()), cupy.asarray(b))
            out = cupy.asnumpy(x)
            if np.all(np.isfinite(out)):
                return out
        except Exception:  # noqa: BLE001
            pass
    if info()["pardiso"] and A.shape[0] > 2000:
        try:
            import pypardiso
            return pypardiso.spsolve(A.tocsr(), b)
        except Exception:  # noqa: BLE001
            pass
    return sp(A.tocsc(), b)


def solver_name(n: int = 10 ** 6) -> str:
    i = info()
    if device() in ("auto", "gpu") and i["cupy"] and n > 20000:
        return f"GPU sparse solver ({i['gpu']})"
    if i["pardiso"] and n > 2000:
        return f"PARDISO ({threads()} threads)"
    return "SciPy SuperLU"


def summary() -> str:
    i = info()
    cpu = f"CPU: {i['cores']} cores, using {i['threads']}" + ("" if i["numba"] else " (numba missing — slow)")
    gpu = f"Graphics card: {i['gpu']} · {i['memory_gb']} GB · CUDA {i['compute']}" if i["cuda"] else \
        f"Graphics card: not used — {i['why']}"
    solv = "Sparse solver: " + ("GPU (CuPy) · " if i["cupy"] else "") + ("PARDISO" if i["pardiso"] else "SciPy")
    return "\n".join((cpu, gpu, solv))
