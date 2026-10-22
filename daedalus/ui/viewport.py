"""3D viewport: VTK inside Qt.

Every object gets its own actors (faces, edges, sketches) carrying the object's
pose as a user matrix, so dragging and motion playback only update matrices.

Mouse (default "SolidWorks" scheme): click selects a part (Ctrl toggles, Shift adds) · left-drag box-selects
(left→right: parts fully inside, right→left: parts touched) · middle-drag rotates, Ctrl+middle pans,
Shift+middle zooms · right-drag pans, right-click opens the context menu · wheel zooms at the cursor ·
double-click zooms to a part · Space opens the view wheel · F zoom to fit.
"Classic" scheme: left-drag rotates, Ctrl+left-drag box-selects.
"""
from __future__ import annotations

import numpy as np
from . import vtklite as vtk
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

from ..core.assembly import pose_matrix
from . import theme
from .tessellate import body_meshes, body_polydata, edges_polydata, shape_key

VIEWS = {
    # name: (direction from focal point to camera, view-up)
    "Front": ((0, -1, 0), (0, 0, 1)), "Back": ((0, 1, 0), (0, 0, 1)),
    "Top": ((0, 0, 1), (0, 1, 0)), "Bottom": ((0, 0, -1), (0, -1, 0)),
    "Right": ((1, 0, 0), (0, 0, 1)), "Left": ((-1, 0, 0), (0, 0, 1)),
    "Isometric": ((1, -1, 1), (0, 0, 1)), "Dimetric": ((1, -2, 1.2), (0, 0, 1)),
}
DISPLAY_MODES = ["Shaded + edges", "Shaded", "Uncoloured", "Hidden line", "Wireframe", "Translucent"]
NAV_SCHEMES = {"solidworks": "SolidWorks (left selects, middle rotates)",
               "classic": "Classic (left rotates)"}
WIRE_COLOR = (0.30, 0.62, 0.95)


def _mix(a, b, t):
    return tuple(x * (1 - t) + y * t for x, y in zip(a, b))


def vtk_matrix(pose) -> vtk.vtkMatrix4x4:
    M = pose_matrix(pose)
    m = vtk.vtkMatrix4x4()
    for i in range(4):
        for j in range(4):
            m.SetElement(i, j, float(M[i, j]))
    return m


_VOL: dict = {}


def _has_volume(body) -> bool:
    """body.volume > 0, remembered per shape (identical imported parts are checked once)."""
    k = shape_key(body)
    v = _VOL.get(k)
    if v is None:
        try:
            from OCP.TopAbs import TopAbs_SOLID
            from OCP.TopExp import TopExp_Explorer
            v = TopExp_Explorer(body.wrapped, TopAbs_SOLID).More() or body.volume > 0
        except Exception:  # noqa: BLE001
            v = False
        if len(_VOL) > 5000:
            _VOL.clear()
        _VOL[k] = v
    return v


class ObjActors:
    """Actors for one object."""

    def __init__(self, ren):
        self.ren = ren
        self.body = vtk.vtkActor()
        self.edges = vtk.vtkActor()
        self.sketch = vtk.vtkActor()
        p = self.body.GetProperty()
        p.SetColor(*theme.BODY_COLOR)
        p.SetAmbient(0.18)
        p.SetDiffuse(0.75)
        p.SetSpecular(0.35)
        p.SetSpecularPower(40)
        p.SetInterpolationToPhong()
        e = self.edges.GetProperty()
        e.SetColor(*theme.EDGE_COLOR)
        e.SetLineWidth(1.4)
        e.SetLighting(False)
        s = self.sketch.GetProperty()
        s.SetColor(*theme.SKETCH_COLOR)
        s.SetLineWidth(2.0)
        s.SetLighting(False)
        for a in (self.body, self.edges, self.sketch):
            a.SetVisibility(False)
            ren.AddActor(a)
        self.edges.PickableOff()
        self.sketch.PickableOff()
        self.faces = []
        self.pd = None
        self.visible = True
        self.has_sketch = False
        self.pose = [0.0] * 6

    def remove(self):
        for a in (self.body, self.edges, self.sketch):
            self.ren.RemoveActor(a)

    def set_pose(self, pose):
        pose = list(pose)
        if pose == self.pose and getattr(self, "_posed", False):
            return
        self.pose = pose
        self._posed = True
        m = vtk_matrix(pose)
        for a in (self.body, self.edges, self.sketch):
            a.SetUserMatrix(m)


