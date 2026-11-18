"""DIMS MRP-lite screens: Engineering (part numbers, BOM workbench, where used, job orders, material needs), the
Time-Phased tab (incoming, projected stock, order log), the part profile, vendor / price import."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, QTimer, Signal
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
def _fit(t):
    """Table as tall as its rows — several short tables stack without gaps."""
    from PySide6.QtWidgets import QAbstractScrollArea, QSizePolicy
    t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    t.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
    t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    t.setMinimumHeight(0)
    return t


class EngineeringView(QWidget):
    edit_part = Signal(str)
    new_part = Signal(dict)

    def __init__(self, page, parent=None, parts_widget=None, profile_widget=None, browser=None):
        super().__init__(parent)
        self.page = page
        self.inv = page.inv
        self.st = page.mrp_state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        if parts_widget is not None:                 # Parts and Engineering are one tab
            self.tabs.addTab(parts_widget, "Parts")
        if profile_widget is not None:
            self.tabs.addTab(profile_widget, "Part Profile")
        if browser is not None:                      # part numbers merged into the Part Profile tab
            browser.install_numbers(self.new_part_bar(), self.numbers())
        tabs = [("BOM Workbench", self.bom), ("Where Used", self.used),
                ("Job Order && Kit", self.job), ("Material Needs", self.materials)]
        if browser is None:
            tabs.insert(0, ("Part Numbers", self.numbers))
        self._fns = {}
        cur = self.st.get("eng_tab", 0)
        for name, fn in tabs:
            i = self.tabs.count()
            self._fns[i] = fn
            # only the tab you're on is built; the others are built when first opened (each one is a full view —
            # building them all on every change made the Parts tab slow)
            self.tabs.addTab(fn() if i == cur else self._placeholder(), name)
        self.tabs.setCurrentIndex(cur)
        self.tabs.currentChanged.connect(self._tab_changed)
        v.addWidget(self.tabs)

    @staticmethod
    def _placeholder():
        w = QWidget()
        w._lazy = True
        return w

    def _tab_changed(self, i):
        self.st["eng_tab"] = i
        if getattr(self.tabs.widget(i), "_lazy", False):
            self._build_tab(i)

    def _build_tab(self, i):
        fn = self._fns.get(i)
        if fn is None:
            return
        old = self.tabs.widget(i)
        label = self.tabs.tabText(i)
        new = fn()
        from . import table_prefs as TP
        TP.apply_all(new, self.page.settings)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(i)
        self.tabs.insertTab(i, new, label)
        self.tabs.setCurrentIndex(i)
        self.tabs.blockSignals(False)
        if old is not None:
            old.deleteLater()

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        return w, lay

    def refresh_tab(self):
        """Rebuild just the visible engineering tab (Build, pickers …) — after the click that asked for it, and
        without touching the Part Profile editor (its unsaved edits would otherwise block a full page rebuild)."""
        QTimer.singleShot(0, self._refresh_tab)

    def _refresh_tab(self):
        try:
            i = self.tabs.currentIndex()
        except RuntimeError:                         # the page was rebuilt meanwhile
            return
        if self._fns.get(i) is None:
            return self.page.refresh_current()
        self._build_tab(i)

    def _commit(self, msg):
        self.page.commit(msg)

    # ------------------------------------------------------------ part numbers
    def new_part_bar(self):
        """Pick a prefix → the next number is reserved and a blank profile opens with its type preset."""
        inv = self.inv
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        pre = QComboBox()
        pre.addItem("No number (purchased / fastener)", "")
        for p in R.all_prefixes(inv):
            lab = R.describe_prefix(inv, p)["label"]
            pre.addItem(p + (f" — {lab}" if lab else ""), p)
        pre.setCurrentIndex(max(0, pre.findData(self.st.get("new_prefix", ""))))
        nxt = QLabel()
        nxt.setStyleSheet(f"color:{theme.ACCENT_TEXT}; font-weight:600")

        def preview(*_a):
            try:
                nxt.setText(R.next_numbers(inv, pre.currentData(), 1)[0] if pre.currentData() else "")
            except ValueError:
                nxt.setText("")
        pre.currentIndexChanged.connect(preview)
        preview()

        def go():
            prefix = pre.currentData()
            self.st["new_prefix"] = prefix
            if not prefix:
                return self.page.new_part_from({})
            try:
                got = self.reserve(prefix, 1, None, "new part")
            except Exception as e:  # noqa: BLE001
                return QMessageBox.warning(self, "Part numbers", str(e))
            self.create_profile(got[0]["id"])
        b = _btn("+ New Part", go, "Reserve the next number for this prefix and open a blank profile", primary=True)
        self.new_btn, self.new_prefix = b, pre
        h.addWidget(QLabel("New"))
        h.addWidget(pre, 1)
        h.addWidget(nxt)
        h.addWidget(b)
        return w

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
        v.addLayout(_hrow(pre, count, nxt, res))                    # two rows: fits the profile side panel
        v.addLayout(_hrow(note, proj))
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
        self.page.new_part_from({"partNumber": number, "type": d["type"] or "manufactured",
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
        pk.activated.connect(lambda _i: (self.st.__setitem__("bom_part", pk.part_id()), self.refresh_tab()))
        levels = QComboBox()
        levels.addItems(["1 level", "All levels"])
        levels.setCurrentIndex(self.st.get("bom_levels", 0))
        levels.currentIndexChanged.connect(lambda i: (self.st.__setitem__("bom_levels", i), self.refresh_tab()))
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
                for x in [inv.part(r["id"]) for r in rows] if x and R.mat_of(x).get("shape")]
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
        pk.activated.connect(lambda _i: (self.st.__setitem__("wu_part", pk.part_id()), self.refresh_tab()))
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
                                                    self.refresh_tab()))
        v.addWidget(t if sh else _muted("No part is used in more than one place yet."), 1)
        return w

    # ------------------------------------------------------------ job order
    def job(self):
        w, v = self._page()
        inv = self.inv
        st = self.st
        mode = st.get("job_mode") or "project"
        modes = QComboBox()
        modes.addItem("Project", "project")
        modes.addItem("Single part / assembly", "part")
        modes.setCurrentIndex(max(0, modes.findData(mode)))
        modes.currentIndexChanged.connect(lambda _i: (st.__setitem__("job_mode", modes.currentData()),
                                                      self.refresh_tab()))
        net = QCheckBox("Use stock on hand")
        net.setChecked(st.get("job_net", True))
        kit_mfg = QCheckBox("MFG parts in the kit")
        kit_mfg.setToolTip("Fasteners and purchased parts are always kit (bin) items; tick to pick fabricated parts "
                           "from their bins too")
        kit_mfg.setChecked(bool(st.get("job_kit_mfg")))
        kit_mfg.toggled.connect(lambda on: (st.__setitem__("job_kit_mfg", on),
                                            self.refresh_tab()))
        qty = QDoubleSpinBox()
        qty.setRange(0.001, 100000)
        qty.setDecimals(0)
        proj = QComboBox()
        if mode == "project":
            for pr in inv.active_projects():
                proj.addItem(pr["name"], pr["id"])
            if proj.count() == 0:
                v.addLayout(_hrow(QLabel("Build"), modes))
                v.addWidget(_muted("Add a project first."))
                v.addStretch(1)
                return w
            proj.setCurrentIndex(max(0, proj.findData(st.get("job_project"))))
            units_default = inv.project_qty(proj.currentData()) or 1
            qty.setValue(st.get("job_units") if st.get("job_units_for") == proj.currentData()
                         and st.get("job_units") else units_default)
            proj.currentIndexChanged.connect(lambda _i: (st.update(job_project=proj.currentData(), job_units=None),
                                                         self.refresh_tab()))

            def go():
                st.update(job_project=proj.currentData(), job_units=qty.value(), job_units_for=proj.currentData(),
                          job_net=net.isChecked())
                self.refresh_tab()
            v.addLayout(_hrow(QLabel("Build"), modes, proj, QLabel("Units"), qty, net, kit_mfg,
                              _btn("Build", go, primary=True)))
            v.addWidget(_muted("Every part on the project × units: what to order from whom, and the kit to pull from "
                               "the bins. Units default to what's on open production."))
            pid_proj = proj.currentData()
            job = R.project_job_order(inv, pid_proj, qty.value(), st.get("job_net", True))
            title = f"{qty.value():g} × {inv.project_name(pid_proj)}"
        else:
            pk = PartPicker(inv, None, w, "— what to build —")
            pk.set_part(st.get("job_part"))
            qty.setValue(st.get("job_qty", 1))
            proj.addItem("For stock", None)
            for pr in inv.active_projects():
                proj.addItem(pr["name"], pr["id"])
            proj.setCurrentIndex(max(0, proj.findData(st.get("job_project"))))

            def go():
                st.update(job_part=pk.part_id(), job_qty=qty.value(), job_net=net.isChecked(),
                          job_project=proj.currentData())
                self.refresh_tab()
            v.addLayout(_hrow(QLabel("Build"), modes, pk, QLabel("Qty"), qty, net, proj, kit_mfg,
                              _btn("Build", go, primary=True)))
            v.addWidget(_muted("Explodes the BOM (all levels) and routes every requirement to who supplies it: "
                               "purchased parts → primary vendor, fabricated parts → their fab / machine shop, raw "
                               "material → material vendor, finishing → finisher."))
            pid = st.get("job_part")
            if not pid or not inv.part(pid):
                v.addWidget(_muted("Pick an assembly and a quantity."))
                v.addStretch(1)
                return w
            job = R.job_order(inv, pid, st.get("job_qty", 1), st.get("job_net", True), st.get("job_project"))
            title = f"{job['qty']:g} × {part_label(inv.part(pid))}"
        show_mfg = bool(st.get("job_kit_mfg"))
        job["kit"] = [r for r in job["pick"] if show_mfg or r.get("Group") != "MFG parts"]
        self._job = job
        buy = {k: x for k, x in job["groups"].items() if not k.startswith("In-house")}
        v.addLayout(_hrow(QLabel(f"<b>{title}</b> — {len(buy)} vendor(s), "
                                 f"est. {M.fmt_money(sum(job['totals'].get(k, 0) for k in buy))}"),
                          _btn("PO drafts…", self.export_pos, "One Excel per vendor, with their address and contact"),
                          _btn("Log as orders", self.log_orders, "Record every vendor's lines as part orders (shows "
                                                                 "on Time-Phased)"),
                          _btn("Pick list…", self.pick_list, "Kit for assembly: what to pull from which bin")))
        # the kit first: bin items by type
        v.addWidget(_h("Kit / pick list"))
        kit_hdr = ["Bin", "Part Number", "Part", "Need", "Pick", "Unit", "On Hand", "Short"]
        any_kit = False
        for g in R.KIT_GROUPS:
            if g == "MFG parts" and not show_mfg:
                continue
            lines = [r for r in job["kit"] if r.get("Group") == g]
            if not lines:
                continue
            any_kit = True
            binned = sum(1 for r in lines if r["Bin"] != "—")
            v.addWidget(QLabel(f"<b>{g}</b>  <span style='color:{theme.MUTED}'>{len(lines)} line(s) · "
                               f"{binned} binned" + (f" · <span style='color:{theme.WARN}'>{len(lines) - binned} "
                                                    "without a bin</span>" if binned < len(lines) else "")
                               + "</span>"))
            v.addWidget(_fit(_table(kit_hdr, lines, 2)))
        if not any_kit:
            v.addWidget(_muted("Nothing comes off the shelf."))
        if not show_mfg:
            n = sum(1 for r in job["pick"] if r.get("Group") == "MFG parts")
            if n:
                v.addWidget(_muted(f"{n} MFG part(s) not in the kit — tick “MFG parts in the kit” to pick them too."))
        v.addWidget(_h("Order by vendor"))
        hdr = ["Kind", "Part Number", "Item", "Vendor Part #", "Qty", "Unit", "Unit Price", "Ext", "Note"]
        for vend, lines in job["groups"].items():
            vr = R.vendor_record(inv, vend) or {}
            lead = M.Inventory.lead_label(inv.lead_time_range(vend))
            v.addWidget(_h(f"{vend}  <span style='color:{theme.MUTED}; font-weight:normal'>"
                           f"{len(lines)} line(s) · {M.fmt_money(job['totals'][vend])}"
                           + (f" · lead {lead}" if lead else "") + (f" · {vr.get('phone')}" if vr.get("phone") else "")
                           + "</span>"))
            v.addWidget(_fit(_table(hdr, lines, 2, money=("Unit Price", "Ext"))))
        if not job["groups"]:
            v.addWidget(_muted("Nothing to order — stock covers it."))
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
        rows = [{**r, "Picked": ""} for r in job.get("kit", job["pick"])]
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
                                                    self.refresh_tab()))
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

    def refresh_tab(self):
        """Rebuild just the visible engineering tab (Build, pickers …) — after the click that asked for it, and
        without touching the Part Profile editor (its unsaved edits would otherwise block a full page rebuild)."""
        QTimer.singleShot(0, self._refresh_tab)

    def _refresh_tab(self):
        try:
            i = self.tabs.currentIndex()
        except RuntimeError:                         # the page was rebuilt meanwhile
            return
        if self._fns.get(i) is None:
            return self.page.refresh_current()
        self._build_tab(i)

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
            receive_dialog(self.page, o)
            return
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
        m, f = R.mat_of(p), R.fin_of(p)
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
        tabs = profile_tabs(page, p)
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


def profile_tabs(page, p) -> QTabWidget:
    """Vendors, order history, where used, BOM, stock moves for one part."""
    inv = page.inv
    tabs = QTabWidget()
    tabs.addTab(_table(["Role", "Vendor", "Vendor Part #", "Price", "Currency", "Price CAD", "Quoted Lead",
                        "Last Lead (days)", "Avg Lead (days)", "Price Date"], R.vendor_options(inv, p), 1,
                       money=("Price CAD",)), "Vendors")
    oh = R.order_history(inv, p["id"])
    if oh:
        t = _table(["Ordered", "Qty", "Vendor", "PO", "Expected", "Status", "Received", "Rcvd Qty", "For",
                    "Lead (days)", ""], [{**r, "": ""} for r in oh], 2)
        for i, r in enumerate(oh):
            o = next((x for x in inv.partOrders if x.get("id") == r["id"]), None)
            if o is None:
                continue
            if o.get("status") == "Ordered":
                b = _btn("Receive…", lambda _=False, oo=o: receive_dialog(page, oo))
            elif o.get("status") in ("Received", "Binned"):
                n = len(o.get("receivedChanges") or [])
                b = _btn(f"🔒 Change…{f' ({n})' if n else ''}", lambda _=False, oo=o: change_received_dialog(page, oo))
                b.setToolTip("Received quantity is locked — changing it moves stock by the difference and is logged"
                             + "".join(f"\n{R._d(c['at'])} {c['by']}: {c['from']:g} → {c['to']:g}"
                                       + (f" ({c['reason']})" if c.get("reason") else "")
                                       for c in o.get("receivedChanges") or []))
            else:
                continue
            t.setCellWidget(i, 10, b)
        tabs.addTab(t, f"Orders ({len(oh)})")
    else:
        tabs.addTab(_muted("No orders yet."), "Orders (0)")
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
    return tabs


def receive_dialog(page, o) -> bool:
    """Confirm the quantity actually received; stock goes up by it and it is locked."""
    inv = page.inv
    item = R.order_item(inv, o)
    q, ok = QInputDialog.getDouble(page, "Receive", f"{item}\nOrdered {M.num(o.get('qty')):g}"
                                   + (f" · PO {o['poNumber']}" if o.get("poNumber") else "")
                                   + "\n\nQuantity received:", M.num(o.get("qty")), 0, 1e9, 2)
    if not ok:
        return False
    inv.receive_order(o["id"], q)
    page.commit(f"Received {q:g} × {item} — stock updated, quantity locked")
    return True


def change_received_dialog(page, o) -> bool:
    """Correct a locked received quantity — logged, stock moves by the difference."""
    inv = page.inv
    old = inv.received_qty(o)
    q, ok = QInputDialog.getDouble(page, "Change received quantity",
                                   f"{R.order_item(inv, o)}\nReceived {old:g} (locked). New received quantity:",
                                   old, 0, 1e9, 2)
    if not ok or q == old:
        return False
    reason, ok = QInputDialog.getText(page, "Change received quantity", f"Why {old:g} → {q:g}? (logged)")
    if not ok:
        return False
    inv.change_received(o["id"], q, reason)
    page.commit(f"Received qty {old:g} → {q:g} (logged)")
    return True


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
