"""Orders (PO, name, projects → confirm → change orders) and the production work view (fab PO, purchasing
checklist split by vendor, finish estimate). The logic lives in inventory/orders.py."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..inventory import model as M
from ..inventory import orders as O
from . import theme
from .inventory_dialogs import _Base, _qty


def _when(ms) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else ""


# ------------------------------------------------------------------ order entry
class OrderEntryDialog(_Base):
    """PO, name, projects. Editing a confirmed order makes a change order."""

    def __init__(self, inv: M.Inventory, o=None, parent=None):
        changing = o is not None and O.status(o) == O.CONFIRMED
        super().__init__("Change Order" if changing else "Edit Order" if o else "New Order", parent,
                         "Save Change Order" if changing else "Save" if o else "Add Order")
        self.inv = inv
        r = o or {}
        f = QFormLayout()
        self.po = QLineEdit(r.get("poNumber") or "")
        self.po.setPlaceholderText("Customer PO #")
        self.name = QLineEdit(O.order_name(inv, r) if o else "")
        self.name.setPlaceholderText("Who / what the order is for")
        self.lines = QTableWidget(0, 3)
        self.lines.setHorizontalHeaderLabels(["Project", "Qty", "Configuration"])
        self.lines.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.lines.setColumnWidth(2, 230)
        self.lines.verticalHeader().hide()
        self.lines.setMinimumHeight(150)
        self._cfg: dict = {}                                    # row → configuration
        for ln in r.get("projectLines") or []:
            self._line(ln.get("projectId"), ln.get("qty", 1), ln.get("config"))
        add = QHBoxLayout()
        self.add_proj = QComboBox()
        for pr in inv.active_projects():
            self.add_proj.addItem(pr["name"], pr["id"])
        self.add_qty = _qty(1, 1)
        ab = QPushButton("+ Add")
        ab.clicked.connect(lambda: self._line(self.add_proj.currentData(), self.add_qty.value())
                           if self.add_proj.currentData() else None)
        rb = QPushButton("Remove")
        rb.clicked.connect(lambda: self.lines.removeRow(self.lines.currentRow()) if self.lines.currentRow() >= 0
                           else None)
        for w_ in (self.add_proj, self.add_qty, ab, rb):
            add.addWidget(w_, 2 if w_ is self.add_proj else 0)
        f.addRow("PO", self.po)
        f.addRow("Name", self.name)
        f.addRow("Projects", self.lines)
        f.addRow("", add)
        self.lay.addLayout(f)
        if changing:
            n = QLabel(f"This order is confirmed — saving logs change order {o.get('rev', 0) + 1} and updates its "
                       "production orders (they show as unread again).")
            n.setObjectName("muted")
            n.setWordWrap(True)
            self.lay.addWidget(n)
        self.resize(640, 400)
        self.finish()

    def _line(self, pid, qty, config=None):
        from ..inventory import configs as C
        pr = self.inv.project(pid)
        cfg = C.normalize(pr, config) if C.has_options(pr) else {}
        k = C.key(pr, cfg)
        for r in range(self.lines.rowCount()):                  # same project + configuration: add to its qty
            if self.lines.item(r, 0).data(Qt.UserRole) == pid and \
                    C.key(pr, self.lines.item(r, 0).data(Qt.UserRole + 1)) == k:
                w_ = self.lines.cellWidget(r, 1)
                w_.setValue(w_.value() + int(M.num(qty)))
                return
        r = self.lines.rowCount()
        self.lines.insertRow(r)
        it = QTableWidgetItem(self.inv.project_name(pid) or "(deleted project)")
        it.setData(Qt.UserRole, pid)
        it.setData(Qt.UserRole + 1, cfg)
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.lines.setItem(r, 0, it)
        self.lines.setCellWidget(r, 1, _qty(qty, 1))
        if C.has_options(pr):
            b = QPushButton(C.label(pr, cfg) or "Standard")
            b.setToolTip("Choose this line's configuration")

            def pick(_=False, it=it, b=b, pid=pid):
                from .project_custom_ui import ConfigDialog
                d = ConfigDialog(self.inv, pid, it.data(Qt.UserRole + 1), self)
                if d.exec() == QDialog.Accepted:
                    it.setData(Qt.UserRole + 1, d.value())
                    b.setText(C.label(self.inv.project(pid), d.value()) or "Standard")
            b.clicked.connect(pick)
            self.lines.setCellWidget(r, 2, b)
        else:
            na = QTableWidgetItem("—")
            na.setFlags(na.flags() & ~Qt.ItemIsEditable)
            self.lines.setItem(r, 2, na)

    def validate(self):
        if not self.name.text().strip():
            raise ValueError("Enter the order name")
        if not self.lines.rowCount():
            raise ValueError("Add at least one project")

    def values(self):
        return {"po": self.po.text().strip(), "name": self.name.text().strip(),
                "lines": [{"projectId": self.lines.item(r, 0).data(Qt.UserRole),
                           "qty": self.lines.cellWidget(r, 1).value(),
                           **({"config": self.lines.item(r, 0).data(Qt.UserRole + 1)}
                              if self.lines.item(r, 0).data(Qt.UserRole + 1) else {})}
                          for r in range(self.lines.rowCount())]}


class OrderHistoryDialog(QDialog):
    def __init__(self, inv, o, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Order log — {O.order_name(inv, o)}")
        v = QVBoxLayout(self)
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["When", "By", "What", "Changes"])
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for e in reversed(o.get("changes") or []):
            r = t.rowCount()
            t.insertRow(r)
            for c, val in enumerate((_when(e.get("at")), e.get("by") or "", e.get("what") or "",
                                     "\n".join(e.get("changes") or []))):
                t.setItem(r, c, QTableWidgetItem(val))
        t.resizeRowsToContents()
        t.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        v.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(760, 420)


# ------------------------------------------------------------------ production work view
def _state_combo(state):
    c = QComboBox()
    for k, lab in O.PO_STATES:
        c.addItem(lab, k)
    c.setCurrentIndex(max(0, c.findData(state)))
    col = {O.YES: theme.OK, O.NA: theme.MUTED, O.NO: theme.WARN}[state]
    c.setStyleSheet(f"QComboBox {{ color: {col}; font-weight: 600; }}")
    return c


class ProductionDialog(QDialog):
    """One production order: fab PO, then the purchasing checklist (one PO per vendor in each group, lines can be
    moved to another vendor), then the finish estimate. Every change is saved right away through `save`."""

    def __init__(self, inv: M.Inventory, p, save, parent=None):
        super().__init__(parent)
        self.inv, self.p, self.save = inv, p, save
        self.by = inv.userName or "unknown"
        self.setWindowTitle(f"Production — {O.line_name(inv, p.get('projectId'), p.get('config'))} ×"
                            f"{M.num(p.get('qty')):g}")
        self.resize(1000, 760)
        outer = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self.build()

    def _changed(self, msg, ask_finish=True):
        was_done = getattr(self, "_done", False)
        self.save(msg)
        done = O.all_pos_done(self.inv, self.p)
        self.build()
        if ask_finish and done and not was_done and not self.p.get("finishBy"):
            self.ask_finish()

    def ask_finish(self):
        """Every PO is done: jump to the inline time-to-finish box (type days or a finish date)."""
        box = getattr(self, "days_box", None)
        if box is None:
            return
        self.scroll.ensureWidgetVisible(box)
        box.setFocus()
        box.selectAll()

    def build(self):
        pos = self.scroll.verticalScrollBar().value()
        self._build()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    def _build(self):
        inv, p = self.inv, self.p
        self._done = O.all_pos_done(inv, p)
        w = QWidget()
        v = QVBoxLayout(w)
        head = QLabel(f"<b>{p.get('orderName') or 'Production order'}</b>"
                      + (f" · PO {p['poNumber']}" if p.get("poNumber") else "")
                      + f" · {O.line_name(inv, p.get('projectId'), p.get('config'))} ×{M.num(p.get('qty')):g}"
                      + f"<br><span style='color:{theme.MUTED}'>{O.stage(inv, p)} · status {p.get('status')}</span>")
        head.setProperty("dimsLink", f"production:{p['id']}")
        head.setToolTip("Shift-click to link this production order in chat")
        v.addWidget(head)
        # 1. fab PO
        v.addWidget(self._title("1 · Fab PO"))
        row = QHBoxLayout()
        st = _state_combo(O.fab_state(p))
        num_ = QLineEdit(p.get("fabPoNumber") or "")
        num_.setPlaceholderText("PO #")
        vend = QComboBox()
        vend.setEditable(True)
        vend.addItems([""] + sorted({x["name"] for x in inv.vendors}, key=str.lower))
        vend.setCurrentText(p.get("fabVendor") or "")
        vend.lineEdit().setPlaceholderText("Fab shop")

        def fab_save(*_):
            O.set_fab_po(p, st.currentData(), num_.text(), vend.currentText(), self.by)
            self._changed("Fab PO updated")
        st.currentIndexChanged.connect(fab_save)
        num_.editingFinished.connect(lambda: fab_save() if num_.text().strip() != (p.get("fabPoNumber") or "") else None)
        vend.lineEdit().editingFinished.connect(
            lambda: fab_save() if vend.currentText().strip() != (p.get("fabVendor") or "") else None)
        for x, lab in ((st, "Sent?"), (num_, None), (vend, None)):
            if lab:
                row.addWidget(QLabel(lab))
            row.addWidget(x, 0 if x is st else 1)
        v.addLayout(row)
        # 2. purchasing
        v.addWidget(self._title("2 · Purchasing POs — one per vendor; move a line to split"))
        if O.fab_state(p) not in O.DONE:
            v.addWidget(self._muted("Set the fab PO to Yes (or Not needed) to fill in the purchasing checklist."))
        else:
            cl = O.checklist(inv, p)
            if not cl:
                v.addWidget(self._muted("Nothing to buy for this project — no fasteners, purchased parts or "
                                        "powder coat / zinc finishes on its parts."))
            vendors = sorted({x["name"] for x in inv.vendors}, key=str.lower)
            for g, vends in cl.items():
                v.addWidget(self._sub(f"{g} · {len(vends)} PO{'s' if len(vends) != 1 else ''}"))
                for vendor, box in vends.items():
                    v.addWidget(self._po_box(g, vendor, box, vendors))
        # 3. dates — type or pick; days ⇄ finish date stay in step
        v.addWidget(self._title("3 · Dates"))
        v.addWidget(self._dates())
        # log
        v.addWidget(self._title("Log"))
        log = QPlainTextEdit("\n".join(f"{_when(e.get('at'))}  {e.get('by') or ''}: {e.get('what')}"
                                       for e in reversed(p.get("events") or [])))
        log.setReadOnly(True)
        log.setMaximumHeight(140)
        v.addWidget(log)
        v.addStretch(1)
        self.scroll.setWidget(w)

    def _dates(self):
        from PySide6.QtWidgets import QGridLayout, QSpinBox

        from .date_field import DateField
        p = self.p
        fr = QFrame()
        fr.setObjectName("card")
        fr.setStyleSheet(f"QFrame#card {{ border: 1px solid {theme.MUTED}; border-radius: 6px; }}")
        g = QGridLayout(fr)
        g.setHorizontalSpacing(10)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

        def cap(t):
            lab = QLabel(t)
            lab.setStyleSheet("font-weight: 600;")
            return lab

        def span_label(a, b):
            n = M.days_between(p.get(a), p.get(b))
            lab = QLabel("" if n is None else f"{n} day{'s' if n != 1 else ''}")
            lab.setObjectName("muted")
            return lab
        self.date_fields = {}

        def field(key):
            f = DateField(p.get(key) or "")
            f.changed.connect(lambda iso, k=key: self._date_set(k, iso))
            self.date_fields[key] = f
            return f
        # finish estimate: days ⇄ date
        days = QSpinBox()
        days.setRange(0, 999)
        days.setSuffix(" days")
        days.setSpecialValueText("—")
        days.setValue(int(p.get("finishDays") or 0) if p.get("finishBy") else 0)
        fin = DateField(p.get("finishBy") or "", placeholder="type or pick")
        self.days_box, self.finish_field = days, fin

        def days_set():
            n = days.value()
            if n == int(p.get("finishDays") or 0) and p.get("finishBy"):
                return
            if n == 0:
                O.set_finish_by(p, "", self.by)
            else:
                O.set_finish(p, n, self.by)
            fin.set_value(p.get("finishBy") or "")
            self.save(f"Finish estimate {n} day(s)" if n else "Finish estimate cleared")

        def fin_set(iso):
            O.set_finish_by(p, iso, self.by)
            days.blockSignals(True)
            days.setValue(int(p.get("finishDays") or 0))
            days.blockSignals(False)
            self.save(f"Finish by {iso}" if iso else "Finish estimate cleared")
        days.editingFinished.connect(days_set)
        fin.changed.connect(fin_set)
        g.addWidget(cap("Due"), 0, 0)
        g.addWidget(field("dueDate"), 0, 1)
        g.addWidget(cap("Ordered"), 0, 2)
        g.addWidget(field("orderDate"), 0, 3)
        g.addWidget(cap("Time to finish"), 1, 0)
        g.addWidget(days, 1, 1)
        g.addWidget(cap("Finish by"), 1, 2)
        g.addWidget(fin, 1, 3)
        for r, (lab, a, b) in enumerate((("Fab", "fabStart", "fabEnd"), ("Finishing", "finishStart", "finishEnd")), 2):
            g.addWidget(cap(f"{lab} start"), r, 0)
            g.addWidget(field(a), r, 1)
            g.addWidget(cap("end"), r, 2)
            g.addWidget(field(b), r, 3)
            g.addWidget(span_label(a, b), r, 4)
        due, fby = p.get("dueDate"), p.get("finishBy")
        if due and fby and fby > due:
            late = QLabel(f"⚠ Finish by is {M.days_between(due, fby)} day(s) after the due date")
            late.setStyleSheet(f"color:{theme.WARN};")
            g.addWidget(late, 4, 0, 1, 5)
        elif not fby and not self._done:
            s_, n_ = O.po_counts(self.inv, p)
            g.addWidget(self._muted(f"{s_} of {n_} POs done — the finish estimate is asked once every PO is Yes or "
                                    "Not needed, or set it here now."), 4, 0, 1, 5)
        hint = self._muted("Type dates directly: 11/20, Nov 20, fri, tomorrow, +10, 2w — or use ▾ for a calendar.")
        g.addWidget(hint, 5, 0, 1, 5)
        return fr

    def _date_set(self, key, iso):
        if O.set_date(self.p, key, iso, self.by):
            self.save(f"{O.DATE_FIELDS[key]} {iso or 'cleared'}")
            self.build()

    def _po_box(self, group, vendor, box, vendors):
        fr = QFrame()
        fr.setObjectName("card")
        fr.setStyleSheet(f"QFrame#card {{ border: 1px solid {theme.MUTED}; border-radius: 6px; }}")
        v = QVBoxLayout(fr)
        top = QHBoxLayout()
        lab = QLabel(f"<b>{vendor}</b> · {len(box['lines'])} line{'s' if len(box['lines']) != 1 else ''}")
        st = _state_combo(box["state"])
        num_ = QLineEdit(box.get("poNumber") or "")
        num_.setPlaceholderText("PO #")
        num_.setMaximumWidth(160)

        def save_state(*_):
            O.set_po(self.p, group, vendor, st.currentData(), num_.text(), self.by)
            self._changed(f"{group} PO to {vendor}: {st.currentText()}")
        st.currentIndexChanged.connect(save_state)
        num_.editingFinished.connect(lambda: save_state() if num_.text().strip() != (box.get("poNumber") or "")
                                     else None)
        top.addWidget(lab, 1)
        top.addWidget(QLabel("Sent?"))
        top.addWidget(st)
        top.addWidget(num_)
        v.addLayout(top)
        t = QTableWidget(len(box["lines"]), 6)
        t.setHorizontalHeaderLabels(["Part", "Need", "On Hand", "To Order", "Vendor (move to split)", ""])
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, ln in enumerate(box["lines"]):
            t.setItem(i, 0, QTableWidgetItem(M.label({"name": ln["name"], "partNumber": ln["partNumber"]})))
            for c, val in ((1, ln["qty"]), (2, ln["onHand"]), (3, ln["toOrder"])):
                it = QTableWidgetItem(f"{val:g}")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 3 and val > 0:
                    it.setForeground(QColor(theme.WARN))
                t.setItem(i, c, it)
            cb = QComboBox()
            cb.setEditable(True)
            cb.addItems(sorted(set(vendors) | {vendor}, key=str.lower))
            cb.setCurrentText(vendor)
            mv = QPushButton("Move")
            mv.clicked.connect(lambda _=False, pid=ln["partId"], cb=cb: self._move(group, pid, cb.currentText(),
                                                                                    vendor))
            t.setCellWidget(i, 4, cb)
            t.setCellWidget(i, 5, mv)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 6):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        t.verticalHeader().setDefaultSectionSize(32)
        t.setFixedHeight(min(t.horizontalHeader().sizeHint().height() + 32 * len(box["lines"]) + 4, 320))
        v.addWidget(t)
        return fr

    def _move(self, group, pid, new_vendor, old_vendor):
        new_vendor = new_vendor.strip()
        if not new_vendor or new_vendor == old_vendor:
            return
        O.move_line(self.p, group, pid, new_vendor, self.by)
        self._changed(f"Line moved to {new_vendor}", ask_finish=False)

    @staticmethod
    def _title(t):
        lab = QLabel(t)
        lab.setObjectName("section")
        lab.setStyleSheet("font-weight: 700; font-size: 14px; margin-top: 10px;")
        return lab

    @staticmethod
    def _sub(t):
        lab = QLabel(t)
        lab.setStyleSheet(f"font-weight: 600; color: {theme.ACCENT_TEXT}; margin-top: 4px;")
        return lab

    @staticmethod
    def _muted(t):
        lab = QLabel(t)
        lab.setObjectName("muted")
        lab.setWordWrap(True)
        return lab


def confirm_order(parent, inv, o) -> bool:
    lines = "\n".join(f"  {O.line_name(inv, l['projectId'], l.get('config'))} ×{M.num(l.get('qty')):g}"
                      for l in o.get("projectLines") or [])
    if QMessageBox.question(parent, "Confirm order", f"Confirm {O.order_name(inv, o)}"
                            + (f" (PO {o['poNumber']})" if o.get("poNumber") else "")
                            + f"?\n\n{lines}\n\nThis adds the demand and creates a production order for each "
                            "project.") != QMessageBox.Yes:
        return False
    O.confirm(inv, o, inv.userName)
    return True
