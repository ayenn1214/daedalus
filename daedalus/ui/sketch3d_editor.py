"""3D sketch editor, drawn directly in the 3D viewport.

Points are placed on the active plane (XY / YZ / XZ, Tab cycles) through the
last point, or along a locked axis (X / Y / Z keys). Segments that come out
within 3° of an axis get an along-axis relation automatically.

Mouse: left click place / select · left-drag a point to move it (on the active plane)
       middle-drag rotate · Shift/Ctrl + middle-drag pan · wheel zoom
       double-click a dimension to edit it · right click ends the current chain
Keys:  S select · L line · P spline · A arc · Tab next plane · X/Y/Z lock axis
       D dimension · Del delete · Enter finish spline · Esc cancel · Ctrl+Z / Ctrl+Y
"""
from __future__ import annotations

import copy
import json
import math

from . import vtklite as vtk
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QMenu, QPushButton, QToolButton, QWidget,
)

from ..core import sketch3d as s3
from ..core.sketch3d import ORIGIN, Sketch3DError
from . import theme

PLANES = {"XY": (0, 0, 1), "YZ": (1, 0, 0), "XZ": (0, 1, 0)}
PLANE_ORDER = ["XY", "YZ", "XZ"]
AXIS_COLOR = {"x": (0.90, 0.30, 0.30), "y": (0.35, 0.80, 0.35), "z": (0.35, 0.55, 0.95)}
COL_DEFINED = (0.90, 0.91, 0.93)
COL_FREE = (0.85, 0.51, 0.17)
COL_SEL = (0.31, 0.76, 0.97)
COL_HOVER = (0.61, 0.89, 1.0)
COL_BAD = (0.90, 0.33, 0.29)
COL_CONSTR = (0.49, 0.52, 0.56)
COL_DIM = (0.62, 0.80, 1.0)
GLYPH = {"along_x": "∥X", "along_y": "∥Y", "along_z": "∥Z", "perpendicular": "⊥", "equal": "=",
         "midpoint": "M", "fix": "F", "coincident": "●", "point_on": "○"}
SNAP_PX = 10
AUTO_DEG = 3.0


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _mul(a, s):
    return [x * s for x in a]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _fmt(v, quantity="length"):
    from ..core import units as U
    if quantity == "length" and U.active().length_format != "decimal":
        return U.fmt(v, "length")
    return U.fmt(v, quantity, show_unit=False)


