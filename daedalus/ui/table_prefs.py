"""Table look for DIMS lists: row spacing, column padding, grid lines, alternating rows, text size, and column
widths you drag (remembered per table). Settings are per PC (QSettings "dims/table/…")."""
from __future__ import annotations

import hashlib

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel, QPushButton,
    QSlider, QTableWidget, QTreeWidget, QVBoxLayout,
)

from . import theme

DEFAULTS = {"row_pad": 4, "col_pad": 6, "grid": False, "hgrid": True, "alt": True, "font_delta": 0.0,
            "resizable": True}
K = "dims/table/"


def prefs(settings=None) -> dict:
    s = settings or QSettings("Daedalus", "Daedalus")
    out = {}
    for k, d in DEFAULTS.items():
        v = s.value(K + k, d)
        out[k] = (str(v).lower() in ("1", "true", "yes")) if isinstance(d, bool) else type(d)(v)
    return out


def save(p: dict, settings=None):
    s = settings or QSettings("Daedalus", "Daedalus")
    for k, v in p.items():
        s.setValue(K + k, v)


def _key(view) -> str:
    h = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
    labels = []
    for i in range(h.count()):
        it = view.headerItem() if isinstance(view, QTreeWidget) else view.horizontalHeaderItem(i)
        labels.append(it.text(i) if isinstance(view, QTreeWidget) else (it.text() if it else ""))
    return hashlib.sha1("|".join(labels).encode()).hexdigest()[:12]


def stylesheet(p: dict) -> str:
    """Lines and row height only. Item padding isn't used for spacing: Qt shrinks the buttons sitting in a cell
    by the item padding, so rows are made taller and columns wider instead."""
    line = theme.BORDER if p["grid"] else "transparent"
    hline = theme.BORDER_SOFT if (p["hgrid"] or p["grid"]) else "transparent"
    return (f"QTableView::item {{ border-bottom: 1px solid {hline}; }}"
            f"QTreeView::item {{ min-height: {22 + 2 * p['row_pad']}px; padding: 0px 4px; "
            f"border-bottom: 1px solid {hline}; border-right: 1px solid {line}; }}"
            f"QTableView {{ gridline-color: {theme.BORDER}; }}")


def apply(view, p: dict | None = None, settings=None):
    """Style one table / tree and restore the widths the user dragged."""
    p = p or prefs(settings)
    s = settings or QSettings("Daedalus", "Daedalus")
    view.setAlternatingRowColors(p["alt"])
    view.setStyleSheet(stylesheet(p))
    if p["font_delta"]:
        f = view.font()
        f.setPointSizeF(max(6.0, f.pointSizeF() + p["font_delta"]))
        view.setFont(f)
    if isinstance(view, QTableWidget):
        view.setShowGrid(p["grid"])
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.resizeRowsToContents()
        for r in range(view.rowCount()):
            view.setRowHeight(r, view.rowHeight(r) + 2 * (p["row_pad"] - 2))
        h = view.horizontalHeader()
        full = h.sizeHint().height() + sum(view.rowHeight(r) for r in range(view.rowCount())) + 6
    else:
        h = view.header()
        rows = sum(1 + (view.topLevelItem(i).childCount() if view.topLevelItem(i).isExpanded() else 0)
                   for i in range(view.topLevelItemCount()))
        full = h.sizeHint().height() + rows * (24 + 2 * p["row_pad"]) + 6
    if view.minimumHeight() and view.minimumHeight() < 700:      # tables sized to show every row keep doing so
        view.setMinimumHeight(min(700, max(view.minimumHeight(), full)))
    key = K + "widths/" + _key(view)
    # Qt measures "fit to contents" columns only once the table is laid out, so widen / free them on the next
    # event-loop turn, when their real widths are known
    QTimer.singleShot(0, lambda: _interactive(view, h, key, s, p))


