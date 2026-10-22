"""Material picker: searchable library grouped by category, property sheet, custom materials,
apply to the document or one object."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import materials as matlib
from ..core.features import FeatureError

FIELDS = [  # attr, label, unit, decimals, max
    ("density", "Density", "g/cm³", 3, 30),
    ("E", "Elastic modulus", "GPa", 3, 1000),
    ("nu", "Poisson's ratio", "", 3, 0.499),
    ("yield_", "Yield strength", "MPa", 1, 5000),
    ("uts", "Tensile strength", "MPa", 1, 5000),
    ("fatigue", "Fatigue strength", "MPa", 1, 3000),
    ("fatigue_cycles", "  at cycles", "", 0, 1e10),
    ("k", "Thermal conductivity", "W/(m·K)", 3, 5000),
    ("cp", "Specific heat", "J/(kg·K)", 0, 10000),
    ("alpha", "Thermal expansion", "µm/(m·K)", 2, 500),
]


class MaterialDialog(QDialog):
    def __init__(self, doc, obj: str | None = None, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("Material")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search (e.g. 6061, stainless, nylon)")
        self.search.textChanged.connect(self._fill)
        ll.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._show)
        self.tree.itemDoubleClicked.connect(lambda *_: self.apply())
        ll.addWidget(self.tree)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight:600; font-size:11pt;")
        self.title.setWordWrap(True)
        rl.addWidget(self.title)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Custom material name")
        rl.addWidget(self.name_edit)
        form = QFormLayout()
        self.spins = {}
        for attr, label, unit, dec, mx in FIELDS:
            sp = QDoubleSpinBox()
            sp.setDecimals(dec)
            sp.setRange(0, mx)
            if unit:
                sp.setSuffix(" " + unit)
            if attr == "fatigue_cycles":
                sp.setSingleStep(1e6)
            self.spins[attr] = sp
            form.addRow(label, sp)
        self.ductile = QCheckBox("Ductile (FoS on yield; brittle uses tensile strength)")
        self.endurance = QCheckBox("Has an endurance limit")
        form.addRow(self.ductile)
        form.addRow(self.endurance)
        rl.addLayout(form)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        rl.addWidget(self.note)
        rl.addStretch(1)
        split.addWidget(right)
        split.setSizes([330, 430])
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Apply to"))
        self.target = QComboBox()
        self.target.addItem("Document (objects without their own)", None)
        for o in doc.objects:
            self.target.addItem(f"Object: {o.name}", o.name)
        if obj:
            i = self.target.findData(obj)
            if i >= 0:
                self.target.setCurrentIndex(i)
        row.addWidget(self.target, 1)
        self.custom_btn = QPushButton("New custom from this")
        self.custom_btn.clicked.connect(self._start_custom)
        self.save_btn = QPushButton("Save custom")
        self.save_btn.clicked.connect(self._save_custom)
        apply = QPushButton("Apply")
        apply.setObjectName("primary")
        apply.clicked.connect(self.apply)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for b in (self.custom_btn, self.save_btn, apply, close):
            row.addWidget(b)
        lay.addLayout(row)
        self._editing = False
        self._fill()
        self._select(doc.material_of(obj).name)

    # ------------------------------------------------------------- list
    def _fill(self, *_):
        cur = self.current_name()
        self.tree.clear()
        cats = {}
        for m in matlib.search(self.search.text(), custom=self.doc.custom_materials):
            cat = "Custom" if m.custom else m.category
            if cat not in cats:
                cats[cat] = QTreeWidgetItem([cat])
                cats[cat].setFlags(Qt.ItemIsEnabled)
                self.tree.addTopLevelItem(cats[cat])
            it = QTreeWidgetItem([m.name])
            it.setData(0, Qt.UserRole, m.name)
            it.setToolTip(0, m.summary())
            cats[cat].addChild(it)
        self.tree.expandAll() if self.search.text() else None
        if cur:
            self._select(cur)

    def _select(self, name):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).data(0, Qt.UserRole) == name:
                    top.setExpanded(True)
                    self.tree.setCurrentItem(top.child(j))
                    return

    def current_name(self):
        it = self.tree.currentItem() if hasattr(self, "tree") else None
        return it.data(0, Qt.UserRole) if it else None

    def _show(self, *_):
        name = self.current_name()
        if not name:
            return
        m = matlib.get(name, self.doc.custom_materials)
        self._set_editing(False)
        self.title.setText(f"{m.name}  ·  {'Custom' if m.custom else m.category}")
        for attr, *_ in FIELDS:
            self.spins[attr].setValue(float(getattr(m, attr)))
        self.ductile.setChecked(m.ductile)
        self.endurance.setChecked(m.endurance)
        self.note.setText(m.note)

    # ----------------------------------------------------------- custom
    def _set_editing(self, on):
        self._editing = on
        for sp in self.spins.values():
            sp.setReadOnly(not on)
            sp.setButtonSymbols(QDoubleSpinBox.UpDownArrows if on else QDoubleSpinBox.NoButtons)
        self.ductile.setEnabled(on)
        self.endurance.setEnabled(on)
        self.name_edit.setVisible(on)
        self.save_btn.setVisible(on)
        self.custom_btn.setVisible(not on)

    def _start_custom(self):
        base = self.current_name() or self.doc.material
        self._set_editing(True)
        self.name_edit.setText(f"{base} (custom)")
        self.name_edit.setFocus()
        self.name_edit.selectAll()
        self.title.setText(f"New material based on {base}")

    def _save_custom(self):
        props = {attr: self.spins[attr].value() for attr, *_ in FIELDS}
        props.update(name=self.name_edit.text().strip(), based_on=self.current_name(), category="Custom",
                     ductile=self.ductile.isChecked(), endurance=self.endurance.isChecked(), note="")
        try:
            m = self.doc.add_custom_material(props)
        except FeatureError as e:
            QMessageBox.warning(self, "Material", str(e))
            return
        self._fill()
        self._select(m.name)

    def apply(self):
        if self._editing:
            self._save_custom()
        name = self.current_name()
        if not name:
            return
        try:
            self.doc.set_material(name, obj=self.target.currentData())
            self.doc.rebuild()
        except FeatureError as e:
            QMessageBox.warning(self, "Material", str(e))
            return
        self.accept()
