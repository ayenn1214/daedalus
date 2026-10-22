"""Layers panel.

Click a row to make it the active layer (new features go there). Columns:
colour (click to change) · name (double-click to rename) · 👁 visible · 🔒 locked ·
⏻ in model · feature count. Buttons add/delete layers and move the features
selected in History onto the selected layer.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QHBoxLayout, QHeaderView, QInputDialog, QMessageBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from ..core.layers import DEFAULT
from . import theme

C_COLOR, C_NAME, C_VIS, C_LOCK, C_MODEL, C_COUNT = range(6)


def swatch(color: str, size=12, ring=False) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(QColor(theme.TEXT) if ring else Qt.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


class LayersPanel(QWidget):
    move_requested = Signal(str)        # layer name; main window supplies the selected features

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["", "Layer", "👁", "🔒", "⏻", "#"])
        from . import icons
        icons.header_icons(self.tree, {2: "eye", 3: "lock", 4: "power"})
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = self.tree.header()
        h.setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        for c in (C_COLOR, C_VIS, C_LOCK, C_MODEL, C_COUNT):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.headerItem().setToolTip(C_VIS, "Visible")
        self.tree.headerItem().setToolTip(C_LOCK, "Locked — no edits")
        self.tree.headerItem().setToolTip(C_MODEL, "In model — off leaves the layer's features out of the rebuild")
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double)
        lay.addWidget(self.tree)
        row = QHBoxLayout()
        for text, fn, tip in (("+ Layer", self.add, "New layer (becomes active)"),
                              ("Delete", self.delete, "Delete the selected layer; its features move to Default"),
                              ("Move selected ▸", self.move_here,
                               "Move the features selected in History onto the selected layer")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

    # ---------------------------------------------------------- refresh
    def refresh(self):
        sel = self.selected()
        self.tree.blockSignals(True)
        self.tree.clear()
        counts = self.doc.layer_counts()
        for l in self.doc.layers:
            active = l.name == self.doc.active_layer
            it = QTreeWidgetItem(["", l.name, "●" if l.visible else "○", "■" if l.locked else "□",
                                  "●" if l.in_model else "○", str(counts.get(l.name, 0))])
            it.setIcon(C_COLOR, swatch(l.color, ring=active))
            it.setData(C_NAME, Qt.UserRole, l.name)
            f = QFont()
            f.setBold(active)
            it.setFont(C_NAME, f)
            if active:
                it.setText(C_NAME, f"▸ {l.name}")
                it.setToolTip(C_NAME, "Active layer — new features go here")
            dim = QBrush(QColor(theme.MUTED))
            for c in (C_VIS, C_LOCK, C_MODEL, C_COUNT):
                it.setTextAlignment(c, Qt.AlignCenter)
                it.setForeground(c, dim)
            if l.locked:
                it.setForeground(C_LOCK, QBrush(QColor(theme.ACCENT_LINE)))
            if not l.in_model:
                it.setForeground(C_NAME, QBrush(QColor(theme.DIM)))
            if not l.visible:
                it.setForeground(C_NAME, QBrush(QColor(theme.DIM)))
            self.tree.addTopLevelItem(it)
            if l.name == sel:
                it.setSelected(True)
                self.tree.setCurrentItem(it)
        self.tree.blockSignals(False)

    def selected(self) -> str | None:
        it = self.tree.currentItem()
        return it.data(C_NAME, Qt.UserRole) if it else None

    # ---------------------------------------------------------- actions
    def _try(self, fn, *a, **k):
        try:
            fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "Layers", str(e))

    def _clicked(self, it, col):
        name = it.data(C_NAME, Qt.UserRole)
        l = self.doc.layer(name)
        if col == C_VIS:
            self._try(self.doc.set_layer, name, visible=not l.visible)
        elif col == C_LOCK:
            self._try(self.doc.set_layer, name, locked=not l.locked)
        elif col == C_MODEL:
            self._try(self.doc.set_layer, name, in_model=not l.in_model)
        elif col == C_COLOR:
            c = QColorDialog.getColor(QColor(l.color), self, f"Colour for {name}")
            if c.isValid():
                self._try(self.doc.set_layer, name, color=c.name())
        else:
            if self.doc.active_layer != name:
                self.doc.active_layer = name
                self.doc._emit()

    def _double(self, it, col):
        if col != C_NAME:
            return
        name = it.data(C_NAME, Qt.UserRole)
        new, ok = QInputDialog.getText(self, "Rename layer", "Name:", text=name)
        if ok and new and new != name:
            if name == DEFAULT:
                QMessageBox.information(self, "Layers", "The Default layer keeps its name.")
                return
            self._try(self.doc.set_layer, name, new_name=new)

    def add(self):
        n = 1
        while self.doc.has_layer(f"Layer {n}"):
            n += 1
        name, ok = QInputDialog.getText(self, "New layer", "Name:", text=f"Layer {n}")
        if ok and name:
            self._try(self.doc.add_layer, name, activate=True)

    def delete(self):
        name = self.selected()
        if not name:
            return
        count = self.doc.layer_counts().get(name, 0)
        if count and QMessageBox.question(self, "Delete layer",
                                          f"Move {count} feature(s) on '{name}' to Default and delete the layer?"
                                          ) != QMessageBox.Yes:
            return
        self._try(self.doc.delete_layer, name)

    def move_here(self):
        name = self.selected()
        if name:
            self.move_requested.emit(name)
