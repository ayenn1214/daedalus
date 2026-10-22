"""Measure panel: point-to-point, entities (vertices / edges / faces, one or many) and whole bodies, with results
in the document's unit system (switchable here)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core import measure as M
from ..core.features import FeatureError

MODES = [("points", "Points", "Two snapped points: distance, ΔX/ΔY/ΔZ, plan length, rise, slope"),
         ("entities", "Entities", "Vertices, edges and faces — one (length, radius, area, angle…) or several "
                                  "(min / centre / max distance, angle, normal distance, totals). Click empty "
                                  "space to start over."),
         ("body", "Body", "Click a part: volume, area, mass, weight, centre of mass, bounding box")]


class MeasurePanel(QWidget):
    units_requested = Signal()
    system_picked = Signal(str)
    cleared = Signal()

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.mode = "entities"
        self.refs: list[dict] = []
        self.res = None
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        for i, (key, text, tip) in enumerate(MODES):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setChecked(key == self.mode)
            self.group.addButton(b, i)
            row.addWidget(b)
        self.group.idClicked.connect(self._mode)
        v.addLayout(row)
        urow = QHBoxLayout()
        from .units_ui import SystemCombo
        self.sys = SystemCombo()
        self.sys.picked.connect(self.system_picked.emit)
        urow.addWidget(QLabel("Units"))
        urow.addWidget(self.sys, 1)
        more = QPushButton("…")
        more.setToolTip("Units dialog: per-measurement units, decimals, fractions, converter")
        more.setFixedWidth(28)
        more.clicked.connect(self.units_requested.emit)
        urow.addWidget(more)
        v.addLayout(urow)
        self.picks = QLabel("Click geometry in the view")
        self.picks.setObjectName("muted")
        self.picks.setWordWrap(True)
        v.addWidget(self.picks)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Measurement", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table, 1)
        brow = QHBoxLayout()
        cp = QPushButton("Copy")
        cp.clicked.connect(self.copy)
        cl = QPushButton("Clear")
        cl.clicked.connect(self.clear)
        brow.addWidget(cp)
        brow.addWidget(cl)
        brow.addStretch(1)
        v.addLayout(brow)
        self.sync_units()

    def _mode(self, i):
        self.mode = MODES[i][0]
        self.clear()
        self.picks.setText(MODES[i][2])

    def sync_units(self):
        self.sys.show_system(self.doc.unit_system())
        if self.res is not None:
            self.show_result(self.res)

    def clear(self):
        self.refs = []
        self.res = None
        self.table.setRowCount(0)
        self.picks.setText("Click geometry in the view")
        self.cleared.emit()

    def add_ref(self, ref) -> dict | None:
        """Add a pick (entities / body modes) and re-measure. Returns the result."""
        if self.mode == "body":
            self.refs = [ref]
        else:
            self.refs.append(ref)
        return self.measure()

    def measure(self):
        try:
            res = M.measure(self.doc, self.refs)
        except (FeatureError, Exception) as e:  # noqa: BLE001
            self.picks.setText(f"Can't measure that: {e}")
            return None
        self.show_result(res)
        return res

    def show_points(self, a, b):
        self.refs = []
        res = M.points(self.doc, a, b)
        self.show_result(res, "2 points")
        return res

    def show_result(self, res, what=None):
        self.res = res
        rows = M.format_rows(res)
        self.table.setRowCount(len(rows))
        for r, (k, val) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(k))
            it = QTableWidgetItem(val)
            it.setFlags(it.flags() | Qt.ItemIsSelectable)
            self.table.setItem(r, 1, it)
        if what is None:
            what = " + ".join(r.get("label", r.get("kind", "?")) for r in self.refs)
        self.picks.setText(what)

    def summary(self) -> str:
        if not self.res:
            return ""
        rows = M.format_rows(self.res)
        return " · ".join(f"{k} {v}" for k, v in rows[:4])

    def copy(self):
        if self.res:
            QGuiApplication.clipboard().setText("\n".join(f"{k}\t{v}" for k, v in M.format_rows(self.res)))