def _interactive(view, h, key, s, p=None):
    try:
        n = h.count()
    except RuntimeError:                              # table already replaced
        return
    saved = s.value(key) or {}
    saved = dict(saved) if isinstance(saved, dict) else {}
    stretch = {i for i in range(n) if h.sectionResizeMode(i) == QHeaderView.Stretch}
    extra = 2 * max(0, (p or DEFAULTS)["col_pad"] - 6)          # column spacing beyond the stylesheet's 6 px
    widths = {i: max(h.sectionSize(i), h.sectionSizeHint(i)) + extra for i in range(n)}
    resizable = (p or DEFAULTS)["resizable"]
    for i in range(n):
        if i in stretch:
            continue
        if not resizable:
            h.setSectionResizeMode(i, QHeaderView.Fixed)
            h.resizeSection(i, widths[i])
            continue
        h.setSectionResizeMode(i, QHeaderView.Interactive)
        h.resizeSection(i, int(saved.get(str(i), widths[i])))
    h.setStretchLastSection(False)

    def remember(i, _old, new):
        if h.sectionResizeMode(i) != QHeaderView.Interactive:
            return
        cur = s.value(key) or {}
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur[str(i)] = int(new)
        s.setValue(key, cur)
    h.sectionResized.connect(remember)


def apply_all(widget, settings=None):
    p = prefs(settings)
    for v in widget.findChildren(QTableWidget) + widget.findChildren(QTreeWidget):
        apply(v, p, settings)


def reset_widths(settings=None):
    s = settings or QSettings("Daedalus", "Daedalus")
    for k in s.allKeys():
        if k.startswith(K + "widths/"):
            s.remove(k)


class TablePrefsDialog(QDialog):
    """Row spacing, column padding, grid lines, alternating rows, text size — applied live."""

    def __init__(self, on_change, parent=None, settings=None):
        super().__init__(parent)
        self.on_change, self.s = on_change, settings or QSettings("Daedalus", "Daedalus")
        self.setWindowTitle("Table layout")
        p = prefs(self.s)
        v = QVBoxLayout(self)
        f = QFormLayout()

        def slider(lo, hi, val):
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(int(val))
            sl.valueChanged.connect(self.changed)
            return sl
        self.row = slider(0, 14, p["row_pad"])
        self.col = slider(0, 24, p["col_pad"])
        self.font = slider(-3, 6, p["font_delta"])
        self.grid = QCheckBox("Grid lines between cells")
        self.grid.setChecked(p["grid"])
        self.hgrid = QCheckBox("Line under each row")
        self.hgrid.setChecked(p["hgrid"])
        self.alt = QCheckBox("Alternate row shading")
        self.alt.setChecked(p["alt"])
        self.resz = QCheckBox("Drag column edges to resize (widths remembered)")
        self.resz.setChecked(p["resizable"])
        for w in (self.grid, self.hgrid, self.alt, self.resz):
            w.toggled.connect(self.changed)
        self.lab = QLabel()
        self.lab.setObjectName("muted")
        f.addRow("Row spacing", self.row)
        f.addRow("Column spacing", self.col)
        f.addRow("Text size", self.font)
        v.addLayout(f)
        for w in (self.grid, self.hgrid, self.alt, self.resz):
            v.addWidget(w)
        v.addWidget(self.lab)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        rw = QPushButton("Reset column widths")
        rw.clicked.connect(lambda: (reset_widths(self.s), self.on_change()))
        rd = QPushButton("Defaults")
        rd.clicked.connect(self.defaults)
        bb.addButton(rw, QDialogButtonBox.ActionRole)
        bb.addButton(rd, QDialogButtonBox.ResetRole)
        bb.rejected.connect(self.accept)
        v.addWidget(bb)
        self._label()

    def values(self):
        return {"row_pad": self.row.value(), "col_pad": self.col.value(), "font_delta": float(self.font.value()),
                "grid": self.grid.isChecked(), "hgrid": self.hgrid.isChecked(), "alt": self.alt.isChecked(),
                "resizable": self.resz.isChecked()}

    def _label(self):
        p = self.values()
        self.lab.setText(f"Rows {p['row_pad']} px · columns {p['col_pad']} px · text {p['font_delta']:+g} pt")

    def changed(self, *_a):
        save(self.values(), self.s)
        self._label()
        self.on_change()

    def defaults(self):
        for w, k in ((self.row, "row_pad"), (self.col, "col_pad"), (self.font, "font_delta")):
            w.blockSignals(True)
            w.setValue(int(DEFAULTS[k]))
            w.blockSignals(False)
        for w, k in ((self.grid, "grid"), (self.hgrid, "hgrid"), (self.alt, "alt"), (self.resz, "resizable")):
            w.blockSignals(True)
            w.setChecked(DEFAULTS[k])
            w.blockSignals(False)
        self.changed()
