"""The Design sidebar as three "islands": each a tabbed panel group, stacked (or side by side) in one splitter.

Any panel can live in any island (or be hidden); presets set the common layouts, the tab bar's right-click
menu moves a single panel, tabs drag to reorder, and every split can be dragged to any size — down to
collapsing an island completely.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QLabel, QMenu, QScrollArea, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

N = 3
PRESETS = {
    "three": ("Three islands — tree · tools · properties",
              [["tree"], ["objects", "mates", "layers", "physics", "studies", "cam"], ["props"]]),
    "two": ("Two islands — model · properties and tools",
            [["tree", "objects", "mates", "layers"], ["props", "physics", "studies", "cam"], []]),
    "one": ("One island — everything in tabs",
            [["tree", "props", "objects", "mates", "layers", "physics", "studies", "cam"], [], []]),
    "focus": ("Tree and properties only",
              [["tree"], ["props"], []]),
}
DEFAULT = "three"


def scrolled(w):
    """Wrap a panel so the island can shrink below the panel's own minimum size (it scrolls instead)."""
    if isinstance(w, QScrollArea):
        w.setMinimumSize(0, 0)
        return w
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setWidget(w)
    sc.setMinimumSize(0, 0)
    return sc


class Sidebar(QWidget):
    """panels: [(key, label, widget)] in their natural order."""
    layout_changed = Signal()

    def __init__(self, panels, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.keys = [k for k, _l, _w in panels]
        self.labels = {k: l for k, l, _w in panels}
        self.inner = {k: w for k, _l, w in panels}                     # the panel itself
        self.pages = {k: scrolled(w) for k, _l, w in panels}            # what goes in a tab
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(True)
        self.split.setHandleWidth(6)
        self.split.splitterMoved.connect(lambda *_: self._save_sizes())
        lay.addWidget(self.split)
        self.tabs = []
        for i in range(N):
            t = QTabWidget()
            t.setDocumentMode(True)
            t.setMovable(True)
            t.setMinimumSize(0, 0)
            t.setStyleSheet("QTabBar::tab { padding: 6px 8px; margin-right: 0; }")
            t.tabBar().setExpanding(False)
            t.setUsesScrollButtons(True)                       # narrow island: arrows instead of clipped tabs
            t.setElideMode(Qt.ElideNone)
            t.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
            t.tabBar().customContextMenuRequested.connect(lambda pos, i=i: self._tab_menu(i, pos))
            t.tabBar().tabMoved.connect(lambda *_a, i=i: self._sync_order(i))
            self.tabs.append(t)
            self.split.addWidget(t)
        self.islands = self._load()
        self.side_by_side = bool(self._setting("ui/islands_horizontal", False, bool))
        self.apply()
        QTimer.singleShot(0, self._restore_sizes)

    # settings ------------------------------------------------------------------------------
    def _setting(self, key, default, typ=None):
        if self.settings is None:
            return default
        return self.settings.value(key, default, type=typ) if typ else self.settings.value(key, default)

    def _load(self):
        raw = self._setting("ui/islands", "")
        try:
            got = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            got = None
        if not (isinstance(got, list) and len(got) == N and all(isinstance(g, list) for g in got)):
            got = PRESETS[DEFAULT][1]
        seen, out = set(), []
        for g in got:
            out.append([k for k in g if k in self.labels and not (k in seen or seen.add(k))])
        return out

    def _save(self):
        if self.settings is not None:
            self.settings.setValue("ui/islands", json.dumps(self.islands))
            self.settings.setValue("ui/islands_horizontal", self.side_by_side)

    def _save_sizes(self):
        if self.settings is not None:
            self.settings.setValue("ui/island_sizes", json.dumps(self.split.sizes()))

    def _restore_sizes(self):
        raw = self._setting("ui/island_sizes", "")
        try:
            sizes = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            sizes = None
        if isinstance(sizes, list) and len(sizes) == N and sum(sizes) > 0:
            self.split.setSizes([int(s) for s in sizes])
        else:
            self.reset_sizes()

    # layout ----------------------------------------------------------------------------------
    def hidden(self):
        placed = {k for g in self.islands for k in g}
        return [k for k in self.keys if k not in placed]

    def apply(self):
        for t in self.tabs:
            t.blockSignals(True)
            t.tabBar().blockSignals(True)
            while t.count():
                t.removeTab(0)
        for k in self.hidden():
            self.pages[k].setParent(None)
        for i, g in enumerate(self.islands):
            t = self.tabs[i]
            for k in g:
                t.addTab(self.pages[k], self.labels[k])
            t.setVisible(bool(g))
            t.tabBar().blockSignals(False)
            t.blockSignals(False)
        self.split.setOrientation(Qt.Horizontal if self.side_by_side else Qt.Vertical)
        self._save()
        self.layout_changed.emit()

    def set_preset(self, key):
        if key in PRESETS:
            self.islands = [list(g) for g in PRESETS[key][1]]
            self.apply()
            self.reset_sizes()

    def current_preset(self):
        for k, (_l, g) in PRESETS.items():
            if g == self.islands:
                return k
        return None

    def set_side_by_side(self, on):
        self.side_by_side = bool(on)
        self.apply()
        self.reset_sizes()

    def move(self, key, island):
        """island 0..2, or None = hide."""
        for g in self.islands:
            if key in g:
                g.remove(key)
        if island is not None:
            self.islands[island].append(key)
        self.apply()
        if island is not None:
            self.show_panel(key)

    def reset_sizes(self):
        used = [bool(g) for g in self.islands]
        total = (self.split.width() if self.side_by_side else self.split.height()) or 900
        weights = {0: 0.48, 1: 0.24, 2: 0.28}
        w = [weights[i] if used[i] else 0 for i in range(N)]
        s = sum(w) or 1
        self.split.setSizes([int(total * x / s) for x in w])
        self._save_sizes()

    def _sync_order(self, i):
        t = self.tabs[i]
        order = []
        for j in range(t.count()):
            page = t.widget(j)
            order += [k for k, p in self.pages.items() if p is page]
        self.islands[i] = order
        self._save()

    # navigation --------------------------------------------------------------------------------
    def island_of(self, key):
        return next((i for i, g in enumerate(self.islands) if key in g), None)

    def show_panel(self, key_or_widget):
        """Bring a panel to the front of its island (un-hiding / un-collapsing as needed)."""
        key = key_or_widget if isinstance(key_or_widget, str) else next(
            (k for k, w in self.inner.items() if w is key_or_widget or self.pages[k] is key_or_widget), None)
        if key is None:
            return False
        i = self.island_of(key)
        if i is None:
            self.islands[0 if not self.islands[0] else min(range(N), key=lambda j: len(self.islands[j]))].append(key)
            self.apply()
            i = self.island_of(key)
        self.tabs[i].setCurrentWidget(self.pages[key])
        sizes = self.split.sizes()
        if sizes[i] < 40:                                        # collapsed island: give it room again
            give = max(160, sum(sizes) // 4)
            big = max(range(N), key=lambda j: sizes[j])
            sizes[big] = max(0, sizes[big] - give)
            sizes[i] = give
            self.split.setSizes(sizes)
        return True

    # menus -------------------------------------------------------------------------------------
    def _tab_menu(self, i, pos):
        t = self.tabs[i]
        j = t.tabBar().tabAt(pos)
        m = QMenu(self)
        if j >= 0:
            key = next(k for k, p in self.pages.items() if p is t.widget(j))
            mv = m.addMenu(f"Move “{self.labels[key]}” to")
            for n in range(N):
                a = mv.addAction(f"Island {n + 1}")
                a.setEnabled(n != i)
                a.triggered.connect(lambda _=False, k=key, n=n: self.move(k, n))
            m.addAction(f"Hide “{self.labels[key]}”", lambda k=key: self.move(k, None))
            m.addSeparator()
        self.fill_menu(m)
        m.exec(t.tabBar().mapToGlobal(pos))

    def fill_menu(self, m):
        """Sidebar layout entries (shared by View ▸ Sidebar and the tabs' right-click menu)."""
        cur = self.current_preset()
        for k, (label, _g) in PRESETS.items():
            a = m.addAction(label, lambda k=k: self.set_preset(k))
            a.setCheckable(True)
            a.setChecked(k == cur)
        m.addSeparator()
        sb = m.addAction("Islands side by side", lambda: self.set_side_by_side(not self.side_by_side))
        sb.setCheckable(True)
        sb.setChecked(self.side_by_side)
        hid = self.hidden()
        if hid:
            sh = m.addMenu("Show hidden panel")
            for k in hid:
                sh.addAction(self.labels[k], lambda k=k: self.show_panel(k))
        m.addAction("Customise islands…", self.customise)
        m.addAction("Reset island sizes", self.reset_sizes)

    def customise(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Sidebar islands")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Pick the island each panel lives in. Panels in one island show as tabs; drag the "
                           "lines between islands to size them (all the way to closed)."))
        g = QGridLayout()
        boxes = {}
        for r, k in enumerate(self.keys):
            g.addWidget(QLabel(self.labels[k]), r, 0)
            cb = QComboBox()
            for n in range(N):
                cb.addItem(f"Island {n + 1}", n)
            cb.addItem("Hidden", None)
            i = self.island_of(k)
            cb.setCurrentIndex(N if i is None else i)
            g.addWidget(cb, r, 1)
            boxes[k] = cb
        v.addLayout(g)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec():
            new = [[] for _ in range(N)]
            for g_ in self.islands:                              # keep the current tab order where possible
                for k in g_:
                    n = boxes[k].currentData()
                    if n is not None:
                        new[n].append(k)
            for k in self.keys:
                n = boxes[k].currentData()
                if n is not None and k not in new[n]:
                    new[n].append(k)
            self.islands = new
            self.apply()
            self.reset_sizes()
        return dlg
