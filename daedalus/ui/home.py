"""Home screen: start actions, recent files with thumbnails and pins, crash recovery, start-up options."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QScrollArea, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import theme
from .recent import ago, human_size

CARD_W, CARD_H, THUMB_H = 236, 214, 148

START = [
    ("new", "New part", "Ctrl+N"),
    ("open", "Open…", "Ctrl+O"),
    ("import", "Open STEP / IGES / STL…", ""),
    ("doodle", "Doodle → model", ""),
    ("drawing2d", "Open 2D drawing…", ""),
]
TOOLS = [
    ("std", "Standard parts library", "Ctrl+L"),
    ("inventory", "Inventory", "Ctrl+Shift+I"),
    ("quote", "Quote / machine schedule", "Ctrl+Alt+Q"),
    ("gcode", "Simulate G-code…", ""),
    ("sw", "SolidWorks Link", "Ctrl+Shift+W"),
    ("fusion", "Fusion Link", ""),
]
TIPS = [
    "Big STEP files load in the background — keep working while parts appear.",
    "View ▸ Display quality ▸ Draft makes very large assemblies faster to display.",
    "Drag files onto the window to open or import them.",
    "Ctrl+Shift+H toggles the δ tool hub; drag it anywhere in the view.",
    "Right-click a recent file to pin it, show it in its folder or forget it.",
]

EXT_COL = {"daedalus": theme.ACCENT_LINE, "strata": theme.ACCENT_LINE, "step": "#4f9fd8", "stp": "#4f9fd8",
           "iges": "#7a8fd8", "igs": "#7a8fd8", "stl": "#57ab5a", "obj": "#57ab5a", "3mf": "#57ab5a",
           "brep": "#b591e8", "dxf": "#d4a72c", "dwg": "#d4a72c", "sldprt": "#e5534b", "sldasm": "#e5534b"}


class _CardDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: dict[str, QPixmap] = {}

    def sizeHint(self, *_):
        return QSize(CARD_W + 12, CARD_H + 12)

    def _thumb(self, path):
        if path not in self._pix:
            self._pix[path] = QPixmap(path)
        return self._pix[path]

    def forget(self):
        self._pix.clear()

    def paint(self, p: QPainter, opt, idx):
        e = idx.data(Qt.UserRole) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(6, 6, -6, -6)
        hover = bool(opt.state & QStyle.State_MouseOver)
        sel = bool(opt.state & QStyle.State_Selected)
        card = QPainterPath()
        card.addRoundedRect(r, 10, 10)
        p.fillPath(card, QColor(theme.RAISED if hover else theme.PANEL))
        p.setPen(QPen(QColor(theme.ACCENT_LINE if sel or hover else theme.BORDER_SOFT), 1.4 if sel or hover else 1))
        p.drawPath(card)
        # thumbnail
        tr = QRectF(r.x() + 1, r.y() + 1, r.width() - 2, THUMB_H)
        clip = QPainterPath()
        clip.addRoundedRect(tr.adjusted(0, 0, 0, 12), 9, 9)        # round top corners, square bottom
        p.setClipRect(tr)
        p.setClipPath(clip, Qt.IntersectClip)
        thumb = e.get("thumb")
        if thumb and not self._thumb(thumb).isNull():
            pm = self._thumb(thumb)
            p.drawPixmap(tr.toRect(), pm, pm.rect())
        else:
            p.fillRect(tr, QColor(theme.PANEL_2))
            col = QColor(EXT_COL.get(e.get("ext", ""), theme.MUTED))
            f = QFont(p.font())
            f.setPointSizeF(20)
            f.setBold(True)
            p.setFont(f)
            p.setPen(col)
            if e.get("exists", True):
                p.drawText(tr, Qt.AlignCenter, (e.get("ext") or "?").upper())
        if not e.get("exists", True):
            p.fillRect(tr, QColor(0, 0, 0, 140))
            f = QFont(p.font())
            f.setPointSizeF(10)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(theme.ERROR))
            p.drawText(tr, Qt.AlignCenter, "missing")
        p.setClipping(False)
        # ext badge + pin
        f = QFont(p.font())
        f.setPointSizeF(7.5)
        f.setBold(True)
        p.setFont(f)
        ext = (e.get("ext") or "").upper()
        if ext:
            bw = p.fontMetrics().horizontalAdvance(ext) + 12
            br = QRectF(tr.x() + 8, tr.y() + 8, bw, 17)
            bp = QPainterPath()
            bp.addRoundedRect(br, 8, 8)
            p.fillPath(bp, QColor(0, 0, 0, 150))
            p.setPen(QColor(EXT_COL.get(e.get("ext", ""), "#ffffff")))
            p.drawText(br, Qt.AlignCenter, ext)
        if e.get("pinned"):
            f.setPointSizeF(11)
            p.setFont(f)
            p.setPen(QColor(theme.ACCENT_LINE))
            p.drawText(QRectF(tr.right() - 30, tr.y() + 4, 24, 24), Qt.AlignCenter, "★")
        # text
        tx = QRectF(r.x() + 12, tr.bottom() + 8, r.width() - 24, 20)
        f = QFont(p.font())
        f.setPointSizeF(9.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if e.get("exists", True) else theme.DIM))
        p.drawText(tx, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(e.get("name", ""), Qt.ElideMiddle, int(tx.width())))
        f.setBold(False)
        f.setPointSizeF(8)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        sub = "  ·  ".join(x for x in (ago(e.get("mtime", 0)), human_size(e["size"]) if e.get("size") else "") if x)
        p.drawText(QRectF(tx.x(), tx.bottom() + 1, tx.width(), 17), Qt.AlignLeft | Qt.AlignVCenter, sub)
        p.setPen(QColor(theme.DIM))
        p.drawText(QRectF(tx.x(), tx.bottom() + 17, tx.width(), 17), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(e.get("folder", ""), Qt.ElideLeft, int(tx.width())))
        p.restore()


class _Logo(QWidget):
    def __init__(self, version, parent=None):
        super().__init__(parent)
        self.version = version
        self.setFixedHeight(64)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        from .splash import logo_pixmap
        if getattr(self, "_pm", None) is None or self._pm_dpr != self.devicePixelRatioF():
            self._pm_dpr = self.devicePixelRatioF()
            self._pm = logo_pixmap(52, self._pm_dpr)
        p.drawPixmap(2, 6, self._pm)
        f = QFont(self.font())
        f.setPointSizeF(19)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRect(68, 4, 400, 34), Qt.AlignLeft | Qt.AlignVCenter, "Daedalus")
        f.setPointSizeF(8.5)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRect(70, 36, 400, 20), Qt.AlignLeft | Qt.AlignVCenter, f"version {self.version}")


class HomePage(QWidget):
    open_requested = Signal(str)
    action = Signal(str)             # new, open, import, doodle, drawing2d, std, inventory, sw, fusion, back,
    #                                  recover:<id>, discard:<id>

    def __init__(self, recent, settings, version="", parent=None):
        super().__init__(parent)
        self.recent = recent
        self.settings = settings
        self.recover_items: list[dict] = []
        self.setObjectName("home")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget#home {{ background: {theme.BG}; }}
            QFrame#homeside {{ background: {theme.PANEL}; border-right: 1px solid {theme.BORDER_SOFT}; }}
            QPushButton#homebtn {{ text-align: left; padding: 8px 12px; border: 1px solid transparent;
                                   border-radius: 7px; background: transparent; color: {theme.TEXT}; }}
            QPushButton#homebtn:hover {{ background: {theme.RAISED}; border-color: {theme.BORDER}; }}
            QPushButton#homeprimary {{ text-align: left; padding: 9px 12px; border-radius: 7px; font-weight: 600;
                                       background: {theme.ACCENT}; color: {theme.ON_ACCENT}; border: none; }}
            QPushButton#homeprimary:hover {{ background: {theme.ACCENT_HOVER}; }}
            QPushButton#seg {{ padding: 4px 12px; border: 1px solid {theme.BORDER}; border-radius: 6px;
                               background: transparent; color: {theme.MUTED}; }}
            QPushButton#seg:checked {{ background: {theme.ACCENT_SOFT}; color: {theme.TEXT};
                                       border-color: {theme.ACCENT_LINE}; }}
            QListWidget#cards {{ background: transparent; border: none; }}
            QFrame#recover {{ background: {theme.ACCENT_SOFT}; border: 1px solid {theme.ACCENT_LINE};
                              border-radius: 8px; }}
        """)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- side column
        side = QFrame()
        side.setObjectName("homeside")
        side.setFixedWidth(290)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(20, 18, 20, 18)
        sv.setSpacing(4)
        sv.addWidget(_Logo(version))
        sv.addSpacing(12)
        self.buttons: dict[str, QPushButton] = {}

        def head(t):
            lab = QLabel(t)
            lab.setObjectName("h2")
            sv.addSpacing(10)
            sv.addWidget(lab)

        def btn(key, text, keys, primary=False):
            b = QPushButton(f"{text}" + (f"     {keys}" if keys and not primary else ""))
            b.setObjectName("homeprimary" if primary else "homebtn")
            b.setCursor(Qt.PointingHandCursor)
            if keys:
                b.setToolTip(keys)
            b.clicked.connect(lambda _=False, k=key: self.action.emit(k))
            sv.addWidget(b)
            self.buttons[key] = b

        head("START")
        for i, (k, t, ks) in enumerate(START):
            btn(k, t, ks, primary=(i == 0))
        head("TOOLS")
        for k, t, ks in TOOLS:
            btn(k, t, ks)
        sv.addStretch(1)
        head("ON START-UP")
        self.show_home = QCheckBox("Show this Home screen")
        self.show_home.setChecked(settings.value("home/show", True, type=bool))
        self.show_home.toggled.connect(lambda on: settings.setValue("home/show", on))
        self.reopen = QCheckBox("Reopen the last file")
        self.reopen.setChecked(settings.value("home/reopen_last", False, type=bool))
        self.reopen.toggled.connect(lambda on: settings.setValue("home/reopen_last", on))
        sv.addWidget(self.show_home)
        sv.addWidget(self.reopen)
        sv.addSpacing(8)
        self.back = QPushButton("Back to the model  ›")
        self.back.setObjectName("homebtn")
        self.back.clicked.connect(lambda: self.action.emit("back"))
        sv.addWidget(self.back)
        root.addWidget(side)

        # ---- main column
        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(28, 22, 28, 14)
        mv.setSpacing(10)
        top = QHBoxLayout()
        title = QLabel("Recent")
        title.setObjectName("h1")
        f = title.font()
        f.setPointSizeF(15)
        title.setFont(f)
        top.addWidget(title)
        top.addSpacing(16)
        self.seg = QButtonGroup(self)
        for i, t in enumerate(("All", "Pinned", "Parts", "CAD imports")):
            b = QPushButton(t)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.seg.addButton(b, i)
            top.addWidget(b)
        self.seg.idClicked.connect(lambda _i: self.refresh())
        top.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search recent files…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        self.search.textChanged.connect(lambda _t: self.refresh())
        top.addWidget(self.search)
        mv.addLayout(top)

        self.recover_box = QVBoxLayout()
        self.recover_box.setSpacing(6)
        mv.addLayout(self.recover_box)

        self.cards = QListWidget()
        self.cards.setObjectName("cards")
        self.cards.setViewMode(QListWidget.IconMode)
        self.cards.setResizeMode(QListWidget.Adjust)
        self.cards.setMovement(QListWidget.Static)
        self.cards.setUniformItemSizes(True)
        self.cards.setSpacing(0)
        self.cards.setMouseTracking(True)
        self.cards.setSelectionMode(QListWidget.SingleSelection)
        self.cards.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.delegate = _CardDelegate(self.cards)
        self.cards.setItemDelegate(self.delegate)
        self.cards.itemActivated.connect(self._activate)
        self.cards.itemClicked.connect(self._activate)
        self.cards.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cards.customContextMenuRequested.connect(self._menu)
        mv.addWidget(self.cards, 1)
        self.empty = QLabel("No recent files yet.\nOpen a part or a STEP file and it will show up here.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("muted")
        mv.addWidget(self.empty, 1)

        bottom = QHBoxLayout()
        self.tip = QLabel()
        self.tip.setObjectName("hint")
        bottom.addWidget(self.tip, 1)
        clear = QPushButton("Clear list")
        clear.setObjectName("homebtn")
        clear.setToolTip("Forget recent files (pinned ones stay)")
        clear.clicked.connect(self._clear)
        bottom.addWidget(clear)
        mv.addLayout(bottom)
        root.addWidget(main, 1)
        self._tip_i = int(settings.value("home/tip", 0)) % len(TIPS)
        settings.setValue("home/tip", self._tip_i + 1)
        self.tip.setText("Tip — " + TIPS[self._tip_i])

    # ------------------------------------------------------------- data
    def set_recoverable(self, items: list[dict]):
        self.recover_items = list(items)
        while self.recover_box.count():
            w = self.recover_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for it in self.recover_items[:3]:
            fr = QFrame()
            fr.setObjectName("recover")
            h = QHBoxLayout(fr)
            h.setContentsMargins(12, 8, 8, 8)
            from .recent import ago as _ago
            lab = QLabel(f"<b>Unsaved work found</b> — {it.get('title') or 'Untitled'}"
                         f"<span style='color:{theme.MUTED}'>  ·  autosaved {_ago(float(it.get('saved') or 0))}"
                         f"</span>")
            h.addWidget(lab, 1)
            for key, text in (("recover", "Recover"), ("discard", "Discard")):
                b = QPushButton(text)
                b.setObjectName("homeprimary" if key == "recover" else "homebtn")
                b.clicked.connect(lambda _=False, k=key, i=it["id"]: self.action.emit(f"{k}:{i}"))
                h.addWidget(b)
            self.recover_box.addWidget(fr)

    def set_has_model(self, on: bool):
        self.back.setVisible(on)

    def refresh(self):
        self.delegate.forget()
        mode = self.seg.checkedId()
        rows = self.recent.entries(self.search.text())
        if mode == 1:
            rows = [r for r in rows if r["pinned"]]
        elif mode == 2:
            rows = [r for r in rows if r["ext"] in ("daedalus", "strata")]
        elif mode == 3:
            rows = [r for r in rows if r["ext"] not in ("daedalus", "strata")]
        self.cards.clear()
        for r in rows:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, r)
            it.setToolTip(r["path"])
            it.setSizeHint(QSize(CARD_W + 12, CARD_H + 12))
            self.cards.addItem(it)
        none = not rows
        self.cards.setVisible(not none)
        self.empty.setVisible(none)
        if none and (self.search.text() or mode):
            self.empty.setText("Nothing matches.")
        elif none:
            self.empty.setText("No recent files yet.\nOpen a part or a STEP file and it will show up here.")

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.search.setFocus()

    # ----------------------------------------------------------- events
    def _activate(self, it):
        r = it.data(Qt.UserRole) or {}
        if r.get("exists"):
            self.open_requested.emit(r["path"])

    def _clear(self):
        self.recent.clear(keep_pinned=True)
        self.refresh()

    def _menu(self, pos):
        it = self.cards.itemAt(pos)
        if not it:
            return
        r = it.data(Qt.UserRole) or {}
        m = QMenu(self)
        a_open = m.addAction("Open")
        a_open.setEnabled(r.get("exists", False))
        a_pin = m.addAction("Unpin" if r.get("pinned") else "Pin to top")
        a_dir = m.addAction("Show in folder")
        a_dir.setEnabled(Path(r.get("folder", "")).exists())
        a_copy = m.addAction("Copy path")
        m.addSeparator()
        a_rm = m.addAction("Remove from list")
        got = m.exec(self.cards.viewport().mapToGlobal(pos))
        if got is a_open:
            self.open_requested.emit(r["path"])
        elif got is a_pin:
            self.recent.set_pinned(r["path"], not r.get("pinned"))
            self.refresh()
        elif got is a_dir:
            show_in_folder(r["path"])
        elif got is a_copy:
            QApplication.clipboard().setText(r["path"])
        elif got is a_rm:
            self.recent.remove(r["path"])
            self.refresh()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.back.isVisible():
            self.action.emit("back")
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.search.hasFocus() and self.cards.count():
            self._activate(self.cards.item(0))
            return
        super().keyPressEvent(e)


def show_in_folder(path):
    p = Path(path)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(p)] if p.exists() else ["explorer", str(p.parent)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.exists() else ["open", str(p.parent)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
    except Exception:  # noqa: BLE001
        pass


def scroll(w):
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setWidget(w)
    return sc
