"""Design study dialog: sweep a feature parameter, optionally re-running a simulation study at each value."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.features import FeatureError
from ..sim.advanced import METRICS
from .physics_ui import TracePlot


class DesignStudyDialog(QDialog):
    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("Design study")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.feat = QComboBox()
        for ft in doc.features:
            if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ft.params.values()):
                self.feat.addItem(ft.name)
        self.param = QComboBox()
        self.feat.currentTextChanged.connect(self._params)
        self.lo, self.hi = QDoubleSpinBox(), QDoubleSpinBox()
        for s in (self.lo, self.hi):
            s.setRange(-1e6, 1e6)
            s.setDecimals(3)
        self.n = QSpinBox()
        self.n.setRange(2, 30)
        self.n.setValue(5)
        rng = QHBoxLayout()
        rng.addWidget(self.lo)
        rng.addWidget(QLabel("to"))
        rng.addWidget(self.hi)
        rng.addWidget(QLabel("steps"))
        rng.addWidget(self.n)
        self.study = QComboBox()
        self.study.addItem("(mass only)")
        for st in doc.studies:
            if st["type"] in ("static", "frequency", "buckling", "thermal", "assembly", "flow", "drop"):
                self.study.addItem(st["name"])
        self.metric_box = QComboBox()
        self.metric_box.addItems(METRICS)
        self.metric_box.setCurrentText("max_von_mises_MPa" if doc.studies else "mass_kg")
        f.addRow("Feature", self.feat)
        f.addRow("Parameter", self.param)
        f.addRow("Values", rng)
        f.addRow("Study", self.study)
        f.addRow("Plot", self.metric_box)
        lay.addLayout(f)
        self.table = QTableWidget(0, 0)
        lay.addWidget(self.table, 1)
        self.plot = TracePlot()
        self.plot.xunit = ""
        self.plot.empty = "Run the study to plot the metric against the parameter"
        self.plot.setMinimumHeight(150)
        lay.addWidget(self.plot)
        self.metric_box.currentTextChanged.connect(lambda _t: self._plot())
        h = QHBoxLayout()
        self.msg = QLabel()
        self.msg.setObjectName("muted")
        h.addWidget(self.msg, 1)
        for text, fn in (("Run", self.run), ("Export CSV…", self.export), ("Close", self.accept)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        lay.addLayout(h)
        self.result = None
        self._params(self.feat.currentText())

    def _params(self, name):
        self.param.clear()
        if not name:
            return
        ft = self.doc.get(name)
        nums = [k for k, v in ft.params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        self.param.addItems(nums)
        if nums:
            v = float(ft.params[nums[0]])
            self.lo.setValue(v * 0.5 if v else 0)
            self.hi.setValue(v * 1.5 if v else 10)
        self.param.currentTextChanged.connect(self._range)

    def _range(self, k):
        if not k:
            return
        v = float(self.doc.get(self.feat.currentText()).params[k])
        self.lo.setValue(v * 0.5 if v else 0)
        self.hi.setValue(v * 1.5 if v else 10)

    def run(self):
        vals = [round(float(v), 6) for v in np.linspace(self.lo.value(), self.hi.value(), self.n.value())]
        st = None if self.study.currentIndex() == 0 else self.study.currentText()
        metrics = ["mass_kg"] + ([self.metric_box.currentText()] if self.metric_box.currentText() != "mass_kg" else [])
        if st:
            t = self.doc.study(st)["type"]
            metrics += {"static": ["max_von_mises_MPa", "max_displacement_mm", "min_factor_of_safety"],
                        "assembly": ["max_von_mises_MPa", "max_displacement_mm", "min_factor_of_safety"],
                        "frequency": ["first_frequency_Hz"], "buckling": ["first_buckling_factor"],
                        "thermal": ["max_temperature_C"], "flow": ["drag_N", "pressure_drop_Pa"],
                        "drop": ["max_von_mises_MPa", "min_factor_of_safety"]}.get(t, [])
        metrics = list(dict.fromkeys(metrics))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.result = self.doc.design_study(self.feat.currentText(), self.param.currentText(), vals, st,
                                                metrics, progress=lambda m: (self.msg.setText(m),
                                                                             QApplication.processEvents()))
        except FeatureError as e:
            QApplication.restoreOverrideCursor()
            return QMessageBox.warning(self, "Design study", str(e))
        QApplication.restoreOverrideCursor()
        rows = self.result["rows"]
        cols = ["value"] + self.result["metrics"] + (["error"] if any("error" in r for r in rows) else [])
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels([self.result["parameter"]] + cols[1:])
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, c in enumerate(cols):
                v = r.get(c, "")
                self.table.setItem(i, j, QTableWidgetItem(f"{v:.5g}" if isinstance(v, float) else str(v)))
        self.msg.setText(f"{len(rows)} designs evaluated · model restored")
        self._plot()

    def _plot(self):
        if not self.result:
            return
        k = self.metric_box.currentText()
        pts = [(r["value"], r[k]) for r in self.result["rows"] if r.get(k) is not None]
        if pts:
            x, y = zip(*pts)
            self.plot.set_data(list(x), list(y), f"{k} vs {self.result['parameter']}")

    def export(self):
        if not self.result:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export", "design_study.csv", "CSV (*.csv)")
        if p:
            import csv
            rows = self.result["rows"]
            keys = list(dict.fromkeys(k for r in rows for k in r))
            with open(p, "w", newline="") as fh:
                w = csv.DictWriter(fh, keys)
                w.writeheader()
                w.writerows(rows)
