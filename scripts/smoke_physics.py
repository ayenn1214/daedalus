"""Physics in the GUI: a crank–rocker four-bar driven by a motor, plus a block dropped onto the base.
Usage: python scripts/smoke_physics.py out.png"""
import math
import os
import sys
import traceback

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "physics.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
d = w.doc


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def inter(c1, r1, c2, r2):
    dd = np.linalg.norm(c2 - c1)
    a = (r1 * r1 - r2 * r2 + dd * dd) / (2 * dd)
    h = math.sqrt(r1 * r1 - a * a)
    p = c1 + a * (c2 - c1) / dd
    return p + h * np.array([-(c2 - c1)[1], (c2 - c1)[0]]) / dd


def build():
    O1, O2, A = np.array([0, 0.]), np.array([100, 0.]), np.array([0, 40.])
    B = inter(A, 100, O2, 80)
    d.set_object("Object1", new_name="Base")
    d.add("sketch", {"entities": [{"type": "rect", "x": 50, "y": -10, "w": 260, "h": 120}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    for p in (O1, O2):
        d.add("sketch", {"plane": "XY", "offset": 10, "entities": [{"type": "circle", "x": p[0], "y": p[1], "r": 4}]})
        d.add("extrude", {"sketch": d.last_of("sketch"), "depth": 4.5})

    def link(name, p, q, z, col):
        d.add_object(name, col)
        d.add("sketch", {"plane": "XY", "offset": z, "entities": [
            {"type": "slot", "x": p[0], "y": p[1], "x2": q[0], "y2": q[1], "w": 16},
            {"type": "circle", "x": p[0], "y": p[1], "r": 4, "mode": "subtract"},
            {"type": "circle", "x": q[0], "y": q[1], "r": 4, "mode": "subtract"}]}, obj=name)
        d.add("extrude", {"sketch": d.last_of("sketch"), "depth": 5}, obj=name)

    link("Crank", O1, A, 10, "#d9822b")
    link("Rocker", O2, B, 10, "#5aa469")
    link("Coupler", A, B, 15, "#4f9fd9")

    def hinge(a, pa, b, pb, z):
        d.add_mate("concentric", {"obj": a, "ref": {"axis_near": [pa[0], pa[1], z]}},
                   {"obj": b, "ref": {"axis_near": [pb[0], pb[1], z]}})
        d.add_mate("coincident", {"obj": a, "ref": {"face_near": [pa[0] + (20 if a == "Base" else 8.5), pa[1], z],
                                                   "normal": [0, 0, 1]}},
                   {"obj": b, "ref": {"face_near": [pb[0] + 6, pb[1], z], "normal": [0, 0, -1]}})

    hinge("Base", O1, "Crank", O1, 10)
    hinge("Base", O2, "Rocker", O2, 10)
    hinge("Crank", A, "Coupler", A, 15)
    d.add_mate("concentric", {"obj": "Coupler", "ref": {"axis_near": [B[0], B[1], 15]}},
               {"obj": "Rocker", "ref": {"axis_near": [B[0], B[1], 15]}})
    d.add_object("Block", "#c94f7c", pose=[150, -40, 60, 0.4, 0.3, 0.2])
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 30, "h": 20}]}, obj="Block")
    d.add("extrude", {"sketch": d.last_of("sketch"), "depth": 15}, obj="Block")


def wait_sim(limit=300):
    import time
    t0 = time.time()
    while w.physics_panel.thread is not None and time.time() - t0 < limit:
        time.sleep(0.03)                              # releases the GIL for the worker (qWait doesn't)
        app.processEvents()


def run():
    build()
    print("dof:", d.result.assembly.dof, "errors:", d.result.errors)
    w.side_tabs.setCurrentWidget(w.physics_panel)
    QTest.qWait(50)
    w.physics_panel.add_mate.setCurrentText("Concentric1")
    w.physics_panel._add_drive()                     # + Drive → speed 90 deg/s
    QTest.qWait(50)
    print("drives:", [x.to_dict() for x in d.physics.drives], "table rows:", w.physics_panel.table.rowCount())
    d.set_physics(duration=3.0)
    w.simulate()
    import time
    t0 = time.time()
    wait_sim()
    print("sim wall time:", round(time.time() - t0, 1), "s | panel:", w.physics_panel.info.text().splitlines()[:1])
    r = w.physics_panel.result
    print("ok:", r.ok, "joints:", r.joints)
    print("notes:", r.notes)
    print("crank travel:", r.summary["Concentric1"], "| block:", r.summary["moved_mm"]["Block"], "settled:",
          r.summary["settled"], "| contacts:", r.summary["contacts_between"])
    QTest.qWait(400)                                 # playing
    w.physics_panel.pause()
    w.physics_panel.slider.setValue(int(len(r.frames) * 0.55))
    w.physics_panel.trace.setCurrentText("Concentric1 torque (N·m)" if False else
                                         next(n for n in r.traces if n.startswith("Rocker speed")))
    QTest.qWait(100)
    w.view.set_view("Isometric")
    w.view.fit()
    QTest.qWait(100)
    compose(out)
    before = list(d.obj("Block").pose)
    w.physics_panel.slider.setValue(len(r.frames) - 1)
    w.physics_panel.keep()
    print("kept block z:", round(before[2], 1), "→", round(d.obj("Block").pose[2], 2))
    d.undo()
    print("undo block z:", round(d.obj("Block").pose[2], 1))
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
