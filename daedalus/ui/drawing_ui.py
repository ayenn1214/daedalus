"""Drawing editor: sheets with model views, 2D drafting tools, dimensions, options and 2D file exchange."""
from __future__ import annotations

import copy
import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from ..drawing import dxfio, render
from ..drawing import views as dv
from ..drawing.model import SCALES, SHEETS, VIEW_DIRS, Options, scale_text
from . import theme

def _invert_lightness(img):
    """HSL lightness inversion with hue and saturation kept: c' = c + 255 - (max + min)."""
    import numpy as np
    from PySide6.QtGui import QImage
    img = img.convertToFormat(QImage.Format_RGB32)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    a = np.frombuffer(img.constBits(), np.uint8, count=bpl * h).reshape(h, bpl)[:, :w * 4].reshape(h, w, 4)
    rgb = a[..., :3].astype(np.int16)
    mx, mn = rgb.max(axis=2, keepdims=True), rgb.min(axis=2, keepdims=True)
    out = np.clip(rgb + 255 - (mx + mn), 0, 255).astype(np.uint8)
    buf = np.empty((h, w, 4), np.uint8)
    buf[..., :3] = out
    buf[..., 3] = 255
    res = QImage(buf.data, w, h, w * 4, QImage.Format_RGB32).copy()
    res.setDevicePixelRatio(img.devicePixelRatio())
    return res


TOOLS = [  # key, glyph, label, tip
    ("select", "↖", "Select", "Select / move items and views · Del deletes"),
    ("line", "╱", "Line", "Line: two clicks (keeps chaining; Esc ends)"),
    ("poly", "⌇", "Polyline", "Polyline: clicks, double-click or Enter to finish"),
    ("rect", "▭", "Rectangle", "Rectangle: two corners"),
    ("circle", "◯", "Circle", "Circle: centre, then radius"),
    ("arc", "◠", "Arc", "Arc: centre, start, end (counter-clockwise)"),
    ("text", "T", "Text", "Text / note: click to place"),
    ("dim", "↔", "Dimension", "Smart dimension: two points then placement (horizontal/vertical/aligned by where "
                              "you place it)"),
    ("dia", "⌀", "Diameter", "Diameter: click a circle, then placement"),
    ("rad", "R", "Radius", "Radius: click an arc/circle, then placement"),
    ("angle", "∠", "Angle", "Angle: vertex, first point, second point, placement"),
    ("leader", "↗", "Leader", "Leader note: arrow point, then text position"),
    ("balloon", "①", "Balloon", "Balloon: item point, then position"),
    ("ordinate", "⊢", "Ordinate", "Ordinate dimensions: origin, then each point (Enter / right-click ends); "
                              "X or Y from where you place the first"),
    ("gdt", "⌖", "GD&T", "Feature control frame: point on the feature, then the frame position"),
    ("datum", "Ⓐ", "Datum", "Datum feature symbol: point on the feature, then the box position"),
    ("finish", "√", "Finish", "Surface finish symbol: click on the surface"),
]


