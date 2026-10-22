"""Inventory dialogs: part, stock movement, project, location, vendor, orders, batches, duplicates, history."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHeaderView,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import model as M
from . import theme


def _date_edit(value: str | None, allow_empty=True):
    w = QDateEdit()
    w.setCalendarPopup(True)
    w.setDisplayFormat("yyyy-MM-dd")
    if allow_empty:
        w.setSpecialValueText("—")
        w.setMinimumDate(QDate(2000, 1, 1))
    d = M.parse_date(value)
    w.setDate(QDate(d.year, d.month, d.day) if d else (w.minimumDate() if allow_empty else QDate.currentDate()))
    return w


def _date_value(w: QDateEdit) -> str:
    if w.date() == w.minimumDate() and w.specialValueText():
        return ""
    return w.date().toString("yyyy-MM-dd")


def _qty(v=0, lo=0, hi=10_000_000):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(int(M.num(v)))
    return s


def _money(v=0.0):
    s = QDoubleSpinBox()
    s.setRange(0, 10_000_000)
    s.setDecimals(2)
    s.setValue(M.num(v))
    return s


def _currency(v="CAD"):
    c = QComboBox()
    c.addItems(["CAD", "USD"])
    c.setCurrentText(v or "CAD")
    return c


class _Base(QDialog):
    def __init__(self, title, parent=None, ok="Save"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.lay = QVBoxLayout(self)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText(ok)
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)

    def finish(self):
        self.lay.addWidget(self.bb)

    def _ok(self):
        try:
            if self.validate() is False:
                return
        except ValueError as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        self.accept()

    def validate(self):
        return True


# ------------------------------------------------------------------ part ---

class PartDialog(_Base):
    def __init__(self, inv: M.Inventory, part=None, parent=None):
        super().__init__("Edit Part" if part else "Add Part", parent, "Save Changes" if part else "Add Part")
        self.inv, self.part = inv, part
        p = part or M.new_part()
        self.resize(640, 760)
        body = QWidget()
        f = QFormLayout(body)
        self.type = QComboBox()
        for k, lab in M.TYPES:
            self.type.addItem(lab, k)
        self.type.setCurrentIndex([k for k, _ in M.TYPES].index(p.get("type", "fastener")))
        self.name = QLineEdit(p.get("name", ""))
        self.cat_combo = QComboBox()
        self.cat_combo.addItem("—", "")
        for c in M.FASTENER_CATEGORIES:
            self.cat_combo.addItem(c, c)
        cat = p.get("category") or ""
        self.cat_custom = QLineEdit(cat if cat and cat not in M.FASTENER_CATEGORIES else "")
        self.cat_custom.setPlaceholderText("Custom category (e.g. Speed Nut)")
        self.cat_combo.setCurrentIndex(self.cat_combo.findData(cat if cat in M.FASTENER_CATEGORIES else
                                                               ("Other" if cat else "")))
        self.cat_text = QLineEdit(cat)
        self.cat_text.setPlaceholderText("e.g. Bracket, Panel")
        f.addRow("Type", self.type)
        f.addRow("Name", self.name)
        self.cat_row = QWidget()
        cr = QHBoxLayout(self.cat_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self.cat_combo)
        cr.addWidget(self.cat_custom)
        cr.addWidget(self.cat_text)
        f.addRow("Category", self.cat_row)
        # fastener spec
        self.fast = QGroupBox("Fastener spec (name fills in automatically)")
        g = QGridLayout(self.fast)
        self.head = QComboBox()
        self.thread_type = QComboBox()
        self.thread_type.addItems(["", *M.THREAD_TYPES])
        self.thread_type.setCurrentText(p.get("threadType") or "")
        self.std = QComboBox()
        self.std.addItems(["", *M.THREAD_STANDARDS])
        self.std.setCurrentText(p.get("threadStandard") or "")
        self.size = QComboBox()
        self.size.setEditable(True)
        self.size.addItems(["", *M.METRIC_SIZES, *M.IMPERIAL_SIZES])
        self.size.setCurrentText(p.get("size") or "")
        self.length = QLineEdit(str(p.get("length") or ""))
        self.length.setPlaceholderText("e.g. 1, 1-1/2, 20 (blank for nuts/washers)")
        for i, (lab, w) in enumerate((("Type", self.head), ("Thread type", self.thread_type),
                                      ("Thread standard", self.std), ("Size", self.size), ("Length", self.length))):
            g.addWidget(QLabel(lab), i // 2, (i % 2) * 2)
            g.addWidget(w, i // 2, (i % 2) * 2 + 1)
        f.addRow(self.fast)
        self.pn = QLineEdit(p.get("partNumber") or "")
        self.qb = QLineEdit(p.get("qbPartNumber") or "")
        self.qb.setPlaceholderText("QuickBooks item number")
        f.addRow("Part number", self.pn)
        f.addRow("QuickBooks part #", self.qb)
        dr = QHBoxLayout()
        self.drawing = QLineEdit(p.get("drawingUrl") or "")
        self.drawing.setPlaceholderText("Drawing / datasheet: file path or web link")
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse)
        dr.addWidget(self.drawing)
        dr.addWidget(browse)
        f.addRow("Drawing / datasheet", dr)
        if p.get("stdPartId"):
            try:
                from ..stdparts import get_library
                si = get_library().get(p["stdPartId"])
                txt = f"{si['name']} · v{si.get('version', 1)} · {si.get('category') or 'standard library'}"
            except Exception:  # noqa: BLE001
                txt = f"{p['stdPartId']} (not found in the library folder)"
            self.std_lab = QLabel(txt)
            self.std_lab.setToolTip("Linked to this Daedalus standard part (Std parts ▸ Inventory tab to unlink)")
            f.addRow("Standard part", self.std_lab)
        self.notes = QPlainTextEdit(p.get("notes") or "")
        self.notes.setFixedHeight(54)
        f.addRow("Notes", self.notes)
        # weldment
        self.weld = QCheckBox("This is a weldment (assembly of other parts)")
        self.weld.setChecked(bool(p.get("isWeldment")))
        f.addRow(self.weld)
        self.comp_box = QGroupBox("Components")
        cl = QVBoxLayout(self.comp_box)
        self.comps = QTableWidget(0, 2)
        self.comps.setHorizontalHeaderLabels(["Part", "Qty"])
        self.comps.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.comps.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.comps.verticalHeader().hide()
        self.comps.setMinimumHeight(110)
        self.comps.setMaximumHeight(220)
        cl.addWidget(self.comps)
        ch = QHBoxLayout()
        self.comp_part = QComboBox()
        self.comp_qty = _qty(1, 1)
        add = QPushButton("+ Add")
        add.clicked.connect(self._add_comp)
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self.comps.removeRow(self.comps.currentRow()) if self.comps.currentRow() >= 0
                           else None)
        ch.addWidget(self.comp_part, 2)
        ch.addWidget(self.comp_qty)
        ch.addWidget(add)
        ch.addWidget(rm)
        cl.addLayout(ch)
        f.addRow(self.comp_box)
        for c in p.get("components") or []:
            cp = inv.part(c.get("partId"))
            if cp:
                self._comp_row(cp, c.get("qty", 1))
        # stock
        self.stock = QSpinBox()
        self.stock.setRange(-1_000_000, 10_000_000)
        self.stock.setValue(int(M.num(p.get("currentStock"))))
        self.unit = QLineEdit(p.get("unit") or "ea")
        self.rp = _qty(p.get("reorderPoint"))
        self.rq = _qty(p.get("reorderQty"))
        grid = QGridLayout()
        for i, (lab, w) in enumerate((("Current stock", self.stock), ("Unit", self.unit), ("Reorder point", self.rp),
                                      ("Reorder qty", self.rq))):
            grid.addWidget(QLabel(lab), i // 2, (i % 2) * 2)
            grid.addWidget(w, i // 2, (i % 2) * 2 + 1)
        f.addRow(grid)
        cost = QHBoxLayout()
        self.cost = _money(p.get("unitCost"))
        self.cost_cur = _currency(p.get("unitCostCurrency"))
        self.cost_hint = QLabel()
        self.cost_hint.setObjectName("muted")
        cost.addWidget(self.cost)
        cost.addWidget(self.cost_cur)
        cost.addWidget(self.cost_hint, 1)
        f.addRow("Unit cost", cost)
        self.loc = QComboBox()
        self.loc.addItem("— None —", None)
        for lid, lab in inv.location_options():
            self.loc.addItem(lab, lid)
        lid = p.get("locationId")
        i = self.loc.findData(lid)
        if i < 0 and lid:
            lab = inv.location_label_for(lid)
            if lab:
                self.loc.addItem(lab, lid)
                i = self.loc.count() - 1
        self.loc.setCurrentIndex(max(0, i))
        f.addRow("Location", self.loc)
        # suppliers
        sup_box = QGroupBox("Suppliers")
        sg = QGridLayout(sup_box)
        names = [v["name"] for v in inv.sorted_vendors()]
        self.sups = []
        for i, L in enumerate("ABC"):
            s = ((p.get("suppliers") or []) + [{}, {}, {}])[i] or {}
            nm = QLineEdit(s.get("name") or "")
            nm.setPlaceholderText(f"Supplier {L} (autocompletes vendors)")
            nm.setCompleter(QCompleter(names))
            pr = QDoubleSpinBox()
            pr.setRange(-1, 1_000_000)
            pr.setDecimals(2)
            pr.setSpecialValueText("—")
            pr.setValue(M.num(s.get("price"), -1) if s.get("price") not in (None, "") else -1)
            cur = _currency(s.get("currency"))
            use = QPushButton("Use as cost")
            use.clicked.connect(lambda _=False, pr=pr, cur=cur: (self.cost.setValue(max(pr.value(), 0)),
                                                                self.cost_cur.setCurrentText(cur.currentText())))
            spn = QLineEdit(s.get("partNumber") or "")
            spn.setPlaceholderText("Supplier part #")
            web = QLineEdit(s.get("website") or "")
            web.setPlaceholderText("https://")
            sg.addWidget(nm, i * 2, 0, 1, 2)
            sg.addWidget(pr, i * 2, 2)
            sg.addWidget(cur, i * 2, 3)
            sg.addWidget(use, i * 2, 4)
            sg.addWidget(spn, i * 2 + 1, 0)
            sg.addWidget(web, i * 2 + 1, 1, 1, 4)
            self.sups.append((nm, pr, cur, spn, web))
        f.addRow(sup_box)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(body)
        self.lay.addWidget(sc)
        if part:
            h = QHBoxLayout()
            dl = QPushButton("Delete part")
            dl.setStyleSheet(f"color:{theme.ERROR}")
            dl.clicked.connect(self._delete)
            h.addWidget(dl)
            h.addStretch(1)
            self.lay.addLayout(h)
        self.deleted = False
        self.finish()
        self.type.currentIndexChanged.connect(self._sync)
        self.cat_combo.currentIndexChanged.connect(self._sync_cat)
        for w in (self.head, self.thread_type, self.std):
            w.currentIndexChanged.connect(self._autoname)
        self.size.currentTextChanged.connect(self._autoname)
        self.length.textChanged.connect(self._autoname)
        self.cat_custom.textChanged.connect(self._autoname)
        self.weld.toggled.connect(self._sync)
        self.cost.valueChanged.connect(self._hint)
        self.cost_cur.currentIndexChanged.connect(self._hint)
        self._sync_cat(init_head=p.get("headStyle") or "")
        self._sync()
        self._hint()
        self._fill_comp_parts()

    def _browse(self):
        from PySide6.QtWidgets import QFileDialog
        f, _ = QFileDialog.getOpenFileName(self, "Drawing / datasheet", "", "PDF and drawings (*.pdf *.dxf *.dwg "
                                                                            "*.png *.jpg *.daedalus);;All files (*)")
        if f:
            self.drawing.setText(f)

    def _hint(self):
        self.cost_hint.setText(f"≈ {M.fmt_money(self.inv.to_cad(self.cost.value(), 'USD'))} CAD"
                               if self.cost_cur.currentText() == "USD" and self.cost.value() else "")

    def category(self):
        if self.type.currentData() != "fastener":
            return self.cat_text.text().strip()
        c = self.cat_combo.currentData()
        return self.cat_custom.text().strip() if c == "Other" and self.cat_custom.text().strip() else (c or "")

    def _sync_cat(self, *_a, init_head=None):
        cur = init_head if init_head is not None else self.head.currentText()
        self.head.blockSignals(True)
        self.head.clear()
        self.head.addItems(["", *M.type_options_for_category(self.cat_combo.currentData() or "")])
        self.head.setCurrentText(cur)
        self.head.blockSignals(False)
        self.cat_custom.setVisible(self.type.currentData() == "fastener" and self.cat_combo.currentData() == "Other")
        self._autoname()

    def _sync(self):
        fast = self.type.currentData() == "fastener"
        self.fast.setVisible(fast)
        self.cat_combo.setVisible(fast)
        self.cat_custom.setVisible(fast and self.cat_combo.currentData() == "Other")
        self.cat_text.setVisible(not fast)
        manu = self.type.currentData() == "manufactured"
        self.weld.setVisible(manu)
        self.comp_box.setVisible(manu and self.weld.isChecked())

    def _autoname(self, *_):
        if self.type.currentData() != "fastener":
            return
        n = M.fastener_name(self.size.currentText().strip(), self.std.currentText(), self.head.currentText(),
                            self.thread_type.currentText(), self.category(), self.length.text())
        if n:
            self.name.setText(n)

    def _fill_comp_parts(self):
        self.comp_part.clear()
        me = self.part["id"] if self.part else None
        for p in sorted(self.inv.parts, key=lambda x: x["name"].lower()):
            if p["id"] == me or (me and self.inv.contains_recursive(p["id"], me)):
                continue
            self.comp_part.addItem(p["name"], p["id"])

    def _comp_row(self, part, qty):
        r = self.comps.rowCount()
        self.comps.insertRow(r)
        it = QTableWidgetItem(part["name"])
        it.setData(Qt.UserRole, part["id"])
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.comps.setItem(r, 0, it)
        self.comps.setCellWidget(r, 1, _qty(qty, 1))
        self.comps.setMinimumHeight(min(220, 40 + 32 * max(self.comps.rowCount(), 2)))

    def _add_comp(self):
        pid = self.comp_part.currentData()
        if pid:
            self._comp_row(self.inv.part(pid), self.comp_qty.value())

    def _delete(self):
        if QMessageBox.question(self, "Delete", f"Delete {self.part['name']}? This cannot be undone.") == \
                QMessageBox.Yes:
            self.deleted = True
            self.accept()

    def validate(self):
        if not self.name.text().strip():
            raise ValueError("Give the part a name")
        return True

    def values(self) -> dict:
        typ = self.type.currentData()
        sups = []
        for nm, pr, cur, spn, web in self.sups:
            sups.append({"name": nm.text().strip(), "price": pr.value() if pr.value() >= 0 else "",
                         "currency": cur.currentText(), "partNumber": spn.text().strip(), "website": web.text().strip()})
        for s in sups:
            if s["name"]:
                self.inv.ensure_vendor(s["name"])
        out = {"type": typ, "name": self.name.text().strip(), "category": self.category(),
               "partNumber": self.pn.text().strip(), "qbPartNumber": self.qb.text().strip(),
               "drawingUrl": self.drawing.text().strip(), "notes": self.notes.toPlainText().strip(),
               "currentStock": self.stock.value(), "unit": self.unit.text().strip() or "ea",
               "reorderPoint": self.rp.value(), "reorderQty": self.rq.value(), "unitCost": self.cost.value(),
               "unitCostCurrency": self.cost_cur.currentText(), "locationId": self.loc.currentData(),
               "suppliers": sups, "updatedAt": M.now_ms()}
        if typ == "fastener":
            out.update(headStyle=self.head.currentText(), threadType=self.thread_type.currentText(),
                       threadStandard=self.std.currentText(), size=self.size.currentText().strip(),
                       length=self.length.text().strip())
        if typ == "manufactured":
            out["isWeldment"] = self.weld.isChecked()
            out["components"] = [{"partId": self.comps.item(r, 0).data(Qt.UserRole),
                                  "qty": self.comps.cellWidget(r, 1).value()} for r in range(self.comps.rowCount())] \
                if self.weld.isChecked() else []
        return out


class MoveDialog(_Base):
    """Use (deduct) or receive (add) stock."""

    def __init__(self, part, mode, parent=None):
        super().__init__(f"{'Record Usage' if mode == 'usage' else 'Record Receiving'}: {part['name']}", parent,
                         "Deduct Stock" if mode == "usage" else "Add Stock")
        lab = QLabel(f"On hand: {part.get('currentStock')} {part.get('unit') or ''}")
        lab.setObjectName("muted")
        self.lay.addWidget(lab)
        f = QFormLayout()
        self.qty = _qty(1, 1)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Job #, PO #, … (optional)")
        f.addRow("Quantity", self.qty)
        f.addRow("Note", self.note)
        self.lay.addLayout(f)
        self.finish()


class ProjectDialog(_Base):
    def __init__(self, proj=None, parent=None):
        super().__init__("Edit Project" if proj else "Add Project", parent, "Save Changes" if proj else "Add Project")
        p = proj or {}
        f = QFormLayout()
        self.name = QLineEdit(p.get("name", ""))
        self.name.setPlaceholderText("e.g. Line 3 Retrofit")
        self.qty = QSpinBox()
        self.qty.setRange(0, 1_000_000)
        self.qty.setSpecialValueText("— (1)")
        self.qty.setValue(int(M.num(p.get("totalQtyNeeded"), 0)) if p.get("totalQtyNeeded") not in (None, "") else 0)
        self.folder = QLineEdit(p.get("folderPath", ""))
        self.folder.setPlaceholderText("Network path or URL")
        f.addRow("Project name", self.name)
        f.addRow("Total qty needed (units to build)", self.qty)
        f.addRow("Folder location", self.folder)
        self.lay.addLayout(f)
        self.finish()

    def validate(self):
        if not self.name.text().strip():
            raise ValueError("Enter a project name")

    def values(self):
        return {"name": self.name.text().strip(), "totalQtyNeeded": self.qty.value() if self.qty.value() else "",
                "folderPath": self.folder.text().strip()}


class LocationDialog(_Base):
    def __init__(self, loc=None, parent=None):
        super().__init__("Edit Location" if loc else "Add Location", parent, "Save Changes" if loc else "Add Location")
        l = loc or {"rack": M.RACKS[0], "shelf": 1, "binSize": "small", "binPosition": 1}
        f = QFormLayout()
        self.rack = QComboBox()
        self.rack.setEditable(True)
        self.rack.addItems(M.RACKS)
        self.rack.setCurrentText(l.get("rack") or M.RACKS[0])
        self.shelf = QComboBox()
        for s in M.SHELVES:
            self.shelf.addItem(f"Shelf {s}", s)
        self.shelf.setCurrentIndex(max(0, self.shelf.findData(int(M.num(l.get("shelf"), 1)))))
        self.size = QComboBox()
        for b in M.BIN_SIZES:
            self.size.addItem(b["label"], b["id"])
        self.size.setCurrentIndex(max(0, self.size.findData(l.get("binSize"))))
        self.pos = QComboBox()
        self._positions(int(M.num(l.get("binPosition"), 1)))
        self.size.currentIndexChanged.connect(lambda _i: self._positions(self.pos.currentData()))
        nm = QHBoxLayout()
        self.name = QLineEdit(l.get("name") or "")
        self.name.setPlaceholderText("Custom name, or generate a serial")
        gen = QPushButton("Generate")
        gen.clicked.connect(lambda: self.name.setText(M.generate_bin_serial(self.rack.currentText(),
                                                                            self.shelf.currentData(),
                                                                            self.pos.currentData())))
        nm.addWidget(self.name)
        nm.addWidget(gen)
        self.sections = QCheckBox("Split into 3 sections (.a / .b / .c), each its own location")
        self.sections.setChecked(bool(l.get("sections")))
        for lab, w in (("Rack", self.rack), ("Shelf", self.shelf), ("Bin size", self.size), ("Bin position", self.pos)):
            f.addRow(lab, w)
        f.addRow("Bin name", nm)
        f.addRow(self.sections)
        self.lay.addLayout(f)
        self.finish()

    def _positions(self, keep=None):
        self.pos.clear()
        for n in M.bin_positions(self.size.currentData()):
            self.pos.addItem(f"Bin {n}", n)
        i = self.pos.findData(keep)
        self.pos.setCurrentIndex(max(0, i))

    def values(self):
        return {"rack": self.rack.currentText().strip(), "shelf": self.shelf.currentData(),
                "binSize": self.size.currentData(), "binPosition": self.pos.currentData(),
                "name": self.name.text().strip(), "sections": self.sections.isChecked()}


class VendorDialog(_Base):
    def __init__(self, v=None, parent=None):
        super().__init__("Edit Vendor" if v else "Add Vendor", parent, "Save Changes" if v else "Add Vendor")
        r = v or {}
        f = QFormLayout()
        self.name = QLineEdit(r.get("name", ""))
        self.cat = QComboBox()
        self.cat.addItems(M.VENDOR_CATEGORIES)
        cat = r.get("category") or "Other"
        if self.cat.findText(cat) < 0:
            self.cat.addItem(cat)
        self.cat.setCurrentText(cat)
        self.contact = QLineEdit(r.get("contactName") or "")
        self.phone = QLineEdit(r.get("phone") or "")
        self.email = QLineEdit(r.get("email") or "")
        self.web = QLineEdit(r.get("website") or "")
        self.web.setPlaceholderText("https://")
        self.lo = QSpinBox()
        self.hi = QSpinBox()
        for s, val in ((self.lo, r.get("leadTimeMinDays")), (self.hi, r.get("leadTimeMaxDays"))):
            s.setRange(-1, 3650)
            s.setSpecialValueText("—")
            s.setValue(int(M.num(val, -1)) if val not in (None, "") else -1)
        self.notes = QPlainTextEdit(r.get("notes") or "")
        self.notes.setFixedHeight(60)
        for lab, w in (("Vendor name", self.name), ("Category", self.cat), ("Contact name", self.contact),
                       ("Phone", self.phone), ("Email", self.email), ("Website", self.web),
                       ("Lead time min (days)", self.lo), ("Lead time max (days)", self.hi), ("Notes", self.notes)):
            f.addRow(lab, w)
        self.lay.addLayout(f)
        self.finish()

    def validate(self):
        if not self.name.text().strip():
            raise ValueError("Enter a vendor name")

    def values(self):
        return {"name": self.name.text().strip(), "category": self.cat.currentText(),
                "contactName": self.contact.text().strip(), "phone": self.phone.text().strip(),
                "email": self.email.text().strip(), "website": self.web.text().strip(),
                "leadTimeMinDays": self.lo.value() if self.lo.value() >= 0 else None,
                "leadTimeMaxDays": self.hi.value() if self.hi.value() >= 0 else None,
                "notes": self.notes.toPlainText().strip()}


class OrderDialog(_Base):
    """Production order: project, qty, dates, fab/finishing windows, status."""

    def __init__(self, inv: M.Inventory, o=None, parent=None):
        super().__init__("Edit Production Order" if o else "Add Production Order", parent,
                         "Save Changes" if o else "Add Order")
        r = o or {}
        f = QFormLayout()
        self.project = QComboBox()
        for pr in inv.active_projects():
            self.project.addItem(pr["name"], pr["id"])
        if r.get("projectId"):
            i = self.project.findData(r["projectId"])
            if i < 0:
                self.project.addItem(inv.project_name(r["projectId"]) or "(deleted project)", r["projectId"])
                i = self.project.count() - 1
            self.project.setCurrentIndex(i)
        self.qty = _qty(r.get("qty", 1), 1)
        self.d = {k: _date_edit(r.get(k)) for k in ("orderDate", "dueDate", "fabStart", "fabEnd", "finishStart",
                                                     "finishEnd")}
        if not o:
            self.d["orderDate"].setDate(QDate.currentDate())
        self.status = QComboBox()
        self.status.addItems(M.ORDER_STATUSES)
        self.status.setCurrentText(r.get("status") or "Not Started")
        f.addRow("Project", self.project)
        f.addRow("Qty", self.qty)
        for k, lab in (("orderDate", "Order date"), ("dueDate", "Due date"), ("fabStart", "Fab start"),
                       ("fabEnd", "Fab end"), ("finishStart", "Finishing start"), ("finishEnd", "Finishing end")):
            f.addRow(lab, self.d[k])
        f.addRow("Status", self.status)
        self.lay.addLayout(f)
        self.finish()

    def validate(self):
        if self.project.currentData() is None:
            raise ValueError("Add a project first")

    def values(self):
        return {"projectId": self.project.currentData(), "qty": self.qty.value(),
                **{k: _date_value(w) for k, w in self.d.items()}, "status": self.status.currentText()}


class ClientOrderDialog(_Base):
    def __init__(self, inv: M.Inventory, o=None, parent=None):
        super().__init__("Edit Client Order" if o else "Add Client Order", parent,
                         "Save Changes" if o else "Add Client Order")
        self.inv = inv
        r = o or {}
        c = inv.client(r.get("clientId")) or {}
        f = QFormLayout()
        self.client = QComboBox()
        self.client.setEditable(True)
        self.client.addItems([x["name"] for x in sorted(inv.clients, key=lambda x: x["name"].lower())])
        self.client.setCurrentText(c.get("name", ""))
        self.contact = QLineEdit(c.get("contactName") or "")
        self.phone = QLineEdit(c.get("phone") or "")
        self.email = QLineEdit(c.get("email") or "")
        self.client.currentTextChanged.connect(self._fill_client)
        self.lines = QTableWidget(0, 2)
        self.lines.setHorizontalHeaderLabels(["Project", "Qty"])
        self.lines.setColumnWidth(0, 280)
        self.lines.verticalHeader().hide()
        self.lines.setMaximumHeight(140)
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
        add.addWidget(self.add_proj, 2)
        add.addWidget(self.add_qty)
        add.addWidget(ab)
        add.addWidget(rb)
        self.od = _date_edit(r.get("orderDate") or (M.today() if not o else ""))
        self.dd = _date_edit(r.get("dueDate"))
        self.status = QComboBox()
        self.status.addItems(M.CLIENT_ORDER_STATUSES)
        self.status.setCurrentText(r.get("status") or "Requested")
        self.notes = QPlainTextEdit(r.get("notes") or "")
        self.notes.setFixedHeight(54)
        f.addRow("Client", self.client)
        f.addRow("Contact name", self.contact)
        f.addRow("Phone", self.phone)
        f.addRow("Email", self.email)
        f.addRow("Projects", self.lines)
        f.addRow("", add)
        f.addRow("Order date", self.od)
        f.addRow("Due date", self.dd)
        f.addRow("Status", self.status)
        f.addRow("Notes", self.notes)
        self.lay.addLayout(f)
        self.finish()

    def _fill_client(self, name):
        c = next((x for x in self.inv.clients if x["name"].lower() == name.strip().lower()), None)
        if c:
            self.contact.setText(c.get("contactName") or "")
            self.phone.setText(c.get("phone") or "")
            self.email.setText(c.get("email") or "")

    def _line(self, pid, qty):
        r = self.lines.rowCount()
        self.lines.insertRow(r)
        it = QTableWidgetItem(self.inv.project_name(pid) or "(deleted project)")
        it.setData(Qt.UserRole, pid)
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.lines.setItem(r, 0, it)
        self.lines.setCellWidget(r, 1, _qty(qty, 1))

    def validate(self):
        if not self.client.currentText().strip():
            raise ValueError("Enter a client name")

    def values(self):
        c = self.inv.find_or_create_client(self.client.currentText(), self.contact.text().strip(),
                                           self.phone.text().strip(), self.email.text().strip())
        lines = [{"projectId": self.lines.item(r, 0).data(Qt.UserRole), "qty": self.lines.cellWidget(r, 1).value()}
                 for r in range(self.lines.rowCount())]
        return {"clientId": c["id"], "projectLines": lines, "orderDate": _date_value(self.od),
                "dueDate": _date_value(self.dd), "status": self.status.currentText(),
                "notes": self.notes.toPlainText().strip()}


class PartOrderDialog(_Base):
    def __init__(self, inv: M.Inventory, part, qty, parent=None):
        super().__init__(f"Order: {part['name']}", parent, "Place Order")
        best = inv.best_supplier(part)
        lead = inv.lead_time(best["name"]) if best else None
        f = QFormLayout()
        self.qty = _qty(qty, 1)
        self.od = _date_edit(M.today(), allow_empty=False)
        self.ed = _date_edit(M.add_days(M.today(), lead) if lead is not None else "")
        f.addRow("Qty", self.qty)
        f.addRow("Order date", self.od)
        f.addRow("Expected date", self.ed)
        if best:
            note = QLabel(f"Best supplier: {best['name']} {M.fmt_money(best.get('price'))} {best.get('currency') or 'CAD'}"
                          + (f" · lead time {inv.lead_label(inv.lead_time_range(best['name']))}" if lead is not None
                             else ""))
            note.setObjectName("muted")
            f.addRow(note)
        self.lay.addLayout(f)
        self.finish()

    def values(self):
        return self.qty.value(), _date_value(self.od), _date_value(self.ed)


class BatchDialog(_Base):
    """Batch receive or batch order for selected reorder parts."""

    def __init__(self, inv: M.Inventory, parts, mode, parent=None):
        super().__init__(f"Batch {'Receive' if mode == 'receive' else 'Order'} ({len(parts)} parts)", parent,
                         "Receive All" if mode == "receive" else "Place Orders")
        self.mode = mode
        self.t = QTableWidget(len(parts), 2)
        self.t.setHorizontalHeaderLabels(["Part", "Qty"])
        self.t.verticalHeader().hide()
        self.t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.t.setMinimumHeight(min(420, 40 + 34 * max(len(parts), 2)))
        self.setMinimumWidth(520)
        lead = None
        for i, p in enumerate(parts):
            it = QTableWidgetItem(p["name"])
            it.setData(Qt.UserRole, p["id"])
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.t.setItem(i, 0, it)
            sq = inv.suggested_qty(p, inv.total_needed(p))
            self.t.setCellWidget(i, 1, _qty(sq, 0 if mode == "receive" else 1))
            b = inv.best_supplier(p)
            lt = inv.lead_time(b["name"]) if b else None
            if lt is not None:
                lead = max(lead or 0, lt)
        self.lay.addWidget(self.t)
        f = QFormLayout()
        if mode == "receive":
            self.note = QLineEdit()
            self.note.setPlaceholderText("Job #, PO #, … (optional)")
            f.addRow("Note", self.note)
        else:
            self.od = _date_edit(M.today(), allow_empty=False)
            self.ed = _date_edit(M.add_days(M.today(), lead) if lead is not None else "")
            f.addRow("Order date", self.od)
            f.addRow("Expected date", self.ed)
        self.lay.addLayout(f)
        self.finish()

    def items(self):
        return [(self.t.item(r, 0).data(Qt.UserRole), self.t.cellWidget(r, 1).value()) for r in range(self.t.rowCount())]


class ChooseProjectDialog(_Base):
    """Bulk-assign selected parts to a project with a qty per unit."""

    def __init__(self, inv: M.Inventory, count, parent=None):
        super().__init__(f"Assign {count} part{'s' if count != 1 else ''} to a project", parent, "Assign")
        f = QFormLayout()
        self.project = QComboBox()
        for pr in inv.active_projects():
            self.project.addItem(pr["name"], pr["id"])
        self.qty = _qty(1, 0)
        f.addRow("Project", self.project)
        f.addRow("Qty per unit", self.qty)
        self.lay.addLayout(f)
        self.finish()


class AddPartsToProjectDialog(_Base):
    def __init__(self, inv: M.Inventory, pid, parent=None):
        super().__init__(f"Manage parts for {inv.project_name(pid)}", parent, "Save")
        self.resize(560, 520)
        note = QLabel("Qty is per unit of the project — the total scales with the project's Total Qty Needed.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.lay.addWidget(note)
        self.all = QCheckBox("Select all parts")
        self.lay.addWidget(self.all)
        comp = inv.component_ids()
        eligible = sorted([p for p in inv.parts if p["id"] not in comp], key=lambda p: p["name"].lower())
        self.t = QTableWidget(len(eligible), 2)
        self.t.setHorizontalHeaderLabels(["Part", "Qty/unit"])
        self.t.verticalHeader().hide()
        self.t.setColumnWidth(0, 380)
        for i, p in enumerate(eligible):
            a = inv.allocation(p, pid)
            it = QTableWidgetItem(p["name"])
            it.setData(Qt.UserRole, p["id"])
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if a else Qt.Unchecked)
            self.t.setItem(i, 0, it)
            self.t.setCellWidget(i, 1, _qty(a.get("qty") if a else 1, 0))
        self.all.toggled.connect(lambda on: [self.t.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
                                             for r in range(self.t.rowCount())])
        self.lay.addWidget(self.t)
        if not eligible:
            self.lay.addWidget(QLabel("No eligible parts — add one from the Parts tab first."))
        self.finish()

    def items(self):
        return [(self.t.item(r, 0).data(Qt.UserRole), self.t.item(r, 0).checkState() == Qt.Checked,
                 self.t.cellWidget(r, 1).value()) for r in range(self.t.rowCount())]


class DuplicatesDialog(QDialog):
    """Possible duplicate groups (same name or part number) with Merge buttons."""

    def __init__(self, inv: M.Inventory, on_merge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Possible duplicate parts")
        self.inv, self.on_merge = inv, on_merge
        self.resize(640, 460)
        self.lay = QVBoxLayout(self)
        self.list = QVBoxLayout()
        w = QWidget()
        w.setLayout(self.list)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(w)
        self.lay.addWidget(sc)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        self.lay.addWidget(close)
        self._fill()

    def _fill(self):
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        groups = self.inv.duplicate_groups()
        if not groups:
            self.list.addWidget(QLabel("No duplicates left."))
        for g in groups:
            box = QGroupBox(f"{len(g)} matching parts")
            v = QVBoxLayout(box)
            for p in g:
                h = QHBoxLayout()
                h.addWidget(QLabel(f"{p['name']}  ·  {p.get('partNumber') or '—'}  ·  stock {p.get('currentStock')}"
                                   f"  ·  {M.TYPE_LABEL.get(p.get('type'), '')}"), 1)
                b = QPushButton("Keep this, merge others")
                b.clicked.connect(lambda _=False, keep=p, grp=g: self._merge(keep, grp))
                h.addWidget(b)
                v.addLayout(h)
            self.list.addWidget(box)
        self.list.addStretch(1)

    def _merge(self, keep, group):
        for p in group:
            if p["id"] != keep["id"]:
                self.inv.merge_parts(keep["id"], p["id"])
        self.on_merge(f"Merged into {keep['name']}")
        self._fill()


class HistoryDialog(QDialog):
    def __init__(self, inv: M.Inventory, pid, parent=None):
        super().__init__(parent)
        p = inv.part(pid)
        self.setWindowTitle(f"History: {p['name'] if p else '(deleted part)'}")
        self.resize(620, 420)
        lay = QVBoxLayout(self)
        tx = [t for t in reversed(inv.transactions) if t.get("partId") == pid]
        t = QTableWidget(len(tx), 5)
        t.setHorizontalHeaderLabels(["When", "Action", "Qty", "By", "Note"])
        t.verticalHeader().hide()
        for i, x in enumerate(tx):
            vals = [dt.datetime.fromtimestamp(x["ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
                    {"usage": "Used", "receiving": "Received"}.get(x.get("mode"), "Adjusted"),
                    f"{'-' if x.get('mode') == 'usage' else '+'}{x.get('qty')}", x.get("by") or "", x.get("note") or ""]
            for j, v in enumerate(vals):
                t.setItem(i, j, QTableWidgetItem(str(v)))
        t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(t if tx else QLabel("No activity for this part yet."))
        ords = [o for o in inv.partOrders if o.get("partId") == pid]
        if ords:
            lay.addWidget(QLabel("Part orders: " + "; ".join(f"{o.get('qty')} on {M.fmt_date(o.get('orderDate'))} "
                                                             f"({o.get('status')})" for o in ords)))
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        lay.addWidget(b)


class SendBomDialog(_Base):
    """Daedalus → inventory: project name and units to build."""

    def __init__(self, inv: M.Inventory, default_name, parent=None):
        super().__init__("Send BOM to Inventory", parent, "Send")
        f = QFormLayout()
        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.addItems([p["name"] for p in inv.active_projects()])
        self.name.setCurrentText(default_name)
        self.units = _qty(1, 1)
        ex = next((p for p in inv.projects if p["name"].lower() == default_name.lower()), None)
        if ex and ex.get("totalQtyNeeded") not in (None, ""):
            self.units.setValue(int(M.num(ex["totalQtyNeeded"], 1)))
        self.weld = QCheckBox("Frames become weldments with their cut-list members")
        self.weld.setChecked(True)
        f.addRow("Project", self.name)
        f.addRow("Units to build", self.units)
        f.addRow(self.weld)
        note = QLabel("New parts are added with 0 in stock; parts already in the inventory (same part number, "
                      "fastener spec or name) are linked. The quantities per assembly go on the project.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        f.addRow(note)
        self.lay.addLayout(f)
        self.finish()

    def validate(self):
        if not self.name.currentText().strip():
            raise ValueError("Enter a project name")


GUIDE = """<h3>Holocron inventory in Daedalus</h3>
<p><b>Parts</b> — fasteners, purchased and manufactured parts with stock, reorder point/qty, unit cost (CAD/USD),
up to three suppliers, bin location, drawing link. Fastener names fill in from the spec. Manufactured parts can be
weldments made of other parts. <b>Use</b> deducts stock, <b>Receive</b> adds it — every move is logged, and the
toast offers Undo.</p>
<p><b>Projects</b> — set <i>Total Qty Needed</i> (units to build); each part's qty is per unit, so the totals scale.
<b>Send BOM</b> (Inventory tab in the toolbelt, or here) turns the open design into a project: library fasteners
and bearings are matched by spec, modelled parts by part number.</p>
<p><b>Reorder</b> lists parts at or below their reorder point with a suggested qty, best supplier and cost; place
part orders (expected date from the vendor's lead time), batch order / receive, and export the list to Excel.
<b>Shortages</b> adds up demand across all projects. <b>Production</b> schedules builds with fab/finishing
timelines; <b>Vendors</b> keeps contacts, lead times and an on-time scorecard; <b>Clients</b> tracks orders.</p>
<p><b>Data</b> lives in one file (Import / Export tab shows where). Put it on a shared drive and point every
Daedalus at it — changes from other PCs appear within seconds. Import/export the master Excel workbook any time.</p>"""
