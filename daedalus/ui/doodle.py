"""Doodle page: draw (or import a photo of) a rough sketch and turn it into a model.

• Make sketch — offline clean-up: strokes → lines/arcs/circles, joined, H/V-snapped,
  scaled to the size you set → a constrained Sketch (+ Extrude) you can dimension.
• Ask Claude — sends the drawing (ink + photo) and your notes to the copilot, which
  models it with features and then checks renders of its result against the drawing.

Pen: left-drag draws (tablet pressure supported) · right-click a stroke erases it ·
Ctrl+Z undoes the last stroke · live clean-up preview in copper.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QToolButton, QVBoxLayout, QWidget,
)

from ..core.doodle import DoodleOptions, doodle_to_geometry, summarize
from . import theme

PAPER = QColor(theme.DOODLE_PAPER)
GRID = QColor(theme.DOODLE_GRID)
INK = QColor(theme.DOODLE_INK)
PREVIEW = QColor(theme.ACCENT_LINE)


class DoodleCanvas(QWidget):
    strokes_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(500, 380)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_TabletTracking, True)
        self.strokes: list[list[tuple[float, float, float]]] = []   # (x, y, pressure)
        self._cur = None
        self.photo: QImage | None = None
        self.preview_geom = None
        self._prev_map = None
        self.show_preview = True

    # ----------------------------------------------------------- input
    def _begin(self, p: QPointF, pressure=0.5):
        self._cur = [(p.x(), p.y(), pressure)]

    def _extend(self, p: QPointF, pressure=0.5):
        if self._cur is not None:
            last = self._cur[-1]
            if math.hypot(p.x() - last[0], p.y() - last[1]) >= 1.0:
                self._cur.append((p.x(), p.y(), pressure))
                self.update()

    def _end(self):
        if self._cur and len(self._cur) >= 2:
            self.strokes.append(self._cur)
            self.strokes_changed.emit()
        self._cur = None
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._begin(ev.position())
        elif ev.button() == Qt.RightButton:
            self.erase_at(ev.position())

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            self._extend(ev.position())

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._end()

    def tabletEvent(self, ev):
        t = ev.type()
        pr = max(0.1, ev.pressure())
        if t == ev.Type.TabletPress:
            self._begin(ev.position(), pr)
        elif t == ev.Type.TabletMove and ev.buttons():
            self._extend(ev.position(), pr)
        elif t == ev.Type.TabletRelease:
            self._end()
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier and ev.key() == Qt.Key_Z:
            self.undo()
        else:
            super().keyPressEvent(ev)

    def erase_at(self, p: QPointF):
        best, bd = None, 10.0
        for i, s in enumerate(self.strokes):
            for x, y, _ in s:
                d = math.hypot(x - p.x(), y - p.y())
                if d < bd:
                    best, bd = i, d
        if best is not None:
            self.strokes.pop(best)
            self.strokes_changed.emit()
            self.update()

    def undo(self):
        if self.strokes:
            self.strokes.pop()
            self.strokes_changed.emit()
            self.update()

    def clear(self):
        self.strokes.clear()
        self.photo = None
        self.preview_geom = None
        self.strokes_changed.emit()
        self.update()

    def strokes_yup(self):
        """Strokes as [(x, y)] with y up (what the recogniser expects)."""
        return [[(x, -y) for x, y, _ in s] for s in self.strokes]

    # ---------------------------------------------------------- preview
    def set_preview(self, geom, offset):
        self.preview_geom = geom
        self._prev_map = offset
        self.update()

    # ------------------------------------------------------------ paint
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_paper(p, self.rect())
        if self.show_preview and self.preview_geom:
            self._paint_preview(p)
        self._paint_ink(p)
        if not self.strokes and self._cur is None:
            p.setPen(QColor(theme.DOODLE_HINT))
            f = p.font()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Doodle the part here — outline, holes, slots.\n"
                       "Right-click a stroke to erase it. Import a photo of a napkin sketch if you have one.")
        p.end()

    def _paint_paper(self, p, rect):
        p.fillRect(rect, PAPER)
        p.setPen(QPen(GRID, 1))
        for x in range(0, rect.width(), 24):
            p.drawLine(x, 0, x, rect.height())
        for y in range(0, rect.height(), 24):
            p.drawLine(0, y, rect.width(), y)
        if self.photo is not None and not self.photo.isNull():
            img = self.photo.scaled(rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.setOpacity(0.55)
            p.drawImage(QPointF((rect.width() - img.width()) / 2, (rect.height() - img.height()) / 2), img)
            p.setOpacity(1.0)

    def _paint_ink(self, p):
        for s in self.strokes + ([self._cur] if self._cur else []):
            for (x0, y0, w0), (x1, y1, _w1) in zip(s, s[1:]):
                p.setPen(QPen(INK, 1.2 + 2.6 * w0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.drawLine(QPointF(x0, y0), QPointF(x1, y1))

    def _paint_preview(self, p):
        g = self.preview_geom
        ox, oy = self._prev_map
        P = g["points"]

        def S(x, y):
            return QPointF(x + ox, -(y + oy))

        p.setPen(QPen(PREVIEW, 2.2))
        p.setBrush(Qt.NoBrush)
        for c in g["curves"]:
            if c["type"] == "line":
                p.drawLine(S(*P[c["a"]]), S(*P[c["b"]]))
            elif c["type"] == "circle":
                cen = S(*P[c["c"]])
                p.drawEllipse(cen, c["r"], c["r"])
            else:
                cx, cy = P[c["c"]]
                ax, ay = P[c["a"]]
                bx, by = P[c["b"]]
                r = math.hypot(ax - cx, ay - cy)
                a0 = math.degrees(math.atan2(ay - cy, ax - cx))
                a1 = math.degrees(math.atan2(by - cy, bx - cx))
                span = (a1 - a0) % 360 or 360
                tl = S(cx - r, cy + r)
                rect = QRectF(tl.x(), tl.y(), 2 * r, 2 * r)
                path = QPainterPath()
                path.arcMoveTo(rect, a0)
                path.arcTo(rect, a0, span)
                p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(PREVIEW)
        for pid, (x, y) in P.items():
            if pid != "O" and not pid.startswith("_"):
                q = S(x, y)
                p.drawRect(QRectF(q.x() - 2.5, q.y() - 2.5, 5, 5))

    # ----------------------------------------------------------- export
    def to_png(self, max_side=1200) -> bytes:
        """Paper + photo + ink as PNG (what Claude sees). No preview overlay."""
        img = QImage(self.size(), QImage.Format_RGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_paper(p, self.rect())
        self._paint_ink(p)
        p.end()
        if max(img.width(), img.height()) > max_side:
            img = img.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        buf = QByteArray()
        b = QBuffer(buf)
        b.open(QIODevice.WriteOnly)
        img.save(b, "PNG")
        return bytes(buf)


class DoodlePage(QWidget):
    make_sketch = Signal(dict, str, float)        # geometry, plane, extrude depth (0 = none)
    ask_claude = Signal(str, bytes)               # prompt, png
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("doodlebar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet(f"#doodlebar {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 4, 6, 4)
        t = QLabel("Doodle to model")
        t.setObjectName("h1")
        h.addWidget(t)
        h.addSpacing(10)
        self.canvas = DoodleCanvas()
        for label, fn, tip in (("↶ Undo stroke", self.canvas.undo, "Ctrl+Z"),
                               ("✕ Clear", self.canvas.clear, "Remove all strokes and the photo"),
                               ("Photo…", self._import, "Trace over / send a photo of a hand sketch")):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        self.preview_cb = QCheckBox("Clean-up preview")
        self.preview_cb.setChecked(True)
        self.preview_cb.toggled.connect(self._toggle_preview)
        h.addWidget(self.preview_cb)
        h.addStretch(1)
        h.addWidget(QLabel("Size"))
        self.size = QDoubleSpinBox()
        self.size.setRange(1, 10000)
        self.size.setValue(150)
        self.size.setSuffix(" mm")
        self.size.setToolTip("Longest side of the finished sketch")
        h.addWidget(self.size)
        h.addWidget(QLabel("Depth"))
        self.depth = QDoubleSpinBox()
        self.depth.setRange(0, 10000)
        self.depth.setValue(10)
        self.depth.setSuffix(" mm")
        self.depth.setToolTip("Extrude depth (0 = sketch only)")
        h.addWidget(self.depth)
        self.plane = QComboBox()
        self.plane.addItems(["XY", "XZ", "YZ"])
        h.addWidget(self.plane)
        lay.addWidget(bar)

        row = QWidget()
        row.setObjectName("doodlerow")
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setStyleSheet(f"#doodlerow {{ background:{theme.BG}; }}")
        r = QHBoxLayout(row)
        r.setContentsMargins(6, 4, 6, 4)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Notes for Icarus — e.g. “6061 plate, 12 thick, holes are M8 clearance, 200 long”")
        r.addWidget(self.notes, 1)
        self.recog = QLabel("")
        self.recog.setObjectName("muted")
        r.addWidget(self.recog)
        self.make_btn = QPushButton("✓  Make sketch")
        self.make_btn.setToolTip("Offline clean-up → constrained sketch (+ extrude)")
        self.make_btn.clicked.connect(self._make)
        self.ask_btn = QPushButton("✦  Ask Icarus to model it")
        self.ask_btn.setObjectName("primary")
        self.ask_btn.clicked.connect(self._ask)
        close = QPushButton("Close")
        close.clicked.connect(self.closed.emit)
        r.addWidget(self.make_btn)
        r.addWidget(self.ask_btn)
        r.addWidget(close)
        lay.addWidget(row)
        lay.addWidget(self.canvas, 1)
        self.canvas.strokes_changed.connect(self._refresh)
        self._refresh()

    def _toggle_preview(self, on):
        self.canvas.show_preview = on
        self.canvas.update()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Photo of a sketch", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.canvas.photo = QImage(path)
            self._refresh()

    def _refresh(self):
        has = bool(self.canvas.strokes)
        self.make_btn.setEnabled(has)
        self.ask_btn.setEnabled(has or self.canvas.photo is not None)
        if not has:
            self.canvas.set_preview(None, (0, 0))
            self.recog.setText("")
            return
        pts = [p for s in self.canvas.strokes_yup() for p in s]
        minx = min(p[0] for p in pts)
        miny = min(p[1] for p in pts)
        span = max(max(p[0] for p in pts) - minx, max(p[1] for p in pts) - miny) or 1
        try:
            g = doodle_to_geometry(self.canvas.strokes_yup(),
                                   DoodleOptions(size_mm=span, round_mm=0, centre=False,
                                                 exact_size=False))
            self.canvas.set_preview(g, (minx, miny))
            self.recog.setText(summarize(g))
        except Exception as e:  # noqa: BLE001 — preview must never break drawing
            self.canvas.set_preview(None, (0, 0))
            self.recog.setText(f"(preview failed: {e})")

    def _make(self):
        g = doodle_to_geometry(self.canvas.strokes_yup(), DoodleOptions(size_mm=self.size.value()))
        self.make_sketch.emit(g, self.plane.currentText(), self.depth.value())

    def _ask(self):
        notes = self.notes.text().strip()
        prompt = ("Model the part shown in this hand drawing. Read its outline, holes, slots and any written "
                  f"dimensions. Unless the drawing or notes say otherwise: longest side ≈ {self.size.value():g} mm, "
                  f"thickness {self.depth.value():g} mm, sketched on the {self.plane.currentText()} plane. "
                  "Prefer constrained 'geometry' sketches with dimensions so I can edit them, and round "
                  "to sensible values." + (f"\nNotes: {notes}" if notes else ""))
        self.ask_claude.emit(prompt, self.canvas.to_png())
