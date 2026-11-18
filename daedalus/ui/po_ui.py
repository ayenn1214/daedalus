"""Create a purchase order (PDF) from the buy list, and the PO settings: your company's name, address, contact,
logo, ship-to, footer notes and PO numbering — kept for the team in the inventory."""
from __future__ import annotations

import base64
import os

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import model as M
from ..inventory import purchase_order as P


def _text(t, h=64):
    e = QPlainTextEdit(t or "")
    e.setFixedHeight(h)
    return e


class POSettingsDialog(QDialog):
    """Your company on the PO. Fill from a PO PDF… reads them from a PO you already have."""

    def __init__(self, inv, parent=None):
        super().__init__(parent)
        self.inv = inv
        self.setWindowTitle("Purchase order settings")
        self.resize(620, 720)
        st = P.settings(inv)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        n = QLabel("Your company as it prints on every PO. Saved for the whole team.")
        n.setObjectName("muted")
        n.setWordWrap(True)
        top.addWidget(n, 1)
        imp = QPushButton("Fill from a PO PDF…")
        imp.setToolTip("Read your company block, logo, ship-to and footer from a PO your accounting system printed")
        imp.clicked.connect(self.import_pdf)
        top.addWidget(imp)
        v.addLayout(top)
        f = QFormLayout()
        self.name = QLineEdit(st["companyName"])
        self.address = _text(st["address"], 56)
        self.phone = QLineEdit(st["phone"])
        self.email = QLineEdit(st["email"])
        self.web = QLineEdit(st["website"])
        self.logo = st.get("logo") or ""
        lr = QHBoxLayout()
        self.logo_view = QLabel()
        self.logo_view.setFixedHeight(48)
        self.logo_view.setMinimumWidth(160)
        lr.addWidget(self.logo_view, 1)
        for lab, fn in (("Choose…", self.choose_logo), ("Remove", lambda: self.set_logo(""))):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            lr.addWidget(b)
        lw = QWidget()
        lw.setLayout(lr)
        self.ship = _text(st["shipTo"], 70)
        self.ship.setPlaceholderText("Blank = your company name and address")
        self.footer = _text(st["footer"], 64)
        self.footer.setPlaceholderText("e.g. tax / exemption number, terms, freight note (up to 3 lines)")
        self.prefix = QLineEdit(st["poPrefix"])
        self.next = QSpinBox()
        self.next.setRange(0, 99_999_999)
        self.next.setValue(int(M.num(st["nextPo"])))
        nr = QHBoxLayout()
        nr.addWidget(QLabel("Prefix"))
        nr.addWidget(self.prefix)
        nr.addWidget(QLabel("Next number"))
        nr.addWidget(self.next)
        nw = QWidget()
        nw.setLayout(nr)
        self.cur = QComboBox()
        self.cur.addItems(["CAD", "USD", "EUR", "GBP", "AUD"])
        self.cur.setEditable(True)
        self.cur.setCurrentText(st["currency"])
        self.tag = QLineEdit(st["fileTag"])
        self.tag.setPlaceholderText("Short company name for file names (blank = first word of the name)")
        self.potag = QLineEdit(st["poTag"])
        for lab, w in (("Company name", self.name), ("Address", self.address), ("Phone", self.phone),
                       ("Email", self.email), ("Website", self.web), ("Logo", lw), ("Ship to", self.ship),
                       ("Footer notes", self.footer), ("PO numbers", nw), ("Currency", self.cur),
                       ("File name: company", self.tag), ("File name: PO tag", self.potag)):
            f.addRow(lab, w)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.set_logo(self.logo)

    def set_logo(self, b64):
        self.logo = b64 or ""
        if not self.logo:
            self.logo_view.setText("No logo")
            self.logo_view.setPixmap(QPixmap())
            return
        pm = QPixmap()
        pm.loadFromData(QByteArray(base64.b64decode(self.logo)))
        self.logo_view.setPixmap(pm.scaledToHeight(44, Qt.SmoothTransformation))

    def choose_logo(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Logo", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if fn:
            self.load_logo_file(fn)

    def load_logo_file(self, fn):
        from io import BytesIO

        from PIL import Image
        im = Image.open(fn)
        im.thumbnail((900, 300))
        buf = BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.convert("RGBA").save(buf, "PNG")
        else:
            im.convert("RGB").save(buf, "JPEG", quality=92)
        self.set_logo(base64.b64encode(buf.getvalue()).decode())

    def import_pdf(self):
        fn, _ = QFileDialog.getOpenFileName(self, "A PO you already have", "", "PDF (*.pdf)")
        if fn:
            self.fill(P.read_template(fn))

    def fill(self, t: dict):
        for w, k in ((self.name, "companyName"), (self.phone, "phone"), (self.email, "email"), (self.web, "website"),
                     (self.tag, "fileTag"), (self.potag, "poTag")):
            if t.get(k):
                w.setText(t[k])
        for w, k in ((self.address, "address"), (self.ship, "shipTo"), (self.footer, "footer")):
            if t.get(k):
                w.setPlainText(t[k])
        if t.get("logo"):
            self.set_logo(t["logo"])

    def value(self) -> dict:
        return {"companyName": self.name.text().strip(), "address": self.address.toPlainText().strip(),
                "phone": self.phone.text().strip(), "email": self.email.text().strip(),
                "website": self.web.text().strip(), "logo": self.logo, "shipTo": self.ship.toPlainText().strip(),
                "footer": self.footer.toPlainText().strip(), "poPrefix": self.prefix.text(),
                "nextPo": self.next.value(), "currency": self.cur.currentText().strip() or "CAD",
                "fileTag": self.tag.text().strip(), "poTag": self.potag.text().strip() or "PO"}


class PurchaseOrderDialog(QDialog):
    """One vendor's PO from buy-list lines: number, date, vendor and ship-to boxes, lines (MPN, description, qty,
    rate) → PDF, and optionally logged as open part orders with the PO number."""

    COLS = ["MPN", "Description", "Qty", "Rate", ""]

    def __init__(self, page, items_by_vendor: dict, vendor=None, job="", project_id=None, parent=None):
        super().__init__(parent or page)
        self.page, self.inv = page, page.inv
        self.items = items_by_vendor                    # {vendor: [(part, qty)]}
        self.project_id = project_id
        self.saved_path = None
        self.setWindowTitle("Create purchase order")
        self.resize(900, 720)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.who = QLabel()
        self.who.setObjectName("muted")
        top.addWidget(self.who, 1)
        sb = QPushButton("PO settings…")
        sb.setToolTip("Your company name, address, logo, ship-to, footer and PO numbering")
        sb.clicked.connect(self.edit_settings)
        top.addWidget(sb)
        v.addLayout(top)
        f = QFormLayout()
        self.vendor = QComboBox()
        for name in sorted(items_by_vendor, key=lambda x: (x == "No vendor set", x.lower())):
            self.vendor.addItem(f"{name}  ({len(items_by_vendor[name])} line{'s' if len(items_by_vendor[name]) != 1 else ''})",
                                name)
        if vendor:
            i = self.vendor.findData(vendor)
            if i >= 0:
                self.vendor.setCurrentIndex(i)
        self.number = QLineEdit()
        from .date_field import DateField
        self.date = DateField(M.today())
        self.job = QLineEdit(job)
        self.job.setPlaceholderText("Job / project — goes in the file name")
        row = QHBoxLayout()
        row.addWidget(self.number, 1)
        row.addWidget(QLabel("Date"))
        row.addWidget(self.date)
        rw = QWidget()
        rw.setLayout(row)
        self.vblock = _text("", 92)
        self.save_addr = QCheckBox("Save to the vendor")
        self.save_addr.setToolTip("Keep this vendor box text as the vendor's address for next time")
        self.ship = _text("", 76)
        boxes = QHBoxLayout()
        vb = QVBoxLayout()
        vb.addWidget(QLabel("<b>Vendor</b>"))
        vb.addWidget(self.vblock)
        vb.addWidget(self.save_addr)
        sb2 = QVBoxLayout()
        sb2.addWidget(QLabel("<b>Ship to</b>"))
        sb2.addWidget(self.ship)
        sb2.addStretch(1)
        boxes.addLayout(vb, 1)
        boxes.addLayout(sb2, 1)
        f.addRow("Vendor", self.vendor)
        f.addRow("P.O. No.", rw)
        f.addRow("Job", self.job)
        v.addLayout(f)
        v.addLayout(boxes)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)
        tr = QHBoxLayout()
        add = QPushButton("+ Line")
        add.setToolTip("A free line (service, drilling, freight …)")
        add.clicked.connect(lambda: self._row({"partId": None, "mpn": "", "description": "", "qty": 1, "rate": 0}))
        tr.addWidget(add)
        tr.addStretch(1)
        self.total = QLabel()
        f_ = self.total.font()
        f_.setBold(True)
        self.total.setFont(f_)
        tr.addWidget(self.total)
        v.addLayout(tr)
        self.log = QCheckBox("Mark as ordered — add open part orders with this PO number")
        self.log.setChecked(True)
        v.addWidget(self.log)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.save_btn = bb.addButton("Save PDF…", QDialogButtonBox.AcceptRole)
        self.save_btn.setObjectName("primary")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.vendor.currentIndexChanged.connect(lambda _i: self.load_vendor())
        self.load_settings()
        self.load_vendor()

    # ---------------------------------------------------------------- filling
    def load_settings(self):
        st = P.settings(self.inv)
        self.st = st
        self.number.setText(P.next_number(st))
        self.ship.setPlainText(P.ship_to(st))
        who = st.get("companyName") or "Your company isn't set yet"
        self.who.setText(f"From <b>{who}</b>" + ("" if st.get("companyName") else
                                                  " — PO settings… adds your name, address and logo"))

    def edit_settings(self):
        d = POSettingsDialog(self.inv, self)
        if d.exec() == QDialog.Accepted:
            P.save_settings(self.inv, d.value())
            self.page.quiet_save()
            num = self.number.text()
            self.load_settings()
            if num and num != P.next_number(self.st):
                self.number.setText(num)

    def vendor_record(self):
        name = self.vendor.currentData() or ""
        return next((v for v in self.inv.vendors if (v.get("name") or "").lower() == name.lower()), None)

    def load_vendor(self):
        name = self.vendor.currentData() or ""
        rec = self.vendor_record()
        self.vblock.setPlainText(P.vendor_block(rec, "" if name == "No vendor set" else name))
        self.save_addr.setVisible(rec is not None)
        self.save_addr.setChecked(False)
        self.table.setRowCount(0)
        for ln in P.lines_from_parts(self.inv, self.items.get(name, []), name):
            self._row(ln)
        self._total()

    def _row(self, ln):
        r = self.table.rowCount()
        self.table.insertRow(r)
        mpn = QTableWidgetItem(ln.get("mpn") or "")
        mpn.setData(Qt.UserRole, ln.get("partId"))
        self.table.setItem(r, 0, mpn)
        d = QPlainTextEdit(ln.get("description") or "")
        d.setFixedHeight(44)
        self.table.setCellWidget(r, 1, d)
        q = QDoubleSpinBox()
        q.setRange(0, 1e7)
        q.setDecimals(0 if float(M.num(ln["qty"])).is_integer() else 2)
        q.setValue(M.num(ln["qty"]))
        q.valueChanged.connect(lambda _v: self._total())
        self.table.setCellWidget(r, 2, q)
        rate = QDoubleSpinBox()
        rate.setRange(0, 1e8)
        rate.setDecimals(2)
        rate.setValue(M.num(ln.get("rate")))
        rate.valueChanged.connect(lambda _v: self._total())
        self.table.setCellWidget(r, 3, rate)
        x = QPushButton("✕")
        x.setToolTip("Remove this line")
        x.setFixedWidth(28)
        x.clicked.connect(lambda _=False, w=x: self._remove(w))
        self.table.setCellWidget(r, 4, x)
        self.table.setRowHeight(r, 48)
        self._total()

    def _remove(self, btn):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 4) is btn:
                self.table.removeRow(r)
                break
        self._total()

    def lines(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            out.append({"partId": self.table.item(r, 0).data(Qt.UserRole), "mpn": self.table.item(r, 0).text(),
                        "description": self.table.cellWidget(r, 1).toPlainText().strip(),
                        "qty": self.table.cellWidget(r, 2).value(), "rate": self.table.cellWidget(r, 3).value()})
        return out

    def _total(self):
        self.total.setText(f"Total  {self.st.get('currency') or 'CAD'} {P.total(self.lines()):,.2f}")

    def po(self) -> dict:
        return {"number": self.number.text().strip(), "date": self.date.value() or M.today(),
                "vendor": self.vblock.toPlainText().strip(), "shipTo": self.ship.toPlainText().strip(),
                "lines": [ln for ln in self.lines() if ln["description"] or ln["mpn"]],
                "currency": self.st.get("currency") or "CAD"}

    # ---------------------------------------------------------------- save
    def default_path(self):
        po = self.po()
        name = P.file_name(self.st, self.vendor.currentData() or "", po["number"], self.job.text(), po["date"])
        d = self.page.settings.value("inventory/po_dir") or self.page.settings.value("inventory/export_dir") or \
            os.path.expanduser("~")
        return os.path.join(str(d), name)

    def save(self, path=None):
        po = self.po()
        if not po["number"]:
            return QMessageBox.warning(self, "Purchase order", "Enter the P.O. number")
        if not po["lines"]:
            return QMessageBox.warning(self, "Purchase order", "Add at least one line")
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Save purchase order", self.default_path(), "PDF (*.pdf)")
            if not path:
                return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.page.settings.setValue("inventory/po_dir", os.path.dirname(path))
        try:
            P.write_pdf(path, self.st, po)
        except OSError as e:
            return QMessageBox.warning(self, "Purchase order", f"Couldn't write the PDF:\n{e}")
        vend = self.vendor.currentData() or ""
        rec = self.vendor_record()
        if rec is not None and self.save_addr.isChecked():
            rec["poBlock"] = po["vendor"]
        if self.log.isChecked() and vend != "No vendor set":
            P.log_orders(self.inv, po, vend, self.project_id)
        P.used_number(self.inv, po["number"])
        self.saved_path = path
        self.page.commit(f"PO {po['number']} saved" + (" — parts marked as ordered" if self.log.isChecked() else ""))
        self.accept()
