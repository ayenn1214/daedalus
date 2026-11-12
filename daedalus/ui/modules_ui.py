"""Help ▸ Startup modules…: choose which parts of Daedalus load at start (used when no --only flag is given)."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QMessageBox, QVBoxLayout

from .. import modules as MOD


class StartupModulesDialog(QDialog):
    def __init__(self, settings, parent=None, current=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Startup modules")
        v = QVBoxLayout(self)
        intro = QLabel("Load only what you need. Without 3D modelling and Simulation, Daedalus opens a light window "
                       "with no CAD kernel or 3D view — it starts in about a second.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        saved = MOD.load(settings)
        self.boxes = {}
        for key, (label, desc) in MOD.MODULES.items():
            cb = QCheckBox(f"{label} — {desc}")
            cb.setChecked(key in saved)
            self.boxes[key] = cb
            v.addWidget(cb)
        note = QLabel("Simulation needs the 3D view, so it loads it; modelling tools stay hidden unless 3D modelling "
                      "is on. Changes apply next time Daedalus starts.\n"
                      "One-off: python -m daedalus --only inventory   (or --only drawing,inventory · --only sim)")
        note.setObjectName("muted")
        note.setWordWrap(True)
        v.addWidget(note)
        if current is not None and set(current) != set(saved):
            cur = QLabel(f"This session: {MOD.describe(current)} (started with --only)")
            cur.setObjectName("muted")
            v.addWidget(cur)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def chosen(self) -> frozenset:
        return frozenset(k for k, cb in self.boxes.items() if cb.isChecked())

    def save(self):
        mods = self.chosen()
        if not mods:
            QMessageBox.warning(self, "Startup modules", "Pick at least one module.")
            return
        MOD.save(self.settings, mods)
        self.accept()