class Viewport(QWidget):
    face_picked = Signal(object)   # dict or None
    selection_changed = Signal(list)          # selected object names (click / box select)
    context_requested = Signal(object, object)  # pick dict or None, global QPoint
    view_wheel_requested = Signal()           # Space: radial view picker
    view_changed = Signal()
    entities_changed = Signal(list)           # selected faces / edges: [{object, kind, index, point, local}]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.vtk = QVTKRenderWindowInteractor(self)
        lay.addWidget(self.vtk)

        self.ren = vtk.vtkRenderer()
        self.ren.GradientBackgroundOn()
        self.ren.SetBackground(*theme.VIEW_BG_BOTTOM)
        self.ren.SetBackground2(*theme.VIEW_BG_TOP)
        rw = self.vtk.GetRenderWindow()
        rw.AddRenderer(self.ren)
        rw.SetMultiSamples(0)                  # FXAA by default: MSAA ×4 doubles the cost of every frame
        self.ren.UseFXAAOn()
        self.antialias = "fxaa"
        self.simplify_moving = True            # big models: drop edges / AA while the view moves
        self._moving = None

        self.iren = rw.GetInteractor()
        style = vtk.vtkInteractorStyleTrackballCamera()
        self.iren.SetInteractorStyle(style)
        self.iren.AddObserver("KeyPressEvent", self._on_key, 1.0)
        # navigation + selection (below editing modes, which observe at priority 10 and abort what they use)
        for evt, fn in (("LeftButtonPressEvent", self._nav_left_down), ("LeftButtonReleaseEvent", self._nav_left_up),
                        ("MiddleButtonPressEvent", self._nav_mid_down),
                        ("MiddleButtonReleaseEvent", self._nav_mid_up),
                        ("RightButtonPressEvent", self._nav_right_down),
                        ("RightButtonReleaseEvent", self._nav_right_up),
                        ("MouseMoveEvent", self._nav_move),
                        ("MouseWheelForwardEvent", self._nav_wheel), ("MouseWheelBackwardEvent", self._nav_wheel)):
            tag = self.iren.AddObserver(evt, fn, 5.0)
            self.__dict__.setdefault("_nav_tags", {})[evt] = tag
        self.iren.AddObserver("LeaveEvent", lambda *_: self._hover_set(None), 1.0)
        self.nav = "solidworks"
        self.zoom_to_cursor = True
        self.selected: list[str] = []          # selected objects, in selection order
        self.sel_mode = "part"                 # click/box selects: "part" | "face" | "edge" | "any" (face or edge)
        self.hover_highlight = True            # pre-highlight what a click would pick
        self.entities: list[dict] = []         # selected faces / edges
        self._ent_actors: list = []
        self._hover = None
        self.isolated: set[str] = set()        # view-only isolate
        self._nav_state = None                 # current drag: ("box", x0, y0) / ("rotate"|"pan"|"zoom", style-call)
        self._view_hist: list = []
        self.section = None                    # (axis 0..2, offset, flip)
        self.animate_views = True
        # VTK's built-in char keys (w/s/e/q/3/…) would toggle wireframe or even exit; disable them
        self._char_tag = self.iren.AddObserver("CharEvent", self._eat_char, 5.0)
        self._press_pos = None
        self.sketch_mode = False

        self._setup_lights()
        self._triad = self._make_triad()

        self.objs: dict[str, ObjActors] = {}
        self.select_actor = vtk.vtkActor()
        h = self.select_actor.GetProperty()
        h.SetColor(*theme.SELECT_COLOR)
        h.SetOpacity(0.85)
        h.SetAmbient(0.6)
        self.select_actor.SetVisibility(False)
        self.select_actor.PickableOff()
        self.ren.AddActor(self.select_actor)
        self.display_mode = DISPLAY_MODES[0]
        self.result_hidden: set = set()     # objects replaced by a result plot ("*" = all)
        self.show_sketches = True
        self._has_framed = False
        self.picker = vtk.vtkCellPicker()
        self.picker.SetTolerance(0.0005)
        self._loc_sig = None
        self._locators: dict = {}
        self.grid_on = True
        self._grid = None
        self._grid_key = None
        self._band = self._make_band()
        self.set_view("Isometric", fit=False, animate=False)

    # ---------------------------------------------------------- setup
    def _setup_lights(self):
        self.ren.AutomaticLightCreationOff()
        key = vtk.vtkLight()
        key.SetLightTypeToCameraLight()
        key.SetPosition(0.6, 1.0, 1.0)
        key.SetIntensity(0.85)
        fill = vtk.vtkLight()
        fill.SetLightTypeToCameraLight()
        fill.SetPosition(-1.0, -0.3, 0.6)
        fill.SetIntensity(0.35)
        self.ren.AddLight(key)
        self.ren.AddLight(fill)

    def _make_triad(self):
        axes = vtk.vtkAxesActor()
        axes.SetShaftTypeToCylinder()
        axes.SetTotalLength(1, 1, 1)
        for cap in (axes.GetXAxisCaptionActor2D(), axes.GetYAxisCaptionActor2D(), axes.GetZAxisCaptionActor2D()):
            cap.GetCaptionTextProperty().SetFontSize(14)
            cap.GetCaptionTextProperty().ShadowOff()
            cap.GetCaptionTextProperty().ItalicOff()
            cap.GetCaptionTextProperty().SetColor(*theme.VIEW_TEXT)
        w = vtk.vtkOrientationMarkerWidget()
        w.SetOrientationMarker(axes)
        w.SetInteractor(self.iren)
        w.SetViewport(0.0, 0.0, 0.14, 0.2)
        w.SetEnabled(1)
        w.InteractiveOff()
        return w

    # -------------------------------------------------------- content
    def _sketches_for(self, oa, name, result, hidden_sketches, feature_obj):
        """Sketch outlines of one object; each sketch shape is tessellated once and reused."""
        cache = self.__dict__.setdefault("_sk_cache", {})
        shown = []
        for sk_name, (shape, _plane) in result.sketches.items():
            if sk_name in hidden_sketches:
                continue
            owner = feature_obj.get(sk_name)
            if name != "_" and owner != name:
                continue
            hit = cache.get(id(shape))
            if hit is None or hit[0] is not shape:
                hit = cache[id(shape)] = (shape, edges_polydata(shape, chord=0.5))
            shown.append(hit)
        sig = tuple(id(h[0]) for h in shown)
        oa.has_sketch = bool(shown)
        if sig == getattr(oa, "_sk_sig", None):
            return
        oa._sk_sig = sig
        if shown:
            app = vtk.vtkAppendPolyData()
            for _shape, pd in shown:
                app.AddInputData(pd)
            app.Update()
            sm = vtk.vtkPolyDataMapper()
            sm.SetInputData(app.GetOutput())
            oa.sketch.SetMapper(sm)
        if len(cache) > 400:                                   # drop outlines of shapes no longer shown
            live = {id(sh) for sh, _p in result.sketches.values()}
            for k in [k for k in cache if k not in live]:
                cache.pop(k, None)

    def batch(self):
        """Context manager: render once at the end instead of on every change inside."""
        view = self

        class _B:
            def __enter__(self_):
                view._batch = getattr(view, "_batch", 0) + 1

            def __exit__(self_, *exc):
                view._batch -= 1
                if view._batch == 0 and getattr(view, "_render_pending", False):
                    view._render_pending = False
                    view.render_later()          # back-to-back redraws (edit → rebuild → select) share one frame
                return False
        return _B()

    def show_result(self, result, hidden_sketches=(), layer_style=None, objects=None):
        """objects: [(name, pose, visible, colour rgb)] — defaults to one object at the origin.
        layer_style: {layer: (rgb, visible)}. Faces on the Default layer take the object's colour
        when there are several objects."""
        bodies = dict(getattr(result, "bodies", {}) or {})
        if not bodies and result.body is not None:
            bodies = {"_": result.body}
        if objects is None:
            objects = [(n, [0.0] * 6, True, None) for n in bodies] or [("_", [0.0] * 6, True, None)]
        fl_by = getattr(result, "face_layers_by_obj", {}) or {}
        feature_obj = getattr(result, "feature_obj", {}) or {}
        multi = len(objects) > 1
        names = {o[0] for o in objects}
        for gone in set(self.objs) - names:
            self.objs.pop(gone).remove()
        from . import tessellate as from_tess
        todo = []                                  # bodies still to mesh
        self._mesh_queue = []                      # a new result replaces any unfinished progressive load
        style_sig = tuple(sorted((k, tuple(v[0]), v[1]) for k, v in (layer_style or {}).items()))
        for name, pose, visible, ocol in objects:
            oa = self.objs.get(name) or self.objs.setdefault(name, ObjActors(self.ren))
            oa.visible = visible
            body = bodies.get(name)
            same = body is not None and getattr(oa, "_body", None) is body
            fl_sig = tuple(fl_by.get(name, ())) if len(layer_style or {}) > 1 else None
            quick = (id(body), tuple(ocol) if ocol else None, style_sig, multi, fl_sig)
            if same and oa.pd is not None and getattr(oa, "_quick", None) == quick:
                oa.set_pose(pose)                                     # unchanged: nothing to re-derive
                self._sketches_for(oa, name, result, hidden_sketches, feature_obj)
                continue
            oa._quick = quick
            if body is not None and (getattr(oa, "_vol_ok", False) if same else _has_volume(body)):
                oa._vol_ok = True
                colors, hidden = None, set()
                fl = fl_by.get(name, getattr(result, "face_layers", []) if name == "_" or not multi else [])
                from ..core.document import nfaces
                nf = nfaces(body)
                if layer_style and len(fl) == nf:
                    default = next(iter(layer_style.values()))
                    colors = []
                    for ln in fl:
                        rgb, vis = layer_style.get(ln, default)
                        colors.append(ocol if (multi and ocol and ln == "Default") else rgb)
                    hidden = {i for i, ln in enumerate(fl) if not layer_style.get(ln, default)[1]}
                elif multi and ocol:
                    colors = [ocol] * nf
                uniform = None
                if colors and all(tuple(c) == tuple(colors[0]) for c in colors):
                    uniform, colors = tuple(colors[0]), None          # one colour: actor colour, shared mesh
                key = (tuple(map(tuple, colors)) if colors else None, frozenset(hidden), uniform)
                if same and getattr(oa, "_key", None) == key and oa.pd is not None:   # unchanged: keep its mesh
                    oa.set_pose(pose)
                    self._sketches_for(oa, name, result, hidden_sketches, feature_obj)
                    continue
                oa._body, oa._key = body, key
                if from_tess.is_cached(body, face_colors=colors, hidden=hidden):
                    self._attach(oa, body, colors, hidden, uniform)
                else:
                    oa.pd = None                                      # shown once meshed
                    todo.append((oa, body, colors, hidden, uniform))
            else:
                oa.faces, oa.pd, oa._body, oa._key, oa._vol_ok = [], None, None, None, False
            self._sketches_for(oa, name, result, hidden_sketches, feature_obj)
            oa.set_pose(pose)
        self.clear_selection()
        self.selected = [n for n in self.selected if n in self.objs]
        self.isolated &= set(self.objs)
        ents = [e for e in self.entities if e["object"] in self.objs]
        if ents != self.entities:
            self.entities = ents
            self.entities_changed.emit(list(ents))
        self._hover_set(None)
        progressive = len(todo) > self.PROGRESSIVE_AFTER
        if todo and not progressive:
            from_tess.premesh([t[1] for t in todo])
            for oa, body, colors, hidden, uniform in todo:
                self._attach(oa, body, colors, hidden, uniform)
            todo = []
        self._show_surfaces(result, objects)
        self.apply_display_mode()
        self._draw_entities()
        self._update_grid()
        if not self._has_framed and any(oa.pd is not None for oa in self.objs.values()):
            self.fit()
            self._has_framed = True
        self.render()
        if todo:
            self._start_progressive(todo)

    def _show_surfaces(self, result, objects):
        """Surface features (v2.8): translucent teal sheets on their object's pose."""
        surfs = getattr(result, "surfaces", {}) or {}
        old = self.__dict__.setdefault("_surf_actors", {})
        poses = {o[0]: o[1] for o in objects}
        vis = {o[0]: o[2] for o in objects}
        for k in [k for k in old if k not in surfs or old[k][0] is not surfs[k][0]]:
            self.ren.RemoveActor(old.pop(k)[1])
        for name, (shape, obj) in surfs.items():
            hit = old.get(name)
            if hit is None:
                try:
                    pd, _faces, _e = body_meshes(shape, None, None)
                except Exception:  # noqa: BLE001
                    continue
                m = vtk.vtkPolyDataMapper()
                m.SetInputData(pd)
                m.ScalarVisibilityOff()
                a = vtk.vtkActor()
                a.SetMapper(m)
                pr = a.GetProperty()
                pr.SetColor(*theme.SKETCH_COLOR)
                pr.SetOpacity(0.45)
                pr.BackfaceCullingOff()
                a.PickableOff()
                self.ren.AddActor(a)
                old[name] = hit = (shape, a, obj)
            a = hit[1]
            pose = poses.get(obj, [0.0] * 6)
            a.SetUserMatrix(vtk_matrix(pose))
            a.SetVisibility(bool(vis.get(obj, True)))

    PROGRESSIVE_AFTER = 24          # more bodies to mesh than this: show them in batches as they're ready
    loading = Signal(int, int)      # meshed, total (progressive loads)

    def _attach(self, oa, body, colors, hidden, uniform):
        pd, oa.faces, epd = body_meshes(body, None, None, face_colors=colors, hidden=hidden)
        oa.pd = pd if pd.GetNumberOfCells() else None
        oa.uniform, oa.has_colors = uniform, bool(colors)
        oa.body.GetProperty().SetColor(*(uniform or theme.BODY_COLOR))
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(pd)
        if colors:
            m.ScalarVisibilityOn()
            m.SetScalarModeToUseCellData()
            m.SetColorModeToDirectScalars()
        else:
            m.ScalarVisibilityOff()
        m.SetResolveCoincidentTopologyToPolygonOffset()
        m.SetRelativeCoincidentTopologyPolygonOffsetParameters(1.5, 1.5)
        oa.body.SetMapper(m)
        em = vtk.vtkPolyDataMapper()
        em.SetInputData(epd)
        oa.edges.SetMapper(em)
        oa.epd = epd

    def _start_progressive(self, todo):
        """Mesh big models in batches between UI events: parts appear as they are ready and the window keeps
        responding. Bodies sharing a shape (repeated parts) are meshed once."""
        from PySide6.QtCore import QTimer
        order = sorted(todo, key=lambda t: -len(t[1].faces()))       # big parts first: the model takes shape
        self._mesh_queue = order
        self._mesh_total = len(order)
        self.loading.emit(0, self._mesh_total)
        QTimer.singleShot(0, self._mesh_tick)

    def _mesh_tick(self):
        import time as _t
        from PySide6.QtCore import QTimer

        from . import tessellate as from_tess
        q = self._mesh_queue
        if not q:
            return
        t0 = _t.perf_counter()
        batch, faces = [], 0
        while q and (faces < 6000 or not batch):
            batch.append(q.pop(0))
            faces += len(batch[-1][1].faces())
        from_tess.premesh([b[1] for b in batch])
        for oa, body, colors, hidden, uniform in batch:
            if oa._body is body:                                  # still current
                self._attach(oa, body, colors, hidden, uniform)
        # attach everything else that is now cached (repeated parts) without waiting for its turn
        rest = []
        for item in q:
            oa, body, colors, hidden, uniform = item
            if from_tess.is_cached(body, face_colors=colors, hidden=hidden):
                if oa._body is body:
                    self._attach(oa, body, colors, hidden, uniform)
            else:
                rest.append(item)
        q[:] = rest
        self.apply_display_mode()
        if not self._has_framed and any(oa.pd is not None for oa in self.objs.values()):
            self.fit()
            self._has_framed = True
        done = self._mesh_total - len(q)
        if not q:
            self._update_grid()
        self.loading.emit(done, self._mesh_total)
        now = _t.perf_counter()
        if not q or now - getattr(self, "_last_prog_render", 0) > 0.6:     # don't spend the load on redraws
            self._last_prog_render = now
            self.render()
        if q:
            QTimer.singleShot(max(1, int((_t.perf_counter() - t0) * 50)), self._mesh_tick)

    def update_poses(self, poses: dict):
        """Fast path for dragging/animation: move objects without re-tessellating."""
        for name, pose in poses.items():
            oa = self.objs.get(name)
            if oa is not None:
                oa.set_pose(pose)
        if self.select_actor.GetVisibility() and getattr(self, "_sel_obj", None) in self.objs:
            self.select_actor.SetUserMatrix(vtk_matrix(self.objs[self._sel_obj].pose))
        for a, name in self._ent_actors:
            if name in self.objs:
                a.SetUserMatrix(vtk_matrix(self.objs[name].pose))
        self.render_later()                      # drags: one frame per event-loop pass

    def apply_display_mode(self, mode: str | None = None):
        if mode:
            self.display_mode = mode
        mode = self.display_mode
        if mode not in DISPLAY_MODES:
            mode = self.display_mode = DISPLAY_MODES[0]
        sel = set(self.selected)
        for name, oa in self.objs.items():
            has = oa.pd is not None and oa.visible and not (name in self.result_hidden or "*" in self.result_hidden)
            if self.isolated and name not in self.isolated:
                has = False
            oa.body.SetVisibility(has and mode != "Wireframe")
            oa.edges.SetVisibility(has and mode != "Shaded")
            if has and mode == "Shaded" and name in sel:
                oa.edges.SetVisibility(True)                      # outline what's selected
            oa.sketch.SetVisibility(oa.has_sketch and self.show_sketches and oa.visible and not self.result_hidden
                                    and not (self.isolated and name not in self.isolated))
            self._style(oa, mode, name in sel)
        self._apply_section()
        self.render()

    def _style(self, oa, mode, selected):
        """Colours/lighting of one object's actors for a display mode and selection state."""
        p = oa.body.GetProperty()
        m = oa.body.GetMapper()
        uncol = mode in ("Uncoloured", "Hidden line")
        if m is not None:
            m.SetScalarVisibility(bool(getattr(oa, "has_colors", False)) and not uncol and not selected)
        base = theme.BODY_COLOR if uncol else (getattr(oa, "uniform", None) or theme.BODY_COLOR)
        if mode == "Hidden line":
            base = (0.97, 0.97, 0.96)
            p.SetAmbient(1.0)
            p.SetDiffuse(0.0)
            p.SetSpecular(0.0)
        else:
            p.SetAmbient(0.18)
            p.SetDiffuse(0.75)
            p.SetSpecular(0.2 if uncol else 0.35)
        p.SetColor(*(_mix(base, theme.SELECT_COLOR, 0.55) if selected else base))
        p.SetOpacity(0.35 if mode == "Translucent" else 1.0)
        e = oa.edges.GetProperty()
        if selected:
            e.SetColor(*theme.SELECT_COLOR)
            e.SetLineWidth(2.4)
        else:
            e.SetColor(*({"Wireframe": theme.VIEW_LINES, "Hidden line": (0.08, 0.08, 0.09)}.get(mode, theme.EDGE_COLOR)))
            e.SetLineWidth(1.0 if mode == "Wireframe" else 1.4)

    def set_sketches_visible(self, on: bool):
        self.show_sketches = on
        self.apply_display_mode()

    # --------------------------------------------------------- camera
    def _cam_state(self):
        c = self.ren.GetActiveCamera()
        return (np.array(c.GetPosition()), np.array(c.GetFocalPoint()), np.array(c.GetViewUp()),
                float(c.GetParallelScale()))

    def _set_cam(self, st):
        c = self.ren.GetActiveCamera()
        pos, fp, up, ps = st
        c.SetPosition(*pos)
        c.SetFocalPoint(*fp)
        c.SetViewUp(*up)
        c.SetParallelScale(ps)
        c.OrthogonalizeViewUp()
        self.ren.ResetCameraClippingRange()

    def _push_view(self):
        st = self._cam_state()
        if self._view_hist and all(np.allclose(a, b) for a, b in zip(self._view_hist[-1], st)):
            return
        self._view_hist.append(st)
        del self._view_hist[:-30]

    def previous_view(self):
        if not self._view_hist:
            return False
        self._go(self._view_hist.pop(), push=False)
        return True

    def _go(self, target, push=True, ms=280):
        """Move the camera to `target` — animated (slerp of the view direction) unless turned off."""
        from PySide6.QtCore import QElapsedTimer, QTimer
        start = self._cam_state()
        if push:
            self._push_view()
        old = getattr(self, "_anim", None)
        if old is not None:
            old.stop()
            self._anim = None
        same = all(np.allclose(a, b, atol=1e-6) for a, b in zip(start, target))
        if same or not self.animate_views or not self.isVisible() or ms <= 0:
            self._set_cam(target)
            self.render()
            self.view_changed.emit()
            return
        p0, f0, u0, s0 = start
        p1, f1, u1, s1 = target
        d0, d1 = p0 - f0, p1 - f1
        r0, r1 = np.linalg.norm(d0) or 1.0, np.linalg.norm(d1) or 1.0
        a, b = d0 / r0, d1 / r1

        def slerp(x, y, t):
            dot = float(np.clip(np.dot(x, y), -1, 1))
            if dot > 0.9995:
                v = x + (y - x) * t
            elif dot < -0.9995:                                  # opposite: go round via any perpendicular
                ax = np.cross(x, [0, 0, 1] if abs(x[2]) < 0.9 else [1, 0, 0])
                ax /= np.linalg.norm(ax)
                ang = np.pi * t
                v = x * np.cos(ang) + ax * np.sin(ang)
            else:
                th = np.arccos(dot)
                v = (np.sin((1 - t) * th) * x + np.sin(t * th) * y) / np.sin(th)
            return v / (np.linalg.norm(v) or 1.0)

        clock = QElapsedTimer()
        clock.start()
        timer = QTimer(self)
        timer.setInterval(15)

        def step():
            t = min(1.0, clock.elapsed() / ms)
            e = 1 - (1 - t) ** 3                                        # ease-out cubic
            fp = f0 + (f1 - f0) * e
            d = slerp(a, b, e) * (r0 + (r1 - r0) * e)
            up = slerp(u0 / (np.linalg.norm(u0) or 1), u1 / (np.linalg.norm(u1) or 1), e)
            self._set_cam((fp + d, fp, up, s0 + (s1 - s0) * e))
            self.render()
            if t >= 1.0:
                timer.stop()
                self._anim = None
                self._set_cam(target)
                self.render()
                self.view_changed.emit()
        timer.timeout.connect(step)
        self._anim = timer
        timer.start()

    def _target(self, fn):
        """Camera state after `fn()` (which moves the camera); the camera is put back."""
        start = self._cam_state()
        fn()
        out = self._cam_state()
        self._set_cam(start)
        return out

    def _bounds(self, names=None):
        bb = None
        for n, oa in self.objs.items():
            if names is not None and n not in names:
                continue
            if oa.pd is None or not (oa.body.GetVisibility() or oa.edges.GetVisibility()):
                continue
            b = oa.body.GetBounds()
            if b[0] > b[1]:
                continue
            bb = list(b) if bb is None else [min(bb[0], b[0]), max(bb[1], b[1]), min(bb[2], b[2]),
                                             max(bb[3], b[3]), min(bb[4], b[4]), max(bb[5], b[5])]
        return bb

    def _fit_now(self, names=None):
        bb = self._bounds(names)
        if bb is not None:
            self.ren.ResetCamera(*bb)
        else:
            self.ren.ResetCamera()
        self.ren.GetActiveCamera().Zoom(0.9)

    def set_view(self, name: str, fit: bool = True, animate: bool = True):
        d, up = VIEWS[name]

        def apply():
            cam = self.ren.GetActiveCamera()
            fp = cam.GetFocalPoint()
            dist = cam.GetDistance() or 500
            n = np.array(d, float) / np.linalg.norm(d)
            cam.SetPosition(*(np.array(fp) + n * dist))
            cam.SetViewUp(*up)
            cam.OrthogonalizeViewUp()
            if fit:
                self._fit_now()
        if animate:
            self._go(self._target(apply))
        else:
            apply()
            self.render()

    def normal_to(self, normal, center=None):
        """Look straight at a face: camera along its normal (SolidWorks "Normal To")."""
        n = np.array(normal, float)
        n /= np.linalg.norm(n) or 1.0
        up = np.array([0, 0, 1.0]) if abs(n[2]) < 0.95 else np.array([0, 1.0, 0])

        def apply():
            cam = self.ren.GetActiveCamera()
            fp = np.array(center if center is not None else cam.GetFocalPoint(), float)
            dist = cam.GetDistance() or 500
            cam.SetFocalPoint(*fp)
            cam.SetPosition(*(fp + n * dist))
            cam.SetViewUp(*up)
            cam.OrthogonalizeViewUp()
            self._fit_now()
            c2 = self.ren.GetActiveCamera()                     # keep the face centred
            shift = fp - np.array(c2.GetFocalPoint())
            shift -= n * np.dot(shift, n)
            c2.SetFocalPoint(*(np.array(c2.GetFocalPoint()) + shift))
            c2.SetPosition(*(np.array(c2.GetPosition()) + shift))
        self._go(self._target(apply))

    def rotate_view(self, deg_az=0.0, deg_el=0.0, roll=0.0):
        def apply():
            cam = self.ren.GetActiveCamera()
            cam.Azimuth(deg_az)
            cam.Elevation(deg_el)
            cam.Roll(roll)
            cam.OrthogonalizeViewUp()
        self._go(self._target(apply), ms=180)

    def fit(self, names=None, animate=False):
        names = set(names) if names else None
        if animate:
            self._go(self._target(lambda: self._fit_now(names)))
            return
        self._fit_now(names)
        self.render()

    def set_perspective(self, on: bool):
        self.ren.GetActiveCamera().SetParallelProjection(not on)
        self._fit_now()
        self.render()

    def zoom_at(self, x, y, factor):
        """Zoom by `factor` (>1 = in) keeping the point under (x, y) fixed on screen."""
        cam = self.ren.GetActiveCamera()
        z = self.ren.GetZ(int(x), int(y))
        if z >= 0.9999:                                   # background: zoom about the focal plane
            fp = np.array(cam.GetFocalPoint())
            self.ren.SetWorldPoint(*fp, 1.0)
            self.ren.WorldToDisplay()
            z = self.ren.GetDisplayPoint()[2]
        self.ren.SetDisplayPoint(x, y, z)
        self.ren.DisplayToWorld()
        w = np.array(self.ren.GetWorldPoint())
        P = w[:3] / (w[3] or 1.0)
        pos, fp = np.array(cam.GetPosition()), np.array(cam.GetFocalPoint())
        if cam.GetParallelProjection():
            cam.SetParallelScale(cam.GetParallelScale() / factor)
            n = np.array(cam.GetDirectionOfProjection())
            off = (fp - P) / factor + P - fp
            off -= n * np.dot(off, n)
            cam.SetFocalPoint(*(fp + off))
            cam.SetPosition(*(pos + off))
        else:
            if np.linalg.norm(pos - P) < 1e-3 and factor > 1:
                return
            cam.SetPosition(*(P + (pos - P) / factor))
            cam.SetFocalPoint(*(P + (fp - P) / factor))
        self.ren.ResetCameraClippingRange()
        self.render_later()

    def set_hint(self, text: str | None):
        """Centered message card in the view (empty document); None hides it."""
        actor = getattr(self, "_hint", None)
        if actor is None:
            if not text:
                return
            actor = vtk.vtkTextActor()
            tp = actor.GetTextProperty()
            tp.SetFontFamilyToArial()
            tp.SetFontSize(15)
            tp.SetColor(0.90, 0.91, 0.92)
            tp.SetJustificationToCentered()
            tp.SetVerticalJustificationToCentered()
            tp.SetLineSpacing(1.35)
            tp.SetBackgroundColor(0.106, 0.118, 0.133)
            tp.SetBackgroundOpacity(0.86)
            tp.SetFrame(True)
            tp.SetFrameColor(*theme.SELECT_COLOR)
            tp.SetFrameWidth(1)
            actor.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
            actor.SetPosition(0.5, 0.5)
            actor.SetPickable(False)
            self.ren.AddActor(actor)
            self._hint = actor
        state = (bool(text), text)
        if getattr(self, "_hint_state", None) == state:
            return
        self._hint_state = state
        if text:
            pad = "      "
            actor.SetInput("\n" + "\n".join(pad + ln + pad for ln in text.split("\n")) + "\n")
        actor.SetVisibility(bool(text))
        self.render()

    def render(self):
        if getattr(self, "_batch", 0):
            self._render_pending = True
            return
        self._later = False
        if not self.isVisible():                 # not on screen yet (start-up, other page): the first paint draws
            return
        self.vtk.GetRenderWindow().Render()

    def render_later(self):
        """Coalesce bursts (mouse-move previews) into one render on the next event-loop pass."""
        if getattr(self, "_later", False):
            return
        self._later = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._later and self.render())

    def screenshot(self, path: str):
        f = vtk.vtkWindowToImageFilter()
        f.SetInput(self.vtk.GetRenderWindow())
        f.ReadFrontBufferOff()
        f.ShouldRerenderOn()
        f.Update()
        w = vtk.vtkPNGWriter()
        w.SetFileName(path)
        w.SetInputConnection(f.GetOutputPort())
        w.Write()

    # ----------------------------------------------------- navigation
    def _style_obj(self, x, y):
        st = self.iren.GetInteractorStyle()
        st.FindPokedRenderer(x, y)
        return st

    def _abort_nav(self, obj, evt):
        obj.GetCommand(self._nav_tags[evt]).SetAbortFlag(1)

    def _nav_left_down(self, obj, evt):
        if self.sketch_mode:
            return
        x, y = obj.GetEventPosition()
        ctrl, shift, alt = obj.GetControlKey(), obj.GetShiftKey(), obj.GetAltKey()
        if obj.GetRepeatCount():                                   # double-click: zoom to that part
            hit = self.pick_info(x, y)
            if hit:
                self.fit([hit[0]], animate=True)
            else:
                self.fit(animate=True)
            self._nav_state = None
            self._abort_nav(obj, evt)
            return
        if self.nav == "solidworks":
            if alt:                                                # laptop: Alt+left rotates
                self._moving_start()
                self._style_obj(x, y).StartRotate()
                self._nav_state = ("rotate",)
            else:
                self._nav_state = ("press", x, y, ctrl, shift)
            self._abort_nav(obj, evt)
            return
        if ctrl:                                                   # classic: Ctrl+left-drag box-selects
            self._nav_state = ("press", x, y, ctrl, shift)
            self._abort_nav(obj, evt)
            return
        self._nav_state = ("classic", x, y, ctrl, shift)           # style rotates; a click still selects

    def _nav_left_up(self, obj, evt):
        st, self._nav_state = self._nav_state, None
        if self.sketch_mode or st is None:
            return
        x, y = obj.GetEventPosition()
        if st[0] == "rotate":
            self.iren.GetInteractorStyle().EndRotate()
            self._abort_nav(obj, evt)
            self._moving_end()
            self.view_changed.emit()
            return
        if st[0] == "box":
            self._band_show(False)
            self.box_select(st[1], st[2], x, y, add=st[4], toggle=st[3] and self.nav == "solidworks")
            self._abort_nav(obj, evt)
            return
        if st[0] in ("press", "classic"):
            if abs(x - st[1]) + abs(y - st[2]) <= 3:
                self.click_select(x, y, ctrl=st[3] if self.nav == "solidworks" else False, shift=st[4])
            if st[0] == "press":
                self._abort_nav(obj, evt)
            else:
                self._moving_end()
                self.view_changed.emit()

    def _nav_mid_down(self, obj, evt):
        if self.sketch_mode or self.nav != "solidworks":
            return
        x, y = obj.GetEventPosition()
        style = self._style_obj(x, y)
        self._moving_start()
        if obj.GetControlKey():
            style.StartPan()
            self._nav_state = ("pan",)
        elif obj.GetShiftKey():
            style.StartDolly()
            self._nav_state = ("dolly",)
        else:
            style.StartRotate()
            self._nav_state = ("rotate",)
        self._abort_nav(obj, evt)

    def _nav_mid_up(self, obj, evt):
        if self.sketch_mode or self.nav != "solidworks":
            return
        st, self._nav_state = self._nav_state, None
        style = self.iren.GetInteractorStyle()
        if st:
            {"pan": style.EndPan, "dolly": style.EndDolly, "rotate": style.EndRotate}.get(st[0], lambda: None)()
            self.view_changed.emit()
        self._abort_nav(obj, evt)
        self._moving_end()

    def _nav_right_down(self, obj, evt):
        if self.sketch_mode:
            return
        x, y = obj.GetEventPosition()
        if self.nav == "solidworks":
            self._nav_state = ("rpress", x, y)
            self._abort_nav(obj, evt)
        else:
            self._nav_state = ("rclassic", x, y)

    def _nav_right_up(self, obj, evt):
        st, self._nav_state = self._nav_state, None
        if self.sketch_mode or st is None:
            return
        x, y = obj.GetEventPosition()
        if st[0] == "rpan":
            self.iren.GetInteractorStyle().EndPan()
            self._abort_nav(obj, evt)
            self._moving_end()
            self.view_changed.emit()
            return
        if st[0] in ("rpress", "rclassic") and abs(x - st[1]) + abs(y - st[2]) <= 3:
            hit = self.pick_info(x, y)
            info = None
            if hit:
                name, fid, wp, lp = hit
                info = {"object": name, "index": fid, "point": tuple(wp), "local": tuple(lp)}
                try:
                    face = self.objs[name].faces[fid]
                    n, c = face.normal_at(), face.center()
                    info.update(type=face.geom_type.name.lower(), normal=(n.X, n.Y, n.Z), center=(c.X, c.Y, c.Z))
                except Exception:  # noqa: BLE001
                    pass
            from PySide6.QtGui import QCursor
            self.context_requested.emit(info, QCursor.pos())
        if st[0] == "rpress":
            self._abort_nav(obj, evt)

    def _nav_move(self, obj, evt):
        st = self._nav_state
        if st is None and not self.sketch_mode and self.hover_highlight:
            self._hover_later(*obj.GetEventPosition())
        if st is None or self.sketch_mode:
            return
        x, y = obj.GetEventPosition()
        if st[0] == "press" and abs(x - st[1]) + abs(y - st[2]) > 4:
            self._nav_state = st = ("box",) + st[1:]
        if st[0] == "box":
            self._band_update(st[1], st[2], x, y)
            self._abort_nav(obj, evt)
        elif st[0] == "rpress" and abs(x - st[1]) + abs(y - st[2]) > 4:
            self._moving_start()
            self._style_obj(st[1], st[2]).StartPan()
            self._nav_state = ("rpan",)
        elif st[0] == "classic" and abs(x - st[1]) + abs(y - st[2]) > 4 and self._moving is None:
            self._moving_start()

    def _nav_wheel(self, obj, evt):
        if not self.zoom_to_cursor:
            return
        x, y = obj.GetEventPosition()
        f = 1.15 if evt == "MouseWheelForwardEvent" else 1 / 1.15
        self._moving_start()
        self.zoom_at(x, y, f)
        self._abort_nav(obj, evt)
        from PySide6.QtCore import QTimer
        if not getattr(self, "_wheel_t", None):
            self._wheel_t = QTimer(self, singleShot=True, interval=250)
            self._wheel_t.timeout.connect(self._moving_end)
            self._wheel_t.timeout.connect(self.view_changed.emit)
        self._wheel_t.start()

    # ------------------------------------------------------------- grid
    @staticmethod
    def _nice(v):
        import math
        e = 10 ** math.floor(math.log10(max(v, 1e-9)))
        for m in (1, 2, 5, 10):
            if v <= m * e:
                return m * e
        return 10 * e

    def grid_spacing(self):
        """(major, minor, half-size) of the floor grid for the current model, mm."""
        bb = self._bounds()
        if bb is None:
            ext, reach = 100.0, 100.0
        else:
            ext = max(bb[1] - bb[0], bb[3] - bb[2], bb[5] - bb[4], 1.0)
            reach = max(abs(bb[0]), abs(bb[1]), abs(bb[2]), abs(bb[3]), ext * 0.5)
        major = self._nice(ext / 4)
        minor = major / 5
        import math
        half = math.ceil(max(reach + ext * 0.35, major * 2) / major) * major
        return major, minor, half

    def set_grid(self, on: bool):
        self.grid_on = bool(on)
        self._update_grid()
        self.render()

    def _update_grid(self):
        """A see-through floor grid on the XY plane (Z = 0): faint minor lines, stronger major lines, X / Y axes
        tinted. Sized and spaced to the model; never pickable, never part of zoom-to-fit."""
        if not self.grid_on:
            for a in self._grid or ():
                a.SetVisibility(False)
            return
        major, minor, half = self.grid_spacing()
        key = (round(major, 9), round(half, 9))
        if self._grid is not None and key == self._grid_key:
            for a in self._grid:
                a.SetVisibility(True)
            return
        for a in self._grid or ():
            self.ren.RemoveActor(a)
        self._grid_key = key
        n_minor = int(round(half / minor))
        every = int(round(major / minor))

        def lines(vals, z=0.0):
            pts = vtk.vtkPoints()
            cells = vtk.vtkCellArray()
            for v in vals:
                for a_, b_ in (((v, -half), (v, half)), ((-half, v), (half, v))):
                    i = pts.InsertNextPoint(a_[0], a_[1], z)
                    pts.InsertNextPoint(b_[0], b_[1], z)
                    cells.InsertNextCell(2)
                    cells.InsertCellPoint(i)
                    cells.InsertCellPoint(i + 1)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(cells)
            return pd

        minor_v = [k * minor for k in range(-n_minor, n_minor + 1) if k % every and k]
        major_v = [k * minor for k in range(-n_minor, n_minor + 1) if not k % every and k]
        col = theme.VIEW_LINES
        actors = []
        for pd, op, w in ((lines(minor_v), 0.16, 1.0), (lines(major_v), 0.36, 1.2)):
            m = vtk.vtkPolyDataMapper()
            m.SetInputData(pd)
            m.StaticOn()
            a = vtk.vtkActor()
            a.SetMapper(m)
            pr = a.GetProperty()
            pr.SetColor(*col)
            pr.SetOpacity(op)
            pr.SetLineWidth(w)
            pr.SetLighting(False)
            a.PickableOff()
            actors.append(a)
        for (p0, p1), c in ((((-half, 0, 0), (half, 0, 0)), (0.86, 0.30, 0.30)),
                            (((0, -half, 0), (0, half, 0)), (0.36, 0.72, 0.36))):
            ls = vtk.vtkLineSource()
            ls.SetPoint1(*p0)
            ls.SetPoint2(*p1)
            ls.Update()
            m = vtk.vtkPolyDataMapper()
            m.SetInputData(ls.GetOutput())
            a = vtk.vtkActor()
            a.SetMapper(m)
            pr = a.GetProperty()
            pr.SetColor(*c)
            pr.SetOpacity(0.55)
            pr.SetLineWidth(1.6)
            pr.SetLighting(False)
            a.PickableOff()
            actors.append(a)
        for a in actors:
            self.ren.AddActor(a)
        self._grid = actors

    # ------------------------------------------------------ sketch-plane preview
    def show_plane_preview(self, origin, normal, x_dir, size, pose=None, label=None):
        """A translucent square showing where a sketch would go (cleared with clear_plane_preview)."""
        self.clear_plane_preview()
        o, n, xd = (np.asarray(v, float) for v in (origin, normal, x_dir))
        n /= np.linalg.norm(n) or 1.0
        xd /= np.linalg.norm(xd) or 1.0
        yd = np.cross(n, xd)
        h = size / 2
        src = vtk.vtkPlaneSource()
        src.SetOrigin(*(o - xd * h - yd * h))
        src.SetPoint1(*(o + xd * h - yd * h))
        src.SetPoint2(*(o - xd * h + yd * h))
        src.Update()
        acts = []
        fill = vtk.vtkPolyDataMapper()
        fill.SetInputData(src.GetOutput())
        fill.SetResolveCoincidentTopologyToPolygonOffset()
        a = vtk.vtkActor()
        a.SetMapper(fill)
        col = tuple(int(theme.ACCENT_LINE[i:i + 2], 16) / 255 for i in (1, 3, 5))
        a.GetProperty().SetColor(*col)
        a.GetProperty().SetOpacity(0.28)
        a.GetProperty().SetLighting(False)
        acts.append(a)
        edges = vtk.vtkFeatureEdges()
        edges.SetInputData(src.GetOutput())
        edges.BoundaryEdgesOn()
        edges.FeatureEdgesOff()
        edges.ManifoldEdgesOff()
        edges.NonManifoldEdgesOff()
        edges.Update()
        em = vtk.vtkPolyDataMapper()
        em.SetInputData(edges.GetOutput())
        em.ScalarVisibilityOff()
        e = vtk.vtkActor()
        e.SetMapper(em)
        e.GetProperty().SetColor(*col)
        e.GetProperty().SetLineWidth(2.0)
        e.GetProperty().SetLighting(False)
        acts.append(e)
        arrow = vtk.vtkLineSource()                          # the normal: which side the sketch faces
        arrow.SetPoint1(*o)
        arrow.SetPoint2(*(o + n * size * 0.18))
        arrow.Update()
        am = vtk.vtkPolyDataMapper()
        am.SetInputData(arrow.GetOutput())
        ar = vtk.vtkActor()
        ar.SetMapper(am)
        ar.GetProperty().SetColor(*col)
        ar.GetProperty().SetLineWidth(3.0)
        ar.GetProperty().SetLighting(False)
        acts.append(ar)
        for act in acts:
            act.PickableOff()
            if pose is not None:
                act.SetUserMatrix(vtk_matrix(pose))
            self.ren.AddActor(act)
        self._plane_preview = acts
        self.render()

    def clear_plane_preview(self):
        for a in getattr(self, "_plane_preview", None) or ():
            self.ren.RemoveActor(a)
        self._plane_preview = None
        self.render()

    # ------------------------------------------------------ performance
    HEAVY_CELLS = 400_000          # above this (or HEAVY_OBJECTS parts) the view simplifies while it moves
    HEAVY_OBJECTS = 120

    def _heavy(self) -> bool:
        live = [oa for oa in self.objs.values() if oa.pd is not None and oa.body.GetVisibility()]
        if len(live) > self.HEAVY_OBJECTS:
            return True
        return sum(oa.pd.GetNumberOfCells() for oa in live) > self.HEAVY_CELLS

    def _moving_start(self):
        """While the camera moves on a big model: no anti-aliasing, no edge lines (like a large-assembly
        mode). Everything comes back on release."""
        if self._moving is not None or not self.simplify_moving or not self._heavy():
            return
        st = {"fxaa": bool(self.ren.GetUseFXAA()), "edges": []}
        if st["fxaa"]:
            self.ren.UseFXAAOff()
        for oa in self.objs.values():
            if oa.edges.GetVisibility() and oa.body.GetVisibility():
                oa.edges.SetVisibility(False)
                st["edges"].append(oa)
        self._moving = st

    def _moving_end(self):
        st, self._moving = self._moving, None
        if st is None:
            return
        if st["fxaa"] and self.antialias == "fxaa":
            self.ren.UseFXAAOn()
        live = set(map(id, self.objs.values()))
        for oa in st["edges"]:
            if id(oa) in live and oa.body.GetVisibility():
                oa.edges.SetVisibility(True)
        self.render()

    def set_antialiasing(self, mode: str):
        """'off' | 'fxaa' (default, cheap) | 'msaa' (multisampling ×4: smoothest, slowest)."""
        mode = mode if mode in ("off", "fxaa", "msaa") else "fxaa"
        self.antialias = mode
        self.ren.SetUseFXAA(mode == "fxaa")
        self.vtk.GetRenderWindow().SetMultiSamples(4 if mode == "msaa" else 0)
        self.render()

    def graphics_info(self) -> dict:
        """OpenGL vendor / renderer / version, and whether it looks like software rendering."""
        rw = self.vtk.GetRenderWindow()
        try:
            rep = rw.ReportCapabilities() or ""
        except Exception:  # noqa: BLE001
            rep = ""
        info = {}
        for line in rep.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                if k in ("opengl vendor string", "opengl renderer string", "opengl version string"):
                    info[k.split()[1]] = v.strip()
        r = (info.get("renderer", "") + " " + info.get("vendor", "")).lower()
        info["software"] = any(w in r for w in ("llvmpipe", "softpipe", "swrast", "gdi generic",
                                                "microsoft basic render", "mesa offscreen"))
        live = [oa for oa in self.objs.values() if oa.pd is not None]
        info["objects"] = len(live)
        info["triangles"] = sum(oa.pd.GetNumberOfCells() for oa in live)
        import time as _t
        t0 = _t.perf_counter()
        for _ in range(3):
            rw.Render()
        info["frame_ms"] = round((_t.perf_counter() - t0) * 1000 / 3, 1)
        return info

    # ---------------------------------------------------- box select band
    def _make_band(self):
        pts = vtk.vtkPoints()
        for _ in range(4):
            pts.InsertNextPoint(0, 0, 0)
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        fill_c = vtk.vtkCellArray()
        fill_c.InsertNextCell(4, [0, 1, 2, 3])
        poly.SetPolys(fill_c)
        line = vtk.vtkPolyData()
        line.SetPoints(pts)
        lc = vtk.vtkCellArray()
        lc.InsertNextCell(5, [0, 1, 2, 3, 0])
        line.SetLines(lc)
        actors = []
        for pd, op in ((poly, 0.14), (line, 1.0)):
            m = vtk.vtkPolyDataMapper2D()
            m.SetInputData(pd)
            a = vtk.vtkActor2D()
            a.SetMapper(m)
            a.GetProperty().SetOpacity(op)
            a.GetProperty().SetLineWidth(1.5)
            a.SetVisibility(False)
            a.PickableOff()
            self.ren.AddViewProp(a)
            actors.append(a)
        return {"pts": pts, "poly": poly, "line": line, "actors": actors}

    def _band_update(self, x0, y0, x1, y1):
        b = self._band
        for i, (px, py) in enumerate(((x0, y0), (x1, y0), (x1, y1), (x0, y1))):
            b["pts"].SetPoint(i, px, py, 0)
        b["pts"].Modified()
        col = (0.30, 0.62, 0.95) if x1 >= x0 else (0.35, 0.80, 0.45)     # window: blue · crossing: green
        for a in b["actors"]:
            a.GetProperty().SetColor(*col)
            a.SetVisibility(True)
        self.render_later()

    def _band_show(self, on):
        for a in self._band["actors"]:
            a.SetVisibility(on)
        self.render()

    # -------------------------------------------------------- selection
    def set_selected(self, names, emit=True):
        names = [n for n in dict.fromkeys(names or []) if n in self.objs]
        if names == self.selected:
            return
        self.selected = names
        self.apply_display_mode()
        if emit:
            self.selection_changed.emit(list(names))

    def click_select(self, x, y, ctrl=False, shift=False):
        if self.sel_mode in ("face", "edge", "any"):
            return self.click_entity(x, y, ctrl, shift)
        hit = self.pick_info(x, y)
        if hit is None:
            if not (ctrl or shift):
                self.set_selected([])
            self.pick(x, y, hit=hit)
            return
        name = hit[0]
        if ctrl:
            new = [n for n in self.selected if n != name] if name in self.selected else self.selected + [name]
        elif shift:
            new = self.selected + [name]
        else:
            new = [name]
        self.set_selected(new)
        self.pick(x, y, hit=hit)

    def _sample_points(self, oa, n=2500):
        key = id(oa.pd)
        hit = getattr(oa, "_sample", None)
        if hit is None or hit[0] != key:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            P = vtk_to_numpy(oa.pd.GetPoints().GetData()).astype(float)
            if len(P) > n:
                P = P[:: int(np.ceil(len(P) / n))]
            hit = oa._sample = (key, P)
        return hit[1]

    def objects_in_rect(self, x0, y0, x1, y1, crossing=None):
        """Objects in a screen rectangle: window (fully inside) when dragged left→right, crossing (touched)
        when dragged right→left — the usual CAD convention."""
        from ..core.snap3d import vtk_projector
        if crossing is None:
            crossing = x1 < x0
        lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        proj = vtk_projector(self.ren)
        out = []
        for name, oa in self.objs.items():
            if oa.pd is None or not (oa.body.GetVisibility() or oa.edges.GetVisibility()):
                continue
            b = oa.body.GetBounds()
            corners = np.array([[b[i], b[2 + j], b[4 + k]] for i in (0, 1) for j in (0, 1) for k in (0, 1)])
            c2 = proj(corners)
            inside_bb = (c2[:, 0] >= lo_x) & (c2[:, 0] <= hi_x) & (c2[:, 1] >= lo_y) & (c2[:, 1] <= hi_y)
            if inside_bb.all():
                out.append(name)
                continue
            if c2[:, 0].max() < lo_x or c2[:, 0].min() > hi_x or c2[:, 1].max() < lo_y or c2[:, 1].min() > hi_y:
                continue
            M = pose_matrix(oa.pose)
            W = self._sample_points(oa) @ M[:3, :3].T + M[:3, 3]
            p = proj(W)
            ins = (p[:, 0] >= lo_x) & (p[:, 0] <= hi_x) & (p[:, 1] >= lo_y) & (p[:, 1] <= hi_y)
            if (crossing and ins.any()) or (not crossing and ins.all()):
                out.append(name)
        if crossing:                                   # big faces crossing the box with no vertex inside it
            try:
                hs = vtk.vtkHardwareSelector()
                hs.SetRenderer(self.ren)
                hs.SetArea(int(lo_x), int(lo_y), int(hi_x), int(hi_y))
                hs.SetFieldAssociation(vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS)
                sel = hs.Select()
                by_actor = {id(oa.body): n for n, oa in self.objs.items()}
                for i in range(sel.GetNumberOfNodes()):
                    a = sel.GetNode(i).GetProperties().Get(vtk.vtkSelectionNode.PROP())
                    n = by_actor.get(id(a)) if a is not None else None
                    if n and n not in out:
                        out.append(n)
            except Exception:  # noqa: BLE001
                pass
        return out

    def box_select(self, x0, y0, x1, y1, add=False, toggle=False):
        if self.sel_mode in ("face", "edge", "any"):
            got = self.entities_in_rect(x0, y0, x1, y1, "face" if self.sel_mode == "face" else "edge") \
                if self.sel_mode != "any" else (self.entities_in_rect(x0, y0, x1, y1, "face") +
                                                self.entities_in_rect(x0, y0, x1, y1, "edge"))
            self._merge_entities(got, add=add, toggle=toggle)
            self.render()
            return
        got = self.objects_in_rect(x0, y0, x1, y1)
        if toggle:
            cur = list(self.selected)
            for n in got:
                cur.remove(n) if n in cur else cur.append(n)
            self.set_selected(cur)
        elif add:
            self.set_selected(self.selected + got)
        else:
            self.set_selected(got)
        self.render()

    def set_isolated(self, names):
        self.isolated = set(names or [])
        self.apply_display_mode()

    # ------------------------------------------------- faces & edges
    def set_sel_mode(self, mode):
        """What clicking / box-selecting picks: whole parts, faces or edges."""
        self.sel_mode = mode if mode in ("part", "face", "edge", "any") else "part"
        self._hover_set(None)
        self.clear_selection()
        if self.sel_mode != "part" and self.selected:
            self.set_selected([])
        if self.sel_mode == "part" and self.entities:
            self.set_entities([])
        self.render()

    @staticmethod
    def _ekey(e):
        return (e["object"], e["kind"], e["index"])

    def set_entities(self, ents, emit=True):
        self.entities = list({self._ekey(e): e for e in ents}.values())
        self._draw_entities()
        self.render()
        if emit:
            self.entities_changed.emit(list(self.entities))

    def _merge_entities(self, got, add=False, toggle=False):
        if toggle:
            have = {self._ekey(e): e for e in self.entities}
            for e in got:
                k = self._ekey(e)
                have.pop(k) if k in have else have.__setitem__(k, e)
            self.set_entities(list(have.values()))
        elif add:
            self.set_entities(self.entities + got)
        else:
            self.set_entities(got)

    def click_entity(self, x, y, ctrl=False, shift=False):
        e = self.entity_at(x, y, self.sel_mode)
        if e is None:
            if not (ctrl or shift):
                self.set_entities([])
                self.clear_selection()
            return
        if e["kind"] == "face":
            self.pick(x, y)                         # face info / Normal to / sketch-on-face, as before
        self.clear_selection()                      # the entity highlight replaces the single-face one
        self._merge_entities([e], add=shift, toggle=ctrl)

    # geometry caches (local coordinates) -------------------------------------
    def _edge_data(self, oa):
        epd = getattr(oa, "epd", None)
        if epd is None or not epd.GetNumberOfCells():
            return None
        key = id(epd)
        c = getattr(oa, "_ecache", None)
        if c is None or c[0] != key:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            P = vtk_to_numpy(epd.GetPoints().GetData()).astype(float)
            lines = epd.GetLines()
            off = vtk_to_numpy(lines.GetOffsetsArray()).astype(np.int64)
            con = vtk_to_numpy(lines.GetConnectivityArray()).astype(np.int64)
            a, b, eid = [], [], []
            for i in range(len(off) - 1):
                ids = con[off[i]:off[i + 1]]
                a.append(ids[:-1])
                b.append(ids[1:])
                eid.append(np.full(len(ids) - 1, i))
            c = oa._ecache = (key, P, np.concatenate(a), np.concatenate(b), np.concatenate(eid), off, con)
        return c

    def _face_data(self, oa):
        if oa.pd is None:
            return None
        key = id(oa.pd)
        c = getattr(oa, "_fcache", None)
        if c is None or c[0] != key:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            P = vtk_to_numpy(oa.pd.GetPoints().GetData()).astype(float)
            polys = oa.pd.GetPolys()
            con = vtk_to_numpy(polys.GetConnectivityArray()).astype(np.int64).reshape(-1, 3)
            fid = vtk_to_numpy(oa.pd.GetCellData().GetArray("face_id")).astype(np.int64)
            c = oa._fcache = (key, P[con].mean(axis=1), fid)
        return c

    def edge_midpoint(self, name, eid):
        """A point half way along edge `eid` of an object (local coordinates) — what at(x,y,z) records."""
        d = self._edge_data(self.objs[name])
        _k, P, _a, _b, _e, off, con = d
        pts = P[con[off[eid]:off[eid + 1]]]
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        if not len(seg) or seg.sum() == 0:
            return pts[0]
        cum = np.concatenate(([0.0], np.cumsum(seg)))
        half = cum[-1] / 2
        i = min(int(np.searchsorted(cum, half)) - 1, len(seg) - 1)
        i = max(i, 0)
        t = (half - cum[i]) / (seg[i] or 1.0)
        return pts[i] + (pts[i + 1] - pts[i]) * t

    def edge_length(self, name, eid):
        d = self._edge_data(self.objs[name])
        _k, P, _a, _b, _e, off, con = d
        pts = P[con[off[eid]:off[eid + 1]]]
        return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())

    def _px_size(self, at):
        """World size of one screen pixel at a point."""
        cam = self.ren.GetActiveCamera()
        h = max(1, self.ren.GetSize()[1])
        if cam.GetParallelProjection():
            return 2 * cam.GetParallelScale() / h
        import math
        d = float(np.linalg.norm(np.asarray(at) - np.array(cam.GetPosition())))
        return 2 * d * math.tan(math.radians(cam.GetViewAngle()) / 2) / h

    def _world(self, oa, L):
        M = pose_matrix(oa.pose)
        return L @ M[:3, :3].T + M[:3, 3]

    def entity_at(self, x, y, kind, tol=7.0):
        """Face, edge or part under the cursor: {object, kind, index, point (world), local}. kind "any": an
        edge when the cursor is within a few pixels of one, else the face."""
        if kind == "any":
            return self.entity_at(x, y, "edge", tol=6.0) or self.entity_at(x, y, "face")
        hit = self.pick_info(x, y)
        if kind == "part":
            if hit is None:
                return None
            return {"object": hit[0], "kind": "part", "index": 0, "point": tuple(map(float, hit[2])),
                    "local": tuple(map(float, hit[3]))}
        if kind == "face":
            if hit is None:
                return None
            name, fid, wp, lp = hit
            return {"object": name, "kind": "face", "index": int(fid), "point": tuple(map(float, wp)),
                    "local": tuple(map(float, lp))}
        from ..core.snap3d import vtk_projector
        proj = vtk_projector(self.ren)
        cands = [hit[0]] if hit else [n for n, oa in self.objs.items() if oa.pd is not None and
                                       (oa.body.GetVisibility() or oa.edges.GetVisibility())]
        best = None
        for name in cands:
            oa = self.objs[name]
            d = self._edge_data(oa)
            if d is None:
                continue
            if not hit:                                   # quick reject by screen bounds
                b = oa.body.GetBounds()
                cc = proj(np.array([[b[i], b[2 + j], b[4 + k]] for i in (0, 1) for j in (0, 1) for k in (0, 1)]))
                if (x < cc[:, 0].min() - tol or x > cc[:, 0].max() + tol or y < cc[:, 1].min() - tol
                        or y > cc[:, 1].max() + tol):
                    continue
            _k, P, a, b_, eid, _o, _c = d
            W = self._world(oa, P)
            S = proj(W)
            A, B = S[a], S[b_]
            AB = B - A
            L2 = (AB ** 2).sum(1)
            L2[L2 == 0] = 1e-12
            t = np.clip(((np.array([x, y]) - A) * AB).sum(1) / L2, 0, 1)
            Q = A + AB * t[:, None]
            dist = np.hypot(Q[:, 0] - x, Q[:, 1] - y)
            near = np.where(dist <= tol)[0]
            if not len(near):
                continue
            if hit:                                       # prefer the edge nearest the picked surface point
                H3 = np.asarray(hit[2], float)
                A3, B3 = W[a[near]], W[b_[near]]                  # 3D point-to-segment (screen t isn't
                D3 = B3 - A3                                      # linear in depth under perspective)
                tt = np.clip(((H3 - A3) * D3).sum(1) / np.maximum((D3 ** 2).sum(1), 1e-12), 0, 1)
                d3 = np.linalg.norm(A3 + D3 * tt[:, None] - H3, axis=1)
                ok = d3 <= self._px_size(hit[2]) * tol * 2.5      # hidden edges behind the face don't count
                if not ok.any():
                    continue
                near, d3 = near[ok], d3[ok]
                score = d3 + dist[near] * 1e-3
            else:
                cam = np.array(self.ren.GetActiveCamera().GetPosition())
                Pw = W[a[near]] + (W[b_[near]] - W[a[near]]) * t[near, None]
                score = dist[near] * 1e3 + np.linalg.norm(Pw - cam, axis=1) * 1e-6
            j = int(np.argmin(score))
            sc = float(score[j]) if hit else float(dist[near][j])
            if best is None or sc < best[0]:
                i = near[j]
                wp = W[a[i]] + (W[b_[i]] - W[a[i]]) * t[i]
                best = (sc, name, int(eid[i]), wp)
        if best is None:
            return None
        _sc, name, e, wp = best
        mid = self.edge_midpoint(name, e)
        return {"object": name, "kind": "edge", "index": e, "point": tuple(map(float, wp)),
                "local": tuple(map(float, mid))}

    def entities_in_rect(self, x0, y0, x1, y1, kind):
        """Faces / edges in a screen box: window (left→right, fully inside) or crossing (right→left, touched)."""
        from ..core.snap3d import vtk_projector
        crossing = x1 < x0
        lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        proj = vtk_projector(self.ren)
        out = []
        for name, oa in self.objs.items():
            if oa.pd is None or not (oa.body.GetVisibility() or oa.edges.GetVisibility()):
                continue
            b = oa.body.GetBounds()
            cc = proj(np.array([[b[i], b[2 + j], b[4 + k]] for i in (0, 1) for j in (0, 1) for k in (0, 1)]))
            if cc[:, 0].max() < lo_x or cc[:, 0].min() > hi_x or cc[:, 1].max() < lo_y or cc[:, 1].min() > hi_y:
                continue
            if kind == "face":
                d = self._face_data(oa)
                if d is None:
                    continue
                _k, C, fid = d
                S = proj(self._world(oa, C))
                ins = (S[:, 0] >= lo_x) & (S[:, 0] <= hi_x) & (S[:, 1] >= lo_y) & (S[:, 1] <= hi_y)
                for f in np.unique(fid):
                    m = fid == f
                    if (ins[m].any() if crossing else ins[m].all()):
                        c = C[m].mean(axis=0)
                        out.append({"object": name, "kind": "face", "index": int(f),
                                    "point": tuple(map(float, self._world(oa, c[None])[0])),
                                    "local": tuple(map(float, c))})
            else:
                d = self._edge_data(oa)
                if d is None:
                    continue
                _k, P, _a, _b, _e, off, con = d
                S = proj(self._world(oa, P))
                ins = (S[:, 0] >= lo_x) & (S[:, 0] <= hi_x) & (S[:, 1] >= lo_y) & (S[:, 1] <= hi_y)
                for i in range(len(off) - 1):
                    m = ins[con[off[i]:off[i + 1]]]
                    if (m.any() if crossing else m.all()):
                        mid = self.edge_midpoint(name, i)
                        out.append({"object": name, "kind": "edge", "index": i,
                                    "point": tuple(map(float, self._world(oa, mid[None])[0])),
                                    "local": tuple(map(float, mid))})
        return out

    # highlight -------------------------------------------------------------
    def _entity_actor(self, oa, kind, ids, color, width=4.0, opacity=1.0):
        src = oa.pd if kind == "face" else getattr(oa, "epd", None)
        if src is None:
            return None
        if kind == "face":
            from vtkmodules.util.numpy_support import vtk_to_numpy
            fid = vtk_to_numpy(src.GetCellData().GetArray("face_id"))
            cells = np.where(np.isin(fid, list(ids)))[0]
        else:
            cells = np.array(sorted(ids), dtype=np.int64)
        idl = vtk.vtkIdList()
        for c in cells:
            idl.InsertNextId(int(c))
        ex = vtk.vtkExtractCells()
        ex.SetInputData(src)
        ex.SetCellList(idl)
        geo = vtk.vtkGeometryFilter()
        geo.SetInputConnection(ex.GetOutputPort())
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(geo.GetOutputPort())
        m.ScalarVisibilityOff()
        m.SetResolveCoincidentTopologyToPolygonOffset()
        m.SetRelativeCoincidentTopologyPolygonOffsetParameters(-2, -2)
        m.SetRelativeCoincidentTopologyLineOffsetParameters(-4, -4)
        a = vtk.vtkActor()
        a.SetMapper(m)
        p = a.GetProperty()
        p.SetColor(*color)
        p.SetOpacity(opacity)
        if kind == "edge":
            p.SetLineWidth(width)
            p.SetLighting(False)
            p.SetRenderLinesAsTubes(True)
        else:
            p.SetAmbient(0.55)
        a.PickableOff()
        a.SetUserMatrix(vtk_matrix(oa.pose))
        if self.section is not None and hasattr(self, "_clip"):
            m.AddClippingPlane(self._clip)
        self.ren.AddActor(a)
        return a

    def _draw_entities(self):
        for a, _n in self._ent_actors:
            self.ren.RemoveActor(a)
        self._ent_actors = []
        groups: dict = {}
        for e in self.entities:
            groups.setdefault((e["object"], e["kind"]), set()).add(e["index"])
        for (name, kind), ids in groups.items():
            oa = self.objs.get(name)
            if oa is None:
                continue
            col = theme.SELECT_COLOR if kind == "face" else (0.20, 0.75, 1.0)
            a = self._entity_actor(oa, kind, ids, col)
            if a is not None:
                self._ent_actors.append((a, name))

    def _hover_later(self, x, y):
        """Pre-highlight once the cursor settles (debounced): no picking while the mouse sweeps across."""
        if (x, y) == getattr(self, "_hover_pos", None):
            return
        self._hover_pos = (x, y)
        if getattr(self, "_hover_t", None) is None:
            from PySide6.QtCore import QTimer
            self._hover_t = QTimer(self, singleShot=True, interval=40)
            self._hover_t.timeout.connect(self._hover_tick)
        self._hover_t.start()

    def _hover_tick(self):
        if not self.hover_highlight or self._nav_state is not None or self.sketch_mode:
            return self._hover_set(None)
        e = self.entity_at(*self._hover_pos, self.sel_mode)
        self._hover_set(e)

    def _hover_set(self, e):
        key = self._ekey(e) if e else None
        if key == getattr(self, "_hover_key", None):
            return
        self._hover_key = key
        if self._hover is not None:
            self.ren.RemoveActor(self._hover)
            self._hover = None
        if e and e["object"] in self.objs:
            oa = self.objs[e["object"]]
            if e["kind"] == "part":
                if e["object"] in self.selected or getattr(oa, "epd", None) is None:
                    self.render_later()
                    return
                m = vtk.vtkPolyDataMapper()                      # the part's outline, brighter
                m.SetInputData(oa.epd)
                m.SetResolveCoincidentTopologyToPolygonOffset()
                m.SetRelativeCoincidentTopologyLineOffsetParameters(-4, -4)
                a = vtk.vtkActor()
                a.SetMapper(m)
                p = a.GetProperty()
                p.SetColor(0.45, 0.88, 0.80)
                p.SetLineWidth(2.2)
                p.SetLighting(False)
                a.PickableOff()
                a.SetUserMatrix(vtk_matrix(oa.pose))
                if self.section is not None and hasattr(self, "_clip"):
                    m.AddClippingPlane(self._clip)
                self.ren.AddActor(a)
                self._hover = a
            else:
                self._hover = self._entity_actor(oa, e["kind"], {e["index"]}, (0.45, 0.88, 0.80), width=3.0,
                                                 opacity=0.6 if e["kind"] == "face" else 1)
        self.render_later()

    # --------------------------------------------------------- section
    def set_section(self, axis=None, offset=0.5, flip=False):
        """Section view: cut everything with a plane normal to X/Y/Z (axis 0/1/2) at `offset` (0..1 across
        the model's bounds). axis None turns it off."""
        prev = self.section
        self.section = None if axis is None else (int(axis), float(offset), bool(flip))
        if prev is None or self.section is None or prev[0] != self.section[0]:
            self._sec_bounds = None
        self._apply_section()
        self.render()

    def _apply_section(self):
        sec = self.section
        if sec is not None:
            bb = getattr(self, "_sec_bounds", None)
            if bb is None:
                bb = self._bounds() or [0, 1, 0, 1, 0, 1]
                self._sec_bounds = bb
            ax, off, flip = sec
            lo, hi = bb[2 * ax], bb[2 * ax + 1]
            o = [(bb[0] + bb[1]) / 2, (bb[2] + bb[3]) / 2, (bb[4] + bb[5]) / 2]
            o[ax] = lo + (hi - lo) * off
            nrm = [0.0, 0.0, 0.0]
            nrm[ax] = 1.0 if flip else -1.0
            if not hasattr(self, "_clip"):
                self._clip = vtk.vtkPlane()
                self._back = vtk.vtkProperty()
                self._back.SetColor(*_mix(theme.SELECT_COLOR, (0.2, 0.2, 0.2), 0.35))
                self._back.SetAmbient(0.6)
                self._back.SetDiffuse(0.3)
            self._clip.SetOrigin(*o)
            self._clip.SetNormal(*nrm)
        else:
            self._sec_bounds = None
        for oa in self.objs.values():
            for a in (oa.body, oa.edges, oa.sketch):
                m = a.GetMapper()
                if m is None:
                    continue
                has = m.GetNumberOfClippingPlanes() if hasattr(m, "GetNumberOfClippingPlanes") else 0
                if sec is None:
                    if has:
                        m.RemoveAllClippingPlanes()
                elif not has:
                    m.AddClippingPlane(self._clip)
            if sec is not None:
                oa.body.SetBackfaceProperty(self._back)
            elif oa.body.GetBackfaceProperty() is not None:
                oa.body.SetBackfaceProperty(None)

    def _eat_char(self, obj, _evt):
        obj.GetCommand(self._char_tag).SetAbortFlag(1)

    def _on_key(self, obj, _evt):
        if self.sketch_mode:
            return
        k = obj.GetKeySym()
        if k in ("f", "F"):
            self.fit(animate=True)
        elif k == "space":
            self.view_wheel_requested.emit()

    def pick_info(self, x, y):
        """(object name, face index, world point, local point) under the cursor, or None."""
        flip = []
        if self.display_mode == "Wireframe":                     # bodies are hidden; pick them anyway
            for oa in self.objs.values():
                if oa.edges.GetVisibility() and not oa.body.GetVisibility():
                    oa.body.SetVisibility(True)
                    flip.append(oa)
        self._sync_locators()
        try:
            self.picker.Pick(x, y, 0, self.ren)
        finally:
            for oa in flip:
                oa.body.SetVisibility(False)
        actor = self.picker.GetActor()
        cell = self.picker.GetCellId()
        if cell < 0 or actor is None:
            return None
        for name, oa in self.objs.items():
            if actor is oa.body and oa.pd is not None:
                fid = int(oa.pd.GetCellData().GetArray("face_id").GetValue(cell))
                wp = np.array(self.picker.GetPickPosition())
                M = pose_matrix(oa.pose)
                lp = np.linalg.inv(M) @ np.append(wp, 1.0)
                return name, fid, wp, lp[:3]
        return None

    def _sync_locators(self):
        """Cell locators for the picker: a pick searches a tree instead of every triangle of the part."""
        pds = [oa.pd for oa in self.objs.values() if oa.pd is not None]
        sig = tuple(id(pd) for pd in pds)
        if sig == self._loc_sig:
            return
        old, self._locators = self._locators, {}
        self.picker.RemoveAllLocators()
        for pd in pds:
            loc = old.get(id(pd))
            if loc is None or loc.GetDataSet() is not pd:
                loc = vtk.vtkStaticCellLocator()
                loc.SetDataSet(pd)
                loc.BuildLocator()
            self._locators[id(pd)] = loc
            self.picker.AddLocator(loc)
        self._loc_sig = sig

    def pick(self, x, y, hit=False):
        if hit is False:
            hit = self.pick_info(x, y)
        if hit is None:
            self.clear_selection()
            self.face_picked.emit(None)
            self.render()
            return
        name, fid, wp, lp = hit
        self.highlight(name, fid)
        face = self.objs[name].faces[fid]
        n = face.normal_at()
        c = face.center()
        self.face_picked.emit({
            "object": name, "index": fid, "type": face.geom_type.name.lower(), "area": face.area,
            "normal": (n.X, n.Y, n.Z), "center": (c.X, c.Y, c.Z),
            "point": tuple(wp), "local": tuple(lp),
        })
        self.render()

    def highlight(self, name: str, fid: int, color=None):
        oa = self.objs.get(name)
        if oa is None or oa.pd is None:
            return
        th = vtk.vtkThreshold()
        th.SetInputData(oa.pd)
        th.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS, "face_id")
        th.SetLowerThreshold(fid - 0.5)
        th.SetUpperThreshold(fid + 0.5)
        geo = vtk.vtkGeometryFilter()
        geo.SetInputConnection(th.GetOutputPort())
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(geo.GetOutputPort())
        m.ScalarVisibilityOff()
        m.SetResolveCoincidentTopologyToPolygonOffset()
        m.SetRelativeCoincidentTopologyPolygonOffsetParameters(-1, -1)
        self.select_actor.SetMapper(m)
        self.select_actor.SetUserMatrix(vtk_matrix(oa.pose))
        self.select_actor.GetProperty().SetColor(*(color or theme.SELECT_COLOR))
        self.select_actor.SetVisibility(True)
        self._sel_obj = name

    def highlight_faces(self, name: str, fids, color=None):
        """Highlight several faces of one object (fixtures/loads being picked)."""
        oa = self.objs.get(name)
        if oa is None or oa.pd is None or not fids:
            self.clear_selection()
            return
        from vtkmodules.util.numpy_support import vtk_to_numpy
        ids = vtk_to_numpy(oa.pd.GetCellData().GetArray("face_id"))
        sel = np.where(np.isin(ids, list(fids)))[0]
        idl = vtk.vtkIdList()
        for c in sel:
            idl.InsertNextId(int(c))
        ex = vtk.vtkExtractCells()
        ex.SetInputData(oa.pd)
        ex.SetCellList(idl)
        geo = vtk.vtkGeometryFilter()
        geo.SetInputConnection(ex.GetOutputPort())
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(geo.GetOutputPort())
        m.ScalarVisibilityOff()
        m.SetResolveCoincidentTopologyToPolygonOffset()
        m.SetRelativeCoincidentTopologyPolygonOffsetParameters(-1, -1)
        self.select_actor.SetMapper(m)
        self.select_actor.SetUserMatrix(vtk_matrix(oa.pose))
        self.select_actor.GetProperty().SetColor(*(color or theme.SELECT_COLOR))
        self.select_actor.SetVisibility(True)
        self._sel_obj = name
        self.render()

    def clear_selection(self):
        self.select_actor.SetVisibility(False)
        self._sel_obj = None

    def start(self):
        self.vtk.Initialize()
        self.vtk.Start()
