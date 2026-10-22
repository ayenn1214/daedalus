"""Unit-aware fields, the Units dialog (systems, per-quantity units, decimals, fractions, ft-in) and a unit
converter. Values in the model stay in mm / deg / kg / N / MPa; these widgets show and accept any unit."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import units as U

SETTINGS_KEY = "units/default"


class UnitSpin(QDoubleSpinBox):
    """A spin box holding a base value (mm, deg …) shown in the active unit system. Typing accepts any unit or
    an expression: 1/2", 3' 4-1/2", 25mm+2, 2*pi."""

    def __init__(self, quantity="length", parent=None):
        super().__init__(parent)
        self.quantity = quantity
        self.setDecimals(9)
        self.setRange(-1e9, 1e9)
        self.setKeyboardTracking(False)
        self.setAccelerated(True)
        self.setCorrectionMode(QDoubleSpinBox.CorrectToPreviousValue)
        self._step()
        self.setToolTip(self.toolTip() or "Type a value in any unit, e.g. 1/2\", 3' 4\", 25 mm, 2 m, 45°, 1 rad")

    def _step(self):
        try:
            u = U.active().unit(self.quantity)
            step = U.to_base(1.0, u, self.quantity)
            if self.quantity == "length" and u in ("ft", "m"):
                step = U.to_base(1.0, "in" if u == "ft" else "cm", "length")
            self.setSingleStep(step)
        except Exception:  # noqa: BLE001
            self.setSingleStep(1.0)

    def refresh_units(self):
        self._step()
        v = self.value()
        self.blockSignals(True)
        self.setValue(v + 1e-12)
        self.setValue(v)
        self.blockSignals(False)
        self.lineEdit().setText(self.textFromValue(v))

    def textFromValue(self, v):            # noqa: N802 (Qt)
        return U.fmt(v, self.quantity)

    def valueFromText(self, text):         # noqa: N802
        v = U.try_parse(text, self.quantity)
        return self.value() if v is None else v

    def validate(self, text, pos):
        if U.try_parse(text, self.quantity) is not None:
            return QValidator.Acceptable, text, pos
        return QValidator.Intermediate, text, pos

    def fixup(self, text):
        return self.textFromValue(self.value())


def ask_quantity(parent, title, label, value, quantity="length", minimum=None):
    """Ask for a value in any unit; returns the base value or None."""
    text = U.fmt(value, quantity)
    while True:
        txt, ok = QInputDialog.getText(parent, title, f"{label} [{U.label(quantity)} — any unit OK]:", text=text)
        if not ok:
            return None
        try:
            v = U.parse(txt, quantity)
        except (U.UnitError, ZeroDivisionError, ValueError) as e:
            QMessageBox.warning(parent, title, str(e) or "Can't read that value")
            text = txt
            continue
        if minimum is not None and v < minimum:
            QMessageBox.warning(parent, title, f"Must be at least {U.fmt(minimum, quantity)}")
            text = txt
            continue
        return v


def default_units(settings) -> dict | None:
    import json
    try:
        raw = settings.value(SETTINGS_KEY, "")
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


class SystemCombo(QComboBox):
    """Status-bar unit system switcher."""
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        for k, s in U.SYSTEMS.items():
            self.addItem(s.label, k)
        self.setToolTip("Units — what you see and type. The model is unchanged (geometry is stored exactly).")
        self.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.activated.connect(lambda i: self.picked.emit(self.itemData(i)))

    def show_system(self, sys):
        i = self.findData(sys.name)
        self.blockSignals(True)
        if i >= 0:
            self.setCurrentIndex(i)
        self.blockSignals(False)


class ConverterPanel(QWidget):
    """Any value, any quantity: every unit at once (and ft-in-fractions)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.q = QComboBox()
        for k, lab in U.quantities().items():
            if k != "ratio":
                self.q.addItem(lab, k)
        self.inp = QLineEdit("1 in")
        self.inp.setPlaceholderText("Value with a unit — 5'-6 1/4\", 3 kip, 50 psf, 4/12, 72°F…")
        row.addWidget(self.q)
        row.addWidget(self.inp, 1)
        v.addLayout(row)
        self.err = QLabel()
        self.err.setObjectName("muted")
        v.addWidget(self.err)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Unit", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table, 1)
        cp = QPushButton("Copy table")
        cp.clicked.connect(self.copy)
        v.addWidget(cp, 0, Qt.AlignLeft)
        self.q.currentIndexChanged.connect(self.update)
        self.inp.textChanged.connect(self.update)
        self.q.setCurrentIndex(self.q.findData("length"))
        self.update()

    def update(self):
        q = self.q.currentData()
        try:
            base = U.parse(self.inp.text(), q)
            self.err.setText("")
        except Exception as e:  # noqa: BLE001
            self.err.setText(str(e) or "Can't read that")
            return
        rows = []
        for u in U.units_of(q):
            try:
                rows.append((u if u else "—", U.fmt(base, q, unit=u, places=6, show_unit=False)))
            except Exception:  # noqa: BLE001
                continue
        if q == "length":
            rows += [("ft-in (1/16)", U.fmt_ft_in(base, 16)), ("ft-in (1/32)", U.fmt_ft_in(base, 32)),
                     ("in fraction (1/64)", U.fmt_fraction_in(base / U.IN, 64))]
        self.table.setRowCount(len(rows))
        for r, (a, b) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(a))
            self.table.setItem(r, 1, QTableWidgetItem(b))

    def copy(self):
        QGuiApplication.clipboard().setText("\n".join(
            f"{self.table.item(r, 1).text()} {self.table.item(r, 0).text()}" for r in range(self.table.rowCount())))


