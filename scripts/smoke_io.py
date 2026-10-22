"""Import/export in the GUI: STEP assembly via File ▸ Import path, an STL dropped on the window,
a DXF turned into a sketch and extruded, exports to 3MF/DXF. Usage: python scripts/smoke_io.py out.png"""
import math
import os
import sys
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QDropEvent, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from daedalus.core.document import Document
from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "io.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
shown = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[2] if len(a) > 2 else "")))
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warn", a[2] if len(a) > 2 else "")))
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
tmp = Path(tempfile.mkdtemp())


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


def make_inputs():
    d = Document()
    d.set_object("Object1", new_name="Bracket")
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50},
                                  {"type": "circle", "x": -25, "y": 0, "r": 6, "mode": "subtract"},
                                  {"type": "circle", "x": 25, "y": 0, "r": 6, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 8})
    d.add("fillet", {"edges": "vertical", "radius": 6})
    d.add_object("Shaft", "#d9822b", pose=[25, 0, -10, 0, 0, 0])
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 6}]}, obj="Shaft")
    d.add("extrude", {"sketch": "Sketch2", "depth": 60}, obj="Shaft")
    d.export(tmp / "bracket.step")
    d2 = Document()
    d2.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 15}]})
    d2.add("extrude", {"sketch": "Sketch1", "depth": 20})
    d2.export(tmp / "puck.stl")
    import ezdxf
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    m = doc.modelspace()
    b = math.tan(math.radians(90) / 4)
    r = 5
    m.add_lwpolyline([(r, 0, 0), (60 - r, 0, b), (60, r, 0), (60, 30 - r, b), (60 - r, 30, 0), (r, 30, b),
                      (0, 30 - r, 0), (0, r, b)], format="xyb", close=True)
    m.add_circle((15, 15), 4)
    m.add_circle((45, 15), 4)
    doc.saveas(str(tmp / "plate.dxf"))


def drop(path):
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(path))])
    ev = QDropEvent(QPointF(400, 300), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    w.dropEvent(ev)


def run():
    make_inputs()
    d = w.doc
    s = w.import_path(tmp / "bracket.step")
    print("step:", s["objects"], [o.color for o in d.objects], "errors:", d.result.errors)
    drop(tmp / "puck.stl")
    print("after drop:", [o.name for o in d.objects], "faces of puck:", len(d.result.bodies["puck"].faces()))
    d.place_object("puck", translate=(0, 70, 0))
    s = w.import_path(tmp / "plate.dxf")
    print("dxf:", s["features"], "active object:", d.active_object)
    d.add_object("Plate", "#5aa469")
    d.get(s["features"][0]).obj = "Plate"
    d.add("extrude", {"sketch": s["features"][0], "depth": 4}, obj="Plate")
    d.place_object("Plate", translate=(-30, -90, 0))
    print("plate volume:", round(d.result.bodies["Plate"].volume, 1), "errors:", d.result.errors)
    # build on an imported part: hole through the bracket
    d.add("sketch", {"plane": "top", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 4}]}, obj="Bracket")
    d.add("extrude", {"sketch": d.last_of("sketch"), "operation": "cut", "end": "through_all"}, obj="Bracket")
    print("bracket after hole errors:", d.result.errors)
    w.export_sketch_dxf(s["features"][0], str(tmp / "plate_out.dxf"))
    d.export(tmp / "all.3mf")
    d.export(tmp / "all.step")
    print("exports:", sorted(p.name for p in tmp.iterdir()))
    print("dialogs:", shown)
    w._redraw() if hasattr(w, "_redraw") else None
    w.history.refresh(select="Bracket")
    w.inspector.show_feature("Bracket")
    w.view.fit()
    QTest.qWait(150)
    compose(out)
    # Open on a non-.daedalus file → new doc with it imported
    d.dirty = False
    w.open_path(str(tmp / "all.step"))
    print("reopened:", [o.name for o in w.doc.objects], "errors:", w.doc.result.errors)
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
