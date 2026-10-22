"""CAM side panel: setup, tools, operations, toolpath preview and G-code; manufacturability checks."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core.features import FeatureError
from ..manufacturing import cam
from . import theme


class ToolDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add tool")
        f = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(["flat", "face", "drill", "tap", "spot", "ball"])
        self.dia = QDoubleSpinBox()
        self.dia.setRange(0.1, 200)
        self.dia.setValue(8)
        self.dia.setSuffix(" mm")
        self.fl = QSpinBox()
        self.fl.setRange(1, 12)
        self.fl.setValue(3)
        self.pitch = QDoubleSpinBox()
        self.pitch.setRange(0, 10)
        self.pitch.setDecimals(3)
        self.pitch.setSuffix(" mm (taps)")
        for lab, w in (("Type", self.kind), ("Diameter", self.dia), ("Flutes", self.fl), ("Pitch", self.pitch)):
            f.addRow(lab, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class CycleDialog(QDialog):
    """Drilling operation: canned cycle and its parameters."""

    def __init__(self, op, tool, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel
        self.setWindowTitle(f"{op.get('name') or 'Drill'} — canned cycle")
        f = QFormLayout(self)
        self.cycle = QComboBox()
        for k, v in cam.CYCLES.items():
            self.cycle.addItem(f"{k} · {v}" if k != "auto" else v, k)
        self.cycle.setCurrentIndex(max(0, self.cycle.findData(op.get("cycle", "auto"))))
        f.addRow("Cycle", self.cycle)

        def spin(val, suffix, lo=0.0, hi=1000.0, dec=3, tip=""):
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(dec)
            sb.setValue(float(val))
            sb.setSuffix(suffix)
            sb.setToolTip(tip)
            return sb
        D = float(tool.get("diameter", 5.0)) if tool else 5.0
        self.peck = spin(op.get("peck", 0), " mm", tip="Q — depth per peck (G83 / G73). 0 = 1.5×Ø (G83), 1×Ø (G73)")
        self.dwell = spin(op.get("dwell", 0), " s", 0, 60, 2, "P — dwell at the bottom (G82 / G89 / taps)")
        self.r_clear = spin(op.get("r_clear", 2.0), " mm", 0, 100, 2, "R plane above the hole top")
        self.shift = spin(op.get("shift", 0.2), " mm", 0, 5, 3, "Q — lift-off from the wall before retract (G76)")
        self.brk = spin(op.get("breakthrough", 1.0), " mm", 0, 50, 2,
                        "Extra depth past the bottom of through holes (plus the drill point, 0.3×Ø)")
        self.retract = QComboBox()
        self.retract.addItem("G98 — back to the initial height between holes", "initial")
        self.retract.addItem("G99 — back to the R plane between holes (faster)", "r")
        self.retract.setCurrentIndex(1 if str(op.get("retract", "initial")).lower() in ("r", "g99") else 0)
        f.addRow("Peck (Q)", self.peck)
        f.addRow("Dwell (P)", self.dwell)
        f.addRow("R clearance", self.r_clear)
        f.addRow("Retract", self.retract)
        f.addRow("Fine-bore shift", self.shift)
        f.addRow("Breakthrough", self.brk)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        f.addRow(self.hint)
        self.cycle.currentIndexChanged.connect(self._sync)
        self._sync()
        self._tool_kind = (tool or {}).get("kind", "drill")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._D = D

    def _sync(self):
        k = self.cycle.currentData()
        self.peck.setEnabled(k in ("auto", "G83", "G73"))
        self.dwell.setEnabled(k in ("auto", "G82", "G89", "G84", "G74"))
        self.shift.setEnabled(k == "G76")
        self.hint.setText(cam.CYCLES.get(k, "") + {
            "G84": " · Fanuc: M29 rigid tap · feed = rpm × pitch", "G74": " · spindle runs M4",
            "G76": " · needs spindle orientation (M19) on the machine",
        }.get(k, "") + "\nGRBL writes the moves out (no taps); Siemens uses MCALL CYCLE8x.")

    def values(self):
        return {"cycle": self.cycle.currentData(), "peck": self.peck.value(), "dwell": self.dwell.value(),
                "r_clear": self.r_clear.value(), "retract": self.retract.currentData(), "shift": self.shift.value(),
                "breakthrough": self.brk.value()}


class CamPanel(QWidget):
    status = Signal(str)
    auto_requested = Signal()

    def __init__(self, doc, viewport, parent=None):
        super().__init__(parent)
        self.doc, self.vp = doc, viewport
        self._actors = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        f = QFormLayout()
        self.obj = QComboBox()
        self.post = QComboBox()
        for k, v in cam.POSTS.items():
            self.post.addItem(v, k)
        self.origin = QComboBox()
        self.origin.addItems(["corner", "center"])
        self.units = QComboBox()
        self.units.addItems(["mm", "in"])
        f.addRow("Part", self.obj)
        f.addRow("Post", self.post)
        row = QHBoxLayout()
        row.addWidget(QLabel("G54"))
        row.addWidget(self.origin)
        row.addWidget(self.units)
        f.addRow(row)
        lay.addLayout(f)
        for w in (self.obj, self.post, self.origin, self.units):
            w.activated.connect(self._setup_changed)
        self.tools = QTableWidget(0, 4)
        self.tools.setHorizontalHeaderLabels(["T", "Type", "Ø mm", "Fl"])
        self.tools.verticalHeader().hide()
        self.tools.setMaximumHeight(120)
        self.tools.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.tools)
        tb = QHBoxLayout()
        b = QPushButton("+ Tool")
        b.clicked.connect(self.add_tool)
        tb.addWidget(b)
        tb.addStretch(1)
        lay.addLayout(tb)
        self.ops = QListWidget()
        self.ops.setToolTip("Double-click a drilling operation to choose its canned cycle (G81 / G83 / G84 …)")
        self.ops.itemDoubleClicked.connect(lambda _it: self.edit_op())
        lay.addWidget(self.ops, 1)
        ob = QHBoxLayout()
        for text, t in (("Facing", "facing"), ("Pocket", "pocket"), ("Profile", "profile"), ("Drill", "drill")):
            b = QPushButton(text)
            b.setToolTip(cam.OP_TYPES[t])
            b.clicked.connect(lambda _=False, t=t: self.add_op(t))
            ob.addWidget(b)
        lay.addLayout(ob)
        ob2 = QHBoxLayout()
        for text, fn in (("Auto CAM…", self.auto_requested.emit), ("Cycle…", self.edit_op), ("Remove", self.remove_op), ("Generate", self.generate),
                         ("Save G-code…", self.save),
                         ("Hide", self.clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            ob2.addWidget(b)
        lay.addLayout(ob2)
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setMaximumHeight(130)
        lay.addWidget(self.info)

    def _setup(self):
        return {**cam.default_setup(), **(self.doc.cam or {})}

    def refresh(self):
        s = self._setup()
        names = [o.name for o in self.doc.objects]
        self.obj.blockSignals(True)
        self.obj.clear()
        self.obj.addItems(names)
        self.obj.setCurrentText(s.get("object") or (names[0] if names else ""))
        self.obj.blockSignals(False)
        self.post.setCurrentIndex(max(0, list(cam.POSTS).index(s["post"]) if s["post"] in cam.POSTS else 0))
        self.origin.setCurrentText(s["origin"])
        self.units.setCurrentText(s["units"])
        self.tools.setRowCount(len(s["tools"]))
        for i, t in enumerate(s["tools"]):
            for j, v in enumerate((f"T{t['number']}", t["kind"], f"{t['diameter']:g}", str(t.get("flutes", "")))):
                self.tools.setItem(i, j, QTableWidgetItem(v))
        tools = {t["number"]: t for t in s["tools"]}
        self.ops.clear()
        for i, o in enumerate(s["ops"]):
            t = tools.get(o.get("tool"), {})
            cyc = ""
            if o["type"] == "drill":
                cyc = f"  · {o.get('cycle', 'auto')}" + (" G99" if o.get("retract") == "r" else "")
            self.ops.addItem(f"{i + 1}. {o['type'].title()}  ·  T{o.get('tool')} "
                             f"{t.get('name') or t.get('kind', '')}" + (f"  · Ø{o['diameter']:g} holes"
                                                                        if o.get("diameter") else "") + cyc)

    def _try(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "CAM", str(e))
            return None

    def _setup_changed(self, *_):
        self._try(self.doc.cam_setup, object=self.obj.currentText() or None, post=self.post.currentData(),
                  origin=self.origin.currentText(), units=self.units.currentText())

    def add_tool(self):
        d = ToolDialog(self)
        if d.exec():
            self._try(self.doc.cam_add_tool, d.kind.currentText(), d.dia.value(), d.fl.value(),
                      pitch=d.pitch.value())

    def add_op(self, t):
        if self.doc.cam is None or not self.doc.cam:
            self._setup_changed()
        made = self._try(self.doc.cam_add_op, t)
        if made:
            self.status.emit(f"Added {len(made)} {t} operation" + ("s" if len(made) > 1 else ""))

    def edit_op(self):
        i = self.ops.currentRow()
        s = self._setup()
        if i < 0 or i >= len(s["ops"]):
            return
        op = s["ops"][i]
        if op["type"] != "drill":
            self.status.emit("Canned cycles apply to drilling operations")
            return
        tool = next((t for t in s["tools"] if t["number"] == op.get("tool")), None)
        d = CycleDialog(op, tool, self)
        if d.exec():
            if self._try(self.doc.cam_edit_op, i, **d.values()) is not None:
                self.ops.setCurrentRow(i)
                self.status.emit(f"Operation {i + 1}: {d.values()['cycle']}")

    def remove_op(self):
        i = self.ops.currentRow()
        if i >= 0:
            self._try(self.doc.cam_remove_op, i)

    def generate(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            s = self._try(self.doc.cam_plan)
        finally:
            QApplication.restoreOverrideCursor()
        if not s:
            return
        self.show_paths(self.doc.last_cam)
        lines = [f"{s['object']} · {s['material']} · stock {' × '.join(f'{v:g}' for v in s['stock_mm'])} mm · "
                 f"≈{s['cycle_time_min']:.1f} min"]
        for o in s["operations"]:
            lines.append(f"{o['name']}: {o['tool']} S{o['rpm']} F{o['feed_mm_min']:g} · {o['time_min']:.1f} min"
                         + (f" · {o['holes']} holes" if o["holes"] else "") + "".join(f"\n   ⚠ {n}" for n in o["notes"]))
        self.info.setPlainText("\n".join(lines))
        self.status.emit(f"Toolpaths ready · cycle ≈{s['cycle_time_min']:.1f} min")

    def save(self):
        base = (self.doc.path.stem if self.doc.path else "part") + ".nc"
        p, _ = QFileDialog.getSaveFileName(self, "Save G-code", base, "G-code (*.nc *.tap *.ngc *.gcode)")
        if p:
            s = self._try(self.doc.export_gcode, p)
            if s:
                self.show_paths(self.doc.last_cam)
                self.status.emit(f"Saved {Path(p).name}: {s['lines']} lines, ≈{s['cycle_time_min']:.1f} min")

    # ------------------------------------------------------------ preview
    def clear(self):
        for a in self._actors:
            self.vp.ren.RemoveActor(a)
        self._actors = []
        self.vp.render()

    def show_paths(self, result):
        from . import vtklite as vtk
        self.clear()
        cols = {"facing": (0.6, 0.6, 0.65), "pocket": (0.2, 0.55, 1.0), "profile": (0.2, 0.85, 0.4),
                "drill": (1.0, 0.75, 0.1)}
        for r in result["ops"]:
            segs = {"rapid": [], "cut": []}
            prev = None
            for kind, x, y, z in r.moves:
                if prev is not None:
                    segs["rapid" if kind == "rapid" else "cut"].append((prev, (x, y, z)))
                prev = (x, y, z)
            for c in r.cycles:
                segs["cut"].append(((c["x"], c["y"], c["r"]), (c["x"], c["y"], c["z"])))
            for key, ss in segs.items():
                if not ss:
                    continue
                pts = vtk.vtkPoints()
                lines = vtk.vtkCellArray()
                for a, b in ss:
                    i = pts.InsertNextPoint(a)
                    j = pts.InsertNextPoint(b)
                    ln = vtk.vtkLine()
                    ln.GetPointIds().SetId(0, i)
                    ln.GetPointIds().SetId(1, j)
                    lines.InsertNextCell(ln)
                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetLines(lines)
                m = vtk.vtkPolyDataMapper()
                m.SetInputData(pd)
                a = vtk.vtkActor()
                a.SetMapper(m)
                a.GetProperty().SetColor(*((1.0, 0.25, 0.25) if key == "rapid" else cols.get(r.type, (1, 1, 1))))
                a.GetProperty().SetLineWidth(1.0 if key == "rapid" else 2.0)
                a.GetProperty().SetOpacity(0.45 if key == "rapid" else 1.0)
                a.GetProperty().LightingOff()
                a.PickableOff()
                self.vp.ren.AddActor(a)
                self._actors.append(a)
        st = result["stock"]
        cube = vtk.vtkCubeSource()
        cube.SetBounds(st["x0"], st["x1"], st["y0"], st["y1"], st["z0"], st["z1"])
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(cube.GetOutputPort())
        a = vtk.vtkActor()
        a.SetMapper(m)
        a.GetProperty().SetRepresentationToWireframe()
        a.GetProperty().SetColor(*theme.CAM_RAPID)
        a.PickableOff()
        self.vp.ren.AddActor(a)
        self._actors.append(a)
        self.vp.render()
