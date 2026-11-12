"""Part thumbnails for DIMS: a wireframe icon (feature edges, isometric) of a part's 3D model, the preview a
SolidWorks file carries, or a picture file — cached per PC, keyed by file path + modified time.

STL / OBJ are read here with numpy; STEP / IGES / BREP / .daedalus need the CAD kernel, so they are tessellated in
a child process (`python -m daedalus.inventory.thumbs --tessellate IN OUT.stl`) and the inventory window stays
light. Without a model, parts get a drawn icon for their type."""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
MESH_EXTS = {".stl", ".obj"}
KERNEL_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".daedalus", ".3mf"}
SW_EXTS = {".sldprt", ".sldasm", ".slddrw"}
SIZE = 96


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    d = Path(base) / "Daedalus" / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(src: str) -> Path | None:
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = hashlib.sha1(f"{os.path.abspath(src)}|{int(st.st_mtime)}|{st.st_size}|{SIZE}".encode()).hexdigest()[:20]
    return cache_dir() / f"{key}.png"


def source_for(p) -> str:
    """The file a part's thumbnail comes from: its picture, else its model, else a model-type drawing link."""
    for k in ("imageFile", "cadFile", "drawingUrl"):
        v = (p.get(k) or "").strip()
        ext = os.path.splitext(v)[1].lower()
        if v and not v.startswith(("http://", "https://")) and ext in IMAGE_EXTS | MESH_EXTS | KERNEL_EXTS | SW_EXTS:
            return v
    return ""


def cached(p) -> Path | None:
    src = source_for(p)
    if not src:
        return None
    c = cache_path(src)
    return c if c and c.exists() else None


# ------------------------------------------------------------------ meshes
def read_stl(path) -> tuple[np.ndarray, np.ndarray]:
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + n * 50 == len(data):
            rec = np.frombuffer(data[84:84 + n * 50], dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]))
            tri = rec["v"].reshape(-1, 3).astype(np.float64)
            return _weld(tri)
    pts = [list(map(float, ln.split()[1:4])) for ln in data.decode("ascii", "ignore").splitlines()
           if ln.strip().startswith("vertex")]
    return _weld(np.array(pts, dtype=np.float64).reshape(-1, 3))


def read_obj(path) -> tuple[np.ndarray, np.ndarray]:
    verts, faces = [], []
    for ln in Path(path).read_text(errors="ignore").splitlines():
        t = ln.split()
        if not t:
            continue
        if t[0] == "v":
            verts.append([float(x) for x in t[1:4]])
        elif t[0] == "f":
            idx = [int(x.split("/")[0]) for x in t[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            faces += [[idx[0], idx[k], idx[k + 1]] for k in range(1, len(idx) - 1)]
    return np.array(verts, dtype=np.float64).reshape(-1, 3), np.array(faces, dtype=np.int64).reshape(-1, 3)


def _weld(tri_pts):
    """Triangle soup (3n × 3) → shared vertices + faces."""
    if len(tri_pts) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    scale = float(np.ptp(tri_pts, axis=0).max()) or 1.0
    q = np.round(tri_pts / (scale * 1e-6)).astype(np.int64)
    uniq, inv = np.unique(q, axis=0, return_inverse=True)
    verts = np.zeros((len(uniq), 3))
    verts[inv.ravel()] = tri_pts
    return verts, inv.reshape(-1, 3)


def feature_edges(verts, faces, angle_deg=35.0) -> np.ndarray:
    """Segments (m × 2 × 3) where neighbouring faces meet at more than angle_deg, plus open edges."""
    if len(faces) == 0:
        return np.zeros((0, 2, 3))
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    nrm = np.cross(b - a, c - a)
    ln = np.linalg.norm(nrm, axis=1)
    keep = ln > 0
    faces, nrm = faces[keep], nrm[keep] / ln[keep][:, None]
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    fid = np.tile(np.arange(len(faces)), 3)
    es = np.sort(e, axis=1)
    order = np.lexsort((es[:, 1], es[:, 0]))
    es, fid = es[order], fid[order]
    same = np.all(es[1:] == es[:-1], axis=1)
    cos = np.cos(np.radians(angle_deg))
    out = []
    i, n = 0, len(es)
    while i < n:
        j = i
        while j + 1 < n and same[j]:
            j += 1
        if j == i:                                         # open edge
            out.append(es[i])
        else:
            fs = fid[i:j + 1]
            d = nrm[fs] @ nrm[fs].T
            if d.min() < cos:
                out.append(es[i])
        i = j + 1
    if not out:
        return np.zeros((0, 2, 3))
    idx = np.array(out)
    return np.stack([verts[idx[:, 0]], verts[idx[:, 1]]], axis=1)


def iso_project(segs) -> np.ndarray:
    """Isometric view (looking from +X +Y +Z toward the origin, Z up) → 2D segments (m × 2 × 2), y down."""
    r = np.array([[np.cos(np.pi / 4), -np.sin(np.pi / 4), 0], [np.sin(np.pi / 4), np.cos(np.pi / 4), 0], [0, 0, 1]])
    t = np.arctan(1 / np.sqrt(2))
    rx = np.array([[1, 0, 0], [0, np.cos(t), -np.sin(t)], [0, np.sin(t), np.cos(t)]])
    m = rx @ r
    p = segs.reshape(-1, 3) @ m.T
    xy = np.stack([p[:, 0], -p[:, 2]], axis=1)             # screen x = rotated x, screen y = -z (up)
    return xy.reshape(-1, 2, 2)


def render_png(segs2d, out: Path, size=SIZE, color="#7aa7ff", width=1.2):
    from PySide6.QtCore import QLineF, QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    if len(segs2d):
        pts = segs2d.reshape(-1, 2)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        span = float(max(hi - lo)) or 1.0
        s = (size - 8) / span
        off = (size - (hi - lo) * s) / 2
        qp = QPainter(img)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap))
        lines = [QLineF(QPointF(*((a - lo) * s + off)), QPointF(*((b - lo) * s + off))) for a, b in segs2d]
        qp.drawLines(lines)
        qp.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out), "PNG")
    return out


