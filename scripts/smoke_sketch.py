"""Drives the sketch editor with real mouse events under a (virtual) display:
draw a rectangle + circle + slot-ish arc profile, dimension it fully, finish,
extrude, then edit a dimension from the Inspector. Saves screenshots."""
import os
import sys

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "sketch.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1500, 900)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)


def click(canvas, x, y, button=Qt.LeftButton):
    p = canvas.w2s(x, y)
    pt = QPoint(round(p.x()), round(p.y()))
    QTest.mouseMove(canvas, pt)
    QTest.mouseClick(canvas, button, Qt.NoModifier, pt)
    QTest.qWait(10)


def run():
    w.new_sketch(plane="XY")              # opens the editor on a new sketch (no plane prompt)
    cv = w.sketch_editor.canvas
    cv.scale, cv.off = 4.0, cv.off.__class__(cv.width() / 2, cv.height() / 2)
    assert w._sketching
    cv.set_tool("rect")
    click(cv, -62, -38)
    click(cv, 58, 33)
    cv.set_tool("circle")
    click(cv, 0, 0)                       # snaps to origin
    click(cv, 9, 0)
    cv.set_tool("line")                   # a triangle cut-out, closed by snapping
    click(cv, -45, -20)
    click(cv, -25, -20)
    click(cv, -45, 10)
    click(cv, -45, -20)
    g = cv.geom
    print("curves", [c["id"] for c in g["curves"]], "status", cv.result.status, cv.result.dof)
    # dimensions (same calls the Dimension tool makes after its value dialog)
    cv.add_dimension("length", ["l1"], 120)
    cv.add_dimension("length", ["l2"], 70)
    cv.add_dimension("diameter", ["c1"], 20)
    rect_corner = cv.geom["curves"][0]["a"]
    cv.add_dimension("hdistance", [rect_corner, "O"], 60)
    cv.add_dimension("vdistance", [rect_corner, "O"], 35)
    tri = [c for c in cv.geom["curves"] if c["id"] in ("l5", "l6", "l7")]
    cv.add_dimension("length", ["l5"], 20)
    cv.add_dimension("length", ["l7"], 30)
    a = tri[0]["a"]
    cv.add_dimension("hdistance", [a, "O"], 45)
    cv.add_dimension("vdistance", [a, "O"], 20)
    print("after dims:", cv.result.status, cv.result.dof)
    # an over-defining dimension must be rejected
    n = len(cv.geom["constraints"])
    cv.add_dimension("length", ["l3"], 99)
    print("conflict rejected:", len(cv.geom["constraints"]) == n)
    w.sketch_editor.msg.setText("")
    # drag two dimension labels out of the way with real mouse drags
    cv.set_tool("select")
    cv.fit()
    QTest.qWait(30)
    cv.update(); cv.repaint()
    for idx, target in ((len(cv.geom["constraints"]) - 2, (-30, 45)), (len(cv.geom["constraints"]) - 1, (-55, 0))):
        rect = next(r for r, h in cv._hits if h == f"k:{idx}")
        a = QPoint(round(rect.center().x()), round(rect.center().y()))
        tw = cv.w2s(*target)
        b = QPoint(round(tw.x()), round(tw.y()))
        QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, a)
        QTest.mouseMove(cv, b)
        QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, b)
        cv.repaint()
    print("label positions stored:", [k.get("at") for k in cv.geom["constraints"] if "at" in k])
    cv.sel = {"l1"}
    cv.hover = None
    QTest.qWait(50)
    cv.grab().save(out.replace(".png", "_editor.png"))
    w.sketch_editor.grab().save(out.replace(".png", "_editor_full.png"))
    w.sketch_editor._finish()
    QTest.qWait(50)
    print("errors after finish:", w.doc.result.errors, w.doc.result.sketch_status)
    w.add("extrude")
    QTest.qWait(50)
    import math
    vol = w.doc.result.body.volume
    print("volume", round(vol, 2), "expected", round(10 * 20 * (120 * 70 - math.pi * 100 - 0.5 * 20 * 30), 2))
    # edit a driving dimension from the inspector
    w.history.refresh(select="Sketch1")
    w.inspector.show_feature("Sketch1")
    g = w.doc.get("Sketch1").params["geometry"]
    i = next(i for i, k in enumerate(g["constraints"]) if k["type"] == "length" and k["e"] == ["l1"])
    g2 = dict(g, constraints=[dict(k) for k in g["constraints"]])
    g2["constraints"][i]["value"] = 150
    w.inspector._commit("geometry", g2)
    print("bbox after dim edit:", w.doc.mass_properties()["bbox_mm"])
    # undo returns to 120
    w._undo()
    print("bbox after undo:", w.doc.mass_properties()["bbox_mm"])
    w._redraw()
    w.history.refresh(select="Extrude1")
    QTest.qWait(100)
    w.view.render()
    vp = out.replace(".png", "_viewport.png")
    w.view.screenshot(vp)
    from PySide6.QtGui import QImage, QPainter
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(out)
    print("saved", flush=True)
    os._exit(0)


def safe():
    try:
        run()
    except Exception:
        import traceback; traceback.print_exc(); sys.stdout.flush(); os._exit(1)


QTimer.singleShot(300, safe)
app.exec()
