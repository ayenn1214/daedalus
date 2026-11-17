"""DIMS (Daedalus Inventory Management System) page — native Qt (dashboard, projects, production, locations, parts, vendors, clients,
reorder (incl. project shortages), activity log, import / export)."""
from __future__ import annotations

import datetime as dt
import os
import re
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
from . import orders_ui as OU
from .chat_panel import LINK_ROLE, tag
from . import theme

LEVEL = {"red": theme.ERROR, "amber": theme.WARN, "green": theme.OK}
ORDER_RANK = {"Draft": 0, "Confirmed": 1, "Shipped": 2, "Cancelled": 3}
ORDER_COL = {"Draft": theme.WARN, "Confirmed": theme.INFO, "Shipped": theme.OK, "Cancelled": theme.MUTED}
STATUS_COL = {"Not Started": theme.MUTED, "In Fab": theme.INFO, "In Finishing": theme.ACCENT_LINE, "Complete": theme.OK,
              "Requested": theme.MUTED, "Confirmed": theme.INFO, "In Production": theme.ACCENT_LINE, "Shipped": theme.PURPLE,
              "Cancelled": theme.MUTED}


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


REORDER_TYPES = (("all", "All types"), ("manufactured", "Manufactured only"), ("fastener", "Fasteners only"),
                 ("purchased", "Purchased only"))


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

PROJECT_GROUPS = ("MFG parts", "Purchased parts", "Fasteners", "Other")


def project_group(p) -> str:
    return {"manufactured": "MFG parts", "purchased": "Purchased parts",
            "fastener": "Fasteners"}.get(p.get("type"), "Other")


