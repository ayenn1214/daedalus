"""A date box you can type into: 2026-11-20, 11/20, Nov 20, fri, tomorrow, +10, 2w … — or pick from a calendar.
Blank means no date. Shows the weekday and how far away it is."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (
    QCalendarWidget, QHBoxLayout, QLabel, QLineEdit, QMenu, QToolButton, QWidget, QWidgetAction,
)

from ..inventory import model as M
from . import theme

HINT = "Type a date — 11/20, Nov 20, fri, tomorrow, +10 (days), 2w — or pick one"


def show_date(iso: str) -> str:
    d = M.parse_date(iso)
    return d.strftime("%a %b %d, %Y") if d else ""


def relative(iso: str, base: dt.date | None = None) -> str:
    d = M.parse_date(iso)
    if d is None:
        return ""
    n = (d - (base or dt.date.today())).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    if abs(n) >= 14:
        w = round(abs(n) / 7)
        span = f"{w} weeks"
    else:
        span = f"{abs(n)} days"
    return f"in {span}" if n > 0 else f"{span} ago"


class DateField(QWidget):
    changed = Signal(str)                      # ISO date, '' when cleared — only when the value really changes

    def __init__(self, value: str = "", parent=None, placeholder="—", relative_hint=True, clearable=True):
        super().__init__(parent)
        self._value = ""
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setToolTip(HINT)
        self.edit.setMinimumWidth(150)
        self.edit.editingFinished.connect(self._typed)
        h.addWidget(self.edit, 1)
        self.cal_btn = QToolButton()
        self.cal_btn.setText("▾")
        self.cal_btn.setToolTip("Pick from a calendar")
        self.cal_btn.setAutoRaise(True)
        self.cal_btn.clicked.connect(self._popup)
        h.addWidget(self.cal_btn)
        self.clear_btn = None
        if clearable:
            self.clear_btn = QToolButton()
            self.clear_btn.setText("×")
            self.clear_btn.setToolTip("No date")
            self.clear_btn.setAutoRaise(True)
            self.clear_btn.clicked.connect(lambda: self.set_value("", emit=True))
            sp = self.clear_btn.sizePolicy()
            sp.setRetainSizeWhenHidden(True)                  # fields line up whether or not they have a date
            self.clear_btn.setSizePolicy(sp)
            h.addWidget(self.clear_btn)
        self.rel = QLabel() if relative_hint else None
        if self.rel is not None:
            self.rel.setObjectName("muted")
            self.rel.setMinimumWidth(78)
            h.addWidget(self.rel)
        self.set_value(value or "")

    # ------------------------------------------------------------ value
    def value(self) -> str:
        self._typed()                            # text typed but not yet committed (Enter / focus out)
        return self._value

    def set_value(self, iso: str, emit=False):
        iso = iso or ""
        old = self._value
        self._value = iso
        self.edit.setText(show_date(iso))
        self.edit.setStyleSheet("")
        if self.rel is not None:
            self.rel.setText(relative(iso))
        if self.clear_btn is not None:
            self.clear_btn.setVisible(bool(iso))
        if emit and iso != old:
            self.changed.emit(iso)

    def _typed(self):
        text = self.edit.text()
        if text == show_date(self._value):
            return
        iso = M.parse_date_input(text)
        if iso is None:
            self.edit.setStyleSheet(f"border:1px solid {theme.ERROR};")
            self.edit.setToolTip(f"Can't read “{text}” as a date.\n{HINT}")
            return
        self.edit.setToolTip(HINT)
        self.set_value(iso, emit=True)

    def _popup(self):
        menu = QMenu(self)
        cal = QCalendarWidget()
        cal.setGridVisible(False)
        d = M.parse_date(self._value) or dt.date.today()
        cal.setSelectedDate(QDate(d.year, d.month, d.day))
        cal.clicked.connect(lambda q: (self.set_value(q.toString("yyyy-MM-dd"), emit=True), menu.close()))
        act = QWidgetAction(menu)
        act.setDefaultWidget(cal)
        menu.addAction(act)
        quick = QWidget()
        qh = QHBoxLayout(quick)
        qh.setContentsMargins(6, 2, 6, 6)
        for lab, days in (("Today", 0), ("+1 week", 7), ("+2 weeks", 14), ("+30 days", 30)):
            b = QToolButton()
            b.setText(lab)
            b.clicked.connect(lambda _=False, n=days: (self.set_value(M.add_days(M.today(), n), emit=True),
                                                       menu.close()))
            qh.addWidget(b)
        qa = QWidgetAction(menu)
        qa.setDefaultWidget(quick)
        menu.addAction(qa)
        menu.exec(self.edit.mapToGlobal(self.edit.rect().bottomLeft()))
