"""Holocron inventory page — native Qt (dashboard, projects, production, locations, parts, vendors, clients,
reorder, shortages, activity log, import / export)."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import model as M
from ..inventory.store import InventoryStore, get_store
from . import inventory_dialogs as D
from . import theme

LEVEL = {"red": theme.ERROR, "amber": theme.WARN, "green": theme.OK}
STATUS_COL = {"Not Started": theme.MUTED, "In Fab": theme.INFO, "In Finishing": theme.ACCENT_LINE, "Complete": theme.OK,
              "Requested": theme.MUTED, "Confirmed": theme.INFO, "In Production": theme.ACCENT_LINE, "Shipped": theme.PURPLE}


def _btn(text, fn, tip="", primary=False, danger=False):
    b = QPushButton(text)
    b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    if primary:
        b.setObjectName("primary")
    if danger:
        b.setStyleSheet(f"color:{theme.ERROR}")
    return b


def _small(text, fn, tip="", danger=False, icon=None):
    from . import icons
    b = QToolButton()
    b.setText(text)
    if icon and icons.available():
        b.setText("")
        b.setIcon(icons.icon(icon))
    b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    if danger:
        b.setStyleSheet(f"color:{theme.ERROR}")
    return b


def _actions(*buttons):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(2)
    for b in buttons:
        h.addWidget(b)
    h.addStretch(1)
    return w


def _muted(text, wrap=True):
    l = QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(wrap)
    return l


def _section(text):
    l = QLabel(text)
    f = l.font()
    f.setBold(True)
    f.setPointSizeF(f.pointSizeF() + 1.5)
    l.setFont(f)
    l.setStyleSheet(f"padding-top:10px; color:{theme.TEXT}")
    return l


def _num_item(v, color=None, bold=False):
    it = QTableWidgetItem("" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v)))
    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if color:
        it.setForeground(QBrush(QColor(color)))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    return it


def _open_link(target: str):
    t = (target or "").strip()
    if not t:
        return
    if t.startswith(("http://", "https://", "mailto:")):
        QDesktopServices.openUrl(QUrl(t))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(t.replace("\\\\", "//") if t.startswith("\\\\") else t))


class Gauge(QWidget):
    def __init__(self, ratio, level):
        super().__init__()
        self.ratio, self.color = ratio, QColor(LEVEL.get(level, theme.OK))
        self.setFixedSize(80, 14)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(QRectF(0, 3, 80, 8), 4, 4)
        p.setBrush(self.color)
        p.drawRoundedRect(QRectF(0, 3, 80 * max(0.0, min(1.0, (self.ratio or 0) / 1.4)), 8), 4, 4)


class Gantt(QWidget):
    """bars: [(group label, [(row label, [(start, end, color)])])], dates as ISO strings."""

    def __init__(self, groups, legend=()):
        super().__init__()
        self.groups, self.legend = groups, legend
        dates = [M.parse_date(d) for _g, rows in groups for _l, bars in rows for s, e, _c in bars for d in (s, e)]
        dates = [d for d in dates if d]
        today = dt.date.today()
        lo, hi = (min(dates), max(dates)) if dates else (today, today)
        pad = max((hi - lo).days * 0.05, 2)
        self.lo = lo - dt.timedelta(days=pad)
        self.hi = hi + dt.timedelta(days=pad)
        n = sum(1 + len(rows) for _g, rows in groups)
        self.setMinimumHeight(40 + n * 26)
        self.setMinimumWidth(500)

    def x(self, d, x0, w):
        span = max((self.hi - self.lo).days, 1)
        return x0 + w * min(1, max(0, (d - self.lo).days / span))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W = self.width()
        x0, w = 220, W - 240
        p.setPen(QColor(theme.MUTED))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.drawText(x0, 14, self.lo.strftime("%b %d, %Y"))
        p.drawText(x0 + w - 80, 14, self.hi.strftime("%b %d, %Y"))
        lx = x0 + w / 2 - 120
        for lab, col in self.legend:
            p.setBrush(QColor(col))
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(lx, 5, 10, 10))
            p.setPen(QColor(theme.MUTED))
            p.drawText(int(lx + 14), 14, lab)
            lx += 80
        y = 26
        today = dt.date.today()
        for glabel, rows in self.groups:
            p.setPen(QColor(theme.TEXT))
            fb = QFont(f)
            fb.setBold(True)
            p.setFont(fb)
            p.drawText(6, y + 14, glabel[:34])
            p.setFont(f)
            y += 22
            for rlabel, bars in rows:
                p.setPen(QColor(theme.MUTED))
                p.drawText(18, y + 13, rlabel[:32])
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.PANEL))
                p.drawRoundedRect(QRectF(x0, y, w, 18), 4, 4)
                for s, e, col in bars:
                    ds, de = M.parse_date(s), M.parse_date(e)
                    if not ds or not de:
                        continue
                    a, b = self.x(ds, x0, w), self.x(de, x0, w)
                    p.setBrush(QColor(col))
                    p.drawRoundedRect(QRectF(a, y + 2, max(b - a, 3), 14), 3, 3)
                if self.lo <= today <= self.hi:
                    p.setPen(QPen(QColor(theme.ERROR), 2))
                    tx = self.x(today, x0, w)
                    p.drawLine(int(tx), y - 2, int(tx), y + 20)
                    p.setPen(Qt.NoPen)
                y += 24
            y += 4


class Toast(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("invtoast")
        self.setStyleSheet(f"#invtoast{{background:{theme.PANEL}; border:1px solid {theme.ACCENT_LINE}; border-radius:6px;}}")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 6, 8, 6)
        self.label = QLabel()
        self.undo = QPushButton("Undo")
        h.addWidget(self.label)
        h.addWidget(self.undo)
        self.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self._fn = None
        self.undo.clicked.connect(self._do_undo)

    def show_msg(self, text, undo=None):
        self.label.setText(text)
        self._fn = undo
        self.undo.setVisible(undo is not None)
        self.adjustSize()
        par = self.parentWidget()
        self.move(par.width() - self.width() - 20, par.height() - self.height() - 20)
        self.raise_()
        self.show()
        self.timer.start(6000 if undo else 2600)

    def _do_undo(self):
        fn, self._fn = self._fn, None
        self.hide()
        if fn:
            fn()


# ======================================================================= page

class InventoryPage(QWidget):
    closed = Signal()
    status = Signal(str)
    send_bom_requested = Signal()
    insert_std_requested = Signal(str)        # standard part id → insert into the open model

    TABS = [("dashboard", "Dashboard"), ("projects", "Projects"), ("production", "Production"),
            ("locations", "Locations"), ("parts", "Parts"), ("vendors", "Vendors"), ("clients", "Clients"),
            ("reorder", "Reorder"), ("shortages", "Shortages"), ("log", "Activity Log"), ("sync", "Import / Export")]

    def __init__(self, store: InventoryStore | None = None, parent=None):
        super().__init__(parent)
        self.settings = QSettings("Daedalus", "Daedalus")
        path = self.settings.value("inventory/path") or None
        self.store = store or get_store(path)
        self.store.inv.userName = self.settings.value("inventory/user") or os.environ.get("USERNAME") or \
            os.environ.get("USER") or ""
        self.parts_filter = {"type": "all", "project": "all", "q": ""}
        self.reorder_filter = {"project": "all", "vendor": "all"}
        self.sel_parts: set = set()
        self.sel_reorder: set = set()
        self.collapsed: set = set()
        self.expanded: set = set()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        top.setObjectName("invtop")
        top.setAttribute(Qt.WA_StyledBackground, True)
        top.setStyleSheet(f"#invtop{{background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};}}")
        h = QHBoxLayout(top)
        h.setContentsMargins(8, 6, 8, 6)
        h.addWidget(_btn("◀ 3D model", self.closed.emit))
        title = QLabel("⬡  Holocron Inventory")
        title.setObjectName("h1")
        h.addWidget(title)
        h.addWidget(_muted("Fasteners · Purchased · Manufactured", False))
        h.addStretch(1)
        self.sync_lab = _muted("", False)
        h.addWidget(self.sync_lab)
        self.low_btn = _btn("", lambda: self.goto("reorder"), "Jump to Reorder")
        self.low_btn.setStyleSheet(f"color:{theme.ERROR}; border-color:{theme.ERROR}")
        h.addWidget(self.low_btn)
        h.addWidget(_btn("Send BOM ⇢", self.send_bom_requested.emit, "Add the open design's BOM to a project"))
        h.addWidget(_btn("?", self.guide, "User guide"))
        self.who = _btn("", self.set_name, "Your name on the activity log")
        h.addWidget(self.who)
        lay.addWidget(top)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.pages = {}
        for key, label in self.TABS:
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            self.tabs.addTab(sc, label)
            self.pages[key] = sc
        self.tabs.currentChanged.connect(lambda _i: self.refresh_current())
        lay.addWidget(self.tabs, 1)
        self.toast = Toast(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(3000)
        self._committing = False
        self.store.listeners.append(self._on_store_saved)
        self.refresh()

    def _on_store_saved(self):
        if not self._committing:
            QTimer.singleShot(0, self.refresh)

    # ---------------------------------------------------------------- core
    @property
    def inv(self) -> M.Inventory:
        return self.store.inv

    def commit(self, msg=None, undo=None):
        self._committing = True
        try:
            self.store.save()
        except OSError as e:
            QMessageBox.warning(self, "Inventory", f"Couldn't save the inventory file:\n{e}")
        finally:
            self._committing = False
        self.refresh()
        if msg:
            self.toast.show_msg(msg, undo)
            self.status.emit(msg)

    def _poll(self):
        if not self.isVisible() or QApplication.activeModalWidget() is not None:
            return
        if self.store.check_external():
            self.refresh()

    def goto(self, key):
        self.tabs.setCurrentIndex([k for k, _ in self.TABS].index(key))

    def current_key(self):
        return self.TABS[self.tabs.currentIndex()][0]

    def refresh(self):
        inv = self.inv
        low = len(inv.low_stock())
        self.low_btn.setText(f"⚠ {low} low")
        self.low_btn.setVisible(low > 0)
        self.who.setText(f"Signed in as {inv.userName or 'unknown'} ⚙")
        comp = inv.component_ids()
        counts = {"projects": len(inv.projects), "production": len(inv.productionOrders),
                  "locations": len(inv.locations), "parts": len([p for p in inv.parts if p["id"] not in comp]),
                  "vendors": len(inv.vendors), "clients": len(inv.clientOrders), "reorder": low,
                  "shortages": len(inv.shortages())}
        for i, (k, lab) in enumerate(self.TABS):
            self.tabs.setTabText(i, f"{lab}  {counts[k]}" if k in counts else lab)
        try:
            ts = dt.datetime.fromtimestamp(self.store.path.stat().st_mtime).strftime("%H:%M:%S")
        except OSError:
            ts = "not saved yet"
        self.sync_lab.setText(f"● {self.store.path.name} · {ts}")
        self.sync_lab.setToolTip(str(self.store.path))
        self.refresh_current()

    def refresh_current(self):
        key = self.current_key()
        sc = self.pages[key]
        y = sc.verticalScrollBar().value()
        w = getattr(self, f"build_{key}")()
        w.setObjectName("invpage")
        sc.setWidget(w)
        QTimer.singleShot(0, lambda: sc.verticalScrollBar().setValue(y))

    def _page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 10, 14, 14)
        v.setSpacing(6)
        return w, v

    # ------------------------------------------------------------- tables
    def parts_tree(self, parts, actions=True, selectable=False, max_h=None):
        inv = self.inv
        comp = inv.component_ids()
        cols = (["", ] if selectable else []) + ["Part", "Type", "Project", "Stock", "Level", "Reorder Pt",
                                                  "Unit Cost"] + (["Actions"] if actions else [])
        t = QTreeWidget()
        t.setColumnCount(len(cols))
        t.setHeaderLabels(cols)
        t.setRootIsDecorated(True)
        t.setAlternatingRowColors(True)
        off = 1 if selectable else 0

        def add(parent, p, depth, qty_label, stock_override):
            it = QTreeWidgetItem(parent)
            emb = stock_override is not None
            stock = stock_override if emb else M.num(p.get("currentStock"))
            name = ("× " + str(qty_label) + "  " if qty_label is not None else "") + p["name"] + \
                ("  (weldment)" if p.get("isWeldment") and p.get("components") else "") + \
                ("  [std]" if p.get("stdPartId") else "")
            it.setText(off + 0, name)
            f = it.font(off)
            f.setBold(depth == 0)
            it.setFont(off, f)
            spec = inv.spec_line(p)
            if p.get("stdPartId"):
                spec = (spec + "\n" if spec else "") + "[std] Daedalus standard part — Insert in model puts it in the open model"
            it.setToolTip(off, spec)
            it.setText(off + 1, M.TYPE_LABEL.get(p.get("type"), ""))
            it.setText(off + 2, ", ".join(inv.project_chips(p)) or "—")
            it.setText(off + 3, f"{stock:g}{' *' if emb else ''} {p.get('unit') or ''}")
            it.setTextAlignment(off + 3, Qt.AlignRight | Qt.AlignVCenter)
            if emb:
                it.setToolTip(off + 3, "Embedded in assembled stock — already welded in, not separate on-hand inventory")
            it.setText(off + 5, f"{M.num(p.get('reorderPoint')):g}")
            it.setText(off + 6, M.fmt_money(p.get("unitCost")))
            it.setData(0, Qt.UserRole, p["id"])
            if selectable and depth == 0:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if p["id"] in self.sel_parts else Qt.Unchecked)
            t.setItemWidget(it, off + 4, Gauge(inv.stock_ratio(p, stock), inv.level(p, stock)))
            if actions:
                btns = [_small("Use", lambda _=False, i=p["id"]: self.move(i, "usage")),
                        _small("Receive", lambda _=False, i=p["id"]: self.move(i, "receiving")),
                        _small("Edit", lambda _=False, i=p["id"]: self.edit_part(i)),
                        _small("History", lambda _=False, i=p["id"]: D.HistoryDialog(self.inv, i, self).exec())]
                if p.get("drawingUrl"):
                    btns.append(_small("📄", lambda _=False, u=p["drawingUrl"]: _open_link(u), "Open drawing", icon="file"))
                if p.get("stdPartId"):
                    btns.append(_small("Insert in model", lambda _=False, i=p["stdPartId"]: self.insert_std_requested.emit(i),
                                       "Insert this standard part into the open model"))
                t.setItemWidget(it, off + 7, _actions(*btns))
            if p.get("type") == "manufactured" and p.get("isWeldment"):
                for c in p.get("components") or []:
                    child = inv.part(c.get("partId"))
                    if child:
                        add(it, child, depth + 1, c.get("qty"), stock * M.num(c.get("qty")))
                it.setExpanded(p["id"] in self.expanded)
            return it

        for p in parts:
            if p["id"] not in comp:
                add(t, p, 0, None, None)
        t.itemExpanded.connect(lambda it: self.expanded.add(it.data(0, Qt.UserRole)))
        t.itemCollapsed.connect(lambda it: self.expanded.discard(it.data(0, Qt.UserRole)))
        t.itemDoubleClicked.connect(lambda it, _c: self.edit_part(it.data(0, Qt.UserRole)))
        if selectable:
            t.itemChanged.connect(self._sel_changed)
        hdr = t.header()
        hdr.setSectionResizeMode(off, QHeaderView.Stretch)
        for c in range(len(cols)):
            if c != off:
                hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        rows = t.topLevelItemCount() + sum(t.topLevelItem(i).childCount() for i in range(t.topLevelItemCount())
                                           if t.topLevelItem(i).isExpanded())
        t.setMinimumHeight(min(max_h or 10_000, 34 + 30 * max(rows, 1)))
        return t

    def _sel_changed(self, it, col):
        if col != 0:
            return
        pid = it.data(0, Qt.UserRole)
        if it.checkState(0) == Qt.Checked:
            self.sel_parts.add(pid)
        else:
            self.sel_parts.discard(pid)
        QTimer.singleShot(0, self.refresh_current)

    @staticmethod
    def table(headers, rows, stretch=0):
        """rows: list of lists of str/QTableWidgetItem/QWidget."""
        t = QTableWidget(len(rows), len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setAlternatingRowColors(True)
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                if isinstance(v, QWidget):
                    t.setCellWidget(i, j, v)
                elif isinstance(v, QTableWidgetItem):
                    t.setItem(i, j, v)
                else:
                    t.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))
        hdr = t.horizontalHeader()
        for c in range(len(headers)):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch if c == stretch else QHeaderView.ResizeToContents)
        t.setMinimumHeight(min(700, 34 + 32 * max(len(rows), 1)))
        t.resizeRowsToContents()
        return t

    @staticmethod
    def pill(text, color):
        l = QLabel(text)
        l.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:8px; padding:1px 8px;")
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 2, 2, 2)
        h.addWidget(l)
        h.addStretch(1)
        return w

    # ========================================================== dashboard
    def build_dashboard(self):
        inv = self.inv
        w, v = self._page()
        grid = QGridLayout()
        low = inv.low_stock()
        cards = [("Total Parts", len(inv.parts), None), ("Need Reorder", len(low), theme.ERROR if low else None),
                 ("Fasteners", sum(p.get("type") == "fastener" for p in inv.parts), None),
                 ("Purchased", sum(p.get("type") == "purchased" for p in inv.parts), None),
                 ("Manufactured", sum(p.get("type") == "manufactured" for p in inv.parts), None),
                 ("Stock Value", M.fmt_money(inv.stock_value()), None)]
        for i, (lab, val, col) in enumerate(cards):
            c = QFrame()
            c.setObjectName("invcard")
            c.setStyleSheet(f"#invcard{{background:{theme.PANEL}; border:1px solid {col or theme.BORDER}; "
                            f"border-radius:8px;}}")
            cv = QVBoxLayout(c)
            cv.setContentsMargins(14, 10, 14, 12)
            cv.setSpacing(2)
            cap = QLabel(lab.upper())
            cap.setObjectName("h2")
            cv.addWidget(cap)
            n = QLabel(str(val))
            f = n.font()
            f.setPointSizeF(f.pointSizeF() + 9)
            f.setWeight(f.Weight.DemiBold)
            n.setFont(f)
            if col:
                n.setStyleSheet(f"color:{col}")
            cv.addWidget(n)
            grid.addWidget(c, i // 3, i % 3)
        v.addLayout(grid)
        v.addWidget(_section("Low Stock"))
        v.addWidget(self.parts_tree(low[:8], actions=False) if low else _muted("Nothing below its reorder point."))
        v.addWidget(_section("Recent Activity"))
        rec = list(reversed(inv.transactions[-6:]))
        v.addWidget(self.log_table(rec) if rec else _muted("No activity logged yet."))
        v.addStretch(1)
        return w

    # ============================================================ projects
    def build_projects(self):
        inv = self.inv
        w, v = self._page()
        projects = inv.active_projects()
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(projects)} project{'s' if len(projects) != 1 else ''} · qty per unit × the "
                             f"project's Total Qty Needed = total needed. Edit Qty/Unit right in the table."), 1)
        row.addWidget(_btn("Send BOM from design…", self.send_bom_requested.emit))
        row.addWidget(_btn("+ Add Project", self.add_project, primary=True))
        v.addLayout(row)
        if not projects:
            v.addWidget(_muted("No active projects. Add one, or send the open design's BOM."))
        for i, pr in enumerate(projects):
            s = inv.project_summary(pr["id"])
            hdr = QHBoxLayout()
            tog = _small("▸" if pr["id"] in self.collapsed else "▾", lambda _=False, i=pr["id"]: self.toggle(i))
            hdr.addWidget(tog)
            lab = QLabel(f"<b>{pr['name']}</b>  <span style='color:{theme.MUTED}'>· {s['parts']} part"
                         f"{'s' if s['parts'] != 1 else ''} · {M.fmt_money(s['value'])}"
                         + (f" · <span style='color:{theme.ERROR}'>{s['short']} short</span>" if s["short"] else "")
                         + f" · {s['units']:g} unit{'s' if s['units'] != 1 else ''}</span>")
            hdr.addWidget(lab, 1)
            if pr.get("folderPath"):
                hdr.addWidget(_small("📁", lambda _=False, f=pr["folderPath"]: _open_link(f), pr["folderPath"], icon="folder"))
            up = _small("▲", lambda _=False, i=pr["id"]: (inv.move_project(i, -1), self.commit()), "Move up")
            up.setEnabled(i > 0)
            dn = _small("▼", lambda _=False, i=pr["id"]: (inv.move_project(i, 1), self.commit()), "Move down")
            dn.setEnabled(i < len(projects) - 1)
            for b in (up, dn, _small("+ Add Parts", lambda _=False, i=pr["id"]: self.add_parts_to_project(i)),
                      _small("Duplicate", lambda _=False, i=pr["id"]: self.duplicate_project(i)),
                      _small("Edit", lambda _=False, i=pr["id"]: self.edit_project(i)),
                      _small("Archive", lambda _=False, i=pr["id"]: self.archive(i, True))):
                hdr.addWidget(b)
            v.addLayout(hdr)
            if pr["id"] in self.collapsed:
                continue
            rows = inv.project_rows(pr["id"])
            if not rows:
                v.addWidget(_muted("No parts assigned yet."))
                continue
            tr = []
            for p, depth, ql, need, on_hand, emb in rows:
                short = max(0.0, need - on_hand)
                name = QTableWidgetItem(("    " * depth) + (f"× {ql}  " if ql is not None else "") + p["name"])
                name.setToolTip(inv.spec_line(p))
                if depth == 0:
                    a = inv.allocation(p, pr["id"])
                    q = QSpinBox()
                    q.setRange(0, 1_000_000)
                    q.setValue(int(M.num(a.get("qty") if a else 0)))
                    q.editingFinished.connect(lambda q=q, pp=p, pid=pr["id"]: self.set_alloc(pp, pid, q.value()))
                else:
                    q = "—"
                acts = [_small("Use", lambda _=False, i=p["id"]: self.move(i, "usage")),
                        _small("Receive", lambda _=False, i=p["id"]: self.move(i, "receiving")),
                        _small("Edit", lambda _=False, i=p["id"]: self.edit_part(i))]
                if depth == 0:
                    acts.append(_small("Unassign", lambda _=False, pp=p, pid=pr["id"]: self.unassign(pp, pid),
                                       danger=True))
                tr.append([name, M.TYPE_LABEL.get(p.get("type"), ""), q, _num_item(need),
                           _num_item(on_hand, None) if not emb else _num_item(f"{on_hand:g} *"),
                           _num_item(f"−{short:g}" if short > 0 else "—", theme.ERROR if short > 0 else None,
                                     short > 0), M.fmt_money(p.get("unitCost")), _actions(*acts)])
            v.addWidget(self.table(["Part", "Type", "Qty/Unit", "Total Needed", "On Hand", "Short", "Unit Cost", ""],
                                   tr))
        v.addWidget(_section("Unassigned"))
        un = [p for p in inv.parts if not inv.allocations(p)]
        v.addWidget(self.parts_tree(sorted(un, key=lambda p: p["name"].lower())) if un
                    else _muted("Every part is assigned to at least one project."))
        arch = inv.archived_projects()
        if arch:
            hdr = QHBoxLayout()
            hdr.addWidget(_small("▸" if "__arch__" in self.collapsed else "▾", lambda: self.toggle("__arch__")))
            hdr.addWidget(_section(f"Archived · {len(arch)}"), 1)
            v.addLayout(hdr)
            if "__arch__" not in self.collapsed:
                for pr in arch:
                    h = QHBoxLayout()
                    h.addWidget(_muted(f"{pr['name']} · {len(inv.parts_for_project(pr['id']))} parts", False), 1)
                    h.addWidget(_small("Unarchive", lambda _=False, i=pr["id"]: self.archive(i, False)))
                    h.addWidget(_small("Delete", lambda _=False, i=pr["id"]: self.delete_project(i), danger=True))
                    v.addLayout(h)
        v.addStretch(1)
        return w

    def toggle(self, key):
        self.collapsed ^= {key}
        self.refresh_current()

    def set_alloc(self, p, pid, qty):
        a = self.inv.allocation(p, pid)
        if a and int(M.num(a.get("qty"))) == qty:
            return
        self.inv.set_allocation(p, pid, qty)
        self.commit()

    def unassign(self, p, pid):
        self.inv.unassign(p, pid)
        self.commit("Removed from project")

    def add_project(self):
        d = D.ProjectDialog(None, self)
        if d.exec():
            vals = d.values()
            pid = self.inv.find_or_create_project(vals.pop("name"), **vals)
            self.commit(f"Project {self.inv.project_name(pid)} added")

    def edit_project(self, pid):
        pr = self.inv.project(pid)
        d = D.ProjectDialog(pr, self)
        if d.exec():
            pr.update(d.values())
            self.commit("Project saved")

    def duplicate_project(self, pid):
        clone, n = self.inv.duplicate_project(pid)
        self.commit(f"Duplicated with {n} part(s)")
        self.edit_project(clone["id"])

    def archive(self, pid, on):
        self.inv.project(pid)["archived"] = on
        self.commit("Archived — see Archived below" if on else "Unarchived")

    def delete_project(self, pid):
        pr = self.inv.project(pid)
        n = len(self.inv.parts_for_project(pid))
        if QMessageBox.question(self, "Delete project", f"Permanently delete project “{pr['name']}”?"
                                + (f" It will be unassigned from {n} part(s)." if n else "")
                                + " This cannot be undone.") == QMessageBox.Yes:
            self.inv.delete_project(pid)
            self.commit("Project deleted")

    def add_parts_to_project(self, pid):
        d = D.AddPartsToProjectDialog(self.inv, pid, self)
        if d.exec():
            for part_id, on, qty in d.items():
                p = self.inv.part(part_id)
                if on:
                    self.inv.set_allocation(p, pid, qty)
                else:
                    self.inv.unassign(p, pid)
            self.commit("Project parts saved")

    # ========================================================== production
    def build_production(self):
        inv = self.inv
        w, v = self._page()
        orders = sorted(inv.productionOrders, key=lambda o: o.get("dueDate") or "")
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(orders)} production order{'s' if len(orders) != 1 else ''} · build quantity, "
                             "dates and fab/finishing progress."), 1)
        row.addWidget(_btn("+ Add Order", lambda: self.edit_order(None), primary=True))
        v.addLayout(row)
        if orders:
            rows = [[inv.project_name(o.get("projectId")) or "(deleted project)", _num_item(o.get("qty")),
                     M.fmt_date(o.get("orderDate")), M.fmt_date(o.get("dueDate")),
                     self.pill(o.get("status") or "Not Started", STATUS_COL.get(o.get("status"), theme.MUTED)),
                     _actions(_small("Edit", lambda _=False, oo=o: self.edit_order(oo)),
                              _small("Delete", lambda _=False, oo=o: self.delete_order(oo), danger=True))]
                    for o in orders]
            v.addWidget(self.table(["Project", "Qty", "Order Date", "Due Date", "Status", ""], rows))
            v.addWidget(_section("Fab & Finishing Timeline — by Project"))
            dated = [o for o in orders if any(o.get(k) for k in ("fabStart", "fabEnd", "finishStart", "finishEnd"))]
            if dated:
                groups = {}
                for o in dated:
                    groups.setdefault(o.get("projectId"), []).append(o)
                g = []
                for pid, os_ in groups.items():
                    allb = [(o.get("fabStart") or o.get("finishStart"), o.get("finishEnd") or o.get("fabEnd"),
                             theme.MUTED) for o in os_]
                    rows = [("Overall", [(min(b[0] for b in allb if b[0]), max(b[1] for b in allb if b[1]),
                                          theme.MUTED)] if any(b[0] for b in allb) and any(b[1] for b in allb) else [])]
                    for o in os_:
                        bars = []
                        if o.get("fabStart") and o.get("fabEnd"):
                            bars.append((o["fabStart"], o["fabEnd"], theme.INFO))
                        if o.get("finishStart") and o.get("finishEnd"):
                            bars.append((o["finishStart"], o["finishEnd"], theme.ACCENT_LINE))
                        rows.append((f"qty {o.get('qty')}" + (f" · due {M.fmt_date(o.get('dueDate'))}"
                                                              if o.get("dueDate") else ""), bars))
                    g.append((inv.project_name(pid) or "(deleted project)", rows))
                v.addWidget(Gantt(g, [("Overall", theme.MUTED), ("Fab", theme.INFO), ("Finishing", theme.ACCENT_LINE)]))
                v.addWidget(_muted("Red line = today"))
            else:
                v.addWidget(_muted("Add fab/finishing dates to an order to see it on the timeline."))
        else:
            v.addWidget(_muted("No production orders yet."))
        po = [o for o in inv.open_part_orders() if o.get("orderDate") and o.get("expectedDate")]
        if po:
            v.addWidget(_section("Parts On Order Timeline"))
            g = [("Parts on order", [(f"{(inv.part(o['partId']) or {}).get('name', '(deleted part)')} · qty {o['qty']}",
                                      [(o["orderDate"], o["expectedDate"], theme.PURPLE)]) for o in po])]
            v.addWidget(Gantt(g, [("Ordered → Expected", theme.PURPLE)]))
        v.addStretch(1)
        return w

    def edit_order(self, o):
        if not self.inv.active_projects() and not o:
            return self.toast.show_msg("Add a project first")
        d = D.OrderDialog(self.inv, o, self)
        if d.exec():
            if o:
                o.update(d.values())
            else:
                self.inv.productionOrders.append({"id": M.uid(), **d.values()})
            self.commit("Order saved")

    def delete_order(self, o):
        if QMessageBox.question(self, "Delete", f"Delete this production order for "
                                f"{self.inv.project_name(o.get('projectId')) or 'this project'}?") == QMessageBox.Yes:
            self.inv.productionOrders = [x for x in self.inv.productionOrders if x["id"] != o["id"]]
            self.commit("Order deleted")

    # ========================================================== locations
    def build_locations(self):
        inv = self.inv
        w, v = self._page()
        locs = inv.sorted_locations()
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(locs)} bin location{'s' if len(locs) != 1 else ''} · assign parts from their "
                             "Edit form."), 1)
        row.addWidget(_btn("+ Add Location", lambda: self.edit_location(None), primary=True))
        v.addLayout(row)
        if locs:
            rows = []
            for l in locs:
                size = next((b["label"] for b in M.BIN_SIZES if b["id"] == l.get("binSize")), "")
                if l.get("sections"):
                    assigned = "\n".join(f".{c}: " + (", ".join(p["name"] for p in inv.parts_at(l["id"], c)) or "Empty")
                                         for c in "abc")
                else:
                    assigned = ", ".join(p["name"] for p in inv.parts_at(l["id"])) or "Empty"
                rows.append([(l.get("name") or "—") + ("  (3 sections)" if l.get("sections") else ""), l.get("rack"),
                             l.get("shelf"), l.get("binPosition"), size, assigned,
                             _actions(_small("Edit", lambda _=False, ll=l: self.edit_location(ll)),
                                      _small("Delete", lambda _=False, ll=l: self.delete_location(ll), danger=True))])
            v.addWidget(self.table(["Name", "Rack", "Shelf", "Bin", "Size", "Assigned Part(s)", ""], rows, 5))
        else:
            v.addWidget(_muted("No bin locations set up yet."))
        v.addStretch(1)
        return w

    def edit_location(self, loc):
        d = D.LocationDialog(loc, self)
        if d.exec():
            if loc:
                loc.update(d.values())
            else:
                self.inv.locations.append({"id": M.uid(), **d.values()})
            self.commit("Location saved")

    def delete_location(self, loc):
        n = len(self.inv.parts_at(loc["id"]))
        if QMessageBox.question(self, "Delete", f"Delete location “{M.Inventory.location_label(loc)}”?"
                                + (f" {n} part(s) there will be unassigned." if n else "")) == QMessageBox.Yes:
            self.inv.delete_location(loc["id"])
            self.commit("Location deleted")

    # =============================================================== parts
    def build_parts(self):
        inv = self.inv
        f = self.parts_filter
        w, v = self._page()
        dupes = inv.duplicate_groups()
        if dupes:
            b = QHBoxLayout()
            lab = QLabel(f"⚠ {len(dupes)} possible duplicate part group{'s' if len(dupes) != 1 else ''} "
                         "(same name or part number)")
            lab.setStyleSheet(f"color:{theme.ERROR}")
            b.addWidget(lab, 1)
            b.addWidget(_small("Review", self.review_dupes))
            v.addLayout(b)
        row = QHBoxLayout()
        tsel = QComboBox()
        tsel.addItem("All types", "all")
        for k, lab in M.TYPES:
            tsel.addItem(lab, k)
        tsel.setCurrentIndex(max(0, tsel.findData(f["type"])))
        tsel.currentIndexChanged.connect(lambda _i: (f.__setitem__("type", tsel.currentData()), self.refresh_current()))
        psel = QComboBox()
        psel.addItem("All projects", "all")
        for pr in inv.active_projects():
            psel.addItem(pr["name"], pr["id"])
        psel.addItem("Unassigned", "unassigned")
        psel.setCurrentIndex(max(0, psel.findData(f["project"])))
        psel.currentIndexChanged.connect(lambda _i: (f.__setitem__("project", psel.currentData()),
                                                     self.refresh_current()))
        self.q = QLineEdit(f["q"])
        self.q.setPlaceholderText("Search name, #, supplier, size, project…")
        self.q.setClearButtonEnabled(True)
        self.q.textChanged.connect(self._search)
        row.addWidget(tsel)
        row.addWidget(psel)
        row.addWidget(self.q, 1)
        row.addWidget(_btn("+ Add Part", lambda: self.edit_part(None), primary=True))
        v.addLayout(row)
        if self.sel_parts:
            b = QHBoxLayout()
            b.addWidget(_muted(f"{len(self.sel_parts)} part{'s' if len(self.sel_parts) != 1 else ''} selected", False), 1)
            b.addWidget(_small("Clear", lambda: (self.sel_parts.clear(), self.refresh_current())))
            b.addWidget(_small("Assign to Project", self.bulk_assign))
            v.addLayout(b)
        lst = list(inv.parts)
        if f["type"] != "all":
            lst = [p for p in lst if p.get("type") == f["type"]]
        if f["project"] == "unassigned":
            lst = [p for p in lst if not inv.allocations(p)]
        elif f["project"] != "all":
            lst = [p for p in lst if inv.allocation(p, f["project"])]
        if f["q"]:
            q = f["q"].lower()
            lst = [p for p in lst if q in " ".join(str(x) for x in (
                p.get("name"), p.get("partNumber"), p.get("qbPartNumber"), *[s["name"] for s in inv.suppliers(p)],
                p.get("size"), p.get("headStyle"), p.get("threadStandard"), *inv.project_chips(p),
                inv.location_label_for(p.get("locationId")))).lower()]
        lst.sort(key=lambda p: p["name"].lower())
        v.addWidget(self.parts_tree(lst, actions=True, selectable=True) if lst
                    else _muted("No parts match. Add one, or clear the filters."))
        v.addStretch(1)
        QTimer.singleShot(0, lambda: (self.q.setFocus(), self.q.setCursorPosition(len(self.q.text())))
                          if f["q"] else None)
        return w

    def _search(self, text):
        self.parts_filter["q"] = text
        if not hasattr(self, "_qtimer"):
            self._qtimer = QTimer(self)
            self._qtimer.setSingleShot(True)
            self._qtimer.timeout.connect(self.refresh_current)
        self._qtimer.start(250)

    def edit_part(self, pid):
        part = self.inv.part(pid) if pid else None
        d = D.PartDialog(self.inv, part, self)
        if not d.exec():
            return
        if d.deleted:
            self.inv.delete_part(part["id"])
            return self.commit("Part deleted")
        vals = d.values()
        if part:
            part.update(vals)
            self.commit("Part saved")
        else:
            self.inv.parts.append(M.new_part(**vals))
            self.commit(f"Added {vals['name']}")

    def move(self, pid, mode):
        p = self.inv.part(pid)
        d = D.MoveDialog(p, mode, self)
        if d.exec():
            tx = self.inv.record(pid, mode, d.qty.value(), d.note.text().strip())
            self.commit("Usage recorded" if mode == "usage" else "Receiving recorded",
                        lambda: (self.inv.undo_transaction(tx), self.commit("Undone")))

    def bulk_assign(self):
        if not self.inv.active_projects():
            return self.toast.show_msg("Add a project first")
        d = D.ChooseProjectDialog(self.inv, len(self.sel_parts), self)
        if d.exec():
            pid = d.project.currentData()
            for i in self.sel_parts:
                p = self.inv.part(i)
                if p:
                    self.inv.set_allocation(p, pid, d.qty.value())
            n = len(self.sel_parts)
            self.sel_parts.clear()
            self.commit(f"Assigned {n} part(s) to {self.inv.project_name(pid)}")

    def review_dupes(self):
        D.DuplicatesDialog(self.inv, lambda msg: self.commit(msg), self).exec()

    # ============================================================= vendors
    def build_vendors(self):
        inv = self.inv
        w, v = self._page()
        vs = inv.sorted_vendors()
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(vs)} vendor{'s' if len(vs) != 1 else ''} across fab, fastener and finishing."), 1)
        row.addWidget(_btn("+ Add Vendor", lambda: self.edit_vendor(None), primary=True))
        v.addLayout(row)
        for cat in M.VENDOR_CATEGORIES:
            lst = [x for x in vs if (x.get("category") or "Other") == cat]
            if not lst:
                continue
            v.addWidget(_section(cat))
            rows = []
            for x in lst:
                email = _small(x["email"], lambda _=False, e=x["email"]: _open_link("mailto:" + e)) if x.get("email") \
                    else "—"
                web = _small("Visit", lambda _=False, u=x["website"]: _open_link(u)) if x.get("website") else "—"
                rows.append([x["name"], x.get("contactName") or "—", x.get("phone") or "—", email, web,
                             inv.lead_label(inv.lead_time_range(x["name"])) or "—", x.get("notes") or "",
                             _actions(_small("Edit", lambda _=False, vv=x: self.edit_vendor(vv)),
                                      _small("Delete", lambda _=False, vv=x: self.delete_vendor(vv), danger=True))])
            v.addWidget(self.table(["Name", "Contact", "Phone", "Email", "Website", "Lead Time", "Notes", ""], rows, 6))
        if not vs:
            v.addWidget(_muted("No vendors yet."))
        v.addWidget(_section("Scorecard"))
        sc = inv.scorecard()
        if sc:
            v.addWidget(_muted("On-time rate compares each received part order's order-to-received time with the "
                               "vendor's promised lead time (high end of its range)."))
            rows = [[s["vendorName"], inv.lead_label(s["range"]) if s["range"] else "Not set", _num_item(s["orderCount"]),
                     _num_item(f"{s['avgDays']:.1f}" if s["avgDays"] is not None else "—"),
                     _num_item(f"{s['onTimeRate']}%" if s["onTimeRate"] is not None else "—",
                               theme.ERROR if s["onTimeRate"] is not None and s["onTimeRate"] < 70 else None)]
                    for s in sc]
            v.addWidget(self.table(["Vendor", "Promised", "Orders", "Avg. Actual Days", "On-Time Rate"], rows))
        else:
            v.addWidget(_muted("No received part orders yet — fills in once orders placed from Reorder are received."))
        v.addStretch(1)
        return w

    def edit_vendor(self, x):
        d = D.VendorDialog(x, self)
        if d.exec():
            if x:
                x.update(d.values())
            else:
                self.inv.vendors.append({"id": M.uid(), **d.values()})
            self.commit("Vendor saved")

    def delete_vendor(self, x):
        if QMessageBox.question(self, "Delete", f"Delete vendor “{x['name']}”?") == QMessageBox.Yes:
            self.inv.vendors = [y for y in self.inv.vendors if y["id"] != x["id"]]
            self.commit("Vendor deleted")

    # ============================================================= clients
    def build_clients(self):
        inv = self.inv
        w, v = self._page()
        orders = sorted(inv.clientOrders, key=lambda o: o.get("dueDate") or "")
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(orders)} client order{'s' if len(orders) != 1 else ''} · which project each "
                             "client wants, how many, and by when."), 1)
        row.addWidget(_btn("+ Add Client Order", lambda: self.edit_client_order(None), primary=True))
        v.addLayout(row)
        if orders:
            rows = []
            for o in orders:
                c = inv.client(o.get("clientId")) or {}
                lines = "\n".join(f"{inv.project_name(l.get('projectId')) or '(deleted project)'} ×{l.get('qty')}"
                                  for l in o.get("projectLines") or []) or "—"
                rows.append([c.get("name") or "(deleted client)",
                             " · ".join(x for x in (c.get("contactName"), c.get("phone"), c.get("email")) if x) or "—",
                             lines, M.fmt_date(o.get("orderDate")), M.fmt_date(o.get("dueDate")),
                             self.pill(o.get("status") or "Requested", STATUS_COL.get(o.get("status"), theme.MUTED)),
                             _actions(_small("Edit", lambda _=False, oo=o: self.edit_client_order(oo)),
                                      _small("Delete", lambda _=False, oo=o: self.delete_client_order(oo),
                                             danger=True))])
            v.addWidget(self.table(["Client", "Contact", "Projects", "Order Date", "Due Date", "Status", ""], rows, 2))
        else:
            v.addWidget(_muted("No client orders yet."))
        v.addStretch(1)
        return w

    def edit_client_order(self, o):
        if not self.inv.projects and not o:
            return self.toast.show_msg("Add a project first")
        d = D.ClientOrderDialog(self.inv, o, self)
        if d.exec():
            if o:
                o.update(d.values())
            else:
                self.inv.clientOrders.append({"id": M.uid(), **d.values()})
            self.commit("Client order saved")

    def delete_client_order(self, o):
        if QMessageBox.question(self, "Delete", "Delete this client order?") == QMessageBox.Yes:
            self.inv.clientOrders = [x for x in self.inv.clientOrders if x["id"] != o["id"]]
            self.commit("Client order deleted")

    # ============================================================= reorder
    def build_reorder(self):
        inv = self.inv
        f = self.reorder_filter
        w, v = self._page()
        all_low = inv.low_stock()
        if all_low:
            row = QHBoxLayout()
            ps = QComboBox()
            ps.addItem("All projects", "all")
            for pr in inv.projects_with_low():
                ps.addItem(pr["name"], pr["id"])
            ps.setCurrentIndex(max(0, ps.findData(f["project"])))
            ps.currentIndexChanged.connect(lambda _i: (f.__setitem__("project", ps.currentData()),
                                                       self.refresh_current()))
            vs = QComboBox()
            vs.addItem("All vendors", "all")
            for n in inv.vendors_among_low():
                vs.addItem(n, n)
            vs.setCurrentIndex(max(0, vs.findData(f["vendor"])))
            vs.currentIndexChanged.connect(lambda _i: (f.__setitem__("vendor", vs.currentData()),
                                                       self.refresh_current()))
            lst = inv.reorder_list(f["project"], f["vendor"])
            row.addWidget(ps)
            row.addWidget(vs)
            row.addWidget(_muted(f"{len(lst)} of {len(all_low)} parts", False), 1)
            row.addWidget(_btn("Export All (per project)", self.export_reorder_by_project))
            row.addWidget(_btn("Export List (.xlsx)", self.export_reorder, primary=True))
            v.addLayout(row)
            if self.sel_reorder:
                b = QHBoxLayout()
                b.addWidget(_muted(f"{len(self.sel_reorder)} selected", False), 1)
                b.addWidget(_small("Clear", lambda: (self.sel_reorder.clear(), self.refresh_current())))
                b.addWidget(_small("Batch Order", lambda: self.batch("order")))
                b.addWidget(_small("Batch Receive", lambda: self.batch("receive")))
                v.addLayout(b)
            rows = []
            for p in lst:
                best = inv.best_supplier(p)
                sups = [s for s in inv.suppliers(p) if f["vendor"] == "all" or s["name"] == f["vendor"]]
                need = inv.needed_for_project(p, f["project"]) if f["project"] != "all" else inv.total_needed(p)
                sq = inv.suggested_qty(p, need)
                unit = inv.to_cad(best["price"], best.get("currency")) if best else \
                    inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
                cb = QCheckBox()
                cb.setChecked(p["id"] in self.sel_reorder)
                cb.toggled.connect(lambda on, i=p["id"]: (self.sel_reorder.add(i) if on else self.sel_reorder.discard(i),
                                                          QTimer.singleShot(0, self.refresh_current)))
                sup_w = QWidget()
                sl = QVBoxLayout(sup_w)
                sl.setContentsMargins(0, 0, 0, 0)
                for s in sups:
                    txt = f"{s['name']}  {M.fmt_money(s.get('price'))} {s.get('currency') or 'CAD'}" + \
                        (f"  ({M.fmt_money(inv.to_cad(s.get('price'), 'USD'))} CAD)" if s.get("currency") == "USD" else "") + \
                        (f" · {s['partNumber']}" if s.get("partNumber") else "") + \
                        ("  · best" if best and s["name"] == best["name"] and s.get("price") == best.get("price") else "")
                    if s.get("website"):
                        sl.addWidget(_small(txt, lambda _=False, u=s["website"]: _open_link(u), s["website"]))
                    else:
                        sl.addWidget(QLabel(txt))
                if not sups:
                    sl.addWidget(_muted("—", False))
                name = QTableWidgetItem(p["name"])
                name.setToolTip(inv.spec_line(p))
                rows.append([cb, name, M.TYPE_LABEL.get(p.get("type"), ""),
                             _num_item(f"{M.num(p.get('currentStock')):g} {p.get('unit') or ''}", theme.ERROR),
                             _num_item(M.num(p.get("reorderPoint"))),
                             _num_item(need if need else "—", theme.ERROR if need > M.num(p.get("currentStock")) else None),
                             _num_item(sq), sup_w, _num_item(M.fmt_money(sq * unit)),
                             _actions(_small("Order", lambda _=False, pp=p, q=sq: self.order_part(pp, q)),
                                      _small("Mark Received", lambda _=False, i=p["id"]: self.move(i, "receiving")))])
            nh = "Needed (this project)" if f["project"] != "all" else "Needed (projects)"
            v.addWidget(self.table(["", "Part", "Type", "On Hand", "Reorder Pt", nh, "Suggested Qty", "Suppliers",
                                    "Est. Cost", ""], rows, 7) if rows else _muted("No parts match this filter."))
        else:
            v.addWidget(_muted("Everything is above its reorder point."))
        oo = inv.open_part_orders()
        if oo:
            v.addWidget(_section("Parts On Order"))
            rows = [[(inv.part(o.get("partId")) or {}).get("name", "(deleted part)"), _num_item(o.get("qty")),
                     o.get("vendorName") or "—", M.fmt_date(o.get("orderDate")), M.fmt_date(o.get("expectedDate")),
                     _actions(_small("Mark Received", lambda _=False, i=o["id"]: self.receive_order(i)),
                              _small("Cancel", lambda _=False, oo=o: self.cancel_order(oo), danger=True))]
                    for o in oo]
            v.addWidget(self.table(["Part", "Qty", "Vendor", "Order Date", "Expected", ""], rows))
        v.addStretch(1)
        return w

    def order_part(self, p, qty):
        d = D.PartOrderDialog(self.inv, p, qty, self)
        if d.exec():
            q, od, ed = d.values()
            self.inv.place_order(p["id"], q, od, ed)
            self.commit("Order placed")

    def receive_order(self, oid):
        self.inv.receive_order(oid)
        self.commit("Received — stock updated")

    def cancel_order(self, o):
        if QMessageBox.question(self, "Cancel order", "Cancel this part order?") == QMessageBox.Yes:
            o["status"] = "Cancelled"
            self.commit("Order cancelled")

    def batch(self, mode):
        parts = [self.inv.part(i) for i in self.sel_reorder if self.inv.part(i)]
        if not parts:
            return
        d = D.BatchDialog(self.inv, parts, mode, self)
        if not d.exec():
            return
        n = 0
        if mode == "receive":
            txs = []
            for pid, q in d.items():
                if q > 0:
                    txs.append(self.inv.record(pid, "receiving", q, d.note.text().strip()))
                    n += 1
            self.sel_reorder.clear()
            self.commit(f"Received {n} part(s)", lambda: ([self.inv.undo_transaction(t) for t in txs],
                                                          self.commit("Undone")))
        else:
            for pid, q in d.items():
                if q > 0:
                    self.inv.place_order(pid, q, D._date_value(d.od), D._date_value(d.ed))
                    n += 1
            self.sel_reorder.clear()
            self.commit(f"Placed {n} order(s)")

    def export_reorder(self):
        inv = self.inv
        f = self.reorder_filter
        lst = inv.reorder_list(f["project"], f["vendor"])
        if not lst:
            return self.toast.show_msg("Nothing to export with the current filters")
        need = (lambda p: inv.needed_for_project(p, f["project"])) if f["project"] != "all" else inv.total_needed
        rows = inv.reorder_rows(lst, need, f["vendor"] if f["vendor"] != "all" else None)
        path = self._save_path(f"holocron-reorder-{M.today()}.xlsx", "Excel (*.xlsx)")
        if path:
            name = (inv.project_name(f["project"]) or "Reorder") if f["project"] != "all" else "Reorder List"
            M.write_xlsx(path, [(name, rows)])
            self.toast.show_msg(f"Exported {len(rows)} part(s)")

    def export_reorder_by_project(self):
        inv = self.inv
        low = inv.low_stock()
        if not low:
            return self.toast.show_msg("Nothing to export — everything is above its reorder point")
        path = self._save_path(f"holocron-reorder-by-project-{M.today()}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        sheets = []
        for pr in inv.projects_with_low():
            parts = [p for p in low if inv.allocation(p, pr["id"])]
            sheets.append((pr["name"], inv.reorder_rows(parts, lambda p, pid=pr["id"]: inv.needed_for_project(p, pid))))
        sheets.append(("All", inv.reorder_rows(low, inv.total_needed)))
        M.write_xlsx(path, sheets)
        self.toast.show_msg("Exported reorder list by project")

    def _save_path(self, name, filt):
        d = self.settings.value("inventory/export_dir") or str(Path.home())
        p, _ = QFileDialog.getSaveFileName(self, "Save", str(Path(d) / name), filt)
        if p:
            self.settings.setValue("inventory/export_dir", str(Path(p).parent))
        return p

    # ============================================================ shortages
    def build_shortages(self):
        inv = self.inv
        w, v = self._page()
        rows = inv.shortages()
        if not rows:
            v.addWidget(_muted("No cross-project shortages — every part's combined demand across its projects is "
                               "covered by stock."))
        else:
            v.addWidget(_muted("Total demand for a part across every project it's on, compared with stock — catches "
                               "projects that each look fine alone but compete for the same parts."))
            t = []
            for r in rows:
                p = r["part"]
                name = QTableWidgetItem(p["name"])
                name.setToolTip(inv.spec_line(p))
                comp = "\n".join(f"{inv.project_name(c['projectId']) or '(deleted project)'} needs {c['needed']:g}"
                                 for c in r["contributing"])
                t.append([name, _num_item(f"{M.num(p.get('currentStock')):g} {p.get('unit') or ''}"),
                          _num_item(r["totalNeeded"]), _num_item(f"−{r['short']:g}", theme.ERROR, True), comp,
                          _actions(_small("Use", lambda _=False, i=p["id"]: self.move(i, "usage")),
                                   _small("Receive", lambda _=False, i=p["id"]: self.move(i, "receiving")),
                                   _small("Order", lambda _=False, pp=p, s=r["short"]: self.order_part(pp, int(s))),
                                   _small("Edit", lambda _=False, i=p["id"]: self.edit_part(i)))])
            v.addWidget(self.table(["Part", "On Hand", "Total Needed", "Short", "Competing Projects", ""], t, 4))
        v.addStretch(1)
        return w

    # ================================================================= log
    def log_table(self, entries):
        rows = []
        for t in entries:
            used = t.get("mode") == "usage"
            act = QTableWidgetItem("Used" if used else ("Received" if t.get("mode") == "receiving" else "Adjusted"))
            act.setForeground(QBrush(QColor(theme.ERROR if used else theme.OK)))
            rows.append([dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%Y-%m-%d %H:%M"), t.get("partName"), act,
                         _num_item(f"{'-' if used else '+'}{t.get('qty')}"), t.get("by") or "", t.get("note") or ""])
        return self.table(["When", "Part", "Action", "Qty", "By", "Note"], rows, 1)

    def build_log(self):
        w, v = self._page()
        e = list(reversed(self.inv.transactions))
        v.addWidget(self.log_table(e) if e else _muted("No activity logged yet."))
        v.addStretch(1)
        return w

    # ========================================================= import/export
    def build_sync(self):
        inv = self.inv
        m = inv.syncMeta or {}
        w, v = self._page()

        def fmt(ts):
            return dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else "never"
        grid = QGridLayout()
        for i, (lab, ts, by) in enumerate((("Last imported", m.get("lastImportAt"), m.get("lastImportBy")),
                                           ("Last exported", m.get("lastExportAt"), m.get("lastExportBy")))):
            c = QFrame()
            c.setObjectName("invcard")
            c.setStyleSheet(f"#invcard{{background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:8px;}}")
            cv = QVBoxLayout(c)
            cv.addWidget(_muted(lab.upper(), False))
            cv.addWidget(QLabel(fmt(ts)))
            cv.addWidget(_muted(f"by {by}" if by else "", False))
            grid.addWidget(c, 0, i)
        v.addLayout(grid)
        v.addWidget(_section("Data file"))
        row = QHBoxLayout()
        path = QLineEdit(str(self.store.path))
        path.setReadOnly(True)
        row.addWidget(path, 1)
        row.addWidget(_btn("Open folder", lambda: _open_link(str(self.store.path.parent))))
        row.addWidget(_btn("Use another file…", self.choose_file, "Point at a shared file (e.g. on a network "
                                                                   "drive) — everyone using it sees the same data"))
        v.addLayout(row)
        v.addWidget(_muted("Everything is saved to this file as you work. Put it on a shared drive and point each "
                           "Daedalus at it to work as a team; changes from other PCs appear within a few seconds."))
        v.addWidget(_section("Settings"))
        row = QHBoxLayout()
        row.addWidget(_muted("USD → CAD exchange rate for totals (stock value, project value, reorder cost)."), 1)
        rate = QDoubleSpinBox()
        rate.setRange(0.01, 100)
        rate.setDecimals(4)
        rate.setValue(inv.exchangeRate)
        row.addWidget(rate)
        row.addWidget(_btn("Save Rate", lambda: (setattr(inv, "exchangeRate", rate.value()),
                                                 self.commit("Exchange rate saved"))))
        v.addLayout(row)
        v.addWidget(_section("Import from Excel / CSV"))
        row = QHBoxLayout()
        row.addWidget(_muted("Columns: Type, Name, Part Number, QuickBooks Part #, Drawing URL, Category, Head Style, "
                             "Thread Type, Thread Standard, Size, Length, Unit, Current Stock, Reorder Point, Reorder "
                             "Qty, Supplier A–C Name/Price/Currency/Part #/Website, Project (Name:qty; …), Unit Cost, "
                             "Unit Cost Currency, Rack, Shelf, Bin Size, Bin Position, Bin Section, Weldment, "
                             "Components (Name xQty; …). Existing parts (same name and type) are updated."), 1)
        row.addWidget(_btn("Import…", self.import_file, primary=True))
        v.addLayout(row)
        v.addWidget(_section("Master workbook"))
        row = QHBoxLayout()
        row.addWidget(_muted("Inventory + Activity Log sheets, ready to save on the network drive or reopen later."), 1)
        row.addWidget(_btn("Export Master Copy (.xlsx)", lambda: self.export("xlsx"), primary=True))
        row.addWidget(_btn("Export CSV", lambda: self.export("csv")))
        v.addLayout(row)
        v.addStretch(1)
        return w

    def choose_file(self):
        p, _ = QFileDialog.getSaveFileName(self, "Inventory data file", str(self.store.path),
                                           "Inventory (*.json)", options=QFileDialog.DontConfirmOverwrite)
        if not p:
            return
        if not p.lower().endswith(".json"):
            p += ".json"
        existed = Path(p).exists()
        self.store.move_to(p)
        self.settings.setValue("inventory/path", p)
        self.commit("Opened " + Path(p).name if existed else "Data moved to " + Path(p).name)

    def import_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import", self.settings.value("inventory/export_dir") or "",
                                           "Spreadsheets (*.xlsx *.xlsm *.csv)")
        if not p:
            return
        before = self.inv.snapshot()
        try:
            a, u = self.inv.import_file(p)
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Import", f"Couldn't read that file:\n{e}")

        def undo():
            self.inv.load_values(before)
            self.commit("Import undone")
        self.commit(f"Import done: {a} added, {u} updated", undo)

    def export(self, kind):
        if not self.inv.parts:
            return self.toast.show_msg("No parts to export")
        p = self._save_path(f"holocron-inventory-{M.today()}.{kind}", "Excel (*.xlsx)" if kind == "xlsx" else "CSV (*.csv)")
        if p:
            self.inv.export_file(p)
            self.commit(f"Exported {len(self.inv.parts)} part(s)")

    # ================================================================ misc
    def set_name(self):
        name, ok = QInputDialog.getText(self, "Who's using this?", "Your name (for the activity log):",
                                        text=self.inv.userName)
        if ok and name.strip():
            self.inv.userName = name.strip()
            self.settings.setValue("inventory/user", name.strip())
            self.refresh()

    def guide(self):
        QMessageBox.information(self, "Holocron inventory", D.GUIDE)

    def keyPressEvent(self, e):
        if e.text() == "/" and not isinstance(QApplication.focusWidget(), (QLineEdit,)):
            self.goto("parts")
            QTimer.singleShot(50, lambda: getattr(self, "q", None) and self.q.setFocus())
            return
        super().keyPressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.move(self.width() - self.toast.width() - 20, self.height() - self.toast.height() - 20)