class DrawingCanvas(QWidget):
    status = Signal(str)
    changed = Signal()

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.k = 2.5                     # px per mm
        self.ox, self.oy = 40.0, 700.0   # device position of sheet origin
        self.tool = "select"
        self.pts = []
        self.mouse = None                # sheet mm
        self.snap_pt = None
        self.sel = None                  # ("item", index) | ("view", id)
        self._drag = None
        self._pan = None
        self._cache = None
        self._items = None
        self.snap_kind = None
        self.snap_guides = []

    # ------------------------------------------------------------ helpers
    @property
    def doc(self):
        return self.page.doc

    def drawing(self):
        return self.page.current_drawing()

    def sheet(self):
        return self.page.current_sheet()

    def invalidate(self):
        self._cache = None
        self._items = None
        self.update()

    def prims(self):
        if self._cache is None:
            dr, sh = self.drawing(), self.sheet()
            if dr is None:
                self._cache = []
            else:
                try:
                    flat = render.flatten(self.doc, dr, sh)
                except Exception as e:  # noqa: BLE001
                    flat = []
                    self.status.emit(f"Drawing error: {e}")
                self._cache = render.explode(flat, dr.options)
                self._flat = flat
        return self._cache

    def to_sheet(self, p):
        return [(p.x() - self.ox) / self.k, (self.oy - p.y()) / self.k]

    def to_dev(self, q):
        return QPointF(q[0] * self.k + self.ox, self.oy - q[1] * self.k)

    def fit(self):
        dr, sh = self.drawing(), self.sheet()
        if dr is None:
            return
        W, H = sh.dims()
        if W:
            x0, y0, x1, y1 = 0, 0, W, H
        else:
            x0, y0, x1, y1 = render.extents(self.prims() or [{"t": "line", "a": [0, 0], "b": [100, 100]}])
        w, h = max(x1 - x0, 1), max(y1 - y0, 1)
        self.k = min((self.width() - 40) / w, (self.height() - 40) / h)
        self.ox = (self.width() - w * self.k) / 2 - x0 * self.k
        self.oy = (self.height() + h * self.k) / 2 + y0 * self.k
        self.update()

    # ------------------------------------------------------------- snapping
    def _snap(self, q):
        """Snap (endpoint, midpoint, centre, quadrant, intersection, nearest, ⊥/tangent from the last point,
        H/V inference, grid, angle steps). Returns (point, marker point or None)."""
        dr = self.drawing()
        self.snap_kind = None
        self.snap_guides = []
        if dr is None:
            return q, None
        from ..core import snap2d
        if self._items is None:
            self._items = snap2d.prim_items(self.prims())
        opts = dict(snap2d.DEFAULTS)
        opts.update(getattr(self.page, "snap_opts", {}))
        if not dr.options.snap:
            opts = {k: False for k in opts}
        g = dr.options.grid
        opts["grid"] = bool(g and g > 0)
        sn = snap2d.find(*self._items, q[0], q[1], 8 / self.k, opts, self.pts[-1] if self.pts else None, g or 0.0)
        if sn is None:
            return q, None
        self.snap_kind = sn.kind
        self.snap_guides = sn.guides
        return [sn.x, sn.y], (sn.xy if sn.kind not in ("grid",) else None)

    def _circle_at(self, q):
        tol = 8 / self.k
        best, bd = None, tol
        for p in self.prims():
            if p["t"] in ("circle", "arc") and p.get("r", 0) > 0.5:
                d = abs(math.hypot(q[0] - p["c"][0], q[1] - p["c"][1]) - p["r"])
                if d < bd:
                    best, bd = p, d
        return best

    def _view_at(self, q):
        sh = self.sheet()
        for v in reversed(sh.views):
            if "x" not in v:
                continue
            x0, y0, x1, y1 = v.get("_bbox", (0, 0, 0, 0))
            s = v["scale"]
            if v["x"] + x0 * s - 3 <= q[0] <= v["x"] + x1 * s + 3 and v["y"] + y0 * s - 3 <= q[1] <= v["y"] + y1 * s + 3:
                return v
        return None

    def _item_at(self, q):
        sh = self.sheet()
        tol = 6 / self.k
        best, bd = None, tol
        for i, it in enumerate(sh.items):
            d = _distance(it, q)
            if d < bd:
                best, bd = i, d
        return best

    def _unit_for(self, q):
        v = self._view_at(q)
        if v is not None:
            return 1.0 / v["scale"]
        return None

    # --------------------------------------------------------------- paint
    def paintEvent(self, _e):
        from PySide6.QtGui import QImage
        if not theme.DRAW_INVERT or self.drawing() is None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            self._paint(p)
            p.end()
            return
        # Black theme: paint the normal (white paper) sheet off-screen, then invert lightness keeping each
        # colour's hue (white paper → black, black lines → white, blue stays blue). Exports are unaffected.
        img = QImage(self.size() * self.devicePixelRatioF(), QImage.Format_RGB32)
        img.setDevicePixelRatio(self.devicePixelRatioF())
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing)
        self._paint(q)
        q.end()
        img = _invert_lightness(img)
        p = QPainter(self)
        p.drawImage(0, 0, img)
        p.end()

    def _paint(self, p):
        p.fillRect(self.rect(), QColor(theme.DRAW_BG))
        dr, sh = self.drawing(), self.sheet()
        if dr is None:
            p.setPen(QColor(theme.DRAW_TEXT))
            p.drawText(self.rect(), Qt.AlignCenter, "No drawing yet.\nNew drawing makes standard views of the model; "
                                                    "Open 2D opens a DXF / DWG / SVG for editing.")
            return
        W, H = sh.dims()
        if W:
            r = QRectF(self.to_dev([0, H]), self.to_dev([W, 0]))
            if not theme.DRAW_INVERT:
                p.fillRect(r.translated(4, 4), QColor(0, 0, 0, 60))
            p.fillRect(r, Qt.white)
        else:
            p.fillRect(self.rect(), Qt.white)
        prims = self.prims()
        sel_ids = set()
        if self.sel and self.sel[0] == "item":
            idx = self.sel[1]
            items = sh.items
            if 0 <= idx < len(items):
                one = render.explode([render.flatten_item(items[idx], sh)], dr.options)
                for q in one:
                    q["_sel"] = True
                prims = prims + one
        render.paint(p, prims, dr.options, k=self.k, ox=self.ox, oy=self.oy, screen=True, selected=sel_ids,
                     layers=sh.layers)
        # selected view frame
        if self.sel and self.sel[0] == "view":
            v = next((v for v in sh.views if v["id"] == self.sel[1]), None)
            if v is not None:
                x0, y0, x1, y1 = v.get("_bbox", (0, 0, 0, 0))
                s = v["scale"]
                p.setPen(QPen(QColor(theme.DRAW_SEL), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(self.to_dev([v["x"] + x0 * s - 3, v["y"] + y1 * s + 3]),
                                  self.to_dev([v["x"] + x1 * s + 3, v["y"] + y0 * s - 3])))
        # rubber band preview
        if self.mouse is not None and self.tool != "select":
            prev = self._preview()
            if prev:
                for q in prev:
                    q.setdefault("color", theme.DRAW_SEL)
                render.paint(p, render.explode(prev, dr.options), dr.options, k=self.k, ox=self.ox, oy=self.oy)
        if self.snap_guides and self.tool != "select":
            p.setPen(QPen(QColor(theme.DRAW_SNAP), 1, Qt.DashLine))
            for a, b in self.snap_guides:
                p.drawLine(self.to_dev(a), self.to_dev(b))
        if self.snap_pt is not None:
            from ..core.snap2d import GLYPHS
            c = self.to_dev(self.snap_pt)
            p.setPen(QPen(QColor(theme.DRAW_SNAP), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(c.x() - 4, c.y() - 4, 8, 8))
            if self.snap_kind:
                p.drawText(c + QPointF(8, 16), f"{GLYPHS.get(self.snap_kind, '')} {self.snap_kind}")

    def _preview(self):
        m, P = self.mouse, self.pts
        t = self.tool
        if not P:
            return []
        if t == "line":
            return [{"t": "line", "a": P[-1], "b": m, "style": "visible"}]
        if t == "poly":
            return [{"t": "poly", "pts": P + [m], "closed": False, "style": "visible"}]
        if t == "rect":
            a = P[0]
            return [{"t": "poly", "pts": [a, [m[0], a[1]], m, [a[0], m[1]]], "closed": True, "style": "visible"}]
        if t == "circle":
            return [{"t": "circle", "c": P[0], "r": math.hypot(m[0] - P[0][0], m[1] - P[0][1]), "style": "visible"}]
        if t == "arc":
            c = P[0]
            r = math.hypot(P[1][0] - c[0], P[1][1] - c[1]) if len(P) > 1 else math.hypot(m[0] - c[0], m[1] - c[1])
            if len(P) == 1:
                return [{"t": "line", "a": c, "b": m, "style": "construction"}]
            a0 = math.degrees(math.atan2(P[1][1] - c[1], P[1][0] - c[0]))
            a1 = math.degrees(math.atan2(m[1] - c[1], m[0] - c[0]))
            return [{"t": "arc", "c": c, "r": r, "a0": a0, "a1": a1, "style": "visible"}]
        if t == "dim":
            if len(P) == 1:
                return [{"t": "line", "a": P[0], "b": m, "style": "construction"}]
            return [self._make_dim(P[0], P[1], m)]
        if t in ("dia", "rad") and len(P) == 1 and self._picked_circle:
            c = self._picked_circle
            return [{"t": "dim", "kind": t, "c": c["c"], "r": c["r"], "p1": m, "p2": m, "at": m}]
        if t == "angle":
            if len(P) < 3:
                return [{"t": "poly", "pts": P + [m], "closed": False, "style": "construction"}]
            return [{"t": "dim", "kind": "angle", "c": P[0], "p1": P[1], "p2": P[2], "at": m}]
        if t in ("leader", "balloon", "gdt", "datum"):
            return [{"t": "line", "a": P[0], "b": m, "style": "dim"}]
        if t == "ordinate" and P:
            from ..drawing.annot import ordinate_set
            ax = getattr(self, "_ord_axis", None) or ("x" if abs(m[1] - P[0][1]) >= abs(m[0] - P[0][0]) else "y")
            return ordinate_set(P[0], P[1:] + [m], ax, 10.0)
        return []

    def _make_dim(self, a, b, at):
        dx, dy = b[0] - a[0], b[1] - a[1]
        # placement decides: beside the span → vertical, above/below → horizontal, otherwise aligned
        minx, maxx, miny, maxy = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
        if abs(dy) < 1e-6:
            kind = "h"
        elif abs(dx) < 1e-6:
            kind = "v"
        elif minx <= at[0] <= maxx and not (miny <= at[1] <= maxy):
            kind = "h"
        elif miny <= at[1] <= maxy and not (minx <= at[0] <= maxx):
            kind = "v"
        else:
            kind = "aligned"
        d = {"t": "dim", "kind": kind, "p1": list(a), "p2": list(b), "at": list(at)}
        u = self._unit_for(a) or self._unit_for(b)
        if u:
            d["unit"] = u
        return d

    # --------------------------------------------------------------- input
    def set_tool(self, t):
        self.tool = t
        self.pts = []
        self._picked_circle = None
        self.setCursor(Qt.ArrowCursor if t == "select" else Qt.CrossCursor)
        tip = dict((k, tip) for k, _g, _l, tip in TOOLS).get(t, "")
        self.status.emit(tip)
        self.update()

    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        pos = e.position()
        self.ox = pos.x() - (pos.x() - self.ox) * f
        self.oy = pos.y() - (pos.y() - self.oy) * f
        self.k *= f
        self.update()

    def mousePressEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton) and not (e.button() == Qt.RightButton and self.pts):
            self._pan = (e.position(), self.ox, self.oy)
            return
        if e.button() == Qt.RightButton:          # right click ends a chain
            self._finish_chain()
            return
        if self.drawing() is None:
            return
        raw = self.to_sheet(e.position())
        q, _ = self._snap(raw)
        if self.tool == "select":
            it = self._item_at(raw)
            if it is not None:
                self.sel = ("item", it)
                self._drag = ("item", it, raw, copy.deepcopy(self.sheet().items[it]), False)
            else:
                v = self._view_at(raw)
                self.sel = ("view", v["id"]) if v else None
                if v:
                    self._drag = ("view", v, raw, (v["x"], v["y"]), False)
            self.page.selection_changed()
            self.update()
            return
        self._click(q, raw)

    def mouseDoubleClickEvent(self, e):
        if self.tool == "poly":
            self._finish_chain()
        elif self.tool == "select" and self.sel and self.sel[0] == "item":
            self.page.edit_item(self.sel[1])
        elif self.tool == "select" and self.sel and self.sel[0] == "view":
            self.page.edit_view(self.sel[1])

    def mouseMoveEvent(self, e):
        if self._pan:
            p0, ox, oy = self._pan
            self.ox = ox + e.position().x() - p0.x()
            self.oy = oy + e.position().y() - p0.y()
            self.update()
            return
        raw = self.to_sheet(e.position())
        if self._drag:
            kind, obj, start, orig, moved = self._drag
            dx, dy = raw[0] - start[0], raw[1] - start[1]
            if not moved and math.hypot(dx, dy) * self.k < 3:
                return
            if not moved:
                self.doc.checkpoint()
                self._drag = (kind, obj, start, orig, True)
            if kind == "view":
                obj["x"], obj["y"] = orig[0] + dx, orig[1] + dy
                for c in self.sheet().views:         # projected children follow their parent's alignment
                    pass
            else:
                self.sheet().items[obj] = _moved(orig, dx, dy)
            self.invalidate()
            return
        if self.tool != "select":
            q, sp = self._snap(raw)
            self.mouse, self.snap_pt = q, sp
            self.update()
        self.page.show_coords(raw)

    def mouseReleaseEvent(self, e):
        if self._pan:
            self._pan = None
            return
        if self._drag:
            moved = self._drag[4]
            self._drag = None
            if moved:
                self.doc.dirty = True
                self.changed.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.pts:
                self._finish_chain()
            else:
                self.set_tool("select")
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish_chain()
        elif e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.page.delete_selected()
        else:
            super().keyPressEvent(e)

    def _add(self, item):
        sh = self.sheet()
        item.setdefault("layer", self.page.current_layer())
        self.doc.add_drawing_item(item, self.drawing().name, sh.name)
        self.invalidate()

    def _finish_chain(self):
        if self.tool == "ordinate" and len(self.pts) > 1:
            ax = getattr(self, "_ord_axis", None) or "x"
            try:
                self.doc.add_ordinate_dims(self.pts[0], self.pts[1:], ax, self.drawing().name, self.sheet().name)
            except Exception as e:  # noqa: BLE001
                self.status.emit(str(e))
            self._ord_axis = None
            self.pts = []
            self.invalidate()
            self.update()
            return
        if self.tool == "poly" and len(self.pts) > 1:
            closed = len(self.pts) > 2 and math.hypot(self.pts[0][0] - self.pts[-1][0],
                                                      self.pts[0][1] - self.pts[-1][1]) < 1e-6
            pts = self.pts[:-1] if closed else self.pts
            self._add({"t": "poly", "pts": pts, "closed": closed, "style": self.page.current_style()})
        self.pts = []
        self._picked_circle = None
        self.update()

    def _click(self, q, raw):
        t = self.tool
        P = self.pts
        st = self.page.current_style()
        if t == "line":
            if P:
                self._add({"t": "line", "a": P[-1], "b": q, "style": st})
                self.pts = [q]
            else:
                self.pts = [q]
        elif t == "poly":
            P.append(q)
        elif t == "rect":
            if P:
                a = P[0]
                self._add({"t": "poly", "pts": [a, [q[0], a[1]], q, [a[0], q[1]]], "closed": True, "style": st})
                self.pts = []
            else:
                self.pts = [q]
        elif t == "circle":
            if P:
                r = math.hypot(q[0] - P[0][0], q[1] - P[0][1])
                if r > 1e-6:
                    self._add({"t": "circle", "c": P[0], "r": r, "style": st})
                self.pts = []
            else:
                self.pts = [q]
        elif t == "arc":
            P.append(q)
            if len(P) == 3:
                c = P[0]
                r = math.hypot(P[1][0] - c[0], P[1][1] - c[1])
                a0 = math.degrees(math.atan2(P[1][1] - c[1], P[1][0] - c[0]))
                a1 = math.degrees(math.atan2(P[2][1] - c[1], P[2][0] - c[0]))
                self._add({"t": "arc", "c": c, "r": r, "a0": a0, "a1": a1, "style": st})
                self.pts = []
        elif t == "text":
            txt, ok = QInputDialog.getMultiLineText(self, "Text", "Text:")
            if ok and txt.strip():
                h = self.drawing().options.text_height
                self._add({"t": "text", "p": q, "text": txt, "h": h, "style": "text"})
        elif t == "dim":
            P.append(q)
            if len(P) == 3:
                self._add(self._make_dim(P[0], P[1], P[2]))
                self.pts = []
        elif t in ("dia", "rad"):
            if not P:
                c = self._circle_at(raw)
                if c is None:
                    self.status.emit("Click on a circle or arc")
                    return
                self._picked_circle = c
                self.pts = [q]
            else:
                c = self._picked_circle
                d = {"t": "dim", "kind": t, "c": list(c["c"]), "r": c["r"], "p1": q, "p2": q, "at": q}
                u = self._unit_for(c["c"])
                if u:
                    d["unit"] = u
                self._add(d)
                self.pts = []
                self._picked_circle = None
        elif t == "angle":
            P.append(q)
            if len(P) == 4:
                self._add({"t": "dim", "kind": "angle", "c": P[0], "p1": P[1], "p2": P[2], "at": P[3]})
                self.pts = []
        elif t == "leader":
            P.append(q)
            if len(P) == 2:
                txt, ok = QInputDialog.getMultiLineText(self, "Leader note", "Text:")
                if ok and txt.strip():
                    self._add({"t": "dim", "kind": "leader", "p1": P[0], "p2": P[0], "at": P[1], "text": txt})
                self.pts = []
        elif t == "ordinate":
            if len(P) == 1 and getattr(self, "_ord_axis", None) is None:
                ax, ok = QInputDialog.getItem(self, "Ordinate", "Measure along:", ["x (horizontal)", "y (vertical)"], 0, False)
                self._ord_axis = "x" if not ok or ax.startswith("x") else "y"
            P.append(q)
        elif t in ("gdt", "datum"):
            P.append(q)
            if len(P) == 2:
                d = GdtDialog(t, self)
                if d.exec():
                    it = d.item(P[0], P[1])
                    self._add(it)
                self.pts = []
        elif t == "finish":
            d = GdtDialog("finish", self)
            if d.exec():
                self._add(d.item(q, q))
        elif t == "balloon":
            P.append(q)
            if len(P) == 2:
                n = len([i for i in self.sheet().items if i["t"] == "balloon"]) + 1
                txt, ok = QInputDialog.getText(self, "Balloon", "Item number:", text=str(n))
                if ok and txt.strip():
                    self._add({"t": "balloon", "p": P[0], "at": P[1], "text": txt.strip()})
                self.pts = []
        self.update()


# ------------------------------------------------------------ geometry utils

def _key_points(p):
    t = p["t"]
    if t == "line":
        return [p["a"], p["b"], [(p["a"][0] + p["b"][0]) / 2, (p["a"][1] + p["b"][1]) / 2]]
    if t == "poly" and not p.get("fill"):
        return p["pts"]
    if t == "circle":
        c, r = p["c"], p["r"]
        return [c, [c[0] + r, c[1]], [c[0] - r, c[1]], [c[0], c[1] + r], [c[0], c[1] - r]]
    if t == "arc":
        c, r = p["c"], p["r"]
        return [c] + [[c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a))]
                      for a in (p["a0"], p["a1"])]
    return []


