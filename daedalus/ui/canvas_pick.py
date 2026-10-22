"""Selection filter floating on the 3D view (top-left): Part · Face · Edge · Face+Edge.

A frameless tool window (like the δ hub) — Qt widgets can't be drawn over the OpenGL view reliably on
Windows — that follows the view as the window moves and hides with it.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QToolButton, QWidget

from . import icons, theme

MODES = [("part", "Part", "cube", "Pick whole parts (Ctrl+Shift+P)"),
         ("face", "Face", "square", "Pick faces (Ctrl+Shift+F)"),
         ("edge", "Edge", "line-segment", "Pick edges — Fillet / Chamfer use them (Ctrl+Shift+E)"),
         ("any", "Both", "cursor", "Pick faces and edges: an edge when you're on one, else the face "
                                   "(Ctrl+Shift+J)")]


class CanvasPick(QWidget):
    def __init__(self, main, view, on_mode):
        super().__init__(main, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint |
                         Qt.WindowDoesNotAcceptFocus)
        self.main, self.view, self.on_mode = main, view, on_mode
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 5, 6, 5)
        h.setSpacing(2)
        self.group = QButtonGroup(self)
        self.btns = {}
        for key, text, ic, tip in MODES:
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.setText(text)
            i = icons.icon(ic, theme.ICON, theme.ACCENT_LINE)
            if not i.isNull():
                b.setIcon(i)
                b.setIconSize(QSize(15, 15))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setStyleSheet(f"QToolButton {{ padding: 3px 7px; border-radius: 6px; color:{theme.TEXT}; }}"
                            f"QToolButton:checked {{ background:{theme.ACCENT_SOFT}; color:{theme.ACCENT_LINE};"
                            f" border:1px solid {theme.ACCENT_LINE}; }}"
                            f"QToolButton:hover:!checked {{ background:{theme.RAISED}; }}")
            b.clicked.connect(lambda _=False, k=key: self.on_mode(k))
            self.group.addButton(b)
            self.btns[key] = b
            h.addWidget(b)
        self.sync("part")
        self.adjustSize()
        for w in (main, view):
            w.installEventFilter(self)
        stack = getattr(main, "stack", None)
        if stack is not None:
            stack.currentChanged.connect(lambda _i: self.place())
        QTimer.singleShot(0, self.place)

    def sync(self, mode):
        b = self.btns.get(mode)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        c = QColor(theme.PANEL)
        c.setAlpha(235)
        p.fillPath(path, c)
        p.setPen(QColor(theme.BORDER))
        p.drawPath(path)

    def place(self):
        m = self.main
        stack = getattr(m, "stack", None)
        on = (m.isVisible() and not m.isMinimized() and self.view.isVisible()
              and (stack is None or stack.currentIndex() == 0))
        if not on:
            self.hide()
            return
        tl = self.view.mapToGlobal(QPoint(10, 10))
        self.move(tl)
        if not self.isVisible():
            self.show()
        self.raise_()

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide, QEvent.WindowStateChange):
            QTimer.singleShot(0, self.place)
        return False
