"""Project BOM window (MFG hierarchy · Fasteners · Purchased parts, copy / export) and the Reorder Buy List."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import model as M
from ..inventory import project_export as PX
from . import theme
from .chat_panel import LINK_ROLE

NUMERIC = re.compile(r"^(Qty|Per Unit|Total|On Hand|Short|Need|To Order|Price|Ext)")


def _item_text(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}" if not v.is_integer() else f"{int(v):,}"
    return str(v)


def _table(cols, rows, links=None, money=()):
    t = QTableWidget(len(rows), len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            txt = f"${v:,.2f}" if cols[j] in money and isinstance(v, (int, float)) else _item_text(v)
            it = QTableWidgetItem(txt)
            if NUMERIC.match(cols[j]):
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if cols[j] in ("Short", "To Order") and isinstance(v, (int, float)) and v > 0:
                it.setForeground(QBrush(QColor(theme.ERROR)))
            if links and links[i]:
                it.setData(LINK_ROLE, ("part", links[i]))
            t.setItem(i, j, it)
    t.resizeColumnsToContents()
    for j, c in enumerate(cols):
        if c in ("Part", "Description"):
            t.setColumnWidth(j, min(340, max(t.columnWidth(j), 160)))
        elif t.columnWidth(j) > 260:
            t.setColumnWidth(j, 260)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _copy(text, page=None, what="table"):
    QGuiApplication.clipboard().setText(text)
    if page is not None and hasattr(page, "toast"):
        page.toast.show_msg(f"Copied the {what} — paste it into Excel, Sheets, an email or a PO")


class ProjectBomDialog(QDialog):
    """A project's parts list: MFG parts as a hierarchy, fasteners and purchased parts totalled for the units."""

    def __init__(self, page, pid, parent=None):
        super().__init__(parent or page)
        self.page, self.pid = page, pid
        self.inv = page.inv
        self.setWindowTitle(f"Project BOM — {self.inv.project_name(pid)}")
        self.resize(1250, 760)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        v = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setProperty("dimsLink", f"project:{pid}")
        head.addWidget(self.title, 1)
        head.addWidget(QLabel("Units"))
        self.units = QDoubleSpinBox()
        self.units.setRange(1, 100000)
        self.units.setDecimals(0)
        self.units.setValue(self.inv.project_qty(pid) or 1)
        self.units.setToolTip("How many to build — totals, shortages and the buy list follow it")
        self.units.valueChanged.connect(lambda *_: self.build())
        head.addWidget(self.units)
        v.addLayout(head)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        row = QHBoxLayout()
        hint = QLabel("Copy pastes as a table (Excel, Sheets, email). Double-click a part to open it · "
                      "Shift-click to share it in chat.")
        hint.setObjectName("muted")
        row.addWidget(hint, 1)
        for lab, fn in (("Copy tab", self.copy_tab), ("Copy all", self.copy_all), ("Export Excel…", self.export)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            if lab.startswith("Export"):
                b.setObjectName("primary")
            row.addWidget(b)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        v.addLayout(row)
        self.build()

    # ------------------------------------------------------------ view
    def build(self):
        keep = self.tabs.currentIndex()
        self.d = d = PX.project_data(self.inv, self.pid, self.units.value())
        u = d["units"]
        cu = sum(d["perUnitCost"].values())
        self.title.setText(
            f"<b style='font-size:15px'>{d['project']['name']}</b>  ·  {u:g} unit{'s' if u != 1 else ''}"
            f"  ·  {d['counts']['MFG parts']} MFG · {d['counts']['Fasteners']} fasteners · "
            f"{d['counts']['Purchased parts']} purchased" + (f" · {d['counts']['Other']} other"
                                                             if d['counts']['Other'] else "")
            + f"  ·  ≈ {M.fmt_money(cu)} / unit"
            + (f"  ·  <span style='color:{theme.ERROR}'>{d['short']} short</span>" if d["short"] else ""))
        self.tabs.clear()
        self.sections = []
        cols, rows = PX.tree_rows(self.inv, d)
        self.tabs.addTab(self._tree(cols), f"MFG parts · {len(rows)}")
        self.sections.append(("MFG parts", cols, rows))
        for g in PX.LIST_GROUPS:
            if g == "Other" and not d["lists"][g]:
                continue
            cols, rows = PX.list_rows(self.inv, d, g)
            t = _table(cols, rows, [r["part"]["id"] for r in d["lists"][g]], money=("Price",))
            t.cellDoubleClicked.connect(lambda r_, _c, g=g: self._open(d["lists"][g][r_]["part"]["id"]))
            self.tabs.addTab(t, f"{g} · {len(rows)}")
            self.sections.append((g, cols, rows))
        cols, rows = PX.buy_rows(self.inv, d)
        self.tabs.addTab(_table(cols, rows, money=("Price", "Ext. (CAD)")), f"Buy List · {len(rows)}")
        self.sections.append(("Buy List", cols, rows))
        if keep >= 0:
            self.tabs.setCurrentIndex(min(keep, self.tabs.count() - 1))

    def _tree(self, cols):
        t = QTreeWidget()
        t.setColumnCount(len(cols))
        t.setHeaderLabels(cols)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        stack: list[QTreeWidgetItem] = []
        bold = QFont()
        bold.setBold(True)
        for ln in self.d["tree"]:
            p = ln["part"]
            vals = [ln["item"], p.get("partNumber") or "", p["name"], p.get("description") or "", PX.kind(p),
                    PX.material(p), PX.finish(p), PX._n(ln["qty"]), PX._n(ln["perUnit"]),
                    PX._n(ln["total"]), PX._n(p.get("currentStock")),
                    self.inv.location_label_for(p.get("locationId"))]
            it = QTreeWidgetItem([_item_text(x) for x in vals])
            it.setData(0, LINK_ROLE, ("part", p["id"]))
            for j in range(7, 11):
                it.setTextAlignment(j, Qt.AlignRight | Qt.AlignVCenter)
            if ln["level"] == 0:
                for j in range(len(cols)):
                    it.setFont(j, bold)
            if M.num(p.get("currentStock")) < ln["total"]:
                it.setForeground(10, QBrush(QColor(theme.ERROR)))
            del stack[ln["level"]:]
            (stack[-1].addChild(it) if stack else t.addTopLevelItem(it))
            stack.append(it)
        t.expandAll()
        for j in range(len(cols)):
            t.resizeColumnToContents(j)
        t.setColumnWidth(2, max(t.columnWidth(2), 260))
        t.itemDoubleClicked.connect(lambda it, _c: self._open(it.data(0, LINK_ROLE)[1]))
        t.header().setSectionResizeMode(QHeaderView.Interactive)
        return t

    def _open(self, part_id):
        self.page.open_link("part", part_id)

    # ------------------------------------------------------------ copy / export
    def copy_tab(self):
        i = self.tabs.currentIndex()
        title, cols, rows = self.sections[i]
        _copy(PX.tsv(cols, rows), self.page, title)

    def copy_all(self):
        out = []
        for title, cols, rows in self.sections:
            if title == "Buy List" or not rows:
                continue
            out.append(f"{title}\n" + PX.tsv(cols, rows))
        _copy("\n".join(out), self.page, "parts list")

    def export(self):
        name = re.sub(r"[^\w\- ]+", "", self.d["project"]["name"]).strip().replace(" ", "-") or "project"
        path = self.page._save_path(f"{name}-BOM-{M.today()}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        PX.write(path, self.inv, [self.pid], {self.pid: self.units.value()}, self.inv.userName or "")
        self.page.toast.show_msg("Exported the project BOM")


# ------------------------------------------------------------------ Reorder → Buy List
def buy_list(inv: M.Inventory, parts, need_fn, vendor=None):
    """(cols, rows) to buy — one line per part, sorted by vendor, everything a PO line needs."""
    cols = ["Vendor", "Part No.", "Vendor PN", "Part", "Description", "Spec", "Qty", "Unit", "Price", "Currency",
            "Ext. (CAD)", "Projects", "Website"]
    rows = []
    for p in parts:
        sups = [s for s in inv.suppliers(p) if not vendor or s["name"] == vendor]
        best = inv.best_supplier(p)
        v = best if best and (not vendor or best["name"] == vendor) else (sups[0] if sups else {})
        q = inv.suggested_qty(p, need_fn(p))
        price = M.num(v.get("price")) if v.get("price") not in (None, "") else None
        rows.append([v.get("name") or "No vendor set", p.get("partNumber") or "", v.get("partNumber") or "",
                     p["name"], p.get("description") or "", PX.spec(p), q, p.get("unit") or "ea", price,
                     v.get("currency") or "CAD",
                     round(inv.to_cad(price, v.get("currency")) * q, 2) if price is not None else None,
                     ", ".join(inv.project_chips(p)), v.get("website") or ""])
    rows.sort(key=lambda r: (r[0] == "No vendor set", r[0].lower(), r[1] or "~", r[3].lower()))
    return cols, rows


class BuyListWidget(QWidget):
    """The buy list as a panel (Reorder tab) — table + copy / export."""

    def __init__(self, page, parts, need_fn, vendor=None, closable=False, parent=None):
        super().__init__(parent)
        self.page = page
        from PySide6.QtWidgets import QCheckBox
        self.all_parts, self.need_fn, self.vendor = list(parts), need_fn, vendor
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.head = QLabel()
        self.head.setWordWrap(True)
        top.addWidget(self.head, 1)
        self.mfg = QCheckBox("Include MFG parts")
        self.mfg.setToolTip("Manufactured parts are made, not bought — tick to list them too (e.g. outsourced fab)")
        self.mfg.setChecked(bool(page.reorder_filter.get("buy_mfg")) if hasattr(page, "reorder_filter") else False)
        self.mfg.toggled.connect(lambda on: (page.reorder_filter.__setitem__("buy_mfg", on)
                                             if hasattr(page, "reorder_filter") else None, self.fill()))
        top.addWidget(self.mfg)
        v.addLayout(top)
        self.box = QVBoxLayout()
        v.addLayout(self.box, 1)
        self.table = None
        row = QHBoxLayout()
        hint = QLabel("Copy pastes as a table — Excel, Sheets, an email or a vendor's quick-order box.")
        hint.setObjectName("muted")
        row.addWidget(hint, 1)
        for lab, fn in (("Copy selected", self.copy_selected), ("Copy all", self.copy_all),
                        ("Export Excel…", self.export)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            if lab == "Copy all":
                b.setObjectName("primary")
            row.addWidget(b)
        if closable:
            close = QPushButton("Close")
            close.clicked.connect(lambda: self.window().close())
            row.addWidget(close)
        v.addLayout(row)
        self.fill()

    def fill(self):
        parts = [p for p in self.all_parts if self.mfg.isChecked() or p.get("type") != "manufactured"]
        left = len(self.all_parts) - len(parts)
        self.cols, self.rows = buy_list(self.page.inv, parts, self.need_fn, self.vendor)
        by_key = {(p.get("partNumber") or "", p["name"]): p["id"] for p in parts}
        ids_by_row = [by_key.get((r[1], r[3])) for r in self.rows]
        vend = len({r[0] for r in self.rows})
        tot = sum(r[10] or 0 for r in self.rows)
        self.head.setText(
            f"<b>{len(self.rows)} line{'s' if len(self.rows) != 1 else ''}</b> · {vend} "
            f"vendor{'s' if vend != 1 else ''} · ≈ {M.fmt_money(tot)} CAD — qty is the suggested order qty "
            "(covers what projects need and refills to the reorder point)"
            + (f" · {left} MFG part{'s' if left != 1 else ''} not listed (made, not bought)" if left else ""))
        if self.table is not None:
            self.table.setParent(None)
            self.table.deleteLater()
        self.table = _table(self.cols, self.rows, ids_by_row, money=("Price", "Ext. (CAD)"))
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.box.addWidget(self.table)

    def copy_all(self):
        _copy(PX.tsv(self.cols, self.rows), self.page, "buy list")

    def copy_selected(self):
        sel = sorted({i.row() for i in self.table.selectedIndexes()})
        if not sel:
            return self.page.toast.show_msg("Select rows first (Ctrl / Shift-click), or use Copy all")
        _copy(PX.tsv(self.cols, [self.rows[i] for i in sel]), self.page, f"{len(sel)} line(s)")

    def export(self):
        path = self.page._save_path(f"dims-buy-list-{M.today()}.xlsx", "Excel (*.xlsx)")
        if path:
            M.write_xlsx(path, [("Buy List", [dict(zip(self.cols, r)) for r in self.rows])], link_col="Website")
            self.page.toast.show_msg("Exported the buy list")


class BuyListDialog(QDialog):
    def __init__(self, page, parts, need_fn, vendor=None, title="Buy List"):
        super().__init__(page)
        self.setWindowTitle(title)
        self.resize(1250, 640)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        v = QVBoxLayout(self)
        self.panel = BuyListWidget(page, parts, need_fn, vendor, closable=True, parent=self)
        v.addWidget(self.panel)

    def __getattr__(self, name):                 # rows / cols / table / copy_* live on the panel
        panel = self.__dict__.get("panel") if "panel" in self.__dict__ else None
        if panel is None:
            raise AttributeError(name)
        return getattr(panel, name)
