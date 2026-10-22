"""Recent files, pins, thumbnails and autosave/recovery for the Home screen (no widgets here)."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

MAX_RECENT = 30


def home_dir() -> Path:
    d = Path(os.environ.get("DAEDALUS_HOME") or Path.home() / ".daedalus")
    return d


def _thumb_dir() -> Path:
    return home_dir() / "thumbs"


def _auto_dir() -> Path:
    return home_dir() / "autosave"


def _norm(p) -> str:
    return str(Path(p))


class Recent:
    """Recent-file list kept in QSettings (``recent_files`` / ``recent_pinned``)."""

    def __init__(self, settings):
        self.s = settings

    def _get(self, key) -> list[str]:
        v = self.s.value(key, [], type=list) or []
        return [str(x) for x in v if x]

    def files(self) -> list[str]:
        return self._get("recent_files")

    def pinned(self) -> list[str]:
        return self._get("recent_pinned")

    def add(self, path):
        p = _norm(path)
        rec = [r for r in self.files() if _norm(r) != p]
        self.s.setValue("recent_files", [p] + rec[:MAX_RECENT - 1])

    def remove(self, path):
        p = _norm(path)
        self.s.setValue("recent_files", [r for r in self.files() if _norm(r) != p])
        self.s.setValue("recent_pinned", [r for r in self.pinned() if _norm(r) != p])

    def clear(self, keep_pinned=True):
        pins = set(self.pinned()) if keep_pinned else set()
        self.s.setValue("recent_files", [r for r in self.files() if r in pins])
        if not keep_pinned:
            self.s.setValue("recent_pinned", [])

    def is_pinned(self, path) -> bool:
        return _norm(path) in {_norm(p) for p in self.pinned()}

    def set_pinned(self, path, on: bool):
        p = _norm(path)
        pins = [r for r in self.pinned() if _norm(r) != p]
        if on:
            pins.insert(0, p)
            if p not in {_norm(r) for r in self.files()}:
                self.add(p)
        self.s.setValue("recent_pinned", pins)

    def entries(self, query: str = "") -> list[dict]:
        """Pinned first, then most recent. Each: path, name, folder, ext, exists, mtime, size, pinned, thumb."""
        pins = self.pinned()
        seen, out = set(), []
        q = [t for t in (query or "").lower().split() if t]
        for p in pins + self.files():
            k = _norm(p)
            if k in seen:
                continue
            seen.add(k)
            P = Path(k)
            if q and not all(t in k.lower() for t in q):
                continue
            try:
                st = P.stat()
                exists, mtime, size = True, st.st_mtime, st.st_size
            except OSError:
                exists, mtime, size = False, 0.0, 0
            th = thumb_path(k)
            out.append({"path": k, "name": P.name, "folder": str(P.parent), "ext": P.suffix.lower().lstrip("."),
                        "exists": exists, "mtime": mtime, "size": size, "pinned": k in {_norm(x) for x in pins},
                        "thumb": str(th) if th.exists() else None})
        return out


# ------------------------------------------------------------------ thumbnails
def thumb_path(path) -> Path:
    h = hashlib.sha1(_norm(path).lower().encode()).hexdigest()[:20]
    return _thumb_dir() / f"{h}.png"


def save_thumb(view, path, size=(360, 240)) -> Path | None:
    """Grab the 3D view into a small PNG for the Home screen. Never raises."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        out = thumb_path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".full.png")
        view.screenshot(str(tmp))
        img = QImage(str(tmp))
        tmp.unlink(missing_ok=True)
        if img.isNull():
            return None
        w, h = size
        img = img.scaled(w * 2, h * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        img = img.copy((img.width() - w * 2) // 2, (img.height() - h * 2) // 2, w * 2, h * 2)
        img = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        img.save(str(out))
        return out
    except Exception:  # noqa: BLE001
        return None


def ago(t: float, now: float | None = None) -> str:
    if not t:
        return ""
    s = max(0, (now or time.time()) - t)
    for n, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400 * 7, "w"), (86400, "d"), (3600, "h"),
                    (60, "min")):
        if s >= n:
            return f"{int(s // n)} {unit} ago"
    return "just now"


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return ""


# ------------------------------------------------------------------ autosave
class Autosave:
    """Periodic copies of unsaved work in ~/.daedalus/autosave; a clean exit or a save removes them, so any
    left at start-up are from a crash and offered for recovery."""

    def __init__(self):
        self.session = uuid.uuid4().hex[:12]
        self.last_cost = 0.0

    def _paths(self, sid=None):
        d = _auto_dir()
        sid = sid or self.session
        return d / f"{sid}.daedalus", d / f"{sid}.json"

    def write(self, doc, source: str | None = None) -> float:
        """Snapshot `doc` (without changing its path/dirty flag). Returns seconds taken."""
        t = time.perf_counter()
        data, meta = self._paths()
        data.parent.mkdir(parents=True, exist_ok=True)
        tmp = data.with_suffix(".tmp")
        doc.snapshot_to(tmp)
        os.replace(tmp, data)
        title = doc.path.name if doc.path else (Path(source).name if source else "Untitled")
        meta.write_text(json.dumps({"title": title, "path": str(doc.path) if doc.path else None,
                                    "source": source, "saved": time.time(), "pid": os.getpid()}))
        self.last_cost = time.perf_counter() - t
        return self.last_cost

    def discard(self, sid=None):
        for p in self._paths(sid):
            try:
                p.unlink()
            except OSError:
                pass

    def orphans(self) -> list[dict]:
        """Autosaves left by earlier sessions (crashes)."""
        d = _auto_dir()
        if not d.is_dir():
            return []
        out = []
        for m in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            sid = m.stem
            if sid == self.session:
                continue
            data = m.with_suffix(".daedalus")
            if not data.exists():
                continue
            try:
                meta = json.loads(m.read_text())
            except Exception:  # noqa: BLE001
                meta = {}
            if meta.get("pid") and meta["pid"] != os.getpid() and _alive(meta["pid"]) and \
                    time.time() - float(meta.get("saved") or 0) < 600:
                continue                                       # another Daedalus window is still running
            out.append({"id": sid, "file": str(data), **meta})
        return out


def _alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                                          # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:  # noqa: BLE001
        return False