class Sketch3DController(QObject):
    changed = Signal()
    message = Signal(str)

    def __init__(self, viewport):
        super().__init__()
        self.vp = viewport
        self.ren = viewport.ren
        self.iren = viewport.iren
        self.active = False
        self.geom = s3.empty_geometry()
        self.result = None
        self.tool = "line"
        self.plane = "XY"
        self.lock: str | None = None
        self.pending: list = []          # [(point id | None, xyz)]
        self.chain_start = None
        self.anchor = [0.0, 0.0, 0.0]
        self.cursor = None               # world position under the mouse (snapped)
        self.snap_id = None
        self.sel: set[str] = set()
        self.hover = None
        self._tags = []
        self._drag = None
        self._mid = None
        self._press = None
        self._undo, self._redo = [], []
        self._ref_pts: list[tuple] = []
        self._labels: list[tuple] = []   # (world xyz, hid, actor)
        self._build_actors()

    # ---------------------------------------------------------- actors
    def _build_actors(self):
        def line_actor(width, tubes=True):
            a = vtk.vtkActor()
            a.GetProperty().SetLineWidth(width)
            a.GetProperty().SetRenderLinesAsTubes(tubes)
            a.GetProperty().SetLighting(False)
            a.SetVisibility(False)
            return a
        self.lines = line_actor(4)
        self.points = vtk.vtkActor()
        self.points.GetProperty().SetPointSize(11)
        self.points.GetProperty().SetRenderPointsAsSpheres(True)
        self.points.SetVisibility(False)
        self.preview = line_actor(3)
        self.grid = line_actor(1, tubes=False)
        self.grid.GetProperty().SetOpacity(0.35)
        self.triad = line_actor(3)
        self.snap = vtk.vtkActor()
        self.snap.GetProperty().SetPointSize(18)
        self.snap.GetProperty().SetRenderPointsAsSpheres(True)
        self.snap.GetProperty().SetColor(*theme.VIEW_SNAP)
        self.snap.GetProperty().SetOpacity(0.6)
        self.snap.SetVisibility(False)
        self.preview_text = vtk.vtkBillboardTextActor3D()
        self.preview_text.GetTextProperty().SetFontSize(15)
        self.preview_text.GetTextProperty().SetBold(True)
        self.preview_text.GetTextProperty().SetColor(*COL_SEL)
        self.preview_text.SetVisibility(False)
        self._actors = [self.grid, self.triad, self.lines, self.points, self.preview, self.snap, self.preview_text]

    # ------------------------------------------------------ lifecycle
    def activate(self, geom, body=None, pose=None):
        """pose: the owning object's placement — everything is drawn and picked in object coordinates."""
        import numpy as np
        from ..core.assembly import pose_matrix
        self.M = pose_matrix(pose or [0.0] * 6)
        self.Minv = np.linalg.inv(self.M)
        self.geom = s3.normalize(geom)
        self.active = True
        self.sel.clear()
        self.pending.clear()
        self.chain_start = None
        self._undo.clear()
        self._redo.clear()
        self.tool = "line" if not self.geom["curves"] else "select"
        self.plane = "XY"
        self.lock = None
        self.anchor = [0.0, 0.0, 0.0]
        self._ref_pts = []
        self._body = body
        if body is not None:
            try:
                self._ref_pts = [(v.X, v.Y, v.Z) for v in body.vertices()]
            except Exception:  # noqa: BLE001
                pass
        from .viewport import vtk_matrix
        um = vtk_matrix(pose or [0.0] * 6)
        for a in self._actors:
            if a is not self.preview_text:
                a.SetUserMatrix(um)
            self.ren.AddActor(a)
        it = self.iren
        for evt, fn in (("LeftButtonPressEvent", self._on_left_down), ("LeftButtonReleaseEvent", self._on_left_up),
                        ("MiddleButtonPressEvent", self._on_mid_down), ("MiddleButtonReleaseEvent", self._on_mid_up),
                        ("RightButtonPressEvent", self._on_right), ("RightButtonReleaseEvent", self._swallow),
                        ("MouseMoveEvent", self._on_move), ("KeyPressEvent", self._swallow),
                        ("CharEvent", self._swallow)):
            self._tags.append(it.AddObserver(evt, fn, 10.0))
        self.vp.vtk.installEventFilter(self)
        self.vp.sketch_mode = True
        self._solve()
        self._redraw()

    def deactivate(self):
        self.active = False
        for t in self._tags:
            self.iren.RemoveObserver(t)
        self._tags.clear()
        self.vp.vtk.removeEventFilter(self)
        self.vp.sketch_mode = False
        for a in self._actors:
            self.ren.RemoveActor(a)
        self._clear_labels()
        self.vp.render()

    def geometry(self):
        return copy.deepcopy(self.geom)

    # -------------------------------------------------------- solving
    def _solve(self):
        try:
            self.result = s3.solve(self.geom, analyze=True)
            if self.result.ok:
                self.geom = self.result.geometry
        except Sketch3DError as e:
            self.result = s3.Solve3DResult(self.geom, False, -1, "failed")
            self.message.emit(str(e))
        self.changed.emit()

    def commit(self, g, what="That", quiet=False) -> bool:
        try:
            res = s3.solve(g, analyze=True)
        except Sketch3DError as e:
            res, err = None, e
        drop = getattr(self, "_snap_cons", [])
        if (res is None or not res.ok) and drop:          # snap relations are optional: retry without them
            g["constraints"] = [c for c in g["constraints"] if c not in drop]
            try:
                res = s3.solve(g, analyze=True)
            except Sketch3DError as e:
                res, err = None, e
        self._snap_cons = []
        if res is None:
            self.message.emit(str(err))
            return False
        if not res.ok:
            if not quiet:
                self.message.emit(f"{what} would over-define the 3D sketch — not applied.")
            return False
        self._undo.append(json.dumps(self.geom))
        self._redo.clear()
        self.geom, self.result = res.geometry, res
        if not quiet:
            self.message.emit("")
        self.changed.emit()
        self._redraw()
        return True

    def undo(self):
        if self._undo:
            self._redo.append(json.dumps(self.geom))
            self.geom = json.loads(self._undo.pop())
            self.sel.clear()
            self._solve()
            self._redraw()

    def redo(self):
        if self._redo:
            self._undo.append(json.dumps(self.geom))
            self.geom = json.loads(self._redo.pop())
            self.sel.clear()
            self._solve()
            self._redraw()

    # ----------------------------------------------------- projection
    def _ray(self, x, y):
        """Pick ray in the object's own coordinates."""
        r = self.ren
        r.SetDisplayPoint(x, y, 0.0)
        r.DisplayToWorld()
        p0 = r.GetWorldPoint()
        r.SetDisplayPoint(x, y, 1.0)
        r.DisplayToWorld()
        p1 = r.GetWorldPoint()
        a = [p0[i] / p0[3] for i in range(3)]
        b = [p1[i] / p1[3] for i in range(3)]
        a, b = self._local(a), self._local(b)
        return a, _sub(b, a)

    def _local(self, w):
        M = getattr(self, "Minv", None)
        if M is None:
            return list(w)
        return [float(M[i][0] * w[0] + M[i][1] * w[1] + M[i][2] * w[2] + M[i][3]) for i in range(3)]

    def _world(self, l):
        M = getattr(self, "M", None)
        if M is None:
            return list(l)
        return [float(M[i][0] * l[0] + M[i][1] * l[1] + M[i][2] * l[2] + M[i][3]) for i in range(3)]

    def _to_display(self, xyz):
        r = self.ren
        xyz = self._world(xyz)
        r.SetWorldPoint(xyz[0], xyz[1], xyz[2], 1.0)
        r.WorldToDisplay()
        d = r.GetDisplayPoint()
        return d[0], d[1]

    def _step(self):
        """Grid step from the settings, else ≈ world size of 8 px at the anchor (1-2-5 series)."""
        g = float(self._settings().get("grid", 0) or 0)
        if g > 0:
            return g
        a = self._to_display(self.anchor)
        p0, _ = self._ray(a[0], a[1])
        p1, _ = self._ray(a[0] + 8, a[1])
        w = max(math.dist(p0, p1), 1e-6)
        e = 10 ** math.floor(math.log10(w))
        for m in (1, 2, 5, 10):
            if e * m >= w:
                return e * m
        return e * 10

    def _place(self, x, y, through=None):
        """World point under the mouse on the active plane (or locked axis) through `through`."""
        o, d = self._ray(x, y)
        A = through or self.anchor
        if self.lock:
            ax = list(s3.AXES[self.lock])
            # closest point on axis line A + s*ax to the ray o + t*d
            w0 = _sub(A, o)
            a, b, c = _dot(ax, ax), _dot(ax, d), _dot(d, d)
            dd, e = _dot(ax, w0), _dot(d, w0)
            den = a * c - b * b
            sv = (b * e - c * dd) / den if abs(den) > 1e-12 else 0.0
            P = _add(A, _mul(ax, sv))
        else:
            n = list(PLANES[self.plane])
            den = _dot(n, d)
            if abs(den) < 1e-9:
                cam = self.ren.GetActiveCamera()
                dw = list(cam.GetDirectionOfProjection())
                R = getattr(self, "Minv", None)
                n = [sum(R[i][j] * dw[j] for j in range(3)) for i in range(3)] if R is not None else dw
                den = _dot(n, d)
            t = _dot(n, _sub(A, o)) / den
            P = _add(o, _mul(d, t))
        st = self._step()
        return [round(v / st) * st if abs(v - A[i]) > 1e-9 else v for i, v in enumerate(P)]

    def _snap_point(self, x, y, exclude=()):
        best, bd = None, SNAP_PX
        for pid, xyz in self.geom["points"].items():
            if pid in exclude:
                continue
            dx, dy = self._to_display(xyz)
            dist = math.hypot(dx - x, dy - y)
            if dist < bd:
                best, bd = pid, dist
        return best

    def _snap_ref_old(self, x, y):
        """Body snaps: vertices, edge midpoints, hole/arc centres, face centres, points on edges."""
        body = getattr(self, "_body", None)
        if body is None:
            return None
        from ..core import snap3d
        try:
            hit = snap3d.find([("body", snap3d.candidates(body), getattr(self, "M", None))],
                              snap3d.vtk_projector(self.ren), x, y, SNAP_PX)
        except Exception:  # noqa: BLE001
            return None
        self.snap_kind = hit["kind"] if hit else None
        return hit["local"] if hit else None

    def _hit(self, x, y):
        for xyz, hid, _a, off in self._labels:
            dx, dy = self._to_display(xyz)
            if abs(dx + 8 - x) < 22 and abs(dy + off + 8 - y) < 11:
                return hid
        pid = self._snap_point(x, y)
        if pid and pid != ORIGIN:
            return pid
        best, bd = None, 7.0
        edges = s3.edges_for(self.geom, construction=True)
        for cid, e in edges.items():
            n = 2 if e.geom_type.name == "LINE" else 40
            pts = [self._to_display(tuple(p)) for p in e.positions([i / (n - 1) for i in range(n)])]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                vx, vy = bx - ax, by - ay
                L2 = vx * vx + vy * vy or 1e-9
                t = max(0, min(1, ((x - ax) * vx + (y - ay) * vy) / L2))
                dist = math.hypot(x - (ax + t * vx), y - (ay + t * vy))
                if dist < bd:
                    best, bd = cid, dist
        return best

    # ---------------------------------------------------------- events
    def _abort(self, obj, evt):
        cmd = obj.GetCommand(self._tags[[e for e in ("LeftButtonPressEvent", "LeftButtonReleaseEvent",
                                                      "MiddleButtonPressEvent", "MiddleButtonReleaseEvent",
                                                      "RightButtonPressEvent", "RightButtonReleaseEvent",
                                                      "MouseMoveEvent", "KeyPressEvent", "CharEvent")].index(evt)])
        cmd.SetAbortFlag(1)

    def _swallow(self, obj, evt):
        self._abort(obj, evt)

    def _on_left_down(self, obj, evt):
        self._abort(obj, evt)
        x, y = obj.GetEventPosition()
        self._press = (x, y)
        if obj.GetRepeatCount():                       # double click
            hid = self._hit(x, y)
            if hid and hid.startswith("k:"):
                self.edit_dimension(int(hid[2:]))
            elif self.tool == "spline":
                self.finish_spline()
            return
        if self.tool == "select":
            hid = self._hit(x, y)
            additive = obj.GetShiftKey() or obj.GetControlKey()
            if hid:
                if additive:
                    self.sel ^= {hid}
                elif hid not in self.sel:
                    self.sel = {hid}
                if hid in self.geom["points"] and hid != ORIGIN:
                    self._drag = {"pid": hid, "before": json.dumps(self.geom),
                                  "origin": list(self.geom["points"][hid]), "moved": False}
            elif not additive:
                self.sel.clear()
            self.changed.emit()
            self._redraw()
            return
        self._update_cursor(x, y)
        self._click_tool(x, y)

    def _on_left_up(self, obj, evt):
        self._abort(obj, evt)
        if self._drag:
            d, self._drag = self._drag, None
            if d["moved"]:
                g = copy.deepcopy(self.geom)
                if self.snap_id and self.snap_id != d["pid"]:
                    s3.merge_points(g, self.snap_id, d["pid"])
                    self.sel.clear()
                res = s3.solve(g, analyze=True)
                if res.ok:
                    self._undo.append(d["before"])
                    self._redo.clear()
                    self.geom, self.result = res.geometry, res
                else:
                    self.geom = json.loads(d["before"])
                    self._solve()
                    self.message.emit("That would over-define the 3D sketch — not applied.")
                self.changed.emit()
            self._redraw()

    def _on_mid_down(self, obj, evt):
        self._abort(obj, evt)
        style = obj.GetInteractorStyle()
        if obj.GetShiftKey() or obj.GetControlKey():
            self._mid = "pan"
            style.OnMiddleButtonDown()
        else:
            self._mid = "rotate"
            style.OnLeftButtonDown()

    def _on_mid_up(self, obj, evt):
        self._abort(obj, evt)
        style = obj.GetInteractorStyle()
        if self._mid == "pan":
            style.OnMiddleButtonUp()
        elif self._mid == "rotate":
            style.OnLeftButtonUp()
        self._mid = None
        self._redraw()

    def _on_right(self, obj, evt):
        self._abort(obj, evt)
        if self.tool == "spline" and len(self.pending) >= 2:
            self.finish_spline()
        elif self.pending:
            self.pending.clear()
            self.chain_start = None
        else:
            self.set_tool("select")
        self._redraw()

    def _on_move(self, obj, evt):
        if self._mid:                 # let the style rotate/pan
            return
        self._abort(obj, evt)
        x, y = obj.GetEventPosition()
        if self._drag:
            through = self._drag["origin"]
            P = self._place(x, y, through=through)
            self.snap_id = self._snap_point(x, y, exclude={self._drag["pid"]})
            if self.snap_id:
                P = list(self.geom["points"][self.snap_id])
            base = json.loads(self._drag["before"])
            try:
                res = s3.solve(base, drag={self._drag["pid"]: P})
                if res.ok:
                    self.geom, self.result = res.geometry, res
                    self._drag["moved"] = True
            except Sketch3DError:
                pass
            self._redraw()
            return
        if self.tool == "select":
            h = self._hit(x, y)
            if h != self.hover:
                self.hover = h
                self._redraw()
            return
        self._update_cursor(x, y)
        self._redraw_preview()

    def _update_cursor(self, x, y):
        self.snap_hit = None
        self.snap_note = ""
        st = self._settings()
        self.snap_id = self._snap_point(x, y) if st.get("enabled", True) and st.get("endpoint", True) else None
        if self.snap_id:
            self.cursor = list(self.geom["points"][self.snap_id])
            self.snap_note = "endpoint"
            return
        hit = self._snap3d(x, y) if st.get("enabled", True) and not self.lock else None
        if hit is not None:
            self.snap_hit = hit
            self.cursor = list(hit["local"])
            self.snap_note = hit["kind"] + (f" {hit['refs']['axis']}" if hit.get("refs", {}).get("axis") else "")
            return
        P = self._place(x, y)
        if self.pending and st.get("enabled", True) and (st.get("length_step") or st.get("angle_step")):
            n = None if self.lock else list(PLANES[self.plane])
            P, note = snap3d_increments(self.pending[-1][1], P, st.get("length_step", 0.0),
                                        st.get("angle_step", 0.0), n)
            self.snap_note = note or ""
        self.cursor = P

    # Qt-level keys (Tab must be caught before Qt moves focus)
    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.KeyPress or not self.active:
            return False
        k, mods = ev.key(), ev.modifiers()
        if mods & Qt.ControlModifier:
            if k == Qt.Key_Z:
                self.undo()
                return True
            if k == Qt.Key_Y:
                self.redo()
                return True
            return False
        if k == Qt.Key_Tab or k == Qt.Key_Backtab:
            self.cycle_plane()
        elif k in (Qt.Key_X, Qt.Key_Y, Qt.Key_Z):
            ax = {Qt.Key_X: "x", Qt.Key_Y: "y", Qt.Key_Z: "z"}[k]
            if self.tool == "select" and self.sel:
                self.apply_relation(f"along_{ax}")
            else:
                self.set_lock(None if self.lock == ax else ax)
        elif k == Qt.Key_Escape:
            if self.pending:
                self.pending.clear()
                self.chain_start = None
            elif self.tool != "select":
                self.set_tool("select")
            else:
                self.sel.clear()
            self._redraw()
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selection()
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_spline()
        elif k == Qt.Key_S:
            self.set_tool("select")
        elif k == Qt.Key_L:
            self.set_tool("line")
        elif k == Qt.Key_P:
            self.set_tool("spline")
        elif k == Qt.Key_A:
            self.set_tool("arc")
        elif k == Qt.Key_D:
            self.message.emit("Use the Dimension button with a selection")
        else:
            return False
        self.changed.emit()
        return True

    # ----------------------------------------------------------- tools
    def set_tool(self, t):
        self.tool = t
        self.pending.clear()
        self.chain_start = None
        if t != "select":
            self.sel.clear()
        self.changed.emit()
        self._redraw()

    def set_plane(self, p):
        self.plane = p
        self.lock = None
        self.changed.emit()
        self._redraw()

    def cycle_plane(self):
        self.set_plane(PLANE_ORDER[(PLANE_ORDER.index(self.plane) + 1) % 3])

    def set_lock(self, ax):
        self.lock = ax
        self.changed.emit()
        self._redraw()

    def _new_point(self, g, xyz, pid):
        if pid and pid in g["points"]:
            return pid
        nid = s3.new_id(g, "p")
        g["points"][nid] = list(xyz)
        hit = getattr(self, "_snap_at", {}).get(tuple(round(v, 7) for v in xyz))
        if hit is not None and self._settings().get("auto_relations", True):
            refs = hit.get("refs") or {}
            lines = {c["id"] for c in g["curves"] if c["type"] == "line"}
            k = None
            if hit["kind"] == "midpoint" and refs.get("line") in lines:
                k = {"type": "midpoint", "e": [nid, refs["line"]]}
            elif refs.get("on_line") in lines:
                k = {"type": "point_on", "e": [nid, refs["on_line"]]}
            elif hit["kind"] == "perpendicular" and refs.get("line") in lines:
                k = {"type": "point_on", "e": [nid, refs["line"]]}
            if k:
                g["constraints"].append(k)
                self._snap_cons = getattr(self, "_snap_cons", []) + [k]
        return nid

    # ------------------------------------------------------------ snapping
    def _settings(self):
        from .snap_settings import load
        return load()

    def _snap3d(self, x, y):
        """Body + sketch snaps and inference (settings: Snaps & inference)."""
        from ..core import snap3d
        sets = []
        body = getattr(self, "_body", None)
        M = getattr(self, "M", None)
        if body is not None:
            sets.append(("body", snap3d.candidates(body), M))
        lines = [(c["id"], c["a"], c["b"]) for c in self.geom["curves"] if c["type"] == "line"
                 and not c.get("construction") and c["a"] in self.geom["points"] and c["b"] in self.geom["points"]]
        busy = {p for p, _ in self.pending if p}
        sets.append(("sketch", snap3d.sketch_candidates(self.geom["points"], lines, exclude=busy), M))
        anchor = self._world(self.pending[-1][1]) if self.pending else None
        ray = snap3d.vtk_ray(self.ren, x, y)
        try:
            hit = snap3d.find(sets, snap3d.vtk_projector(self.ren), x, y, SNAP_PX, self._settings(),
                              anchor=anchor, ray=ray, origin=False)
        except Exception:  # noqa: BLE001
            return None
        if hit is not None and hit.get("local") is None:
            hit["local"] = self._local(hit["p"])
        return hit

    def _auto_axis(self, a, b):
        d = _sub(b, a)
        L = math.sqrt(_dot(d, d))
        if L < 1e-9:
            return None
        for i, ax in enumerate("xyz"):
            if abs(d[i]) / L > math.cos(math.radians(AUTO_DEG)):
                return ax
        return None

    def _click_tool(self, x, y):
        if self.cursor is None:
            self.cursor = self._place(x, y)
        xyz, sid = list(self.cursor), self.snap_id
        if getattr(self, "snap_hit", None) is not None and not sid:
            self._snap_at = {tuple(round(v, 7) for v in xyz): self.snap_hit}
        if self.tool == "line":
            if not self.pending:
                self.pending = [(sid, xyz)]
                self.chain_start = sid
                self.anchor = xyz
                self._redraw()
                return
            a_id, a_xyz = self.pending[-1]
            g = copy.deepcopy(self.geom)
            a = self._new_point(g, a_xyz, a_id)
            b = self._new_point(g, xyz, sid)
            if a == b:
                return
            lid = s3.new_id(g, "l")
            g["curves"].append({"id": lid, "type": "line", "a": a, "b": b})
            sh = getattr(self, "snap_hit", None)
            if sh is not None and sh["kind"] == "perpendicular" and self._settings().get("auto_relations", True):
                tl = (sh.get("refs") or {}).get("line")
                if tl and any(c["id"] == tl for c in g["curves"]):
                    k = {"type": "perpendicular", "e": [lid, tl]}
                    g["constraints"].append(k)
                    self._snap_cons = getattr(self, "_snap_cons", []) + [k]
            ax = self.lock or self._auto_axis(a_xyz, xyz)
            ok = False
            if ax:
                g2 = copy.deepcopy(g)
                g2["constraints"].append({"type": f"along_{ax}", "e": [lid]})
                ok = self.commit(g2, quiet=True)
            if not ok and not self.commit(g, what="Line"):
                return
            if self.chain_start is None:
                self.chain_start = a
            closing = sid is not None and sid == self.chain_start and len(self.pending) >= 1 and a != sid
            if closing:
                self.pending.clear()
                self.chain_start = None
            else:
                self.pending = [(b, list(self.geom["points"][b]))]
                self.anchor = list(self.geom["points"][b])
            self.lock = None
        elif self.tool == "spline":
            self.pending.append((sid, xyz))
            self.anchor = xyz
        elif self.tool == "arc":
            self.pending.append((sid, xyz))
            self.anchor = xyz
            if len(self.pending) == 3:
                g = copy.deepcopy(self.geom)
                ids = [self._new_point(g, p, i) for i, p in self.pending]
                if len(set(ids)) == 3:
                    g["curves"].append({"id": s3.new_id(g, "a"), "type": "arc", "a": ids[0], "m": ids[1], "b": ids[2]})
                    self.commit(g, what="Arc")
                self.pending.clear()
        self.changed.emit()
        self._redraw()

    def finish_spline(self):
        if self.tool != "spline" or len(self.pending) < 2:
            return
        g = copy.deepcopy(self.geom)
        ids = [self._new_point(g, p, i) for i, p in self.pending]
        if len(set(ids)) >= 2:
            g["curves"].append({"id": s3.new_id(g, "s"), "type": "spline", "pts": ids})
            self.commit(g, what="Spline")
        self.pending.clear()
        self._redraw()

    # ------------------------------------------------------- relations
    def _kinds(self):
        sel = sorted(x for x in self.sel if not x.startswith("k:"))
        return sel, [s3.kind(self.geom, x) for x in sel]

    def available(self) -> set[str]:
        sel, ks = self._kinds()
        n_l, n_p = ks.count("line"), ks.count("point")
        out = set()
        if (n_l and n_l == len(ks)) or (n_p == 2 and len(ks) == 2):
            out |= {"along_x", "along_y", "along_z"}
        if n_l == 2 and len(ks) == 2:
            out |= {"perpendicular"}
        if n_l >= 2 and n_l == len(ks):
            out.add("equal")
        if n_p == 2 and len(ks) == 2:
            out.add("coincident")
        if n_p == 1 and n_l == 1 and len(ks) == 2:
            out |= {"point_on", "midpoint"}
        if n_p and n_p == len(ks):
            out.add("fix")
        if any(k != "point" for k in ks):
            out.add("construction")
        if sel and (n_l == 1 and len(ks) == 1 or len(ks) == 2 and (n_p == 2 or (n_p == 1 and n_l == 1) or n_l == 2)):
            out.add("dimension")
        if self.sel:
            out.add("delete")
        return out

    def apply_relation(self, rel):
        sel, ks = self._kinds()
        if rel not in self.available():
            return
        g = copy.deepcopy(self.geom)
        pts = [x for x, k in zip(sel, ks) if k == "point"]
        lines = [x for x, k in zip(sel, ks) if k == "line"]
        cons = g["constraints"]
        if rel.startswith("along_"):
            cons += [{"type": rel, "e": [l]} for l in lines] if lines else [{"type": rel, "e": pts}]
        elif rel == "perpendicular":
            cons.append({"type": rel, "e": lines})
        elif rel == "equal":
            cons += [{"type": "equal", "e": [lines[0], l]} for l in lines[1:]]
        elif rel == "coincident":
            s3.merge_points(g, pts[0], pts[1])
        elif rel in ("point_on", "midpoint"):
            cons.append({"type": rel, "e": [pts[0], lines[0]]})
        elif rel == "fix":
            cons += [{"type": "fix", "e": [p]} for p in pts if p != ORIGIN]
        elif rel == "construction":
            for c in g["curves"]:
                if c["id"] in sel:
                    c["construction"] = not c.get("construction", False)
        elif rel == "delete":
            return self.delete_selection()
        if self.commit(g, what=rel.replace("_", " ").title()):
            self.sel.clear()
            self._redraw()

    def dimension_options(self):
        sel, ks = self._kinds()
        if ks == ["line"]:
            return [("length", sel), ("dx", list(self._ends(sel[0]))), ("dy", list(self._ends(sel[0]))),
                    ("dz", list(self._ends(sel[0])))]
        if ks == ["point", "point"]:
            return [("distance", sel), ("dx", sel), ("dy", sel), ("dz", sel)]
        if sorted(ks) == ["line", "point"]:
            p = sel[ks.index("point")]
            l = sel[ks.index("line")]
            return [("distance", [p, l])]
        if ks == ["line", "line"]:
            return [("angle", sel)]
        return []

    def _ends(self, lid):
        c = s3.curve(self.geom, lid)
        return c["a"], c["b"]

    def measure(self, t, e):
        P = self.geom["points"]
        if t == "length":
            return math.dist(*[P[p] for p in self._ends(e[0])])
        if t in ("dx", "dy", "dz"):
            i = "xyz".index(t[1])
            return abs(P[e[0]][i] - P[e[1]][i])
        if t == "distance":
            if e[1] in P:
                return math.dist(P[e[0]], P[e[1]])
            return s3.residual(self.geom, {"type": "point_on", "e": e})
        if t == "angle":
            r = s3.residual(self.geom, {"type": "angle", "e": e, "value": 0})
            return r
        return 0.0

    def add_dimension(self, t, e, value=None, parent=None):
        if value is None:
            label = {"length": "Length", "distance": "Distance", "dx": "ΔX", "dy": "ΔY", "dz": "ΔZ",
                     "angle": "Angle"}[t]
            from .units_ui import ask_quantity
            value = ask_quantity(parent, "Dimension", label, self.measure(t, e), "angle" if t == "angle" else "length",
                                 0.0)
            if value is None:
                return False
        g = copy.deepcopy(self.geom)
        g["constraints"].append({"type": t, "e": list(e), "value": float(value)})
        ok = self.commit(g, what="That dimension")
        if ok:
            self.sel.clear()
            self._redraw()
        return ok

    def edit_dimension(self, i):
        k = self.geom["constraints"][i]
        if k["type"] not in s3.DIMENSIONS:
            return
        from .units_ui import ask_quantity
        v = ask_quantity(None, "Edit dimension", k["type"], float(k["value"]),
                         "angle" if k["type"] == "angle" else "length", 0.0)
        if v is not None:
            g = copy.deepcopy(self.geom)
            g["constraints"][i]["value"] = float(v)
            self.commit(g, what="That value")

    def delete_selection(self):
        if not self.sel:
            return
        g = copy.deepcopy(self.geom)
        for i in sorted((int(x[2:]) for x in self.sel if x.startswith("k:")), reverse=True):
            if i < len(g["constraints"]):
                g["constraints"].pop(i)
        s3.remove_entities(g, {x for x in self.sel if not x.startswith("k:")})
        self.sel.clear()
        if not self.commit(g, quiet=True):
            self._undo.append(json.dumps(self.geom))
            self.geom = s3.normalize(g)
            self._solve()
        self._redraw()

    # ---------------------------------------------------------- drawing
    def _clear_labels(self):
        for _xyz, _hid, a, _off in self._labels:
            self.ren.RemoveActor(a)
        self._labels = []

    def _label(self, xyz, text, hid, color, slot=0):
        a = vtk.vtkBillboardTextActor3D()
        a.SetPosition(*self._world(xyz))
        a.SetInput(text)
        tp = a.GetTextProperty()
        tp.SetFontSize(15)
        tp.SetBold(True)
        tp.SetColor(*(COL_SEL if hid in self.sel else COL_HOVER if hid == self.hover else color))
        tp.SetBackgroundColor(0.10, 0.11, 0.13)
        tp.SetBackgroundOpacity(0.75)
        tp.SetFrame(False)
        dy = 10 + 22 * slot
        a.SetDisplayOffset(-12, dy)
        self.ren.AddActor(a)
        self._labels.append((xyz, hid, a, dy))

    def _scene_size(self):
        pts = list(self.geom["points"].values()) + [list(p) for p in self._ref_pts]
        if len(pts) < 2:
            return 200.0
        span = max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3))
        return max(span, 100.0)

    def _redraw(self):
        if not self.active:
            return
        res = self.result
        free = res.free_points if res and res.ok else set()
        bad = {e for i in (res.failed if res and not res.ok else []) if i < len(self.geom["constraints"])
               for e in self.geom["constraints"][i]["e"]}
        P = self.geom["points"]
        # curves
        pts, lines, cols = vtk.vtkPoints(), vtk.vtkCellArray(), vtk.vtkUnsignedCharArray()
        cols.SetNumberOfComponents(3)
        edges = s3.edges_for(self.geom, construction=True)
        for c in self.geom["curves"]:
            e = edges.get(c["id"])
            if e is None:
                continue
            n = 2 if c["type"] == "line" else 64
            ps = e.positions([i / (n - 1) for i in range(n)])
            pl = vtk.vtkPolyLine()
            pl.GetPointIds().SetNumberOfIds(len(ps))
            for i, p in enumerate(ps):
                pl.GetPointIds().SetId(i, pts.InsertNextPoint(p.X, p.Y, p.Z))
            lines.InsertNextCell(pl)
            if c["id"] in self.sel:
                col = COL_SEL
            elif c["id"] == self.hover:
                col = COL_HOVER
            elif c["id"] in bad:
                col = COL_BAD
            elif c.get("construction"):
                col = COL_CONSTR
            elif any(p in free for p in s3.curve_points(c)):
                col = COL_FREE
            else:
                col = COL_DEFINED
            cols.InsertNextTuple3(*[int(v * 255) for v in col])
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(lines)
        pd.GetCellData().SetScalars(cols)
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(pd)
        m.SetResolveCoincidentTopologyToPolygonOffset()
        self.lines.SetMapper(m)
        self.lines.SetVisibility(True)
        # points
        vp, verts, vcols = vtk.vtkPoints(), vtk.vtkCellArray(), vtk.vtkUnsignedCharArray()
        vcols.SetNumberOfComponents(3)
        for pid, xyz in P.items():
            if pid.startswith("_"):
                continue
            i = vp.InsertNextPoint(*xyz)
            verts.InsertNextCell(1)
            verts.InsertCellPoint(i)
            col = (COL_SEL if pid in self.sel else COL_HOVER if pid == self.hover else
                   (0.85, 0.51, 0.17) if pid == ORIGIN else COL_FREE if pid in free else COL_DEFINED)
            vcols.InsertNextTuple3(*[int(v * 255) for v in col])
        ppd = vtk.vtkPolyData()
        ppd.SetPoints(vp)
        ppd.SetVerts(verts)
        ppd.GetPointData().SetScalars(vcols)
        pm = vtk.vtkPolyDataMapper()
        pm.SetInputData(ppd)
        self.points.SetMapper(pm)
        self.points.SetVisibility(True)
        # labels
        self._clear_labels()
        stack: dict[str, int] = {}
        for i, k in enumerate(self.geom["constraints"]):
            hid = f"k:{i}"
            t, e = k["type"], k["e"]
            col = COL_BAD if res and not res.ok and i in res.failed else COL_DIM
            pos = self._anchor_of(k)
            if pos is None:
                continue
            key = ",".join(f"{v:.3f}" for v in pos)
            j = stack.get(key, 0)
            stack[key] = j + 1
            if t in s3.DIMENSIONS:
                pre = {"dx": "ΔX ", "dy": "ΔY ", "dz": "ΔZ ", "angle": "∠ "}.get(t, "")
                self._label(pos, pre + _fmt(k["value"], "angle" if t == "angle" else "length") + ("°" if t == "angle" else ""), hid, col, j)
            else:
                self._label(pos, GLYPH.get(t, "?"), hid, (0.78, 0.80, 0.83) if col == COL_DIM else col, j)
        self._redraw_grid()
        self._redraw_preview()

    def _anchor_of(self, k):
        P = self.geom["points"]
        e = k["e"]
        try:
            if k["type"] in ("length", "equal", "perpendicular", "angle") or \
                    (k["type"].startswith("along_") and len(e) == 1):
                a, b = self._ends(e[0])
                return [(P[a][i] + P[b][i]) / 2 for i in range(3)]
            if all(x in P for x in e[:2]) and len(e) >= 2:
                return [(P[e[0]][i] + P[e[1]][i]) / 2 for i in range(3)]
            if e[0] in P:
                return list(P[e[0]])
        except (Sketch3DError, KeyError):
            return None
        return None

    def _redraw_grid(self):
        size = self._scene_size() * 0.6
        A = self.anchor
        n = PLANES[self.plane]
        u = [1, 0, 0] if n != (1, 0, 0) else [0, 1, 0]
        v = [n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0]]
        if self.plane == "XZ":
            u, v = [1, 0, 0], [0, 0, 1]
        pts, lines = vtk.vtkPoints(), vtk.vtkCellArray()
        k = 10
        for i in range(-k, k + 1):
            f = size * i / k
            for (p0, p1) in ((_add(A, _add(_mul(u, f), _mul(v, -size))), _add(A, _add(_mul(u, f), _mul(v, size)))),
                             (_add(A, _add(_mul(v, f), _mul(u, -size))), _add(A, _add(_mul(v, f), _mul(u, size))))):
                a = pts.InsertNextPoint(*p0)
                b = pts.InsertNextPoint(*p1)
                lines.InsertNextCell(2)
                lines.InsertCellPoint(a)
                lines.InsertCellPoint(b)
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(lines)
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(pd)
        self.grid.SetMapper(m)
        tint = {"XY": AXIS_COLOR["z"], "YZ": AXIS_COLOR["x"], "XZ": AXIS_COLOR["y"]}[self.plane]
        self.grid.GetProperty().SetColor(*tint)
        self.grid.SetVisibility(self.tool != "select")
        # axis triad at the anchor
        tp, tl, tc = vtk.vtkPoints(), vtk.vtkCellArray(), vtk.vtkUnsignedCharArray()
        tc.SetNumberOfComponents(3)
        L = size * 0.25
        for ax, d in s3.AXES.items():
            a = tp.InsertNextPoint(*A)
            b = tp.InsertNextPoint(*_add(A, _mul(list(d), L)))
            tl.InsertNextCell(2)
            tl.InsertCellPoint(a)
            tl.InsertCellPoint(b)
            col = AXIS_COLOR[ax] if self.lock in (None, ax) else (0.4, 0.4, 0.45)
            tc.InsertNextTuple3(*[int(c * 255) for c in col])
        tpd = vtk.vtkPolyData()
        tpd.SetPoints(tp)
        tpd.SetLines(tl)
        tpd.GetCellData().SetScalars(tc)
        tm = vtk.vtkPolyDataMapper()
        tm.SetInputData(tpd)
        self.triad.SetMapper(tm)
        self.triad.SetVisibility(self.tool != "select")

    def _redraw_preview(self):
        show = self.tool != "select" and self.cursor is not None
        self.snap.SetVisibility(bool(show and (self.snap_id or getattr(self, "snap_hit", None))))
        if show:
            sp = vtk.vtkPoints()
            sp.InsertNextPoint(*self.cursor)
            sv = vtk.vtkCellArray()
            sv.InsertNextCell(1)
            sv.InsertCellPoint(0)
            spd = vtk.vtkPolyData()
            spd.SetPoints(sp)
            spd.SetVerts(sv)
            sm = vtk.vtkPolyDataMapper()
            sm.SetInputData(spd)
            self.snap.SetMapper(sm)
        if show and self.pending:
            a = self.pending[-1][1]
            b = self.cursor
            pts, lines = vtk.vtkPoints(), vtk.vtkCellArray()
            chain = [p for _, p in self.pending] + [b] if self.tool in ("spline", "arc") else [a, b]
            pl = vtk.vtkPolyLine()
            pl.GetPointIds().SetNumberOfIds(len(chain))
            for i, p in enumerate(chain):
                pl.GetPointIds().SetId(i, pts.InsertNextPoint(*p))
            lines.InsertNextCell(pl)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(lines)
            m = vtk.vtkPolyDataMapper()
            m.SetInputData(pd)
            self.preview.SetMapper(m)
            ax = self.lock or self._auto_axis(a, b)
            self.preview.GetProperty().SetColor(*(AXIS_COLOR[ax] if ax else COL_SEL))
            self.preview.SetVisibility(True)
            L = math.dist(a, b)
            self.preview_text.SetPosition(*self._world(b))
            self.preview_text.SetInput(f"{_fmt(L)}" + (f"  ∥{ax.upper()}" if ax else "")
                                       + (f"  · {self.snap_note}" if getattr(self, "snap_note", "") else ""))
            self.preview_text.SetDisplayOffset(14, 14)
            self.preview_text.SetVisibility(True)
        else:
            self.preview.SetVisibility(False)
            note = getattr(self, "snap_note", "") if show else ""
            if note:
                self.preview_text.SetPosition(*self._world(self.cursor))
                self.preview_text.SetInput(note)
                self.preview_text.SetDisplayOffset(14, 14)
            self.preview_text.SetVisibility(bool(note))
        self.vp.render()

    def _snap_ref_at_cursor(self):
        return self.cursor is not None and any(math.dist(self.cursor, p) < 1e-9 for p in self._ref_pts)

    def status(self) -> tuple[str, tuple]:
        r = self.result
        if r is None:
            return "", COL_DEFINED
        if not r.ok:
            return f"Over-defined — {len(r.failed)} conflicting relation(s) in red", COL_BAD
        if r.dof == 0:
            return "Fully defined", (0.34, 0.67, 0.35)
        return f"Under-defined · {r.dof} DOF", COL_FREE


