"""Reorder ▸ Request quotes… — one email per vendor, generated (not sent): copy it, or open it as a draft in your
mail program with the line list attached. Quotes… enters the prices that come back."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..inventory import model as M
from ..inventory import rfq as R


def drafts_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "Daedalus RFQs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class RFQDialog(QDialog):
    def __init__(self, page, items, parent=None):
        super().__init__(parent or page)
        self.page, self.inv, self.items = page, page.inv, list(items)
        self.st = R.settings(self.inv)
        self.rfq = R.next_number(self.st)
        self.mails: dict = {}
        self.sent: set = set()
        self.setWindowTitle(f"Request quotes — {self.rfq}")
        self.resize(1000, 680)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.every = QCheckBox("Ask every vendor that sells each part")
        self.every.setToolTip("Off: ask only each part's cheapest vendor")
        self.every.setChecked(True)
        self.every.toggled.connect(self.regroup)
        top.addWidget(self.every)
        self.prices = QCheckBox("Show our last price")
        self.prices.toggled.connect(self.rebuild)
        top.addWidget(self.prices)
        top.addStretch(1)
        top.addWidget(QLabel("Reply by"))
        from .date_field import DateField
        due = M.add_days(M.today(), int(M.num(self.st.get("dueDays") or 7))) if M.num(self.st.get("dueDays")) else ""
        self.due = DateField(due or M.today())
        self.due.changed.connect(self.rebuild) if hasattr(self.due, "changed") else None
        top.addWidget(self.due)
        tb = QPushButton("Template…")
        tb.setToolTip("Subject, wording, your name, numbering, attachment format")
        tb.clicked.connect(self.template)
        top.addWidget(tb)
        v.addLayout(top)
        sp = QSplitter()
        self.vendors = QListWidget()
        self.vendors.currentRowChanged.connect(lambda _r: self.show_mail())
        sp.addWidget(self.vendors)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.to = QLineEdit()
        self.to.setPlaceholderText("vendor email — add it on the Vendors tab to fill this in")
        self.subject = QLineEdit()
        f.addRow("To", self.to)
        f.addRow("Subject", self.subject)
        rv.addLayout(f)
        self.body = QPlainTextEdit()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.body.setFont(mono)
        rv.addWidget(self.body, 1)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        rv.addWidget(self.note)
        row = QHBoxLayout()
        self.draft_btn = QPushButton("Open as draft (with attachment)")
        self.draft_btn.setObjectName("primary")
        self.draft_btn.setToolTip("Opens in Outlook / Mail / Thunderbird ready to send, the line list attached")
        self.draft_btn.clicked.connect(self.open_draft)
        row.addWidget(self.draft_btn)
        mt = QPushButton("Open in mail (text only)")
        mt.setToolTip("Your default mail program via a mailto: link — no attachment")
        mt.clicked.connect(self.open_mailto)
        row.addWidget(mt)
        cp = QPushButton("Copy email")
        cp.clicked.connect(self.copy)
        row.addWidget(cp)
        sv = QPushButton("Save sheet…")
        sv.setToolTip("The line list as CSV or Excel, for the vendor to fill in prices")
        sv.clicked.connect(self.save_sheet)
        row.addWidget(sv)
        row.addStretch(1)
        rv.addLayout(row)
        sp.addWidget(right)
        sp.setSizes([260, 740])
        v.addWidget(sp, 1)
        self.log = QCheckBox("Log as requested (Quotes… then enters the prices that come back)")
        self.log.setChecked(True)
        v.addWidget(self.log)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.finish)
        bb.accepted.connect(self.finish)
        v.addWidget(bb)
        self.regroup()

    # ------------------------------------------------------------------ build
    def regroup(self, *_):
        self.groups = R.group(self.inv, self.items, self.every.isChecked())
        self.vendors.blockSignals(True)
        self.vendors.clear()
        for name in sorted(self.groups, key=lambda x: (x == R.NO_VENDOR, x.lower())):
            it = QListWidgetItem(f"{name}  ({len(self.groups[name])})")
            it.setData(256, name)
            self.vendors.addItem(it)
        self.vendors.blockSignals(False)
        self.rebuild()
        if self.vendors.count():
            self.vendors.setCurrentRow(0)

    def due_value(self):
        try:
            return self.due.value() or ""
        except Exception:  # noqa: BLE001
            return ""

    def rebuild(self, *_):
        self.mails = {n: R.compose(self.inv, n, items, self.rfq, self.due_value(), self.st, self.prices.isChecked())
                      for n, items in self.groups.items()}
        self.show_mail()

    def vendor(self):
        it = self.vendors.currentItem()
        return it.data(256) if it else None

    def show_mail(self):
        m = self.mails.get(self.vendor())
        if not m:
            return
        self.to.setText(m["to"])
        self.subject.setText(m["subject"])
        self.body.setPlainText(m["body"])
        miss = "" if m["to"] else " No email on file for this vendor."
        nv = " These parts have no vendor — pick one by adding a supplier on the part." \
            if m["vendor"] == R.NO_VENDOR else ""
        self.note.setText(f"{len(m['lines'])} line(s) · {m['rfq']}.{miss}{nv}")

    def current(self) -> dict:
        m = dict(self.mails[self.vendor()])
        m.update(to=self.to.text().strip(), subject=self.subject.text().strip(), body=self.body.toPlainText())
        self.mails[self.vendor()] = m
        return m

    def _mark(self, m):
        self.sent.add(m["vendor"])
        it = self.vendors.currentItem()
        if it and not it.text().startswith("✓ "):
            it.setText("✓ " + it.text())

    # ------------------------------------------------------------------ outputs
    def open_draft(self):
        if not self.vendor():
            return
        m = self.current()
        name = "".join(c if c.isalnum() or c in " .-_" else "_" for c in f"{m['rfq']} {m['vendor']}") + ".eml"
        p = drafts_dir() / name
        p.write_bytes(R.eml_bytes(m, self.st.get("attach") or "csv"))
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
            self.note.setText(f"Saved {p} — open it with your mail program.")
        self.last_draft = p
        self._mark(m)

    def open_mailto(self):
        if not self.vendor():
            return
        m = self.current()
        url = R.mailto(m)
        if url is None:
            self.copy()
            self.note.setText("Too long for a mail link — the email is copied instead; paste it into a new message "
                              "(or use Open as draft).")
            return
        QDesktopServices.openUrl(QUrl(url))
        self._mark(m)

    def copy(self):
        if not self.vendor():
            return
        m = self.current()
        QGuiApplication.clipboard().setText(f"To: {m['to']}\nSubject: {m['subject']}\n\n{m['body']}")
        self.note.setText("Copied — paste into a new email.")
        self._mark(m)

    def save_sheet(self):
        if not self.vendor():
            return
        m = self.current()
        base = "".join(c if c.isalnum() or c in " .-_" else "_" for c in f"{m['rfq']} {m['vendor']}")
        fn, filt = QFileDialog.getSaveFileName(self, "Save line list", base + ".xlsx", "Excel (*.xlsx);;CSV (*.csv)")
        if not fn:
            return
        if fn.lower().endswith(".csv"):
            Path(fn).write_text(m["csv"], encoding="utf-8")
        else:
            Path(fn).write_bytes(R.xlsx_bytes(m["lines"], m["rfq"]))
        self.note.setText(f"Saved {fn}")

    def template(self):
        d = RFQTemplateDialog(self.st, self)
        if d.exec():
            self.st.update(d.value())
            R.save_settings(self.inv, self.st)
            self.page.commit("RFQ template saved")
            self.rebuild()

    def finish(self):
        if self.log.isChecked() and self.sent:
            R.log(self.inv, [self.mails[n] for n in self.mails if n in self.sent and n != R.NO_VENDOR])
            self.page.commit(f"{self.rfq}: quotes requested from {len(self.sent)} vendor(s)")
        self.accept()


class RFQTemplateDialog(QDialog):
    FIELDS = "{vendor} {contact} {company} {rfq} {date} {due} {due_line} {lines} {count} {sender} {phone} {email}"

    def __init__(self, st, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RFQ template")
        self.resize(640, 560)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.subject = QLineEdit(st.get("subject") or R.SUBJECT)
        self.sender = QLineEdit(st.get("sender") or "")
        self.sender.setPlaceholderText("your name — signs the email")
        self.prefix = QLineEdit(st.get("prefix") or "RFQ-")
        self.next = QSpinBox()
        self.next.setRange(1, 999999)
        self.next.setValue(int(M.num(st.get("next") or 1)))
        nr = QHBoxLayout()
        nr.addWidget(self.prefix)
        nr.addWidget(QLabel("next"))
        nr.addWidget(self.next)
        nw = QWidget()
        nw.setLayout(nr)
        self.days = QSpinBox()
        self.days.setRange(0, 90)
        self.days.setSuffix(" days")
        self.days.setValue(int(M.num(st.get("dueDays") or 0)))
        self.attach = QComboBox()
        for k, label in (("csv", "CSV"), ("xlsx", "Excel"), ("none", "None")):
            self.attach.addItem(label, k)
        self.attach.setCurrentIndex(max(0, self.attach.findData(st.get("attach") or "csv")))
        f.addRow("Subject", self.subject)
        f.addRow("Signed", self.sender)
        f.addRow("Numbering", nw)
        f.addRow("Reply within", self.days)
        f.addRow("Attachment", self.attach)
        v.addLayout(f)
        self.body = QPlainTextEdit(st.get("body") or R.BODY)
        v.addWidget(self.body, 1)
        hint = QLabel("Fields: " + self.FIELDS)
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        v.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: (self.subject.setText(R.SUBJECT), self.body.setPlainText(R.BODY)))
        v.addWidget(bb)

    def value(self) -> dict:
        return {"subject": self.subject.text(), "body": self.body.toPlainText(), "sender": self.sender.text().strip(),
                "prefix": self.prefix.text(), "next": self.next.value(), "dueDays": self.days.value(),
                "attach": self.attach.currentData()}


class QuotesDialog(QDialog):
    """Open requests → type the prices that came back (or read the vendor's filled-in sheet)."""

    def __init__(self, page, parent=None):
        super().__init__(parent or page)
        self.page, self.inv = page, page.inv
        self.setWindowTitle("Quotes")
        self.resize(820, 560)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.pick = QComboBox()
        row.addWidget(QLabel("Request"))
        row.addWidget(self.pick, 1)
        rd = QPushButton("Read vendor's sheet…")
        rd.setToolTip("The CSV / Excel you attached, with the Unit Price column filled in")
        rd.clicked.connect(self.read_sheet)
        row.addWidget(rd)
        v.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Part", "Vendor #", "Qty", "Last price", "Quoted price"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        v.addWidget(self.table, 1)
        f = QFormLayout()
        self.lead = QLineEdit()
        self.lead.setPlaceholderText("days")
        self.cur = QComboBox()
        self.cur.addItems(["CAD", "USD"])
        f.addRow("Lead time", self.lead)
        f.addRow("Currency", self.cur)
        v.addLayout(f)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = bb.addButton("Save prices", QDialogButtonBox.AcceptRole)
        self.ok.setObjectName("primary")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.entries = R.open_requests(self.inv)
        for e in reversed(self.entries):
            self.pick.addItem(f"{e['rfq']} · {e['vendor']} · {M.fmt_date(e['date'])} · {len(e['lines'])} line(s)",
                              e["id"])
        self.pick.currentIndexChanged.connect(lambda _i: self.load())
        if not self.entries:
            self.info.setText("No open requests — Reorder ▸ Request quotes… creates them.")
            self.ok.setEnabled(False)
        self.load()

    def entry(self):
        eid = self.pick.currentData()
        return next((e for e in self.entries if e["id"] == eid), None)

    def load(self):
        e = self.entry()
        self.table.setRowCount(0)
        if not e:
            return
        for ln in e["lines"]:
            p = self.inv.part(ln["partId"]) or {"name": "(deleted part)"}
            s = next((s for s in self.inv.suppliers(p) if (s.get("name") or "").lower() == e["vendor"].lower()), {}) \
                if p.get("id") else {}
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [p.get("name", ""), s.get("partNumber") or "", f"{M.num(ln['qty']):g}",
                    M.fmt_money(s.get("price")) if s.get("price") not in (None, "") else "", ""]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(t)
                if c < 4:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, it)

    def read_sheet(self):
        e = self.entry()
        if not e:
            return
        fn, _ = QFileDialog.getOpenFileName(self, "Vendor's quote sheet", "", "Sheets (*.csv *.xlsx)")
        if not fn:
            return
        if fn.lower().endswith(".xlsx"):
            from openpyxl import load_workbook
            import csv
            import io
            ws = load_workbook(fn, read_only=True, data_only=True).active
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if x is None else x for x in row])
            text = buf.getvalue()
        else:
            text = Path(fn).read_text(encoding="utf-8", errors="replace")
        prices = R.read_quote_csv(text, e, self.inv)
        for i, ln in enumerate(e["lines"]):
            if ln["partId"] in prices:
                self.table.item(i, 4).setText(f"{prices[ln['partId']]:g}")
        self.info.setText(f"Read {len(prices)} price(s) from {os.path.basename(fn)}")

    def save(self):
        e = self.entry()
        if not e:
            return self.reject()
        prices = {}
        for i, ln in enumerate(e["lines"]):
            t = (self.table.item(i, 4).text() or "").replace("$", "").replace(",", "").strip()
            if t:
                try:
                    prices[ln["partId"]] = float(t)
                except ValueError:
                    pass
        lead = self.lead.text().strip()
        n = R.enter_quote(self.inv, e["id"], prices, int(M.num(lead)) if lead else None, self.cur.currentText())
        self.page.commit(f"{e['rfq']}: {n} price(s) from {e['vendor']}")
        self.accept()
