"""Assembly UI: Move / Mate modes in the viewport, Objects and Mates panels, Drive dialog.

Move mode  — left-drag any non-fixed object; mates carry connected objects along and stop
             motion the mates don't allow (a hinged arm swings, a slider slides).
Mate mode  — click a face on one object, then a face on another; pick the mate type
             from the menu (only the ones that fit the two faces are offered).
Both modes: middle-drag rotates the view, Shift/Ctrl+middle pans, wheel zooms, Esc/right-click exits.
"""
from __future__ import annotations

import json
import math

import numpy as np
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QSpinBox,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import assembly as asm
from ..core.features import FeatureError
from . import theme
from .layers import swatch

MATE_GLYPH = {"coincident": "◉", "distance": "↔", "parallel": "∥", "perpendicular": "⊥", "angle": "∠",
              "concentric": "◎", "hinge": "⟳", "lock": "⊡", "gear": "⚙", "rack": "⫼"}
MATE_HELP = {"coincident": "faces flush / axes collinear / point on face", "distance": "offset by a distance",
             "parallel": "same direction", "perpendicular": "at right angles", "angle": "at an angle",
             "concentric": "shared axis (can still slide/rotate)", "hinge": "shared axis — add a face mate to stop sliding",
             "lock": "no relative motion",
             "gear": "rotations coupled by a ratio (pick two axes)", "rack": "rotation drives travel (axis + face)"}


