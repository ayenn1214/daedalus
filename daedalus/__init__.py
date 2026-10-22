"""Daedalus — parametric desktop CAD with a Claude copilot."""
__version__ = "2.9.0"
APP_NAME = "Daedalus"
FILE_EXT = ".daedalus"
LEGACY_EXTS = (".strata",)                  # files saved by Strata CAD (pre-rename) open unchanged
NATIVE_EXTS = (FILE_EXT, *LEGACY_EXTS)


def is_native(path) -> bool:
    return str(path).lower().endswith(NATIVE_EXTS)


def _fast_build123d():
    """build123d imports scikit-learn (≈1 s) only for DBSCAN in STL→B-rep conversion. While build123d loads,
    give it a lazy stand-in so sklearn is imported on first use instead of at startup."""
    import importlib.abc
    import importlib.util
    import sys
    import types

    class _LazyDBSCAN:
        def __new__(cls, *a, **k):
            from sklearn.cluster import DBSCAN
            return DBSCAN(*a, **k)

    class _Loader(importlib.abc.Loader):
        def __init__(self, real):
            self.real = real

        def create_module(self, spec):
            return self.real.create_module(spec)

        def exec_module(self, module):
            if "sklearn" in sys.modules:
                return self.real.exec_module(module)
            fakes = {"sklearn": types.ModuleType("sklearn"), "sklearn.cluster": types.ModuleType("sklearn.cluster")}
            fakes["sklearn"].__path__ = []
            fakes["sklearn"].cluster = fakes["sklearn.cluster"]
            fakes["sklearn.cluster"].DBSCAN = _LazyDBSCAN
            sys.modules.update(fakes)
            try:
                self.real.exec_module(module)
            finally:
                for k, v in fakes.items():
                    if sys.modules.get(k) is v:
                        del sys.modules[k]

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "build123d":
                return None
            sys.meta_path.remove(self)
            spec = importlib.util.find_spec(name)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader)
            return spec

    import sys as _s
    if "build123d" not in _s.modules:
        _s.meta_path.insert(0, _Finder())


try:
    _fast_build123d()
except Exception:  # noqa: BLE001  (never block startup over an optimisation)
    pass