class InventoryPage(QWidget):
    closed = Signal()
    status = Signal(str)
    _fetched = Signal(object)                 # background team-server check → UI thread
    backup_due = Signal(bool)                 # weekly Excel backup reminder on / off
    send_bom_requested = Signal()
    insert_std_requested = Signal(str)        # standard part id → insert into the open model

    TABS = [("dashboard", "Dashboard"), ("projects", "Projects"), ("production", "Production"),
            ("locations", "Locations"), ("parts", "Parts"), ("vendors", "Vendors"), ("clients", "Orders"),
            ("reorder", "Reorder"),
            ("timephased", "Time-Phased"), ("analytics", "Analytics"), ("log", "Activity Log"),
            ("sync", "Import / Export")]

    def __init__(self, store: InventoryStore | None = None, parent=None, lite: bool = False):
        super().__init__(parent)
        self.lite = lite                  # inventory module on its own: no 3D model to send a BOM from
        self.settings = QSettings("Daedalus", "Daedalus")
        path = self.settings.value("inventory/path") or None
        if store is None:
            from ..inventory.backends import open_store
            store = open_store(self.settings, path)
        self.store = store
        self.store.inv.userName = self.settings.value("inventory/user") or os.environ.get("USERNAME") or \
            os.environ.get("USER") or ""
        self.parts_filter = {"type": "all", "project": "all", "q": "", "vendor": "all", "sort": "name",
                             "discontinued": False}
        self.analytics_state: dict = {}
        self.mrp_state: dict = {}
        self.parts_sel = None                       # part open in the Parts profile pane
        from .inventory_extras_ui import ThumbLoader
        self.thumbs = ThumbLoader(self)
        self.thumbs.ready.connect(lambda _src: self._thumb_refresh())
        self.reorder_filter = {"project": "all", "vendor": "all", "why": "all", "type": "all"}
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
        if not lite:
            h.addWidget(_btn("◀ 3D model", self.closed.emit))
        title = QLabel("⬡  DIMS · Daedalus Inventory Management System")
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
        self.icarus_btn = _btn("✦ Icarus", self.toggle_icarus, "Ask Icarus about the inventory — stock, costs, "
                               "trends, orders — or have it record changes for you")
        self.icarus_btn.setCheckable(True)
        h.addWidget(self.icarus_btn)
        self.chat_btn = _btn("💬 Chat", self.toggle_chat, "Team chat — Shift-click a part, project or order to "
                             "link it in a message")
        self.chat_btn.setCheckable(True)
        h.addWidget(self.chat_btn)
        h.addWidget(_btn("▦", self.table_layout, "Table layout: row and column spacing, grid lines, text size, "
                                               "column widths"))
        h.addWidget(_btn("?", self.guide, "User guide"))
        h.addWidget(_btn("Report bug", self.report_bug, "Report a bug or ask for a feature"))
        self.who = _btn("", self.set_name, "Your name on the activity log")
        h.addWidget(self.who)
        lay.addWidget(top)
        self.backup_bar = QFrame()
        self.backup_bar.setObjectName("invbackup")
        self.backup_bar.setStyleSheet(f"#invbackup{{background:{theme.PANEL}; border-bottom:2px solid {theme.WARN};}}")
        bh = QHBoxLayout(self.backup_bar)
        bh.setContentsMargins(10, 5, 10, 5)
        self.backup_lab = QLabel()
        bh.addWidget(self.backup_lab, 1)
        bh.addWidget(_btn("Download Excel backup", self.download_backup, "Saves the full inventory as an Excel "
                          "workbook and clears this reminder for everyone", primary=True))
        bh.addWidget(_btn("Later", self.snooze_backup, "Hide for 2 hours on this PC"))
        self.backup_bar.hide()
        self._backup_on = None
        self._snooze_until = 0.0
        lay.addWidget(self.backup_bar)
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
        from PySide6.QtWidgets import QSplitter
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.tabs)
        self.icarus = None
        from .chat_panel import ChatPanel
        self.chat = ChatPanel(self)
        self.chat.setMinimumWidth(300)
        self.chat.hide()
        self.chat.open_link.connect(self.open_link)
        self.chat.unread_changed.connect(self._chat_unread)
        self.split.addWidget(self.chat)
        lay.addWidget(self.split, 1)
        QApplication.instance().installEventFilter(self)
        if self.settings.value("dims/chat_visible", False, type=bool):
            QTimer.singleShot(0, lambda: self.toggle_chat(True))
        if self.settings.value("dims/icarus_visible", False, type=bool):
            QTimer.singleShot(0, lambda: self.toggle_icarus(True))
        self.toast = Toast(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(3000)
        self._committing = False
        self._bg = None
        self._fetched.connect(self._on_fetched)
        self.store.listeners.append(self._on_store_saved)
        self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        st = self.store
        if getattr(st, "kind", "file") == "supabase" and not st.signed_in and not getattr(self, "_asked", False):
            self._asked = True
            QTimer.singleShot(0, lambda: self.team_sign_in(reason="Sign in to the team inventory."))

    def _on_store_saved(self):
        if not self._committing:
            QTimer.singleShot(0, self.refresh)

    # ---------------------------------------------------------------- core
    @property
    def inv(self) -> M.Inventory:
        return self.store.inv

    def commit(self, msg=None, undo=None):
        from ..inventory.supabase import AuthError, SyncError
        self._committing = True
        try:
            self.store.save()
        except AuthError:
            self._committing = False
            self.team_sign_in(reason="Your session ended — sign in again to send your changes.")
        except SyncError as e:
            if type(e).__name__ == "PermissionDenied":
                QMessageBox.warning(self, "DIMS", f"{e}.\nYour change was not saved.")
                if hasattr(self.store, "discard_pending"):
                    self.store.discard_pending()
                self._committing = False
                self.refresh()
                return
            msg = (msg + " — " if msg else "") + "saved on this PC, will sync when the team server is back"
        except OSError as e:
            QMessageBox.warning(self, "Inventory", f"Couldn't save the inventory file:\n{e}")
        finally:
            self._committing = False
        conf = getattr(self.store, "conflicts", None)
        if conf:
            names = ", ".join(sorted({str(c[0]) for c in conf})[:4])
            self.status.emit(f"Merged with a teammate's changes ({names}) — the newest edit to the same field won")
            conf.clear()
        self.refresh()
        if msg:
            self.toast.show_msg(msg, undo)
            self.status.emit(msg)

    def _poll(self):
        self._update_backup()
        if QApplication.activeModalWidget() is not None:
            return
        if not self.isVisible():                  # hidden: still pick up teammates' changes, every 30 s
            self._hidden_ticks = getattr(self, "_hidden_ticks", 0) + 1
            if self._hidden_ticks % 10:
                return
        if getattr(self.store, "kind", "file") == "supabase":
            if self._bg is None and self.store.signed_in:       # network on a thread; merge on the UI thread
                import threading
                st = self.store

                def work():
                    try:
                        self._fetched.emit(("ok", st, st.fetch_changes()))
                    except Exception as e:  # noqa: BLE001
                        self._fetched.emit(("err", st, e))
                self._bg = threading.Thread(target=work, daemon=True)
                self._bg.start()
            return
        if self.store.check_external():
            self.refresh()

    def _on_fetched(self, res):
        kind, st, payload = res
        self._bg = None
        if st is not self.store:
            return
        if kind == "err":
            st.last_error = str(payload)
            self._sync_status()
            return
        changed = st.apply_changes(payload)
        if st.pending():
            from ..inventory.supabase import SyncError
            try:
                st.save()
                changed = True
            except SyncError as e:
                st.last_error = str(e)
        if changed:
            self.refresh()
        else:
            self._sync_status()

    def goto(self, key):
        if key == "engineering":                     # merged into Parts
            key = "parts"
        if key == "shortages":                       # merged into Reorder: show only the project shortages
            self.reorder_filter["why"] = "short"
            key = "reorder"
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
        from ..inventory import orders as O
        counts = {"projects": len(inv.projects),
                  "production": sum(O.is_unread(p, inv.userName) for p in inv.productionOrders
                                    if p.get("status") not in O.CLOSED_PROD),
                  "locations": len(inv.locations), "parts": len(inv.parts),
                  "vendors": len(inv.vendors), "clients": sum(O.status(o) in (O.DRAFT, O.CONFIRMED) for o in inv.clientOrders),
                  "reorder": low,
                  "shortages": len(inv.shortages())}
        for i, (k, lab) in enumerate(self.TABS):
            self.tabs.setTabText(i, f"{lab}  {counts[k]}" if k in counts else lab)
        self._sync_status()
        self.refresh_current()

    def _sync_status(self):
        st = self.store
        t = st.saved_at()
        ts = dt.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "not saved yet"
        color, tip = theme.MUTED, st.location()
        if getattr(st, "kind", "file") == "supabase":
            pend = st.pending()
            if not st.signed_in:
                text, color = "● Team server · sign in", theme.ERROR
            elif pend or not st.online:
                text, color = f"● Team server · offline — {len(pend)} change(s) waiting", theme.WARN \
                    if hasattr(theme, "WARN") else "#e0a030"
            else:
                text, color = f"● Team · {st.email} · synced {ts}", theme.OK if hasattr(theme, "OK") else "#4caf50"
            if st.last_error:
                tip += "\n" + st.last_error
        else:
            text = f"● {st.label()} · {ts}"
        self.sync_lab.setText(text)
        self.sync_lab.setToolTip(tip)
        self.sync_lab.setStyleSheet(f"color:{color}")
        self._update_backup()

    # -------------------------------------------------------------- weekly backup
    def _update_backup(self):
        import time as _t

        from ..inventory import backup as BK
        due = BK.is_due(self.inv.syncMeta or {})
        last = BK.last_backup(self.inv.syncMeta or {})
        if due:
            when = (f"last one {dt.datetime.fromtimestamp(last['at'] / 1000):%a %b %d} by {last['by']}" if last
                    else "none yet")
            self.backup_lab.setText(f"📦  Weekly Excel backup is due — nobody has downloaded this week's yet "
                                    f"({when}). One download clears it for everyone.")
        self.backup_bar.setVisible(due and _t.time() >= self._snooze_until)
        if due != self._backup_on:
            self._backup_on = due
            self.backup_due.emit(due)

    def snooze_backup(self):
        import time as _t
        self._snooze_until = _t.time() + 2 * 3600
        self.backup_bar.hide()

    def download_backup(self):
        from ..inventory import backup as BK
        d = self.settings.value("inventory/backup_dir") or self.settings.value("inventory/export_dir") or \
            str(Path.home())
        p, _ = QFileDialog.getSaveFileName(self, "Weekly inventory backup", str(Path(d) / BK.default_name()),
                                           "Excel (*.xlsx)")
        if not p:
            return
        if not p.lower().endswith(".xlsx"):
            p += ".xlsx"
        self.settings.setValue("inventory/backup_dir", str(Path(p).parent))
        try:
            BK.write_backup(self.inv, p)
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Backup", f"Couldn't write the backup:\n{e}")
        BK.mark_done(self.inv, p)
        self.commit(f"Backup saved: {Path(p).name} — reminder cleared for everyone")

    def refresh_current(self):
        key = self.current_key()
        br = getattr(self, "_browser", None)
        if key == "parts" and br is not None and br.dirty():
            try:                                     # unsaved edits in the profile: don't wipe them
                br.isVisible()
                self.status.emit("Changes arrived — save or revert the part you're editing to see them")
                return
            except RuntimeError:
                pass
        sc = self.pages[key]
        y = sc.verticalScrollBar().value()
        w = getattr(self, f"build_{key}")()
        w.setObjectName("invpage")
        from . import table_prefs as TP
        TP.apply_all(w, self.settings)                # row / column spacing, grid lines, remembered widths
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
        cols = (["", ] if selectable else []) + ["Part", "Type", "Project", "Vendor", "Stock", "Level", "Reorder Pt",
                                                  "Unit Cost"] + (["Actions"] if actions else [])
        t = QTreeWidget()
        t.setColumnCount(len(cols))
        t.setHeaderLabels(cols)
        t.setRootIsDecorated(True)
        t.setAlternatingRowColors(True)
        from PySide6.QtCore import QSize
        t.setIconSize(QSize(36, 36))
        off = 1 if selectable else 0

        def add(parent, p, depth, qty_label, stock_override):
            it = QTreeWidgetItem(parent)
            emb = stock_override is not None
            stock = stock_override if emb else M.num(p.get("currentStock"))
            name = ("× " + str(qty_label) + "  " if qty_label is not None else "") + M.label(p) + \
                ("  (weldment)" if p.get("isWeldment") and p.get("components") else "") + \
                ("  [std]" if p.get("stdPartId") else "") + \
                ("  (discontinued)" if p.get("discontinued") else "") + \
                (f"  Rev {p['revision']}" if p.get("revision") else "")
            it.setText(off + 0, name)
            it.setIcon(off + 0, self.thumbs.icon_for(p))
            it.setData(off + 0, LINK_ROLE, ("part", p["id"]))
            f = it.font(off)
            f.setBold(depth == 0)
            f.setStrikeOut(bool(p.get("discontinued")))
            it.setFont(off, f)
            if p.get("discontinued"):
                it.setForeground(off, QBrush(QColor(theme.MUTED)))
            spec = inv.spec_line(p)
            if p.get("stdPartId"):
                spec = (spec + "\n" if spec else "") + "[std] Daedalus standard part — Insert in model puts it in the open model"
            it.setToolTip(off, spec)
            it.setText(off + 1, M.TYPE_LABEL.get(p.get("type"), ""))
            it.setText(off + 2, ", ".join(inv.project_chips(p)) or "—")
            best = inv.best_supplier(p) or (inv.suppliers(p)[0] if inv.suppliers(p) else None)
            it.setText(off + 3, best["name"] if best else "—")
            if best and len(inv.suppliers(p)) > 1:
                it.setToolTip(off + 3, "\n".join(f"{x['name']}: {M.fmt_money(x.get('price'))} {x.get('currency') or ''}"
                                                 for x in inv.suppliers(p)))
            it.setText(off + 4, f"{stock:g}{' *' if emb else ''} {p.get('unit') or ''}")
            it.setTextAlignment(off + 4, Qt.AlignRight | Qt.AlignVCenter)
            if emb:
                it.setToolTip(off + 4, "Embedded in assembled stock — already welded in, not separate on-hand inventory")
            it.setText(off + 6, f"{M.num(p.get('reorderPoint')):g}")
            it.setText(off + 7, M.fmt_money(p.get("unitCost")))
            it.setData(0, Qt.UserRole, p["id"])
            if selectable and depth == 0:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if p["id"] in self.sel_parts else Qt.Unchecked)
            t.setItemWidget(it, off + 5, Gauge(inv.stock_ratio(p, stock), inv.level(p, stock)))
            if actions:
                btns = [_small("Profile", lambda _=False, i=p["id"]: self.profile(i), "Everything about this part"),
                        _small("Use", lambda _=False, i=p["id"]: self.move(i, "usage")),
                        _small("Receive", lambda _=False, i=p["id"]: self.move(i, "receiving")),
                        _small("Edit", lambda _=False, i=p["id"]: self.edit_part(i)),
                        _small("History", lambda _=False, i=p["id"]: D.HistoryDialog(self.inv, i, self).exec())]
                if p.get("drawingUrl"):
                    btns.append(_small("📄", lambda _=False, u=p["drawingUrl"]: _open_link(u), "Open drawing", icon="file"))
                if p.get("stdPartId"):
                    btns.append(_small("Insert in model", lambda _=False, i=p["stdPartId"]: self.insert_std_requested.emit(i),
                                       "Insert this standard part into the open model"))
                t.setItemWidget(it, off + 8, _actions(*btns))
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
        t.setMinimumHeight(min(max_h or 10_000, 34 + 40 * max(rows, 1)))
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
        v.addWidget(self.parts_tree(low[:8], actions=False) if low else _muted("Nothing to reorder."))
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
        row.addWidget(_muted(f"{len(projects)} project{'s' if len(projects) != 1 else ''} · quantities to make one "
                             "unit — demand comes from confirmed orders (Orders tab). Edit Qty/Unit right in the "
                             "table."), 1)
        row.addWidget(_btn("Send BOM from design…", self.send_bom_requested.emit))
        row.addWidget(_btn("Export Projects…", lambda: self.export_projects(),
                           "Excel workbook: summary, BOM by type, build list, purchasing by vendor, MFG list"))
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
                         + self._demand_note(pr["id"]) + "</span>")
            lab.setProperty("dimsLink", f"project:{pr['id']}")
            lab.setToolTip("Shift-click to link this project in chat")
            hdr.addWidget(lab, 1)
            if any(p.get("isWeldment") and p.get("components") for p in inv.parts_for_project(pr["id"])):
                flat_now = pr["id"] in self.mrp_state.get("proj_flat", [])
                nb = _small("Weldments: flat" if flat_now else "Weldments: nested",
                            lambda _=False, i=pr["id"]: self.toggle_nest(i),
                            "Nested: weldment parts sit under their weldment (▾ / ▸ to fold). Flat: every MFG part "
                            "at the top level with its total per unit")
                hdr.addWidget(nb)
            if pr.get("folderPath"):
                hdr.addWidget(_small("📁", lambda _=False, f=pr["folderPath"]: _open_link(f), pr["folderPath"], icon="folder"))
            up = _small("▲", lambda _=False, i=pr["id"]: (inv.move_project(i, -1), self.commit()), "Move up")
            up.setEnabled(i > 0)
            dn = _small("▼", lambda _=False, i=pr["id"]: (inv.move_project(i, 1), self.commit()), "Move down")
            dn.setEnabled(i < len(projects) - 1)
            extra = []
            if pr.get("totalQtyNeeded") not in (None, "", 0):
                extra.append(_small("Clear old plan", lambda _=False, i=pr["id"]: self.clear_old_plan(i),
                                    "Remove the project's old planned quantity — demand then comes only from "
                                    "confirmed orders"))
            for b in (*extra, up, dn, _small("+ Add Parts", lambda _=False, i=pr["id"]: self.add_parts_to_project(i)),
                      _small("Duplicate", lambda _=False, i=pr["id"]: self.duplicate_project(i)),
                      _small("BOM / Export", lambda _=False, i=pr["id"]: self.project_bom(i),
                             "Parts list: MFG parts as a hierarchy, fasteners and purchased parts — copy or "
                             "export to Excel"),
                      _small("Edit", lambda _=False, i=pr["id"]: self.edit_project(i)),
                      _small("Archive", lambda _=False, i=pr["id"]: self.archive(i, True))):
                hdr.addWidget(b)
            v.addLayout(hdr)
            if pr["id"] in self.collapsed:
                continue
            flat = pr["id"] in self.mrp_state.setdefault("proj_flat", [])
            rows = inv.project_rows(pr["id"], per_unit=True, nest=not flat)
            if not rows:
                v.addWidget(_muted("No parts assigned yet."))
                continue
            groups = {k: [] for k in PROJECT_GROUPS}             # top-level part + its BOM lines stay together
            for r in rows:
                if r[1] == 0:
                    cur = groups[project_group(r[0])]
                cur.append(r)
            ordered = []                                         # one table, a header row per type
            for gname, grows in groups.items():
                blocks, cur = [], None                           # sort by part number, BOM lines stay under
                for r in grows:
                    if r[1] == 0:
                        cur = []
                        blocks.append(cur)
                    cur.append(r)
                blocks.sort(key=lambda b: ((b[0][0].get("partNumber") or "~").upper(), b[0][0]["name"].lower()))
                grows = [r for b in blocks for r in b]
                if grows:
                    ordered.append(("hdr", gname, sum(1 for r in grows if r[1] == 0)))
                    ordered.extend(grows)
            v.addWidget(self._project_table(pr, ordered, flat))
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

    def toggle_nest(self, pid):
        flat = self.mrp_state.setdefault("proj_flat", [])
        flat.remove(pid) if pid in flat else flat.append(pid)
        self.refresh_current()

    def toggle_weld(self, pid, part_id):
        key = f"{pid}:{part_id}"
        shut = self.mrp_state.setdefault("weld_shut", [])
        shut.remove(key) if key in shut else shut.append(key)
        self.refresh_current()

    def _project_table(self, pr, rows, flat=False):
        inv = self.inv
        shut = set(self.mrp_state.get("weld_shut", []))
        hidden_under = None                                     # depth of a collapsed weldment we're inside
        tr, heads = [], []
        for row in rows:
            if row[0] == "hdr":
                hidden_under = None
                it = QTableWidgetItem(f"{row[1]}  ·  {row[2]}")
                f_ = it.font()
                f_.setBold(True)
                it.setFont(f_)
                it.setForeground(QBrush(QColor(theme.ACCENT_TEXT)))
                it.setFlags(Qt.ItemIsEnabled)
                heads.append(len(tr))
                tr.append([it] + [""] * 7)
                continue
            p, depth, ql, need, on_hand, emb = row
            if hidden_under is not None:
                if depth > hidden_under:
                    continue
                hidden_under = None
            is_weld = p.get("type") == "manufactured" and p.get("isWeldment") and p.get("components")
            open_ = f"{pr['id']}:{p['id']}" not in shut
            if is_weld and not flat and not open_:
                hidden_under = depth
            short = max(0.0, need - on_hand)
            caret = ("▾ " if open_ else "▸ ") if is_weld and not flat else ("   " if not flat else "")
            label = (f"× {ql}  " if ql is not None and not flat else "") + p["name"]
            if is_weld:
                label += f"   [weldment · {len(p.get('components') or [])}]"
            if flat and ql:
                label += f"   ({ql})"
            name = QTableWidgetItem(("    " * depth) + caret + label)
            if is_weld and not flat:
                name.setData(Qt.UserRole + 5, p["id"])
                f_ = name.font()
                f_.setBold(True)
                name.setFont(f_)
            name.setToolTip(inv.spec_line(p))
            tag(name, "part", p["id"])
            if depth == 0 and inv.allocation(p, pr["id"]):
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
            if depth == 0 and inv.allocation(p, pr["id"]):
                acts.append(_small("Unassign", lambda _=False, pp=p, pid=pr["id"]: self.unassign(pp, pid),
                                   danger=True))
            tr.append([tag(QTableWidgetItem(p.get("partNumber") or ""), "part", p["id"]), name, q, _num_item(need),
                       _num_item(on_hand, None) if not emb else _num_item(f"{on_hand:g} *"),
                       _num_item(f"−{short:g}" if short > 0 else "—", theme.ERROR if short > 0 else None,
                                 short > 0), M.fmt_money(p.get("unitCost")), _actions(*acts)])
        t = self.table(["Part No.", "Part", "Qty/Unit", "For 1 Unit", "On Hand", "Short (1 unit)",
                        "Unit Cost", ""], tr, 1)
        for i in heads:                                             # type header rows span the table
            t.setSpan(i, 0, 1, t.columnCount())
            t.item(i, 0).setBackground(QBrush(QColor(theme.PANEL)))
            t.setRowHeight(i, max(26, t.fontMetrics().height() + 12))
        from PySide6.QtWidgets import QAbstractScrollArea, QSizePolicy
        t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)          # the whole project shows, no gaps:
        t.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)  # height follows the rows (also after styling)
        t.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        t.setMinimumHeight(0)

        def clicked(r, c):                                          # ▾ / ▸ on a weldment folds its parts
            it = t.item(r, 1)
            wid = it.data(Qt.UserRole + 5) if it is not None else None
            if wid and c == 1 and not (QApplication.keyboardModifiers() & Qt.ShiftModifier):
                self.toggle_weld(pr["id"], wid)
        t.cellClicked.connect(clicked)
        return t

    def project_bom(self, pid):
        from .project_bom import ProjectBomDialog
        d = ProjectBomDialog(self, pid)
        d.setAttribute(Qt.WA_DeleteOnClose)
        d.show()
        self._bom_window = d
        return d

    def buy_list(self):
        """Reorder → copyable buy list for what's showing (filters apply)."""
        from .project_bom import BuyListDialog
        inv, f = self.inv, self.reorder_filter
        lst = self.reorder_filtered()
        if not lst:
            return self.toast.show_msg("Nothing to buy with the current filters")
        need = (lambda p: inv.needed_for_project(p, f["project"])) if f["project"] != "all" else inv.total_needed
        title = "Buy List" + (f" — {inv.project_name(f['project'])}" if f["project"] != "all" else "") + \
            (f" — {f['vendor']}" if f["vendor"] != "all" else "")
        d = BuyListDialog(self, lst, need, f["vendor"] if f["vendor"] != "all" else None, title)
        d.setAttribute(Qt.WA_DeleteOnClose)
        d.show()
        self._buy_window = d
        return d

    def export_projects(self, pids=None):
        """Pick projects + units, then write the project workbook."""
        from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout

        from ..inventory import project_export as PX
        inv = self.inv
        projects = inv.active_projects()
        if not projects:
            return self.toast.show_msg("No projects to export")
        d = QDialog(self)
        d.setWindowTitle("Export Projects")
        v = QVBoxLayout(d)
        v.addWidget(_muted("Units: how many to build — defaults to the units on open production (else 1). "
                           "Quantities, shortages, purchasing and costs follow it."))
        g = QGridLayout()
        g.addWidget(QLabel("<b>Project</b>"), 0, 0)
        g.addWidget(QLabel("<b>Units</b>"), 0, 1)
        g.addWidget(QLabel("<b>On production</b>"), 0, 2)
        picks = {}
        for r, pr in enumerate(projects, 1):
            cb = QCheckBox(pr["name"])
            cb.setChecked(pids is None or pr["id"] in pids)
            on = inv.project_qty(pr["id"])
            sp = QDoubleSpinBox()
            sp.setRange(1, 100000)
            sp.setDecimals(0)
            sp.setValue(on or 1)
            g.addWidget(cb, r, 0)
            g.addWidget(sp, r, 1)
            g.addWidget(QLabel(f"{on:g}" if on else "—"), r, 2)
            picks[pr["id"]] = (cb, sp)
        box = QWidget()
        box.setLayout(g)
        if len(projects) > 12:
            from PySide6.QtWidgets import QScrollArea
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setWidget(box)
            sa.setMinimumHeight(360)
            v.addWidget(sa)
        else:
            v.addWidget(box)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export…")
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        if d.exec() != QDialog.Accepted:
            return
        chosen = [pid for pid, (cb, _sp) in picks.items() if cb.isChecked()]
        if not chosen:
            return self.toast.show_msg("No project selected")
        units = {pid: picks[pid][1].value() for pid in chosen}
        name = (inv.project_name(chosen[0]) if len(chosen) == 1 else "projects")
        fn = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "-") or "project"
        path = self._save_path(f"{fn}-{M.today()}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            PX.write(path, inv, chosen, units, inv.userName or "")
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Export", f"Couldn't export:\n{e}")
        self.toast.show_msg(f"Exported {len(chosen)} project{'s' if len(chosen) != 1 else ''}")

    def _demand_note(self, pid):
        from ..inventory import orders as O
        u = O.units_on_order(self.inv, pid)
        if u is not None:
            return f" · {u:g} unit{'s' if u != 1 else ''} on open production"
        old = self.inv.legacy_qty(pid)
        return (f" · <span style='color:{theme.WARN}'>old plan {old:g} units (until its first order is confirmed)"
                "</span>" if old else " · no open orders")

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

    def clear_old_plan(self, pid):
        pr = self.inv.project(pid)
        old = pr.get("totalQtyNeeded")
        pr.pop("totalQtyNeeded", None)
        self.commit(f"{pr['name']}: old plan of {old} units removed",
                    undo=lambda: (pr.__setitem__("totalQtyNeeded", old), self.commit("Undone")))

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
            from ..inventory import orders as O
            me = inv.userName
            unread = sum(O.is_unread(o, me) for o in orders if o.get("status") not in O.CLOSED_PROD)
            if unread:
                v.addWidget(_muted(f"● {unread} unread — open an order to work it", False))
            inbox = sorted(orders, key=lambda o: (o.get("status") in O.CLOSED_PROD, not O.is_unread(o, me),
                                                  -(o.get("createdAt") or 0), o.get("dueDate") or ""))
            rows = []
            for o in inbox:
                new = O.is_unread(o, me) and o.get("status") not in O.CLOSED_PROD
                dot = QTableWidgetItem("●" if new else "")
                dot.setForeground(QBrush(QColor(theme.INFO)))
                dot.setTextAlignment(Qt.AlignCenter)
                cells = [o.get("orderName") or "—", o.get("poNumber") or "—",
                         inv.project_name(o.get("projectId")) or "(deleted project)", f"{M.num(o.get('qty')):g}",
                         O.stage(inv, o), M.fmt_date(o.get("finishBy") or o.get("dueDate"))]
                items = []
                for c in cells:
                    it = QTableWidgetItem(c)
                    if new:
                        f_ = it.font()
                        f_.setBold(True)
                        it.setFont(f_)
                    if o.get("status") in O.CLOSED_PROD:
                        it.setForeground(QBrush(QColor(theme.MUTED)))
                    items.append(tag(it, "production", o["id"]))
                rows.append([dot, *items, self.pill(o.get("status") or "Not Started",
                                                    STATUS_COL.get(o.get("status"), theme.MUTED)),
                             _actions(_small("Open", lambda _=False, oo=o: self.open_production(oo)),
                                      _small("Edit", lambda _=False, oo=o: self.edit_order(oo)),
                                      _small("Delete", lambda _=False, oo=o: self.delete_order(oo), danger=True))])
            t = self.table(["", "Order", "PO", "Project", "Qty", "Stage", "Finish / Due", "Status", ""], rows, 5)
            t.cellDoubleClicked.connect(lambda r, _c, ib=inbox: self.open_production(ib[r]))
            v.addWidget(t)
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

    def open_production(self, o):
        from ..inventory import orders as O
        if O.mark_read(o, self.inv.userName):
            self.commit()
        OU.ProductionDialog(self.inv, o, lambda msg: self.commit(), self).exec()
        self.refresh()

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
        row.addWidget(_btn("Print binning list…", self.print_binning, "Every bin with its parts and stock — "
                                                                       "print, PDF or Excel, with a column to count"))
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
        vsel = QComboBox()
        vsel.addItem("All vendors", "all")
        for name in sorted({s["name"] for p in inv.parts for s in inv.suppliers(p)}, key=str.lower):
            vsel.addItem(name, name)
        vsel.addItem("No vendor", "none")
        vsel.setCurrentIndex(max(0, vsel.findData(f["vendor"])))
        vsel.currentIndexChanged.connect(lambda _i: (f.__setitem__("vendor", vsel.currentData()),
                                                     self.refresh_current()))
        ssel = QComboBox()
        for k, lab in (("partno", "Sort: Part No."), ("name", "Sort: Name"), ("vendor", "Sort: Vendor"), ("stock", "Sort: Stock level"),
                       ("location", "Sort: Location"), ("value", "Sort: Stock value"), ("type", "Sort: Type")):
            ssel.addItem(lab, k)
        ssel.setCurrentIndex(max(0, ssel.findData(f["sort"])))
        ssel.currentIndexChanged.connect(lambda _i: (f.__setitem__("sort", ssel.currentData()),
                                                     self.refresh_current()))
        disc = QCheckBox("Discontinued")
        disc.setToolTip("Show discontinued parts")
        disc.setChecked(f["discontinued"])
        disc.toggled.connect(lambda on: (f.__setitem__("discontinued", on), self.refresh_current()))
        row.addWidget(tsel)
        row.addWidget(psel)
        row.addWidget(vsel)
        row.addWidget(ssel)
        row.addWidget(self.q, 1)
        row.addWidget(disc)
        row.addWidget(_btn("MFG revisions…", self.revision_check, "Check manufactured parts against their model / "
                                                                  "drawing files for newer revisions"))
        v.addLayout(row)
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
        if f["vendor"] == "none":
            lst = [p for p in lst if not inv.suppliers(p)]
        elif f["vendor"] != "all":
            lst = [p for p in lst if any(s["name"] == f["vendor"] for s in inv.suppliers(p))]
        if not f["discontinued"]:
            lst = [p for p in lst if not p.get("discontinued")]

        def vendor_key(p):
            b = inv.best_supplier(p) or (inv.suppliers(p)[0] if inv.suppliers(p) else None)
            return ((b["name"].lower() if b else "~~~"), p["name"].lower())
        keys = {"name": lambda p: p["name"].lower(), "vendor": vendor_key,
                "partno": lambda p: ((p.get("partNumber") or "~").upper(), p["name"].lower()),
                "stock": lambda p: (inv.stock_ratio(p), p["name"].lower()),
                "location": lambda p: (inv.location_label_for(p.get("locationId")) or "~~~", p["name"].lower()),
                "value": lambda p: -M.num(p.get("currentStock")) * inv.to_cad(p.get("unitCost"),
                                                                            p.get("unitCostCurrency")),
                "type": lambda p: (p.get("type") or "", p["name"].lower())}
        lst.sort(key=keys.get(f["sort"], keys["name"]))
        v.setContentsMargins(0, 0, 0, 0)
        from .inventory_mrp_ui import EngineeringView
        from .parts_browser import PartsBrowser
        self._browser = PartsBrowser(self, lst, w)
        QTimer.singleShot(0, lambda: (self.q.setFocus(), self.q.setCursorPosition(len(self.q.text())))
                          if f["q"] else None)
        ev = EngineeringView(self, parts_widget=self._browser, profile_widget=self._browser.profile_page,
                            browser=self._browser)
        self._browser.open_profile = lambda: ev.tabs.setCurrentWidget(self._browser.profile_page)
        ev.edit_part.connect(self.edit_part)
        ev.new_part.connect(self.new_part_from)
        return ev

    def add_part_inline(self, preset=None):
        br = getattr(self, "_browser", None)
        if br is not None and br.dirty() and QMessageBox.question(
                self, "Unsaved changes", "Discard your changes to the open part?") != QMessageBox.Yes:
            return
        self.parts_sel = "__new__"
        self._new_preset = preset or None
        if br is not None:
            br._clean = None
        self.refresh_current()
        br = getattr(self, "_browser", None)
        if br is not None:
            br.open_profile()

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
        from ..inventory import mrp as R
        if part:
            part.update(vals)
        else:
            self.inv.parts.append(M.new_part(**vals, createdBy=self.inv.userName or "unknown"))
        R.sync_assignments(self.inv)
        self.commit("Part saved" if part else f"Added {vals['name']}")

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
        row.addWidget(_muted(f"{len(vs)} vendor{'s' if len(vs) != 1 else ''} · "
                             f"{sum(1 for x in vs if x.get('status') == 'Prospective')} prospective."), 1)
        from .inventory_mrp_ui import import_prices_ui, import_vendors_ui
        row.addWidget(_btn("Import vendors…", lambda: import_vendors_ui(self), "Vendor list from Excel / CSV: company, "
                                                                            "address, contact, phone, email, terms …"))
        row.addWidget(_btn("Import prices…", lambda: import_prices_ui(self), "Vendor prices by part number (e.g. the "
                                                                          "pricing Google Sheet downloaded as .xlsx)"))
        row.addWidget(_btn("+ Add Vendor", lambda: self.edit_vendor(None), primary=True))
        v.addLayout(row)
        cats = list(M.VENDOR_CATEGORIES)
        cats[-1:-1] = sorted({x.get("category") for x in vs if x.get("category") and x["category"] not in cats})
        for cat in cats:
            lst = [x for x in vs if (x.get("category") or "Other") == cat]
            if not lst:
                continue
            v.addWidget(_section(cat))
            rows = []
            for x in lst:
                email = _small(x["email"], lambda _=False, e=x["email"]: _open_link("mailto:" + e)) if x.get("email") \
                    else "—"
                web = _small("Visit", lambda _=False, u=x["website"]: _open_link(u)) if x.get("website") else "—"
                addr = ", ".join(y for y in (x.get("address"), x.get("city"), x.get("area") or x.get("province")) if y)
                tag = f"  ({x['status']})" if x.get("status") and x["status"] != "Active" else ""
                contact = " / ".join(y for y in (x.get("contactName"), x.get("contactName2")) if y) or "—"
                phone = "\n".join(y for y in (x.get("phone"), x.get("phone2")) if y) or "—"
                rows.append([x["name"] + tag + (f"\n{addr}" if addr else ""), contact, phone, email, web,
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
        """Orders: PO, name, projects. Draft → Confirm (adds the demand, creates production) → Shipped. Editing a
        confirmed order is a change order — logged and pushed to production."""
        from ..inventory import orders as O
        inv = self.inv
        w, v = self._page()
        orders = sorted(inv.clientOrders, key=lambda o: (ORDER_RANK.get(O.status(o), 9), -(o.get("createdAt") or 0)))
        row = QHBoxLayout()
        row.addWidget(_muted(f"{len(orders)} order{'s' if len(orders) != 1 else ''} · confirm an order to add its "
                             "demand and send it to Production. Changing a confirmed order logs a change order."), 1)
        row.addWidget(_btn("+ New Order", lambda: self.edit_client_order(None), primary=True))
        v.addLayout(row)
        if not orders:
            v.addWidget(_muted("No orders yet."))
            v.addStretch(1)
            return w
        rows = []
        for o in orders:
            st = O.status(o)
            lines = "\n".join(f"{inv.project_name(l.get('projectId')) or '(deleted project)'} ×{M.num(l.get('qty')):g}"
                              for l in o.get("projectLines") or []) or "—"
            prog = O.progress(inv, o)
            prod = "\n".join(f"{x['project']} ×{x['qty']:g} — {x['stage']}" for x in prog
                             if x["status"] != "Cancelled" or st == O.CANCELLED) or \
                ("Not sent — confirm to start" if st == O.DRAFT else "—")
            acts = [_small("Change" if st == O.CONFIRMED else "Edit", lambda _=False, oo=o: self.edit_client_order(oo))]
            if st == O.DRAFT:
                acts.insert(0, _small("Confirm", lambda _=False, oo=o: self.confirm_client_order(oo),
                                      "Add the demand and create production orders"))
            if st == O.CONFIRMED and not prog:          # confirmed before orders created production
                acts.insert(0, _small("Send to production", lambda _=False, oo=o: self.send_to_production(oo),
                                      "Create its production orders and add the demand"))
                prod = "Not in production yet"
            if st == O.CONFIRMED:
                acts.append(_small("Shipped", lambda _=False, oo=o: self.order_status(oo, O.SHIPPED)))
            if st in (O.DRAFT, O.CONFIRMED):
                acts.append(_small("Cancel", lambda _=False, oo=o: self.order_status(oo, O.CANCELLED), danger=True))
            acts.append(_small(f"Log ({len(o.get('changes') or [])})",
                               lambda _=False, oo=o: OU.OrderHistoryDialog(self.inv, oo, self).exec()))
            if st in (O.DRAFT, O.CANCELLED):
                acts.append(_small("Delete", lambda _=False, oo=o: self.delete_client_order(oo), danger=True))
            rev = f" · rev {o['rev']}" if o.get("rev") else ""
            rows.append([tag(QTableWidgetItem(o.get("poNumber") or "—"), "order", o["id"]),
                         tag(QTableWidgetItem(O.order_name(inv, o)), "order", o["id"]), lines,
                         self.pill(st + rev, ORDER_COL.get(st, theme.MUTED)), prod, _actions(*acts)])
        t = self.table(["PO", "Name", "Projects", "Order", "Production", ""], rows, 4)
        t.resizeRowsToContents()
        v.addWidget(t)
        v.addStretch(1)
        return w

    def edit_client_order(self, o):
        from ..inventory import orders as O
        if not self.inv.active_projects() and not o:
            return self.toast.show_msg("Add a project first")
        d = OU.OrderEntryDialog(self.inv, o, self)
        if d.exec():
            vals = d.values()
            rev = (o or {}).get("rev", 0)
            o = O.save_order(self.inv, o, vals["po"], vals["name"], vals["lines"], self.inv.userName)
            self.commit(f"Change order {o['rev']} saved — production updated" if o.get("rev", 0) > rev
                        else "Order saved")

    def confirm_client_order(self, o):
        if OU.confirm_order(self, self.inv, o):
            self.commit("Order confirmed — demand added, production orders created")

    def send_to_production(self, o):
        from ..inventory import orders as O
        O.sync_production(self.inv, o, self.inv.userName, "From confirmed order")
        self.commit("Production orders created")

    def order_status(self, o, new):
        from ..inventory import orders as O
        if new == O.CANCELLED and QMessageBox.question(
                self, "Cancel order", f"Cancel {O.order_name(self.inv, o)}? Production that hasn't finished is "
                "cancelled too and its demand removed.") != QMessageBox.Yes:
            return
        O.set_status(self.inv, o, new, self.inv.userName)
        self.commit(f"Order {new.lower()}")

    def delete_client_order(self, o):
        from ..inventory import orders as O
        if QMessageBox.question(self, "Delete", f"Delete order {O.order_name(self.inv, o)} and its log?") \
                == QMessageBox.Yes:
            self.inv.clientOrders = [x for x in self.inv.clientOrders if x["id"] != o["id"]]
            self.commit("Order deleted")

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
            short = {r["part"]["id"]: r for r in inv.shortages()}
            ws = QComboBox()
            for k, lab in (("all", "All reasons"), ("short", "Short for projects"), ("low", "Below reorder point")):
                ws.addItem(lab, k)
            ws.setCurrentIndex(max(0, ws.findData(f.get("why", "all"))))
            ws.currentIndexChanged.connect(lambda _i: (f.__setitem__("why", ws.currentData()),
                                                       self.refresh_current()))
            ts = QComboBox()
            for k, lab in REORDER_TYPES:
                ts.addItem(lab, k)
            ts.setCurrentIndex(max(0, ts.findData(f.get("type", "all"))))
            ts.currentIndexChanged.connect(lambda _i: (f.__setitem__("type", ts.currentData()),
                                                       self.refresh_current()))
            lst = self.reorder_filtered(short)
            row.addWidget(ps)
            row.addWidget(vs)
            row.addWidget(ws)
            row.addWidget(ts)
            row.addWidget(_muted(f"{len(lst)} of {len(all_low)} parts", False), 1)
            row.addWidget(_btn("Export All (per project)", self.export_reorder_by_project))
            row.addWidget(_btn("Export List (.xlsx)", self.export_reorder))
            v.addLayout(row)
            view = f.get("view", "list")                 # Reorder List ⇄ Buy List (same filters)
            vr = QHBoxLayout()
            for k, lab, tip in (("list", "Reorder List", "Why each part needs buying, suppliers, order / receive"),
                                ("buy", "Buy List", "One line per part, ready to copy into Excel, an email or a PO")):
                b = _btn(lab, lambda _=False, k=k: (f.__setitem__("view", k), self.refresh_current()), tip,
                         primary=view == k)
                b.setCheckable(True)
                b.setChecked(view == k)
                vr.addWidget(b)
            vr.addStretch(1)
            v.addLayout(vr)
            if view == "buy":
                from .project_bom import BuyListWidget
                need = (lambda p: inv.needed_for_project(p, f["project"])) if f["project"] != "all" \
                    else inv.total_needed
                self.buy_panel = BuyListWidget(self, lst, need, f["vendor"] if f["vendor"] != "all" else None)
                self.buy_panel.setMinimumHeight(420)
                v.addWidget(self.buy_panel, 1)
                lst = []
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
                name = tag(QTableWidgetItem(M.label(p)), "part", p["id"])
                name.setToolTip(inv.spec_line(p))
                is_short, is_low = p["id"] in short, M.num(p.get("currentStock")) <= M.num(p.get("reorderPoint"))
                why = QTableWidgetItem(" + ".join(x for x, on in (("Short", is_short), ("Below RP", is_low)) if on))
                why.setForeground(QBrush(QColor(theme.ERROR if is_short else theme.WARN)))
                why.setToolTip("Short = the projects it's on need more than is in stock; Below RP = at or below its "
                               "reorder point")
                comp = "\n".join(f"{inv.project_name(c['projectId']) or '(deleted project)'} needs {c['needed']:g}"
                                 for c in short[p["id"]]["contributing"]) if is_short else ""
                rows.append([cb, name, why, M.TYPE_LABEL.get(p.get("type"), ""),
                             _num_item(f"{M.num(p.get('currentStock')):g} {p.get('unit') or ''}", theme.ERROR),
                             _num_item(M.num(p.get("reorderPoint"))),
                             _num_item(need if need else "—", theme.ERROR if need > M.num(p.get("currentStock")) else None),
                             _num_item(sq), comp, sup_w, _num_item(M.fmt_money(sq * unit)),
                             _actions(_small("Order", lambda _=False, pp=p, q=sq: self.order_part(pp, q)),
                                      _small("Mark Received", lambda _=False, i=p["id"]: self.move(i, "receiving")))])
            nh = "Needed (this project)" if f["project"] != "all" else "Needed (projects)"
            if view != "buy":
                v.addWidget(_muted("Everything to buy: parts short for their projects (combined demand of every "
                                   "project they're on) and parts at or below their reorder point. Suggested qty "
                                   "covers both."))
                v.addWidget(self.table(["", "Part", "Why", "Type", "On Hand", "Reorder Pt", nh, "Suggested Qty",
                                        "Competing Projects", "Suppliers", "Est. Cost", ""], rows, 9) if rows
                            else _muted("No parts match this filter."))
        else:
            v.addWidget(_muted("Nothing to reorder — every part is above its reorder point and covers its projects."))
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
        from .inventory_mrp_ui import receive_dialog
        o = next((x for x in self.inv.partOrders if x["id"] == oid), None)
        if o is not None:
            receive_dialog(self, o)

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

    def reorder_filtered(self, short=None):
        """The reorder list with every filter applied (project, vendor, reason, part type)."""
        inv, f = self.inv, self.reorder_filter
        lst = inv.reorder_list(f["project"], f["vendor"])
        if f.get("why") == "short":
            short = short if short is not None else {r["part"]["id"] for r in inv.shortages()}
            lst = [p for p in lst if p["id"] in short]
        elif f.get("why") == "low":
            lst = [p for p in lst if M.num(p.get("currentStock")) <= M.num(p.get("reorderPoint"))]
        if f.get("type", "all") != "all":
            lst = [p for p in lst if (p.get("type") or "fastener") == f["type"]]
        return lst

    def export_reorder(self):
        inv = self.inv
        f = self.reorder_filter
        lst = self.reorder_filtered()
        if not lst:
            return self.toast.show_msg("Nothing to export with the current filters")
        need = (lambda p: inv.needed_for_project(p, f["project"])) if f["project"] != "all" else inv.total_needed
        rows = inv.reorder_rows(lst, need, f["vendor"] if f["vendor"] != "all" else None)
        path = self._save_path(f"dims-reorder-{M.today()}.xlsx", "Excel (*.xlsx)")
        if path:
            name = (inv.project_name(f["project"]) or "Reorder") if f["project"] != "all" else "Reorder List"
            M.write_xlsx(path, [(name, rows)])
            self.toast.show_msg(f"Exported {len(rows)} part(s)")

    def export_reorder_by_project(self):
        inv = self.inv
        low = inv.low_stock()
        if not low:
            return self.toast.show_msg("Nothing to export — nothing to reorder")
        path = self._save_path(f"dims-reorder-by-project-{M.today()}.xlsx", "Excel (*.xlsx)")
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
        v.addWidget(_section("Where the data lives"))
        st = self.store
        if getattr(st, "kind", "file") == "supabase":
            row = QHBoxLayout()
            loc = QLineEdit(st.location())
            loc.setReadOnly(True)
            row.addWidget(loc, 1)
            if st.signed_in:
                row.addWidget(_btn("Sign out", self.team_sign_out))
            else:
                row.addWidget(_btn("Sign in…", self.team_sign_in, primary=True))
            row.addWidget(_btn("Server settings…", self.team_connect))
            row.addWidget(_btn("Use a file instead…", self.choose_file))
            v.addLayout(row)
            if getattr(st, "layout", "") == "rows":
                row = QHBoxLayout()
                row.addWidget(_muted(f"DIMS database · your role: {st.role or '—'}. Stock changes are applied by "
                                     "the database, every change is recorded in the audit trail.", False), 1)
                row.addWidget(_btn("People & roles…", self.team_people))
                if st.role == "admin":
                    row.addWidget(_btn("Import old web-app data", self.team_import, "Copy the old app_storage data "
                                       "into DIMS (existing records are left alone)"))
                v.addLayout(row)
            elif st.signed_in:
                v.addWidget(_muted("This project still uses the original single-table layout. Run the DIMS setup SQL "
                                   "(Server settings… ▸ Copy DIMS setup SQL) and reconnect for per-record saving, "
                                   "database-applied stock, roles and an audit trail.", True))
            pend = st.pending()
            v.addWidget(_muted(
                (f"Signed in as {st.email}. " if st.signed_in else "Not signed in. ") +
                "Everyone signed in to this team server shares one inventory: changes are saved as you work and "
                "teammates' changes appear within a few seconds. If two people edit at once, both edits are kept "
                "(stock counts add up); only the same field edited twice keeps the newest." +
                (f" {len(pend)} change(s) waiting to be sent." if pend else "") +
                (f" Last problem: {st.last_error}" if st.last_error else "")))
        else:
            row = QHBoxLayout()
            path = QLineEdit(st.location())
            path.setReadOnly(True)
            row.addWidget(path, 1)
            row.addWidget(_btn("Open folder", lambda: _open_link(str(self.store.path.parent))))
            row.addWidget(_btn("Use another file…", self.choose_file, "Point at a shared file (e.g. on a network "
                                                                       "drive) — everyone using it sees the same data"))
            row.addWidget(_btn("Connect to team server…", self.team_connect, "Supabase: sign-in per person, works "
                                                                            "from anywhere", primary=True))
            v.addLayout(row)
            v.addWidget(_muted("Saved to this file as you work. For a team, either put the file on a shared drive and "
                               "point each PC at it, or connect to a team server (Supabase — the same one the original "
                               "web app used) so everyone signs in with their own account from anywhere."))
        v.addWidget(_section("Weekly backup"))
        from ..inventory import backup as BK
        last = BK.last_backup(inv.syncMeta or {})
        row = QHBoxLayout()
        row.addWidget(_muted(
            "Every Friday everyone gets a reminder to download a full Excel backup; the first person to do it clears "
            "it for the whole team. " + (f"Last backup: {dt.datetime.fromtimestamp(last['at'] / 1000):%Y-%m-%d %H:%M}"
                                         f" by {last['by']} ({last['file']})." if last else "No backup yet.") +
            (" This week's is due." if BK.is_due(inv.syncMeta or {}) else " This week's is done.")), 1)
        row.addWidget(_btn("Download backup now", self.download_backup, primary=True))
        v.addLayout(row)
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
        start = str(getattr(self.store, "path", "") or (self.settings.value("inventory/path") or ""))
        p, _ = QFileDialog.getSaveFileName(self, "Inventory data file", start,
                                           "Inventory (*.json)", options=QFileDialog.DontConfirmOverwrite)
        if not p:
            return
        if not p.lower().endswith(".json"):
            p += ".json"
        existed = Path(p).exists()
        if getattr(self.store, "kind", "file") != "file":
            from ..inventory.backends import use_file
            values = self.inv.dump_values()
            self._swap_store(use_file(self.settings, p))
            if not existed:
                self.store.inv.load_values(values)
        self.store.move_to(p)
        self.settings.setValue("inventory/path", p)
        self.commit("Opened " + Path(p).name if existed else "Data moved to " + Path(p).name)

    # ------------------------------------------------------------- team server
    def _swap_store(self, store):
        try:
            self.store.listeners.remove(self._on_store_saved)
        except (ValueError, AttributeError):
            pass
        user = self.inv.userName
        self.store = store
        if not store.inv.userName:
            store.inv.userName = user
        store.listeners.append(self._on_store_saved)

    def team_connect(self):
        from .inventory_team_ui import TeamServerDialog
        dlg = TeamServerDialog(self.settings, self.store, self)
        if dlg.exec() and dlg.store is not None:
            from ..inventory.backends import use_team
            local = self.inv.dump_values() if getattr(self.store, "kind", "file") == "file" else None
            self._swap_store(use_team(self.settings, dlg.store))
            if local is not None and dlg.upload.isChecked():
                from ..inventory.supabase import SyncError
                try:
                    self.store.upload_all(local)
                    self.toast.show_msg("This PC's inventory is now on the team server")
                except SyncError as e:
                    QMessageBox.warning(self, "Team server", str(e))
            self.refresh()

    def team_sign_in(self, reason=None):
        from .inventory_team_ui import SignInDialog
        if SignInDialog(self.store, self, reason=reason).exec():
            from ..inventory.supabase import SyncError
            try:
                self.store.load()
            except SyncError as e:
                QMessageBox.warning(self, "Team server", str(e))
            self.refresh()

    def team_people(self):
        from .inventory_team_ui import PeopleDialog
        PeopleDialog(self.store, self).exec()

    def team_import(self):
        from ..inventory.supabase import SyncError
        try:
            got = self.store.import_old_data()
        except SyncError as e:
            return QMessageBox.warning(self, "Import", str(e))
        self.toast.show_msg("Imported: " + ", ".join(f"{v} {k}" for k, v in got.items() if isinstance(v, int)))
        self.refresh()

    def team_sign_out(self):
        self.store.sign_out()
        self.refresh()

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
        p = self._save_path(f"dims-inventory-{M.today()}.{kind}", "Excel (*.xlsx)" if kind == "xlsx" else "CSV (*.csv)")
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

    # ============================================================ analytics
    def build_analytics(self):
        from .inventory_extras_ui import AnalyticsView
        w = AnalyticsView(self)
        w.edit_part.connect(self.edit_part)
        return w

    def build_timephased(self):
        from .inventory_mrp_ui import TimePhasedView
        return TimePhasedView(self)

    def new_part_from(self, preset: dict):
        """Add Part, starting from a reserved number (type from its prefix) or as a new line of a BOM."""
        from ..inventory import mrp as R
        parent = preset.pop("_bom_parent", None)
        if parent is None and self.current_key() == "parts":     # new parts open in the Part Profile editor
            return self.add_part_inline(preset)
        d = D.PartDialog(self.inv, None, self, preset=preset)
        if not d.exec():
            return
        vals = d.values()
        p = M.new_part(**vals, createdBy=self.inv.userName or "unknown")
        self.inv.parts.append(p)
        if p.get("partNumber"):
            try:
                R.assign(self.inv, p["partNumber"], p)
            except ValueError:
                pass
        if parent and self.inv.part(parent):
            par = self.inv.part(parent)
            R.set_bom(self.inv, parent, [(c["partId"], c["qty"]) for c in par.get("components") or []] + [(p["id"], 1)],
                      R.bom_kind(par) or "assembly")
        self.commit(f"Added {p['name']}")

    def profile(self, pid):
        from .inventory_mrp_ui import PartProfileDialog
        PartProfileDialog(self, pid).exec()

    def print_binning(self):
        from .inventory_extras_ui import BinningDialog
        BinningDialog(self.inv, self).exec()

    def revision_check(self):
        from .inventory_extras_ui import VersionCheckDialog
        VersionCheckDialog(self.inv, lambda msg: self.commit(msg), self).exec()

    def _thumb_refresh(self):
        """A thumbnail finished: redraw the visible list once things settle."""
        if not hasattr(self, "_thumb_timer"):
            self._thumb_timer = QTimer(self)
            self._thumb_timer.setSingleShot(True)
            self._thumb_timer.timeout.connect(lambda: self.current_key() in ("parts", "dashboard", "projects")
                                              and self.refresh_current())
        self._thumb_timer.start(600)

    # ============================================================== Icarus
    def toggle_icarus(self, on=None):
        """on: the button's new checked state (clicked passes it); None flips."""
        if on is None:
            on = self.icarus is None or not self.icarus.isVisible()
        on = bool(on)
        if on and self.icarus is None:
            from ..ai.dims_tools import DIMS_EXAMPLES, DIMS_SYSTEM, DIMS_TOOLS, DimsToolExecutor
            from .copilot_panel import CopilotPanel
            ex = DimsToolExecutor(lambda: self.store, lambda msg, undo=None: self.commit(msg, undo))
            self.icarus = CopilotPanel(None, executor=ex, tools=DIMS_TOOLS, system=DIMS_SYSTEM,
                                       examples=DIMS_EXAMPLES,
                                       welcome=("Ask about the inventory",
                                                "Icarus ({who}) reads DIMS — stock, bins, projects, orders, costs, "
                                                "trends — and records changes when you ask (each one can be undone). "
                                                "Try one:"))
            self.icarus.setMinimumWidth(320)
            self.split.addWidget(self.icarus)
            self.split.setStretchFactor(0, 3)
            self.split.setStretchFactor(1, 1)
            tot = max(self.split.width(), 1200)
            self.split.setSizes([tot - 420, 420])
        if self.icarus is not None:
            self.icarus.setVisible(on)
        self.icarus_btn.setChecked(on)
        self.settings.setValue("dims/icarus_visible", on)

    # ============================================================== chat
    def toggle_chat(self, on=None):
        if on is None:
            on = not self.chat.isVisible()
        on = bool(on)
        self.chat.setVisible(on)
        if on:
            sizes = self.split.sizes()
            if sizes[self.split.indexOf(self.chat)] < 200:
                tot = sum(sizes) or 1400
                sizes[self.split.indexOf(self.chat)] = 360
                sizes[0] = max(400, tot - sum(sizes[1:]))
                self.split.setSizes(sizes)
            self.chat.mark_read()
        self.chat_btn.setChecked(on)
        self._chat_unread(self.chat.unread)
        self.settings.setValue("dims/chat_visible", on)

    def _chat_unread(self, n):
        self.chat_btn.setText(f"💬 Chat · {n}" if n else "💬 Chat")
        self.chat_btn.setStyleSheet(f"color:{theme.INFO}; font-weight:600;" if n else "")

    def link_to_chat(self, kind, id_) -> bool:
        if not self.chat.isVisible():
            self.toggle_chat(True)
        ok = self.chat.insert_link(kind, id_)
        if ok:
            self.toast.show_msg("Linked in chat — add a message and press Enter")
        return ok

    def open_link(self, kind, id_):
        """A chat link was clicked: open that record."""
        inv = self.inv
        if kind == "part":
            if inv.part(id_) is None:
                return self.toast.show_msg("That part no longer exists")
            self.parts_sel = id_
            self.goto("parts")
            br = getattr(self, "_browser", None)
            if br is not None:
                br.open_profile()
        elif kind == "project":
            self.collapsed.discard(id_)
            self.goto("projects")
        elif kind == "production":
            o = next((x for x in inv.productionOrders if x.get("id") == id_), None)
            if o is None:
                return self.toast.show_msg("That production order no longer exists")
            self.goto("production")
            self.open_production(o)
        elif kind == "order":
            self.goto("clients")
        elif kind == "vendor":
            self.goto("vendors")
        elif kind == "location":
            self.goto("locations")

    def _link_at(self, obj, pos):
        """What a Shift-click points at: ('link', kind, id) for a record, ('quote', text) for anything else,
        None for controls (buttons, inputs) that should keep their own Shift-click."""
        import re

        from PySide6.QtWidgets import QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QTextEdit
        from ..inventory import chat as C
        view = obj.parent() if obj is not None else None
        if isinstance(view, QAbstractItemView) and obj is view.viewport():
            idx = view.indexAt(pos)
            if not idx.isValid():
                return None
            m = idx.model()
            cols = [idx.column(), *[c for c in range(m.columnCount(idx.parent())) if c != idx.column()]]
            cells = [m.index(idx.row(), c, idx.parent()) for c in cols]
            for ix in cells:
                d = ix.data(LINK_ROLE)
                if d:
                    return ("link", *d)
            ids = [ix.data(r) for ix in cells for r in (Qt.UserRole, Qt.UserRole + 1)]
            texts = [ix.data(Qt.DisplayRole) or "" for ix in cells]
            if isinstance(view, QAbstractItemView) and hasattr(view, "cellWidget"):
                for c in cols:                                    # cells drawn by widgets (labels, spin boxes)
                    w = view.cellWidget(idx.row(), c)
                    if isinstance(w, QLabel):
                        texts.append(re.sub(r"<[^>]+>", " ", w.text()))
            hit = C.resolve(self.inv, [str(t) for t in texts], ids)
            if hit:
                return ("link", *hit)
            return ("quote", str(texts[0]) if texts and texts[0] else " · ".join(str(t) for t in texts if t))
        if isinstance(obj, (QAbstractButton, QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit)) or \
                isinstance(view, (QAbstractSpinBox, QComboBox, QTextEdit)):
            return None
        w = obj
        for _ in range(4):
            if w is None:
                break
            d = w.property("dimsLink") if hasattr(w, "property") else None
            if d:
                k, _s, i = str(d).partition(":")
                return "link", k, i
            w = w.parent()
        if isinstance(obj, QLabel) and obj.text():
            txt = re.sub(r"<[^>]+>", " ", obj.text()).replace("&nbsp;", " ").replace("&amp;", "&")
            hit = C.resolve(self.inv, [txt, *[x for x in re.split(r"\s*·\s*", txt)]])
            return ("link", *hit) if hit else ("quote", txt)
        return None

    def _owned(self, w) -> bool:
        """In DIMS: on this page or in a dialog opened from it."""
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton and \
                ev.modifiers() & Qt.ShiftModifier and isinstance(obj, QWidget) and self._owned(obj) and \
                not self.chat.isAncestorOf(obj):
            what = self._link_at(obj, ev.position().toPoint())
            if what and what[0] == "link" and self.link_to_chat(*what[1:]):
                return True
            if what and what[0] == "quote" and what[1].strip():
                if not self.chat.isVisible():
                    self.toggle_chat(True)
                if self.chat.insert_quote(what[1]):
                    return True
        return super().eventFilter(obj, ev)

    def report_bug(self):
        from .bug_report import BugReportDialog
        BugReportDialog(self.window(), context={"Area": f"DIMS · {self.tabs.tabText(self.tabs.currentIndex())}",
                                                "Data": self.store.location(),
                                                "Role": getattr(self.store, "role", None) or "file",
                                                "Last sync problem": getattr(self.store, "last_error", None) or "—"},
                        inv=self.inv, commit=lambda msg: self.commit(msg)).exec()

    def table_layout(self):
        from .table_prefs import TablePrefsDialog
        d = TablePrefsDialog(self.refresh_current, self, self.settings)
        d.setModal(False)
        d.show()
        self._table_dlg = d

    def guide(self):
        QMessageBox.information(self, "DIMS", D.GUIDE)

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
