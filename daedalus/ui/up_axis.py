"""'Which way is up?' after an import: SolidWorks, Fusion, Inventor… save Y-up by default, Daedalus is Z-up.
Live preview in the 3D view; the choice can be remembered per source program."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QPushButton, QVBoxLayout,
)

from . import theme

CHOICES = [("+Z", "Z up", "already right"), ("+Y", "Y up", "SolidWorks / Fusion default"),
           ("-Y", "−Y up", ""), ("+X", "X up", ""), ("-X", "−X up", ""), ("-Z", "−Z up", "upside down")]
Y_UP = ("solidworks", "fusion", "inventor", "creo", "pro/engineer", "catia", "nx", "siemens", "spaceclaim",
        "solid edge", "onshape", "freecad", "blender", "unity", "rhino")


NICE = {"solidworks": "SolidWorks", "fusion": "Fusion", "inventor": "Inventor", "creo": "Creo",
        "pro/engineer": "Pro/ENGINEER", "catia": "CATIA", "nx": "NX", "siemens": "Siemens NX", "spaceclaim": "SpaceClaim",
        "solid edge": "Solid Edge", "onshape": "Onshape", "freecad": "FreeCAD", "rhino": "Rhino",
        "build123d": "build123d", "open cascade": "Open CASCADE", "daedalus": "Daedalus", "autocad": "AutoCAD"}


def nice(program: str) -> str:
    return NICE.get(program, program.title())


def source_program(path) -> str:
    """Originating CAD program from a STEP / IGES header ('' when unknown)."""
    p = Path(path)
    try:
        head = p.open("r", errors="ignore").read(6000)
    except OSError:
        return ""
    if p.suffix.lower() in (".step", ".stp"):
        m = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", head, re.S)
        if m:
            fields = re.findall(r"'([^']*)'", m.group(1))
            text = " ".join(fields[-3:]).lower()
            for k in Y_UP + ("autocad", "catia", "build123d", "open cascade", "daedalus"):
                if k in text:
                    return k
            return fields[-2].lower().strip() if len(fields) >= 2 else ""
    if p.suffix.lower() in (".iges", ".igs"):
        low = head.lower()
        for k in Y_UP:
            if k in low:
                return k
    if p.suffix.lower() in (".sldprt", ".sldasm"):
        return "solidworks"
    return ""


def suggest(settings, program: str) -> str:
    saved = settings.value(f"import/up/{program or 'other'}")
    if saved in dict((c[0], 1) for c in CHOICES):
        return saved
    if program in ("daedalus", "build123d", "open cascade"):
        return "+Z"
    return "+Y" if program in Y_UP else "+Z"


class UpAxisDialog(QDialog):
    def __init__(self, doc, view, names, program="", settings=None, parent=None):
        super().__init__(parent)
        self.doc, self.view, self.names = doc, view, list(names)
        self.settings = settings
        self.program = program
        self.base = {n: list(doc.obj(n).pose) for n in self.names}
        self.setWindowTitle("Which way is up?")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        v = QVBoxLayout(self)
        who = f" (made in {nice(program)})" if program else ""
        lab = QLabel(f"<b>Which axis was up in the original model{who}?</b><br>"
                     f"<span style='color:{theme.MUTED}'>Daedalus is Z-up. Pick the axis that should point up — the "
                     "view previews it.</span>")
        lab.setWordWrap(True)
        v.addWidget(lab)
        grid = QGridLayout()
        self.group = QButtonGroup(self)
        start = suggest(settings, program) if settings is not None else "+Z"
        for i, (key, text, hint) in enumerate(CHOICES):
            b = QPushButton(f"{text}" + (f"\n{hint}" if hint else ""))
            b.setCheckable(True)
            b.setMinimumHeight(44)
            b.setStyleSheet(f"QPushButton:checked {{ background:{theme.ACCENT_SOFT}; border:1px solid {theme.ACCENT_LINE};"
                            f" font-weight:600; }}")
            b.setProperty("key", key)
            self.group.addButton(b, i)
            grid.addWidget(b, i // 3, i % 3)
            if key == start:
                b.setChecked(True)
        self.group.idClicked.connect(lambda _i: self.preview())
        for c in range(3):
            grid.setColumnStretch(c, 1)
        v.addLayout(grid)
        self.floor = QCheckBox("Set it on the floor (lowest point at Z = 0)")
        self.floor.setChecked(True)
        self.floor.toggled.connect(lambda _v: self.preview())
        v.addWidget(self.floor)
        self.remember = QCheckBox(f"Remember for files from {nice(program) if program else 'unknown programs'}")
        self.remember.setChecked(bool(program))
        v.addWidget(self.remember)
        self.never = QCheckBox("Don't ask on import (Edit ▸ Which way is up… still works)")
        v.addWidget(self.never)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.button(QDialogButtonBox.Cancel).setText("Leave as imported")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.preview()

    def up(self):
        b = self.group.checkedButton()
        return b.property("key") if b else "+Z"

    def preview(self):
        try:
            poses = self.doc.up_poses(self.names, self.up(), self.floor.isChecked(), self.base)
            self.view.update_poses(poses)
            self.view.fit()
        except Exception:  # noqa: BLE001
            pass

    def done(self, r):
        self.view.update_poses(self.base)                    # back to the model's state; apply() commits
        if r and self.settings is not None:
            if self.remember.isChecked():
                self.settings.setValue(f"import/up/{self.program or 'other'}", self.up())
        if self.settings is not None and self.never.isChecked():
            self.settings.setValue("import/ask_up", False)
        super().done(r)

    def apply(self):
        """Commit the choice to the document (one undo step)."""
        if self.up() == "+Z" and not self.floor.isChecked():
            return None
        return self.doc.set_up_axis(self.names, self.up(), self.floor.isChecked())
