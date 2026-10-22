"""Simulation in the GUI: material picker, static stress on an L-bracket (faces picked in the panel),
frequency, fatigue, thermal and a quick flow run. Usage: python scripts/smoke_sim.py out.png"""
import os
import sys
import time
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from daedalus.ui.main_window import MainWindow
from daedalus.ui.materials_ui import MaterialDialog
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "sim.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
d = w.doc
P = w.studies_panel


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def wait(limit=600):
    t0 = time.time()
    while P.thread is not None and time.time() - t0 < limit:
        time.sleep(0.03)
        app.processEvents()
    app.processEvents()


def face(spec):
    return d.ref_on(d.active_object, spec)["face"]


def run():
    # L-bracket 80×40 base, 60 tall upright, 6 thick, Ø10 hole in the upright
    d.add("sketch", {"plane": "XZ", "entities": [{"type": "polygon", "points": [[0, 0], [80, 0], [80, 6], [6, 6],
                                                                               [6, 60], [0, 60]]}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 40, "end": "mid_plane"})
    d.add("sketch", {"plane": "YZ", "offset": 6, "entities": [{"type": "circle", "x": 0, "y": 45, "r": 5}]})
    d.add("extrude", {"sketch": "Sketch2", "operation": "cut", "end": "through_all"})
    print("errors:", d.result.errors)
    dlg = MaterialDialog(d, None, w)
    dlg.search.setText("6061")
    dlg._fill()
    dlg._select("Aluminum 6061-T6")
    dlg.apply()
    print("material:", d.material, d.mass_properties()["mass_kg"], "kg")
    w.new_study("static")
    P.start_pick("fixtures", "fixed")
    P.on_pick({"object": d.active_object, "index": face({"face": "bottom"})})
    P._commit_pick()
    P.start_pick("loads", "bearing")
    P.on_pick({"object": d.active_object, "index": face({"axis_near": [0, 0, 45]})})
    P.pick_ws["value"].setValue(800)
    P.pick_ws["direction"].setCurrentText("+X")
    P._commit_pick()
    print("static study:", [(i["type"], len(i.get("refs", []))) for i in d.study("Static1")["fixtures"] + d.study("Static1")["loads"]])
    P.run()
    wait()
    print("msg:", P.msg.text())
    r = P.result
    print("static:", P.msg.text(), "|", r.summary["max_von_mises_MPa"], "MPa, disp", r.summary["max_displacement_mm"],
          "mm, FoS", r.summary["min_factor_of_safety"], "| reactions", r.summary["reactions"][0]["resultant_N"])
    w.view.set_view("Isometric")
    w.view.fit()
    QTest.qWait(150)
    compose(out)
    P.field.setCurrentText("factor of safety")
    QTest.qWait(50)
    compose(out.replace(".png", "_fos.png"))
    # fatigue from the static study
    w.new_study("fatigue")
    d.set_study("Fatigue1", cycles=1e7, loading="zero")
    P.run()
    wait()
    print("fatigue:", P.result.summary["min_life_cycles"], P.result.summary["min_fatigue_FoS"], P.msg.text())
    # frequency
    w.new_study("frequency")
    P.start_pick("fixtures", "fixed")
    P.on_pick({"object": d.active_object, "index": face({"face": "bottom"})})
    P._commit_pick()
    P.run()
    wait()
    print("frequency:", P.result.summary["frequencies_Hz"])
    QTest.qWait(300)
    compose(out.replace(".png", "_mode.png"))
    # thermal: hot upright face, convection elsewhere
    w.new_study("thermal")
    P.start_pick("thermal", "heat_power")
    P.on_pick({"object": d.active_object, "index": face({"face": "left"})})
    P.pick_ws["value"].setValue(20)
    P._commit_pick()
    P.start_pick("thermal", "temperature")
    P.on_pick({"object": d.active_object, "index": face({"face": "bottom"})})
    P.pick_ws["value"].setValue(25)
    P._commit_pick()
    P.run()
    wait()
    print("thermal:", P.result.summary["min_temperature_C"], "…", P.result.summary["max_temperature_C"])
    compose(out.replace(".png", "_thermal.png"))
    # flow (coarse)
    w.new_study("flow")
    d.set_study("Flow1", velocity=5.0, resolution=10, flow_throughs=1.5, direction="+Y")
    t0 = time.time()
    P.run()
    wait()
    print("flow:", round(time.time() - t0), "s", P.msg.text(), "|",
          P.result.summary.get("objects") if P.result else None)
    P.axis.setCurrentIndex(0)
    QTest.qWait(100)
    w.view.fit()
    compose(out.replace(".png", "_flow.png"))
    # back to the static result for the main shot
    P.combo.setCurrentText("Static1")
    QTest.qWait(100)
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
