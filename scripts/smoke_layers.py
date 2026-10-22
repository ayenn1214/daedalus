"""Layers in the GUI: frame / deck / hardware layers, toggled through the Layers panel
with real clicks. Saves screenshots. Usage: python scripts/smoke_layers.py out.png"""
import os
import sys
import traceback

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from daedalus.ui.main_window import MainWindow
from daedalus.ui.layers import C_LOCK, C_MODEL, C_VIS
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "layers.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
d = w.doc

CART = {
    "points": {"a": [-300, -200, 0], "b": [300, -200, 0], "c": [300, 200, 0], "d": [-300, 200, 0],
               "e": [300, 200, 300], "f": [-300, 200, 300]},
    "curves": [{"id": "l1", "type": "line", "a": "a", "b": "b"}, {"id": "l2", "type": "line", "a": "b", "b": "c"},
               {"id": "l3", "type": "line", "a": "c", "b": "d"}, {"id": "l4", "type": "line", "a": "d", "b": "a"},
               {"id": "l5", "type": "line", "a": "c", "b": "e"}, {"id": "l6", "type": "line", "a": "d", "b": "f"},
               {"id": "l7", "type": "line", "a": "f", "b": "e"}],
    "constraints": [],
}


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def click_cell(layer, col):
    tree = w.layers.tree
    for i in range(tree.topLevelItemCount()):
        it = tree.topLevelItem(i)
        if it.data(1, Qt.UserRole) == layer:
            r = tree.visualItemRect(it)
            x = tree.header().sectionViewportPosition(col) + tree.header().sectionSize(col) // 2
            QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(x, r.center().y()))
            QTest.qWait(30)
            return
    raise RuntimeError(f"no row for {layer}")


def run():
    d.add_layer("Frame", "#d9822b", activate=True)
    d.add("sketch3d", {"geometry": CART})
    d.add("frame", {"path": "3DSketch1", "profile": "square_tube", "width": 25.4, "thickness": 1.65})
    d.add_layer("Deck", "#4f9fd9", activate=True)
    d.add("sketch", {"plane": "XY", "offset": 12.7, "entities": [{"type": "rect", "x": 0, "y": 0, "w": 600, "h": 400}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 12})
    d.add_layer("Hardware", "#6cbf6c", activate=True)
    d.add("hole", {"face": "top", "diameter": 9, "positions": [[-260, -160], [260, -160], [260, 160], [-260, 160]]})
    w._redraw()
    w.view._has_framed = False
    w.view.set_view("Isometric")
    QTest.qWait(80)
    from collections import Counter
    print("faces by layer:", dict(Counter(d.result.face_layers)), "errors:", d.result.errors)
    compose(out.replace(".png", "_all.png"))
    v_all = d.result.body.volume
    # 👁 hide the deck (display only)
    click_cell("Deck", C_VIS)
    print("deck visible:", d.layer("Deck").visible, "volume unchanged:", abs(d.result.body.volume - v_all) < 1e-6)
    w._redraw()
    QTest.qWait(60)
    compose(out.replace(".png", "_deck_hidden.png"))
    click_cell("Deck", C_VIS)
    # 🔒 lock the frame, try to edit it
    click_cell("Frame", C_LOCK)
    from daedalus.core.features import FeatureError
    try:
        d.edit("Frame1", {"width": 30})
        print("LOCK FAILED")
    except FeatureError as e:
        print("frame locked:", e)
    # ⏻ take hardware out of the model
    click_cell("Hardware", C_MODEL)
    print("hardware in model:", d.layer("Hardware").in_model, "volume rose:", d.result.body.volume > v_all)
    click_cell("Hardware", C_MODEL)
    # move a feature with the History menu path
    d.move_to_layer(["Sketch1"], "Default")
    print("Sketch1 →", d.get("Sketch1").layer, "| counts:", d.layer_counts())
    # one undo reverses that move
    d.undo()
    print("after undo Sketch1 on:", d.get("Sketch1").layer)
    w._redraw()
    w.history.refresh(select="Frame1")
    w.inspector.show_feature("Frame1")
    QTest.qWait(80)
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