def sw_preview(path) -> bytes | None:
    """The largest PNG embedded in a SolidWorks file (its saved preview), if it has one."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    best, i = None, 0
    sig = b"\x89PNG\r\n\x1a\n"
    while True:
        i = data.find(sig, i)
        if i < 0:
            break
        j = data.find(b"IEND", i)
        if j < 0:
            break
        png = data[i:j + 8]
        if best is None or len(png) > len(best):
            best = png
        i = j
    return best


def _scaled(img_bytes_or_path, out: Path, size=SIZE):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    img = QImage()
    ok = img.loadFromData(img_bytes_or_path) if isinstance(img_bytes_or_path, (bytes, bytearray)) else \
        img.load(str(img_bytes_or_path))
    if not ok or img.isNull():
        return None
    img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(out), "PNG")
    return out


def make(src: str, out: Path | None = None, color="#7aa7ff", timeout=180) -> Path | None:
    """Build the thumbnail for one file (blocking). None if the file can't give one."""
    out = out or cache_path(src)
    if out is None:
        return None
    ext = os.path.splitext(src)[1].lower()
    if ext in IMAGE_EXTS:
        return _scaled(src, out)
    if ext in SW_EXTS:
        png = sw_preview(src)
        return _scaled(png, out) if png else None
    if ext == ".stl":
        v, f = read_stl(src)
    elif ext == ".obj":
        v, f = read_obj(src)
    elif ext in KERNEL_EXTS:
        stl = out.with_suffix(".tmp.stl")
        try:
            root = str(Path(__file__).resolve().parents[2])
            env = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
            subprocess.run([sys.executable, "-m", "daedalus.inventory.thumbs", "--tessellate", os.path.abspath(src),
                            str(stl.resolve())], check=True, timeout=timeout, capture_output=True, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            v, f = read_stl(stl)
        except (subprocess.SubprocessError, OSError):
            return None
        finally:
            stl.unlink(missing_ok=True)
    else:
        return None
    if len(f) == 0:
        return None
    return render_png(iso_project(feature_edges(v, f)), out, color=color)


def _tessellate(src, out_stl):
    from ..core.document import _load_any
    doc = _load_any(Path(src))
    doc.export(Path(out_stl))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--tessellate":
        _tessellate(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2:
        print(make(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else None))
