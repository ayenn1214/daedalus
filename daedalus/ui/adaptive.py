"""Bars that fit the window: they step down through denser layouts as the window narrows (Auto), or stay at
the level the user picked (View ▸ Toolbar size)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QToolButton

SIZES = {"auto": "Auto (fit the window)", "full": "Full", "compact": "Compact", "icons": "Icons only"}


class Adaptive(QObject):
    """Drives one bar. `apply(level)` sets the bar to level 0 (roomiest) … `levels - 1` (densest);
    in auto mode the first level whose width fits is used on every resize."""

    def __init__(self, bar, apply, levels, mode="auto", fixed=None, content=None):
        super().__init__(bar)
        self.bar, self.apply, self.levels = bar, apply, levels
        self.content = content or bar            # the widget whose layout must fit inside `bar`
        self.fixed = fixed or {"full": 0, "compact": min(1, levels - 1), "icons": levels - 1}
        self.mode = mode
        self.level = -1
        self._busy = False
        bar.installEventFilter(self)

    def set_mode(self, mode):
        self.mode = mode if mode in SIZES else "auto"
        self.level = -1
        self.fit()

    def _set(self, lvl):
        if lvl != self.level:
            self.level = lvl
            self.apply(lvl)

    def fit(self):
        if self._busy:
            return
        self._busy = True
        try:
            if self.mode != "auto":
                self._set(self.fixed.get(self.mode, 0))
                return
            w = self.bar.width()
            for lvl in range(self.levels):
                self._set(lvl)
                lay = self.content.layout()
                if lay is not None:
                    lay.invalidate()                   # hidden / restyled widgets: recompute now
                    lay.activate()
                need = lay.minimumSize().width() if lay is not None else self.content.minimumSizeHint().width()
                if need <= w:
                    return
        finally:
            self._busy = False

    def eventFilter(self, obj, ev):
        if obj is self.bar and ev.type() == QEvent.Show:
            self.level = -1
            self.fit()
        if obj is self.bar and ev.type() == QEvent.Resize:
            old, new = ev.oldSize().width(), ev.size().width()
            if old != new:
                self.fit()
        return False


class _WheelToH(QObject):
    """Mouse wheel scrolls a horizontal-only scroll area sideways."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel:
            sa = obj.parent()
            bar = sa.horizontalScrollBar() if isinstance(sa, QScrollArea) else None
            if bar is not None and bar.maximum() > 0:
                d = ev.angleDelta().y() or ev.angleDelta().x()
                bar.setValue(bar.value() - d)
                return True
        return False


class _Follow(QObject):
    """Keep the scroll area shown / hidden together with the row inside it."""

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.ShowToParent, QEvent.HideToParent):
            self.parent().setVisible(not obj.isHidden())
        return False


def h_scroller(widget, follow=False):
    """Wrap a row so it scrolls sideways (thin bar, wheel works) once even the densest layout doesn't fit.
    follow: the wrapper hides / shows with the row (bars that come and go)."""
    # hidden on purpose (hide() called), not merely a fresh parentless widget
    was_hidden = widget.testAttribute(Qt.WA_WState_ExplicitShowHide) and widget.isHidden()
    sa = QScrollArea()
    sa.setWidget(widget)                      # (this shows the widget — restore its state below)
    if was_hidden:
        widget.hide()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sa.horizontalScrollBar().setFixedHeight(5)
    sa.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget "
                     "{ background: transparent; }")
    f = _WheelToH(sa)
    sa.viewport().installEventFilter(f)
    sa._wheel = f
    sa.setFixedHeight(widget.sizeHint().height() + 6)
    if follow:
        fl = _Follow(sa)
        widget.installEventFilter(fl)
        sa._follow = fl
        sa.setVisible(not was_hidden)
    return sa


def page_scroller(page):
    """A full page that scrolls (both ways) only when the window is smaller than the page's minimum —
    so no page forces the main window to stay wide."""
    sa = QScrollArea()
    sa.setWidget(page)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.page = page
    return sa


def text_mode(buttons, on: bool, under=False):
    """Tool buttons with text (beside / under the icon) or icon only; buttons without an icon keep text."""
    for b in buttons:
        if not isinstance(b, QToolButton):
            continue
        if b.icon().isNull():
            continue
        if on:
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if under else Qt.ToolButtonTextBesideIcon)
        else:
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
