"""Headless UI smoke test: builds a demo part through the same tool layer
Claude uses, then saves a window screenshot. Usage: python scripts/smoke_ui.py out.png"""
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from daedalus.ai.tools import ToolExecutor
from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

DEMO = [
    ("add_sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 160, "h": 100}]}),
    ("add_extrude", {"sketch": "Sketch1", "depth": 14}),
    ("add_sketch", {"plane": "top", "entities": [{"type": "slot", "x": -40, "y": 0, "x2": 40, "y2": 0, "w": 24}]}),
    ("add_extrude", {"sketch": "Sketch2", "operation": "cut", "depth": 6}),
    ("add_hole", {"face": "top", "style": "counterbore", "diameter": 6.6, "head_diameter": 11,
                  "head_depth": 6.5, "positions": [[-65, -35], [65, -35], [65, 35], [-65, 35]]}),
    ("add_fillet", {"radius": 8, "edges": "vertical&linear"}),
    ("add_chamfer", {"distance": 1, "edges": "top&linear"}),
    ("set_material", {"material": "Aluminum 6061"}),
]

app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1500, 900)
w.show()
w.view.vtk.Initialize()
ex = ToolExecutor(w.doc)
for name, args in DEMO:
    text, err = ex.run(name, args)
    print(("ERR " if err else "ok  ") + name, text[:120])
w.copilot._append("You", "Mounting plate 160×100×14, centre slot, 4× M6 counterbored, R8 corners")
w.copilot._append("Claude", "Built a 160 × 100 × 14 mm aluminum plate with an 80 mm centre slot 6 deep, "
                            "4× M6 counterbores 15 mm from the edges, R8 corners and 1 mm top chamfer.")


def snap():
    w._redraw()
    w.history.refresh(select="Hole1")
    w.inspector.show_feature("Hole1")
    app.processEvents()
    out = sys.argv[1] if len(sys.argv) > 1 else "screenshot.png"
    w.view.render()
    vp = out.replace(".png", "_viewport.png")
    w.view.screenshot(vp)
    # compose: window chrome + live GL viewport (grab() can't read GL surfaces)
    from PySide6.QtGui import QImage, QPainter
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(out)
    print("saved", out, "errors:", w.doc.result.errors, flush=True)
    import os
    os._exit(0)


QTimer.singleShot(500, snap)
app.exec()
