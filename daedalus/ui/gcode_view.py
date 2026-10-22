"""G-code back-plot in the 3D view: rapids / feeds as lines, a tool marker with play / scrub, stats."""
from __future__ import annotations

import bisect
from pathlib import Path

import numpy as np
from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from ..manufacturing import gcode_sim as G
from . import theme
from . import vtklite as vtk


def _lines_actor(segs, color, width, opacity=1.0):
    pts = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    for k, (_kind, a, b, _f, _n) in enumerate(segs):
        pts.InsertNextPoint(*a)
        pts.InsertNextPoint(*b)
        cells.InsertNextCell(2, [2 * k, 2 * k + 1])
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetLines(cells)
    m = vtk.vtkPolyDataMapper()
    m.SetInputData(pd)
    a = vtk.vtkActor()
    a.SetMapper(m)
    p = a.GetProperty()
    p.SetColor(*color)
    p.SetLineWidth(width)
    p.SetOpacity(opacity)
    p.SetLighting(False)
    a.PickableOff()
    return a


class GcodeBar(QWidget):
    """Shown under the view bar while a program is back-plotted."""

    def __init__(self, viewport, parent=None):
        super().__init__(parent)
        self.vp = viewport
        self.actors = []
        self.parsed = None
        self.offset = np.zeros(3)
        self.setObjectName("viewbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 3, 8, 3)
        t = QLabel("G-code")
        t.setObjectName("h2")
        h.addWidget(t)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        h.addWidget(self.info)
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.toggle_play)
        h.addWidget(self.play_btn)
        self.speed = QComboBox()
        for s in (1, 5, 20, 100, 500):
            self.speed.addItem(f"{s}×", s)
        self.speed.setCurrentIndex(2)
        h.addWidget(self.speed)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._scrub)
        h.addWidget(self.slider, 1)
        self.at = QLabel("")
        self.at.setObjectName("muted")
        self.at.setMinimumWidth(170)
        h.addWidget(self.at)
        close = QPushButton("Close")
        close.clicked.connect(self.close_plot)
        h.addWidget(close)
        self.timer = QTimer(self, interval=30)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.hide()

    def load(self, path, offset=None):
        text = Path(path).read_text(errors="replace")
        self.show_program(text, Path(path).name, offset)

    def show_program(self, text, name="program", offset=None):
        """offset: model position of the program zero (the CAM work offset) so the path sits on the part."""
        self.clear()
        self.offset = np.array(offset if offset is not None else (0.0, 0.0, 0.0), float)
        p = self.parsed = G.parse(text)
        if not p["segments"]:
            raise ValueError("No moves found in this program")
        st = self.stats = G.stats(p)
        self.times = G.timeline(p)
        rap = [s for s in p["segments"] if s[0] == "rapid"]
        feed = [s for s in p["segments"] if s[0] != "rapid"]
        self.done_actor = None
        for segs, col, w, op in ((rap, (0.95, 0.35, 0.25), 1.0, 0.7), (feed, (0.25, 0.65, 1.0), 1.6, 1.0)):
            if segs:
                a = _lines_actor(segs, col, w, op)
                a.SetPosition(*self.offset)
                self.vp.ren.AddActor(a)
                self.actors.append(a)
        src = vtk.vtkSphereSource()
        ext = max(st["extent_mm"]) or 10
        src.SetRadius(max(ext * 0.008, 0.4))
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(src.GetOutputPort())
        self.tool = vtk.vtkActor()
        self.tool.SetMapper(m)
        self.tool.GetProperty().SetColor(*theme.SELECT_COLOR)
        self.tool.PickableOff()
        self.vp.ren.AddActor(self.tool)
        self.actors.append(self.tool)
        self.info.setText(f"{name} · {st['segments']} moves · feed {st['feed_mm'] / 1000:.2f} m · rapid "
                          f"{st['rapid_mm'] / 1000:.2f} m · ≈ {st['total_min']:.1f} min · Z {st['z_min']:.2f} … "
                          f"{st['z_max']:.2f} ({p['unit']} program)")
        self.slider.blockSignals(True)
        self.slider.setValue(1000)
        self.slider.blockSignals(False)
        self._scrub(1000)
        self.show()
        lo, hi = (np.array(p["bounds"][0]) + self.offset), (np.array(p["bounds"][1]) + self.offset)
        self.vp.ren.ResetCamera(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
        self.vp.render()

    def _scrub(self, v):
        if not self.parsed:
            return
        total = self.times[-1] if self.times else 0
        t = total * v / 1000
        i = min(bisect.bisect_left(self.times, t), len(self.times) - 1)
        kind, a, b, f, line = self.parsed["segments"][i]
        t0 = self.times[i - 1] if i else 0.0
        frac = (t - t0) / (self.times[i] - t0) if self.times[i] > t0 else 1.0
        p = np.array(a) + (np.array(b) - np.array(a)) * min(max(frac, 0), 1)
        self.tool.SetPosition(*(p + self.offset))
        self.at.setText(f"{t:.2f} / {total:.2f} min · line {line + 1} · {kind}")
        self.vp.render_later()

    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("▶ Play")
            return
        if self.slider.value() >= 1000:
            self.slider.setValue(0)
        self._t0 = self.slider.value() / 1000 * (self.times[-1] if self.times else 0)
        self.clock.start()
        self.timer.start()
        self.play_btn.setText("❚❚ Pause")

    def _tick(self):
        total = self.times[-1] if self.times else 0
        if not total:
            return self.toggle_play()
        t = self._t0 + self.clock.elapsed() / 60000 * self.speed.currentData()
        if t >= total:
            self.slider.setValue(1000)
            self.toggle_play()
            return
        self.slider.setValue(int(t / total * 1000))

    def clear(self):
        self.timer.stop()
        for a in self.actors:
            self.vp.ren.RemoveActor(a)
        self.actors = []
        self.parsed = None

    def close_plot(self):
        self.clear()
        self.hide()
        self.vp.render()
