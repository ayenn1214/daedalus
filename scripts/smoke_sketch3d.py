"""Drives the 3D sketch editor with real mouse/keyboard events in the VTK viewport:
draws a cart base loop on XY, two posts and a handle rail locked along Z/Y,
dimensions it, finishes, then builds a Frame and a swept handle grip.
Usage: python scripts/smoke_sketch3d.py out.png"""
import os
import sys
import traceback

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "s3d.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
ctl = w.s3d
vw = w.view.vtk


def qpos(xyz):
    dx, dy = ctl._to_display(xyz)
    return QPoint(round(dx), round(vw.height() - 1 - dy))


def click(xyz):
    p = qpos(xyz)
    QTest.mouseMove(vw, p)
    QTest.qWait(15)
    QTest.mouseClick(vw, Qt.LeftButton, Qt.NoModifier, p)
    QTest.qWait(15)


def key(k):
    QTest.keyClick(vw, k)
    QTest.qWait(10)


def snap(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def run():
    w.add("sketch3d")
    assert w._sketching and ctl.active
    cam = w.view.ren.GetActiveCamera()
    cam.SetFocalPoint(300, 200, 150)
    cam.SetPosition(300 + 900, 200 - 1300, 150 + 800)
    cam.SetViewUp(0, 0, 1)
    w.view.ren.ResetCameraClippingRange()
    w.view.render()
    QTest.qWait(50)
    # base loop on XY, closing on the start point
    QTest.qWait(300)            # let the window layout settle after entering sketch mode
    for xyz in [(0, 0, 0), (600, 0, 0), (600, 400, 0), (0, 400, 0), (0, 0, 0)]:
        click(xyz)
    print("after loop:", [c["id"] for c in ctl.geom["curves"]], ctl.result.status, ctl.result.dof)
    # posts: start on a corner, lock Z
    for corner in ((600, 400, 0), (0, 400, 0)):
        click(corner)
        key(Qt.Key_Z)
        click((corner[0], corner[1], 300))
        QTest.mouseClick(vw, Qt.RightButton, Qt.NoModifier, qpos(corner))     # end this chain
    # handle rail between post tops (snaps to both endpoints → along X auto)
    click((0, 400, 300))
    click((600, 400, 300))
    QTest.mouseClick(vw, Qt.RightButton, Qt.NoModifier, qpos((0, 0, 0)))   # end chain
    g = ctl.geom
    print("curves:", len(g["curves"]), "relations:", sorted({k['type'] for k in g['constraints']}),
          "status:", ctl.result.status, ctl.result.dof)
    snap(out.replace(".png", "_drawn.png"))
    # dimensions (the Dimension button calls add_dimension after its dialog)
    lines = {c["id"]: c for c in g["curves"]}
    ctl.add_dimension("length", ["l1"], 600)
    ctl.add_dimension("length", ["l2"], 400)
    ctl.add_dimension("length", ["l5"], 300)
    ctl.sel = {"l5", "l6"}
    ctl.apply_relation("equal")
    print("dimensioned:", ctl.result.status, ctl.result.dof, "redundant:", ctl.result.redundant)
    # an over-defining dimension must bounce
    n = len(ctl.geom["constraints"])
    ctl.add_dimension("length", ["l3"], 590)
    print("conflict rejected:", len(ctl.geom["constraints"]) == n)
    # drag test: select tool, drag a free point? (sketch is fully defined → nothing moves)
    ctl.set_tool("select")
    ctl.sel = {"l7"}
    ctl._redraw()
    w.s3d_bar.sync()
    QTest.qWait(50)
    snap(out.replace(".png", "_editing.png"))
    w.s3d_bar.finished.emit(ctl.geometry())
    QTest.qWait(50)
    print("after finish:", w.doc.result.errors, w.doc.result.sketch_status)
    import json; print(json.dumps(w.doc.features[0].params["geometry"]))
    w.add("frame")
    QTest.qWait(30)
    f = w.doc.features[-1]
    w.doc.edit(f.name, {"profile": "square_tube", "width": 25.4, "thickness": 1.65})
    print("frame errors:", w.doc.result.errors, "mass kg:", w.doc.mass_properties()["mass_kg"])
    for r in w.doc.cut_list():
        print("  cut:", r)
    # undo removes the frame, redo restores it
    w._undo()
    print("undo → features:", [x.name for x in w.doc.features])
    w._redo()
    w._redraw()
    w.history.refresh(select=f.name)
    w.inspector.show_feature(f.name)
    w.view.set_view("Isometric")
    QTest.qWait(100)
    snap(out)
    print("saved", flush=True)
    os._exit(0)


def safe():
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)


QTimer.singleShot(300, safe)
app.exec()
