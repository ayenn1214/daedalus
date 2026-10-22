"""Simulation results in the 3D view: contour plots on the (deformed) mesh, mode animation,
flow slices and streamlines, legend and title."""
from __future__ import annotations

import numpy as np
from . import vtklite as vtk
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray

from .viewport import vtk_matrix
from . import theme


def _lut(log=False, reverse=False, n=256):
    lut = vtk.vtkLookupTable()
    lut.SetNumberOfTableValues(n)
    # blue → cyan → green → yellow → red (the familiar FEA rainbow, slightly desaturated)
    stops = [(0.0, (0.13, 0.25, 0.80)), (0.25, (0.10, 0.65, 0.90)), (0.5, (0.20, 0.78, 0.35)),
             (0.75, (0.98, 0.82, 0.15)), (1.0, (0.88, 0.18, 0.12))]
    for i in range(n):
        t = i / (n - 1)
        if reverse:
            t = 1 - t
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= t <= b:
                u = (t - a) / (b - a)
                c = [ca[k] + (cb[k] - ca[k]) * u for k in range(3)]
                break
        lut.SetTableValue(i, *c, 1.0)
    if log:
        lut.SetScaleToLog10()
    lut.Build()
    return lut


class ResultOverlay:
    def __init__(self, viewport):
        self.vp = viewport
        ren = viewport.ren
        self.surf = vtk.vtkActor()
        self.edges = vtk.vtkActor()
        self.ghost = vtk.vtkActor()
        self.flow = vtk.vtkActor()
        self.lines = vtk.vtkActor()
        for a in (self.surf, self.edges, self.ghost, self.flow, self.lines):
            a.SetVisibility(False)
            a.PickableOff()
            ren.AddActor(a)
        self.edges.GetProperty().SetColor(*theme.EDGE_COLOR)
        self.edges.GetProperty().SetLineWidth(1.0)
        self.edges.GetProperty().SetLighting(False)
        g = self.ghost.GetProperty()
        g.SetRepresentationToWireframe()
        g.SetColor(*theme.VIEW_GHOST)
        g.SetOpacity(0.25)
        g.SetLighting(False)
        self.lines.GetProperty().SetLineWidth(1.6)
        self.bar = vtk.vtkScalarBarActor()
        self.bar.SetNumberOfLabels(7)
        self.bar.SetWidth(0.09)
        self.bar.SetHeight(0.55)
        self.bar.SetPosition(0.9, 0.2)
        self.bar.SetLabelFormat("%-#8.4g")
        self.bar.SetVerticalTitleSeparation(14)
        self.bar.SetHeight(0.5)
        for tp in (self.bar.GetLabelTextProperty(), self.bar.GetTitleTextProperty()):
            tp.SetColor(*theme.VIEW_TEXT)
            tp.ShadowOff()
            tp.ItalicOff()
            tp.SetFontSize(12)
        self.bar.GetTitleTextProperty().BoldOn()
        self.bar.SetVisibility(False)
        ren.AddViewProp(self.bar)
        self.title = vtk.vtkTextActor()
        self.title.GetTextProperty().SetColor(*theme.VIEW_TEXT)
        self.title.GetTextProperty().SetFontSize(14)
        self.title.SetPosition(14, 14)
        self.title.SetVisibility(False)
        ren.AddViewProp(self.title)
        self.active = False
        self._base = None
        self._pd = None
        self._vec = None

    # ----------------------------------------------------------- fields
    def show_field(self, p, tris, values, label, unit="", vectors=None, scale=0.0, pose=None, rng=None,
                   log=False, reverse=False, title="", hide=()):
        p = np.asarray(p, float)
        pd = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetData(numpy_to_vtk(np.ascontiguousarray(p), deep=True))
        pd.SetPoints(pts)
        tris = np.asarray(tris, np.int64)
        cells = np.hstack([np.full((len(tris), 1), 3, np.int64), tris]).ravel()
        ca = vtk.vtkCellArray()
        ca.SetCells(len(tris), numpy_to_vtkIdTypeArray(cells, deep=True))
        pd.SetPolys(ca)
        vals = np.asarray(values, float)
        arr = numpy_to_vtk(np.ascontiguousarray(vals), deep=True)
        arr.SetName("v")
        pd.GetPointData().SetScalars(arr)
        self._pd, self._base, self._vec = pd, p.copy(), (None if vectors is None else np.asarray(vectors, float))
        self.set_scale(scale, render=False)
        finite = vals[np.isfinite(vals)]
        lo, hi = rng if rng else ((finite.min(), finite.max()) if finite.size else (0, 1))
        if log:
            lo = max(lo, hi * 1e-6, 1e-12)
        if hi - lo < 1e-12:
            hi = lo + 1e-12
        lut = _lut(log, reverse)
        lut.SetRange(lo, hi)
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(pd)
        m.SetLookupTable(lut)
        m.SetScalarRange(lo, hi)
        m.SetInterpolateScalarsBeforeMapping(True)
        m.ScalarVisibilityOn()
        self.surf.SetMapper(m)
        self.surf.GetProperty().SetAmbient(0.35)
        self.surf.GetProperty().SetDiffuse(0.7)
        self.surf.GetProperty().SetSpecular(0.1)
        fe = vtk.vtkFeatureEdges()
        fe.SetInputData(pd)
        fe.BoundaryEdgesOn()
        fe.FeatureEdgesOn()
        fe.SetFeatureAngle(70)
        fe.ManifoldEdgesOff()
        fe.NonManifoldEdgesOff()
        em = vtk.vtkPolyDataMapper()
        em.SetInputConnection(fe.GetOutputPort())
        em.ScalarVisibilityOff()
        self.edges.SetMapper(em)
        gpd = vtk.vtkPolyData()
        gpts = vtk.vtkPoints()
        gpts.SetData(numpy_to_vtk(np.ascontiguousarray(p), deep=True))
        gpd.SetPoints(gpts)
        gpd.SetPolys(ca)
        gfe = vtk.vtkFeatureEdges()
        gfe.SetInputData(gpd)
        gfe.BoundaryEdgesOn()
        gfe.FeatureEdgesOn()
        gfe.SetFeatureAngle(70)
        gfe.ManifoldEdgesOff()
        gm = vtk.vtkPolyDataMapper()
        gm.SetInputConnection(gfe.GetOutputPort())
        gm.ScalarVisibilityOff()
        self.ghost.SetMapper(gm)
        mat = vtk_matrix(pose or [0.0] * 6)
        for a in (self.surf, self.edges, self.ghost):
            a.SetUserMatrix(mat)
        self.surf.SetVisibility(True)
        self.edges.SetVisibility(True)
        self.ghost.SetVisibility(bool(vectors is not None and scale))
        self.flow.SetVisibility(False)
        self.lines.SetVisibility(False)
        self.bar.SetLookupTable(lut)
        self.bar.SetTitle(f"{label} ({unit})" if unit else label)
        self.bar.SetVisibility(True)
        self.title.SetInput(title)
        self.title.SetVisibility(bool(title))
        self.active = True
        self.vp.result_hidden = set(hide)
        self.vp.apply_display_mode()

    def set_scale(self, scale, phase=1.0, render=True):
        if self._pd is None:
            return
        P = self._base if (self._vec is None or not scale) else self._base + self._vec * (scale * phase)
        self._pd.GetPoints().SetData(numpy_to_vtk(np.ascontiguousarray(P), deep=True))
        self._pd.Modified()
        self.ghost.SetVisibility(bool(self._vec is not None and scale) and self.active)
        if render:
            self.vp.render()

    # ------------------------------------------------------------- flow
    def show_flow(self, res, field="speed", axis=2, frac=0.5, streamlines=True, title=""):
        from ..sim.cfd import slice_plane
        P, sp, pr = slice_plane(res, axis, frac)
        vals = sp if field == "speed" else pr
        h, w = vals.shape                    # (n along axis a, n along axis b); b varies fastest
        pts = vtk.vtkPoints()
        pts.SetData(numpy_to_vtk(np.ascontiguousarray(P.reshape(-1, 3)), deep=True))
        sg = vtk.vtkStructuredGrid()
        sg.SetDimensions(w, h, 1)
        sg.SetPoints(pts)
        v = np.ascontiguousarray(vals.reshape(-1).astype(float))
        arr = numpy_to_vtk(v, deep=True)
        arr.SetName("v")
        sg.GetPointData().SetScalars(arr)
        finite = v[np.isfinite(v)]
        lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        if field != "speed":
            a = max(abs(lo), abs(hi))
            lo, hi = -a, a
        lut = _lut()
        lut.SetRange(lo, hi)
        lut.SetNanColor(0.55, 0.57, 0.6, 0.0)
        geo = vtk.vtkStructuredGridGeometryFilter()
        geo.SetInputData(sg)
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(geo.GetOutputPort())
        m.SetLookupTable(lut)
        m.SetScalarRange(lo, hi)
        m.ScalarVisibilityOn()
        self.flow.SetMapper(m)
        self.flow.GetProperty().SetOpacity(0.92)
        self.flow.GetProperty().SetLighting(False)
        self.flow.SetUserMatrix(vtk.vtkMatrix4x4())
        self.flow.SetVisibility(True)
        self.lines.SetVisibility(False)
        if streamlines:
            self._streamlines(res, lut if field == "speed" else None, lo, hi)
        for a in (self.surf, self.edges, self.ghost):
            a.SetVisibility(False)
        self.bar.SetLookupTable(lut)
        self.bar.SetTitle("velocity (m/s)" if field == "speed" else "pressure (Pa)")
        self.bar.SetVisibility(True)
        self.title.SetInput(title)
        self.title.SetVisibility(bool(title))
        self.active = True
        self.vp.result_hidden = set()
        self.vp.apply_display_mode()

    def _streamlines(self, res, lut, lo, hi):
        img = vtk.vtkImageData()
        perm, sign = res.axes
        nx, ny, nz = res.shape
        # image data in world axes needs grid axes == world axes; build it in grid space then transform
        img.SetDimensions(nx, ny, nz)
        img.SetSpacing(res.spacing, res.spacing, res.spacing)
        vel_grid = np.stack([res.velocity[perm[k]] * sign[k] for k in range(3)])     # grid-axis comps
        V = np.ascontiguousarray(vel_grid.transpose(3, 2, 1, 0).reshape(-1, 3).astype(float))
        va = numpy_to_vtk(V, deep=True)
        va.SetName("vel")
        img.GetPointData().SetVectors(va)
        sp = np.ascontiguousarray(np.linalg.norm(V, axis=1))
        sa = numpy_to_vtk(sp, deep=True)
        sa.SetName("speed")
        img.GetPointData().AddArray(sa)
        seeds = vtk.vtkPointSource()
        seeds.SetCenter(res.spacing * 2, res.spacing * ny / 2, res.spacing * nz / 2)
        seeds.SetRadius(res.spacing * min(ny, nz) * 0.3)
        seeds.SetNumberOfPoints(160)
        if res.summary.get("mode") == "internal":
            seeds.SetRadius(res.spacing * min(ny, nz) * 0.45)
        st = vtk.vtkStreamTracer()
        st.SetInputData(img)
        st.SetSourceConnection(seeds.GetOutputPort())
        st.SetMaximumPropagation(res.spacing * nx * 2)
        st.SetIntegrationDirectionToForward()
        st.SetIntegratorTypeToRungeKutta45()
        st.SetInitialIntegrationStep(0.3)
        st.Update()
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(st.GetOutputPort())
        if lut is not None:
            m.SetLookupTable(lut)
            m.SetScalarRange(lo, hi)
            m.SetScalarModeToUsePointFieldData()
            m.SelectColorArray("speed")
            m.ScalarVisibilityOn()
        else:
            m.ScalarVisibilityOff()
            self.lines.GetProperty().SetColor(*theme.VIEW_LINES)
        self.lines.SetMapper(m)
        # grid space → world: axis permutation, flips and origin
        M = vtk.vtkMatrix4x4()
        for k in range(3):
            for j in range(3):
                M.SetElement(j, k, 0.0)
            M.SetElement(perm[k], k, float(sign[k]))
        for j in range(3):
            M.SetElement(j, 3, float(res.origin[j]))
        self.lines.SetUserMatrix(M)
        self.lines.SetVisibility(True)

    # ------------------------------------------------------------ clear
    def clear(self):
        for a in (self.surf, self.edges, self.ghost, self.flow, self.lines):
            a.SetVisibility(False)
        self.bar.SetVisibility(False)
        self.title.SetVisibility(False)
        self.active = False
        self._pd = None
        self.vp.result_hidden = set()
        self.vp.apply_display_mode()
