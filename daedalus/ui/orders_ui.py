"""Orders (PO, name, projects → confirm → change orders) and the production work view (fab PO, purchasing
checklist split by vendor, finish estimate). The logic lives in inventory/orders.py."""
from __future__ import annotations

from PySide6.QtCore import Qt
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
        self.lines = QTableWidget(0, 2)
        self.lines.setHorizontalHeaderLabels(["Project", "Qty"])
        self.lines.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.lines.verticalHeader().hide()
        self.lines.setMinimumHeight(150)
        for ln in r.get("projectLines") or []:
            self._line(ln.get("projectId"), ln.get("qty", 1))
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
        self.resize(520, 380)
        self.finish()

    def _line(self, pid, qty):
        for r in range(self.lines.rowCount()):                  # same project again: add to its qty
            if self.lines.item(r, 0).data(Qt.UserRole) == pid:
                w_ = self.lines.cellWidget(r, 1)
                w_.setValue(w_.value() + int(M.num(qty)))
                return
        r = self.lines.rowCount()
        self.lines.insertRow(r)
        it = QTableWidgetItem(self.inv.project_name(pid) or "(deleted project)")
        it.setData(Qt.UserRole, pid)
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.lines.setItem(r, 0, it)
        self.lines.setCellWidget(r, 1, _qty(qty, 1))

    def validate(self):
        if not self.name.text().strip():
            raise ValueError("Enter the order name")
        if not self.lines.rowCount():
            raise ValueError("Add at least one project")

    def values(self):
        return {"po": self.po.text().strip(), "name": self.name.text().strip(),
                "lines": [{"projectId": self.lines.item(r, 0).data(Qt.UserRole),
                           "qty": self.lines.cellWidget(r, 1).value()} for r in range(self.lines.rowCount())]}


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
        self.setWindowTitle(f"Production — {inv.project_name(p.get('projectId')) or '(deleted project)'} ×"
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
        days, ok = QInputDialog.getInt(self, "Time to finish", "Every PO is sent or not needed.\n"
                                       "How many days to finish this build?", int(self.p.get("finishDays") or 10),
                                       1, 365)
        if ok:
            O.set_finish(self.p, days, self.by)
            self.save(f"Finish estimate {days} day(s)")
            self.build()

    def build(self):
        inv, p = self.inv, self.p
        self._done = O.all_pos_done(inv, p)
        w = QWidget()
        v = QVBoxLayout(w)
        head = QLabel(f"<b>{p.get('orderName') or 'Production order'}</b>"
                      + (f" · PO {p['poNumber']}" if p.get("poNumber") else "")
                      + f" · {inv.project_name(p.get('projectId')) or '(deleted project)'} ×{M.num(p.get('qty')):g}"
                      + f"<br><span style='color:{theme.MUTED}'>{O.stage(inv, p)} · status {p.get('status')}</span>")
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
        # 3. finish
        v.addWidget(self._title("3 · Finish"))
        if p.get("finishBy"):
            r = QHBoxLayout()
            r.addWidget(QLabel(f"{p.get('finishDays')} day(s) → finish by <b>{M.fmt_date(p['finishBy'])}</b>"), 1)
            b = QPushButton("Change…")
            b.clicked.connect(self.ask_finish)
            r.addWidget(b)
            v.addLayout(r)
        elif self._done:
            b = QPushButton("Set time to finish…")
            b.clicked.connect(self.ask_finish)
            v.addWidget(b)
        else:
            s, n = O.po_counts(inv, p)
            v.addWidget(self._muted(f"{s} of {n} POs done — asked once every PO is Yes or Not needed."))
        # log
        v.addWidget(self._title("Log"))
        log = QPlainTextEdit("\n".join(f"{_when(e.get('at'))}  {e.get('by') or ''}: {e.get('what')}"
                                       for e in reversed(p.get("events") or [])))
        log.setReadOnly(True)
        log.setMaximumHeight(140)
        v.addWidget(log)
        v.addStretch(1)
        self.scroll.setWidget(w)

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
    lines = "\n".join(f"  {inv.project_name(l['projectId']) or '(deleted project)'} ×{M.num(l.get('qty')):g}"
                      for l in o.get("projectLines") or [])
    if QMessageBox.question(parent, "Confirm order", f"Confirm {O.order_name(inv, o)}"
                            + (f" (PO {o['poNumber']})" if o.get("poNumber") else "")
                            + f"?\n\n{lines}\n\nThis adds the demand and creates a production order for each "
                            "project.") != QMessageBox.Yes:
        return False
    O.confirm(inv, o, inv.userName)
    return True
