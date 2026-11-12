"""Table look for DIMS lists, adjusted like a spreadsheet:
- drag a column edge to resize, double-click it to fit; right-click a column heading for Autofit, Column width…,
  Hide / Unhide columns and Reset;
- row numbers down the side (optional): drag a row edge to resize, double-click to fit, right-click for Row height…;
- plus overall row / column spacing, grid lines, alternating rows and text size (▦ Table layout).
Widths, hidden columns and row height are remembered per table, per PC (QSettings "dims/table/…")."""
from __future__ import annotations

import hashlib

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel, QPushButton,
    QSlider, QTableWidget, QTreeWidget, QVBoxLayout,
)

from . import theme

DEFAULTS = {"row_pad": 4, "col_pad": 6, "grid": False, "hgrid": True, "alt": True, "font_delta": 0.0,
            "resizable": True, "row_numbers": False}
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


def table_state(s, key) -> dict:
    """{"w": {col: px}, "hidden": [col], "rowh": px or 0}. Older saves held just the widths."""
    v = s.value(key) or {}
    v = dict(v) if isinstance(v, dict) else {}
    if v and "w" not in v and all(str(k).isdigit() for k in v):
        v = {"w": v}
    return {"w": {str(k): int(x) for k, x in dict(v.get("w") or {}).items()},
            "hidden": [int(x) for x in (v.get("hidden") or [])], "rowh": int(v.get("rowh") or 0)}


def _save_state(s, key, st):
    s.setValue(key, st)


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
    key = K + "widths/" + _key(view)
    st = table_state(s, key)
    if isinstance(view, QTableWidget):
        view.setShowGrid(p["grid"])
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.resizeRowsToContents()
        for r in range(view.rowCount()):
            view.setRowHeight(r, max(view.rowHeight(r) + 2 * (p["row_pad"] - 2), st["rowh"]))
        vh = view.verticalHeader()
        vh.setVisible(p["row_numbers"])
        vh.setSectionResizeMode(QHeaderView.Interactive)
        vh.setMinimumSectionSize(14)
        vh.setContextMenuPolicy(Qt.CustomContextMenu)
        vh.customContextMenuRequested.connect(lambda pos, v=view, k=key: _row_menu(v, k, s, pos))
        vh.setToolTip("Drag a row edge to resize · double-click to fit · right-click for Row height")
        h = view.horizontalHeader()
        full = h.sizeHint().height() + sum(view.rowHeight(r) for r in range(view.rowCount())) + 6
    else:
        h = view.header()
        rows = sum(1 + (view.topLevelItem(i).childCount() if view.topLevelItem(i).isExpanded() else 0)
                   for i in range(view.topLevelItemCount()))
        full = h.sizeHint().height() + rows * max(24 + 2 * p["row_pad"], st["rowh"]) + 6
        if st["rowh"]:
            view.setStyleSheet(view.styleSheet() + f"QTreeView::item {{ min-height: {st['rowh']}px; }}")
    if view.minimumHeight() and view.minimumHeight() < 700:      # tables sized to show every row keep doing so
        view.setMinimumHeight(min(700, max(view.minimumHeight(), full)))
    h.setContextMenuPolicy(Qt.CustomContextMenu)
    h.customContextMenuRequested.connect(lambda pos, v=view, hh=h, k=key: _col_menu(v, hh, k, s, pos))
    h.setToolTip("Drag a column edge to resize · double-click it to fit · right-click for more")
    # Qt measures "fit to contents" columns only once the table is laid out, so widen / free them on the next
    # event-loop turn, when their real widths are known
    QTimer.singleShot(0, lambda: _interactive(view, h, key, s, p))


def _interactive(view, h, key, s, p=None):
    try:
        n = h.count()
    except RuntimeError:                              # table already replaced
        return
    st = table_state(s, key)
    saved = st["w"]
    for i in st["hidden"]:
        if i < n:
            h.setSectionHidden(i, True)
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
        if h.sectionResizeMode(i) != QHeaderView.Interactive or new <= 0:
            return
        cur = table_state(s, key)
        cur["w"][str(i)] = int(new)
        _save_state(s, key, cur)
    h.sectionResized.connect(remember)


# ------------------------------------------------------------------ spreadsheet-style menus
def _label(view, i):
    if isinstance(view, QTreeWidget):
        return view.headerItem().text(i) or f"Column {i + 1}"
    it = view.horizontalHeaderItem(i)
    return (it.text() if it else "") or f"Column {i + 1}"


def autofit(view, i):
    h = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
    if h.sectionResizeMode(i) == QHeaderView.Stretch:
        return
    view.resizeColumnToContents(i)
    h.resizeSection(i, max(h.sectionSize(i), h.sectionSizeHint(i)))


