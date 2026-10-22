"""Doodle page with real mouse drags: draw a bracket outline with a slot-ish hole and a
round hole, check the live clean-up, Make sketch (offline), then run the Ask Claude
path against a stand-in model that records the images it receives.
Usage: python scripts/smoke_doodle.py out.png"""
import math
import os
import random
import sys
import traceback

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import daedalus.ui.copilot_panel as cp
from daedalus.ui.main_window import MainWindow
from daedalus.ui.theme import QSS

out = sys.argv[1] if len(sys.argv) > 1 else "doodle.png"
app = QApplication(sys.argv)
from daedalus.ui import theme as _theme; _theme.apply(app)
w = MainWindow()
w.resize(1600, 950)
w.show()
w.view.vtk.Initialize()
QTest.qWait(200)
rng = random.Random(5)


def wobble(pts, amp=1.6):
    ph = rng.uniform(0, 6.3)
    n = max(len(pts) - 1, 1)
    return [(x + amp * math.sin(5 * i / n + ph) + rng.uniform(-0.5, 0.5),
             y + amp * math.cos(4 * i / n + ph) + rng.uniform(-0.5, 0.5)) for i, (x, y) in enumerate(pts)]


def seg(a, b, step=5):
    n = max(2, int(math.dist(a, b) / step))
    return [(a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n) for t in range(n + 1)]


def poly(*pts):
    o = []
    for a, b in zip(pts, pts[1:]):
        o += seg(a, b)[:-1]
    return o + [pts[-1]]


def arc(cx, cy, r, a0, a1, step=5):
    n = max(6, int(abs(a1 - a0) / 360 * 2 * math.pi * r / step))
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def draw(canvas, pts):
    pts = [QPoint(round(x), round(y)) for x, y in wobble(pts)]
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, pts[0])
    for p in pts[1:]:
        ev_pos = p
        QTest.mouseMove(canvas, ev_pos)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, pts[-1])
    QTest.qWait(20)


def compose(path):
    w.view.render()
    vp = path.replace(".png", "_vp.png")
    w.view.screenshot(vp)
    img = w.grab().toImage()
    p = QPainter(img)
    p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
    p.end()
    img.save(path)


class FakeCopilot:
    """Stands in for Claude: records images, builds via the same tools, then answers the check."""
    model = "fake"

    def __init__(self, **k):
        self.messages = []
        self.calls = []
        self._stop = False

    def ask(self, prompt, state, on_text, on_tool, images=None):
        self.calls.append((prompt[:60], [len(i) for i in (images or [])], [i[:4] for i in (images or [])]))
        if len(self.calls) == 1:
            r = self.call_tool("add_sketch", {"plane": "XY", "geometry": {
                "points": {"p1": [-75, -40], "p2": [75, -40], "p3": [75, 40], "p4": [-75, 40], "c": [40, 0]},
                "curves": [{"id": "l1", "type": "line", "a": "p1", "b": "p2"},
                           {"id": "l2", "type": "line", "a": "p2", "b": "p3"},
                           {"id": "l3", "type": "line", "a": "p3", "b": "p4"},
                           {"id": "l4", "type": "line", "a": "p4", "b": "p1"},
                           {"id": "c1", "type": "circle", "c": "c", "r": 12}],
                "constraints": [{"type": "horizontal", "e": ["l1"]}, {"type": "horizontal", "e": ["l3"]},
                                {"type": "vertical", "e": ["l2"]}, {"type": "vertical", "e": ["l4"]},
                                {"type": "symmetric", "e": ["p1", "p3", "O"]},
                                {"type": "length", "e": ["l1"], "value": 150},
                                {"type": "length", "e": ["l2"], "value": 80},
                                {"type": "diameter", "e": ["c1"], "value": 24}]}})
            on_tool("add_sketch", {}, *r)
            r = self.call_tool("add_extrude", {"sketch": "Sketch1", "depth": 10})
            on_tool("add_extrude", {}, *r)
            on_text("Built a 150 × 80 × 10 plate with a Ø24 hole.")
        else:
            on_text("Matches the drawing.")
        return ""

    def stop(self):
        self._stop = True


def run():
    w.open_doodle()
    page = w.doodle
    cv = page.canvas
    QTest.qWait(50)
    W, H = cv.width(), cv.height()
    ox, oy = 60, 60          # keep the drawing well inside the (virtual) screen
    T = lambda pts: [(ox + x, oy + y) for x, y in pts]
    # L-bracket outline with a rounded inside corner (one stroke), a round hole, a 4-stroke rectangle cut-out
    outline = poly((0, 0), (440, 0), (440, 80), (180, 80)) + arc(180, 140, 60, -90, -180)[1:] + \
        poly((120, 140), (120, 260), (0, 260), (0, 0))
    draw(cv, T(outline))
    draw(cv, T(arc(60, 60, 28, 15, 385)))
    for a, b in (((340, 25), (410, 27)), ((412, 22), (408, 58)), ((410, 60), (338, 57)), ((336, 61), (339, 23))):
        draw(cv, T([a] + seg(a, b)[1:]))
    QTest.qWait(50)
    print("recognised:", page.recog.text())
    page.size.setValue(200)
    page.depth.setValue(8)
    page.notes.setText("bracket, 8 mm aluminium")
    page.grab().save(out.replace(".png", "_page.png"))
    # offline path
    if os.environ.get("DUMP"):
        import json
        json.dump(cv.strokes_yup(), open(os.environ["DUMP"], "w"))
    page._make()
    QTest.qWait(100)
    print("make sketch → errors:", w.doc.result.errors, "status:", w.doc.result.sketch_status,
          "bbox:", w.doc.mass_properties().get("bbox_mm"))
    compose(out.replace(".png", "_model.png"))
    # Claude path with a stand-in model
    w.doc.undo()
    print("after undo:", [f.name for f in w.doc.features])
    fake = FakeCopilot()
    cp.Copilot = lambda **k: fake
    os.environ["ANTHROPIC_API_KEY"] = "test"
    w.copilot.model.addItem("fake")
    w.copilot.model.setCurrentText("fake")
    w.open_doodle()
    QTest.qWait(30)
    page._ask()

    def wait():
        if w.copilot.worker and w.copilot.worker.isRunning() or not w.copilot.send_btn.isEnabled():
            return QTimer.singleShot(100, wait)
        for i, c in enumerate(fake.calls):
            print(f"claude call {i + 1}: prompt '{c[0]}…' images {c[1]} bytes, headers {c[2]}")
        print("features:", [f.name for f in w.doc.features], "errors:", w.doc.result.errors)
        w.doc.undo()
        print("one undo removes the whole Claude turn:", [f.name for f in w.doc.features])
        w.doc.redo()
        w._redraw()
        QTest.qWait(80)
        compose(out)
        print("saved", flush=True)
        os._exit(0)

    QTimer.singleShot(200, wait)


def safe():
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)


QTimer.singleShot(300, safe)
app.exec()
