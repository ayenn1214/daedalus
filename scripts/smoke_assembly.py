"""Assembly in the GUI with real input: Mate mode (click pin, click arm end → Concentric from the
menu), Move mode drag of the arm tip, Drive dialog animation, interference check.
Usage: python scripts/smoke_assembly.py out.png"""
import math
import os
import sys
import traceback

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from daedalus.core.assembly import rotmat
from daedalus.ui.assembly_ui import DriveDialog
from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "asm.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
d = w.doc
vw = w.view.vtk


def world(obj, p):
    o = d.obj(obj)
    return rotmat(o.pose[3:]) @ np.asarray(p, float) + np.asarray(o.pose[:3])


def qpos(wp):
    r = w.view.ren
    r.SetWorldPoint(float(wp[0]), float(wp[1]), float(wp[2]), 1.0)
    r.WorldToDisplay()
    x, y, _ = r.GetDisplayPoint()
    return QPoint(round(x), round(vw.height() - 1 - y))


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def pick_menu(text):
    """Click the menu action containing `text` once the popup is up."""
    def go():
        m = QApplication.activePopupWidget()
        if isinstance(m, QMenu):
            for a in m.actions():
                if text.lower() in a.text().lower():
                    m.setActiveAction(a)
                    QTest.keyClick(m, Qt.Key_Return)
                    return
            m.close()
        else:
            QTimer.singleShot(50, go)
    QTimer.singleShot(80, go)


def run():
    # parts
    d.set_object("Object1", new_name="Base")
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 100}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    d.add("sketch", {"plane": "top", "entities": [{"type": "circle", "x": -80, "y": 0, "r": 5}]})
    d.add("extrude", {"sketch": "Sketch2", "depth": 30})
    w.new_object()
    d.set_object(d.active_object, new_name="Arm", color="#d9822b", pose=[0, 150, 40, 0, 0, 0.4])
    d.add("sketch", {"entities": [{"type": "slot", "x": 0, "y": 0, "x2": 150, "y2": 0, "w": 30},
                                  {"type": "circle", "x": 0, "y": 0, "r": 5.1, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch3", "depth": 8})
    w._redraw()
    w.view._has_framed = False
    w.view.set_view("Isometric")
    QTest.qWait(300)
    compose(out.replace(".png", "_1_parts.png"))
    print("objects:", [o.name for o in d.objects], "errors:", d.result.errors)

    # Mate mode with real clicks: pin side, then the arm's rounded end → Concentric
    w.asm.set_mode("mate")
    QTest.qWait(50)
    pin_pt = [-80 + 5 * math.cos(math.radians(-60)), 5 * math.sin(math.radians(-60)), 25]
    pq = qpos(pin_pt)
    QTest.mouseClick(vw, Qt.LeftButton, Qt.NoModifier, pq)
    QTest.qWait(50)
    print("first pick:", w.asm._first and w.asm._first[:2], "| pick_info at pin:",
          w.view.pick_info(pq.x(), vw.height() - 1 - pq.y()) and w.view.pick_info(pq.x(), vw.height() - 1 - pq.y())[:2],
          "| status:", w.statusBar().currentMessage()[:80])
    end_local = [15 * math.cos(math.radians(-120)), 15 * math.sin(math.radians(-120)), 4]
    pick_menu("Concentric")
    QTest.mouseClick(vw, Qt.LeftButton, Qt.NoModifier, qpos(world("Arm", end_local)))
    QTest.qWait(300)
    print("after clicked mate:", [(m.name, m.type, m.a["ref"]["kind"], m.b["ref"]["kind"]) for m in d.mates])
    w.asm.set_mode(None)
    # seat it on the plate (bottom face isn't visible from here, so via the API)
    d.add_mate("coincident", {"obj": "Base", "ref": {"face_near": [50, 0, 10], "normal": [0, 0, 1]}},
               {"obj": "Arm", "ref": {"face": "bottom"}})
    res = d.result.assembly
    print("assembly:", "ok" if res.ok else "FAIL", "dof", res.dof, "hole centre", np.round(world("Arm", [0, 0, 0]), 3))
    w._redraw()
    QTest.qWait(100)

    # Move mode: real drag of the arm tip toward +Y
    w.asm.set_mode("move")
    QTest.qWait(50)
    tip0 = world("Arm", [140, 0, 8])
    a = qpos(tip0)
    target = np.array([-80 + 10, 140, 18])
    b = qpos(target)
    QTest.mousePress(vw, Qt.LeftButton, Qt.NoModifier, a)
    for i in range(1, 21):
        p = QPoint(round(a.x() + (b.x() - a.x()) * i / 20), round(a.y() + (b.y() - a.y()) * i / 20))
        QTest.mouseMove(vw, p)
        QTest.qWait(10)
    QTest.mouseRelease(vw, Qt.LeftButton, Qt.NoModifier, b)
    QTest.qWait(150)
    tip = world("Arm", [150, 0, 4])
    grab = world("Arm", [140, 0, 8])
    gq = qpos(grab)
    print("dragged tip:", np.round(tip, 1), "radius", round(math.hypot(tip[0] + 80, tip[1]), 2),
          "z", round(tip[2], 3), "| grabbed point on screen", (gq.x(), gq.y()), "cursor", (b.x(), b.y()),
          "off by", round(math.hypot(gq.x() - b.x(), gq.y() - b.y()), 1), "px | undo steps", len(d._undo))
    w.asm.set_mode(None)
    compose(out.replace(".png", "_2_mated_dragged.png"))

    # Drive: angle mate between the plate's front face and the arm's front face
    d.add_mate("angle", {"obj": "Base", "ref": {"face": "front"}}, {"obj": "Arm", "ref": {"face": "front"}},
               value=45)
    dlg = DriveDialog(d, w.view, "Angle1", w)
    dlg.start.setValue(5)
    dlg.end.setValue(175)
    dlg.steps.setValue(34)
    dlg.secs.setValue(1.0)
    dlg.show()
    dlg.play()
    print("drive:", dlg.info.text())
    QTest.qWait(450)
    compose(out.replace(".png", "_3_driving.png"))
    QTest.qWait(1200)
    print("after drive, angle mate =", round(d.mate("Angle1").value, 2), "frames ok:",
          all(f["_ok"] for f in dlg.frames))
    dlg.interference()
    print("interference:", dlg.info.text())
    dlg.close()
    w._redraw()
    w.side_tabs.setCurrentWidget(w.mates_panel)
    w.history.refresh(select="Extrude3")
    QTest.qWait(150)
    compose(out)
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
