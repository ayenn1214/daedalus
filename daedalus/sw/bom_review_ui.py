"""Review for DIMS: the open assembly's BOM (every level) against the inventory — tick the lines to add, fix part
numbers / types, preview, then add the profiles, BOMs and project allocation and write PartNo / DIMSId back."""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..inventory import model as M
from ..ui import theme
from . import bom_inventory as BI

STATUS_COL = {"new": theme.INFO, "matched": theme.OK, "linked": theme.OK, "changed": theme.WARN}


class BomReviewDialog(QDialog):
    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.rv = BI.review(link)
        self.summary = ""
        self.setWindowTitle(f"Review for DIMS — {self.rv['assembly']}")
        self.resize(1150, 620)
        v = QVBoxLayout(self)
        c = self.rv["counts"]
        v.addWidget(QLabel(f"<b>{self.rv['assembly_no'] or ''} {self.rv['assembly']}</b> — {len(self.rv['lines'])} "
                           f"lines: {c['new']} new · {c['changed']} changed · {c['matched'] + c['linked']} already "
                           f"in DIMS" + (f" · product line {self.rv['product_line']}" if self.rv['product_line']
                                         else " · <span style='color:%s'>no product line — give the top assembly a "
                                              "C30-style number or set lines under Prefixes</span>" % theme.WARN)))
        hdr = ["Add", "#", "Part No.", "Description", "Rev", "Type", "Status", "Qty / top", "Vendor", "Notes"]
        t = self.t = QTableWidget(len(self.rv["lines"]), len(hdr))
        t.setHorizontalHeaderLabels(hdr)
        t.verticalHeader().hide()
        self.checks, self.pns, self.types = {}, {}, {}
        for i, x in enumerate(self.rv["lines"]):
            cb = QCheckBox()
            cb.setChecked(x["status"] in ("new", "changed") or x["kind"] == "assembly")
            self.checks[x["item"]] = cb
            t.setCellWidget(i, 0, cb)
            t.setItem(i, 1, QTableWidgetItem(str(x["item"])))
            pn = QLineEdit(x["part_no"] or x["suggested_no"])
            pn.setPlaceholderText("P0x-#### (category?)" if x["type"] == "purchased" and not x["part_no"] else "")
            if x["suggested_no"] and not x["part_no"]:
                pn.setStyleSheet(f"color:{theme.ACCENT_TEXT}")
                pn.setToolTip("Suggested — the next free number; reserved when you add")
            pn.setEnabled(not x["part_no"])
            self.pns[x["item"]] = pn
            t.setCellWidget(i, 2, pn)
            t.setItem(i, 3, QTableWidgetItem(("    " if x["kind"] != "assembly" else "") + x["description"]))
            t.setItem(i, 4, QTableWidgetItem(x["revision"] or ""))
            ty = QComboBox()
            for k, lab in M.TYPES:
                ty.addItem(lab, k)
            ty.setCurrentIndex(max(0, ty.findData(x["type"])))
            ty.setEnabled(x["status"] == "new")
            self.types[x["item"]] = ty
            t.setCellWidget(i, 5, ty)
            st = QTableWidgetItem(x["status"] + (f" ({x['matched_by']})" if x["matched_by"] else ""))
            st.setForeground(QColor(STATUS_COL.get(x["status"], theme.MUTED)))
            t.setItem(i, 6, st)
            t.setItem(i, 7, QTableWidgetItem(f"{x['qty_per_top']:g}" if x["kind"] != "assembly" or x["qty_per_top"]
                                             else ""))
            t.setItem(i, 8, QTableWidgetItem(x["vendor"] or ""))
            t.setItem(i, 9, QTableWidgetItem("; ".join(x["changes"]) or (x["dims_name"] or "")))
        h = t.horizontalHeader()
        for col in range(len(hdr)):
            h.setSectionResizeMode(col, QHeaderView.Stretch if col in (3, 9) else QHeaderView.ResizeToContents)
        v.addWidget(t, 1)
        row = QHBoxLayout()
        self.project = QLineEdit()
        self.project.setPlaceholderText("Project to allocate to (optional)")
        self.units = QDoubleSpinBox()
        self.units.setRange(0, 100000)
        self.units.setDecimals(0)
        self.units.setSpecialValueText("units")
        self.wb = QCheckBox("Write PartNo / DIMSId back to SolidWorks")
        self.wb.setChecked(True)
        for w in (QLabel("Project"), self.project, self.units, self.wb):
            row.addWidget(w)
        row.addStretch(1)
        v.addLayout(row)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        bb = QDialogButtonBox()
        bb.addButton("Preview", QDialogButtonBox.ActionRole).clicked.connect(lambda: self.run(True))
        go = bb.addButton("Add to DIMS", QDialogButtonBox.AcceptRole)
        go.setObjectName("primary")
        go.clicked.connect(lambda: self.run(False))
        bb.addButton(QDialogButtonBox.Cancel).clicked.connect(self.reject)
        v.addWidget(bb)

    def _args(self):
        items = [i for i, cb in self.checks.items() if cb.isChecked()]
        over = {}
        for x in self.rv["lines"]:
            i = x["item"]
            o = {}
            txt = self.pns[i].text().strip().upper()
            if not x["part_no"] and txt != (x["suggested_no"] or ""):
                o["part_no"] = txt
            if self.types[i].currentData() != x["type"]:
                o["type"] = self.types[i].currentData()
            if o:
                over[i] = o
        return dict(items=items, overrides=over, project=self.project.text().strip() or None,
                    units=self.units.value() or None, write_back=self.wb.isChecked())

    def run(self, preview):
        try:
            rep = BI.apply(self.link, preview=preview, **self._args())
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Review for DIMS", str(e))
        txt = (f"{'Would create' if preview else 'Created'} {len(rep['created'])} profile(s)"
               + (f" ({', '.join(p['partNumber'] or p['name'] for p in rep['created'][:8])}"
                  + ("…" if len(rep['created']) > 8 else "") + ")" if rep["created"] else "")
               + f", link {rep['linked']}, update {len(rep['updated'])}, build {rep['boms']} BOM(s)"
               + (f", allocate {rep['allocated']} part(s) to {rep['project']}" if rep["project"] else "")
               + (f"; PartNo / DIMSId written to {rep.get('written_back', 0)} file(s)" if not preview else ""))
        if rep.get("write_failed"):
            txt += f" — {len(rep['write_failed'])} write-back(s) failed"
        self.summary = txt
        self.note.setText(txt)
        if not preview:
            self.accept()
