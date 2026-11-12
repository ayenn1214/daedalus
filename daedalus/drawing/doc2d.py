"""Light document for the drawings module on its own: 2D drawings (DXF / DWG / SVG / HPGL editing, blank sheets,
title blocks, revisions, GD&T, export) without the CAD kernel. Model views need the 3D workspace."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ..errors import FeatureError
from .docmix import DrawingDocMixin
from .model import Drawing

NEEDS_3D = "Model views need the 3D workspace (start Daedalus with 3D, or File ▸ Open 3D workspace)"


def _strip(o):
    """Drop '_'-prefixed cache keys (computed view geometry)."""
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


class Doc2D(DrawingDocMixin):
    lite = True

    def __init__(self):
        self.drawings: list[Drawing] = []
        self.dirty = False
        self.path: Path | None = None
        self.objects: list = []
        self.explode = None
        self.result = SimpleNamespace(bodies={}, flats={}, errors={})
        self._listeners = []
        self._undo: list[str] = []
        self._redo: list[str] = []

    # ---- host hooks the mixin expects
    def subscribe(self, fn):
        self._listeners.append(fn)

    def _emit(self):
        for fn in self._listeners:
            fn()

    def _state(self) -> str:
        return json.dumps([_strip(d.to_dict()) for d in self.drawings])

    def _restore(self, state: str):
        self.drawings = [Drawing.from_dict(x) for x in json.loads(state)]
        self.dirty = True
        self._emit()

    def checkpoint(self):
        self._undo.append(self._state())
        self._undo = self._undo[-200:]
        self._redo.clear()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self._restore(self._redo.pop())
        return True

    def obj(self, name):
        raise FeatureError(NEEDS_3D)

    def auto_explode(self, **_kw):
        return None

    # ---- model-dependent operations
    def new_drawing(self, name: str | None = None, size: str = "A3", views=(), **kw):
        if views or kw.get("flat") or kw.get("objects"):
            raise FeatureError(NEEDS_3D)
        return super().new_drawing(name=name, size=size, views=(), **kw)

    def add_drawing_view(self, *a, **kw):
        raise FeatureError(NEEDS_3D)

    # ---- files: drawings from a .daedalus file (no model), or 2D files
    def load_drawings(self, path) -> list[Drawing]:
        """Read the drawings of a .daedalus file (their model views stay empty without the 3D workspace)."""
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        got = [Drawing.from_dict(x) for x in d.get("drawings", [])]
        if not got:
            raise FeatureError(f"{Path(path).name} has no drawings")
        self.checkpoint()
        self.drawings.extend(got)
        self.dirty = True
        self._emit()
        return got
