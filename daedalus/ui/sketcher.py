"""Interactive 2D sketch editor.

Draw on the sketch plane (viewed normal-to), connect by snapping to points,
add relations and dimensions, drag geometry — the SolveSpace solver keeps
everything consistent. Under-defined geometry is copper, fully defined is
light grey, conflicts are red.

Keys: S select · L line · R rectangle · C circle · A arc · D dimension
      H / V horizontal / vertical · X construction · Del delete · F fit
      Esc cancel · Ctrl+Z / Ctrl+Y undo / redo · Ctrl+Enter finish
Mouse: wheel zoom · middle-drag pan · drag points/curves · box-select on empty space
"""
from __future__ import annotations

import copy
import json
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..core import sketch2d as sk
from ..core import sketch_shapes as SS
from ..core.sketch2d import ORIGIN, SketchError
from . import theme

C_BG = QColor(theme.SK_BG)
C_GRID = QColor(theme.SK_GRID)
C_GRID_MAJOR = QColor(theme.SK_GRID_MAJOR)
C_AXIS = QColor(theme.SK_AXIS)
C_DEFINED = QColor(theme.SK_DEFINED)
C_FREE = QColor(theme.SK_FREE)
C_CONSTR = QColor(theme.SK_CONSTR)
C_SEL = QColor(theme.SK_SEL)
C_HOVER = QColor(theme.SK_HOVER)
C_BAD = QColor(theme.SK_BAD)
C_DIM = QColor(theme.SK_DIM)
C_UNDER = QColor(theme.SK_UNDER)

GLYPH = {"horizontal": "H", "vertical": "V", "parallel": "∥", "perpendicular": "⊥", "equal": "=",
         "tangent": "T", "midpoint": "M", "symmetric": "S", "fix": "F", "coincident": "●",
         "point_on": "○"}
EXTRA_TOOLS = {"trim": "Trim: click the piece of a curve to remove", "extend": "Extend: click near a line's end",
               "split": "Split: click a line or arc where it should split",
               "convert": "Convert entities: click a model edge (grey) to make it sketch geometry",
               "text": "Text: click where the text starts", "dim": "Dimension: pick line, circle, arc, or two items"}
SNAP_PX = 9
PICK_PX = 6
AUTO_HV_DEG = 3.0


def _fmt(v: float, quantity: str = "length") -> str:
    """Sketch dimension text in the document's units (no unit suffix for plain decimals)."""
    from ..core import units as U
    s = U.active()
    if quantity == "length" and s.length_format != "decimal":
        return U.fmt(v, "length")
    return U.fmt(v, quantity, show_unit=False)


def _ulab():
    from ..core import units as U
    return "" if U.active().length_format != "decimal" else U.label("length")


