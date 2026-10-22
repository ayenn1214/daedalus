"""Export options: STEP schema, units, mesh quality, per-object files, neutral copies on save."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QVBoxLayout,
)

from ..io.formats import EXPORT_DEFAULTS, STEP_SCHEMAS

AUTO = [("step", "STEP"), ("iges", "IGES"), ("stl", "STL"), ("3mf", "3MF"), ("glb", "glTF")]


class ExportOptionsDialog(QDialog):
    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export options")
        o = {**EXPORT_DEFAULTS, **(doc.export_options or {})}
        lay = QVBoxLayout(self)
        g1 = QGroupBox("Exact geometry (STEP · IGES · BREP)")
        f1 = QFormLayout(g1)
        self.schema = QComboBox()
        self.schema.addItems(list(STEP_SCHEMAS))
        self.schema.setCurrentText(o["step_schema"])
        self.schema.setToolTip("AP214: names, colours, assemblies — the most widely read.\n"
                               "AP242: newest (SolidWorks 2016+, NX, Creo, CATIA, Fusion).\n"
                               "AP203: geometry only, for very old systems.")
        self.units = QComboBox()
        self.units.addItems(["mm", "in"])
        self.units.setCurrentText(o["units"])
        f1.addRow("STEP schema", self.schema)
        f1.addRow("Units", self.units)
        g2 = QGroupBox("Meshes (STL · OBJ · 3MF · PLY · VRML · glTF)")
        f2 = QFormLayout(g2)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0.001, 2.0)
        self.tol.setDecimals(3)
        self.tol.setSingleStep(0.005)
        self.tol.setSuffix(" mm")
        self.tol.setValue(float(o["mesh_tolerance"]))
        self.ang = QDoubleSpinBox()
        self.ang.setRange(0.01, 1.0)
        self.ang.setDecimals(2)
        self.ang.setSingleStep(0.05)
        self.ang.setSuffix(" rad")
        self.ang.setValue(float(o["angular_tolerance"]))
        self.ascii = QCheckBox("ASCII STL (binary is smaller)")
        self.ascii.setChecked(bool(o["stl_ascii"]))
        f2.addRow("Chordal tolerance", self.tol)
        f2.addRow("Angular tolerance", self.ang)
        f2.addRow(self.ascii)
        g3 = QGroupBox("Files")
        f3 = QVBoxLayout(g3)
        self.per = QCheckBox("One file per object")
        self.per.setChecked(bool(o["per_object"]))
        self.vis = QCheckBox("Visible objects only")
        self.vis.setChecked(bool(o["visible_only"]))
        f3.addWidget(self.per)
        f3.addWidget(self.vis)
        row = QHBoxLayout()
        row.addWidget(QLabel("On Save also write:"))
        self.auto = {}
        for key, lab in AUTO:
            c = QCheckBox(lab)
            c.setChecked(key in (o.get("auto_exchange") or []))
            self.auto[key] = c
            row.addWidget(c)
        row.addStretch(1)
        f3.addLayout(row)
        tip = QLabel("STEP is the most versatile exchange format: exact solids every CAD system reads, with "
                     "names, colours and assembly structure. IGES is kept for legacy systems (surfaces only, "
                     "no assembly). Meshes are for printing, rendering and games.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        for w in (g1, g2, g3, tip):
            lay.addWidget(w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {"step_schema": self.schema.currentText(), "units": self.units.currentText(),
                "mesh_tolerance": self.tol.value(), "angular_tolerance": self.ang.value(),
                "stl_ascii": self.ascii.isChecked(), "per_object": self.per.isChecked(),
                "visible_only": self.vis.isChecked(),
                "auto_exchange": [k for k, c in self.auto.items() if c.isChecked()]}