class AssemblyController(QObject):
    message = Signal(str)
    mode_changed = Signal(str)

    def __init__(self, viewport, doc, parent_widget):
        super().__init__()
        self.vp, self.doc, self.win = viewport, doc, parent_widget
        self.mode: str | None = None
        self._tags = []
        self._drag = None
        self._mid = None
        self._first = None

    # ------------------------------------------------------------ modes
    def set_mode(self, mode: str | None):
        if mode == self.mode:
            mode = None
        self._detach()
        self.mode = mode
        self._first = None
        self._pending_mate = None
        self.vp.clear_selection()
        if mode:
            it = self.vp.iren
            for evt, fn in (("LeftButtonPressEvent", self._down), ("LeftButtonReleaseEvent", self._up),
                            ("MouseMoveEvent", self._move), ("MiddleButtonPressEvent", self._mid_down),
                            ("MiddleButtonReleaseEvent", self._mid_up), ("RightButtonPressEvent", self._right)):
                self._tags.append((evt, it.AddObserver(evt, fn, 10.0)))
            self.vp.sketch_mode = True
            self.message.emit({"move": "Move: drag an object — mates carry the rest; grab near a corner/centre and "
                                       "drop it on another part's corner/centre to snap (Shift = no snap). Esc to "
                                       "finish.",
                               "mate": "Mate: click a face on one object, then a face on another.",
                               "rotate": "Rotate: drag an object to spin it about its centre — left/right turns about "
                                         "the view's up axis, up/down tips it (Shift = 15° steps on one axis). "
                                         "Esc to finish.",
                               "turntable": "Rotate on floor: drag left/right to turn an object about the vertical "
                                            "axis; it stays on the floor (Shift = 15° steps). Esc to finish.",
                               "measure": "Measure: click points, edges, faces or parts — the Measure panel "
                                          "shows every value in your units. Esc to finish."
                               }[mode])
        else:
            self.vp.sketch_mode = False
            self.message.emit("")
        self.vp.render()
        self.mode_changed.emit(mode or "")

    def _detach(self):
        for _evt, tag in self._tags:
            self.vp.iren.RemoveObserver(tag)
        self._tags.clear()
        self._clear_measure()
        for a in getattr(self, "_hover_actors", []):
            self.vp.ren.RemoveActor(a)
        self._hover_actors = []

    # ------------------------------------------------------------ snapping
    def _snap(self, x, y, only=None, skip=None, tol=12.0, anchor=None, kinds=None):
        """Snap on placed objects using the 3D snap settings (Snaps & inference)."""
        from ..core import snap3d
        from .snap_settings import load
        sets = []
        for o in self.doc.objects:
            b = self.doc.result.bodies.get(o.name)
            if b is None or not o.visible or (only and o.name != only) or (skip and o.name == skip):
                continue
            sets.append((o.name, snap3d.candidates(b, key=(o.name, id(b))), asm.pose_matrix(o.pose)))
        if not sets:
            return None
        opts = load()
        if kinds is not None:
            opts.update({k: (k in kinds and opts.get(k, True)) for k in snap3d.KINDS})
        ray = snap3d.vtk_ray(self.vp.ren, x, y)
        return snap3d.find(sets, snap3d.vtk_projector(self.vp.ren), x, y, tol, opts, anchor=anchor, ray=ray,
                           origin=only is None)

    def _label(self, x, y, text):
        from . import vtklite as vtk
        if getattr(self, "_lab", None) is None:
            self._lab = vtk.vtkTextActor()
            tp = self._lab.GetTextProperty()
            tp.SetFontSize(13)
            tp.SetColor(*theme.VIEW_HILITE)
            tp.SetBackgroundColor(0.1, 0.12, 0.14)
            tp.SetBackgroundOpacity(0.75)
            self.vp.ren.AddActor2D(self._lab) if hasattr(self.vp.ren, "AddActor2D") else self.vp.ren.AddViewProp(self._lab)
        self._lab.SetInput(text)
        self._lab.SetPosition(x + 14, y - 22)
        self._lab.SetVisibility(bool(text))

    def _marker(self, pts, color=(1.0, 0.82, 0.4)):
        from . import vtklite as vtk
        actors = []
        for p in pts:
            sp = vtk.vtkSphereSource()
            sp.SetCenter(*p)
            sp.SetRadius(max(self._scene() * 0.006, 0.05))
            m = vtk.vtkPolyDataMapper()
            m.SetInputConnection(sp.GetOutputPort())
            a = vtk.vtkActor()
            a.SetMapper(m)
            a.GetProperty().SetColor(*color)
            a.PickableOff()
            self.vp.ren.AddActor(a)
            actors.append(a)
        if len(pts) == 2:
            ln = vtk.vtkLineSource()
            ln.SetPoint1(*pts[0])
            ln.SetPoint2(*pts[1])
            m = vtk.vtkPolyDataMapper()
            m.SetInputConnection(ln.GetOutputPort())
            a = vtk.vtkActor()
            a.SetMapper(m)
            a.GetProperty().SetColor(*color)
            a.GetProperty().SetLineWidth(2.5)
            a.PickableOff()
            self.vp.ren.AddActor(a)
            actors.append(a)
        return actors

    def _scene(self):
        b = self.vp.ren.ComputeVisiblePropBounds()
        return max(b[1] - b[0], b[3] - b[2], b[5] - b[4], 1.0)

    def _clear_measure(self):
        for a in getattr(self, "_meas_actors", []):
            self.vp.ren.RemoveActor(a)
        self._meas_actors = []
        self._meas = []
        p = getattr(self.win, "measure_panel", None) if hasattr(self, "win") else None
        if p is not None and p.refs:
            p.refs = []

    def _measure_panel(self):
        return getattr(self.win, "measure_panel", None)

    def _measure_click(self, x, y):
        panel = self._measure_panel()
        mode = panel.mode if panel is not None else "points"
        if mode != "points":
            return self._measure_entity_click(x, y, panel, body=(mode == "body"))
        from ..core import units as U
        anchor = self._meas[0]["p"] if len(getattr(self, "_meas", [])) == 1 else None
        hit = self._snap(x, y, anchor=anchor)
        if hit is None:
            ph = self.vp.pick_info(x, y)
            if ph is None:
                return False
            hit = {"p": list(ph[2]), "kind": "face", "tag": ph[0]}
        if len(getattr(self, "_meas", [])) >= 2:
            self._clear_measure()
        self._meas.append(hit)
        for a in getattr(self, "_meas_actors", []):
            self.vp.ren.RemoveActor(a)
        self._meas_actors = self._marker([m["p"] for m in self._meas])
        if len(self._meas) == 2:
            a, b = (np.array(m["p"]) for m in self._meas)
            d = b - a
            self.last_measure = {"distance": float(np.linalg.norm(d)), "dx": float(d[0]), "dy": float(d[1]),
                                 "dz": float(d[2]), "from": self._meas[0], "to": self._meas[1]}
            if panel is not None:
                panel.show_points(a, b)
            self.message.emit(f"Distance {U.fmt(np.linalg.norm(d))} · ΔX {U.fmt(d[0])} · ΔY {U.fmt(d[1])} · "
                              f"ΔZ {U.fmt(d[2])}  ({self._meas[0]['kind']} of {self._meas[0]['tag']} → "
                              f"{self._meas[1]['kind']} of {self._meas[1]['tag']})")
        else:
            p = hit["p"]
            self.message.emit(f"{hit['kind']} of {hit['tag']} at ({U.fmt(p[0])}, {U.fmt(p[1])}, {U.fmt(p[2])}) — "
                              f"click the second point")
        self.vp.render()
        return True

    def _nearest_edge(self, name, local):
        from build123d import Vertex
        body = self.doc.result.bodies.get(name)
        if body is None:
            return None
        v = Vertex(*map(float, local))
        best = None
        for e in body.edges():
            try:
                d = e.distance_to(v)
            except Exception:  # noqa: BLE001
                continue
            if best is None or d < best[0]:
                best = (d, e)
        return best[1] if best else None

    def _measure_pick(self, x, y, body=False):
        """A measure ref under the cursor: snapped point › edge › face (or the whole part in body mode)."""
        if body:
            ph = self.vp.pick_info(x, y)
            return None if ph is None else {"obj": ph[0], "kind": "body", "label": ph[0], "p": list(ph[2])}
        hit = self._snap(x, y, kinds=("endpoint", "center", "midpoint", "quadrant", "origin"), tol=9.0)
        if hit is not None:
            return {"kind": "point", "p": hit["p"], "label": f"{hit['kind']}" + (f" · {hit['tag']}" if hit.get("tag")
                                                                                else "")}
        try:
            e = self.vp.entity_at(x, y, "edge")
        except Exception:  # noqa: BLE001
            e = None
        if e is not None:
            edge = self._nearest_edge(e["object"], e["local"])
            if edge is not None:
                return {"obj": e["object"], "kind": "edge", "shape": edge, "p": list(e["point"]),
                        "label": f"{edge.geom_type.name.lower()} edge · {e['object']}"}
        ph = self.vp.pick_info(x, y)
        if ph is None:
            return None
        name, fid = ph[0], ph[1]
        try:
            face = self.vp.objs[name].faces[fid]
        except (KeyError, IndexError, AttributeError):
            return None
        return {"obj": name, "kind": "face", "shape": face, "p": list(ph[2]),
                "label": f"{face.geom_type.name.lower()} face · {name}"}

    def _measure_entity_click(self, x, y, panel, body=False):
        ref = self._measure_pick(x, y, body)
        if ref is None:
            self._clear_measure()
            panel.clear()
            self.message.emit("Measure: cleared — click geometry")
            self.vp.render()
            return False
        res = panel.add_ref(ref)
        for a in getattr(self, "_meas_actors", []):
            self.vp.ren.RemoveActor(a)
        self._meas = list(panel.refs)
        marks = [r["p"] for r in panel.refs if r.get("p") is not None]
        self._meas_actors = self._marker(marks[-8:])
        if res and len(res.get("points", [])) == 2 and len(panel.refs) == 2:
            self._meas_actors += self._marker(res["points"], (0.31, 0.76, 0.97))
        if res:
            self.last_measure = {"rows": res["rows"]}
            self.message.emit(f"Measure: {panel.summary()}")
        self.vp.render()
        return True

    def _abort(self, obj, evt):
        for e, tag in self._tags:
            if e == evt:
                obj.GetCommand(tag).SetAbortFlag(1)

    # ---------------------------------------------------------- helpers
    def _ray(self, x, y):
        r = self.vp.ren
        r.SetDisplayPoint(x, y, 0.0)
        r.DisplayToWorld()
        p0 = np.array(r.GetWorldPoint())
        r.SetDisplayPoint(x, y, 1.0)
        r.DisplayToWorld()
        p1 = np.array(r.GetWorldPoint())
        a, b = p0[:3] / p0[3], p1[:3] / p1[3]
        return a, b - a

    # ----------------------------------------------------------- events
    def _down(self, obj, evt):
        x, y = obj.GetEventPosition()
        if self.mode == "measure":
            if self._measure_click(x, y):
                self._abort(obj, evt)
            return
        hit = self.vp.pick_info(x, y)
        if hit is None:
            return                                  # empty space: let the view rotate
        self._abort(obj, evt)
        name, fid, wp, lp = hit
        if self.mode in ("rotate", "turntable"):
            try:
                P = self.doc.object_local_points(name)
            except FeatureError as e:
                self.message.emit(str(e))
                return
            o = self.doc.obj(name)
            M = asm.pose_matrix(o.pose)
            W = P @ M[:3, :3].T + M[:3, 3]
            self._drag = {"obj": name, "rot": True, "x0": x, "y0": y, "pose0": list(o.pose), "P": P,
                          "pivot": (W.min(0) + W.max(0)) / 2, "zmin0": float(W[:, 2].min()),
                          "before": json.dumps(self.doc.to_dict()), "moved": False}
            self.vp.highlight(name, fid)
            return
        if self.mode == "move":
            o = self.doc.obj(name)
            if o.fixed:
                self.message.emit(f"{name} is fixed — unfix it in Objects to move it.")
                return
            n = np.array(self.vp.ren.GetActiveCamera().GetDirectionOfProjection())
            grab = None if obj.GetShiftKey() else self._snap(
                x, y, only=name, kinds=("endpoint", "center", "midpoint", "quadrant", "intersection", "face center",
                                        "center of mass", "bbox center", "on edge"))
            if grab is not None:                    # grab by a corner / centre so it can land on another one
                lp, wp = grab["local"], grab["p"]
            self._drag = {"obj": name, "local": lp, "plane_p": wp, "plane_n": n, "grab": grab, "face": fid,
                          "face_local": list(hit[3]),
                          "before": json.dumps(self.doc.to_dict()), "moved": False}
            self.vp.highlight(name, fid)
        elif self.mode == "mate":
            self._pick_for_mate(name, fid, lp)

    def _hover(self, x, y):
        """Snap preview under the cursor in Move / Measure."""
        anchor = self._meas[0]["p"] if self.mode == "measure" and len(getattr(self, "_meas", [])) == 1 and \
            self._meas[0].get("p") is not None else None
        hit = self._snap(x, y, anchor=anchor)
        if hit is None and not getattr(self, "_hover_actors", None) and not getattr(self, "_hover_had", False):
            return                                   # nothing shown before or now: no redraw
        self._hover_had = hit is not None
        for a in getattr(self, "_hover_actors", []):
            self.vp.ren.RemoveActor(a)
        self._hover_actors = []
        if hit is not None:
            self._hover_actors = self._marker([hit["p"]], (0.31, 0.76, 0.97)) + self._guides(hit)
            extra = ""
            if anchor is not None:
                from ..core import units as U
                extra = f"  {U.fmt(np.linalg.norm(np.subtract(hit['p'], anchor)))}"
            self._label(x, y, f"{hit['kind']}" + (f" · {hit['tag']}" if hit.get("tag") else "") + extra)
        else:
            self._label(x, y, "")
        self.vp.render_later()

    def _guides(self, hit):
        from . import vtklite as vtk
        out = []
        for a, b in hit.get("guides", []):
            ln = vtk.vtkLineSource()
            ln.SetPoint1(*a)
            ln.SetPoint2(*b)
            m = vtk.vtkPolyDataMapper()
            m.SetInputConnection(ln.GetOutputPort())
            act = vtk.vtkActor()
            act.SetMapper(m)
            act.GetProperty().SetColor(*theme.VIEW_HILITE)
            act.GetProperty().SetLineStipplePattern(0xF0F0)
            act.GetProperty().SetLineWidth(1.5)
            act.PickableOff()
            self.vp.ren.AddActor(act)
            out.append(act)
        return out

    def _move(self, obj, evt):
        if self._mid:
            return
        if not self._drag:
            if self.mode in ("measure", "move"):
                x, y = obj.GetEventPosition()
                self._hover(x, y)
            return
        if self._drag.get("rot"):
            self._abort(obj, evt)
            x, y = obj.GetEventPosition()
            self._rotate_drag(x, y, obj.GetShiftKey())
            return
        self._abort(obj, evt)
        x, y = obj.GetEventPosition()
        o, d = self._ray(x, y)
        target = {"ray": (o, d)}
        self._drag["drop"] = None
        if not obj.GetShiftKey():
            if self._drag.get("grab"):
                hit = self._snap(x, y, skip=self._drag["obj"],
                                 kinds=("endpoint", "center", "midpoint", "quadrant", "intersection", "origin",
                                        "face center", "center of mass", "bbox center", "on edge", "on face"))
                if hit is not None:
                    target = np.array(hit["p"])
                    self._label(x, y, f"{self._drag['grab']['kind']} → {hit['kind']} · {hit['tag'] or ''}")
                else:
                    self._label(x, y, "")
            if obj.GetAltKey():
                from .snap_settings import load
                if load().get("smart_mates", True):
                    hit = self._snap(x, y, skip=self._drag["obj"], kinds=("on face",))
                    self._drag["drop"] = hit
                    if hit is not None and hit.get("face") is not None:
                        self._label(x, y, f"SmartMate → face {hit['face']} of {hit['tag']}")
        self.doc.solve_assembly(drag=(self._drag["obj"], self._drag["local"], target))
        self._drag["moved"] = True
        self.vp.update_poses({ob.name: ob.pose for ob in self.doc.objects})

    ROT_DEG_PER_PX = 0.5

    def _rotate_drag(self, x, y, snap):
        from ..core.document import Document
        d = self._drag
        dx, dy = x - d["x0"], y - d["y0"]
        k = self.ROT_DEG_PER_PX
        if self.mode == "turntable":
            ang = dx * k
            if snap:
                ang = round(ang / 15.0) * 15.0
            R = asm.rotmat([0, 0, math.radians(ang)])
            text = f"{ang:+.1f}° about Z"
        else:
            cam = self.vp.ren.GetActiveCamera()
            up = np.array(cam.GetViewUp(), float)
            dop = np.array(cam.GetDirectionOfProjection(), float)
            up = up - dop * (up @ dop)
            up /= np.linalg.norm(up) or 1.0
            right = np.cross(dop, up)
            a_h, a_v = dx * k, -dy * k
            if snap:
                if abs(dx) >= abs(dy):
                    a_h, a_v = round(a_h / 15.0) * 15.0, 0.0
                else:
                    a_h, a_v = 0.0, round(a_v / 15.0) * 15.0
            R = asm.rotmat(right * math.radians(a_v)) @ asm.rotmat(up * math.radians(a_h))
            text = f"{a_h:+.1f}° ↔  {-a_v:+.1f}° ↕"
        pose = Document.rotated_pose(d["pose0"], R, d["pivot"])
        if self.mode == "turntable":
            M = asm.pose_matrix(pose)
            z = (d["P"] @ M[:3, :3].T + M[:3, 3])[:, 2].min()
            pose = [pose[0], pose[1], pose[2] + d["zmin0"] - float(z)] + list(pose[3:])
        o = self.doc.obj(d["obj"])
        o.pose = [float(v) for v in pose]
        if any(d["obj"] in m.objects() for m in self.doc.mates):
            self.doc.solve_assembly(keep=d["obj"])
        d["moved"] = True
        self.vp.update_poses({ob.name: ob.pose for ob in self.doc.objects})
        self._label(x, y, text)
        self.vp.render_later()

    def _up(self, obj, evt):
        if not self._drag:
            return
        self._abort(obj, evt)
        d, self._drag = self._drag, None
        self._label(0, 0, "")
        if d.get("drop") and d["drop"].get("face") is not None and obj.GetAltKey():
            if self._smart_mate(d):
                return
        if d["moved"]:
            self.doc._undo.append(d["before"])
            self.doc._redo.clear()
            self.doc.dirty = True
            self.doc.solve_assembly()
            self.doc._finish_result()
            self.doc._emit()
            res = self.doc.result.assembly
            if res is not None:
                self.message.emit(f"Assembly: {res.dof} DOF free" + ("" if res.ok else " — some mates unsatisfied"))

    def _smart_mate(self, d) -> bool:
        """Alt-drop: mate the grabbed face to the face under the cursor (coincident / concentric)."""
        drop = d["drop"]
        a_name, b_name = d["obj"], drop["tag"]
        try:
            fa = list(self.doc.result.bodies[a_name].faces())[d["face"]]
            fb = list(self.doc.result.bodies[b_name].faces())[drop["face"]]
        except (KeyError, IndexError, TypeError):
            return False
        ga, gb = fa.geom_type.name, fb.geom_type.name
        if ga == "PLANE" and gb == "PLANE":
            mtype = "coincident"
        elif ga == "CYLINDER" and gb == "CYLINDER":
            mtype = "concentric"
        else:
            self.message.emit(f"SmartMates: no mate for {ga.lower()} → {gb.lower()}")
            return False
        if d["moved"]:                     # solve the mate from where the parts were before the drag
            before = {o["name"]: o.get("pose") for o in json.loads(d["before"]).get("objects", [])}
            for o in self.doc.objects:
                if before.get(o.name) is not None:
                    o.pose = list(before[o.name])
            self.doc._undo.append(d["before"])
            self.doc._redo.clear()
        try:
            m = self.doc.add_mate(mtype, {"obj": a_name, "ref": {"face_index": d["face"], "pick": d["face_local"]}},
                                  {"obj": b_name, "ref": {"face_index": drop["face"], "pick": drop["local"]}},
                                  checkpoint=not d["moved"])
        except FeatureError as e:
            self.doc.rebuild()
            self.message.emit(f"SmartMates: {e}")
            return True
        res = self.doc.result.assembly
        self.message.emit(f"SmartMate: {m.name} ({mtype}) · assembly {res.dof if res else '?'} DOF")
        return True

    def _mid_down(self, obj, evt):
        self._abort(obj, evt)
        style = obj.GetInteractorStyle()
        if obj.GetShiftKey() or obj.GetControlKey():
            self._mid = "pan"
            style.OnMiddleButtonDown()
        else:
            self._mid = "rotate"
            style.OnLeftButtonDown()

    def _mid_up(self, obj, evt):
        self._abort(obj, evt)
        style = obj.GetInteractorStyle()
        (style.OnMiddleButtonUp if self._mid == "pan" else style.OnLeftButtonUp)()
        self._mid = None

    def _right(self, obj, evt):
        self._abort(obj, evt)
        if self._first:
            self._first = None
            self.vp.clear_selection()
            self.vp.render()
        else:
            self.set_mode(None)

    # ------------------------------------------------------------ mating
    def _pick_for_mate(self, name, fid, lp):
        pend = getattr(self, "_pending_mate", None)
        if pend is not None:                                 # collecting extra faces (width / symmetric)
            t, a, b, ra, rb, need, extra = pend
            key = need.pop(0)
            want_obj = a[0] if key == "a2" else (b[0] if key == "b2" else None)
            if want_obj and name != want_obj:
                self.message.emit(f"Pick that face on {want_obj}.")
                need.insert(0, key)
                return
            extra[key] = {"obj": name, "ref": {"face_index": fid, "pick": list(lp)}}
            if need:
                self.message.emit(self._need_msg(t, need[0], a, b))
                return
            self._pending_mate = None
            self._commit_mate(t, a, b, ra, rb, extra=extra)
            return
        if self._first is None or self._first[0] == name:
            self._first = (name, fid, lp)
            self.vp.highlight(name, fid, color=(0.31, 0.76, 0.97))
            self.vp.render()
            self.message.emit(f"Mate: {name} face {fid} picked — now click a face on another object.")
            return
        a = self._first
        self._first = None
        self.vp.clear_selection()
        try:
            ra = self.doc.ref_on(a[0], {"face_index": a[1], "pick": list(a[2])})
            rb = self.doc.ref_on(name, {"face_index": fid, "pick": list(lp)})
        except FeatureError as e:
            self.message.emit(str(e))
            return
        from .v28_ui import MATE_GLYPH as G28, MATE_HELP as H28, special_options
        special = special_options(ra, rb)
        options = [t for t, (kinds, _v) in asm.MATE_TYPES.items() if (ra["kind"], rb["kind"]) in kinds
                   and t not in ("width", "symmetric")]
        if not options and not special:
            self.message.emit(f"No mate joins a {ra['kind']} to a {rb['kind']}.")
            return
        menu = QMenu(self.win)
        menu.addSection(f"{a[0]} {ra['kind']} ↔ {name} {rb['kind']}")
        chosen = {}
        for t in options:
            act = menu.addAction(f"{MATE_GLYPH.get(t, G28.get(t, '•'))}  {t.replace('_', ' ').title()} — "
                                 f"{MATE_HELP.get(t, H28.get(t, ''))}")
            chosen[act] = t
        if special:
            menu.addSection("Advanced / mechanical")
            for t in special:
                act = menu.addAction(f"{G28.get(t, '•')}  {t.title()} — {H28.get(t, '')}")
                chosen[act] = t
        act = menu.exec(QCursor.pos())
        if act not in chosen:
            return
        t = chosen[act]
        b = (name, fid, lp)
        if t in ("width", "symmetric"):
            need = ["a2", "b2"] if t == "width" else ["c"]
            self._pending_mate = (t, a, b, ra, rb, need, {})
            self.message.emit(self._need_msg(t, need[0], a, b))
            return
        if t == "path":
            ra = {"edge_near": list(a[2])}
        elif t == "cam":
            ra = {"cam_face": a[1], "pick": list(a[2])}
        elif t == "slot":
            ra = {"slot_face": a[1]}
        self._commit_mate(t, a, b, ra, rb)

    @staticmethod
    def _need_msg(t, key, a, b):
        if key == "a2":
            return f"Width: click the opposite face on {a[0]}."
        if key == "b2":
            return f"Width: click the opposite face on {b[0]}."
        return "Symmetric: click the symmetry plane (a flat face on any part)."

    def _commit_mate(self, t, a, b, ra, rb, extra=None):
        value, value2 = 0.0, None
        if t in asm.RANGE_MATES:
            cur = self._measure("angle" if t == "limit_angle" else "distance", ra, rb, a[0], b[0])
            unit = "°" if t == "limit_angle" else "mm"
            value, ok = QInputDialog.getDouble(self.win, "Limit", f"Minimum ({unit}):", 0.0, -1e6, 1e6, 3)
            if not ok:
                return
            value2, ok = QInputDialog.getDouble(self.win, "Limit", f"Maximum ({unit}):", max(cur, value), -1e6, 1e6, 3)
            if not ok:
                return
        elif t == "screw":
            value, ok = QInputDialog.getDouble(self.win, "Screw mate", "Lead (mm per revolution):", 1.5, -1e4, 1e4, 4)
            if not ok:
                return
        elif asm.MATE_TYPES[t][1]:
            cur = self._measure(t, ra, rb, a[0], b[0])
            unit = "°" if t == "angle" else "mm"
            value, ok = QInputDialog.getDouble(self.win, f"{t.title()} mate", f"Value ({unit}):", cur, -1e6, 1e6, 3)
            if not ok:
                return
        try:
            m = self.doc.add_mate(t, {"obj": a[0], "ref": ra}, {"obj": b[0], "ref": rb}, value=value, value2=value2,
                                  extra=extra)
            res = self.doc.result.assembly
            self.message.emit(f"{m.name} added · assembly {res.dof if res else '?'} DOF")
        except FeatureError as e:
            QMessageBox.warning(self.win, "Mate", str(e))

    def _measure(self, t, ra, rb, oa, ob):
        if "kind" not in ra or "kind" not in rb or "p" not in ra or "p" not in rb:
            return 0.0
        A = asm.world_ref(ra, self.doc.obj(oa).pose)
        B = asm.world_ref(rb, self.doc.obj(ob).pose)
        if t == "angle":
            v1 = A.get("n", A.get("d"))
            v2 = B.get("n", B.get("d"))
            return round(math.degrees(math.acos(max(-1, min(1, abs(float(np.dot(v1, v2))))))), 3)
        if A["kind"] == "plane":
            return round(abs(float(np.dot(A["n"], B["p"] - A["p"]))), 3)
        if B["kind"] == "plane":
            return round(abs(float(np.dot(B["n"], A["p"] - B["p"]))), 3)
        return round(float(np.linalg.norm(B["p"] - A["p"])), 3)