def _col_menu(view, h, key, s, pos):
    col_menu(view, h, key, s, h.logicalIndexAt(pos)).exec(h.mapToGlobal(pos))


def col_menu(view, h, key, s, i):
    """The right-click menu for column heading i (-1 = empty header area)."""
    from PySide6.QtWidgets import QInputDialog, QMenu
    m = QMenu(view)
    if i >= 0:
        name = _label(view, i)
        m.addAction(f"Autofit “{name}”", lambda: autofit(view, i))

        def width():
            w, ok = QInputDialog.getInt(view, "Column width", f"Width of “{name}” (pixels):", h.sectionSize(i), 20,
                                        2000, 5)
            if ok:
                if h.sectionResizeMode(i) == QHeaderView.Stretch:
                    h.setSectionResizeMode(i, QHeaderView.Interactive)
                h.resizeSection(i, w)
        m.addAction("Column width…", width)

        def hide():
            visible = [j for j in range(h.count()) if not h.isSectionHidden(j)]
            if len(visible) <= 1:
                return
            h.setSectionHidden(i, True)
            st = table_state(s, key)
            st["hidden"] = sorted(set(st["hidden"]) | {i})
            _save_state(s, key, st)
        m.addAction(f"Hide “{name}”", hide)
    m.addAction("Autofit all columns", lambda: [autofit(view, j) for j in range(h.count()) if not h.isSectionHidden(j)])
    hidden = [j for j in range(h.count()) if h.isSectionHidden(j)]
    if hidden:
        un = m.addMenu("Unhide")

        def show(js):
            st = table_state(s, key)
            for j in js:
                h.setSectionHidden(j, False)
                if h.sectionSize(j) < 20:
                    h.resizeSection(j, 80)
            st["hidden"] = [x for x in st["hidden"] if x not in js]
            _save_state(s, key, st)
        for j in hidden:
            un.addAction(_label(view, j), lambda j=j: show([j]))
        un.addSeparator()
        un.addAction("All columns", lambda: show(hidden))
    m.addSeparator()

    def reset():
        s.remove(key)
        for j in range(h.count()):
            h.setSectionHidden(j, False)
        for j in range(h.count()):
            if h.sectionResizeMode(j) != QHeaderView.Stretch:
                autofit(view, j)
        if isinstance(view, QTableWidget):
            view.resizeRowsToContents()
    m.addAction("Reset this table", reset)
    return m


def _row_menu(view, key, s, pos):
    vh = view.verticalHeader()
    row_menu(view, key, s, vh.logicalIndexAt(pos)).exec(vh.mapToGlobal(pos))


def row_menu(view, key, s, r):
    from PySide6.QtWidgets import QInputDialog, QMenu
    m = QMenu(view)

    def height():
        cur = view.rowHeight(r) if r >= 0 else (table_state(s, key)["rowh"] or 30)
        hgt, ok = QInputDialog.getInt(view, "Row height", "Height of every row in this table (pixels):", cur, 14, 400)
        if ok:
            for j in range(view.rowCount()):
                view.setRowHeight(j, hgt)
            st = table_state(s, key)
            st["rowh"] = hgt
            _save_state(s, key, st)
    m.addAction("Row height…", height)

    def fit():
        view.resizeRowsToContents()
        st = table_state(s, key)
        st["rowh"] = 0
        _save_state(s, key, st)
    m.addAction("Autofit rows", fit)
    return m


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
        self.rown = QCheckBox("Row numbers down the side (drag row edges to resize)")
        self.rown.setChecked(p["row_numbers"])
        for w in (self.grid, self.hgrid, self.alt, self.resz, self.rown):
            w.toggled.connect(self.changed)
        self.lab = QLabel()
        self.lab.setObjectName("muted")
        f.addRow("Row spacing", self.row)
        f.addRow("Column spacing", self.col)
        f.addRow("Text size", self.font)
        v.addLayout(f)
        for w in (self.grid, self.hgrid, self.alt, self.resz, self.rown):
            v.addWidget(w)
        tip = QLabel("In any table: drag a column edge to resize, double-click it to fit, right-click a column "
                     "heading for Autofit / Column width / Hide / Unhide.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        v.addWidget(tip)
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
                "resizable": self.resz.isChecked(), "row_numbers": self.rown.isChecked()}

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
        for w, k in ((self.grid, "grid"), (self.hgrid, "hgrid"), (self.alt, "alt"), (self.resz, "resizable"),
                     (self.rown, "row_numbers")):
            w.blockSignals(True)
            w.setChecked(DEFAULTS[k])
            w.blockSignals(False)
        self.changed()
