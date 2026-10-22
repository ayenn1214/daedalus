"""Off-screen preview images for standard parts (VTK, no Qt)."""
from __future__ import annotations

from pathlib import Path


def render_png(bodies, path: str | Path, size: int = 320):
    import vtk

    from ..ui.tessellate import body_polydata, edges_polydata
    ren = vtk.vtkRenderer()
    from ..ui import theme
    ren.SetBackground(*theme.THUMB_BG[0])
    ren.SetBackground2(*theme.THUMB_BG[1])
    ren.GradientBackgroundOn()
    for b in bodies:
        a = vtk.vtkActor()
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(body_polydata(b, 0.1, 0.3)[0])
        m.ScalarVisibilityOff()
        a.SetMapper(m)
        p = a.GetProperty()
        p.SetColor(*theme.THUMB_BODY)
        p.SetSpecular(0.25)
        p.SetSpecularPower(25)
        ren.AddActor(a)
        e = vtk.vtkActor()
        em = vtk.vtkPolyDataMapper()
        em.SetInputData(edges_polydata(b, 0.5))
        e.SetMapper(em)
        e.GetProperty().SetColor(*theme.THUMB_EDGE)
        e.GetProperty().SetLineWidth(1.2)
        ren.AddActor(e)
    cam = ren.GetActiveCamera()
    cam.SetPosition(1, -1.2, 0.9)
    cam.SetFocalPoint(0, 0, 0)
    cam.SetViewUp(0, 0, 1)
    ren.ResetCamera()
    cam.Zoom(1.05)
    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.AddRenderer(ren)
    rw.SetSize(size, size)
    rw.SetMultiSamples(4)
    rw.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rw)
    w2i.Update()
    wr = vtk.vtkPNGWriter()
    wr.SetFileName(str(path))
    wr.SetInputConnection(w2i.GetOutputPort())
    wr.Write()
    rw.Finalize()