def _seg_dist(q, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L2 = ax * ax + ay * ay
    t = 0 if L2 < 1e-12 else max(0, min(1, ((q[0] - a[0]) * ax + (q[1] - a[1]) * ay) / L2))
    return math.hypot(a[0] + t * ax - q[0], a[1] + t * ay - q[1])


def _distance(it, q):
    t = it["t"]
    if t == "line":
        return _seg_dist(q, it["a"], it["b"])
    if t == "poly":
        pts = it["pts"] + ([it["pts"][0]] if it.get("closed") else [])
        return min((_seg_dist(q, pts[i], pts[i + 1]) for i in range(len(pts) - 1)), default=1e9)
    if t in ("circle", "arc"):
        d = abs(math.hypot(q[0] - it["c"][0], q[1] - it["c"][1]) - it["r"])
        if t == "arc":
            a = math.degrees(math.atan2(q[1] - it["c"][1], q[0] - it["c"][0]))
            if (a - it["a0"]) % 360 > (it["a1"] - it["a0"]) % 360:
                return 1e9
        return d
    if t == "text":
        h = it.get("h", 3.5)
        lines = str(it.get("text", "")).split("\n")
        w = max(len(s) for s in lines) * h * 0.62
        x0 = it["p"][0] - (w / 2 if it.get("align") == "center" else (w if it.get("align") == "right" else 0))
        y1 = it["p"][1] + h * 1.2
        y0 = it["p"][1] - h * 1.4 * (len(lines) - 1) - 0.5
        if x0 <= q[0] <= x0 + w and y0 <= q[1] <= y1:
            return 0.0
        return 1e9
    if t in ("gdt",):
        from ..drawing.annot import gdt_bbox
        x0, y0, x1, y1 = gdt_bbox(it)
        if x0 <= q[0] <= x1 and y0 <= q[1] <= y1:
            return 0.0
    if t in ("dim", "balloon", "gdt", "datum", "finish"):
        pts = [it.get(k) for k in ("p1", "p2", "at", "p", "leader") if it.get(k)]
        return min(math.hypot(q[0] - p[0], q[1] - p[1]) for p in pts) * 0.5 if pts else 1e9
    if t == "hatch":
        return min((_seg_dist(q, lp[i], lp[(i + 1) % len(lp)]) for lp in it["loops"] for i in range(len(lp))),
                   default=1e9)
    return 1e9


def _moved(it, dx, dy):
    q = copy.deepcopy(it)

    def mv(p):
        return [p[0] + dx, p[1] + dy]

    for k in ("a", "b", "c", "p", "p1", "p2", "at"):
        if k in q and isinstance(q[k], list):
            q[k] = mv(q[k])
    if "pts" in q:
        q["pts"] = [mv(p) for p in q["pts"]]
    if "loops" in q:
        q["loops"] = [[mv(p) for p in lp] for lp in q["loops"]]
    return q


def _flatten_item(it, sh):
    q = dict(it)
    q.setdefault("layer", "0")
    if q["t"] == "dim" and "_unit" not in q:
        from ..drawing.model import scale_factor
        q["_unit"] = q.get("unit") or (1.0 / scale_factor(sh.scale) if sh.dims()[0] else 1.0)
    return q


render.flatten_item = _flatten_item


# --------------------------------------------------------------- dialogs

class GdtDialog(QDialog):
    """GD&T feature control frame / datum / surface finish settings."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        from ..drawing.annot import FINISH_METHODS, GDT, MODIFIERS
        self.kind = kind
        self.setWindowTitle({"gdt": "Feature control frame", "datum": "Datum feature", "finish": "Surface finish"}[kind])
        f = QFormLayout(self)
        self.w = {}
        if kind == "gdt":
            sym = QComboBox()
            for k, (g, _d) in GDT.items():
                sym.addItem(f"{g}  {k.replace('_', ' ')}", k)
            sym.setCurrentIndex(list(GDT).index("position"))
            f.addRow("Characteristic", sym)
            tol = QLineEdit("0.1")
            f.addRow("Tolerance", tol)
            from PySide6.QtWidgets import QCheckBox
            dia = QCheckBox("Ø diameter zone")
            dia.setChecked(True)
            f.addRow("", dia)
            mod = QComboBox()
            for k, g in MODIFIERS.items():
                mod.addItem(f"{g} {k}" if k else "none", k)
            f.addRow("Material condition", mod)
            dat = QLineEdit("A, B, C")
            dat.setToolTip("Datum references, in order; add |M for a modifier (B|M)")
            f.addRow("Datums", dat)
            self.w = {"sym": sym, "tol": tol, "dia": dia, "mod": mod, "dat": dat}
        elif kind == "datum":
            name = QLineEdit("A")
            f.addRow("Datum letter", name)
            self.w = {"name": name}
        else:
            ra = QLineEdit("1.6")
            f.addRow("Ra (µm)", ra)
            meth = QComboBox()
            for k in FINISH_METHODS:
                meth.addItem(k.replace("_", " "), k)
            meth.setCurrentIndex(1)
            f.addRow("Process", meth)
            note = QLineEdit()
            f.addRow("Note", note)
            self.w = {"ra": ra, "meth": meth, "note": note}
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def item(self, a, b):
        w = self.w
        if self.kind == "gdt":
            dats = [x.strip() for x in w["dat"].text().split(",") if x.strip()]
            return {"t": "gdt", "p": list(b), "leader": list(a), "symbol": w["sym"].currentData(),
                    "tol": w["tol"].text().strip(), "dia": w["dia"].isChecked(), "mod": w["mod"].currentData(),
                    "datums": dats}
        if self.kind == "datum":
            return {"t": "datum", "p": list(a), "at": list(b), "name": w["name"].text().strip() or "A"}
        return {"t": "finish", "p": list(a), "ra": w["ra"].text().strip(), "method": w["meth"].currentData(),
                "note": w["note"].text().strip()}


class OptionsDialog(QDialog):
    """Drawing options: standard, units, text, dims, display, sheet, export."""

    def __init__(self, drawing, sheet, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Drawing options · {drawing.name}")
        o = drawing.options
        tabs = QTabWidget()
        # sheet
        w1 = QWidget()
        f1 = QFormLayout(w1)
        self.size = QComboBox()
        self.size.addItems(list(SHEETS))
        self.size.setCurrentText(sheet.size)
        self.landscape = QCheckBox("Landscape")
        self.landscape.setChecked(sheet.landscape)
        self.scale = QComboBox()
        self.scale.setEditable(True)
        self.scale.addItems([scale_text(s) for s in SCALES])
        self.scale.setCurrentText(scale_text(sheet.scale))
        self.border = QCheckBox("Border and title block")
        self.border.setChecked(sheet.border)
        self.standard = QComboBox()
        self.standard.addItems(["ISO", "ANSI"])
        self.standard.setCurrentText(o.standard)
        self.standard.setToolTip("ISO = first-angle projection, ANSI = third-angle")
        f1.addRow("Sheet size", self.size)
        f1.addRow("", self.landscape)
        f1.addRow("Scale", self.scale)
        f1.addRow("", self.border)
        f1.addRow("Standard", self.standard)
        tabs.addTab(w1, "Sheet")
        # dimensions & text
        w2 = QWidget()
        f2 = QFormLayout(w2)
        self.units = QComboBox()
        self.units.addItems(["mm", "in"])
        self.units.setCurrentText(o.units)
        self.decimals = QSpinBox()
        self.decimals.setRange(0, 4)
        self.decimals.setValue(o.decimals)

        def dsb(v, lo=0.5, hi=20.0, step=0.5):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(2)
            s.setValue(v)
            s.setSuffix(" mm")
            return s

        self.text_h, self.dim_h, self.arrow = dsb(o.text_height), dsb(o.dim_text_height), dsb(o.arrow)
        self.hatch = dsb(o.hatch_spacing, 0.5, 20)
        self.font = QLineEdit(o.font)
        for lab, wd in (("Dimension units", self.units), ("Decimals", self.decimals), ("Text height", self.text_h),
                        ("Dimension text height", self.dim_h), ("Arrow size", self.arrow),
                        ("Hatch spacing", self.hatch), ("Font", self.font)):
            f2.addRow(lab, wd)
        tabs.addTab(w2, "Dimensions & text")
        # display
        w3 = QWidget()
        v3 = QVBoxLayout(w3)
        self.checks = {}
        for key, lab in (("hidden_lines", "Hidden lines in orthographic views"), ("center_marks", "Centre marks"),
                         ("auto_dims", "Automatic overall dimensions"), ("hole_callouts", "Hole / thread callouts"),
                         ("tangent_edges", "Tangent edges"), ("bom", "Bill of materials table"),
                         ("balloons", "Item balloons"), ("snap", "Snap to geometry")):
            c = QCheckBox(lab)
            c.setChecked(bool(getattr(o, key)))
            self.checks[key] = c
            v3.addWidget(c)
        g = QHBoxLayout()
        g.addWidget(QLabel("Snap grid"))
        self.grid = dsb(o.grid, 0, 50, 1)
        g.addWidget(self.grid)
        v3.addLayout(g)
        v3.addStretch(1)
        tabs.addTab(w3, "Display")
        # export
        w4 = QWidget()
        f4 = QFormLayout(w4)
        self.dxfv = QComboBox()
        self.dxfv.addItems(list(dxfio.DXF_VERSIONS))
        self.dxfv.setCurrentText(o.dxf_version)
        f4.addRow("DXF / DWG version", self.dxfv)
        dwg = QLabel("DWG: " + ("ODA File Converter found ✓" if dxfio.dwg_available() else
                                "install the free ODA File Converter to read/write DWG"))
        dwg.setWordWrap(True)
        dwg.setObjectName("muted")
        f4.addRow(dwg)
        note = QLabel("DWF is a closed Autodesk format — use PDF for viewing exchange, DXF/DWG for editing.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        f4.addRow(note)
        tabs.addTab(w4, "Export")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)

    def values(self):
        opts = {"standard": self.standard.currentText(), "units": self.units.currentText(),
                "decimals": self.decimals.value(), "text_height": self.text_h.value(),
                "dim_text_height": self.dim_h.value(), "arrow": self.arrow.value(),
                "hatch_spacing": self.hatch.value(), "font": self.font.text() or "Arial",
                "dxf_version": self.dxfv.currentText(), "grid": self.grid.value(),
                **{k: c.isChecked() for k, c in self.checks.items()}}
        sc = self.scale.currentText().replace("/", ":")
        try:
            a, b = (float(x) for x in sc.split(":"))
        except ValueError:
            a, b = 1, 1
        return opts, self.size.currentText(), self.landscape.isChecked(), [a, b], self.border.isChecked()


class TitleDialog(QDialog):
    def __init__(self, sheet, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Title block")
        f = QFormLayout(self)
        self.edits = {}
        for k in render.TITLE_FIELDS:
            e = QLineEdit(str(sheet.title.get(k, "")))
            self.edits[k] = e
            f.addRow(k.capitalize(), e)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return {k: e.text() for k, e in self.edits.items() if e.text()}


class ViewDialog(QDialog):
    def __init__(self, v, parent=None):
        super().__init__(parent)
        self.setWindowTitle("View properties")
        f = QFormLayout(self)
        self.label = QLineEdit(v.get("label", ""))
        self.scale = QComboBox()
        self.scale.setEditable(True)
        self.scale.addItems([scale_text(s) for s in SCALES])
        s = v.get("scale", 1.0)
        self.scale.setCurrentText(f"{s:g}:1" if s >= 1 else f"1:{1 / s:g}")
        self.hidden = QCheckBox("Hidden lines")
        self.hidden.setChecked(bool(v.get("hidden", True)))
        self.annot = QCheckBox("Automatic annotations")
        self.annot.setChecked(bool(v.get("annotate", True)))
        self.expl = QCheckBox("Exploded")
        self.expl.setChecked(bool(v.get("exploded")))
        self.bal = QCheckBox("Balloons + BOM")
        self.bal.setChecked(bool(v.get("balloons")))
        f.addRow("Label", self.label)
        f.addRow("Scale", self.scale)
        for w in (self.hidden, self.annot, self.expl, self.bal):
            f.addRow(w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def apply(self, v):
        v["label"] = self.label.text()
        try:
            a, b = (float(x) for x in self.scale.currentText().replace("/", ":").split(":"))
            v["scale"] = a / b
        except ValueError:
            pass
        v["hidden"] = self.hidden.isChecked()
        v["annotate"] = self.annot.isChecked()
        v["exploded"] = self.expl.isChecked()
        v["balloons"] = v["bom"] = self.bal.isChecked()


# ----------------------------------------------------------------- page

class DrawingPage(QWidget):
    closed = Signal()
    status = Signal(str)

    def __init__(self, doc_getter, parent=None):
        super().__init__(parent)
        self._doc = doc_getter
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        h = QHBoxLayout(top)
        h.setContentsMargins(6, 3, 6, 3)
        back = QPushButton("◀ 3D model")
        back.clicked.connect(self.closed.emit)
        h.addWidget(back)
        self.dr_box = QComboBox()
        self.dr_box.setMinimumWidth(140)
        self.dr_box.activated.connect(lambda _i: self.refresh(fit=True))
        h.addWidget(self.dr_box)
        self.sh_box = QComboBox()
        self.sh_box.activated.connect(lambda _i: self.refresh(fit=True))
        h.addWidget(self.sh_box)

        def btn(text, tip, fn, menu=None):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            if menu is not None:
                b.setMenu(menu)
                b.setPopupMode(QToolButton.InstantPopup)
            else:
                b.clicked.connect(fn)
            h.addWidget(b)
            return b

        btn("New ▾", "New drawing / sheet", None, self._new_menu())
        btn("Open 2D…", "Open DXF / DWG / SVG / HPGL for editing", self.open_2d)
        btn("View ▾", "Add a model view", None, self._view_menu())
        btn("Options…", "Drawing options: sheet, standard, units, text, display, export", self.options)
        btn("Title…", "Title block fields", self.title)
        btn("Export ▾", "Save the drawing as PDF / DXF / DWG / SVG / PNG", None, self._export_menu())
        btn("⟳", "Regenerate views from the model", lambda: self.refresh(regen=True))
        h.addSpacing(10)
        h.addWidget(QLabel("Layer"))
        self.layer_box = QComboBox()
        self.layer_box.setEditable(True)
        self.layer_box.setMinimumWidth(90)
        h.addWidget(self.layer_box)
        self.style_box = QComboBox()
        self.style_box.addItems(["visible", "thin", "hidden", "center", "construction"])
        self.style_box.setToolTip("Line style for new geometry")
        h.addWidget(self.style_box)
        h.addStretch(1)
        self.coords = QLabel()
        self.coords.setObjectName("muted")
        h.addWidget(self.coords)
        lay.addWidget(top)
        tools = QWidget()
        th = QHBoxLayout(tools)
        th.setContentsMargins(6, 0, 6, 3)
        th.setSpacing(2)
        self.tool_btns = {}
        for key, glyph, label, tip in TOOLS:
            b = QToolButton()
            b.setText(f"{glyph} {label}")
            b.setToolTip(tip)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.set_tool(k))
            th.addWidget(b)
            self.tool_btns[key] = b
        th.addStretch(1)
        lay.addWidget(tools)
        self.canvas = DrawingCanvas(self)
        self.canvas.status.connect(self.status.emit)
        self.canvas.changed.connect(lambda: self.refresh())
        lay.addWidget(self.canvas, 1)
        self.set_tool("select")

    @property
    def doc(self):
        return self._doc()

    # --------------------------------------------------------------- state
    def current_drawing(self):
        n = self.dr_box.currentText()
        for d in self.doc.drawings:
            if d.name == n:
                return d
        return self.doc.drawings[0] if self.doc.drawings else None

    def current_sheet(self):
        dr = self.current_drawing()
        if dr is None:
            return None
        n = self.sh_box.currentText()
        return next((s for s in dr.sheets if s.name == n), dr.sheet())

    def current_layer(self):
        return self.layer_box.currentText().strip() or "0"

    def current_style(self):
        return self.style_box.currentText()

    def set_tool(self, k):
        for key, b in self.tool_btns.items():
            b.setChecked(key == k)
        self.canvas.set_tool(k)
        self.canvas.setFocus()

    def show_coords(self, q):
        self.coords.setText(f"x {q[0]:.2f}   y {q[1]:.2f} mm")

    def selection_changed(self):
        pass

    def refresh(self, regen=False, fit=False, select=None):
        drs = [d.name for d in self.doc.drawings]
        cur = select or self.dr_box.currentText()
        self.dr_box.blockSignals(True)
        self.dr_box.clear()
        self.dr_box.addItems(drs)
        if cur in drs:
            self.dr_box.setCurrentText(cur)
        self.dr_box.blockSignals(False)
        dr = self.current_drawing()
        self.sh_box.blockSignals(True)
        cs = self.sh_box.currentText()
        self.sh_box.clear()
        if dr:
            self.sh_box.addItems([s.name for s in dr.sheets])
            if cs in [s.name for s in dr.sheets]:
                self.sh_box.setCurrentText(cs)
        self.sh_box.blockSignals(False)
        if dr is not None:
            needs = any("_prims" not in v for s in dr.sheets for v in s.views)
            if dr.linked and (regen or needs):
                dv.regenerate(self.doc, dr)
            sh = self.current_sheet()
            lay = self.layer_box.currentText()
            self.layer_box.blockSignals(True)
            self.layer_box.clear()
            self.layer_box.addItems(list(sh.layers))
            self.layer_box.setCurrentText(lay if lay else "0")
            self.layer_box.blockSignals(False)
        self.canvas.invalidate()
        if fit:
            self.canvas.fit()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh(regen=True, fit=True)

    # ------------------------------------------------------------- menus
    def _new_menu(self):
        m = QMenu(self)
        m.addAction("Drawing of the model…", self.new_drawing)
        m.addAction("Flat pattern drawing…", self.new_flat)
        m.addAction("Blank 2D drawing", self.new_blank)
        m.addSeparator()
        m.addAction("Add sheet", self.add_sheet)
        m.addAction("Delete drawing", self.delete_drawing)
        return m

    def _view_menu(self):
        m = QMenu(self)
        for name in VIEW_DIRS:
            m.addAction(name.capitalize(), lambda n=name: self.add_view(n))
        m.addSeparator()
        m.addAction("Section view (draw cut line)…", self.add_section)
        m.addAction("Detail view (circle)…", self.add_detail)
        m.addAction("Exploded isometric with balloons", lambda: self.add_view("iso", exploded=True))
        m.addAction("Auxiliary view (click along an inclined edge)…", self.add_auxiliary)
        m.addAction("Break the selected view (two clicks)…", self.add_break)
        m.addAction("Remove breaks from the selected view", self.clear_breaks)
        m.addSeparator()
        m.addAction("Add revision…", self.add_revision)
        m.addSeparator()
        m.addAction("View properties…", lambda: self.edit_view(self.canvas.sel[1]) if self.canvas.sel and
                    self.canvas.sel[0] == "view" else self.status.emit("Select a view first"))
        return m

    def _export_menu(self):
        m = QMenu(self)
        for label, ext in (("PDF (all sheets, vector)", ".pdf"), ("DXF (AutoCAD exchange)", ".dxf"),
                           ("DWG (needs ODA File Converter)", ".dwg"), ("SVG", ".svg"), ("PNG image", ".png")):
            m.addAction(label, lambda e=ext: self.export(e))
        return m

    # ------------------------------------------------------------ actions
    def _err(self, title, e):
        QMessageBox.warning(self, title, str(e))

    def new_drawing(self):
        d = QDialog(self)
        d.setWindowTitle("New drawing")
        f = QFormLayout(d)
        size = QComboBox()
        size.addItems([s for s in SHEETS if s != "Model space"])
        size.setCurrentText("A3")
        std = QComboBox()
        std.addItems(["ISO", "ANSI"])
        units = QComboBox()
        units.addItems(["mm", "in"])
        checks = {}
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        for n, on in (("front", True), ("top", True), ("right", True), ("iso", True), ("left", False),
                      ("back", False), ("bottom", False)):
            c = QCheckBox(n)
            c.setChecked(on)
            checks[n] = c
            rl.addWidget(c)
        objs = QComboBox()
        objs.addItem("All objects")
        objs.addItems([o.name for o in self.doc.objects])
        f.addRow("Sheet", size)
        f.addRow("Standard", std)
        f.addRow("Units", units)
        f.addRow("Views", row)
        f.addRow("Objects", objs)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec() != QDialog.Accepted:
            return
        which = [n for n in ("front", "top", "right", "left", "back", "bottom", "iso") if checks[n].isChecked()]
        if not which:
            return
        try:
            dr = self.doc.new_drawing(size=size.currentText(), views=which, standard=std.currentText(),
                                      units=units.currentText(),
                                      objects=None if objs.currentIndex() == 0 else [objs.currentText()])
        except FeatureError as e:
            return self._err("New drawing", e)
        self.refresh(fit=True, select=dr.name)

    def new_flat(self):
        flats = list(self.doc.result.flats)
        if not flats:
            return self._err("Flat pattern", "No sheet-metal feature yet.")
        name, ok = QInputDialog.getItem(self, "Flat pattern", "Sheet-metal feature:", flats, 0, False)
        if ok:
            try:
                dr = self.doc.new_drawing(name=f"{name} flat", flat=name, views=())
            except FeatureError as e:
                return self._err("Flat pattern", e)
            self.refresh(fit=True, select=dr.name)

    def new_blank(self):
        from ..drawing.model import Drawing, Sheet
        size, ok = QInputDialog.getItem(self, "Blank drawing", "Sheet:", list(SHEETS), 1, False)
        if not ok:
            return
        self.doc.checkpoint()
        n = f"Drawing{len(self.doc.drawings) + 1}"
        dr = Drawing(name=n, sheets=[Sheet(size=size, border=size != "Model space")], linked=False)
        self.doc.drawings.append(dr)
        self.doc.dirty = True
        self.refresh(fit=True, select=n)

    def add_sheet(self):
        dr = self.current_drawing()
        if dr is None:
            return
        from ..drawing.model import Sheet
        self.doc.checkpoint()
        s0 = self.current_sheet()
        sh = Sheet(name=f"Sheet{len(dr.sheets) + 1}", size=s0.size, landscape=s0.landscape, title=dict(s0.title))
        dr.sheets.append(sh)
        self.refresh()
        self.sh_box.setCurrentText(sh.name)
        self.refresh(fit=True)

    def delete_drawing(self):
        dr = self.current_drawing()
        if dr and QMessageBox.question(self, "Delete", f"Delete {dr.name}?") == QMessageBox.Yes:
            self.doc.delete_drawing(dr.name)
            self.refresh(fit=True)

    def open_2d(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open 2D", "",
                                                  "2D drawings (*.dxf *.dwg *.svg *.plt *.hpgl);;All files (*)")
        if not path:
            return
        try:
            dr = self.doc.open_2d(path)
        except FeatureError as e:
            return self._err("Open 2D", e)
        self.refresh(fit=True, select=dr.name)
        n = len(dr.sheets[0].items)
        self.status.emit(f"Opened {Path(path).name}: {n} entities on {len(dr.sheets[0].layers)} layers")

    def add_view(self, name, exploded=False):
        dr = self.current_drawing()
        if dr is None:
            return self.new_drawing()
        try:
            v = self.doc.add_drawing_view(dr.name, kind="iso" if name in ("iso", "dimetric", "trimetric")
                                          else "projected", dir=name, sheet=self.current_sheet().name,
                                          exploded=exploded)
            if exploded:
                if not self.doc.explode:
                    self.doc.auto_explode(checkpoint=False)
                v["balloons"] = v["bom"] = True
        except FeatureError as e:
            return self._err("View", e)
        self.refresh(regen=True)

    def _parent_view(self):
        sh = self.current_sheet()
        if self.canvas.sel and self.canvas.sel[0] == "view":
            v = next((v for v in sh.views if v["id"] == self.canvas.sel[1]), None)
            if v and v.get("kind") in ("base", "projected", "section"):
                return v
        return next((v for v in sh.views if v.get("kind") in ("base", "projected")), None)

    def add_section(self):
        par = self._parent_view()
        if par is None:
            return self._err("Section", "Add an orthographic view first.")
        self.status.emit(f"Click two points across the {par.get('dir', '')} view for the cut line")
        self._pending = ("section", par)
        self.canvas.set_tool("_pick2")
        self.canvas.tool = "_pick2"
        self.canvas.pts = []
        self._hook_pick()

    def add_detail(self):
        par = self._parent_view()
        if par is None:
            return self._err("Detail", "Add an orthographic view first.")
        self.status.emit("Click the detail centre, then a point on its boundary")
        self._pending = ("detail", par)
        self.canvas.tool = "_pick2"
        self.canvas.pts = []
        self._hook_pick()

    def add_auxiliary(self):
        par = self._parent_view()
        if par is None:
            return self._err("Auxiliary", "Add an orthographic view first.")
        self.status.emit("Click two points along the inclined edge — the view looks square onto it")
        self._pending = ("auxiliary", par)
        self.canvas.tool = "_pick2"
        self.canvas.pts = []
        self._hook_pick()

    def add_break(self):
        par = self._parent_view()
        if par is None:
            return self._err("Break", "Select a view first.")
        self.status.emit("Click where the break starts and where it ends (horizontal or vertical apart)")
        self._pending = ("break", par)
        self.canvas.tool = "_pick2"
        self.canvas.pts = []
        self._hook_pick()

    def clear_breaks(self):
        par = self._parent_view()
        if par is not None:
            self.doc.set_drawing_view(par["id"], self.current_drawing().name, self.current_sheet().name, breaks=[])
            self.refresh(regen=True)

    def add_revision(self):
        dr = self.current_drawing()
        if dr is None:
            return
        txt, ok = QInputDialog.getText(self, "Revision", "Description of the change:")
        if ok and txt.strip():
            who, _ok = QInputDialog.getText(self, "Revision", "Approved by (optional):")
            r = self.doc.add_revision(txt.strip(), dr.name, self.current_sheet().name, approved=who or "")
            self.status.emit(f"Revision {r['rev']} added")
            self.refresh()

    def _hook_pick(self):
        canvas = self.canvas
        orig = canvas._click

        def click(q, raw):
            canvas.pts.append(q)
            if len(canvas.pts) < 2:
                return
            a, b = canvas.pts
            canvas._click = orig
            canvas.pts = []
            kind, par = self._pending
            s = par["scale"]
            la = [(a[0] - par["x"]) / s, (a[1] - par["y"]) / s]
            lb = [(b[0] - par["x"]) / s, (b[1] - par["y"]) / s]
            try:
                if kind == "section":
                    self.doc.add_drawing_view(self.current_drawing().name, kind="section", parent=par["id"],
                                              line=[la, lb], sheet=self.current_sheet().name)
                elif kind == "auxiliary":
                    self.doc.add_drawing_view(self.current_drawing().name, kind="auxiliary", parent=par["id"],
                                              line=[la, lb], sheet=self.current_sheet().name)
                elif kind == "break":
                    ax = "x" if abs(lb[0] - la[0]) >= abs(lb[1] - la[1]) else "y"
                    i = 0 if ax == "x" else 1
                    br = list(par.get("breaks") or []) + [{"axis": ax, "a": min(la[i], lb[i]), "b": max(la[i], lb[i])}]
                    self.doc.set_drawing_view(par["id"], self.current_drawing().name, self.current_sheet().name,
                                              breaks=br)
                else:
                    self.doc.add_drawing_view(self.current_drawing().name, kind="detail", parent=par["id"],
                                              center=la, radius=math.hypot(lb[0] - la[0], lb[1] - la[1]),
                                              sheet=self.current_sheet().name)
            except FeatureError as e:
                self._err("View", e)
            self.set_tool("select")
            self.refresh(regen=True)

        canvas._click = click

    def edit_view(self, vid):
        sh = self.current_sheet()
        v = next((v for v in sh.views if v["id"] == vid), None)
        if v is None:
            return
        d = ViewDialog(v, self)
        if d.exec() == QDialog.Accepted:
            self.doc.checkpoint()
            d.apply(v)
            if v.get("exploded") and not self.doc.explode:
                self.doc.auto_explode(checkpoint=False)
            self.doc.dirty = True
            self.refresh(regen=True)

    def edit_item(self, idx):
        sh = self.current_sheet()
        it = sh.items[idx]
        if it["t"] in ("text", "balloon") or (it["t"] == "dim"):
            cur = it.get("text", "")
            lab = "Text:" if it["t"] != "dim" else "Dimension text (empty = measured, <> = value):"
            txt, ok = QInputDialog.getMultiLineText(self, "Edit", lab, cur)
            if ok:
                self.doc.checkpoint()
                it["text"] = txt
                self.refresh()

    def delete_selected(self):
        sel = self.canvas.sel
        sh = self.current_sheet()
        if not sel or sh is None:
            return
        self.doc.checkpoint()
        if sel[0] == "item" and 0 <= sel[1] < len(sh.items):
            del sh.items[sel[1]]
        elif sel[0] == "view":
            gone = {sel[1]}
            sh.views[:] = [v for v in sh.views if v["id"] not in gone and v.get("parent") not in gone]
        self.canvas.sel = None
        self.doc.dirty = True
        self.refresh()

    def options(self):
        dr, sh = self.current_drawing(), self.current_sheet()
        if dr is None:
            return
        d = OptionsDialog(dr, sh, self)
        if d.exec() != QDialog.Accepted:
            return
        opts, size, land, scale, border = d.values()
        try:
            self.doc.set_drawing(dr.name, sh.name, options=opts, size=size, landscape=land,
                                 scale=scale if scale != sh.scale else None)
            sh.border = border
        except FeatureError as e:
            return self._err("Options", e)
        self.refresh(regen=True, fit=True)

    def title(self):
        dr, sh = self.current_drawing(), self.current_sheet()
        if dr is None:
            return
        d = TitleDialog(sh, self)
        if d.exec() == QDialog.Accepted:
            self.doc.set_drawing(dr.name, sh.name, title=d.values())
            self.refresh()

    def export(self, ext):
        dr = self.current_drawing()
        if dr is None:
            return
        if ext == ".dwg" and not dxfio.dwg_available():
            return QMessageBox.information(self, "DWG", dxfio.DWG_HELP)
        filt = {".pdf": "PDF (*.pdf)", ".dxf": "DXF (*.dxf)", ".dwg": "DWG (*.dwg)", ".svg": "SVG (*.svg)",
                ".png": "PNG (*.png)"}[ext]
        base = Path(dr.source).with_suffix(ext) if dr.source else Path(dr.name + ext)
        path, _ = QFileDialog.getSaveFileName(self, "Export drawing", str(base), filt)
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        try:
            out = self.doc.export_drawing(path, dr.name, None if ext == ".pdf" else self.current_sheet().name,
                                          version=dr.options.dxf_version)
            self.status.emit(f"Saved {out}")
        except FeatureError as e:
            self._err("Export", e)