class UnitsDialog(QDialog):
    """Unit system (presets incl. construction), per-measurement-type units, decimals, fractions."""

    def __init__(self, doc, settings=None, parent=None, tab=0):
        super().__init__(parent)
        self.doc, self.settings = doc, settings
        self.setWindowTitle("Units")
        self.resize(640, 640)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        w = QWidget()
        v = QVBoxLayout(w)
        top = QFormLayout()
        self.sys = QComboBox()
        for k, s in U.SYSTEMS.items():
            self.sys.addItem(s.label, k)
        top.addRow("Unit system", self.sys)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        top.addRow("", self.note)
        g = QGridLayout()
        self.fmt = QComboBox()
        self.fmt.addItems(["decimal", "fractional", "ft-in"])
        self.fmt.setToolTip("decimal: 12.375 · fractional: 12 3/8\" · ft-in: 1'-0 3/8\" (architectural)")
        self.den = QComboBox()
        for dn in (2, 4, 8, 16, 32, 64, 128):
            self.den.addItem(f"1/{dn}", dn)
        self.dec_len, self.dec_ang, self.dec_def = QSpinBox(), QSpinBox(), QSpinBox()
        for sp in (self.dec_len, self.dec_ang, self.dec_def):
            sp.setRange(0, 8)
        g.addWidget(QLabel("Length format"), 0, 0)
        g.addWidget(self.fmt, 0, 1)
        g.addWidget(QLabel("Round to"), 0, 2)
        g.addWidget(self.den, 0, 3)
        g.addWidget(QLabel("Length decimals"), 1, 0)
        g.addWidget(self.dec_len, 1, 1)
        g.addWidget(QLabel("Angle decimals"), 1, 2)
        g.addWidget(self.dec_ang, 1, 3)
        g.addWidget(QLabel("Other decimals"), 2, 0)
        g.addWidget(self.dec_def, 2, 1)
        top.addRow(g)
        v.addLayout(top)
        v.addWidget(QLabel("Measurement types"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Measurement", "Unit", "Example"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        v.addWidget(self.table, 1)
        self.default_cb = QCheckBox("Use for new documents")
        v.addWidget(self.default_cb)
        tabs.addTab(w, "Units")
        tabs.addTab(ConverterPanel(), "Converter")
        tabs.setCurrentIndex(tab)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Reset)
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Reset).setText("Reset to preset")
        bb.button(QDialogButtonBox.Reset).clicked.connect(lambda: self.load(U.system_from(self.sys.currentData())))
        lay.addWidget(bb)
        self.combos: dict[str, QComboBox] = {}
        self.sys.activated.connect(lambda _i: self.load(U.system_from(self.sys.currentData())))
        for wdg in (self.fmt, self.den):
            wdg.currentIndexChanged.connect(self.preview)
        for sp in (self.dec_len, self.dec_ang, self.dec_def):
            sp.valueChanged.connect(self.preview)
        self.load(doc.unit_system())

    EXAMPLES = {"length": 1682.75, "angle": 33.69, "slope": 18.435, "area": 9.29e6, "volume": 7.6455e8,
                "mass": 45.36, "density": 7.85, "linear_density": 18.17, "force": 4448.2, "torque": 135.58,
                "pressure": 250.0, "linear_load": 7.3, "area_load": 2.394, "stiffness": 175.1, "time": 90.0,
                "frequency": 50.0, "angular_velocity": 1750.0, "velocity": 254.0, "acceleration": 9806.65,
                "energy": 1355.8, "power": 745.7, "temperature": 20.0, "temp_delta": 10.0,
                "area_moment": 3.24e6, "section_modulus": 6.38e4, "mass_moment": 1.2e5, "flow": 37.85,
                "thermal_conductivity": 50.0, "energy_density": 1e5, "electric_current": 2.0, "voltage": 24.0}

    def load(self, s):
        self.sys.setCurrentIndex(max(0, self.sys.findData(s.name)))
        self.note.setText(s.note or "")
        self.fmt.setCurrentText(s.length_format)
        self.den.setCurrentIndex(max(0, self.den.findData(s.denominator)))
        self.dec_len.setValue(s.places("length"))
        self.dec_ang.setValue(s.places("angle"))
        self.dec_def.setValue(int(s.decimals.get("default", 4)))
        qs = [q for q in U.Q if q != "ratio"]
        self.table.setRowCount(len(qs))
        self.combos = {}
        for r, q in enumerate(qs):
            it = QTableWidgetItem(U.Q[q][0])
            it.setData(Qt.UserRole, q)
            self.table.setItem(r, 0, it)
            cb = QComboBox()
            cb.addItems(U.units_of(q))
            cb.setCurrentText(s.unit(q))
            cb.currentIndexChanged.connect(self.preview)
            self.table.setCellWidget(r, 1, cb)
            self.combos[q] = cb
            self.table.setItem(r, 2, QTableWidgetItem(""))
        self.preview()

    def current(self) -> dict:
        base = U.SYSTEMS[self.sys.currentData()]
        ov = {q: cb.currentText() for q, cb in self.combos.items() if cb.currentText() != base.unit(q)}
        d = {"name": base.name}
        if ov:
            d["units"] = ov
        dec = {"length": self.dec_len.value(), "angle": self.dec_ang.value(), "default": self.dec_def.value()}
        if dec != {k: base.places(k) if k != "default" else int(base.decimals.get("default", 4)) for k in dec}:
            d["decimals"] = dec
        if self.fmt.currentText() != base.length_format:
            d["length_format"] = self.fmt.currentText()
        if self.den.currentData() != base.denominator:
            d["denominator"] = self.den.currentData()
        return d

    def preview(self):
        try:
            s = U.system_from(self.current())
        except Exception:  # noqa: BLE001
            return
        for r in range(self.table.rowCount()):
            q = self.table.item(r, 0).data(Qt.UserRole)
            try:
                txt = U.fmt(self.EXAMPLES.get(q, 1.0), q, s)
            except Exception:  # noqa: BLE001
                txt = ""
            self.table.item(r, 2).setText(txt)
        self.den.setEnabled(self.fmt.currentText() != "decimal")

    def apply(self):
        d = self.current()
        self.doc.set_units(reset=True, **{k: v for k, v in d.items() if k != "name"}, name=d["name"])
        if self.settings is not None and self.default_cb.isChecked():
            import json
            self.settings.setValue(SETTINGS_KEY, json.dumps(d))
            type(self.doc).default_units = d
        self.accept()
