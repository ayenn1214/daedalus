"""Drawings ▸ Sheet ▾: sheet format editor (margins, zones, title block layout, templates), dimension standard,
numbered notes and drawing numbering — the working-drawing conventions in drawing/practice.py."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..drawing import practice as P
from ..drawing.model import SHEETS
from ..errors import FeatureError


def _note(text):
    l = QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(True)
    return l


def _dsb(v, lo=0.0, hi=2000.0, step=1.0, suffix=" mm", dec=1):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(dec)
    s.setValue(float(v))
    s.setSuffix(suffix)
    return s


# ------------------------------------------------------------------ sheet format
class SheetFormatDialog(QDialog):
    """Pick a sheet format or build your own: size, margins (filing margin), zones, frame, title block rows and
    cells, labels; save it as a template for every drawing."""

    def __init__(self, doc, drawing, sheet, parent=None):
        super().__init__(parent)
        self.doc, self.dr, self.sh = doc, drawing, sheet
        self.setWindowTitle("Sheet format")
        self.resize(760, 640)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Format"))
        self.name = QComboBox()
        self._fill_names()
        self.name.activated.connect(lambda _i: self._load(P.list_formats()[self.name.currentText()]))
        top.addWidget(self.name, 1)
        save = QPushButton("Save as template…")
        save.clicked.connect(self.save_template)
        top.addWidget(save)
        dele = QPushButton("Delete template")
        dele.clicked.connect(self.delete_template)
        top.addWidget(dele)
        v.addLayout(top)
        tabs = QTabWidget()
        # ---- sheet
        w1 = QWidget()
        f1 = QFormLayout(w1)
        self.size = QComboBox()
        self.size.addItems([s for s in SHEETS if s != "Model space"])
        self.size.setCurrentText(sheet.size)
        self.cw, self.ch = _dsb(sheet.custom_size[0], 50, 5000), _dsb(sheet.custom_size[1], 50, 5000)
        cs = QWidget()
        ch_ = QHBoxLayout(cs)
        ch_.setContentsMargins(0, 0, 0, 0)
        ch_.addWidget(self.cw)
        ch_.addWidget(QLabel("×"))
        ch_.addWidget(self.ch)
        f1.addRow("Sheet size", self.size)
        f1.addRow("Custom size (W × H)", cs)
        self.m = [_dsb(10) for _ in range(4)]
        mw = QWidget()
        mh = QHBoxLayout(mw)
        mh.setContentsMargins(0, 0, 0, 0)
        for lab, sp in zip(("Left", "Bottom", "Right", "Top"), self.m):
            mh.addWidget(QLabel(lab))
            mh.addWidget(sp)
        f1.addRow("Margins", mw)
        f1.addRow("", _note("ISO 5457: 20 mm on the left (filing margin), 10 mm elsewhere. ASME Y14.1: ½ in all round."))
        self.zones = QCheckBox("Zones (A B C… / 1 2 3… round the frame)")
        self.zone_size = _dsb(100, 20, 500, 5)
        self.frame_w = _dsb(0.7, 0.1, 2.0, 0.05, " mm", 2)
        self.symbol = QCheckBox("Projection symbol in the title block")
        f1.addRow("", self.zones)
        f1.addRow("Zone size", self.zone_size)
        f1.addRow("Frame line weight", self.frame_w)
        f1.addRow("", self.symbol)
        tabs.addTab(w1, "Sheet && frame")
        # ---- title block
        w2 = QWidget()
        v2 = QVBoxLayout(w2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Title block width"))
        self.tb_w = _dsb(180, 60, 600, 5)
        row.addWidget(self.tb_w)
        row.addStretch(1)
        v2.addLayout(row)
        v2.addWidget(_note("Rows from the bottom up. Cells: field:width, … — width is relative (title:3, company:2 "
                           "makes the title 60 % of the row). Fields: " + ", ".join(k for k in P.FIELDS)))
        self.rows = QTableWidget(0, 2)
        self.rows.setHorizontalHeaderLabels(["Height (mm)", "Cells"])
        self.rows.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v2.addWidget(self.rows, 1)
        rb = QHBoxLayout()
        for txt, fn in (("Add row", self.add_row), ("Remove row", self.remove_row), ("Move up", lambda: self.move(1)),
                        ("Move down", lambda: self.move(-1))):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            rb.addWidget(b)
        rb.addStretch(1)
        v2.addLayout(rb)
        v2.addWidget(QLabel("Labels — field=printed label, separated by ; (blank = the default)"))
        self.labels = QLineEdit()
        self.labels.setPlaceholderText("number=PART NO.; drawn=DRAWN BY")
        v2.addWidget(self.labels)
        tabs.addTab(w2, "Title block")
        v.addWidget(tabs, 1)
        self.all_sheets = QCheckBox("Apply to every sheet of this drawing")
        v.addWidget(self.all_sheets)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._load(P.sheet_format(drawing, sheet))
        cur = (sheet.format or {}).get("name")
        if cur:
            self.name.setCurrentText(cur)
        elif (sheet.format or {}).get("custom"):
            self.name.setCurrentIndex(-1)
            self.name.setEditText("(custom)")

    def _fill_names(self):
        self.name.clear()
        self.name.addItems(list(P.list_formats()))

    def _load(self, f):
        for sp, val in zip(self.m, f.get("margins", [10, 10, 10, 10])):
            sp.setValue(float(val))
        self.zones.setChecked(bool(f.get("zones", True)))
        self.zone_size.setValue(float(f.get("zone_size", 100)))
        self.frame_w.setValue(float(f.get("frame_weight", 0.7)))
        self.symbol.setChecked(bool(f.get("projection_symbol", True)))
        tb = f.get("title_block") or {}
        self.tb_w.setValue(float(tb.get("width", 180)))
        self.rows.setRowCount(0)
        for r in reversed(tb.get("rows", [])):                # top row first in the table
            self._add(r["h"], ", ".join(f"{c[0]}:{c[1]:g}" for c in r["cells"]))
        self.labels.setText("; ".join(f"{k}={v}" for k, v in (f.get("labels") or {}).items()))

    def _add(self, h, cells):
        i = self.rows.rowCount()
        self.rows.insertRow(i)
        self.rows.setItem(i, 0, QTableWidgetItem(f"{float(h):g}"))
        self.rows.setItem(i, 1, QTableWidgetItem(cells))

    def add_row(self):
        self._add(8, "blank:1")

    def remove_row(self):
        r = self.rows.currentRow()
        if r >= 0:
            self.rows.removeRow(r)

    def move(self, d):
        r = self.rows.currentRow()
        t = r - d
        if r < 0 or not 0 <= t < self.rows.rowCount():
            return
        a = [self.rows.item(r, c).text() for c in range(2)]
        b = [self.rows.item(t, c).text() for c in range(2)]
        for c in range(2):
            self.rows.item(r, c).setText(b[c])
            self.rows.item(t, c).setText(a[c])
        self.rows.setCurrentCell(t, 1)

    def value(self) -> dict:
        rows = []
        for i in reversed(range(self.rows.rowCount())):     # back to bottom → top
            h = float(self.rows.item(i, 0).text() or 8)
            cells = []
            for part in self.rows.item(i, 1).text().split(","):
                if not part.strip():
                    continue
                k, _, w = part.strip().partition(":")
                cells.append([k.strip(), float(w or 1)])
            rows.append({"h": h, "cells": cells})
        labels = {}
        for part in self.labels.text().split(";"):
            if "=" in part:
                k, val = part.split("=", 1)
                labels[k.strip()] = val.strip()
        f = {"margins": [sp.value() for sp in self.m], "zones": self.zones.isChecked(),
             "zone_size": self.zone_size.value(), "frame_weight": self.frame_w.value(),
             "projection_symbol": self.symbol.isChecked(), "title_block": {"width": self.tb_w.value(), "rows": rows}}
        if labels:
            f["labels"] = labels
        P.check_format(f)
        return f

    def save_template(self):
        try:
            f = self.value()
        except ValueError as e:
            return QMessageBox.warning(self, "Sheet format", str(e))
        name, ok = QInputDialog.getText(self, "Save sheet format", "Template name:", text=self.name.currentText()
                                        if self.name.currentText() not in P.FORMATS else "My format")
        if ok and name.strip():
            P.save_format(name.strip(), f)
            self._fill_names()
            self.name.setCurrentText(name.strip())

    def delete_template(self):
        n = self.name.currentText()
        if n in P.FORMATS:
            return QMessageBox.information(self, "Sheet format", "Built-in formats can't be deleted.")
        if P.delete_format(n):
            self._fill_names()

    def accept(self):
        try:
            f = self.value()
        except ValueError as e:
            return QMessageBox.warning(self, "Sheet format", str(e))
        name = self.name.currentText()
        same = name in P.list_formats() and _same(P.list_formats()[name], f)
        size = self.size.currentText()
        try:
            self.doc.set_sheet_format(self.dr.name, self.sh.name, name=name if same else None,
                                      custom=None if same else f, size=size if size != "Custom" else None,
                                      custom_size=[self.cw.value(), self.ch.value()] if size == "Custom" else None,
                                      all_sheets=self.all_sheets.isChecked())
        except FeatureError as e:
            return QMessageBox.warning(self, "Sheet format", str(e))
        super().accept()


def _same(a, b):
    keys = ("margins", "zones", "zone_size", "frame_weight", "projection_symbol", "title_block", "labels")
    norm = lambda f: {k: copy.deepcopy(f.get(k)) for k in keys if f.get(k) not in (None, {}, [])}  # noqa: E731
    na, nb = norm(a), norm(b)
    for f in (na, nb):
        for r in (f.get("title_block") or {}).get("rows", []):
            r["h"] = float(r["h"])
            r["cells"] = [[c[0], float(c[1])] for c in r["cells"]]
        if "margins" in f:
            f["margins"] = [float(x) for x in f["margins"]]
        if "title_block" in f:
            f["title_block"]["width"] = float(f["title_block"].get("width", 180))
        for k in ("zone_size", "frame_weight"):
            if k in f:
                f[k] = float(f[k])
    return na == nb


# ------------------------------------------------------------------ notes
class NotesDialog(QDialog):
    def __init__(self, doc, drawing, sheet, parent=None):
        super().__init__(parent)
        self.doc, self.dr, self.sh = doc, drawing, sheet
        self.setWindowTitle("Notes")
        self.resize(640, 460)
        v = QVBoxLayout(self)
        v.addWidget(_note("Numbered notes, lower left of the sheet: dimensions, heat treatment, welding, plating, "
                          "surface preparation, inspection… Pick a template and replace the <…> parts."))
        self.list = QListWidget()
        self.list.addItems(sheet.notes)
        for i in range(self.list.count()):
            self.list.item(i).setFlags(self.list.item(i).flags() | Qt.ItemIsEditable)
        self.list.setDragDropMode(QListWidget.InternalMove)
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.tpl = QComboBox()
        self.tpl.addItems(["Template…", *P.NOTE_TEMPLATES])
        self.tpl.activated.connect(self._template)
        row.addWidget(self.tpl)
        add = QPushButton("Add note")
        add.clicked.connect(lambda: self._add(""))
        row.addWidget(add)
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: self.list.takeItem(self.list.currentRow()))
        row.addWidget(rem)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _add(self, text):
        self.list.addItem(text or "NEW NOTE.")
        it = self.list.item(self.list.count() - 1)
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        self.list.setCurrentItem(it)
        self.list.editItem(it)

    def _template(self, i):
        if i > 0:
            self._add(P.NOTE_TEMPLATES[self.tpl.itemText(i)])
        self.tpl.setCurrentIndex(0)

    def notes(self):
        return [self.list.item(i).text() for i in range(self.list.count())]

    def accept(self):
        self.doc.set_drawing_notes(self.notes(), self.dr.name, self.sh.name)
        super().accept()


# ------------------------------------------------------------------ numbering
class NumberingDialog(QDialog):
    """Drawing numbers for the model: your format, previewed as the drawing tree."""

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("Drawing numbers")
        self.resize(940, 560)
        v = QVBoxLayout(self)
        v.addWidget(_note("Every drawing gets a number from the assembly tree: main assembly XXXXX, subassemblies "
                          "XXXXX.AA, their parts XXXXX.AA.BB … Standard parts (bolts, nuts, bearings, keys) need no "
                          "detail drawing unless you include them."))
        h = QHBoxLayout()
        form = QFormLayout()
        o = dict(P.NUMBERING, **(getattr(doc, "numbering", None) or {}))
        self.w = {}
        self.w["base"] = QLineEdit(str(o["base"]))
        self.w["prefix"] = QLineEdit(str(o["prefix"]))
        self.w["suffix"] = QLineEdit(str(o["suffix"]))
        self.w["separator"] = QComboBox()
        self.w["separator"].setEditable(True)
        self.w["separator"].addItems([".", "-", "_", "/", " ", ""])
        self.w["separator"].setCurrentText(str(o["separator"]))
        self.w["digits"] = QLineEdit(str(o["digits"]) if not isinstance(o["digits"], list) else
                                     ",".join(map(str, o["digits"])))
        self.w["start"] = QSpinBox()
        self.w["start"].setRange(0, 9999)
        self.w["start"].setValue(int(o["start"]))
        self.w["step"] = QSpinBox()
        self.w["step"].setRange(1, 1000)
        self.w["step"].setValue(int(o["step"]))
        self.w["levels"] = QSpinBox()
        self.w["levels"].setRange(0, 9)
        self.w["levels"].setSpecialValueText("as deep as the model")
        self.w["levels"].setValue(int(o["levels"]))
        self.w["order"] = QComboBox()
        self.w["order"].addItems(["model", "name"])
        self.w["order"].setCurrentText(str(o["order"]))
        for k in ("fill", "subassemblies_first", "standard_parts"):
            c = QCheckBox(P.NUMBERING_HELP[k])
            c.setChecked(bool(o[k]))
            self.w[k] = c
        for k in ("base", "prefix", "suffix", "separator", "digits", "start", "step", "levels", "order"):
            self.w[k].setToolTip(P.NUMBERING_HELP[k])
            form.addRow(k.replace("_", " ").capitalize(), self.w[k])
        for k in ("fill", "subassemblies_first", "standard_parts"):
            form.addRow("", self.w[k])
        h.addLayout(form, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Drawing", "Number"])
        self.tree.setMinimumWidth(380)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.addWidget(self.tree, 1)
        v.addLayout(h, 1)
        self.apply = QCheckBox("Write the numbers into the drawings' title blocks")
        self.apply.setChecked(True)
        v.addWidget(self.apply)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        for k, w in self.w.items():
            sig = getattr(w, "textChanged", None) or getattr(w, "valueChanged", None) or getattr(w, "toggled", None)
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(self.preview)
            elif isinstance(w, QCheckBox):
                w.toggled.connect(self.preview)
            elif sig is not None:
                sig.connect(self.preview)
        self.preview()

    def values(self) -> dict:
        o = {}
        for k, w in self.w.items():
            if isinstance(w, QCheckBox):
                o[k] = w.isChecked()
            elif isinstance(w, QSpinBox):
                o[k] = w.value()
            elif isinstance(w, QComboBox):
                o[k] = w.currentText()
            else:
                o[k] = w.text()
        d = o["digits"]
        try:
            o["digits"] = int(d) if "," not in d else [int(x) for x in d.split(",") if x.strip()]
        except ValueError:
            o["digits"] = 2
        return o

    def preview(self, *_):
        self.tree.clear()
        try:
            rows = P.drawing_numbers(self.doc, self.values())
        except Exception as e:  # noqa: BLE001
            self.tree.addTopLevelItem(QTreeWidgetItem([f"({e})", ""]))
            return
        stack = []
        for r in rows:
            it = QTreeWidgetItem([("◆ " if r["kind"] == "assembly" else "▣ " if r["kind"] == "subassembly" else
                                   "· ") + r["name"], r["number"] or r.get("note", "")])
            while stack and stack[-1][0] >= r["level"]:
                stack.pop()
            if stack:
                stack[-1][1].addChild(it)
            else:
                self.tree.addTopLevelItem(it)
            stack.append((r["level"], it))
        self.tree.expandAll()

    def accept(self):
        try:
            self.doc.set_numbering(**self.values())
            if self.apply.isChecked():
                self.doc.apply_drawing_numbers()
        except FeatureError as e:
            return QMessageBox.warning(self, "Drawing numbers", str(e))
        super().accept()


def choose_logo(parent):
    p, _ = QFileDialog.getOpenFileName(parent, "Company logo", "", "Images (*.png *.jpg *.jpeg *.bmp *.svg)")
    return p
