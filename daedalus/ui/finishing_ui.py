"""DIMS ▸ Finishing: Zinc Plating (weight × $/lb) and Powder Coating (quoted price per part, reused)."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..inventory import finishing as F
from ..inventory import model as M
from ..inventory import mrp as R
from ..inventory import project_export as PX
from . import theme
from .chat_panel import LINK_ROLE


def _btn(text, fn, tip="", primary=False):
    b = QPushButton(text)
    b.clicked.connect(lambda _=False: fn())
    if tip:
        b.setToolTip(tip)
    if primary:
        b.setObjectName("primary")
    return b


def _muted(text):
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def _fit(t):
    t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    t.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
    t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return t


def _item(v, right=False, money=False):
    if v is None or v == "":
        txt = ""
    elif money:
        txt = f"${v:,.2f}"
    elif isinstance(v, float):
        txt = f"{v:,.3f}".rstrip("0").rstrip(".")
    else:
        txt = str(v)
    it = QTableWidgetItem(txt)
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if right:
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return it


class FinishingView(QWidget):
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page, self.inv = page, page.inv
        self.st = page.mrp_state.setdefault("finishing", {})
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 14)
        v.setSpacing(8)
        # what's being finished: a project × units, or a production order
        src = QHBoxLayout()
        src.addWidget(QLabel("<b>For</b>"))
        self.src = QComboBox()
        for o in self.inv.productionOrders:
            if o.get("status") in ("Complete", "Cancelled"):
                continue
            nm = self.inv.project_name(o.get("projectId")) or "?"
            self.src.addItem(f"Production: {nm} × {M.num(o.get('qty')):g}" + (f" · PO {o['poNumber']}"
                                                                               if o.get("poNumber") else ""),
                             ("production", o["id"]))
        for pr in self.inv.active_projects():
            self.src.addItem(f"Project: {pr['name']}", ("project", pr["id"]))
        want = tuple(self.st.get("src") or ())
        for i in range(self.src.count()):
            if tuple(self.src.itemData(i)) == want:
                self.src.setCurrentIndex(i)
        self.units = QDoubleSpinBox()
        self.units.setRange(1, 100000)
        self.units.setDecimals(0)
        self.units.setPrefix("× ")
        self.units.setValue(self.st.get("units") or 1)
        self.units.setToolTip("Units of the project to finish")
        src.addWidget(self.src, 1)
        src.addWidget(self.units)
        v.addLayout(src)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        v.addWidget(self.tabs, 1)
        self.src.currentIndexChanged.connect(lambda _i: self._src_changed())
        self.units.valueChanged.connect(lambda _v: (self.st.__setitem__("units", self.units.value()), self.later()))
        self.tabs.currentChanged.connect(lambda i: self.st.__setitem__("tab", i))
        self.build()

    # ------------------------------------------------------------ plumbing
    def source(self):
        d = self.src.currentData()
        if not d:
            return None
        kind, id_ = d
        return {"kind": kind, "id": id_, "units": self.units.value()}

    def _src_changed(self):
        d = self.src.currentData()
        self.st["src"] = list(d) if d else None
        self.units.setVisible(bool(d) and d[0] == "project")
        self.later()

    def later(self):
        QTimer.singleShot(0, self.build)          # never rebuild inside the signal that asked for it

    def save(self, msg):
        """Save + rebuild — after the signal that asked for it (rebuilding inside it would delete its sender)."""
        QTimer.singleShot(0, lambda: self.page.commit(msg))

    def build(self):
        keep = self.st.get("tab", 0)
        self.tabs.blockSignals(True)
        while self.tabs.count():
            w = self.tabs.widget(0)
            self.tabs.removeTab(0)
            w.deleteLater()
        d = self.src.currentData()
        self.units.setVisible(bool(d) and d[0] == "project")
        if not d:
            empty = _muted("Add a project or confirm an order first — finishing is priced for what's being built.")
            self.tabs.addTab(empty, "Zinc Plating")
        else:
            self.tabs.addTab(self.zinc_tab(), "Zinc Plating")
            self.tabs.addTab(self.powder_tab(), "Powder Coating")
        self.tabs.setCurrentIndex(min(keep, self.tabs.count() - 1))
        self.tabs.blockSignals(False)

    def _title(self):
        return re.sub(r"^(Production|Project): ", "", self.src.currentText())

    # ------------------------------------------------------------ zinc
    def zinc_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        src = self.source()
        z = F.zinc_rows(self.inv, src)
        top = QHBoxLayout()
        top.addWidget(QLabel("Rate"))
        rate = QDoubleSpinBox()
        rate.setRange(0, 1000)
        rate.setDecimals(2)
        rate.setPrefix("$ ")
        rate.setSuffix(" / lb")
        rate.setValue(z["rate"])
        rate.setToolTip("The plater's rate — shared, so everyone prices with the same number")

        def set_rate():
            if abs(rate.value() - F.zinc_rate(self.inv)) > 1e-9:
                F.set_zinc_rate(self.inv, rate.value())
                self.save(f"Zinc plating rate ${rate.value():.2f}/lb")
        rate.editingFinished.connect(set_rate)
        top.addWidget(rate)
        top.addWidget(_muted("Zinc plating is priced by weight: weight per part × qty × rate."), 1)
        top.addWidget(_btn("Parts to plate…", lambda: self.choose(F.ZINC),
                           "Tick which MFG parts in this build get zinc plated"))
        v.addLayout(top)
        cols = ["Item", "Part Number", "Part", "Qty", "Weight (lb)", "Total (lb)", "Rate", "Cost"]
        t = QTableWidget(len(z["rows"]), len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().hide()
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(True)
        for i, r in enumerate(z["rows"]):
            p = r["part"]
            t.setItem(i, 0, _item(r["item"], True))
            pn = _item(p.get("partNumber") or "")
            pn.setData(LINK_ROLE, ("part", p["id"]))
            t.setItem(i, 1, pn)
            t.setItem(i, 2, _item(p["name"]))
            t.setItem(i, 3, _item(r["qty"] if not float(r["qty"]).is_integer() else int(r["qty"]), True))
            wt = QDoubleSpinBox()                                 # weight per part: typed here, kept on the part
            wt.setRange(0, 100000)
            wt.setDecimals(3)
            wt.setValue(r["weight"])
            wt.setSpecialValueText("enter weight")
            wt.setAlignment(Qt.AlignRight)
            if r["missing"]:
                wt.setStyleSheet(f"color:{theme.WARN};")
            wt.editingFinished.connect(lambda wt=wt, p=p: self.set_weight(p, wt.value()))
            t.setCellWidget(i, 4, wt)
            t.setItem(i, 5, _item(r["total"] or None, True))
            t.setItem(i, 6, _item(r["rate"], True, money=True))
            t.setItem(i, 7, _item(None if r["missing"] else r["cost"], True, money=True))
        t.resizeColumnsToContents()
        t.setColumnWidth(4, max(t.columnWidth(4), 130))
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(_fit(t) if z["rows"] else _muted("No zinc-plated parts in this build yet — use “Parts to "
                                                      "plate…” to pick them."))
        foot = QHBoxLayout()
        miss = f"  ·  <span style='color:{theme.WARN}'>{z['missing']} without a weight</span>" if z["missing"] else ""
        foot.addWidget(QLabel(f"{len(z['rows'])} parts · {z['totalLb']:,.2f} lb{miss}"), 1)
        foot.addWidget(QLabel(f"<b style='font-size:15px'>Zinc Plating Total  ${z['total']:,.2f}</b>"))
        v.addLayout(foot)
        acts = QHBoxLayout()
        acts.addStretch(1)
        acts.addWidget(_btn("Copy table", lambda: self.copy(F.ZINC_COLS, F.zinc_table(z), z["total"],
                                                            "Zinc Plating Total"),
                            "Pastes into Excel or an email to the plater"))
        acts.addWidget(_btn("Export Excel…", lambda: self.export(z, None), primary=True))
        v.addLayout(acts)
        v.addStretch(1)
        self._zinc = z
        return w

    def set_weight(self, p, w):
        if abs(F.weight(p) - w) < 1e-9:
            return
        p["weightLb"] = round(w, 4)
        p["updatedAt"] = M.now_ms()
        self.save(f"Weight {w:g} lb — {M.label(p)}")

    # ------------------------------------------------------------ powder
    def powder_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        pc = F.powder_rows(self.inv, self.source())
        top = QHBoxLayout()
        top.addWidget(_muted("Powder coating is quoted per part. A quote is kept on the part and used again for "
                             "later orders until a new one replaces it."), 1)
        top.addWidget(_btn("Parts to coat…", lambda: self.choose(F.POWDER),
                           "Tick which MFG parts in this build get powder coated"))
        v.addLayout(top)
        cols = ["Item", "Part Number", "Part", "Qty", "Colour / Spec", "Price / Part", "Cost", "Quoted", "Vendor", ""]
        t = QTableWidget(len(pc["rows"]), len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().hide()
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(True)
        for i, r in enumerate(pc["rows"]):
            p = r["part"]
            t.setItem(i, 0, _item(r["item"], True))
            pn = _item(p.get("partNumber") or "")
            pn.setData(LINK_ROLE, ("part", p["id"]))
            t.setItem(i, 1, pn)
            t.setItem(i, 2, _item(p["name"]))
            t.setItem(i, 3, _item(int(r["qty"]) if float(r["qty"]).is_integer() else r["qty"], True))
            t.setItem(i, 4, _item(r["spec"]))
            if r["price"] is None:
                need = _item("needs quote")
                need.setForeground(QBrush(QColor(theme.WARN)))
                t.setItem(i, 5, need)
            else:
                t.setItem(i, 5, _item(r["price"], True, money=True))
            t.setItem(i, 6, _item(r["cost"], True, money=True))
            q = r["quote"] or {}
            qd = _item(M.fmt_date(q["date"]) if q.get("date") else "")
            hist = [f"{M.fmt_date(x.get('date'))}: ${M.num(x.get('price')):,.2f} {x.get('currency') or 'CAD'}"
                    + (f" · {x['vendor']}" if x.get("vendor") else "") + (f" · {x['ref']}" if x.get("ref") else "")
                    for x in (p.get("pcQuotes") or [])]
            if hist:
                qd.setToolTip("Quote history\n" + "\n".join(reversed(hist)))
            t.setItem(i, 7, qd)
            t.setItem(i, 8, _item(r["vendor"]))
            b = QPushButton("New quote…" if r["price"] is not None else "Add quote…")
            b.clicked.connect(lambda _=False, p=p: QTimer.singleShot(0, lambda: self.quote(p)))
            t.setCellWidget(i, 9, b)
        t.resizeColumnsToContents()
        t.setColumnWidth(9, max(t.columnWidth(9), b.sizeHint().width() + 16 if pc["rows"] else 0))
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(_fit(t) if pc["rows"] else _muted("No powder-coated parts in this build yet — use “Parts to "
                                                       "coat…” to pick them."))
        foot = QHBoxLayout()
        need = f"  ·  <span style='color:{theme.WARN}'>{pc['needQuote']} need a quote</span>" if pc["needQuote"] else ""
        foot.addWidget(QLabel(f"{len(pc['rows'])} parts{need}"), 1)
        foot.addWidget(QLabel(f"<b style='font-size:15px'>Powder Coating Total  ${pc['total']:,.2f}</b>"
                              + (" <span style='color:%s'>(quoted parts only)</span>" % theme.MUTED
                                 if pc["needQuote"] else "")))
        v.addLayout(foot)
        acts = QHBoxLayout()
        acts.addStretch(1)
        if pc["needQuote"]:
            acts.addWidget(_btn("Copy quote request", lambda: self.copy_rfq(pc),
                                "Parts that need a price, with qty, spec and weight — paste into an email"))
        acts.addWidget(_btn("Copy table", lambda: self.copy(F.POWDER_COLS, F.powder_table(pc), pc["total"],
                                                            "Powder Coating Total")))
        acts.addWidget(_btn("Export Excel…", lambda: self.export(None, pc), primary=True))
        v.addLayout(acts)
        v.addStretch(1)
        self._powder = pc
        return w

    def quote(self, p):
        d = QDialog(self)
        d.setWindowTitle(f"Powder coat quote — {M.label(p)}")
        lay = QVBoxLayout(d)
        lq = F.last_quote(p)
        if lq:
            lay.addWidget(_muted(f"Last quote: ${M.num(lq['price']):,.2f} {lq.get('currency') or 'CAD'} on "
                                 f"{M.fmt_date(lq.get('date'))}" + (f" from {lq['vendor']}" if lq.get("vendor") else "")))
        row = QHBoxLayout()
        price = QDoubleSpinBox()
        price.setRange(0, 1_000_000)
        price.setDecimals(2)
        price.setPrefix("$ ")
        price.setValue(M.num((lq or {}).get("price")))
        cur = QComboBox()
        cur.addItems(["CAD", "USD"])
        cur.setCurrentText((lq or {}).get("currency") or "CAD")
        row.addWidget(QLabel("Price per part"))
        row.addWidget(price)
        row.addWidget(cur)
        lay.addLayout(row)
        vend = QLineEdit((lq or {}).get("vendor") or R.fin_of(p).get("vendor") or "")
        vend.setPlaceholderText("Powder coater")
        ref = QLineEdit()
        ref.setPlaceholderText("Quote number (optional)")
        lay.addWidget(vend)
        lay.addWidget(ref)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Save quote")
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        if d.exec() != QDialog.Accepted or price.value() <= 0:
            return
        F.add_quote(self.inv, p, price.value(), vend.text().strip(), cur.currentText(), ref.text().strip())
        self.save(f"Powder coat quote ${price.value():.2f} — {M.label(p)}")

    # ------------------------------------------------------------ which parts get which finish
    def choose(self, kind):
        cands = F.candidates(self.inv, self.source())
        d = QDialog(self)
        d.setWindowTitle("Parts to plate" if kind == F.ZINC else "Parts to coat")
        d.resize(620, 560)
        lay = QVBoxLayout(d)
        lay.addWidget(_muted("Ticked parts get this finish on their part profile, so they show up here for every "
                             "build that uses them."))
        t = QTableWidget(len(cands), 4)
        t.setHorizontalHeaderLabels(["", "Part Number", "Part", "Current finish"])
        t.verticalHeader().hide()
        boxes = []
        for i, (p, _q) in enumerate(cands):
            cb = QCheckBox()
            cb.setChecked(F.kind_of(p) == kind)
            boxes.append((cb, p))
            t.setCellWidget(i, 0, cb)
            t.setItem(i, 1, _item(p.get("partNumber") or ""))
            t.setItem(i, 2, _item(p["name"]))
            t.setItem(i, 3, _item(R.fin_of(p).get("process") or ""))
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        lay.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        if d.exec() != QDialog.Accepted:
            return
        n = 0
        for cb, p in boxes:
            on, was = cb.isChecked(), F.kind_of(p) == kind
            if on != was:
                F.set_kind(p, kind if on else None)
                p["updatedAt"] = M.now_ms()
                n += 1
        if n:
            self.save(f"Finish updated on {n} part(s)")

    # ------------------------------------------------------------ out
    def copy(self, cols, rows, total, label):
        text = PX.tsv(cols, rows + [[""] * (len(cols) - 2) + [label, round(total, 2)]])
        QGuiApplication.clipboard().setText(text)
        self.page.toast.show_msg("Copied — paste into Excel or an email")

    def copy_rfq(self, pc):
        rows = [[r["part"].get("partNumber") or "", r["part"]["name"], r["qty"], r["spec"],
                 F.weight(r["part"]) or ""] for r in pc["rows"] if r["price"] is None]
        QGuiApplication.clipboard().setText(PX.tsv(["Part Number", "Part", "Qty", "Colour / Spec", "Weight (lb)"],
                                                   rows))
        self.page.toast.show_msg(f"Copied {len(rows)} part(s) to quote")

    def export(self, z, pc):
        name = re.sub(r"[^\w\- ]+", "", self._title()).strip().replace(" ", "-") or "finishing"
        kind = "zinc-plating" if z else "powder-coating"
        path = self.page._save_path(f"{name}-{kind}-{M.today()}.xlsx", "Excel (*.xlsx)")
        if path:
            F.write_xlsx(path, self._title(), z, pc)
            self.page.toast.show_msg("Exported")
