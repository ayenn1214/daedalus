"""PDF document model and editing (pypdf). No Qt here except the mark-up overlay writer (QPdfWriter).

A PdfDoc is an ordered list of PageRefs. Each page points at a page of a source file (or is a blank page) and carries
an extra rotation and its mark-ups. Nothing touches the source files until save(), which writes a new file (to a
temporary name first, then swaps it in), so combining, reordering and undo are free.

Mark-ups are dicts in PDF points on the page as displayed (origin top-left, y down):
  {"kind": "text", "x", "y", "text", "size", "color"}
  {"kind": "rect" | "highlight" | "whiteout", "x", "y", "w", "h", "color", "width"}
  {"kind": "line" | "arrow", "x1", "y1", "x2", "y2", "color", "width"}
  {"kind": "ink", "points": [[x, y], …], "color", "width"}
  {"kind": "image", "x", "y", "w", "h", "png": base64}
On save they are drawn into the page itself (flattened), so every viewer shows them.
"""
from __future__ import annotations

import base64
import copy
import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation

LETTER = (612.0, 792.0)
A4 = (595.28, 841.89)


class PdfError(Exception):
    pass


@dataclass
class PageRef:
    src: str | None              # source file (None = blank page)
    index: int = 0               # page index in the source
    rotate: int = 0              # extra rotation, multiple of 90 (clockwise)
    marks: list = field(default_factory=list)
    blank: tuple | None = None   # (w, h) points for a blank page

    def copy(self) -> PageRef:
        return PageRef(self.src, self.index, self.rotate, copy.deepcopy(self.marks), self.blank)


