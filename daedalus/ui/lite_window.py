"""Light window for the modules that don't need the 3D model: Drawings and/or Inventory. No CAD kernel, no 3D view,
no simulation libraries are loaded — it opens in about a second. File ▸ Open 3D workspace loads the rest later."""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QTabWidget

from .. import APP_NAME, __version__
from .. import modules as MOD


class LiteWindow(QMainWindow):
    def __init__(self, mods, open_full=None):
        super().__init__()
        self.mods = frozenset(mods)
        self.settings = QSettings("Daedalus", "Daedalus")
        self._open_full = open_full
        self.full = None
        self.setWindowTitle("Holocron Inventory" if self.mods == {"inventory"} else
                            f"{APP_NAME} · {MOD.describe(self.mods)}")
        self.resize(1300, 850)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setVisible(len(self.mods & {"drawing", "inventory"}) > 1)
        self.drawing_page = self.inventory_page = self.doc = None
        if "drawing" in self.mods:
            from ..drawing.doc2d import Doc2D
            from .drawing_ui import DrawingPage
            self.doc = Doc2D()
            self.drawing_page = DrawingPage(lambda: self.doc, lite=True)
            self.drawing_page.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
            self.tabs.addTab(self.drawing_page, "Drawings")
        if "inventory" in self.mods:
            from .inventory_ui import InventoryPage
            self.inventory_page = InventoryPage(lite=True)
            self.inventory_page.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
            self.inventory_page.send_bom_requested.connect(lambda: self._needs_3d("Send BOM"))
            self.inventory_page.insert_std_requested.connect(lambda _i: self._needs_3d("Insert in model"))
            self.tabs.addTab(self.inventory_page, "Inventory")
        self.setCentralWidget(self.tabs)
        self._menus()
        lab = QLabel(f"{MOD.describe(self.mods)} · v{__version__}")
        lab.setObjectName("muted")
        self.statusBar().addPermanentWidget(lab)

    def _menus(self):
        mb = self.menuBar()

        def act(menu, text, fn, key=None):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if key:
                a.setShortcut(QKeySequence(key))
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        if self.drawing_page is not None:
            act(f, "Open 2D file (DXF / DWG / SVG / HPGL)…", self.drawing_page.open_2d, QKeySequence.Open)
            act(f, "Drawings from a .daedalus file…", self.drawing_page.load_drawings)
            f.addSeparator()
        act(f, "Open 3D workspace", self.open_full, "Ctrl+Shift+3")
        f.addSeparator()
        act(f, "Quit", self.close, QKeySequence.Quit)
        if self.doc is not None:
            e = mb.addMenu("&Edit")
            act(e, "Undo", lambda: self.doc.undo() and self.drawing_page.refresh(), QKeySequence.Undo)
            act(e, "Redo", lambda: self.doc.redo() and self.drawing_page.refresh(), QKeySequence.Redo)
        h = mb.addMenu("&Help")
        act(h, "Startup modules…", self.startup_modules)

    def _needs_3d(self, what):
        if QMessageBox.question(self, what, f"{what} needs the 3D workspace. Open it now?") == QMessageBox.Yes:
            self.open_full()

    def open_full(self):
        """Load the CAD kernel and open the full workspace beside this window."""
        if self.full is not None:
            self.full.show()
            self.full.raise_()
            return
        self.statusBar().showMessage("Loading the 3D workspace…")
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.full = (self._open_full or _default_full)()
        finally:
            QApplication.restoreOverrideCursor()
        self.statusBar().clearMessage()

    def startup_modules(self):
        from .modules_ui import StartupModulesDialog
        StartupModulesDialog(self.settings, self).exec()

    def closeEvent(self, e):
        if self.doc is not None and self.doc.dirty and self.doc.drawings:
            r = QMessageBox.question(self, APP_NAME, "Close without exporting the drawings you changed?",
                                     QMessageBox.Yes | QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                e.ignore()
                return
        super().closeEvent(e)


def _default_full():
    from .main_window import MainWindow
    w = MainWindow()
    w.show()
    w.view.start()
    return w
