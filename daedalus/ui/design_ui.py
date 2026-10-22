"""Design Data: machine-design calculators (forms generated from daedalus.design.calcs) and the local reference
library (search your own handbooks)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..design import calcs as C
from ..design import reference as REF


class CalcPanel(QWidget):
    def __init__(self, doc=None, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.cur = None
        self.w = {}
        h = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        h.addWidget(split)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        for chap, items in C.chapters().items():
            top = QTreeWidgetItem([chap])
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            for c in items:
                it = QTreeWidgetItem([c.title])
                it.setData(0, Qt.UserRole, c.id)
                top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.currentItemChanged.connect(lambda it, _p: it and it.data(0, Qt.UserRole) and
                                             self.show_calc(it.data(0, Qt.UserRole)))
        split.addWidget(self.tree)
        right = QWidget()
        rv = QVBoxLayout(right)
        self.title = QLabel()
        self.title.setStyleSheet("font-size:15px; font-weight:600;")
        rv.addWidget(self.title)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setObjectName("muted")
        rv.addWidget(self.help)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        rv.addWidget(self.form_host)
        row = QHBoxLayout()
        self.mat_btn = QPushButton("Use the part's material")
        self.mat_btn.setToolTip("Fill E, ν, Sy, Su, density from the active object's material")
        self.mat_btn.clicked.connect(self.from_material)
        row.addWidget(self.mat_btn)
        b = QPushButton("Calculate")
        b.setDefault(True)
        b.clicked.connect(self.calculate)
        row.addWidget(b)
        cp = QPushButton("Copy report")
        cp.clicked.connect(self.copy)
        row.addWidget(cp)
        row.addStretch(1)
        rv.addLayout(row)
        self.out = QTableWidget(0, 3)
        self.out.setHorizontalHeaderLabels(["Result", "Value", "Note"])
        self.out.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.out.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.out.verticalHeader().setVisible(False)
        rv.addWidget(self.out, 1)
        split.addWidget(right)
        split.setSizes([260, 600])
        first = self.tree.topLevelItem(0).child(0)
        self.tree.setCurrentItem(first)

    def show_calc(self, cid):
        c = C.REGISTRY[cid]
        self.cur = c
        self.title.setText(c.title)
        self.help.setText(c.help)
        while self.form.rowCount():
            self.form.removeRow(0)
        self.w = {}
        for i in c.inputs:
            if i.choices:
                w = QComboBox()
                w.addItems([str(x) for x in i.choices])
                w.setCurrentText(str(i.default))
            elif isinstance(i.default, bool):
                w = QCheckBox()
                w.setChecked(i.default)
            elif isinstance(i.default, (int, float)):
                w = QDoubleSpinBox()
                w.setDecimals(4)
                w.setRange(i.minimum if i.minimum is not None else -1e9, 1e9)
                w.setValue(float(i.default))
                if i.unit:
                    w.setSuffix(f" {i.unit}")
            else:
                w = QLineEdit(str(i.default))
            if i.help:
                w.setToolTip(i.help)
            self.form.addRow(i.label, w)
            self.w[i.name] = w
        self.mat_btn.setEnabled(bool(c.material_inputs) and self.doc is not None)
        self.out.setRowCount(0)
        self.calculate()

    def values(self):
        v = {}
        for i in self.cur.inputs:
            w = self.w[i.name]
            if isinstance(w, QComboBox):
                v[i.name] = w.currentText()
            elif isinstance(w, QCheckBox):
                v[i.name] = w.isChecked()
            elif isinstance(w, QDoubleSpinBox):
                v[i.name] = w.value()
            else:
                v[i.name] = w.text()
        return v

    def from_material(self):
        if self.doc is None or self.cur is None:
            return
        m = self.doc.material_of(self.doc.active_object)
        for k, val in C.prefill_from_material(self.cur.id, m).items():
            w = self.w.get(k)
            if isinstance(w, QDoubleSpinBox):
                w.setValue(val)
        self.help.setText(f"{self.cur.help}\nMaterial: {m.name}")
        self.calculate()

    def calculate(self):
        if self.cur is None:
            return
        try:
            self.vals = self.values()
            self.res = C.run(self.cur.id, self.vals)
        except C.CalcError as e:
            self.out.setRowCount(1)
            self.out.setItem(0, 0, QTableWidgetItem("Error"))
            self.out.setItem(0, 1, QTableWidgetItem(str(e)))
            self.res = None
            return
        self.out.setRowCount(len(self.res))
        for r, (k, x) in enumerate(self.res.items()):
            self.out.setItem(r, 0, QTableWidgetItem(k))
            self.out.setItem(r, 1, QTableWidgetItem(f"{C.fmt_value(x['value'])} {x['unit']}".strip()))
            it = QTableWidgetItem(x["note"])
            if any(s in x["note"] for s in ("too", "exceed", "over", "weak", "limited", "stiffen")):
                it.setForeground(Qt.red)
            self.out.setItem(r, 2, it)

    def copy(self):
        if self.cur is not None and getattr(self, "res", None):
            QGuiApplication.clipboard().setText(C.report(self.cur.id, self.vals, self.res))


class ReferencePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search your handbooks — e.g. 'factor of safety shock', 'band brake', 'Lewis form "
                                  "factor'")
        self.q.returnPressed.connect(self.search)
        top.addWidget(self.q, 1)
        self.doc_box = QComboBox()
        top.addWidget(self.doc_box)
        for text, fn in (("Search", self.search), ("Add PDF / text…", self.add), ("Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        v.addLayout(top)
        split = QSplitter(Qt.Horizontal)
        self.hits = QListWidget()
        self.hits.currentItemChanged.connect(self.show_hit)
        split.addWidget(self.hits)
        self.page = QTextEdit()
        self.page.setReadOnly(True)
        split.addWidget(self.page)
        split.setSizes([330, 520])
        v.addWidget(split, 1)
        self.note = QLabel("Documents are stored on this computer (~/.daedalus/reference) and searchable by Icarus. "
                           "Scanned PDFs need Tesseract OCR installed.")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        self.refresh()

    def refresh(self):
        self.doc_box.clear()
        self.doc_box.addItem("All documents", None)
        for d in REF.documents():
            self.doc_box.addItem(f"{d['name']} ({d['pages']} p)", d["name"])

    def search(self):
        self.hits.clear()
        for h in REF.search(self.q.text(), self.doc_box.currentData(), limit=25):
            it = QListWidgetItem(f"{h['doc']} · p{h['page']}\n{h['snippet'][:140]}")
            it.setData(Qt.UserRole, (h["doc"], h["page"]))
            self.hits.addItem(it)
        if not self.hits.count():
            self.page.setPlainText("No matches." if REF.documents() else
                                   "No documents yet — Add PDF / text… (e.g. your design data handbook).")

    def show_hit(self, it, _prev=None):
        if it is None:
            return
        doc, page = it.data(Qt.UserRole)
        txt = REF.page_text(doc, page)
        self.page.setPlainText(f"{doc} — page {page}\n\n{txt}")
        for w in [w for w in self.q.text().split() if len(w) > 2][:3]:
            self.page.find(w)

    def add(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add reference", "", "Handbooks (*.pdf *.txt)")
        if not path:
            return
        dlg = QProgressDialog("Reading / OCR…", "", 0, 0, self)
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()

        def prog(i, n):
            dlg.setMaximum(n)
            dlg.setValue(i)
            dlg.setLabelText(f"OCR page {i} / {n}")
            QGuiApplication.processEvents()
        try:
            r = REF.add_document(path, progress=prog)
            QMessageBox.information(self, "Reference", f"Added {r['name']}: {r['pages']} pages" +
                                    (" (OCR)" if r["ocr"] else ""))
        except REF.ReferenceError_ as e:
            QMessageBox.warning(self, "Reference", str(e))
        finally:
            dlg.close()
        self.refresh()

    def remove(self):
        name = self.doc_box.currentData()
        if name and QMessageBox.question(self, "Reference", f"Remove {name} from the library?") == QMessageBox.Yes:
            REF.remove_document(name)
            self.refresh()


class DesignDataDialog(QDialog):
    def __init__(self, doc=None, parent=None, tab=0):
        super().__init__(parent)
        self.setWindowTitle("Design Data")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        t = QTabWidget()
        self.calc = CalcPanel(doc)
        self.ref = ReferencePanel()
        t.addTab(self.calc, "Calculators")
        t.addTab(self.ref, "Reference library")
        t.setCurrentIndex(tab)
        v.addWidget(t)