class PdfDoc:
    def __init__(self, path: str | None = None):
        self.path: str | None = None
        self.pages: list[PageRef] = []
        self._readers: dict[str, PdfReader] = {}
        self._data: dict[str, bytes] = {}
        self._undo: list = []
        self._redo: list = []
        self.dirty = False
        if path:
            self.add_file(path)
            self.path = str(path)
            self.dirty = False
            self._undo.clear()

    # ------------------------------------------------------------------ sources
    def data(self, src: str) -> bytes:
        """The source file's bytes, read once (so the file isn't held open and can be overwritten on save)."""
        if src not in self._data:
            try:
                self._data[src] = Path(src).read_bytes()
            except OSError as e:
                raise PdfError(f"Can't read {Path(src).name}: {e}") from e
        return self._data[src]

    def reader(self, src: str) -> PdfReader:
        if src not in self._readers:
            try:
                r = PdfReader(io.BytesIO(self.data(src)))
                if r.is_encrypted and not r.decrypt(""):
                    raise PdfError(f"{Path(src).name} is password-protected")
                len(r.pages)
            except PdfError:
                raise
            except Exception as e:
                raise PdfError(f"{Path(src).name} isn't a readable PDF ({e})") from e
            self._readers[src] = r
        return self._readers[src]

    def base_size(self, i: int) -> tuple[float, float]:
        """Page size as the source shows it (crop box with its own /Rotate), before our extra rotation."""
        ref = self.pages[i]
        if ref.blank:
            return ref.blank
        pg = self.reader(ref.src).pages[ref.index]
        w, h = float(pg.cropbox.width), float(pg.cropbox.height)
        return (h, w) if (pg.get("/Rotate", 0) or 0) % 180 else (w, h)

    def size(self, i: int) -> tuple[float, float]:
        """Page size as displayed (points)."""
        w, h = self.base_size(i)
        return (h, w) if self.pages[i].rotate % 180 else (w, h)

    def __len__(self):
        return len(self.pages)

    # ------------------------------------------------------------------ undo
    def _snap(self):
        self._undo.append([p.copy() for p in self.pages])
        del self._undo[:-100]
        self._redo.clear()
        self.dirty = True

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.pages)
        self.pages = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.pages)
        self.pages = self._redo.pop()
        self.dirty = True
        return True

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    # ------------------------------------------------------------------ page operations
    def add_file(self, path, at: int | None = None, pages: list[int] | None = None) -> int:
        """Insert a PDF's pages (all, or the given 0-based indices) at `at` (end by default). Returns how many."""
        src = str(Path(path).resolve())
        n = len(self.reader(src).pages)
        idx = [i for i in (pages if pages is not None else range(n)) if 0 <= i < n]
        self._snap()
        at = len(self.pages) if at is None else max(0, min(at, len(self.pages)))
        self.pages[at:at] = [PageRef(src, i) for i in idx]
        return len(idx)

    def delete(self, idx):
        idx = sorted(set(i for i in idx if 0 <= i < len(self.pages)))
        if not idx:
            return
        if len(idx) >= len(self.pages):
            raise PdfError("A PDF needs at least one page")
        self._snap()
        for i in reversed(idx):
            del self.pages[i]

    def rotate(self, idx, deg: int):
        self._snap()
        for i in idx:
            self.pages[i].rotate = (self.pages[i].rotate + deg) % 360
            self.pages[i].marks = [_rot_mark(m, deg, *self._pre_rot_size(i, deg)) for m in self.pages[i].marks]

    def _pre_rot_size(self, i, deg):
        w, h = self.size(i)                       # already the new size: undo the swap for the old one
        return (h, w) if deg % 180 else (w, h)

    def move(self, idx, to: int):
        """Move pages (in their order) so the first lands at position `to` of the remaining list."""
        idx = sorted(set(idx))
        self._snap()
        moving = [self.pages[i] for i in idx]
        for i in reversed(idx):
            del self.pages[i]
        to = max(0, min(to - sum(1 for i in idx if i < to), len(self.pages)))
        self.pages[to:to] = moving
        return list(range(to, to + len(moving)))

    def reorder(self, order: list[int]):
        if sorted(order) != list(range(len(self.pages))):
            raise PdfError("Page order must list every page once")
        self._snap()
        self.pages = [self.pages[i] for i in order]

    def duplicate(self, idx):
        idx = sorted(set(idx))
        self._snap()
        for k, i in enumerate(idx):
            self.pages.insert(i + 1 + k, self.pages[i + k].copy())

    def insert_blank(self, at: int, size=None):
        self._snap()
        if size is None:
            size = self.size(min(max(at - 1, 0), len(self.pages) - 1)) if self.pages else LETTER
        self.pages.insert(max(0, min(at, len(self.pages))), PageRef(None, 0, 0, [], tuple(size)))

    def reverse(self, idx=None):
        idx = sorted(idx) if idx else list(range(len(self.pages)))
        self._snap()
        rev = [self.pages[i] for i in reversed(idx)]
        for k, i in enumerate(idx):
            self.pages[i] = rev[k]

    # ------------------------------------------------------------------ mark-ups
    def add_mark(self, i: int, mark: dict):
        self._snap()
        self.pages[i].marks.append(dict(mark))
        return len(self.pages[i].marks) - 1

    def set_mark(self, i: int, k: int, mark: dict, snap=True):
        if snap:
            self._snap()
        self.pages[i].marks[k] = dict(mark)

    def delete_mark(self, i: int, k: int):
        self._snap()
        del self.pages[i].marks[k]

    def clear_marks(self, idx):
        self._snap()
        for i in idx:
            self.pages[i].marks = []

    def has_marks(self) -> bool:
        return any(p.marks for p in self.pages)

    # ------------------------------------------------------------------ text
    def text(self, i: int) -> str:
        ref = self.pages[i]
        if ref.blank:
            return ""
        try:
            return self.reader(ref.src).pages[ref.index].extract_text() or ""
        except Exception:  # noqa: BLE001
            return ""

    def find(self, query: str) -> list[int]:
        q = query.lower().strip()
        return [i for i in range(len(self.pages)) if q and q in self.text(i).lower()] if q else []

    def metadata(self) -> dict:
        srcs = [p.src for p in self.pages if p.src]
        if not srcs:
            return {}
        try:
            m = self.reader(srcs[0]).metadata or {}
            return {k.lstrip("/"): str(v) for k, v in m.items()}
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ write
    def build(self, idx=None, title: str | None = None) -> PdfWriter:
        w = PdfWriter()
        for i in (range(len(self.pages)) if idx is None else idx):
            ref = self.pages[i]
            if ref.blank:
                pg = w.add_blank_page(*ref.blank)
                if ref.rotate:
                    pg.rotate(ref.rotate)
            else:
                pg = w.add_page(self.reader(ref.src).pages[ref.index])
                if ref.rotate:
                    pg.rotate(ref.rotate)
            if ref.marks:
                if pg.get("/Rotate", 0):
                    pg.transfer_rotation_to_content()
                cb = pg.cropbox
                ov = PdfReader(io.BytesIO(overlay_pdf(float(cb.width), float(cb.height), ref.marks))).pages[0]
                pg.merge_transformed_page(ov, Transformation().translate(float(cb.left), float(cb.bottom)))
        meta = {"/Producer": "Daedalus"}
        if title:
            meta["/Title"] = title
        w.add_metadata(meta)
        return w

    def save(self, path=None, idx=None) -> str:
        """Write the PDF (all pages, or `idx`). Overwriting a source file is safe: sources are read into memory."""
        path = str(path or self.path or "")
        if not path:
            raise PdfError("Choose where to save")
        w = self.build(idx, Path(path).stem)
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(Path(path).parent))
        try:
            with os.fdopen(fd, "wb") as f:
                w.write(f)
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise PdfError(f"Couldn't save {Path(path).name}: {e}") from e
        if idx is None:                       # the saved file is the new baseline (marks flattened, rotations applied)
            key = str(Path(path).resolve())
            self._data.pop(key, None)
            self._readers.pop(key, None)
            self.pages = [PageRef(key, i) for i in range(len(self.pages))]
            self._undo.clear()
            self._redo.clear()
            self.path = path
            self.dirty = False
        return path

    def split(self, folder, every: int = 1, ranges: list[tuple[int, int]] | None = None, stem: str | None = None):
        """Write one file per `every` pages, or per (first, last) 0-based inclusive range. Returns the paths."""
        stem = stem or (Path(self.path).stem if self.path else "pages")
        groups = ranges or [(a, min(a + every, len(self.pages)) - 1) for a in range(0, len(self.pages), max(1, every))]
        out = []
        for a, b in groups:
            name = f"{stem}_p{a + 1}" + (f"-{b + 1}" if b > a else "") + ".pdf"
            out.append(self.save(Path(folder) / name, list(range(a, b + 1))))
        return out