class SketchCanvas(QWidget):
    changed = Signal()              # geometry or solve state changed
    selection_changed = Signal()
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(400, 300)
        self.geom = sk.empty_geometry()
        self.result: sk.SolveResult | None = None
        self.underlay: list[list[tuple[float, float]]] = []
        self.underlay_pts: list[tuple[float, float]] = []
        self.tool = "select"
        self.pending: list = []           # picked items for the current tool
        self.chain_start: str | None = None
        self.sel: set[str] = set()
        self.hover: str | None = None
        self.mouse_w = (0.0, 0.0)
        self.snap: str | None = None      # point id under cursor
        self.snap_ref: tuple | None = None
        self.scale = 4.0
        self.off = QPointF(400, 300)
        self._pan = None
        self._drag = None
        self._box = None
        self._hits: list[tuple[QRectF, str]] = []
        self._undo: list[str] = []
        self._redo: list[str] = []
        from ..core import snap2d
        self.snap_opts = dict(snap2d.DEFAULTS)
        self.auto_relations = True
        self.grid = 0.0
        self.angle_step = 15.0
        self.snap_info = None             # snap2d.Snap under the cursor (non-point snaps)
        self._snap_at: dict = {}          # rounded world xy → Snap used when a point is created there
        self._snap_cons: list = []        # relations added from snaps (dropped if they over-define)
        self.opts = {"sides": 6, "circumscribed": False, "rho": 0.6}   # polygon / conic options
        self._load_snap_settings()

    def _load_snap_settings(self):
        from PySide6.QtCore import QSettings
        st = QSettings("Daedalus", "Daedalus")
        for k in self.snap_opts:
            v = st.value(f"sketch/snap/{k}")
            if v is not None:
                self.snap_opts[k] = str(v).lower() in ("true", "1")
        self.grid = float(st.value("sketch/grid", 0.0) or 0.0)
        self.angle_step = float(st.value("sketch/angle_step", 15.0) or 15.0)
        v = st.value("sketch/auto_relations")
        if v is not None:
            self.auto_relations = str(v).lower() in ("true", "1")

    def save_snap_settings(self):
        from PySide6.QtCore import QSettings
        st = QSettings("Daedalus", "Daedalus")
        for k, v in self.snap_opts.items():
            st.setValue(f"sketch/snap/{k}", v)
        st.setValue("sketch/grid", self.grid)
        st.setValue("sketch/angle_step", self.angle_step)
        st.setValue("sketch/auto_relations", self.auto_relations)

    # ------------------------------------------------------------ data
    def load(self, geom: dict | None, underlay=None):
        self.geom = sk.normalize(geom)
        self.underlay = underlay or []
        self.underlay_pts = list({(round(x, 6), round(y, 6)) for poly in self.underlay for x, y in (poly[0], poly[-1])})
        self.sel.clear()
        self.pending.clear()
        self.tool = "select"
        self._undo.clear()
        self._redo.clear()
        self._solve(analyze=True)
        self.fit()

    def geometry(self) -> dict:
        return copy.deepcopy(self.geom)

    def _solve(self, analyze=True):
        try:
            self.result = sk.solve(self.geom, analyze=analyze)
            if self.result.ok:
                self.geom = self.result.geometry
        except SketchError as e:
            self.result = sk.SolveResult(self.geom, False, -1, "failed")
            self.message.emit(str(e))
        self.changed.emit()
        self.update()

    def _snapshot(self):
        self._undo.append(json.dumps(self.geom))
        self._undo = self._undo[-200:]
        self._redo.clear()

    def commit(self, new_geom: dict, revert_if_conflict=True, what="That", quiet=False) -> bool:
        """Apply an edit. Rejects edits that over-define the sketch."""
        before = json.dumps(self.geom)
        try:
            res = sk.solve(new_geom, analyze=True)
        except SketchError as e:
            res = None
            err = e
        if (res is None or not res.ok) and self._snap_cons:
            # relations inferred from snapping are a convenience: drop them rather than reject the edit
            drop = list(self._snap_cons)
            self._snap_cons = []
            new_geom["constraints"] = [c for c in new_geom["constraints"] if c not in drop]
            try:
                res = sk.solve(new_geom, analyze=True)
            except SketchError as e:
                res, err = None, e
        self._snap_cons = []
        if res is None:
            self.message.emit(str(err))
            return False
        if not res.ok and revert_if_conflict:
            if not quiet:
                self.message.emit(f"{what} would over-define the sketch — not applied.")
            return False
        if not quiet:
            self.message.emit("")
        self._undo.append(before)
        self._redo.clear()
        self.geom = res.geometry if res.ok else sk.normalize(new_geom)
        self.result = res
        self.changed.emit()
        self.update()
        return True

    def undo(self):
        if self._undo:
            self._redo.append(json.dumps(self.geom))
            self.geom = json.loads(self._undo.pop())
            self.sel.clear()
            self._solve()

    def redo(self):
        if self._redo:
            self._undo.append(json.dumps(self.geom))
            self.geom = json.loads(self._redo.pop())
            self.sel.clear()
            self._solve()

    # ------------------------------------------------------- transform
    def w2s(self, x, y) -> QPointF:
        return QPointF(self.off.x() + x * self.scale, self.off.y() - y * self.scale)

    def s2w(self, p: QPointF):
        return ((p.x() - self.off.x()) / self.scale, (self.off.y() - p.y()) / self.scale)

    def fit(self):
        xs, ys = [], []
        for x, y in self.geom["points"].values():
            xs.append(x)
            ys.append(y)
        for c in self.geom["curves"]:
            if c["type"] == "circle":
                cx, cy = self.geom["points"][c["c"]]
                r = c["r"]
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
        for poly in self.underlay:
            for x, y in poly:
                xs.append(x)
                ys.append(y)
        w, h = max(self.width(), 200), max(self.height(), 200)
        if len(xs) < 2 or (max(xs) - min(xs) < 1e-6 and max(ys) - min(ys) < 1e-6):
            self.scale = 4.0
            self.off = QPointF(w / 2, h / 2)
        else:
            bw, bh = max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1)
            self.scale = 0.75 * min(w / bw, h / bh)
            cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
            self.off = QPointF(w / 2 - cx * self.scale, h / 2 + cy * self.scale)
        self.update()

    def resizeEvent(self, ev):
        if ev.oldSize().width() <= 0:
            self.fit()
        super().resizeEvent(ev)

    # ------------------------------------------------------- hit tests
    def _pt(self, pid):
        x, y = self.geom["points"][pid]
        return self.w2s(x, y)

    def _hit_point(self, pos: QPointF, exclude=()):
        best, bd = None, SNAP_PX
        for pid in self.geom["points"]:
            if pid in exclude:
                continue
            d = (self._pt(pid) - pos).manhattanLength()
            if d < bd:
                best, bd = pid, d
        return best

    def _hit_curve(self, pos: QPointF):
        wx, wy = self.s2w(pos)
        tol = PICK_PX / self.scale
        P = self.geom["points"]
        best, bd = None, tol
        for c in self.geom["curves"]:
            if c["type"] == "line":
                (ax, ay), (bx, by) = P[c["a"]], P[c["b"]]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy or 1e-12
                t = ((wx - ax) * dx + (wy - ay) * dy) / L2
                if not c.get("infinite"):
                    t = max(0, min(1, t))
                d = math.hypot(wx - (ax + t * dx), wy - (ay + t * dy))
            elif c["type"] in ("point",):
                continue
            elif c["type"] == "text":
                ax, ay = P[c["a"]]
                h = float(c.get("height", 10))
                ang = math.radians(float(c.get("angle", 0)))
                lx = (wx - ax) * math.cos(ang) + (wy - ay) * math.sin(ang)
                ly = -(wx - ax) * math.sin(ang) + (wy - ay) * math.cos(ang)
                d = 0.0 if (-tol <= lx <= len(sk.text_value(c)) * 0.62 * h + tol and -tol <= ly <= h + tol) else 1e9
            elif c["type"] not in ("circle", "arc"):
                pts = sk.samples(self.geom, c)
                d = 1e9
                for i in range(len(pts) - 1):
                    (ax, ay), (bx, by) = pts[i], pts[i + 1]
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy or 1e-12
                    t = max(0, min(1, ((wx - ax) * dx + (wy - ay) * dy) / L2))
                    d = min(d, math.hypot(wx - (ax + t * dx), wy - (ay + t * dy)))
            else:
                cx, cy = P[c["c"]]
                r = c["r"] if c["type"] == "circle" else math.hypot(P[c["a"]][0] - cx, P[c["a"]][1] - cy)
                d = abs(math.hypot(wx - cx, wy - cy) - r)
                if c["type"] == "arc" and d < bd and not self._in_arc(c, wx, wy):
                    continue
            if d < bd:
                best, bd = c["id"], d
        return best

    def _in_arc(self, c, wx, wy):
        P = self.geom["points"]
        cx, cy = P[c["c"]]
        a0 = math.atan2(P[c["a"]][1] - cy, P[c["a"]][0] - cx)
        a1 = math.atan2(P[c["b"]][1] - cy, P[c["b"]][0] - cx)
        t = math.atan2(wy - cy, wx - cx)
        sweep = (a1 - a0) % (2 * math.pi)
        return (t - a0) % (2 * math.pi) <= sweep + 1e-9

    def _hit(self, pos: QPointF):
        for rect, hid in reversed(self._hits):
            if rect.contains(pos):
                return hid
        return self._hit_point(pos) or self._hit_curve(pos)

    def _kind(self, eid):
        if eid.startswith("k:"):
            return "constraint"
        return sk.kind(self.geom, eid)

    # ----------------------------------------------------------- tools
    def set_tool(self, tool: str):
        self.tool = tool
        self.pending.clear()
        self.chain_start = None
        if tool != "select":
            self.sel.clear()
            self.selection_changed.emit()
        self.setCursor(Qt.ArrowCursor if tool == "select" else Qt.CrossCursor)
        if tool in SS.TOOLS:
            self.message.emit(f"{SS.TOOLS[tool][0]}: click the {SS.hint(tool, 0)}")
        elif tool in EXTRA_TOOLS:
            self.message.emit(EXTRA_TOOLS[tool])
        self.update()
        self.changed.emit()

    def _new_point(self, g, pos_w, snap_id):
        if snap_id and snap_id in g["points"]:
            return snap_id
        pid = sk.new_id(g, "p")
        g["points"][pid] = [pos_w[0], pos_w[1]]
        snap = self._snap_at.get((round(pos_w[0], 7), round(pos_w[1], 7)))
        if snap is not None and self.auto_relations:
            from ..core.snap2d import relations_for
            known = set(g["points"]) | {c["id"] for c in g["curves"]}
            for k in relations_for(snap, pid):
                if all(e in known for e in k["e"]):
                    g["constraints"].append(k)
                    self._snap_cons.append(k)
        return pid

    def _line_snap_relations(self, g, lid, end_w):
        """Perpendicular-to / through-centre relations for a line that ended on such a snap."""
        snap = self._snap_at.get((round(end_w[0], 7), round(end_w[1], 7)))
        if snap is None or not self.auto_relations:
            return
        known = set(g["points"]) | {c["id"] for c in g["curves"]}
        if snap.refs.get("perp_line") in known:
            k = {"type": "perpendicular", "e": [lid, snap.refs["perp_line"]]}
            g["constraints"].append(k)
            self._snap_cons.append(k)
        elif snap.refs.get("through_center") in known:
            k = {"type": "point_on", "e": [snap.refs["through_center"], lid]}
            g["constraints"].append(k)
            self._snap_cons.append(k)

    def _auto_hv(self, g, lid):
        c = sk.curve(g, lid)
        (ax, ay), (bx, by) = g["points"][c["a"]], g["points"][c["b"]]
        dx, dy = bx - ax, by - ay
        t = math.tan(math.radians(AUTO_HV_DEG))
        if abs(dy) <= t * abs(dx):
            return {"type": "horizontal", "e": [lid]}
        if abs(dx) <= t * abs(dy):
            return {"type": "vertical", "e": [lid]}
        return None

    def _add_line(self, a_id, a_w, b_id, b_w):
        g = copy.deepcopy(self.geom)
        a = self._new_point(g, a_w, a_id)
        b = self._new_point(g, b_w, b_id)
        if a == b:
            return None
        lid = sk.new_id(g, "l")
        g["curves"].append({"id": lid, "type": "line", "a": a, "b": b})
        self._line_snap_relations(g, lid, b_w)
        auto = self._auto_hv(g, lid) if self.auto_relations else None
        if auto:
            g2 = copy.deepcopy(g)
            g2["constraints"].append(auto)
            keep = list(self._snap_cons)
            if self.commit(g2, quiet=True):
                return b
            self._snap_cons = keep
        return b if self.commit(g, what="Line") else None

    def _tool_click(self, pos: QPointF):
        w = self.mouse_w
        snap = self.snap
        if self.snap_info is not None and not snap:
            if len(self._snap_at) > 200:
                self._snap_at.clear()
            self._snap_at[(round(w[0], 7), round(w[1], 7))] = self.snap_info
        if self.tool in ("trim", "extend"):
            from ..core import sketch_ops as so
            cid = self._hit_curve(pos)
            if not cid:
                self.message.emit("Click the piece of a curve to " + self.tool)
                return
            try:
                g = (so.trim if self.tool == "trim" else so.extend)(self.geom, cid, self.mouse_w)
            except SketchError as e:
                self.message.emit(str(e))
                return
            self.commit(g, revert_if_conflict=False, what=self.tool.title())
            return
        if self.tool == "split":
            from ..core import sketch_ops as so
            cid = self._hit_curve(pos)
            if not cid:
                return self.message.emit("Click a line or arc where it should split")
            try:
                self.commit(so.split(self.geom, cid, self.mouse_w), revert_if_conflict=False, what="Split")
            except SketchError as e:
                self.message.emit(str(e))
            return
        if self.tool == "convert":
            return self._convert_click(pos)
        if self.tool == "text":
            return self._text_click(snap, w)
        if self.tool in SS.TOOLS and self.tool != "line":
            need = SS.needed(self.tool)
            if need is None and len(self.pending) >= 2:          # spline: clicking the first point closes it
                fx, fy = self.pending[0][1]
                if math.hypot(w[0] - fx, w[1] - fy) * self.scale < SNAP_PX:
                    self.pending.append(self.pending[0])
                    return self._finish_shape()
            self.pending.append((snap, w))
            if need is None or len(self.pending) < need:
                self.message.emit(f"{SS.TOOLS[self.tool][0]}: click the {SS.hint(self.tool, len(self.pending))}")
                return
            return self._finish_shape()
        if self.tool == "line":
            if not self.pending:
                self.pending = [(snap, w)]
                self.chain_start = snap
                return
            a_id, a_w = self.pending[-1]
            closing = snap is not None and snap == self.chain_start
            b = self._add_line(a_id, a_w, snap, w)
            if b is None:
                return
            if self.chain_start is None:
                # first segment created its start point; remember it for closing the loop
                last = self.geom["curves"][-1]
                self.chain_start = last["a"]
            if closing:
                self.pending.clear()
                self.chain_start = None
            else:
                self.pending = [(b, tuple(self.geom["points"][b]))]
        elif self.tool == "rect":
            if not self.pending:
                self.pending = [(snap, w)]
                return
            (a_id, (x0, y0)), (x1, y1) = self.pending[0], w
            if abs(x1 - x0) < 1e-6 or abs(y1 - y0) < 1e-6:
                return
            g = copy.deepcopy(self.geom)
            p = [self._new_point(g, (x0, y0), a_id), self._new_point(g, (x1, y0), None),
                 self._new_point(g, (x1, y1), snap), self._new_point(g, (x0, y1), None)]
            ids = []
            for i in range(4):
                lid = sk.new_id(g, "l")
                g["curves"].append({"id": lid, "type": "line", "a": p[i], "b": p[(i + 1) % 4]})
                ids.append(lid)
            g["constraints"] += [{"type": "horizontal", "e": [ids[0]]}, {"type": "horizontal", "e": [ids[2]]},
                                 {"type": "vertical", "e": [ids[1]]}, {"type": "vertical", "e": [ids[3]]}]
            self.commit(g, what="Rectangle")
            self.pending.clear()
        elif self.tool == "circle":
            if not self.pending:
                self.pending = [(snap, w)]
                return
            c_id, (cx, cy) = self.pending[0]
            r = math.hypot(w[0] - cx, w[1] - cy)
            if r < 1e-6:
                return
            g = copy.deepcopy(self.geom)
            c = self._new_point(g, (cx, cy), c_id)
            g["curves"].append({"id": sk.new_id(g, "c"), "type": "circle", "c": c, "r": r})
            self.commit(g, what="Circle")
            self.pending.clear()
        elif self.tool == "arc":
            self.pending.append((snap, w))
            if len(self.pending) < 3:
                return
            (c_id, (cx, cy)), (a_id, (ax, ay)), (b_id, (bx, by)) = self.pending
            r = math.hypot(ax - cx, ay - cy)
            ang = math.atan2(by - cy, bx - cx)
            bx, by = cx + r * math.cos(ang), cy + r * math.sin(ang)
            g = copy.deepcopy(self.geom)
            c = self._new_point(g, (cx, cy), c_id)
            a = self._new_point(g, (ax, ay), a_id)
            b = self._new_point(g, (bx, by), b_id)
            # direction: CCW unless the user swept clockwise past the start
            cross = (ax - cx) * (by - cy) - (ay - cy) * (bx - cx)
            if cross < 0:
                a, b = b, a
            g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": c, "a": a, "b": b})
            self.commit(g, what="Arc")
            self.pending.clear()
        elif self.tool == "dim":
            hid = self._hit(pos)
            if not hid or hid.startswith("k:"):
                return
            self.pending.append(hid)
            self._try_dimension()

    def _finish_shape(self):
        """Build the current click-tool's shape from the clicks so far and commit it."""
        tool, clicks = self.tool, list(self.pending)
        self.pending.clear()
        try:
            g = SS.build(tool, self.geom, clicks, newpt=self._new_point, opts=self.opts)
        except SketchError as e:
            self.message.emit(str(e))
            return
        if self.commit(g, what=SS.TOOLS[tool][0]):
            self.message.emit(f"{SS.TOOLS[tool][0]} added — click the {SS.hint(tool, 0)} for another")
        self.update()

    def finish_open_tool(self):
        """Enter / double-click / right-click: finish a spline (or end a line chain)."""
        if self.tool == "spline" and len(self.pending) >= 2:
            self._finish_shape()
            return True
        if self.tool == "line" and self.pending:
            self.pending.clear()
            self.chain_start = None
            return True
        return False

    def _text_click(self, snap, w):
        dlg = TextDialog(self)
        if not dlg.exec():
            return
        v = dlg.values()
        if not v["text"].strip():
            return
        g = SS.text_entity(self.geom, w, v["text"], v["height"], v["font"], v["angle"], v["bold"], v["italic"],
                           sid=snap)
        self.commit(g, what="Text")

    def _convert_click(self, pos):
        """Convert entities: the reference edge (projected model edge) under the click becomes sketch geometry."""
        if not self.underlay:
            return self.message.emit("No model edges in this sketch's plane to convert")
        wx, wy = self.s2w(pos)
        best, bd = None, PICK_PX / self.scale * 1.5
        for poly in self.underlay:
            for i in range(len(poly) - 1):
                (ax, ay), (bx, by) = poly[i], poly[i + 1]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy or 1e-12
                t = max(0, min(1, ((wx - ax) * dx + (wy - ay) * dy) / L2))
                d = math.hypot(wx - (ax + t * dx), wy - (ay + t * dy))
                if d < bd:
                    best, bd = poly, d
        if best is None:
            return self.message.emit("Click on a model edge (the grey lines) to convert it")
        g = copy.deepcopy(self.geom)
        pts = [tuple(q) for q in best]
        closed = len(pts) > 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6
        straight = len(pts) == 2 or all(
            abs((pts[-1][0] - pts[0][0]) * (q[1] - pts[0][1]) - (pts[-1][1] - pts[0][1]) * (q[0] - pts[0][0]))
            / (math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) or 1) < 1e-4 for q in pts)
        if straight and not closed:
            a, b = self._new_point(g, pts[0], None), self._new_point(g, pts[-1], None)
            g["curves"].append({"id": sk.new_id(g, "l"), "type": "line", "a": a, "b": b})
        else:
            if closed:
                pts = pts[:-1]
            step = max(1, len(pts) // 24)
            keep = pts[::step]
            ids = [self._new_point(g, q, None) for q in keep]
            if not closed and keep[-1] != pts[-1]:
                ids.append(self._new_point(g, pts[-1], None))
            g["curves"].append({"id": sk.new_id(g, "s"), "type": "spline", "pts": ids, "closed": closed})
        self.commit(g, what="Convert")

    def _measure(self, t, e):
        P = self.geom["points"]
        if t == "length":
            c = sk.curve(self.geom, e[0])
            (ax, ay), (bx, by) = P[c["a"]], P[c["b"]]
            return math.hypot(bx - ax, by - ay)
        if t in ("radius", "diameter"):
            c = sk.curve(self.geom, e[0])
            cx, cy = P[c["c"]]
            r = c["r"] if c["type"] == "circle" else math.hypot(P[c["a"]][0] - cx, P[c["a"]][1] - cy)
            return r if t == "radius" else 2 * r
        if t == "hdistance":
            return abs(P[e[1]][0] - P[e[0]][0])
        if t == "vdistance":
            return abs(P[e[1]][1] - P[e[0]][1])
        if t == "distance":
            a = P[e[0]]
            if e[1] in P:
                b = P[e[1]]
                return math.hypot(b[0] - a[0], b[1] - a[1])
            c = sk.curve(self.geom, e[1])
            (x1, y1), (x2, y2) = P[c["a"]], P[c["b"]]
            L = math.hypot(x2 - x1, y2 - y1) or 1
            return abs((x2 - x1) * (y1 - a[1]) - (x1 - a[0]) * (y2 - y1)) / L
        if t == "angle":
            l1, l2 = (sk.curve(self.geom, x) for x in e)
            v1 = (P[l1["b"]][0] - P[l1["a"]][0], P[l1["b"]][1] - P[l1["a"]][1])
            v2 = (P[l2["b"]][0] - P[l2["a"]][0], P[l2["b"]][1] - P[l2["a"]][1])
            d = math.degrees(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))
            return abs(d)
        return 0.0

    def _try_dimension(self):
        ks = [self._kind(x) for x in self.pending]
        e = list(self.pending)
        t = None
        if ks == ["line"]:
            t = "length"
        elif ks == ["circle"]:
            t = "diameter"
        elif ks == ["arc"]:
            t = "radius"
        elif ks == ["point"]:
            self.message.emit("Pick a second point or a line")
            return
        elif ks == ["point", "point"]:
            P = self.geom["points"]
            dx = abs(P[e[1]][0] - P[e[0]][0])
            dy = abs(P[e[1]][1] - P[e[0]][1])
            t = "hdistance" if dx > 3 * dy else "vdistance" if dy > 3 * dx else "distance"
        elif sorted(ks) == ["line", "point"]:
            t = "distance"
            if ks[0] == "line":
                e = [e[1], e[0]]
        elif ks == ["line", "line"]:
            t = "angle"
        if t is None:
            self.message.emit("Can't dimension that combination")
            self.pending.clear()
            return
        self.pending.clear()
        self.add_dimension(t, e)

    def add_dimension(self, t, e, value=None):
        cur = self._measure(t, e)
        if value is None:
            label = {"length": "Length", "diameter": "Diameter", "radius": "Radius", "distance": "Distance",
                     "hdistance": "Horizontal distance", "vdistance": "Vertical distance", "angle": "Angle"}[t]
            from .units_ui import ask_quantity
            value = ask_quantity(self, "Dimension", label, cur, "angle" if t == "angle" else "length", 0.0)
            if value is None:
                return
        if t == "angle":
            # keep the solver on the branch the user sees
            pass
        g = copy.deepcopy(self.geom)
        g["constraints"].append({"type": t, "e": e, "value": float(value)})
        self.commit(g, what="That dimension")

    def edit_dimension(self, idx: int):
        k = self.geom["constraints"][idx]
        from .units_ui import ask_quantity
        v = ask_quantity(self, "Edit dimension", k["type"], k["value"], "angle" if k["type"] == "angle" else "length",
                         0.0)
        if v is not None:
            g = copy.deepcopy(self.geom)
            g["constraints"][idx]["value"] = float(v)
            self.commit(g, what="That value")

    # ---------------------------------------------------- relations api
    def selection_kinds(self):
        return [self._kind(x) for x in sorted(self.sel)]

    def available_relations(self) -> set[str]:
        sel = sorted(x for x in self.sel if not x.startswith("k:"))
        ks = [self._kind(x) for x in sel]
        out = set()
        n_l = ks.count("line")
        n_p = ks.count("point")
        n_c = ks.count("circle") + ks.count("arc")
        if ks and all(k == "line" for k in ks) or (n_p == 2 and len(ks) == 2):
            out |= {"horizontal", "vertical"}
        if n_p == 2 and len(ks) == 2:
            out.add("coincident")
        if n_p == 1 and len(ks) == 2 and (n_l == 1 or n_c == 1):
            out.add("point_on")
        if n_l == 2 and len(ks) == 2:
            out |= {"parallel", "perpendicular"}
        if (n_l >= 2 and n_l == len(ks)) or (n_c >= 2 and n_c == len(ks)):
            out.add("equal")
        if len(ks) == 2 and "arc" in ks and (n_l == 1 or n_c == 2):
            out.add("tangent")
        if n_c == 2 and len(ks) == 2:
            out |= {"concentric", "coradial"}
        if n_c == 1 and n_p == 1 and len(ks) == 2:
            out.add("concentric")
        if n_l == 2 and len(ks) == 2:
            out.add("collinear")
        if n_p == 1 and n_l == 1 and len(ks) == 2:
            out.add("midpoint")
        if n_p == 2 and len(ks) == 3 and (n_l == 1 or ks.count("point") == 3):
            out.add("symmetric")
        if n_p == 3 and len(ks) == 3:
            out.add("symmetric")
        if n_p >= 1 and n_p == len(ks):
            out.add("fix")
        if any(k in ("line", "circle", "arc") for k in ks):
            out.add("construction")
        if self.sel:
            out.add("delete")
        return out

    def apply_relation(self, rel: str):
        sel = sorted(x for x in self.sel if not x.startswith("k:"))
        ks = [self._kind(x) for x in sel]
        g = copy.deepcopy(self.geom)
        pts = [x for x, k in zip(sel, ks) if k == "point"]
        lines = [x for x, k in zip(sel, ks) if k == "line"]
        circ = [x for x, k in zip(sel, ks) if k in ("circle", "arc")]
        cons = g["constraints"]
        if rel in ("horizontal", "vertical"):
            if lines:
                cons += [{"type": rel, "e": [l]} for l in lines]
            else:
                cons.append({"type": rel, "e": pts})
        elif rel == "coincident":
            keep, drop = pts
            sk.merge_points(g, keep, drop)
        elif rel == "point_on":
            cons.append({"type": "point_on", "e": [pts[0], (lines or circ)[0]]})
        elif rel in ("parallel", "perpendicular"):
            cons.append({"type": rel, "e": lines})
        elif rel == "equal":
            group = lines or circ
            cons += [{"type": "equal", "e": [group[0], x]} for x in group[1:]]
        elif rel == "tangent":
            cons.append({"type": "tangent", "e": [x for x in sel if self._kind(x) != "point"]})
        elif rel == "midpoint":
            cons.append({"type": "midpoint", "e": [pts[0], lines[0]]})
        elif rel in ("concentric", "coradial"):
            cs = [sk.curve(g, c) for c in circ]
            if pts:                                   # circle + point: centre on the point
                sk.merge_points(g, pts[0], cs[0]["c"]) if pts[0] == ORIGIN else sk.merge_points(g, cs[0]["c"], pts[0])
            else:
                keep, drop = cs[0]["c"], cs[1]["c"]
                if drop == ORIGIN:
                    keep, drop = drop, keep
                if keep != drop:
                    sk.merge_points(g, keep, drop)
                if rel == "coradial":
                    g["constraints"].append({"type": "equal", "e": [circ[0], circ[1]]})
        elif rel == "collinear":
            a, b = (sk.curve(g, l) for l in lines)
            g["constraints"].append({"type": "parallel", "e": lines})
            g["constraints"].append({"type": "point_on", "e": [b["a"], lines[0]]})
        elif rel == "symmetric":
            about = lines[0] if lines else pts[2]
            a, b = pts[:2]
            cons.append({"type": "symmetric", "e": [a, b, about]})
        elif rel == "fix":
            cons += [{"type": "fix", "e": [p]} for p in pts if p != ORIGIN]
        elif rel == "construction":
            for c in g["curves"]:
                if c["id"] in sel:
                    c["construction"] = not c.get("construction", False)
        elif rel == "delete":
            return self.delete_selection()
        if self.commit(g, what=f"{rel.title()}"):
            self.sel.clear()
            self.selection_changed.emit()

    def action(self, what):
        """Offset / fillet / mirror / patterns on the current selection."""
        from PySide6.QtWidgets import QInputDialog
        from ..core import sketch_ops as so
        order = list(getattr(self, "_sel_order", [])) or sorted(self.sel)
        ids = [x for x in order if x in self.sel and not x.startswith("k:")]
        from ..core.sketch_ops import MOVABLE
        allc = [i for i in ids if sk.kind(self.geom, i) in MOVABLE]
        curves = [i for i in allc if sk.kind(self.geom, i) in ("line", "circle", "arc")]
        pts = [i for i in ids if sk.kind(self.geom, i) == "point"]
        try:
            if what == "equation":
                dlg = EquationDialog(self)
                if dlg.exec():
                    v = dlg.values()
                    from ..core import sketch_shapes as _ss
                    self.commit(_ss.equation_curve(self.geom, v["mode"], v["x"], v["y"], v["t0"], v["t1"]),
                                what="Equation curve")
                return
            if what == "chamfer":
                if not pts:
                    return self.message.emit("Select corner point(s) where two lines meet")
                txt, ok = QInputDialog.getText(self, "Sketch chamfer", "Distance (mm), or two distances  d1, d2:",
                                               text="3")
                if ok:
                    vals = [float(v) for v in txt.replace(";", ",").split(",") if v.strip()]
                    g = self.geom
                    for q in pts:
                        g = so.chamfer(g, q, vals[0], vals[1] if len(vals) > 1 else None)
                    self.commit(g, revert_if_conflict=False, what="Chamfer")
                return
            if what in ("move", "copy", "rotate", "scale"):
                sel = allc + [q for q in pts if q != ORIGIN]
                if not sel:
                    return self.message.emit("Select the geometry first")
                xs = [self.geom["points"][q] for i in sel for q in
                      ([i] if i in self.geom["points"] else sk.curve_points(sk.curve(self.geom, i)))]
                cx = sum(x for x, _ in xs) / len(xs)
                cy = sum(y for _, y in xs) / len(xs)
                prompts = {"move": ("Move", "dx, dy (mm):", "20, 0"), "copy": ("Copy", "dx, dy (mm):", "20, 0"),
                           "rotate": ("Rotate", "angle °, about x, about y:", f"90, {cx:g}, {cy:g}"),
                           "scale": ("Scale", "factor, about x, about y:", f"2, {cx:g}, {cy:g}")}
                title, lab, default = prompts[what]
                txt, ok = QInputDialog.getText(self, title, lab, text=default)
                if not ok:
                    return
                v = [float(x) for x in txt.replace(";", ",").split(",") if x.strip()]
                if what in ("move", "copy"):
                    g = so.transform(self.geom, sel, "move", copy_=what == "copy", dx=v[0], dy=v[1] if len(v) > 1 else 0)
                elif what == "rotate":
                    g = so.transform(self.geom, sel, "rotate", angle=v[0], about=(v[1], v[2]) if len(v) > 2 else (cx, cy))
                else:
                    g = so.transform(self.geom, sel, "scale", scale=v[0], about=(v[1], v[2]) if len(v) > 2 else (cx, cy))
                self.commit(g, revert_if_conflict=False, what=title)
                return
            if what == "segments":
                one = [i for i in allc if sk.kind(self.geom, i) in ("line", "circle", "arc", "spline", "ellipse",
                                                                     "conic", "equation")]
                if len(one) != 1:
                    return self.message.emit("Select one curve to divide")
                n, ok = QInputDialog.getInt(self, "Segments", "Equal segments:", 4, 2, 200)
                if ok:
                    self.commit(so.segment_points(self.geom, one[0], n), revert_if_conflict=False, what="Segments")
                return
            if what == "belt":
                circ = [i for i in ids if i in {c["id"] for c in self.geom["curves"] if c["type"] == "circle"}]
                if len(circ) < 2:
                    return self.message.emit("Select two or more circles (pulleys) in belt order, then Belt")
                from PySide6.QtWidgets import QMessageBox
                as_constr = QMessageBox.question(
                    self, "Belt / chain", "Make the belt construction geometry (a layout, not part of the profile)?",
                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes
                g = so.belt(self.geom, circ, construction=as_constr)
                if self.commit(g, revert_if_conflict=False, what="Belt"):
                    L = so.belt_length(self.geom, [c["id"] for c in self.geom["curves"] if c.get("belt")])
                    self.message.emit(f"Belt length {L:.2f} mm")
                return
            if what == "offset":
                if not curves:
                    return self.message.emit("Select the curves to offset first")
                d, ok = QInputDialog.getDouble(self, "Offset", "Distance (mm, negative = other side):", 5.0,
                                               -1e5, 1e5, 3)
                if ok:
                    self.commit(so.offset(self.geom, curves, d), revert_if_conflict=False, what="Offset")
            elif what == "fillet":
                if not pts:
                    return self.message.emit("Select corner point(s) where two lines meet")
                r, ok = QInputDialog.getDouble(self, "Sketch fillet", "Radius (mm):", 5.0, 0.001, 1e5, 3)
                if ok:
                    g = self.geom
                    for p in pts:
                        g = so.fillet(g, p, r)
                    self.commit(g, revert_if_conflict=False, what="Fillet")
            elif what == "mirror":
                if len(allc) < 2 or sk.kind(self.geom, allc[-1]) != "line":
                    return self.message.emit("Select the curves, then the mirror line last")
                self.commit(so.mirror(self.geom, allc[:-1], allc[-1]), revert_if_conflict=False, what="Mirror")
            elif what == "lpattern":
                if not allc:
                    return self.message.emit("Select curves to pattern")
                txt, ok = QInputDialog.getText(self, "Linear pattern", "count, dx, dy (mm):", text="3, 20, 0")
                if ok:
                    n, dx, dy = [float(v) for v in txt.split(",")]
                    self.commit(so.linear_pattern(self.geom, allc, dx, dy, int(n)), revert_if_conflict=False,
                                what="Pattern")
            elif what == "cpattern":
                if not allc:
                    return self.message.emit("Select curves to pattern")
                txt, ok = QInputDialog.getText(self, "Circular pattern", "count, centre x, centre y, angle:",
                                               text="6, 0, 0, 360")
                if ok:
                    n, cx, cy, ang = [float(v) for v in txt.split(",")]
                    self.commit(so.circular_pattern(self.geom, allc, (cx, cy), int(n), ang),
                                revert_if_conflict=False, what="Pattern")
        except (SketchError, ValueError) as e:
            self.message.emit(str(e))
        self.sel.clear()
        self.selection_changed.emit()

    def delete_selection(self):
        if not self.sel:
            return
        g = copy.deepcopy(self.geom)
        kidx = sorted((int(x[2:]) for x in self.sel if x.startswith("k:")), reverse=True)
        for i in kidx:
            if i < len(g["constraints"]):
                g["constraints"].pop(i)
        sk.remove_entities(g, {x for x in self.sel if not x.startswith("k:")})
        self.sel.clear()
        self.commit(g, revert_if_conflict=False)
        self.selection_changed.emit()

    # ------------------------------------------------------------ mouse
    def _update_snap(self, pos: QPointF):
        exclude = set()
        if self._drag:
            exclude = set(self._drag["pids"])
        self.snap = self._hit_point(pos, exclude) if (self.tool != "select" or self._drag) and \
            self.snap_opts.get("endpoint", True) else None
        self.snap_ref = None
        self.snap_info = None
        if self.snap:
            self.mouse_w = tuple(self.geom["points"][self.snap])
            return
        w = self.s2w(pos)
        if self.tool not in ("select", "trim", "extend", "dim") and not self._drag:
            from ..core import snap2d
            items, points = snap2d.sketch_items(self.geom, self.underlay)
            from_pt = None
            if self.pending and (self.tool in SS.TOOLS):
                from_pt = tuple(self.pending[-1][1]) if self.tool in ("line", "spline", "arc_tangent") \
                    else tuple(self.pending[0][1])
            sn = snap2d.find(items, points, w[0], w[1], SNAP_PX / self.scale, self.snap_opts, from_pt,
                             self.grid, self.angle_step, exclude_points=exclude)
            if sn is not None:
                self.snap_info = sn
                if sn.kind == "endpoint" and sn.refs.get("point") is None:
                    self.snap_ref = sn.xy
                self.mouse_w = sn.xy
                return
        self.mouse_w = w

    def mousePressEvent(self, ev):
        pos = ev.position()
        self.setFocus()
        if ev.button() == Qt.MiddleButton or (ev.button() == Qt.LeftButton and ev.modifiers() & Qt.AltModifier):
            self._pan = (pos, QPointF(self.off))
            return
        if ev.button() == Qt.RightButton:
            if self.tool == "spline" and len(self.pending) >= 2:
                self._finish_shape()
            elif self.pending:
                self.pending.clear()
                self.chain_start = None
            else:
                self.set_tool("select")
            self.update()
            return
        if ev.button() != Qt.LeftButton:
            return
        self._update_snap(pos)
        if self.tool != "select":
            self._tool_click(pos)
            self.update()
            return
        hid = self._hit(pos)
        additive = bool(ev.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
        if hid and hid.startswith("k:") and self.geom["constraints"][int(hid[2:])]["type"] in sk.DIMENSIONS:
            self.sel = {hid}
            self.selection_changed.emit()
            self._dimdrag = {"i": int(hid[2:]), "before": json.dumps(self.geom), "moved": False}
            self.update()
            return
        if hid:
            if additive:
                self.sel ^= {hid}
            elif hid not in self.sel:
                self.sel = {hid}
            order = [x for x in getattr(self, "_sel_order", []) if x in self.sel and x != hid]
            self._sel_order = order + ([hid] if hid in self.sel else [])
            self.selection_changed.emit()
            if not hid.startswith("k:"):
                k = self._kind(hid)
                pids = [hid] if k == "point" else sk.curve_points(sk.curve(self.geom, hid))
                pids = [p for p in pids if p != ORIGIN]
                if pids:
                    self._drag = {"pids": pids, "start": self.s2w(pos), "orig": {p: tuple(self.geom["points"][p]) for p in pids},
                                  "before": json.dumps(self.geom), "moved": False}
        else:
            if not additive:
                self.sel.clear()
                self.selection_changed.emit()
            self._box = (pos, pos)
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._pan:
            p0, off0 = self._pan
            self.off = off0 + (pos - p0)
            self.update()
            return
        if getattr(self, "_dimdrag", None):
            self.geom["constraints"][self._dimdrag["i"]]["at"] = list(self.s2w(pos))
            self._dimdrag["moved"] = True
            self.update()
            return
        self._update_snap(pos)
        if self._drag:
            sx, sy = self._drag["start"]
            if len(self._drag["pids"]) == 1:
                tx, ty = self.mouse_w
                targets = {self._drag["pids"][0]: (tx, ty)}
            else:
                wx, wy = self.s2w(pos)
                targets = {p: (ox + wx - sx, oy + wy - sy) for p, (ox, oy) in self._drag["orig"].items()}
            base = json.loads(self._drag["before"])
            try:
                res = sk.solve(base, drag=targets)
                if res.ok:
                    self.geom = res.geometry
                    self.result = res
                    self._drag["moved"] = True
            except SketchError:
                pass
            self.update()
            return
        if self._box:
            self._box = (self._box[0], pos)
            self.update()
            return
        self.hover = self._hit(pos)
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, ev):
        if self._pan:
            self._pan = None
            return
        if getattr(self, "_dimdrag", None):
            d, self._dimdrag = self._dimdrag, None
            if d["moved"]:
                self._undo.append(d["before"])
                self._redo.clear()
                self.changed.emit()
            return
        if self._drag:
            d = self._drag
            self._drag = None
            if d["moved"]:
                # merge onto a point when a single point is dropped on another
                g = copy.deepcopy(self.geom)
                if len(d["pids"]) == 1 and self.snap and self.snap != d["pids"][0]:
                    sk.merge_points(g, self.snap, d["pids"][0])
                    self.sel.clear()
                self._undo.append(d["before"])
                self._redo.clear()
                ok = sk.solve(g, analyze=True)
                if ok.ok:
                    self.geom, self.result = ok.geometry, ok
                else:
                    self.geom = json.loads(d["before"])
                    self.message.emit("That would over-define the sketch — not applied.")
                    self._solve()
                self.changed.emit()
            self.update()
            return
        if self._box:
            a, b = self._box
            self._box = None
            r = QRectF(a, b).normalized()
            if r.width() > 3 and r.height() > 3:
                for pid in self.geom["points"]:
                    if pid != ORIGIN and r.contains(self._pt(pid)):
                        self.sel.add(pid)
                for c in self.geom["curves"]:
                    if all(r.contains(self._pt(p)) for p in sk.curve_points(c)):
                        self.sel.add(c["id"])
                self.selection_changed.emit()
            self.update()

    def mouseDoubleClickEvent(self, ev):
        hid = self._hit(ev.position())
        if hid and hid.startswith("k:"):
            idx = int(hid[2:])
            if self.geom["constraints"][idx]["type"] in sk.DIMENSIONS:
                self.edit_dimension(idx)
        elif self.tool == "spline" and len(self.pending) >= 2:
            if len(self.pending) >= 3 and self.pending[-1][1] == self.pending[-2][1]:
                self.pending.pop()                          # the double-click's own press
            self._finish_shape()
        elif self.tool == "line" and self.pending:
            self.pending.clear()
            self.chain_start = None
            self.update()

    def wheelEvent(self, ev):
        pos = ev.position()
        w = self.s2w(pos)
        f = 1.15 ** (ev.angleDelta().y() / 120)
        self.scale = max(0.02, min(2000, self.scale * f))
        p2 = self.w2s(*w)
        self.off += pos - p2
        self.update()

    def keyPressEvent(self, ev):
        k = ev.key()
        mods = ev.modifiers()
        if k == Qt.Key_Escape:
            if self.pending:
                self.pending.clear()
                self.chain_start = None
            elif self.tool != "select":
                self.set_tool("select")
            else:
                self.sel.clear()
                self.selection_changed.emit()
            self.update()
        elif k in (Qt.Key_Return, Qt.Key_Enter) and not mods & Qt.ControlModifier and self.finish_open_tool():
            self.update()
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selection()
        elif mods & Qt.ControlModifier and k == Qt.Key_Z:
            self.undo()
        elif mods & Qt.ControlModifier and k == Qt.Key_Y:
            self.redo()
        elif not mods:
            m = {Qt.Key_S: "select", Qt.Key_L: "line", Qt.Key_R: "rect", Qt.Key_C: "circle",
                 Qt.Key_A: "arc", Qt.Key_D: "dim", Qt.Key_T: "trim", Qt.Key_E: "extend", Qt.Key_P: "point",
                 Qt.Key_G: "slot", Qt.Key_N: "spline", Qt.Key_O: "polygon"}
            if k in m:
                self.set_tool(m[k])
            elif k == Qt.Key_F:
                self.fit()
            elif k == Qt.Key_H and "horizontal" in self.available_relations():
                self.apply_relation("horizontal")
            elif k == Qt.Key_V and "vertical" in self.available_relations():
                self.apply_relation("vertical")
            elif k == Qt.Key_X and "construction" in self.available_relations():
                self.apply_relation("construction")
            else:
                super().keyPressEvent(ev)
        else:
            super().keyPressEvent(ev)

    # ------------------------------------------------------------ paint
    def _curve_color(self, c, free, bad_ids):
        if c["id"] in self.sel:
            return C_SEL
        if c["id"] == self.hover:
            return C_HOVER
        if c["id"] in bad_ids:
            return C_BAD
        if c.get("construction"):
            return C_CONSTR
        pts = sk.curve_points(c)
        if any(p in free for p in pts):
            return C_FREE
        if c["type"] == "circle" and not self._radius_fixed(c["id"]):
            return C_FREE
        return C_DEFINED

    def _radius_fixed(self, cid):
        if self.result and self.result.ok and self.result.dof == 0:
            return True
        return any(cid in k["e"] and k["type"] in ("radius", "diameter", "equal", "tangent")
                   for k in self.geom["constraints"])

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), C_BG)
        self._hits = []
        self._paint_grid(p)
        res = self.result
        free = res.free_points if res and res.ok else set()
        bad_k = set(res.failed) if res and not res.ok else set()
        bad_ids = {e for i in bad_k if i < len(self.geom["constraints"]) for e in self.geom["constraints"][i]["e"]}

        # underlay (existing body edges in the sketch plane)
        p.setPen(QPen(C_UNDER, 1.2))
        for poly in self.underlay:
            path = QPainterPath(self.w2s(*poly[0]))
            for x, y in poly[1:]:
                path.lineTo(self.w2s(x, y))
            p.drawPath(path)

        P = self.geom["points"]
        for c in self.geom["curves"]:
            col = self._curve_color(c, free, bad_ids)
            pen = QPen(col, 2.2 if c["id"] in self.sel else 1.8)
            if c.get("construction"):
                pen.setStyle(Qt.DashLine)
                pen.setWidthF(1.2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            self._draw_curve(p, c, self.geom)

        # points
        for pid, (x, y) in P.items():
            s = self.w2s(x, y)
            if pid == ORIGIN:
                p.setPen(QPen(QColor(theme.ACCENT_LINE), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(s, 5, 5)
                p.drawLine(s + QPointF(-9, 0), s + QPointF(9, 0))
                p.drawLine(s + QPointF(0, -9), s + QPointF(0, 9))
                continue
            col = C_SEL if pid in self.sel else C_HOVER if pid == self.hover else \
                C_FREE if pid in free else C_DEFINED
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            r = 3.6 if pid in self.sel or pid == self.hover else 2.6
            p.drawRect(QRectF(s.x() - r, s.y() - r, 2 * r, 2 * r))

        self._paint_constraints(p, bad_k)
        self._paint_preview(p)

        if self._box:
            a, b = self._box
            p.setPen(QPen(C_SEL, 1, Qt.DashLine))
            p.setBrush(QColor(*theme.SK_RUBBER))
            p.drawRect(QRectF(a, b).normalized())
        self._paint_status(p)
        p.end()

    def _draw_curve(self, p, c, g):
        """Draw one curve of geometry g with the current pen."""
        P = g["points"]
        t = c["type"]
        try:
            if t == "line":
                a, b = self.w2s(*P[c["a"]]), self.w2s(*P[c["b"]])
                if c.get("infinite"):
                    d = b - a
                    L = math.hypot(d.x(), d.y()) or 1.0
                    k = 4 * (self.width() + self.height()) / L
                    a, b = a - d * k, b + d * k
                p.drawLine(a, b)
            elif t == "circle":
                cen = self.w2s(*P[c["c"]])
                r = float(c["r"]) * self.scale
                p.drawEllipse(cen, r, r)
            elif t == "arc":
                cx, cy = P[c["c"]]
                ax, ay = P[c["a"]]
                bx, by = P[c["b"]]
                r = math.hypot(ax - cx, ay - cy)
                a0 = math.degrees(math.atan2(ay - cy, ax - cx))
                a1 = math.degrees(math.atan2(by - cy, bx - cx))
                span = (a1 - a0) % 360 or 360
                tl = self.w2s(cx - r, cy + r)
                rect = QRectF(tl.x(), tl.y(), 2 * r * self.scale, 2 * r * self.scale)
                path = QPainterPath()
                path.arcMoveTo(rect, a0)
                path.arcTo(rect, a0, span)
                p.drawPath(path)
            elif t == "text":
                ax, ay = P[c["a"]]
                f = QFont(c.get("font") or "Arial")
                f.setPixelSize(max(1, int(float(c.get("height", 10)) * self.scale * 1.38)))
                f.setBold(bool(c.get("bold")))
                f.setItalic(bool(c.get("italic")))
                path = QPainterPath()
                path.addText(0, 0, f, sk.text_value(c, getattr(self, "props", None)))
                p.save()
                p.translate(self.w2s(ax, ay))
                p.rotate(-float(c.get("angle", 0)))
                p.drawPath(path)
                p.restore()
            elif t == "point":
                pass
            else:
                pts = sk.samples(g, c)
                if len(pts) >= 2:
                    path = QPainterPath(self.w2s(*pts[0]))
                    for x, y in pts[1:]:
                        path.lineTo(self.w2s(x, y))
                    p.drawPath(path)
                if t in ("ellipse", "conic") and c["id"] in self.sel:      # show the helper points' frame
                    p.save()
                    p.setPen(QPen(C_CONSTR, 1, Qt.DotLine))
                    if t == "ellipse":
                        p.drawLine(self.w2s(*P[c["c"]]), self.w2s(*P[c["a"]]))
                        p.drawLine(self.w2s(*P[c["c"]]), self.w2s(*P[c["b"]]))
                    else:
                        p.drawLine(self.w2s(*P[c["a"]]), self.w2s(*P[c["t"]]))
                        p.drawLine(self.w2s(*P[c["t"]]), self.w2s(*P[c["b"]]))
                    p.restore()
        except (KeyError, ValueError, ZeroDivisionError):
            pass

    def _paint_grid(self, p):
        # adaptive grid: aim for ~20 px minor spacing
        target = 20 / self.scale
        step = 10 ** math.floor(math.log10(target))
        for m in (1, 2, 5, 10):
            if step * m >= target:
                step *= m
                break
        x0, y1 = self.s2w(QPointF(0, 0))
        x1, y0 = self.s2w(QPointF(self.width(), self.height()))
        i0, i1 = math.floor(x0 / step), math.ceil(x1 / step)
        j0, j1 = math.floor(y0 / step), math.ceil(y1 / step)
        if (i1 - i0) + (j1 - j0) < 600:
            for i in range(i0, i1 + 1):
                p.setPen(QPen(C_GRID_MAJOR if i % 5 == 0 else C_GRID, 1))
                X = self.w2s(i * step, 0).x()
                p.drawLine(QPointF(X, 0), QPointF(X, self.height()))
            for j in range(j0, j1 + 1):
                p.setPen(QPen(C_GRID_MAJOR if j % 5 == 0 else C_GRID, 1))
                Y = self.w2s(0, j * step).y()
                p.drawLine(QPointF(0, Y), QPointF(self.width(), Y))
        p.setPen(QPen(C_AXIS, 1, Qt.DashDotLine))
        o = self.w2s(0, 0)
        p.drawLine(QPointF(0, o.y()), QPointF(self.width(), o.y()))
        p.drawLine(QPointF(o.x(), 0), QPointF(o.x(), self.height()))

    def _anchor(self, eid):
        """Screen anchor and outward normal for annotating an entity."""
        P = self.geom["points"]
        if eid in P:
            return self._pt(eid), QPointF(1, -1)
        c = sk.curve(self.geom, eid)
        if c["type"] == "line":
            a, b = self._pt(c["a"]), self._pt(c["b"])
            m = (a + b) / 2
            d = b - a
            L = math.hypot(d.x(), d.y()) or 1
            return m, QPointF(-d.y() / L, d.x() / L)
        cen = self._pt(c["c"])
        if c["type"] == "circle":
            r = c["r"] * self.scale
            return cen + QPointF(r * 0.707, -r * 0.707), QPointF(0.707, -0.707)
        mx, my = sk.arc_mid(self.geom, c)
        m = self.w2s(mx, my)
        d = m - cen
        L = math.hypot(d.x(), d.y()) or 1
        return m, QPointF(d.x() / L, d.y() / L)

    def _label(self, p, center: QPointF, text: str, hid: str, color: QColor, boxed=False):
        f = QFont(self.font())
        f.setPointSizeF(8.5 if boxed else 9.5)
        f.setBold(not boxed)
        p.setFont(f)
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + (8 if boxed else 6)
        h = fm.height() + (2 if boxed else 0)
        r = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
        selected = hid in self.sel
        if boxed:
            p.setPen(QPen(C_SEL if selected else color, 1))
            p.setBrush(QColor(theme.SK_BADGE_BG))
            p.drawRoundedRect(r, 3, 3)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SK_BG))
            p.drawRect(r)
        p.setPen(C_SEL if selected else C_HOVER if hid == self.hover else color)
        p.drawText(r, Qt.AlignCenter, text)
        self._hits.append((r, hid))

    def _paint_constraints(self, p, bad_k):
        P = self.geom["points"]
        stack: dict[str, int] = {}
        for i, k in enumerate(self.geom["constraints"]):
            hid = f"k:{i}"
            t, e = k["type"], k["e"]
            col = C_BAD if i in bad_k else C_DIM
            if t in sk.DIMENSIONS:
                self._paint_dimension(p, i, k, col)
                continue
            anchor_id = next((x for x in e if x not in P), e[0])
            if t in ("coincident", "point_on"):
                anchor_id = e[0]
            base, n = self._anchor(anchor_id)
            j = stack.get(anchor_id, 0)
            stack[anchor_id] = j + 1
            pos = base - n * 14 + QPointF(16 * j, 0)
            self._label(p, pos, GLYPH.get(t, "?"), hid, C_BAD if i in bad_k else QColor(theme.SK_BADGE), boxed=True)

    def _paint_dimension(self, p, i, k, col):
        """Dimensions. Drag a label to move it; its world position is kept in k['at']."""
        P = self.geom["points"]
        t, e, v = k["type"], k["e"], k["value"]
        hid = f"k:{i}"
        at = self.w2s(*k["at"]) if "at" in k else None
        p.setPen(QPen(C_SEL if hid in self.sel else col, 1))
        if t in ("length", "distance", "hdistance", "vdistance") and (t == "length" or e[1] in P):
            if t == "length":
                c = sk.curve(self.geom, e[0])
                a, b = P[c["a"]], P[c["b"]]
            else:
                a, b = P[e[0]], P[e[1]]
            A, B = self.w2s(*a), self.w2s(*b)
            if t == "hdistance":
                y = at.y() if at else min(A.y(), B.y()) - 22
                A2, B2 = QPointF(A.x(), y), QPointF(B.x(), y)
            elif t == "vdistance":
                x = at.x() if at else max(A.x(), B.x()) + 22
                A2, B2 = QPointF(x, A.y()), QPointF(x, B.y())
            else:
                d = B - A
                L = math.hypot(d.x(), d.y()) or 1
                n = QPointF(-d.y() / L, d.x() / L)
                off = 20.0
                if at:
                    q = at - A
                    off = q.x() * n.x() + q.y() * n.y()
                A2, B2 = A + n * off, B + n * off
            p.drawLine(A, A2)
            p.drawLine(B, B2)
            p.drawLine(A2, B2)
            mid = (A2 + B2) / 2
            if at:   # slide the text along the dimension line
                d = B2 - A2
                L2 = d.x() ** 2 + d.y() ** 2 or 1
                tt = ((at - A2).x() * d.x() + (at - A2).y() * d.y()) / L2
                mid = A2 + d * tt
                if tt < 0 or tt > 1:
                    p.drawLine(A2 if tt < 0 else B2, mid)
            self._label(p, mid, _fmt(v), hid, col)
        elif t == "distance":
            a = self._pt(e[0])
            c = sk.curve(self.geom, e[1])
            (x1, y1), (x2, y2) = P[c["a"]], P[c["b"]]
            ax, ay = P[e[0]]
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy or 1
            u = ((ax - x1) * dx + (ay - y1) * dy) / L2
            foot = self.w2s(x1 + u * dx, y1 + u * dy)
            p.drawLine(a, foot)
            self._label(p, at or (a + foot) / 2, _fmt(v), hid, col)
        elif t in ("radius", "diameter"):
            c = sk.curve(self.geom, e[0])
            cen = self._pt(c["c"])
            pt, n = self._anchor(e[0])
            if at:
                d = at - cen
                L = math.hypot(d.x(), d.y()) or 1
                out = at
                n = QPointF(d.x() / L, d.y() / L)
            else:
                out = pt + n * 26
            start = cen - n * (math.hypot((pt - cen).x(), (pt - cen).y())) if t == "diameter" else cen
            p.drawLine(start, out)
            self._label(p, out + n * 12, ("R" if t == "radius" else "Ø") + _fmt(v), hid, col)
        elif t == "angle":
            m1, _ = self._anchor(e[0])
            m2, _ = self._anchor(e[1])
            self._label(p, at or (m1 + m2) / 2, _fmt(v, "angle") + "°", hid, col)

    def _paint_preview(self, p):
        if self.tool == "select" or not self.pending and not self.snap and not self.snap_ref:
            if self.tool == "select":
                return
        cur = self.w2s(*self.mouse_w)
        p.setPen(QPen(C_SEL, 1.4, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        if self.pending and self.tool in SS.TOOLS and self.tool != "line":
            self._paint_shape_preview(p, cur)
        if self.pending and self.tool == "line":
            a = self.w2s(*self.pending[0][1])
            if self.tool == "line":
                a = self.w2s(*self.pending[-1][1])
                p.drawLine(a, cur)
                (ax, ay), (bx, by) = self.pending[-1][1], self.mouse_w
                L = math.hypot(bx - ax, by - ay)
                self._label(p, (a + cur) / 2 + QPointF(0, -14), _fmt(round(L, 2)), "_prev", C_SEL)
            elif self.tool == "rect":
                p.drawRect(QRectF(a, cur).normalized())
                (ax, ay), (bx, by) = self.pending[0][1], self.mouse_w
                self._label(p, cur + QPointF(40, -14), f"{_fmt(round(abs(bx-ax),2))} × {_fmt(round(abs(by-ay),2))}", "_prev", C_SEL)
            elif self.tool == "circle":
                d = cur - a
                r = math.hypot(d.x(), d.y())
                p.drawEllipse(a, r, r)
                self._label(p, cur + QPointF(30, -14), "R" + _fmt(round(r / self.scale, 2)), "_prev", C_SEL)
            elif self.tool == "arc":
                if len(self.pending) == 1:
                    p.drawLine(a, cur)
                else:
                    cx, cy = self.pending[0][1]
                    ax, ay = self.pending[1][1]
                    r = math.hypot(ax - cx, ay - cy)
                    a0 = math.degrees(math.atan2(ay - cy, ax - cx))
                    a1 = math.degrees(math.atan2(self.mouse_w[1] - cy, self.mouse_w[0] - cx))
                    span = (a1 - a0) % 360
                    if span > 180:
                        span -= 360
                    tl = self.w2s(cx - r, cy + r)
                    rect = QRectF(tl.x(), tl.y(), 2 * r * self.scale, 2 * r * self.scale)
                    path = QPainterPath()
                    path.arcMoveTo(rect, a0)
                    path.arcTo(rect, a0, span)
                    p.drawPath(path)
        if self.tool == "dim":
            for hid in self.pending:
                base, _ = self._anchor(hid)
                p.setPen(QPen(C_SEL, 2))
                p.drawEllipse(base, 6, 6)
        si = self.snap_info
        if si is not None:
            p.setPen(QPen(QColor(theme.SK_GUIDE), 1, Qt.DashLine))
            for a, b in si.guides:
                p.drawLine(self.w2s(*a), self.w2s(*b))
        if self.snap or self.snap_ref or si is not None:
            kind = "endpoint" if self.snap or self.snap_ref else si.kind
            p.setPen(QPen(QColor(theme.SK_GUIDE), 1.6))
            p.setBrush(Qt.NoBrush)
            if kind == "endpoint":
                p.drawRect(QRectF(cur.x() - 6, cur.y() - 6, 12, 12))
            elif kind == "midpoint":
                path = QPainterPath(QPointF(cur.x(), cur.y() - 7))
                path.lineTo(cur.x() + 7, cur.y() + 5)
                path.lineTo(cur.x() - 7, cur.y() + 5)
                path.closeSubpath()
                p.drawPath(path)
            elif kind == "center":
                p.drawEllipse(cur, 7, 7)
                p.drawLine(cur + QPointF(-4, 0), cur + QPointF(4, 0))
                p.drawLine(cur + QPointF(0, -4), cur + QPointF(0, 4))
            elif kind == "quadrant":
                path = QPainterPath(QPointF(cur.x(), cur.y() - 7))
                for dx, dy in ((7, 0), (0, 7), (-7, 0)):
                    path.lineTo(cur.x() + dx, cur.y() + dy)
                path.closeSubpath()
                p.drawPath(path)
            elif kind == "intersection":
                p.drawLine(cur + QPointF(-6, -6), cur + QPointF(6, 6))
                p.drawLine(cur + QPointF(-6, 6), cur + QPointF(6, -6))
            else:
                p.drawEllipse(cur, 5, 5)
            if si is not None and not self.snap:
                from ..core.snap2d import GLYPHS
                p.setFont(QFont(self.font().family(), 8))
                lab = si.kind if si.kind != "angle" else f"{si.refs.get('angle', 0):g}°"
                p.drawText(cur + QPointF(10, 16), f"{GLYPHS.get(si.kind, '')} {lab}")

    def _paint_shape_preview(self, p, cur):
        need = SS.needed(self.tool)
        clicks = list(self.pending) + [(None, tuple(self.mouse_w))]
        ready = (need is None and len(clicks) >= 2) or (need is not None and len(clicks) >= need)
        if ready:
            try:
                g2 = SS.build(self.tool, self.geom, clicks[:need] if need else clicks, opts=self.opts)
            except (SketchError, ValueError, ZeroDivisionError):
                g2 = None
            if g2 is not None:
                old = {c["id"] for c in self.geom["curves"]}
                for c in g2["curves"]:
                    if c["id"] not in old:
                        pen = QPen(C_SEL, 1.4, Qt.DashLine)
                        if c.get("construction"):
                            pen.setWidthF(1.0)
                            pen.setStyle(Qt.DotLine)
                        p.setPen(pen)
                        self._draw_curve(p, c, g2)
                p.setPen(QPen(C_SEL, 1.4, Qt.DashLine))
                return
        path = QPainterPath(self.w2s(*clicks[0][1]))
        for _sid, xy in clicks[1:]:
            path.lineTo(self.w2s(*xy))
        p.drawPath(path)
        for _sid, xy in self.pending:
            q = self.w2s(*xy)
            p.drawEllipse(q, 3, 3)

    def _paint_status(self, p):
        r = self.result
        if r is None:
            return
        if not r.ok:
            txt, col = f"Over-defined — {len(r.failed)} conflicting relation(s) shown in red", C_BAD
        elif r.dof == 0:
            txt, col = "Fully defined", QColor(theme.OK)
        else:
            txt, col = f"Under-defined · {r.dof} DOF", C_FREE
        hint = {"select": "Select / drag", "line": "Line: click points · close loop or right-click to end",
                "dim": "Dimension: pick line, circle, arc, or two items"}
        x, y = self.mouse_w
        f = QFont(self.font())
        f.setPointSizeF(9)
        p.setFont(f)
        p.setPen(col)
        p.drawText(QPointF(12, self.height() - 30), txt)
        p.setPen(QColor(theme.MUTED))
        if self.tool in SS.TOOLS and self.tool not in hint:
            ht = f"{SS.TOOLS[self.tool][0]}: {SS.hint(self.tool, len(self.pending))}"
        else:
            ht = hint.get(self.tool, EXTRA_TOOLS.get(self.tool, ""))
        p.drawText(QPointF(12, self.height() - 12), f"{ht}   ·   x {_fmt(x)}  y {_fmt(y)} {_ulab()}")


class TextDialog(QDialog):
    """Sketch text: the words, height, font, angle; {PartNo} etc. link document properties."""

    def __init__(self, parent=None, c=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QLineEdit
        self.setWindowTitle("Sketch text")
        c = c or {}
        f = QFormLayout(self)
        self.text = QLineEdit(c.get("text", ""))
        self.text.setPlaceholderText("Text — {PartNo}, {Description}, {Material}, {Name}, {Date} link properties")
        f.addRow("Text", self.text)
        self.height_ = QDoubleSpinBox()
        self.height_.setRange(0.1, 10000)
        self.height_.setDecimals(2)
        self.height_.setSuffix(" mm")
        self.height_.setValue(float(c.get("height", 10.0)))
        f.addRow("Height", self.height_)
        self.font_ = QFontComboBox()
        self.font_.setCurrentText(c.get("font", "Arial"))
        f.addRow("Font", self.font_)
        self.angle = QDoubleSpinBox()
        self.angle.setRange(-360, 360)
        self.angle.setSuffix(" °")
        self.angle.setValue(float(c.get("angle", 0)))
        f.addRow("Angle", self.angle)
        self.bold = QCheckBox("Bold")
        self.bold.setChecked(bool(c.get("bold")))
        self.italic = QCheckBox("Italic")
        self.italic.setChecked(bool(c.get("italic")))
        row = QHBoxLayout()
        row.addWidget(self.bold)
        row.addWidget(self.italic)
        f.addRow("", row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return {"text": self.text.text(), "height": self.height_.value(), "font": self.font_.currentText(),
                "angle": self.angle.value(), "bold": self.bold.isChecked(), "italic": self.italic.isChecked()}


class EquationDialog(QDialog):
    """Equation driven curve: explicit y = f(x) or parametric x(t), y(t); radians; anchored at the origin."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QFormLayout, QLineEdit
        self.setWindowTitle("Equation driven curve")
        f = QFormLayout(self)
        self.mode = QComboBox()
        self.mode.addItem("Parametric  x(t), y(t)", "parametric")
        self.mode.addItem("Explicit  y(x)", "explicit")
        f.addRow("Type", self.mode)
        self.x = QLineEdit("50*cos(t)")
        self.y = QLineEdit("50*sin(t)")
        self.t0 = QLineEdit("0")
        self.t1 = QLineEdit("pi")
        f.addRow("x(t)", self.x)
        self.ylab = QLabel("y(t)")
        f.addRow(self.ylab, self.y)
        f.addRow("t1 / x1", self.t0)
        f.addRow("t2 / x2", self.t1)
        note = QLabel("Radians · sin cos tan sqrt exp log pi · ^ or ** for powers · e.g. y = sqrt(4 - x^2)")
        note.setObjectName("muted")
        note.setWordWrap(True)
        f.addRow(note)
        self.err = QLabel("")
        self.err.setStyleSheet(f"color:{theme.ERROR}")
        f.addRow(self.err)
        self.mode.currentIndexChanged.connect(self._mode)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _mode(self, _i):
        explicit = self.mode.currentData() == "explicit"
        self.x.setEnabled(not explicit)
        self.ylab.setText("y(x)" if explicit else "y(t)")
        if explicit and self.y.text() == "50*sin(t)":
            self.y.setText("sqrt(4 - x^2)")
            self.t0.setText("-2")
            self.t1.setText("2")

    def values(self):
        return {"mode": self.mode.currentData(), "x": self.x.text() or "t", "y": self.y.text(),
                "t0": sk.eval_expr(self.t0.text()), "t1": sk.eval_expr(self.t1.text())}

    def _ok(self):
        try:
            v = self.values()
            c = {"c": ORIGIN, **v}
            sk.equation_points({"points": {ORIGIN: [0.0, 0.0]}}, c, n=8)
        except Exception as e:  # noqa: BLE001
            self.err.setText(str(e))
            return
        self.accept()


class SketchEditor(QWidget):
    """Toolbar + canvas. Emits finished(geometry) or cancelled()."""
    finished = Signal(dict)
    cancelled = Signal()

    # flyouts (SolidWorks-style): the button runs the last tool picked from its menu
    FLYOUTS = [("Line", "╱", ["line", "centerline", "midline", "infline"]),
               ("Rectangle", "▭", ["rect", "rect_center", "rect_3pt", "rect_3pt_center", "parallelogram"]),
               ("Slot", "⊂⊃", ["slot", "slot_center", "slot_arc3", "slot_arc_center"]),
               ("Circle", "◯", ["circle", "circle_perim"]),
               ("Arc", "◠", ["arc", "arc_tangent", "arc3"]),
               ("Polygon", "⬡", ["polygon"]),
               ("Ellipse", "⬭", ["ellipse", "ellipse_partial"]),
               ("Conic", "⌒", ["parabola", "conic"]),
               ("Spline", "∿", ["spline"]),
               ("Point", "•", ["point"])]
    TOOLS = [("select", "↖", "Select", "S"), ("text", "A", "Text", ""), ("dim", "↔", "Dimension", "D"),
             ("trim", "✂", "Trim", "T"), ("extend", "⇥", "Extend", "E"), ("split", "⫽", "Split", ""),
             ("convert", "⎘", "Convert", "")]
    ACTIONS = [("equation", "ƒ", "Equation driven curve…"),
               ("offset", "⧈", "Offset selected curves"), ("fillet", "◜", "Fillet selected corner points"),
               ("chamfer", "◸", "Chamfer selected corner points"),
               ("mirror", "⇋", "Mirror selection about the line selected last"),
               ("lpattern", "⋯", "Linear pattern of selection"), ("cpattern", "✻", "Circular pattern of selection"),
               ("move", "✥", "Move selection…"), ("copy", "⧉", "Copy selection…"),
               ("rotate", "⟳", "Rotate selection…"), ("scale", "⤢", "Scale selection…"),
               ("segments", "⋮", "Divide a curve into equal segments (points)"),
               ("belt", "∞", "Belt / chain around the selected circles")]
    RELATIONS = [("horizontal", "H", "Horizontal"), ("vertical", "V", "Vertical"),
                 ("coincident", "●", "Merge points"), ("point_on", "○", "Point on curve"),
                 ("parallel", "∥", "Parallel"), ("perpendicular", "⊥", "Perpendicular"),
                 ("equal", "=", "Equal"), ("tangent", "T", "Tangent"), ("midpoint", "M", "Midpoint"),
                 ("concentric", "◎", "Concentric"), ("coradial", "⊚", "Coradial (concentric + equal)"),
                 ("collinear", "⎯", "Collinear"),
                 ("symmetric", "S", "Symmetric"), ("fix", "F", "Fix"), ("construction", "┅", "Construction"),
                 ("delete", "✕", "Delete")]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("sketchbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet(f"#sketchbar {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(2)
        self.title = QLabel("Sketch")
        self.title.setObjectName("h1")
        h.addWidget(self.title)
        h.addSpacing(10)
        self.canvas = SketchCanvas()
        self.tool_btns = {}
        self.flyouts = {}
        sel = QToolButton()
        sel.setText("↖  Select")
        sel.setToolTip("Select (S)")
        sel.setCheckable(True)
        sel.clicked.connect(lambda: self.canvas.set_tool("select"))
        self.tool_btns["select"] = sel
        h.addWidget(sel)
        for group, glyph, keys in self.FLYOUTS:
            h.addWidget(self._flyout(group, glyph, keys))
        for key, glyph, label, sc in self.TOOLS:
            if key == "select":
                continue
            b = QToolButton()
            b.setText(f"{glyph}  {label}")
            b.setToolTip(f"{label}" + (f" ({sc})" if sc else ""))
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.canvas.set_tool(k))
            self.tool_btns[key] = b
            h.addWidget(b)
        for key, glyph, label in self.ACTIONS:
            b = QToolButton()
            b.setText(glyph)
            b.setToolTip(label)
            b.setFixedWidth(32)
            b.clicked.connect(lambda _=False, k=key: self.canvas.action(k))
            h.addWidget(b)
        sep = QLabel("│")
        sep.setObjectName("muted")
        h.addWidget(sep)
        self.rel_btns = {}
        for key, glyph, label in self.RELATIONS:
            b = QToolButton()
            b.setText(glyph)
            b.setToolTip(label)
            b.setFixedWidth(32)
            b.clicked.connect(lambda _=False, k=key: self.canvas.apply_relation(k))
            self.rel_btns[key] = b
            h.addWidget(b)
        sep2 = QLabel("│")
        sep2.setObjectName("muted")
        h.addWidget(sep2)
        h.addWidget(self._snap_button())
        h.addStretch(1)
        undo = QToolButton()
        undo.setText("↶")
        undo.setToolTip("Undo (Ctrl+Z)")
        undo.clicked.connect(self.canvas.undo)
        redo = QToolButton()
        redo.setText("↷")
        redo.setToolTip("Redo (Ctrl+Y)")
        redo.clicked.connect(self.canvas.redo)
        h.addWidget(undo)
        h.addWidget(redo)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancelled.emit)
        done = QPushButton("✓  Finish sketch")
        done.setObjectName("primary")
        done.clicked.connect(self._finish)
        h.addSpacing(8)
        h.addWidget(cancel)
        h.addWidget(done)
        from .adaptive import h_scroller
        lay.addWidget(h_scroller(bar))
        self.msg = QLabel("")
        self.msg.setStyleSheet(f"color:{theme.ACCENT_LINE}; padding:3px 10px; background:{theme.BG};")
        lay.addWidget(self.msg)
        lay.addWidget(self.canvas, 1)
        self.canvas.changed.connect(self._sync)
        self.canvas.selection_changed.connect(self._sync)
        self.canvas.message.connect(self.msg.setText)
        self._sync()

    def _flyout(self, group, glyph, keys):
        b = QToolButton()
        b.setCheckable(True)
        b.setPopupMode(QToolButton.MenuButtonPopup if len(keys) > 1 or group in ("Polygon", "Conic")
                       else QToolButton.DelayedPopup)
        m = QMenu(b)
        b._keys = keys
        b._group = group
        b._glyph = glyph

        def pick(k):
            b._current = k
            b.setText(f"{glyph}  {SS.TOOLS[k][0] if len(keys) > 1 else group}")
            self.canvas.set_tool(k)
        for k in keys:
            m.addAction(SS.TOOLS[k][0], lambda k=k: pick(k))
        if group == "Polygon":
            m.addSeparator()

            def sides():
                v, ok = QInputDialog.getInt(self, "Polygon", "Sides (3–40):", int(self.canvas.opts["sides"]), 3, 40)
                if ok:
                    self.canvas.opts["sides"] = v
                    pick("polygon")
            m.addAction("Sides…", sides)
            circ = m.addAction("Click sets a side's middle (circumscribed)")
            circ.setCheckable(True)
            circ.toggled.connect(lambda on: self.canvas.opts.__setitem__("circumscribed", on))
        if group == "Conic":
            m.addSeparator()

            def rho():
                v, ok = QInputDialog.getDouble(self, "Conic", "Rho (0–1: <0.5 elliptical, 0.5 parabolic, "
                                                              ">0.5 hyperbolic):", float(self.canvas.opts["rho"]),
                                               0.01, 0.99, 3)
                if ok:
                    self.canvas.opts["rho"] = v
                    pick("conic")
            m.addAction("Rho…", rho)
        b.setMenu(m)
        b._current = keys[0]
        b.setText(f"{glyph}  {SS.TOOLS[keys[0]][0] if len(keys) > 1 else group}")
        b.setToolTip(group + (": " + ", ".join(SS.TOOLS[k][0] for k in keys) if len(keys) > 1 else ""))
        b.clicked.connect(lambda _=False: pick(b._current))
        self.flyouts[group] = b
        return b

    def _snap_button(self):
        from PySide6.QtWidgets import QInputDialog, QMenu
        from ..core import snap2d
        b = QToolButton()
        b.setText("Snap")
        from . import icons as _icons
        if _icons.available():
            from PySide6.QtCore import QSize as _QSize
            b.setIcon(_icons.icon("snaps"))
            b.setIconSize(_QSize(16, 16))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        else:
            b.setText("🧲 Snap")
        b.setToolTip("Snapping and automatic relations")
        m = QMenu(b)
        labels = {"endpoint": "Endpoints", "midpoint": "Midpoints → midpoint relation",
                  "center": "Centres", "quadrant": "Quadrants", "intersection": "Intersections",
                  "nearest": "On curve → point-on relation", "perpendicular": "Perpendicular → ⊥ relation",
                  "tangent": "Tangent", "inference": "Line up with points (H/V inference)",
                  "grid": "Grid", "angle": "Angle steps"}
        for k in snap2d.KINDS:
            a = m.addAction(labels[k])
            a.setCheckable(True)
            a.setChecked(self.canvas.snap_opts.get(k, True))
            a.toggled.connect(lambda on, k=k: (self.canvas.snap_opts.__setitem__(k, on),
                                               self.canvas.save_snap_settings()))
        m.addSeparator()
        ar = m.addAction("Add relations from snaps and near-H/V lines")
        ar.setCheckable(True)
        ar.setChecked(self.canvas.auto_relations)
        ar.toggled.connect(lambda on: (setattr(self.canvas, "auto_relations", on), self.canvas.save_snap_settings()))

        def ask_grid():
            v, ok = QInputDialog.getDouble(self, "Grid", "Grid spacing (mm, 0 = off):", self.canvas.grid, 0, 1000, 3)
            if ok:
                self.canvas.grid = v
                self.canvas.snap_opts["grid"] = v > 0
                self.canvas.save_snap_settings()
                self.canvas.update()

        def ask_angle():
            v, ok = QInputDialog.getDouble(self, "Angle step", "Angle step (°):", self.canvas.angle_step, 1, 90, 1)
            if ok:
                self.canvas.angle_step = v
                self.canvas.save_snap_settings()
        m.addAction("Grid spacing…", ask_grid)
        m.addAction("Angle step…", ask_angle)
        b.setMenu(m)
        b.setPopupMode(QToolButton.InstantPopup)
        return b

    def load(self, name: str, plane: str, geom: dict | None, underlay=None):
        self.title.setText(f"✎  {name}  ·  {plane}")
        self.msg.setText("")
        self.canvas.load(geom, underlay)
        self.canvas.setFocus()

    def _sync(self):
        for k, b in self.tool_btns.items():
            b.setChecked(self.canvas.tool == k)
        for b in getattr(self, "flyouts", {}).values():
            on = self.canvas.tool in b._keys
            b.setChecked(on)
            if on and b._current != self.canvas.tool:
                b._current = self.canvas.tool
                b.setText(f"{b._glyph}  {SS.TOOLS[b._current][0] if len(b._keys) > 1 else b._group}")
        avail = self.canvas.available_relations()
        for k, b in self.rel_btns.items():
            b.setEnabled(k in avail)

    def _finish(self):
        self.finished.emit(self.canvas.geometry())

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and ev.modifiers() & Qt.ControlModifier:
            self._finish()
        else:
            super().keyPressEvent(ev)
