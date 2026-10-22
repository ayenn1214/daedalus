import sys, time
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from daedalus.ui.theme import QSS; from daedalus.ui import theme as _theme; _theme.apply(app)
from daedalus.ui.main_window import MainWindow
w = MainWindow(); w.resize(1500, 950); w.show()
d = w.doc
d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50}]})
d.add("extrude", {"sketch": "Sketch1", "depth": 20})
d.add("sketch", {"plane": "top", "entities": [{"type": "rect", "x": -10, "y": 0, "w": 40, "h": 30}]})
d.add("extrude", {"sketch": "Sketch2", "depth": 8, "operation": "cut"})
d.add("hole", {"face": "top", "positions": [[30,15],[30,-15]], "fastener": "M6", "fit": "tap", "depth": 15})
def pump(n=10):
    for _ in range(n): app.processEvents(); time.sleep(0.03)
pump()
w.show_cam()
for t in ("facing","pocket","profile","drill"): w.cam_panel.add_op(t)
pump(); w.cam_panel.generate(); pump(); w.view.set_view("Isometric"); w.view.fit(); pump()
w.view.render(); w.view.screenshot(sys.argv[1] + "_cam.png")
w.clear_check()
d.check_manufacturability("thickness", min_wall=3)
r = d.last_check
w.studies_panel.overlay.show_field(r["p"], r["tris"], r["values"], r["label"], r["unit"], rng=r["rng"], reverse=r["reverse"], title="t", hide={r["object"]})
pump(); w.view.render(); w.view.screenshot(sys.argv[1] + "_thick.png")
print("ok", w.cam_panel.info.toPlainText()[:300])