def parse_ranges(text: str, n: int) -> list[int]:
    """'1-3, 5, 8-' → 0-based indices (1-based input, inclusive; open ends allowed)."""
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
        else:
            a = b = int(part)
        if a < 1 or b > n or a > b:
            raise PdfError(f"'{part}' isn't a page range in 1–{n}")
        out += range(a - 1, b)
    return out


def merge(paths, out) -> str:
    d = PdfDoc()
    for p in paths:
        d.add_file(p)
    return d.save(out)


# ------------------------------------------------------------------ geometry helpers
def _rot_mark(m: dict, deg: int, w: float, h: float) -> dict:
    """Rotate a mark with its page (clockwise by deg) — w, h = page size before the turn."""
    turns = (deg // 90) % 4
    m = copy.deepcopy(m)
    for _ in range(turns):
        def pt(x, y, W=w, H=h):
            return H - y, x
        if "points" in m:
            m["points"] = [list(pt(x, y)) for x, y in m["points"]]
        if "x1" in m:
            m["x1"], m["y1"] = pt(m["x1"], m["y1"])
            m["x2"], m["y2"] = pt(m["x2"], m["y2"])
        if "w" in m and "x" in m:
            x, y = pt(m["x"], m["y"] + m["h"])
            m["x"], m["y"], m["w"], m["h"] = x, y, m["h"], m["w"]
        elif "x" in m:
            m["x"], m["y"] = pt(m["x"], m["y"])
        w, h = h, w
    return m


def mark_bounds(m: dict) -> tuple[float, float, float, float]:
    """(x, y, w, h) around a mark, points."""
    k = m.get("kind")
    if k == "text":
        lines = str(m.get("text", "")).split("\n") or [""]
        s = float(m.get("size", 12))
        return m["x"], m["y"], max(len(x) for x in lines) * s * 0.55 + 2, len(lines) * s * 1.25
    if m.get("points"):
        xs, ys = [p[0] for p in m["points"]], [p[1] for p in m["points"]]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    if "x1" in m:
        return min(m["x1"], m["x2"]), min(m["y1"], m["y2"]), abs(m["x2"] - m["x1"]), abs(m["y2"] - m["y1"])
    return m.get("x", 0), m.get("y", 0), m.get("w", 0), m.get("h", 0)


def move_mark(m: dict, dx: float, dy: float) -> dict:
    m = copy.deepcopy(m)
    if "points" in m:
        m["points"] = [[x + dx, y + dy] for x, y in m["points"]]
    if "x1" in m:
        m["x1"] += dx
        m["x2"] += dx
        m["y1"] += dy
        m["y2"] += dy
    if "x" in m:
        m["x"] += dx
        m["y"] += dy
    return m


def image_mark(png_bytes: bytes, x: float, y: float, w: float, h: float) -> dict:
    return {"kind": "image", "x": x, "y": y, "w": w, "h": h, "png": base64.b64encode(png_bytes).decode()}


# ------------------------------------------------------------------ drawing (screen and PDF share this)
def paint_marks(p, marks, scale: float = 1.0, selected: int | None = None):
    """Draw marks with a QPainter whose units are points × scale."""
    import math

    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QImage,
        QPainterPath,
        QPen,
        QPolygonF,
    )
    for k, m in enumerate(marks):
        kind = m.get("kind")
        col = QColor(m.get("color") or ("#ffe14d" if kind == "highlight" else "#d11a1a"))
        pen = QPen(col, max(0.5, float(m.get("width", 1.5))) * scale)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if kind in ("rect", "highlight", "whiteout"):
            r = QRectF(m["x"] * scale, m["y"] * scale, m["w"] * scale, m["h"] * scale)
            if kind == "highlight":
                c = QColor(col)
                c.setAlpha(110)
                p.fillRect(r, c)
            elif kind == "whiteout":
                p.fillRect(r, QColor(m.get("color") or "#ffffff"))
            else:
                p.drawRect(r)
        elif kind in ("line", "arrow"):
            a, b = QPointF(m["x1"] * scale, m["y1"] * scale), QPointF(m["x2"] * scale, m["y2"] * scale)
            p.drawLine(a, b)
            if kind == "arrow":
                ang = math.atan2(b.y() - a.y(), b.x() - a.x())
                L = (6 + 2.5 * float(m.get("width", 1.5))) * scale
                head = QPolygonF([b, QPointF(b.x() - L * math.cos(ang - 0.45), b.y() - L * math.sin(ang - 0.45)),
                                  QPointF(b.x() - L * math.cos(ang + 0.45), b.y() - L * math.sin(ang + 0.45))])
                p.setBrush(QBrush(col))
                p.drawPolygon(head)
        elif kind == "ink" and m.get("points"):
            path = QPainterPath(QPointF(m["points"][0][0] * scale, m["points"][0][1] * scale))
            for x, y in m["points"][1:]:
                path.lineTo(x * scale, y * scale)
            p.drawPath(path)
        elif kind == "text":
            f = QFont("Arial")
            f.setPixelSize(max(1, round(float(m.get("size", 12)) * scale)))
            p.setFont(f)
            p.setPen(col)
            s = float(m.get("size", 12)) * scale
            for j, line in enumerate(str(m.get("text", "")).split("\n")):
                p.drawText(QPointF(m["x"] * scale, m["y"] * scale + s * (0.95 + 1.25 * j)), line)
        elif kind == "image":
            img = QImage.fromData(base64.b64decode(m.get("png", "")))
            if not img.isNull():
                p.drawImage(QRectF(m["x"] * scale, m["y"] * scale, m["w"] * scale, m["h"] * scale), img)
        if selected == k:
            x, y, w, h = mark_bounds(m)
            sel = QPen(QColor("#1f7ae0"), 1)
            sel.setStyle(Qt.DashLine)
            p.setPen(sel)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF((x - 3) * scale, (y - 3) * scale, (w + 6) * scale, (h + 6) * scale))


def overlay_pdf(w: float, h: float, marks) -> bytes:
    """A one-page PDF (w × h points) with the marks drawn on it — merged onto the real page when saving."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    wr = QPdfWriter(buf)
    wr.setResolution(72)
    wr.setPageLayout(QPageLayout(QPageSize(QSizeF(w, h), QPageSize.Point), QPageLayout.Portrait, QMarginsF(0, 0, 0, 0)))
    p = QPainter(wr)
    p.setRenderHint(QPainter.Antialiasing)
    paint_marks(p, marks, 1.0)
    p.end()
    buf.close()
    return bytes(ba)
