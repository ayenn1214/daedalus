"""Sketch ▸ which plane? — the three origin planes, the active part's flat sides, the reference planes, and the
face you clicked; the choice is previewed in the 3D view before the sketch opens."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from . import theme

ORIGIN = [("XY", "Top plane", "XY"), ("XZ", "Front plane", "XZ"), ("YZ", "Right plane", "YZ")]
SIDES = [("top", "Top face"), ("bottom", "Bottom face"), ("front", "Front face"), ("back", "Back face"),
         ("left", "Left face"), ("right", "Right face")]


def plane_options(doc) -> list[tuple[str, str, str]]:
    """[(plane value, label, group)] valid for the active object right now."""
    out = [(v, f"{lab}  ({ax})", "Origin planes") for v, lab, ax in ORIGIN]
    obj = doc.active_object
    for f in doc.features:
        if f.TYPE == "plane" and getattr(f, "obj", obj) == obj and not f.suppressed:
            out.append((f.name, f.name, "Reference planes"))
    if doc.result.bodies.get(obj) is not None:
        for v, lab in SIDES:
            if plane_geometry(doc, v) is not None:
                out.append((v, lab, f"Faces of {obj}" if len(doc.objects) > 1 else "Faces of the part"))
    return out


def plane_geometry(doc, name, offset=0.0):
    """(origin, normal, x_dir, pose) of a sketch plane, in world terms via the object's pose; None if it
    can't be resolved (no such face yet)."""
    try:
        from build123d import Plane

        from ..core.features import BASE_PLANES, BuildContext, resolve_plane
        obj = doc.active_object
        pose = getattr(doc.obj(obj), "pose", None) if obj else None
        if name in BASE_PLANES:
            pl = getattr(Plane, name).offset(offset)
        elif name in getattr(doc.result, "planes", {}):
            pl, owner = doc.result.planes[name]
            pl = pl.offset(offset)
            pose = getattr(doc.obj(owner), "pose", pose)
        else:
            body = doc.result.bodies.get(obj)
            if body is None:
                return None
            pl = resolve_plane(name, offset, BuildContext(body=body))
        o, z, x = pl.origin, pl.z_dir, pl.x_dir
        return (o.X, o.Y, o.Z), (z.X, z.Y, z.Z), (x.X, x.Y, x.Z), pose
    except Exception:  # noqa: BLE001
        return None


def preview_size(doc) -> float:
    body = doc.result.bodies.get(doc.active_object)
    try:
        bb = body.bounding_box()
        return max(60.0, max(bb.size.X, bb.size.Y, bb.size.Z) * 1.3)
    except Exception:  # noqa: BLE001
        return 120.0


class PlanePicker(QDialog):
    def __init__(self, doc, view=None, default="XY", settings=None, parent=None):
        super().__init__(parent)
        self.doc, self.view, self.settings = doc, view, settings
        self.setWindowTitle("Sketch plane")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        v = QVBoxLayout(self)
        lab = QLabel("<b>Where do you want to sketch?</b>")
        v.addWidget(lab)
        sub = QLabel("Pick a plane or a flat side of the part — the view shows it. Double-click to start sketching.")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        v.addWidget(sub)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        opts = plane_options(doc)
        last_group = None
        grid = None
        col = 0
        for value, label, grp in opts:
            if grp != last_group:
                g = QLabel(grp)
                g.setStyleSheet(f"color:{theme.MUTED}; margin-top:6px;")
                v.addWidget(g)
                grid = QGridLayout()
                grid.setSpacing(6)
                v.addLayout(grid)
                last_group, col = grp, 0
            b = QPushButton(label)
            b.setCheckable(True)
            b.setMinimumHeight(34)
            b.setStyleSheet(f"QPushButton:checked {{ background:{theme.ACCENT_SOFT}; border:1px solid "
                            f"{theme.ACCENT_LINE}; color:{theme.ACCENT_TEXT}; font-weight:600; }}")
            b.setProperty("plane", value)
            b.clicked.connect(lambda _=False, val=value: self._pick(val))
            b.installEventFilter(self)
            self.group.addButton(b)
            self.buttons[value] = b
            grid.addWidget(b, col // 3, col % 3)
            col += 1
        row = QHBoxLayout()
        row.addWidget(QLabel("Offset"))
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-100000, 100000)
        self.offset.setDecimals(3)
        self.offset.setSuffix(" mm")
        self.offset.setToolTip("Move the sketch plane along its normal (negative = the other way)")
        self.offset.valueChanged.connect(lambda _v: self._preview())
        row.addWidget(self.offset, 1)
        v.addLayout(row)
        self.ask = QCheckBox("Ask every time (off: use the clicked face, else the Top plane)")
        self.ask.setChecked(True)
        v.addWidget(self.ask)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Sketch")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._value = default if default in self.buttons else "XY"
        self.buttons[self._value].setChecked(True)
        self._preview()

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.MouseButtonDblClick and obj.property("plane"):
            self._pick(obj.property("plane"))
            self.accept()
            return True
        return super().eventFilter(obj, ev)

    def _pick(self, value):
        self._value = value
        b = self.buttons.get(value)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        self._preview()

    def _preview(self):
        if self.view is None:
            return
        g = plane_geometry(self.doc, self._value, self.offset.value())
        if g is None:
            self.view.clear_plane_preview()
            return
        o, n, x, pose = g
        self.view.show_plane_preview(o, n, x, preview_size(self.doc), pose=pose)

    def choice(self):
        return self._value, float(self.offset.value())

    def done(self, r):
        if self.view is not None:
            self.view.clear_plane_preview()
        if r and self.settings is not None and not self.ask.isChecked():
            self.settings.setValue("sketch/ask_plane", False)
        super().done(r)
