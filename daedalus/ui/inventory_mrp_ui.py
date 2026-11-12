"""DIMS MRP-lite screens: Engineering (part numbers, BOM workbench, where used, job orders, material needs), the
Time-Phased tab (incoming, projected stock, order log), the part profile, vendor / price import."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..inventory import model as M
from ..inventory import mrp as R
from . import theme
from .inventory_extras_ui import _h, _hrow, _muted, _save_xlsx, _table


def _btn(text, fn, tip="", primary=False):
    b = QPushButton(text)
    b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    if primary:
        b.setObjectName("primary")
    return b


def part_label(p) -> str:
    return (f"{p['partNumber']} — " if p.get("partNumber") else "") + p["name"]


class PartPicker(QComboBox):
    """Type a part number or name; completes against the inventory."""

    def __init__(self, inv, filt=None, parent=None, blank="— pick a part —"):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItem(blank, None)
        for p in sorted(inv.parts, key=lambda p: ((p.get("partNumber") or "~").upper(), p["name"].lower())):
            if filt is None or filt(p):
                self.addItem(part_label(p), p["id"])
        c = QCompleter([self.itemText(i) for i in range(1, self.count())], self)
        c.setCaseSensitivity(Qt.CaseInsensitive)
        c.setFilterMode(Qt.MatchContains)
        self.setCompleter(c)
        self.setMinimumWidth(320)

    def part_id(self):
        i = self.findText(self.currentText())
        return self.itemData(i) if i >= 0 else None

    def set_part(self, pid):
        i = self.findData(pid)
        self.setCurrentIndex(max(0, i))


# ================================================================== engineering
class EngineeringView(QWidget):
    edit_part = Signal(str)
    new_part = Signal(dict)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.inv = page.inv
        self.st = page.mrp_state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for name, fn in (("Part Numbers", self.numbers), ("BOM Workbench", self.bom), ("Where Used", self.used),
                         ("Job Order && Kit", self.job), ("Material Needs", self.materials)):
            self.tabs.addTab(fn(), name)
        self.tabs.setCurrentIndex(self.st.get("eng_tab", 0))
        self.tabs.currentChanged.connect(lambda i: self.st.__setitem__("eng_tab", i))
        v.addWidget(self.tabs)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        return w, lay

    def _commit(self, msg):
        self.page.commit(msg)

    # ------------------------------------------------------------ part numbers
    def numbers(self):
        w, v = self._page()
        inv = self.inv
        pre = QComboBox()
        pre.setEditable(True)
        for p in R.all_prefixes(inv):
            lab = R.describe_prefix(inv, p)["label"]
            pre.addItem(p + (f" — {lab}" if lab else ""), p)
        pre.setCurrentText(self.st.get("pn_prefix", ""))
        pre.lineEdit().setPlaceholderText("Prefix, e.g. M10")
        count = QSpinBox()
        count.setRange(1, 200)
        count.setPrefix("× ")
        proj = QComboBox()
        proj.addItem("No project", None)
        for pr in inv.active_projects():
            proj.addItem(pr["name"], pr["id"])
        note = QLineEdit()
        note.setPlaceholderText("Note (what it's for)")
        nxt = QLabel()
        nxt.setStyleSheet(f"color:{theme.ACCENT_TEXT}; font-weight:600")

        def prefix():
            i = pre.findText(pre.currentText())
            return pre.itemData(i) if i >= 0 and pre.itemData(i) else pre.currentText().split(" — ")[0]

        def preview(*_a):
            try:
                ns = R.next_numbers(inv, prefix(), count.value())
                nxt.setText("Next: " + (ns[0] if len(ns) == 1 else f"{ns[0]} … {ns[-1]}"))
            except ValueError:
                nxt.setText("")
        pre.currentTextChanged.connect(preview)
        count.valueChanged.connect(preview)
        preview()

        def do_reserve():
            try:
                got = self.reserve(prefix(), count.value(), proj.currentData(), note.text().strip())
            except Exception as e:  # noqa: BLE001
                return QMessageBox.warning(self, "Part numbers", str(e))
            self.st["pn_prefix"] = prefix()
            self.page.toast.show_msg("Reserved " + ", ".join(r["id"] for r in got[:6]) + (" …" if len(got) > 6 else ""))
            self.page.refresh_current()
        res = _btn("Reserve", do_reserve, "Hold these numbers (for a project, a model or a drawing)", primary=True)
        v.addLayout(_hrow(_muted("Numbers are PREFIX-####, counting up from the highest in use for that prefix "
                                 "(released numbers aren't reused). On the team server the database hands them out, "
                                 "so two people never get the same one."), _btn("Prefixes…", self.edit_prefixes)))
        row = QHBoxLayout()
        for x in (QLabel("Prefix"), pre, count, proj, note):
            row.addWidget(x)
        row.addWidget(nxt, 1)
        row.addWidget(res)
        v.addLayout(row)
        # registry
        fpre = QComboBox()
        fpre.addItem("All prefixes", None)
        for p in R.all_prefixes(inv):
            fpre.addItem(p, p)
        fstat = QComboBox()
        for s in ("All", "reserved", "assigned", "released", "in use"):
            fstat.addItem(s.title() if s != "All" else "All statuses", None if s == "All" else s)
        q = QLineEdit(self.st.get("pn_q", ""))
        q.setPlaceholderText("Search number, part, who…")
        f = self.st.setdefault("pn_filter", {"prefix": None, "status": None})
        fpre.setCurrentIndex(max(0, fpre.findData(f["prefix"])))
        fstat.setCurrentIndex(max(0, fstat.findData(f["status"])))
        exp = _btn("Excel…", lambda: _save_xlsx(self, "DIMS-part-numbers.xlsx", [("Part numbers", rows)]))
        v.addLayout(_hrow(q, fpre, fstat, exp))
        rows = R.registry_rows(inv, f["prefix"], f["status"], self.st.get("pn_q", ""))
        t = _table(["Number", "Status", "Part", "Description", "Project", "Created By", "Created", ""],
                   [{**r, "": ""} for r in rows], 2)
        col = {"reserved": theme.WARN, "assigned": theme.OK, "released": theme.MUTED}
        for i, r in enumerate(rows):
            t.item(i, 1).setForeground(QColor(col.get(r["Status"], theme.INFO)))
            box = QWidget()
            hb = QHBoxLayout(box)
            hb.setContentsMargins(0, 0, 0, 0)
            if r["Status"] == "reserved":
                hb.addWidget(_btn("Create profile", lambda _=False, n=r["Number"]: self.create_profile(n)))
                hb.addWidget(_btn("Assign…", lambda _=False, n=r["Number"]: self.assign(n)))
                hb.addWidget(_btn("Release", lambda _=False, n=r["Number"]: self.release(n)))
            elif r.get("partId"):
                hb.addWidget(_btn("Profile", lambda _=False, i_=r["partId"]: PartProfileDialog(self.page, i_).exec()))
            t.setCellWidget(i, 7, box)
        t.resizeColumnsToContents()
        v.addWidget(t, 1)

        def refilter(*_a):
            f.update(prefix=fpre.currentData(), status=fstat.currentData())
            self.st["pn_q"] = q.text()
            self.page.refresh_current()
        fpre.currentIndexChanged.connect(refilter)
        fstat.currentIndexChanged.connect(refilter)
        q.returnPressed.connect(refilter)
        return w

    def reserve(self, prefix, n, project_id, note):
        st = self.page.store
        if hasattr(st, "reserve_part_numbers"):
            got = st.reserve_part_numbers(prefix, n, project_id, note)
            self.page.refresh()
            return got
        got = R.reserve(self.inv, prefix, n, project_id, note)
        self._commit(None)
        return got

    def create_profile(self, number):
        pre, _s = R.split_number(number)
        d = R.describe_prefix(self.inv, pre)
        self.new_part.emit({"partNumber": number, "type": d["type"] or "manufactured",
                            **({"isAssembly": True} if d["bom"] == "assembly" else
                               {"isWeldment": True} if d["bom"] == "weldment" else {})})

    def assign(self, number):
        d = QDialog(self)
        d.setWindowTitle(f"Assign {number}")
        lay = QVBoxLayout(d)
        pk = PartPicker(self.inv, lambda p: not p.get("partNumber"), d, "— a part without a number —")
        lay.addWidget(QLabel(f"Give {number} to:"))
        lay.addWidget(pk)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        if d.exec() and pk.part_id():
            try:
                R.assign(self.inv, number, self.inv.part(pk.part_id()))
            except ValueError as e:
                return QMessageBox.warning(self, "Assign", str(e))
            self._commit(f"{number} → {self.inv.part(pk.part_id())['name']}")

    def release(self, number):
        if QMessageBox.question(self, "Release", f"Release {number}? It won't be handed out again.") != QMessageBox.Yes:
            return
        try:
            R.release(self.inv, number)
        except ValueError as e:
            return QMessageBox.warning(self, "Release", str(e))
        self._commit(f"{number} released")

    def edit_prefixes(self):
        if PrefixDialog(self.inv, self).exec():
            self._commit("Prefixes saved")

    # ------------------------------------------------------------ BOM workbench
    def bom(self):
        w, v = self._page()
        inv = self.inv
        pk = PartPicker(inv, lambda p: p.get("type") == "manufactured" or R.has_bom(p), w, "— pick an assembly —")
        pk.set_part(self.st.get("bom_part"))
        pk.activated.connect(lambda _i: (self.st.__setitem__("bom_part", pk.part_id()), self.page.refresh_current()))
        levels = QComboBox()
        levels.addItems(["1 level", "All levels"])
        levels.setCurrentIndex(self.st.get("bom_levels", 0))
        levels.currentIndexChanged.connect(lambda i: (self.st.__setitem__("bom_levels", i), self.page.refresh_current()))
        v.addLayout(_hrow(_muted("Pick an assembly or fabricated part and build its bill of materials from parts that "
                                 "have profiles. Weldment = parts welded in (not stocked separately); assembly = a kit "
                                 "of stocked parts."), QLabel("Assembly"), pk, levels))
        p = inv.part(self.st.get("bom_part")) if self.st.get("bom_part") else None
        if p is None:
            v.addWidget(_muted("No assembly picked."))
            v.addStretch(1)
            return w
        kind = QComboBox()
        for k, lab in (("assembly", "Assembly (kit of stocked parts)"), ("weldment", "Weldment (welded in)")):
            kind.addItem(lab, k)
        kind.setCurrentIndex(max(0, kind.findData(R.bom_kind(p) or "assembly")))
        kind.activated.connect(lambda _i: (R.set_bom(inv, p["id"], [(c["partId"], c["qty"]) for c in
                                                                   p.get("components") or []], kind.currentData()),
                                           self._commit("BOM type changed")))
        add = PartPicker(inv, lambda x: x["id"] != p["id"] and not inv.contains_recursive(x["id"], p["id"]), w,
                         "— add a line —")
        qty = QDoubleSpinBox()
        qty.setRange(0.001, 100000)
        qty.setValue(1)
        qty.setDecimals(3)

        def add_line():
            cid = add.part_id()
            if not cid:
                return
            lines = [(c["partId"], c["qty"]) for c in p.get("components") or []]
            hit = next((i for i, (x, _q) in enumerate(lines) if x == cid), None)
            if hit is not None:
                lines[hit] = (cid, lines[hit][1] + qty.value())
            else:
                lines.append((cid, qty.value()))
            warn = R.level_warning(p.get("partNumber"), (inv.part(cid) or {}).get("partNumber"))
            if warn and QMessageBox.question(self, "BOM", warn + "\nAdd it anyway?") != QMessageBox.Yes:
                return
            try:
                R.set_bom(inv, p["id"], lines, kind.currentData())
            except ValueError as e:
                return QMessageBox.warning(self, "BOM", str(e))
            self._commit(f"Added to {p['name']}")
        cost = R.bom_cost(inv, p["id"])
        head = QLabel(f"<b>{part_label(p)}</b>  ·  {M.TYPE_LABEL.get(p.get('type'), '')}"
                      + (f"  ·  Rev {p['revision']}" if p.get("revision") else "")
                      + f"  ·  rolled-up cost <b>{M.fmt_money(cost)}</b>  ·  on hand {M.num(p.get('currentStock')):g}")
        v.addLayout(_hrow(head, kind, _btn("Profile", lambda: PartProfileDialog(self.page, p["id"]).exec()),
                          _btn("Edit part", lambda: self.edit_part.emit(p["id"]))))
        v.addLayout(_hrow(add, qty, _btn("+ Add line", add_line, primary=True),
                          _btn("New part…", lambda: self.new_part.emit({"_bom_parent": p["id"]}),
                               "Create a new part profile and add it to this BOM")))
        rows = R.bom_rows(inv, p["id"], 0 if self.st.get("bom_levels") else 1)
        hdr = ["Level", "Part Number", "Part", "Type", "Qty / Assy", "Qty Total", "On Hand", "Unit Cost", "Vendor",
               "Bin", "Kind", ""]
        t = _table(hdr, [{**r, "": ""} for r in rows], 2, money=("Unit Cost",), sortable=False)
        for i, r in enumerate(rows):
            if r["Level"] == 1:
                sp = QDoubleSpinBox()
                sp.setRange(0, 100000)
                sp.setDecimals(3)
                sp.setValue(r["Qty / Assy"])
                sp.editingFinished.connect(lambda sp=sp, cid=r["id"]: self._set_qty(p, cid, sp.value(), kind.currentData()))
                t.setCellWidget(i, 4, sp)
                rm = _btn("Remove", lambda _=False, cid=r["id"]: self._set_qty(p, cid, 0, kind.currentData()),
                          "Remove this line")
                t.setCellWidget(i, 11, rm)
            else:
                t.item(i, 2).setText("    " * (r["Level"] - 1) + r["Part"])
            t.item(i, 2).setData(Qt.UserRole + 1, r["id"])
        t.horizontalHeader().setSectionResizeMode(11, QHeaderView.Fixed)
        t.setColumnWidth(11, 84)
        t.cellDoubleClicked.connect(lambda r_, _c: (lambda pid: pid and PartProfileDialog(self.page, pid).exec())(
            t.item(r_, 2).data(Qt.UserRole + 1)))
        v.addWidget(t, 1)
        mats = [f"{(x.get('material') or {}).get('shape')} {(x.get('material') or {}).get('size') or ''}"
                for x in [inv.part(r["id"]) for r in rows] if x and (x.get("material") or {}).get("shape")]
        v.addWidget(_muted(f"{len(rows)} line(s) · raw material on {len(mats)} part(s). Double-click a line for its "
                           "profile; set material and finishing in Edit part."))
        return w

    def _set_qty(self, p, cid, q, kind):
        lines = [(c["partId"], (q if c["partId"] == cid else c["qty"])) for c in p.get("components") or []]
        lines = [(c, x) for c, x in lines if x > 0]
        R.set_bom(self.inv, p["id"], lines, kind)
        if not lines:
            p.pop("isAssembly", None)
        self._commit("BOM updated")

    # ------------------------------------------------------------ where used
    def used(self):
        w, v = self._page()
        inv = self.inv
        pk = PartPicker(inv, None, w)
        pk.set_part(self.st.get("wu_part"))
        pk.activated.connect(lambda _i: (self.st.__setitem__("wu_part", pk.part_id()), self.page.refresh_current()))
        v.addLayout(_hrow(_muted("Where a part belongs: the assemblies it goes into and the projects that need it. "
                                 "Shared = used by more than one assembly or project."), pk))
        pid = self.st.get("wu_part")
        if pid and inv.part(pid):
            wu = R.where_used(inv, pid)
            p = inv.part(pid)
            v.addWidget(QLabel(f"<b>{part_label(p)}</b> — " + (f"<span style='color:{theme.WARN}'>shared</span>"
                                                              if wu["shared"] else "unique to one use")))
            g = QHBoxLayout()
            for title, rows, hdr in (("Goes into", wu["parents"], ["Part", "Part Number", "Qty", "Kind"]),
                                     ("Top assemblies", wu["top_assemblies"], ["Assembly", "Qty per"]),
                                     ("Projects", wu["projects"], ["Project", "Qty per unit"])):
                c = QVBoxLayout()
                c.addWidget(_h(title))
                c.addWidget(_table(hdr, rows, 0) if rows else _muted("—"))
                g.addLayout(c)
            v.addLayout(g)
        v.addWidget(_h("All shared parts"))
        sh = R.shared_parts(inv)
        t = _table(["Part", "Part Number", "Used In"], sh, 2)
        for i, r in enumerate(sh):
            t.item(i, 0).setData(Qt.UserRole + 1, r["id"])
        t.cellDoubleClicked.connect(lambda r_, _c: (self.st.__setitem__("wu_part", t.item(r_, 0).data(Qt.UserRole + 1)),
                                                    self.page.refresh_current()))
        v.addWidget(t if sh else _muted("No part is used in more than one place yet."), 1)
        return w

    # ------------------------------------------------------------ job order
    def job(self):
        w, v = self._page()
        inv = self.inv
        pk = PartPicker(inv, None, w, "— what to build —")
        pk.set_part(self.st.get("job_part"))
        qty = QDoubleSpinBox()
        qty.setRange(0.001, 100000)
        qty.setDecimals(0)
        qty.setValue(self.st.get("job_qty", 1))
        net = QCheckBox("Use stock on hand")
        net.setChecked(self.st.get("job_net", True))
        proj = QComboBox()
        proj.addItem("For stock", None)
        for pr in inv.active_projects():
            proj.addItem(pr["name"], pr["id"])
        proj.setCurrentIndex(max(0, proj.findData(self.st.get("job_project"))))

        def go():
            self.st.update(job_part=pk.part_id(), job_qty=qty.value(), job_net=net.isChecked(),
                           job_project=proj.currentData())
            self.page.refresh_current()
        v.addLayout(_hrow(_muted("Explodes the BOM (all levels) and routes every requirement to who supplies it: "
                                 "purchased parts → primary vendor, fabricated parts → their fab / machine shop, raw "
                                 "material → material vendor, finishing → finisher."), pk, QLabel("Qty"), qty, net,
                          proj, _btn("Build", go, primary=True)))
        pid = self.st.get("job_part")
        if not pid or not inv.part(pid):
            v.addWidget(_muted("Pick an assembly and a quantity."))
            v.addStretch(1)
            return w
        job = R.job_order(inv, pid, self.st.get("job_qty", 1), self.st.get("job_net", True), self.st.get("job_project"))
        self._job = job
        buy = {k: x for k, x in job["groups"].items() if not k.startswith("In-house")}
        v.addLayout(_hrow(QLabel(f"<b>{job['qty']:g} × {part_label(inv.part(pid))}</b> — {len(buy)} vendor(s), "
                                 f"est. {M.fmt_money(sum(job['totals'].get(k, 0) for k in buy))}"),
                          _btn("PO drafts…", self.export_pos, "One Excel per vendor, with their address and contact"),
                          _btn("Log as orders", self.log_orders, "Record every vendor's lines as part orders (shows "
                                                                 "on Time-Phased)"),
                          _btn("Pick list…", self.pick_list, "Kit for assembly: what to pull from which bin")))
        hdr = ["Kind", "Part Number", "Item", "Vendor Part #", "Qty", "Unit", "Unit Price", "Ext", "Note"]
        for vend, lines in job["groups"].items():
            vr = R.vendor_record(inv, vend) or {}
            lead = M.Inventory.lead_label(inv.lead_time_range(vend))
            v.addWidget(_h(f"{vend}  <span style='color:{theme.MUTED}; font-weight:normal'>"
                           f"{len(lines)} line(s) · {M.fmt_money(job['totals'][vend])}"
                           + (f" · lead {lead}" if lead else "") + (f" · {vr.get('phone')}" if vr.get("phone") else "")
                           + "</span>"))
            v.addWidget(_table(hdr, lines, 2, money=("Unit Price", "Ext")))
        v.addWidget(_h("Kit / pick list"))
        v.addWidget(_table(["Bin", "Part Number", "Part", "Pick", "Unit", "On Hand", "Short"], job["pick"], 2)
                    if job["pick"] else _muted("Nothing comes off the shelf (or nothing is stocked yet)."))
        return w

    def export_pos(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder for the PO drafts")
        if folder:
            paths = R.write_po_drafts(self.inv, self._job, folder)
            self.page.toast.show_msg(f"{len(paths)} PO draft(s) saved")
            from .inventory_ui import _open_link
            _open_link(folder)

    def log_orders(self):
        vend = [k for k in self._job["groups"] if not k.startswith("In-house")]
        if QMessageBox.question(self, "Log orders", f"Log {sum(len(self._job['groups'][k]) for k in vend)} line(s) "
                                f"from {len(vend)} vendor(s) as part orders?") != QMessageBox.Yes:
            return
        made = R.log_job_orders(self.inv, self._job)
        self._commit(f"Logged {len(made)} order line(s)")

    def pick_list(self):
        job = self._job
        rows = [{**r, "Picked": ""} for r in job["pick"]]
        from ..inventory import analytics as A
        html = A.binning_html([{"Bin": r["Bin"], "Location": r["Bin"].split(" · ")[0], "Part": r["Part"],
                                "Part Number": r["Part Number"], "Size": "", "On Hand": r["On Hand"],
                                "Unit": r["Unit"], "Reorder Pt": r["Pick"], "Count": "SHORT " + f"{r['Short']:g}"
                                if r["Short"] else ""} for r in rows],
                              f"Kit: {job['qty']:g} × {job['partNumber']} {job['part']}",
                              f"{dt.datetime.now():%Y-%m-%d %H:%M} · 'Reorder Pt' column = qty to pick")
        d = QDialog(self)
        d.setWindowTitle("Pick list")
        d.resize(900, 650)
        lay = QVBoxLayout(d)
        tb = QTextBrowser()
        tb.setHtml(html)
        lay.addWidget(tb)
        bb = QDialogButtonBox()

        def prt():
            from PySide6.QtGui import QTextDocument
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
            pr = QPrinter(QPrinter.HighResolution)
            if QPrintDialog(pr, d).exec():
                doc = QTextDocument()
                doc.setHtml(html)
                doc.print_(pr)
        bb.addButton("Print…", QDialogButtonBox.ActionRole).clicked.connect(prt)
        bb.addButton("Excel…", QDialogButtonBox.ActionRole).clicked.connect(
            lambda: _save_xlsx(d, "DIMS-pick-list.xlsx", [("Pick list", rows)]))
        bb.addButton(QDialogButtonBox.Close).clicked.connect(d.reject)
        lay.addWidget(bb)
        d.exec()

    # ------------------------------------------------------------ materials
    def materials(self):
        w, v = self._page()
        inv = self.inv
        src = QComboBox()
        src.addItem("Open production orders", "orders")
        src.addItem("Active projects × their units", "projects")
        src.setCurrentIndex(max(0, src.findData(self.st.get("mat_src", "orders"))))
        src.currentIndexChanged.connect(lambda _i: (self.st.__setitem__("mat_src", src.currentData()),
                                                    self.page.refresh_current()))
        if self.st.get("mat_src", "orders") == "orders":
            dem = R.open_demands(inv)
        else:
            dem = [(p["id"], inv.needed_for_project(p, pr["id"])) for pr in inv.active_projects()
                   for p in inv.parts_for_project(pr["id"])]
        rows = R.material_needs(inv, dem)
        v.addLayout(_hrow(_muted("Raw material needed for the demand (material set on each fabricated part, × the "
                                 "quantity needed through every BOM level) — for buying and bulk-buy comparisons."),
                          src, _btn("Excel…", lambda: _save_xlsx(self, "DIMS-material-needs.xlsx",
                                                                 [("Material", rows)]))))
        v.addWidget(_table(["Material", "Qty", "Unit", "Stock Length", "Lengths", "Vendor", "Used By"], rows, 6)
                    if rows else _muted("No raw material set on the parts in demand (Edit part ▸ Raw material)."), 1)
        return w


class PrefixDialog(QDialog):
    def __init__(self, inv, parent=None):
        super().__init__(parent)
        self.inv = inv
        self.setWindowTitle("Part-number prefixes")
        self.resize(560, 420)
        v = QVBoxLayout(self)
        v.addWidget(_muted("Made parts: product-line letter + level (C30 top assembly, C20 sub-assembly / "
                           "weldment, C10 machined part). Purchased: P + category (P00 fasteners …). Numeric prefixes "
                           "(1000-001) are purchased material. Shared with the team."))
        self.lines = QLineEdit("; ".join(f"{k}={v}" for k, v in R.product_lines(inv).items()))
        self.lines.setPlaceholderText("C=Clamping; L=Lift carts")
        lf = QFormLayout()
        lf.addRow("Product lines", self.lines)
        v.addLayout(lf)
        self.t = QTableWidget(0, 3)
        self.t.setHorizontalHeaderLabels(["Prefix", "Meaning", "Type"])
        self.t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.t, 1)
        for p in R.all_prefixes(inv):
            d = R.describe_prefix(inv, p)
            self._row(p, d["label"], d["type"])
        h = QHBoxLayout()
        h.addWidget(_btn("+ Prefix", lambda: self._row("", "", "")))
        h.addStretch(1)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _row(self, p, label, typ):
        r = self.t.rowCount()
        self.t.insertRow(r)
        self.t.setItem(r, 0, QTableWidgetItem(p))
        self.t.setItem(r, 1, QTableWidgetItem(label))
        cb = QComboBox()
        cb.addItem("—", "")
        for k, lab in M.TYPES:
            cb.addItem(lab, k)
        cb.setCurrentIndex(max(0, cb.findData(typ)))
        self.t.setCellWidget(r, 2, cb)

    def save(self):
        out = {}
        for r in range(self.t.rowCount()):
            p = (self.t.item(r, 0).text() if self.t.item(r, 0) else "").strip()
            if not p:
                continue
            try:
                p = R.norm_prefix(p)
            except ValueError as e:
                return QMessageBox.warning(self, "Prefixes", str(e))
            out[p] = {"label": (self.t.item(r, 1).text() if self.t.item(r, 1) else "").strip(),
                      "type": self.t.cellWidget(r, 2).currentData()}
        lines = {}
        for part in self.lines.text().split(";"):
            k, _, val = part.partition("=")
            if k.strip():
                lines[k.strip().upper()[:1]] = val.strip()
        R.set_prefixes(self.inv, out)
        self.inv.syncMeta = {**(self.inv.syncMeta or {}), "pnProductLines": lines or R.DEFAULT_LINES}
        self.accept()


# ================================================================== time phased
class TimePhasedView(QWidget):
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.inv = page.inv
        st = page.mrp_state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.incoming(), "Incoming")
        tabs.addTab(self.projected(), "Projected Stock")
        tabs.addTab(self.log(), "Order Log")
        tabs.setCurrentIndex(st.get("tp_tab", 0))
        tabs.currentChanged.connect(lambda i: st.__setitem__("tp_tab", i))
        v.addWidget(tabs)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        return w, lay

    def incoming(self):
        w, v = self._page()
        rows = R.incoming(self.inv)
        late = sum(r["Late"] for r in rows)
        v.addLayout(_hrow(QLabel(f"<b>{len(rows)} open order line(s)</b>" + (
            f" · <span style='color:{theme.ERROR}'>{late} late</span>" if late else "")),
            _btn("Excel…", lambda: _save_xlsx(self, "DIMS-incoming.xlsx", [("Incoming", rows)]))))
        if not rows:
            v.addWidget(_muted("Nothing on order. Orders come from Reorder ▸ Order, a job order (Log as orders) or "
                               "Icarus."))
            v.addStretch(1)
            return w
        last = None
        for r in rows:
            if r["Week"] != last:
                last = r["Week"]
                wk = [x for x in rows if x["Week"] == last]
                v.addWidget(_h(f"{last}  <span style='color:{theme.MUTED};font-weight:normal'>{len(wk)} line(s)</span>"))
                t = _table(["Expected", "Item", "Qty", "Vendor", "PO", "For", "Ordered", ""],
                           [{**x, "": ""} for x in wk], 1)
                for i, x in enumerate(wk):
                    if x["Late"]:
                        t.item(i, 0).setForeground(QColor(theme.ERROR))
                    box = QWidget()
                    hb = QHBoxLayout(box)
                    hb.setContentsMargins(0, 0, 0, 0)
                    hb.addWidget(_btn("Receive", lambda _=False, oid=x["id"]: self.receive(oid)))
                    t.setCellWidget(i, 7, box)
                v.addWidget(t)
        recd = [o for o in self.inv.partOrders if o.get("status") == "Received"]
        if recd:
            v.addWidget(_h(f"Received, not binned yet ({len(recd)})"))
            rr = [{"Received": o.get("receivedDate"), "Item": R.order_item(self.inv, o), "Qty": M.num(o.get("qty")),
                   "Vendor": o.get("vendorName"), "Bin": self.inv.location_label_for(
                       (self.inv.part(o.get("partId")) or {}).get("locationId")), "": ""} for o in recd]
            t = _table(["Received", "Item", "Qty", "Vendor", "Bin", ""], rr, 1)
            for i, o in enumerate(recd):
                t.setCellWidget(i, 5, _btn("Binned", lambda _=False, oid=o["id"]: self.bin(oid)))
            v.addWidget(t)
        v.addStretch(1)
        return w

    def receive(self, oid):
        o = next(x for x in self.inv.partOrders if x["id"] == oid)
        if o.get("partId"):
            self.inv.receive_order(oid)
        else:
            o.update(status="Received", receivedDate=M.today())
            R.add_event(o, "received", self.inv.userName)
        self.page.commit(f"Received {R.order_item(self.inv, o)}")

    def bin(self, oid):
        o = next(x for x in self.inv.partOrders if x["id"] == oid)
        p = self.inv.part(o.get("partId"))
        loc = None
        if p is not None:
            opts = [("", "— keep / none —")] + self.inv.location_options()
            labels = [lab for _i, lab in opts]
            cur = self.inv.location_label_for(p.get("locationId"))
            lab, ok = QInputDialog.getItem(self, "Binned", f"Bin for {p['name']}:", labels,
                                           labels.index(cur) if cur in labels else 0, False)
            if not ok:
                return
            loc = dict((b, a) for a, b in opts).get(lab) or None
        R.bin_order(self.inv, oid, loc)
        self.page.commit(f"Binned {R.order_item(self.inv, o)}")

    def projected(self):
        w, v = self._page()
        st = self.page.mrp_state
        hz = QSpinBox()
        hz.setRange(14, 365)
        hz.setSuffix(" days")
        hz.setValue(st.get("tp_horizon", 120))
        hz.editingFinished.connect(lambda: (st.__setitem__("tp_horizon", hz.value()), self.page.refresh_current()))
        rows = R.projected(self.inv, st.get("tp_horizon", 120))
        short = [r for r in rows if r["Short On"]]
        v.addLayout(_hrow(_muted("Stock now + orders on their expected dates − open production orders' needs (all BOM "
                                 "levels) on their fab start / due dates. 'Short On' = the day it would run out."),
                          QLabel("Look ahead"), hz,
                          _btn("Excel…", lambda: _save_xlsx(self, "DIMS-projected-stock.xlsx", [("Projected", rows)]))))
        v.addWidget(QLabel(f"<b>{len(rows)} part(s) with incoming or demand</b>" + (
            f" · <span style='color:{theme.ERROR}'>{len(short)} run short</span>" if short else "")))
        t = _table(["Part", "Part Number", "On Hand", "Incoming", "Demand", "Projected", "Short On",
                    "Below Reorder On"], rows, 0)
        for i, r in enumerate(rows):
            if r["Short On"]:
                t.item(i, 6).setForeground(QColor(theme.ERROR))
                t.item(i, 5).setForeground(QColor(theme.ERROR))
            t.item(i, 0).setToolTip("\n".join(f"{d}  {q:+g}" for d, q in r["timeline"][:30]))
        v.addWidget(t if rows else _muted("No open orders or production demand."), 1)
        return w

    def log(self):
        w, v = self._page()
        rows = R.order_log(self.inv)
        v.addLayout(_hrow(_muted("Ordered, received, binned and shipped — newest first."),
                          _btn("Excel…", lambda: _save_xlsx(self, "DIMS-order-log.xlsx", [("Order log", rows)]))))
        v.addWidget(_table(["When", "Event", "Item", "Qty", "Vendor", "PO", "For", "By"], rows, 2) if rows
                    else _muted("No orders logged yet."), 1)
        return w


# ================================================================== part profile
class PartProfileDialog(QDialog):
    """Everything about one item on one page."""

    def __init__(self, page, pid, parent=None):
        super().__init__(parent or page)
        self.page = page
        inv = page.inv
        p = inv.part(pid)
        self.setWindowTitle(f"Profile — {part_label(p)}")
        self.resize(980, 760)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(page.thumbs.icon_for(p).pixmap(110, 110))
        ic.setFixedSize(116, 116)
        ic.setFrameShape(QFrame.StyledPanel)
        top.addWidget(ic)
        g = QGridLayout()
        loc = inv.location_label_for(p.get("locationId"))
        rep = inv.part(p.get("replacementId")) if p.get("discontinued") else None
        created = R._d(p.get("createdAt"))
        facts = [("Part No.", p.get("partNumber") or "—"), ("Description", p.get("description") or p["name"]),
                 ("Type", M.TYPE_LABEL.get(p.get("type"), "")), ("Revision", p.get("revision") or "—"),
                 ("Stock", f"{M.num(p.get('currentStock')):g} {p.get('unit') or 'ea'} (reorder at "
                           f"{M.num(p.get('reorderPoint')):g})"),
                 ("Bin", loc or "—"), ("Unit cost", f"{M.fmt_money(p.get('unitCost'))} {p.get('unitCostCurrency') or ''}"),
                 ("Created", f"{p.get('createdBy') or '—'} · {created or '—'}"),
                 ("Category", p.get("category") or "—"), ("QuickBooks #", p.get("qbPartNumber") or "—")]
        if p.get("discontinued"):
            facts.append(("Discontinued", f"{p.get('discontinuedDate') or ''}" + (f" → {rep['name']}" if rep else "")))
        m, f = p.get("material") or {}, p.get("finish") or {}
        if m.get("shape") or m.get("size"):
            facts.append(("Raw material", f"{' '.join(x for x in (m.get('grade'), m.get('shape'), m.get('size')) if x)}"
                                          f" · {M.num(m.get('qty')):g} {m.get('unit') or ''}/part"
                                          + (f" · {m['vendor']}" if m.get("vendor") else "")))
        if f.get("process"):
            facts.append(("Finish", f"{f['process']}" + (f" — {f['spec']}" if f.get("spec") else "")
                                    + (f" · {f['vendor']}" if f.get("vendor") else "")))
        for i, (k, val) in enumerate(facts):
            a = QLabel(f"<span style='color:{theme.MUTED}'>{k}</span>")
            b = QLabel(f"<b>{val}</b>" if i < 2 else str(val))
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            b.setWordWrap(True)
            g.addWidget(a, i // 2, (i % 2) * 2)
            g.addWidget(b, i // 2, (i % 2) * 2 + 1)
        top.addLayout(g, 1)
        v.addLayout(top)
        tabs = QTabWidget()
        tabs.addTab(_table(["Role", "Vendor", "Vendor Part #", "Price", "Currency", "Price CAD", "Quoted Lead",
                            "Last Lead (days)", "Avg Lead (days)", "Price Date"], R.vendor_options(inv, p), 1,
                           money=("Price CAD",)), "Vendors")
        oh = R.order_history(inv, p["id"])
        tabs.addTab(_table(["Ordered", "Qty", "Vendor", "PO", "Expected", "Received", "Status", "For", "Lead (days)"],
                           oh, 2) if oh else _muted("No orders yet."), f"Order history ({len(oh)})")
        wu = R.where_used(inv, p["id"])
        wt = QWidget()
        wl = QVBoxLayout(wt)
        wl.addWidget(QLabel("<b>Shared</b> — used in more than one place" if wu["shared"] else "Unique to one use"))
        wl.addWidget(_table(["Part", "Part Number", "Qty", "Kind"], wu["parents"], 0) if wu["parents"] else
                     _muted("Not inside any assembly."))
        wl.addWidget(_table(["Project", "Qty per unit"], wu["projects"], 0) if wu["projects"] else
                     _muted("No project needs it."))
        tabs.addTab(wt, "Where used")
        if R.has_bom(p):
            tabs.addTab(_table(["Level", "Part Number", "Part", "Qty / Assy", "Qty Total", "On Hand", "Unit Cost",
                                "Vendor", "Bin"], R.bom_rows(inv, p["id"], 0), 2, money=("Unit Cost",)),
                        f"BOM ({len(p['components'])})")
        tx = [t for t in inv.transactions if t.get("partId") == p["id"]][-60:]
        tabs.addTab(_table(["When", "Action", "Qty", "By", "Note"], [
            {"When": dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
             "Action": {"usage": "Used", "receiving": "Received"}.get(t.get("mode"), t.get("mode")),
             "Qty": M.num(t.get("qty")), "By": t.get("by"), "Note": t.get("note")} for t in reversed(tx)], 4),
            "Stock moves")
        v.addWidget(tabs, 1)
        bb = QDialogButtonBox()
        bb.addButton("Edit", QDialogButtonBox.ActionRole).clicked.connect(lambda: (self.accept(),
                                                                                    page.edit_part(p["id"])))
        for k, lab in (("drawingUrl", "Drawing"), ("cadFile", "Model")):
            if p.get(k):
                from .inventory_ui import _open_link
                bb.addButton(lab, QDialogButtonBox.ActionRole).clicked.connect(lambda _=False, u=p[k]: _open_link(u))
        bb.addButton(QDialogButtonBox.Close).clicked.connect(self.reject)
        v.addWidget(bb)


# ================================================================== imports
def _read_rows(path):
    if str(path).lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = None
        for row in it:                                  # header = first row with 2+ text cells
            if row and sum(isinstance(c, str) and c.strip() != "" for c in row) >= 2:
                head = [str(c or "").strip() for c in row]
                break
        return [dict(zip(head, r)) for r in it if r and any(c not in (None, "") for c in r)] if head else []
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def import_vendors_ui(page):
    path, _ = QFileDialog.getOpenFileName(page, "Vendor list", "", "Spreadsheets (*.xlsx *.xlsm *.csv)")
    if not path:
        return
    a, u = R.import_vendors(page.inv, _read_rows(path))
    page.commit(f"Vendors: {a} added, {u} updated")


def import_prices_ui(page):
    path, _ = QFileDialog.getOpenFileName(page, "Vendor prices (e.g. the pricing Google Sheet saved as .xlsx)", "",
                                          "Spreadsheets (*.xlsx *.xlsm *.csv)")
    if not path:
        return
    r = R.import_prices(page.inv, _read_rows(path))
    page.commit(f"Prices: {r['updated']} updated")
    if r["unmatched"] or r["no_free_slot"]:
        QMessageBox.information(page, "Prices", (f"No part with these numbers: {', '.join(r['unmatched'][:30])}\n"
                                                 if r["unmatched"] else "")
                                + (f"All 3 supplier slots taken: {', '.join(r['no_free_slot'][:30])}"
                                   if r["no_free_slot"] else ""))
