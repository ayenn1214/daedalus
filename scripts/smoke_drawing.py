import sys, time
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
app = QApplication.instance() or QApplication([])
from daedalus.ui.main_window import MainWindow
w = MainWindow(); w.resize(1500, 950); w.show()
d = w.doc
d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50},
                              {"type": "circle", "x": 20, "y": 10, "r": 4, "mode": "subtract"}]})
d.add("extrude", {"sketch": "Sketch1", "depth": 20})
d.add("hole", {"face": "top", "positions": [[-20,-10]], "fastener": "M6", "fit": "tap", "depth": 12})
d.new_drawing(title={"title": "Bracket"})
w.show_drawings()
for _ in range(5): app.processEvents(); time.sleep(0.05)
pg = w.drawing_page; c = pg.canvas
# draw a line and a dimension with the tools
def click(x, y, dbl=False):
    pos = c.to_dev([x, y])
    for t in (QMouseEvent.MouseButtonPress, QMouseEvent.MouseButtonRelease):
        ev = QMouseEvent(t, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        (c.mousePressEvent if t == QMouseEvent.MouseButtonPress else c.mouseReleaseEvent)(ev)
pg.set_tool("line"); click(30, 260); click(90, 260); c._finish_chain()
pg.set_tool("dim"); click(30, 260); click(90, 260); click(60, 270)
pg.set_tool("rect"); click(250, 150); click(300, 180)
sh = pg.current_sheet()
print("items", [(i["t"], i.get("kind")) for i in sh.items])
pg.set_tool("select"); click(60, 260); print("sel", c.sel)
for _ in range(5): app.processEvents(); time.sleep(0.05)
w.grab().save((sys.argv[1] if len(sys.argv) > 1 else "drawing") + "_ui.png")
from daedalus.ui.drawing_ui import OptionsDialog
from daedalus.ui.export_ui import ExportOptionsDialog
OptionsDialog(pg.current_drawing(), sh).values(); print(ExportOptionsDialog(d).values())
print("ok")
