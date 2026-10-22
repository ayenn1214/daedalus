"""HSS builder: pick a standard hollow structural section (AISC, CSA, EN 10219, EN 10210) or define a non-standard
one, see its section properties in your units, and insert it as a straight member or as a frame along a sketch."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core import hss as H
from ..core import units as U
from ..core.features import FeatureError
from . import theme
from .units_ui import UnitSpin


class SectionView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.h: H.HSS | None = None
        self.setMinimumSize(200, 200)

    def set_section(self, h):
        self.h = h
        self.update()

    def paintEvent(self, _e):          # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(24, 27, 30))
        h = self.h
        if h is None:
            return
        W, Hh = self.width(), self.height()
        s = 0.72 * min(W / max(h.B, 1e-6), Hh / max(h.H, 1e-6))
        cx, cy = W / 2, Hh / 2
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        if h.shape == "round":
            for r in (h.B / 2, h.B / 2 - h.t):
                path.addEllipse(QPointF(cx, cy), r * s, r * s)
        else:
            for poly in (H._rounded_rect(h.B, h.H, h.ro, 12),
                         H._rounded_rect(h.B - 2 * h.t, h.H - 2 * h.t, max(0.0, h.ro - h.t), 12)):
                pts = [QPointF(cx + x * s, cy - y * s) for x, y in poly]
                path.moveTo(pts[0])
                for q in pts[1:]:
                    path.lineTo(q)
                path.closeSubpath()
        p.setPen(QPen(QColor.fromRgbF(*theme.VIEW_HILITE), 1.6))
        p.setBrush(QColor(120, 140, 160, 140))
        p.drawPath(path)
        p.setPen(QColor(200, 205, 210))
        txt = (f"OD {U.fmt(h.B)}  t {U.fmt(h.t)}" if h.shape == "round" else
               f"{U.fmt(h.H)} × {U.fmt(h.B)}  t {U.fmt(h.t)}  ro {U.fmt(h.ro)}")
        p.drawText(8, Hh - 10, txt)
        p.drawText(8, 18, h.designation)


class HSSBuilder(QDialog):
    def __init__(self, doc, parent=None, edit: str | None = None):
        super().__init__(parent)
        self.doc = doc
        self.edit_name = edit
        self.setWindowTitle("HSS builder")
        self.resize(1040, 700)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        # ---- left: choose
        left = QWidget()
        lv = QVBoxLayout(left)
        f = QFormLayout()
        self.std = QComboBox()
        for k in H.STANDARDS:
            self.std.addItem(H.STANDARD_LABELS[k], k)
        f.addRow("Standard", self.std)
        self.shape = QComboBox()
        for k, lab in (("square", "Square (SHS)"), ("rect", "Rectangular (RHS)"), ("round", "Round (CHS / pipe)")):
            self.shape.addItem(lab, k)
        f.addRow("Shape", self.shape)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter, e.g. 4x4, 102x102, 200x100, 6.625")
        f.addRow("Find", self.search)
        lv.addLayout(f)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Designation", "Size", "Wall (design)", "Weight"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lv.addWidget(self.table, 1)
        self.custom_box = QGroupBox("Custom / non-standard section")
        cf = QFormLayout(self.custom_box)
        self.cB, self.cH, self.ct = UnitSpin(), UnitSpin(), UnitSpin()
        for w, v in ((self.cB, 101.6), (self.cH, 152.4), (self.ct, 6.35)):
            w.setRange(0.01, 1e5)
            w.setValue(v)
        cf.addRow("Width B / OD", self.cB)
        cf.addRow("Depth H", self.cH)
        cf.addRow("Wall t", self.ct)
        lv.addWidget(self.custom_box)
        cr = QFormLayout()
        self.rule = QComboBox()
        for k, lab in (("standard", "Standard rule"), ("2t", "2·t"), ("1.5t", "1.5·t"), ("2.5t", "2.5·t"),
                       ("3t", "3·t"), ("custom", "Custom radius"), ("sharp", "Sharp (0)")):
            self.rule.addItem(lab, k)
        self.rule.setToolTip("Outside corner radius. Standard: AISC/CSA 2t; EN 10219 2t / 2.5t / 3t by wall; "
                             "EN 10210 1.5t")
        self.rad = UnitSpin()
        self.rad.setRange(0.0, 1e4)
        self.rad.setValue(12.0)
        cr.addRow("Corner radius", self.rule)
        cr.addRow("Radius (custom)", self.rad)
        lv.addLayout(cr)
        split.addWidget(left)

        # ---- middle: preview + properties
        mid = QWidget()
        mv = QVBoxLayout(mid)
        self.view = SectionView()
        mv.addWidget(self.view, 2)
        self.props = QTableWidget(0, 2)
        self.props.setHorizontalHeaderLabels(["Property", "Value"])
        self.props.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.props.verticalHeader().setVisible(False)
        self.props.setEditTriggers(QTableWidget.NoEditTriggers)
        mv.addWidget(self.props, 3)
        self.err = QLabel()
        self.err.setStyleSheet("color:#e57373")
        self.err.setWordWrap(True)
        mv.addWidget(self.err)
        split.addWidget(mid)

        # ---- right: insert
        right = QWidget()
        rv = QVBoxLayout(right)
        self.how = QButtonGroup(self)
        self.r_member = QRadioButton("Straight member")
        self.r_frame = QRadioButton("Frame along a sketch")
        self.r_update = QRadioButton("Update the selected feature")
        for i, b in enumerate((self.r_member, self.r_frame, self.r_update)):
            self.how.addButton(b, i)
            rv.addWidget(b)
        self.r_member.setChecked(True)
        mf = QFormLayout()
        self.length = UnitSpin()
        self.length.setRange(0.1, 1e7)
        self.length.setValue(1000.0)
        mf.addRow("Length", self.length)
        self.axis = QComboBox()
        self.axis.addItems(["Z", "X", "Y", "-Z", "-X", "-Y"])
        mf.addRow("Direction", self.axis)
        self.px, self.py, self.pz = UnitSpin(), UnitSpin(), UnitSpin()
        for w in (self.px, self.py, self.pz):
            w.setRange(-1e7, 1e7)
        mf.addRow("Start X", self.px)
        mf.addRow("Start Y", self.py)
        mf.addRow("Start Z", self.pz)
        self.rot = UnitSpin("angle")
        self.rot.setRange(-360, 360)
        mf.addRow("Rotate section", self.rot)
        self.m0, self.m1 = UnitSpin("angle"), UnitSpin("angle")
        for w in (self.m0, self.m1):
            w.setRange(-79, 79)
        mf.addRow("Start mitre", self.m0)
        mf.addRow("End mitre", self.m1)
        self.caps = QCheckBox("Cap plates on open ends")
        mf.addRow(self.caps)
        self.cap_t = UnitSpin()
        self.cap_t.setRange(0.1, 1000)
        self.cap_t.setValue(6.0)
        mf.addRow("Cap thickness", self.cap_t)
        self.sketch = QComboBox()
        for g in doc.features:
            if g.TYPE in ("sketch3d", "sketch"):
                self.sketch.addItem(g.name)
        self.sketch.setCurrentIndex(self.sketch.count() - 1)
        mf.addRow("Frame sketch", self.sketch)
        rv.addLayout(mf)
        rv.addStretch(1)
        btns = QHBoxLayout()
        self.ok = QPushButton("Insert")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.insert)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(self.ok)
        btns.addWidget(close)
        rv.addLayout(btns)
        split.addWidget(right)
        split.setSizes([400, 360, 280])

        self.std.currentIndexChanged.connect(self.fill)
        self.shape.currentIndexChanged.connect(self.fill)
        self.search.textChanged.connect(self.fill)
        self.table.itemSelectionChanged.connect(self.update_props)
        for w in (self.cB, self.cH, self.ct, self.rad):
            w.valueChanged.connect(self.update_props)
        self.rule.currentIndexChanged.connect(self.update_props)
        self.how.idClicked.connect(self._how)
        self.r_frame.setEnabled(self.sketch.count() > 0)
        self.r_update.setEnabled(bool(edit))
        if edit:
            self._load_edit(edit)
        else:
            self.std.setCurrentIndex(self.std.findData("CSA" if U.active().unit("length") in ("mm", "m", "cm")
                                                       else "AISC"))
            self.fill()
        self._how(self.how.checkedId())

    # ------------------------------------------------------------------ ui
    def _how(self, i):
        member = i == 0 or (i == 2 and self.edit_name and self.doc.get(self.edit_name).TYPE == "hss")
        for w in (self.length, self.axis, self.px, self.py, self.pz, self.rot, self.m0, self.m1, self.caps,
                  self.cap_t):
            w.setEnabled(bool(member))
        self.sketch.setEnabled(i == 1)
        self.ok.setText("Update" if i == 2 else "Insert")

    def fill(self):
        std = self.std.currentData()
        custom = std == "custom"
        self.custom_box.setVisible(custom)
        self.table.setVisible(not custom)
        self.search.setEnabled(not custom)
        if custom:
            self.cH.setEnabled(self.shape.currentData() == "rect")
            self.update_props()
            return
        rows = H.sizes(std, self.shape.currentData(), self.search.text())
        self.table.setRowCount(len(rows))
        self._rows = rows
        for r, h in enumerate(rows):
            props = H.properties(h)
            size = U.fmt(h.B) if h.shape == "round" else f"{U.fmt(h.H, show_unit=False)} × {U.fmt(h.B)}"
            for c, txt in enumerate((h.designation, size, U.fmt(h.t), U.fmt(props["mass_per_m"], "linear_density"))):
                self.table.setItem(r, c, QTableWidgetItem(txt))
        if rows:
            self.table.selectRow(0)
        self.update_props()

    def section(self) -> H.HSS:
        std = self.std.currentData()
        if std == "custom":
            return H.custom(self.shape.currentData(), self.cB.value(), self.cH.value(), self.ct.value(),
                            self.rule.currentData(), self.rad.value())
        r = self.table.currentRow()
        rows = getattr(self, "_rows", [])
        if not (0 <= r < len(rows)):
            raise ValueError("Pick a size")
        h = rows[r]
        rule = self.rule.currentData()
        if rule != "standard" and h.shape != "round":
            ro, ri = H.corner_radii(std, h.t, rule, self.rad.value())
            h = H.HSS(**{**h.to_dict(), "ro": ro, "ri": ri})
        return h

    def update_props(self):
        self.rad.setEnabled(self.rule.currentData() == "custom")
        try:
            h = self.section()
        except (ValueError, KeyError) as e:
            self.err.setText(str(e))
            self.view.set_section(None)
            self.props.setRowCount(0)
            return
        self.err.setText("")
        self.view.set_section(h)
        pr = H.properties(h)
        rows = [("Designation", h.designation), ("Standard", H.STANDARD_LABELS[h.standard]),
                ("Width B" if h.shape != "round" else "OD", U.fmt(h.B))]
        if h.shape != "round":
            rows += [("Depth H", U.fmt(h.H)), ("Outside corner radius", U.fmt(h.ro))]
        rows += [("Wall (nominal)", U.fmt(h.t_nom)), ("Wall (design)", U.fmt(h.t))]
        for k, v in pr.items():
            q = H.PROP_QUANTITY.get(k)
            if k == "surface_per_m":
                rows.append(("Outside perimeter (paint area per length)", U.fmt(H.perimeter(h))))
                continue
            rows.append((H.PROP_LABEL.get(k, k), U.fmt(v, q) if q else f"{v:.2f}"))
        self.props.setRowCount(len(rows))
        for r, (a, b) in enumerate(rows):
            self.props.setItem(r, 0, QTableWidgetItem(a))
            self.props.setItem(r, 1, QTableWidgetItem(b))

    def _load_edit(self, name):
        f = self.doc.get(name)
        p = f.params
        self.r_update.setChecked(True)
        std = p.get("standard", "custom")
        self.std.setCurrentIndex(max(0, self.std.findData(std)))
        try:
            h = f.hss() if f.TYPE == "frame" else f.section()
        except FeatureError:
            h = None
        if h is not None:
            self.shape.setCurrentIndex(max(0, self.shape.findData(h.shape)))
        self.fill()
        if std != "custom" and p.get("designation"):
            for r, row in enumerate(getattr(self, "_rows", [])):
                if H._key(row.designation) == H._key(p["designation"]):
                    self.table.selectRow(r)
        if f.TYPE == "hss":
            self.cB.setValue(p["width"])
            self.cH.setValue(p["height"])
            self.ct.setValue(p["thickness"])
            self.rule.setCurrentIndex(max(0, self.rule.findData(p["corner_rule"])))
            self.rad.setValue(p["corner_radius"])
            self.length.setValue(p["length"])
            self.axis.setCurrentText(p["axis"])
            self.px.setValue(p["x"])
            self.py.setValue(p["y"])
            self.pz.setValue(p["z"])
            self.rot.setValue(p["rotation"])
            self.m0.setValue(p["start_miter"])
            self.m1.setValue(p["end_miter"])
            self.caps.setChecked(p["end_caps"])
            self.cap_t.setValue(p["cap_thickness"])
        elif f.TYPE == "frame":
            self.cB.setValue(p["width"])
            self.cH.setValue(p["height"])
            self.ct.setValue(p["thickness"])
            if p.get("corner_radius"):
                self.rule.setCurrentIndex(self.rule.findData("custom"))
                self.rad.setValue(p["corner_radius"])
        self.update_props()

    # --------------------------------------------------------------- apply
    def params(self, for_type: str) -> dict:
        h = self.section()
        std = h.standard
        if for_type == "hss":
            p = {"standard": std, "designation": h.designation if std != "custom" else "",
                 "shape": h.shape, "width": h.B, "height": h.H, "thickness": h.t,
                 "corner_rule": self.rule.currentData() if std != "custom" or self.rule.currentData() != "standard"
                 else "2t", "corner_radius": h.ro,
                 "length": self.length.value(), "axis": self.axis.currentText(), "x": self.px.value(),
                 "y": self.py.value(), "z": self.pz.value(), "rotation": self.rot.value(),
                 "start_miter": self.m0.value(), "end_miter": self.m1.value(), "end_caps": self.caps.isChecked(),
                 "cap_thickness": self.cap_t.value()}
            if std == "custom":
                p["corner_rule"] = "custom" if h.ro else "sharp"
            return p
        kind = {"square": "square_tube", "rect": "rect_tube", "round": "round_tube"}[h.shape]
        p = {"profile": kind, "width": h.B, "height": h.H, "thickness": h.t, "corner_radius": h.ro}
        if std != "custom" and self.rule.currentData() == "standard":
            p.update(standard=std, designation=h.designation)
        else:
            p.update(standard="custom", designation="")
        return p

    def insert(self):
        try:
            i = self.how.checkedId()
            if i == 2 and self.edit_name:
                t = self.doc.get(self.edit_name).TYPE
                self.doc.edit(self.edit_name, self.params(t))
                self.created = self.edit_name
            elif i == 1:
                if not self.sketch.currentText():
                    raise FeatureError("Draw a 3D sketch (or sketch) of the frame layout first")
                f = self.doc.add("frame", {"path": self.sketch.currentText(), **self.params("frame")})
                self.created = f.name
            else:
                f = self.doc.add("hss", self.params("hss"))
                self.created = f.name
        except (FeatureError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "HSS builder", str(e))
            return
        err = self.doc.result.errors.get(self.created)
        if err:
            QMessageBox.warning(self, "HSS builder", err)
            return
        self.accept()