# ------------------------------------------------------------------ panels

class ObjectsPanel(QWidget):
    selection_changed = Signal(list)          # object names picked in the list (Ctrl/Shift for several)

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.multi: list[str] = []            # selection shared with the 3D view
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["", "Object", "⚓", "👁", "#"])
        from . import icons
        icons.header_icons(self.tree, {2: "anchor", 3: "eye"})
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(12)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = self.tree.header()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.headerItem().setToolTip(2, "Fixed — stays put; others move around it")
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double)
        self.tree.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.tree)
        row = QHBoxLayout()
        for text, fn, tip in (("+ Object", self.add, "New empty object (becomes active)"),
                              ("Place…", self.place, "Move / rotate the selected object by numbers"),
                              ("Material…", self.material, "Material of the selected object"),
                              ("Delete", self.delete, "Delete the object, its features and mates")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

    def refresh(self):
        d = self.doc
        sig = (d.active_object, tuple((o.name, o.color, o.visible, o.fixed, o.group, o.material, o.instance_of,
                                       o.mirrored, o.suppressed) for o in d.objects),
               tuple((sa["name"], sa.get("parent"), sa.get("rigid", True)) for sa in getattr(d, "subassemblies", [])),
               len(d.features), d.material, tuple(self.multi))
        if sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        sel = self.selected()
        self.tree.blockSignals(True)
        self.tree.clear()
        counts = {o.name: 0 for o in self.doc.objects}
        for f in self.doc.features:
            counts[f.obj] = counts.get(f.obj, 0) + 1
        groups = {}
        for sa in getattr(self.doc, "subassemblies", []):
            g = QTreeWidgetItem(["", f"⧉ {sa['name']}", "", "", "rigid" if sa.get("rigid", True) else "flex"])
            g.setData(1, Qt.UserRole + 1, sa["name"])
            g.setToolTip(1, "Subassembly — " + ("moves as one" if sa.get("rigid", True) else "members move by "
                                                                                          "their mates"))
            groups[sa["name"]] = g
        for sa in getattr(self.doc, "subassemblies", []):
            par = groups.get(sa.get("parent"))
            (par.addChild(groups[sa["name"]]) if par is not None else self.tree.addTopLevelItem(groups[sa["name"]]))
            groups[sa["name"]].setExpanded(True)
        for o in self.doc.objects:
            active = o.name == self.doc.active_object
            from . import icons as _ic
            fixed_txt = ("" if _ic.available() else "⚓") if o.fixed else "·"
            it = QTreeWidgetItem(["", f"▸ {o.name}" if active else o.name, fixed_txt,
                                  "●" if o.visible else "○", str(counts.get(o.name, 0))])
            it.setIcon(0, swatch(o.color, ring=active))
            if o.fixed and _ic.available():
                it.setIcon(2, _ic.icon("anchor", theme.ACCENT_LINE))
            it.setData(1, Qt.UserRole, o.name)
            it.setToolTip(1, f"{self.doc.material_of(o.name).name}" + ("" if o.material else " (document material)"))
            f = QFont()
            f.setBold(active)
            it.setFont(1, f)
            for c in (2, 3, 4):
                it.setTextAlignment(c, Qt.AlignCenter)
                it.setForeground(c, QBrush(QColor(theme.ACCENT_LINE if (c == 2 and o.fixed) else theme.MUTED)))
            if not o.visible or getattr(o, "suppressed", False):
                it.setForeground(1, QBrush(QColor(theme.DIM)))
            if getattr(o, "instance_of", None):
                it.setText(4, "⟲" if not o.mirrored else "⇋")
                it.setToolTip(1, f"{'Mirror' if o.mirrored else 'Pattern'} instance of {o.instance_of} "
                                 f"({o.pattern}) — follows it")
            grp = groups.get(getattr(o, "group", None))
            (grp.addChild(it) if grp is not None else self.tree.addTopLevelItem(it))
            if o.name == sel:
                self.tree.setCurrentItem(it)
            it.setSelected(o.name in self.multi or (not self.multi and o.name == sel))
        self.tree.blockSignals(False)

    def set_multi(self, names):
        self.multi = list(names)
        self.tree.blockSignals(True)
        first = None
        from PySide6.QtWidgets import QTreeWidgetItemIterator
        itr = QTreeWidgetItemIterator(self.tree)
        items = []
        while itr.value():
            items.append(itr.value())
            itr += 1
        for it in items:
            on = it.data(1, Qt.UserRole) in self.multi
            it.setSelected(on)
            if on and first is None:
                first = it
        if first is not None:
            self.tree.scrollToItem(first)
        self.tree.blockSignals(False)

    def _sel_changed(self):
        names = []
        for it in self.tree.selectedItems():
            if it.data(1, Qt.UserRole):
                names.append(it.data(1, Qt.UserRole))
            elif it.data(1, Qt.UserRole + 1):                    # a subassembly row selects its members
                names += self.doc.subassembly_members(it.data(1, Qt.UserRole + 1))
        self.multi = names
        self.selection_changed.emit(names)

    def selected(self):
        it = self.tree.currentItem()
        return it.data(1, Qt.UserRole) if it else None

    def _try(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "Objects", str(e))

    def _clicked(self, it, col):
        name = it.data(1, Qt.UserRole)
        if not name:
            sub = it.data(1, Qt.UserRole + 1)
            if sub and col == 4:                                 # toggle rigid / flexible
                self._try(self.doc.set_subassembly, sub, rigid=not self.doc.subassembly(sub).get("rigid", True))
            return
        o = self.doc.obj(name)
        if col == 2:
            self._try(self.doc.set_object, name, fixed=not o.fixed)
        elif col == 3:
            self._try(self.doc.set_object, name, visible=not o.visible)
        elif col == 0:
            c = QColorDialog.getColor(QColor(o.color), self, f"Colour for {name}")
            if c.isValid():
                self._try(self.doc.set_object, name, color=c.name())
        elif self.doc.active_object != name:
            self.doc.active_object = name
            self.doc._emit()

    def _double(self, it, col):
        if not it.data(1, Qt.UserRole):
            return
        if col != 1:
            return
        name = it.data(1, Qt.UserRole)
        new, ok = QInputDialog.getText(self, "Rename object", "Name:", text=name)
        if ok and new and new != name:
            self._try(self.doc.set_object, name, new_name=new)

    def add(self):
        self._try(self.doc.add_object)

    def material(self):
        from .materials_ui import MaterialDialog
        MaterialDialog(self.doc, self.selected() or self.doc.active_object, self).exec()

    def delete(self):
        name = self.selected()
        if name and QMessageBox.question(self, "Delete object",
                                         f"Delete {name} with its features and mates?") == QMessageBox.Yes:
            self._try(self.doc.delete_object, name)

    def place(self):
        name = self.selected() or self.doc.active_object
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Place {name}")
        form = QFormLayout(dlg)
        boxes = []
        for label in ("Move X", "Move Y", "Move Z"):
            b = QDoubleSpinBox()
            b.setRange(-1e6, 1e6)
            b.setDecimals(3)
            b.setSuffix(" mm")
            form.addRow(label, b)
            boxes.append(b)
        axis = QComboBox()
        axis.addItems(["Z", "X", "Y"])
        ang = QDoubleSpinBox()
        ang.setRange(-360, 360)
        ang.setDecimals(2)
        ang.setSuffix(" °")
        form.addRow("Rotate about", axis)
        form.addRow("Angle", ang)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            ax = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[axis.currentText()]
            o = self.doc.obj(name)
            about = o.pose[:3]
            self._try(self.doc.place_object, name, translate=[b.value() for b in boxes], axis=ax,
                      angle_deg=ang.value(), about=about)


class MatesPanel(QWidget):
    drive_requested = Signal(str)

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        lay.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Mate", "Between", "Value", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = self.tree.header()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._edit_value)
        lay.addWidget(self.tree)
        row = QHBoxLayout()
        for text, fn, tip in (("Flip", self.flip, "Flip the mate's alignment"),
                              ("Suppress", self.suppress, "Turn the mate off/on"),
                              ("Drive…", self.drive, "Animate a distance/angle mate through a range"),
                              ("Delete", self.delete, "Delete the mate")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

    def refresh(self):
        sel = self.selected()
        res = self.doc.result.assembly if self.doc.result else None
        if self.doc.mates:
            self.status.setText((f"Assembly: {res.dof} DOF free" if res else "Assembly: not solved") +
                                ("" if (res is None or res.ok) else " · unsatisfied mates in red"))
        else:
            self.status.setText("No mates yet — Assembly ▸ Mate, then click two faces.")
        self.tree.blockSignals(True)
        self.tree.clear()
        for m in self.doc.mates:
            val = ""
            if asm.MATE_TYPES[m.type][1]:
                val = f"{m.value:g}{'°' if m.type == 'angle' else ' mm'}"
            bad = res is not None and m.name in res.failed
            it = QTreeWidgetItem([f"{MATE_GLYPH.get(m.type, '•')} {m.name}", f"{m.a['obj']} ↔ {m.b['obj']}",
                                  val, "✗" if bad else ("–" if m.suppressed else "✓")])
            it.setData(0, Qt.UserRole, m.name)
            col = QColor(theme.ERROR) if bad else QColor(theme.DIM) if m.suppressed else QColor(theme.TEXT)
            for c in range(3):
                it.setForeground(c, QBrush(col))
            it.setForeground(3, QBrush(QColor(theme.ERROR if bad else theme.OK)))
            if res is not None and m.name in res.residuals:
                it.setToolTip(0, f"{m.type} · residual {res.residuals[m.name]:.2e}")
            self.tree.addTopLevelItem(it)
            if m.name == sel:
                it.setSelected(True)
                self.tree.setCurrentItem(it)
        self.tree.blockSignals(False)

    def selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _try(self, fn, *a, **k):
        try:
            fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "Mates", str(e))

    def _edit_value(self, it, _col):
        m = self.doc.mate(it.data(0, Qt.UserRole))
        if not asm.MATE_TYPES[m.type][1]:
            return
        v, ok = QInputDialog.getDouble(self, m.name, "Value:", m.value, -1e6, 1e6, 3)
        if ok:
            self._try(self.doc.set_mate, m.name, value=v)

    def flip(self):
        n = self.selected()
        if n:
            self._try(self.doc.set_mate, n, flip=not self.doc.mate(n).flip)

    def suppress(self):
        n = self.selected()
        if n:
            self._try(self.doc.set_mate, n, suppressed=not self.doc.mate(n).suppressed)

    def delete(self):
        n = self.selected()
        if n:
            self._try(self.doc.delete_mate, n)

    def drive(self):
        self.drive_requested.emit(self.selected() or "")


class DriveDialog(QDialog):
    """Motion study: sweep a distance/angle mate and play the motion in the viewport."""

    def __init__(self, doc, viewport, mate_name="", parent=None):
        super().__init__(parent)
        self.doc, self.vp = doc, viewport
        self.setWindowTitle("Drive mate — motion")
        self.frames, self.i = [], 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        form = QFormLayout(self)
        self.mate = QComboBox()
        drivable = [m.name for m in doc.mates if asm.MATE_TYPES[m.type][1] and not m.suppressed]
        self.mate.addItems(drivable)
        if mate_name in drivable:
            self.mate.setCurrentText(mate_name)
        self.start = QDoubleSpinBox()
        self.end = QDoubleSpinBox()
        for b in (self.start, self.end):
            b.setRange(-1e6, 1e6)
            b.setDecimals(2)
        self.steps = QSpinBox()
        self.steps.setRange(2, 720)
        self.steps.setValue(48)
        self.secs = QDoubleSpinBox()
        self.secs.setRange(0.2, 60)
        self.secs.setValue(2.5)
        self.secs.setSuffix(" s")
        self.loop = QCheckBox("Loop (there and back)")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        form.addRow("Mate", self.mate)
        form.addRow("From", self.start)
        form.addRow("To", self.end)
        form.addRow("Steps", self.steps)
        form.addRow("Duration", self.secs)
        form.addRow(self.loop)
        form.addRow(self.info)
        row = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.setObjectName("primary")
        self.play_btn.clicked.connect(self.play)
        stop = QPushButton("■ Stop")
        stop.clicked.connect(self.stop)
        check = QPushButton("Interference")
        check.clicked.connect(self.interference)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (self.play_btn, stop, check, close):
            row.addWidget(b)
        form.addRow(row)
        self.mate.currentTextChanged.connect(self._defaults)
        self._defaults(self.mate.currentText())
        if not drivable:
            self.info.setText("Add a distance or angle mate to drive.")
            self.play_btn.setEnabled(False)

    def _defaults(self, name):
        if not name:
            return
        m = self.doc.mate(name)
        unit = " °" if m.type == "angle" else " mm"
        for b in (self.start, self.end):
            b.setSuffix(unit)
        self.start.setValue(m.value)
        self.end.setValue(m.value + (90 if m.type == "angle" else 50))

    def play(self):
        name = self.mate.currentText()
        if not name:
            return
        self.doc.checkpoint()
        try:
            self.frames = self.doc.drive(name, self.start.value(), self.end.value(), self.steps.value())
        except FeatureError as e:
            self.info.setText(str(e))
            return
        if self.loop.isChecked():
            self.frames = self.frames + self.frames[-2:0:-1]
        bad = sum(1 for f in self.frames if not f["_ok"])
        self.info.setText(f"{len(self.frames)} frames" + (f" · {bad} couldn't satisfy every mate" if bad else ""))
        self.i = 0
        self.timer.start(max(10, int(self.secs.value() * 1000 / max(len(self.frames), 1))))

    def _tick(self):
        if self.i >= len(self.frames):
            if self.loop.isChecked():
                self.i = 0
            else:
                self.stop()
                return
        f = self.frames[self.i]
        self.vp.update_poses({k: v for k, v in f.items() if not k.startswith("_")})
        self.info.setText(f"{self.mate.currentText()} = {f['_value']:.2f}")
        self.i += 1

    def stop(self):
        self.timer.stop()
        if self.frames:
            last = self.frames[min(self.i, len(self.frames)) - 1]
            for o in self.doc.objects:
                if o.name in last:
                    o.pose = list(last[o.name])
            m = self.doc.mate(self.mate.currentText())
            m.value = last["_value"]
            self.doc.solve_assembly()
            self.doc._finish_result()
            self.doc._emit()

    def interference(self):
        hits = self.doc.interference()
        if hits:
            self.info.setText("Overlap: " + "; ".join(f"{a} × {b} {v:g} mm³" for a, b, v in hits))
        else:
            self.info.setText("No interference at this position.")

    def closeEvent(self, ev):
        self.timer.stop()
        super().closeEvent(ev)


class OrientDialog(QDialog):
    """Orient an object: absolute X/Y/Z angles, quick 90° turns, drop to floor, face down."""

    def __init__(self, doc, obj_name, last_pick=None, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.last_pick = last_pick
        self.setWindowTitle("Orient object")
        self.setModal(False)
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.obj = QComboBox()
        self.obj.addItems([o.name for o in doc.objects if doc.result.bodies.get(o.name) is not None])
        if obj_name:
            self.obj.setCurrentText(obj_name)
        self.obj.currentTextChanged.connect(lambda _t: self._load())
        form.addRow("Object", self.obj)
        self.ang = []
        row = QHBoxLayout()
        for ax in "XYZ":
            sp = QDoubleSpinBox()
            sp.setRange(-360, 360)
            sp.setDecimals(2)
            sp.setSuffix("°")
            sp.setSingleStep(15)
            sp.setToolTip(f"Rotation about world {ax} (applied X, then Y, then Z)")
            lab = QLabel(ax)
            lab.setObjectName("muted")
            row.addWidget(lab)
            row.addWidget(sp, 1)
            self.ang.append(sp)
        form.addRow("Angles", row)
        v.addLayout(form)
        apply_row = QHBoxLayout()
        b = QPushButton("Apply angles")
        b.clicked.connect(self._apply)
        apply_row.addWidget(b)
        b = QPushButton("Reset")
        b.setToolTip("Back to the part's own orientation (0, 0, 0)")
        b.clicked.connect(lambda: (self._set_spins(0, 0, 0), self._apply()))
        apply_row.addWidget(b)
        apply_row.addStretch(1)
        v.addLayout(apply_row)

        lab = QLabel("Quick turns about the object's centre")
        lab.setObjectName("muted")
        v.addWidget(lab)
        grid = QHBoxLayout()
        for i, ax in enumerate("XYZ"):
            for sgn in (-1, 1):
                q = QPushButton(f"{ax} {'+' if sgn > 0 else '−'}90°")
                axis = [0, 0, 0]
                axis[i] = 1
                q.clicked.connect(lambda _=False, a=tuple(axis), s=sgn: self._turn(a, 90 * s))
                grid.addWidget(q)
        v.addLayout(grid)
        self.on_floor = QCheckBox("Keep on the floor (rotate about the floor)")
        self.on_floor.setChecked(True)
        v.addWidget(self.on_floor)
        fl = QHBoxLayout()
        b = QPushButton("Drop to floor")
        b.setToolTip("Lower the object until its lowest point touches z = 0")
        b.clicked.connect(lambda: self._do(self.doc.drop_to_floor, self.obj.currentText()))
        fl.addWidget(b)
        self.face_btn = QPushButton("Picked face down")
        self.face_btn.setToolTip("Turn the object so the face you last clicked points down, then set it on the floor")
        self.face_btn.clicked.connect(self._face_down)
        fl.addWidget(self.face_btn)
        fl.addStretch(1)
        v.addLayout(fl)
        self.info = QLabel()
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        v.addWidget(bb)
        self._load()

    def _set_spins(self, *vals):
        for sp, val in zip(self.ang, vals):
            sp.blockSignals(True)
            sp.setValue(round(float(val), 2))
            sp.blockSignals(False)

    def _load(self):
        n = self.obj.currentText()
        if not n:
            return
        from ..core.document import Document
        self._set_spins(*Document.orientation_angles(self.doc.obj(n).pose))
        pick = self.last_pick() if callable(self.last_pick) else self.last_pick
        self.face_btn.setEnabled(bool(pick) and pick.get("object") == n)
        try:
            lo = self.doc.object_world_points(n).min(0)
            self.info.setText(f"Lowest point z = {lo[2]:.3f} mm")
        except FeatureError:
            self.info.setText("")

    def _do(self, fn, *a, **k):
        try:
            fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "Orient", str(e))
        self._load()

    def _apply(self):
        self._do(self.doc.set_orientation, self.obj.currentText(), *(sp.value() for sp in self.ang))

    def _turn(self, axis, deg):
        self._do(self.doc.rotate_object, self.obj.currentText(), axis=axis, angle_deg=deg,
                 on_floor=self.on_floor.isChecked())

    def _face_down(self):
        pick = self.last_pick() if callable(self.last_pick) else self.last_pick
        if not pick or pick.get("object") != self.obj.currentText():
            return
        self._do(self.doc.face_to_floor, pick["object"], pick["index"], pick.get("local"))
