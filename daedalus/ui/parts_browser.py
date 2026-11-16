"""Parts: every part (weldment / assembly components included, each its own row) on the Parts tab; the selected
part's editable profile on its own Part Profile tab."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from ..inventory import model as M
from . import theme

COLS = ["", "Part No.", "Name", "Description", "Type", "Used In", "Project", "Vendor", "Stock", "Level",
        "Reorder Pt", "Bin"]


def _fingerprint(w) -> tuple:
    """Current values of every input inside w — to tell whether the editor has unsaved changes."""
    out = []
    for c in w.findChildren(QWidget):
        if isinstance(c, QLineEdit):
            out.append(c.text())
        elif isinstance(c, QPlainTextEdit):
            out.append(c.toPlainText())
        elif isinstance(c, QComboBox):
            out.append(c.currentText())
        elif isinstance(c, QAbstractSpinBox):
            out.append(c.text())
        elif isinstance(c, QAbstractButton) and c.isCheckable():
            out.append(c.isChecked())
    return tuple(out)


class PartsBrowser(QWidget):
    def __init__(self, page, parts: list, toolbar: QWidget, parent=None):
        super().__init__(parent)
        self.page, self.inv, self.parts = page, page.inv, parts
        self.editor = None
        self._clean = None
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 14)
        v.setSpacing(6)
        v.addWidget(toolbar)
        self.open_profile = lambda: None            # set by the page: switch to the Part Profile tab
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        sel = QHBoxLayout()
        self.all_cb = QCheckBox("Select all")
        self.all_cb.setToolTip("Select every part in the list (the current filters) — then Assign to Project")
        ids = [p["id"] for p in parts]
        self.all_cb.setChecked(bool(ids) and all(i in page.sel_parts for i in ids))
        self.all_cb.toggled.connect(self._select_all)
        sel.addWidget(self.all_cb)
        n = len(page.sel_parts)
        self.sel_lab = QLabel(f"{n} selected" if n else f"{len(parts)} part{'s' if len(parts) != 1 else ''}")
        self.sel_lab.setObjectName("muted")
        sel.addWidget(self.sel_lab, 1)
        if n:
            for lab, fn in (("Clear", lambda: (page.sel_parts.clear(), page.refresh_current())),
                            ("Assign to Project", page.bulk_assign)):
                b = QPushButton(lab)
                b.clicked.connect(fn)
                sel.addWidget(b)
        lv.addLayout(sel)
        self.table = self._list(parts)
        hint = QLabel("Click a part to load its profile · double-click (or Enter) opens the Part Profile tab")
        hint.setObjectName("muted")
        lv.addWidget(self.table, 1)
        lv.addWidget(hint)
        v.addWidget(left, 1)
        # the Part Profile tab: a compact part list on the left, the profile on the right
        self.profile_page = QSplitter(Qt.Horizontal)
        self.profile_page.setChildrenCollapsible(False)
        pl = QWidget()
        plv = QVBoxLayout(pl)
        plv.setContentsMargins(14, 10, 4, 14)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find part no., description, project…")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(self._filter_picker)
        plv.addWidget(self.find)
        self.picker = self._picker(parts)
        plv.addWidget(self.picker, 1)
        self.profile_page.addWidget(pl)
        right = QWidget()
        self.rv = QVBoxLayout(right)
        self.rv.setContentsMargins(4, 10, 14, 14)
        self.profile_page.addWidget(right)
        self.profile_page.setStretchFactor(0, 2)
        self.profile_page.setStretchFactor(1, 3)
        self.profile_page.setSizes(page.mrp_state.get("profile_split") or [520, 860])
        self.profile_page.splitterMoved.connect(
            lambda *_: page.mrp_state.__setitem__("profile_split", self.profile_page.sizes()))
        self.setMinimumHeight(640)
        pid = page.parts_sel
        if pid == "__new__":
            self.show_new()
        elif pid and self.inv.part(pid):
            self.show_part(pid)
            self._mark_row(pid)
        else:
            self._empty()

    # ------------------------------------------------------------ list
    def _list(self, parts):
        inv, page = self.inv, self.page
        from .inventory_ui import Gauge
        parents: dict = {}
        for p in inv.parts:
            for c in p.get("components") or []:
                parents.setdefault(c.get("partId"), []).append(p.get("partNumber") or p["name"])
        t = QTableWidget(len(parts), len(COLS))
        t.setHorizontalHeaderLabels(COLS)
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setSelectionMode(QTableWidget.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setIconSize(QSize(28, 28))
        for i, p in enumerate(parts):
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Checked if p["id"] in page.sel_parts else Qt.Unchecked)
            cb.setData(Qt.UserRole, p["id"])
            t.setItem(i, 0, cb)
            best = inv.best_supplier(p) or (inv.suppliers(p)[0] if inv.suppliers(p) else None)
            stock = M.num(p.get("currentStock"))
            used = parents.get(p["id"], [])
            cells = [p.get("partNumber") or "", p["name"] + ("  (weldment)" if p.get("isWeldment") and
                                                              p.get("components") else
                                                              "  (assembly)" if p.get("isAssembly") and
                                                              p.get("components") else ""),
                     p.get("description") or "", M.TYPE_LABEL.get(p.get("type"), ""),
                     ", ".join(sorted(set(used))[:3]) + ("…" if len(set(used)) > 3 else ""),
                     ", ".join(inv.project_chips(p)), best["name"] if best else "",
                     f"{stock:g} {p.get('unit') or ''}", None, f"{M.num(p.get('reorderPoint')):g}",
                     inv.location_label_for(p.get("locationId")) or ""]
            for j, val in enumerate(cells, start=1):
                if val is None:
                    continue
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, p["id"])
                if j == 2:
                    it.setIcon(page.thumbs.icon_for(p))
                    it.setToolTip(inv.spec_line(p))
                if j in (8, 10):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if j == 8 and stock <= M.num(p.get("reorderPoint")):
                    it.setForeground(QColor(theme.ERROR))
                if p.get("discontinued"):
                    it.setForeground(QColor(theme.MUTED))
                    f = it.font()
                    f.setStrikeOut(True)
                    it.setFont(f)
                if j == 5 and used:
                    it.setToolTip("Inside: " + ", ".join(sorted(set(used))))
                t.setItem(i, j, it)
            t.setCellWidget(i, 9, Gauge(inv.stock_ratio(p), inv.level(p)))
        h = t.horizontalHeader()
        for c in range(len(COLS)):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
        t.resizeColumnsToContents()
        t.setColumnWidth(2, min(max(t.columnWidth(2), 180), 340))
        t.setColumnWidth(3, min(t.columnWidth(3), 260))
        t.verticalHeader().setDefaultSectionSize(34)
        t.itemChanged.connect(self._checked)
        t.currentCellChanged.connect(lambda r, _c, _pr, _pc: self._pick(r))
        t.cellDoubleClicked.connect(lambda r, _c: (self._pick(r), self.open_profile()))
        t.cellActivated.connect(lambda r, _c: (self._pick(r), self.open_profile()))
        return t

    def _picker(self, parts):
        """Part Profile's list: part number, description, project, used in."""
        inv = self.inv
        parents: dict = {}
        for p in inv.parts:
            for c in p.get("components") or []:
                parents.setdefault(c.get("partId"), set()).add(p.get("partNumber") or p["name"])
        hdr = ["Part No.", "Description", "Project", "Used In"]
        t = QTableWidget(len(parts), len(hdr))
        t.setHorizontalHeaderLabels(hdr)
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setSelectionMode(QTableWidget.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setIconSize(QSize(22, 22))
        for i, p in enumerate(parts):
            desc = p.get("description") or p["name"]
            used = sorted(parents.get(p["id"], ()))
            for j, val in enumerate((p.get("partNumber") or "", desc, ", ".join(inv.project_chips(p)),
                                     ", ".join(used[:3]) + ("…" if len(used) > 3 else ""))):
                it = QTableWidgetItem(val)
                if j == 1:
                    it.setIcon(self.page.thumbs.icon_for(p))
                    it.setToolTip(p["name"] if p.get("description") else inv.spec_line(p))
                if j == 3 and used:
                    it.setToolTip("Inside: " + ", ".join(used))
                if p.get("discontinued"):
                    it.setForeground(QColor(theme.MUTED))
                t.setItem(i, j, it)
        h = t.horizontalHeader()
        for c in range(len(hdr)):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
        t.resizeColumnsToContents()
        t.setColumnWidth(1, min(max(t.columnWidth(1), 160), 300))
        h.setStretchLastSection(True)
        t.verticalHeader().setDefaultSectionSize(30)
        t.currentCellChanged.connect(lambda r, _c, _pr, _pc: self._pick(r))
        return t

    def _filter_picker(self, text):
        q = text.strip().lower()
        for i in range(self.picker.rowCount()):
            row = " ".join(self.picker.item(i, j).text() for j in range(self.picker.columnCount())).lower()
            self.picker.setRowHidden(i, bool(q) and q not in row)

    def _mark_row(self, pid):
        """Select pid's row in both lists without loading it again."""
        r = next((i for i, p in enumerate(self.parts) if p["id"] == pid), -1)
        for t in (self.table, getattr(self, "picker", None)):
            if t is None:
                continue
            t.blockSignals(True)
            if r >= 0:
                t.selectRow(r)
                QTimer.singleShot(0, lambda t=t: t.scrollToItem(t.item(r, 1)))
            else:
                t.clearSelection()
            t.blockSignals(False)

    def _select_all(self, on):
        ids = [p["id"] for p in self.parts]
        if on:
            self.page.sel_parts.update(ids)
        else:
            self.page.sel_parts.difference_update(ids)
        QTimer.singleShot(0, self.page.refresh_current)

    def _checked(self, it):
        if it.column() != 0:
            return
        pid = it.data(Qt.UserRole)
        if it.checkState() == Qt.Checked:
            self.page.sel_parts.add(pid)
        else:
            self.page.sel_parts.discard(pid)
        n = len(self.page.sel_parts)
        self.sel_lab.setText(f"{n} selected" if n else f"{len(self.parts)} parts")
        QTimer.singleShot(0, self.page.refresh_current)        # show / hide Assign to Project

    def _pick(self, row):
        if row < 0 or row >= len(self.parts):
            return
        pid = self.parts[row]["id"]
        if pid == self.page.parts_sel and self.editor is not None:
            return
        if self.dirty() and QMessageBox.question(
                self, "Unsaved changes", "Discard your changes to this part?") != QMessageBox.Yes:
            self._mark_row(self.page.parts_sel)
            return
        self.page.parts_sel = pid
        self._mark_row(pid)
        self.show_part(pid)

    # ------------------------------------------------------------ profile
    def _clear(self):
        while self.rv.count():
            it = self.rv.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                while it.layout().count():
                    x = it.layout().takeAt(0)
                    if x.widget():
                        x.widget().deleteLater()
        self.editor = None
        self._clean = None

    def _empty(self):
        self._clear()
        lab = QLabel("Pick a part on the Parts tab to see and edit its profile — or + Add Part.")
        lab.setObjectName("muted")
        lab.setAlignment(Qt.AlignCenter)
        self.rv.addWidget(lab, 1)

    def dirty(self) -> bool:
        return self.editor is not None and self._clean is not None and _fingerprint(self.editor) != self._clean

    def mark_clean(self):
        if self.editor is not None:
            self._clean = _fingerprint(self.editor)

    def show_new(self, preset=None):
        self._open(None, preset)

    def show_part(self, pid):
        self._open(self.inv.part(pid))

    def _open(self, p, preset=None):
        from . import inventory_dialogs as D
        from .inventory_mrp_ui import profile_tabs
        from .inventory_ui import _open_link
        self._clear()
        page = self.page
        head = QHBoxLayout()
        if p:
            ic = QLabel()
            ic.setPixmap(page.thumbs.icon_for(p).pixmap(56, 56))
            head.addWidget(ic)
        title = QLabel((f"<b>{p.get('partNumber') or ''}</b>  {p['name']}<br><span style='color:{theme.MUTED}'>"
                        f"{M.TYPE_LABEL.get(p.get('type'), '')} · stock {M.num(p.get('currentStock')):g} "
                        f"{p.get('unit') or ''}" + (f" · {p['description']}" if p.get("description") else "")
                        + "</span>") if p else "<b>New part</b>")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        if p:
            for lab, fn in (("Use", lambda: page.move(p["id"], "usage")),
                            ("Receive", lambda: page.move(p["id"], "receiving")),
                            ("History", lambda: D.HistoryDialog(self.inv, p["id"], page).exec())):
                b = QPushButton(lab)
                b.clicked.connect(fn)
                head.addWidget(b)
            for k, lab in (("drawingUrl", "Drawing"), ("cadFile", "Model")):
                if p.get(k):
                    b = QPushButton(lab)
                    b.clicked.connect(lambda _=False, u=p[k]: _open_link(u))
                    head.addWidget(b)
        self.rv.addLayout(head)
        tabs = QTabWidget()
        ed = D.PartDialog(self.inv, p, page, preset=preset)
        ed.setWindowFlags(Qt.Widget)
        ed.bb.hide()
        ed.accepted.connect(lambda: self._deleted(p) if ed.deleted else None)
        ew = QWidget()
        el = QVBoxLayout(ew)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(ed, 1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        rev = QPushButton("Revert")
        rev.clicked.connect(lambda: self.show_part(p["id"]) if p else self._empty())
        save = QPushButton("Save Changes" if p else "Add Part")
        save.setObjectName("primary")
        save.clicked.connect(lambda: self._save(ed, p))
        bar.addWidget(rev)
        bar.addWidget(save)
        el.addLayout(bar)
        tabs.addTab(ew, "Profile")
        if p:
            info = profile_tabs(page, p)
            while info.count():
                w, lab = info.widget(0), info.tabText(0)
                info.removeTab(0)
                tabs.addTab(w, lab)
        tabs.setCurrentIndex(min(page.mrp_state.get("profile_tab", 0), tabs.count() - 1))
        tabs.currentChanged.connect(lambda i: page.mrp_state.__setitem__("profile_tab", i))
        self.rv.addWidget(tabs, 1)
        self.editor = ed
        QTimer.singleShot(0, self.mark_clean)                # after the dialog's own field syncing

    def _save(self, ed, p):
        from ..inventory import mrp as R
        try:
            ed.validate()
        except ValueError as e:
            return QMessageBox.warning(self, "Part", str(e))
        vals = ed.values()
        if p:
            p.update(vals)
            msg = f"Saved {p['name']}"
        else:
            p = M.new_part(**vals, createdBy=self.inv.userName or "unknown")
            self.inv.parts.append(p)
            if p.get("partNumber"):
                try:
                    R.assign(self.inv, p["partNumber"], p)
                except ValueError:
                    pass
            msg = f"Added {p['name']}"
        R.sync_assignments(self.inv)
        self.page.parts_sel = p["id"]
        self.mark_clean()
        self._clean = None                                     # the page rebuilds with the saved values
        self.editor = None
        self.page.commit(msg)

    def _deleted(self, p):
        if p is None:
            return
        self.inv.delete_part(p["id"])
        self.page.parts_sel = None
        self.editor = None
        self.page.commit("Part deleted")
