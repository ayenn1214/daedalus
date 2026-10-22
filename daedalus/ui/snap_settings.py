"""Snap & inference settings for 3D (sketch, move, measure, SmartMates), stored in QSettings."""
from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QLabel, QVBoxLayout,
)

from ..core import snap3d


class _Bus(QObject):
    changed = Signal()


bus = _Bus()
_cached = None


def load() -> dict:
    global _cached
    if _cached is not None:
        return dict(_cached)
    st = QSettings("Daedalus", "Daedalus")
    out = dict(snap3d.SETTINGS)
    for k, v in snap3d.SETTINGS.items():
        raw = st.value(f"snap3d/{k}")
        if raw is None:
            continue
        if isinstance(v, bool):
            out[k] = str(raw).lower() in ("true", "1")
        else:
            try:
                out[k] = float(raw)
            except (TypeError, ValueError):
                pass
    _cached = out
    return dict(out)


def save(values: dict):
    global _cached
    st = QSettings("Daedalus", "Daedalus")
    for k, v in values.items():
        st.setValue(f"snap3d/{k}", v)
    _cached = {**snap3d.SETTINGS, **values}
    bus.changed.emit()


def toggle_enabled() -> bool:
    v = load()
    v["enabled"] = not v["enabled"]
    save(v)
    return v["enabled"]


class SnapSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snaps & inference (3D)")
        v = load()
        lay = QVBoxLayout(self)
        self.enabled = QCheckBox("Enable snapping (hold Shift while dragging to bypass)")
        self.enabled.setChecked(v["enabled"])
        lay.addWidget(self.enabled)
        g1 = QGroupBox("Snap to")
        grid = QGridLayout(g1)
        self.boxes = {}
        for i, k in enumerate(snap3d.KINDS):
            c = QCheckBox(snap3d.LABELS[k])
            c.setChecked(bool(v.get(k, True)))
            self.boxes[k] = c
            grid.addWidget(c, i // 2, i % 2)
        lay.addWidget(g1)
        g2 = QGroupBox("Increments")
        f = QFormLayout(g2)

        def spin(val, lo, hi, suf, dec=2):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setValue(float(val))
            s.setSuffix(suf)
            return s
        self.tol = spin(v["tolerance_px"], 3, 40, " px", 0)
        self.len = spin(v["length_step"], 0, 1000, " mm (0 = off)")
        self.ang = spin(v["angle_step"], 0, 90, " ° (0 = off)", 1)
        self.grid = spin(v["grid"], 0, 1000, " mm (0 = automatic by zoom)")
        for lab, w in (("Snap distance", self.tol), ("Length increment", self.len), ("Angle increment", self.ang),
                       ("Grid", self.grid)):
            f.addRow(lab, w)
        lay.addWidget(g2)
        g3 = QGroupBox("Behaviour")
        v3 = QVBoxLayout(g3)
        self.rel = QCheckBox("Add sketch relations from snaps (midpoint, on line, along X/Y/Z)")
        self.rel.setChecked(v["auto_relations"])
        self.smart = QCheckBox("SmartMates: Alt-drag a part onto another to mate the faces (planar → coincident, "
                               "round → concentric)")
        self.smart.setChecked(v["smart_mates"])
        self.smart.setWordWrap(True) if hasattr(self.smart, "setWordWrap") else None
        v3.addWidget(self.rel)
        v3.addWidget(self.smart)
        lay.addWidget(g3)
        note = QLabel("Applies to 3D sketches, Move, Measure and SmartMates. 2D sketches use the Snap menu in "
                      "the sketch bar.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self.defaults)
        lay.addWidget(bb)

    def defaults(self):
        d = snap3d.SETTINGS
        self.enabled.setChecked(True)
        for k, c in self.boxes.items():
            c.setChecked(bool(d.get(k, True)))
        self.tol.setValue(d["tolerance_px"])
        self.len.setValue(d["length_step"])
        self.ang.setValue(d["angle_step"])
        self.grid.setValue(d["grid"])
        self.rel.setChecked(True)
        self.smart.setChecked(True)

    def values(self) -> dict:
        return {"enabled": self.enabled.isChecked(), **{k: c.isChecked() for k, c in self.boxes.items()},
                "tolerance_px": self.tol.value(), "length_step": self.len.value(), "angle_step": self.ang.value(),
                "grid": self.grid.value(), "auto_relations": self.rel.isChecked(),
                "smart_mates": self.smart.isChecked()}

    def accept(self):
        save(self.values())
        super().accept()
