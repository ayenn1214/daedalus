import sys, time
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from daedalus.ui.theme import QSS; from daedalus.ui import theme as _theme; _theme.apply(app)
from daedalus.ui.main_window import MainWindow
w = MainWindow(); w.resize(1500, 950); w.show()
d = w.doc
d.add("sketch", {"entities": [{"type": "rect", "x": 50, "y": 0, "w": 100, "h": 10}]})
d.add("extrude", {"sketch": "Sketch1", "depth": 40})
def pump(n=10):
    for _ in range(n): app.processEvents(); time.sleep(0.03)
b = d.result.bodies["Object1"]; fs=list(b.faces())
xmin = min(range(len(fs)), key=lambda i: fs[i].center().X); xmax = max(range(len(fs)), key=lambda i: fs[i].center().X)
w.new_study("topology")
st = d.studies[-1]["name"]
d.set_study(st, iterations=20) if hasattr(d, "set_study") else None
d.add_study_item(st, "fixtures", "fixed", faces=[xmin])
d.add_study_item(st, "loads", "force", faces=[xmax], value=100, direction=[0,0,-1])
pump()
p = w.studies_panel
p.run()
t0=time.time()
while p.thread is not None and time.time()-t0 < 300: time.sleep(0.05); app.processEvents()
pump(); w.view.set_view("Isometric"); w.view.fit(); pump()
w.view.render(); w.view.screenshot(sys.argv[1] + "_topo.png")
p.create_body(); pump()
from daedalus.ui.design_study_ui import DesignStudyDialog
dlg = DesignStudyDialog(d, w); print(dlg.feat.count(), dlg.param.count())
print("ok", d.objects[-1].name, p.info.text()[:80])