def snap3d_increments(a, p, length_step, angle_step, n):
    from ..core.snap3d import increments
    return increments(a, p, length_step, angle_step, n)


class Sketch3DBar(QWidget):
    """Command bar shown above the viewport while editing a 3D sketch."""
    finished = Signal(dict)
    cancelled = Signal()

    TOOLS = [("select", "↖  Select", "S"), ("line", "╱  Line", "L"), ("spline", "∿  Spline", "P"),
             ("arc", "◠  Arc", "A")]
    RELS = [("along_x", "X", "Along X"), ("along_y", "Y", "Along Y"), ("along_z", "Z", "Along Z"),
            ("perpendicular", "⊥", "Perpendicular"), ("equal", "=", "Equal length"),
            ("coincident", "●", "Merge points"), ("point_on", "○", "Point on line"),
            ("midpoint", "M", "Midpoint"), ("fix", "F", "Fix"), ("construction", "┅", "Construction"),
            ("delete", "✕", "Delete")]

    def __init__(self, ctl: Sketch3DController, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.setObjectName("sketch3dbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"#sketch3dbar {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(2)
        self.title = QLabel("3D Sketch")
        self.title.setObjectName("h1")
        h.addWidget(self.title)
        h.addSpacing(8)
        self.tool_btns = {}
        for key, label, sc in self.TOOLS:
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"{label.split()[-1]} ({sc})")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: ctl.set_tool(k))
            self.tool_btns[key] = b
            h.addWidget(b)
        h.addWidget(self._sep())
        self.plane_btns = {}
        for p in PLANE_ORDER:
            b = QToolButton()
            b.setText(p)
            b.setToolTip("Drawing plane (Tab cycles)")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, p=p: ctl.set_plane(p))
            self.plane_btns[p] = b
            h.addWidget(b)
        self.lock_btns = {}
        for ax in "xyz":
            b = QToolButton()
            b.setText(f"⇢{ax.upper()}")
            b.setToolTip(f"Lock next point along {ax.upper()} ({ax.upper()} key)")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, a=ax: ctl.set_lock(None if ctl.lock == a else a))
            self.lock_btns[ax] = b
            h.addWidget(b)
        h.addWidget(self._sep())
        self.rel_btns = {}
        for key, glyph, tip in self.RELS:
            b = QToolButton()
            b.setText(glyph)
            b.setToolTip(tip)
            b.setFixedWidth(30)
            b.clicked.connect(lambda _=False, k=key: ctl.apply_relation(k))
            self.rel_btns[key] = b
            h.addWidget(b)
        self.dim_btn = QToolButton()
        self.dim_btn.setText("↔  Dimension")
        self.dim_btn.clicked.connect(self._dimension)
        h.addWidget(self.dim_btn)
        h.addWidget(self._sep())
        snap = QToolButton()
        snap.setText("Snaps…")
        from . import icons as _icons
        if _icons.available():
            from PySide6.QtCore import QSize as _QSize
            snap.setIcon(_icons.icon("snaps"))
            snap.setIconSize(_QSize(16, 16))
            snap.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        else:
            snap.setText("🧲 Snaps…")
        snap.setToolTip("Snaps & inference: end/centre/mid/quadrant points, intersections, perpendicular, tangent, "
                        "on edge/face, extensions, along X/Y/Z, parallel, aligned, increments")
        snap.clicked.connect(self._snaps)
        h.addWidget(snap)
        h.addStretch(1)
        self.status = QLabel("")
        h.addWidget(self.status)
        h.addSpacing(8)
        for txt, fn, tip in (("↶", ctl.undo, "Undo (Ctrl+Z)"), ("↷", ctl.redo, "Redo (Ctrl+Y)")):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancelled.emit)
        done = QPushButton("✓  Finish 3D sketch")
        done.setObjectName("primary")
        done.clicked.connect(lambda: self.finished.emit(ctl.geometry()))
        h.addWidget(cancel)
        h.addWidget(done)
        ctl.changed.connect(self.sync)
        ctl.message.connect(lambda m: self.window().statusBar().showMessage(m, 5000) if m else None)

    def _snaps(self):
        from .snap_settings import SnapSettingsDialog
        SnapSettingsDialog(self).exec()

    @staticmethod
    def _sep():
        s = QLabel("│")
        s.setObjectName("muted")
        return s

    def sync(self):
        c = self.ctl
        for k, b in self.tool_btns.items():
            b.setChecked(c.tool == k)
        for p, b in self.plane_btns.items():
            b.setChecked(c.plane == p and not c.lock)
        for a, b in self.lock_btns.items():
            b.setChecked(c.lock == a)
        avail = c.available()
        for k, b in self.rel_btns.items():
            b.setEnabled(k in avail)
        self.dim_btn.setEnabled("dimension" in avail)
        txt, col = c.status()
        self.status.setText(txt)
        self.status.setStyleSheet("color: rgb(%d,%d,%d)" % tuple(int(v * 255) for v in col))

    def _dimension(self):
        opts = self.ctl.dimension_options()
        if not opts:
            return
        if len(opts) == 1:
            self.ctl.add_dimension(*opts[0], parent=self)
            return
        m = QMenu(self)
        names = {"length": "Length", "distance": "Distance", "dx": "ΔX", "dy": "ΔY", "dz": "ΔZ"}
        for t, e in opts:
            m.addAction(names.get(t, t), lambda t=t, e=e: self.ctl.add_dimension(t, e, parent=self))
        m.exec(self.dim_btn.mapToGlobal(self.dim_btn.rect().bottomLeft()))
