"""Import hub dialog: one place to bring parts in from projects, the standard library, the fastener library,
inventory or any file — linked to the source and, optionally, mated to the picked face."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core import importer
from ..core.features import FeatureError
from . import icons, theme

SRC_ICON = {"all": "squares-four", "project": "folder-open", "std": "books", "inventory": "package",
            "fastener": "nut", "file": "file-arrow-down"}
ORDER = ["all", "project", "std", "inventory", "fastener"]


class ImportDialog(QDialog):
    imported = Signal(dict)

    def __init__(self, doc, pick=None, parent=None, settings=None):
        super().__init__(parent)
        self.doc, self.pick = doc, pick
        self.settings = settings or QSettings("Daedalus", "Daedalus")
        self.setWindowTitle("Import part")
        self.resize(980, 600)
        self._items: list[dict] = []
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search projects, standard parts, inventory, fasteners…")
        self.q.setClearButtonEnabled(True)
        ic = icons.icon("search", theme.MUTED)
        if not ic.isNull():
            self.q.addAction(ic, QLineEdit.LeadingPosition)
        self._timer = QTimer(self, singleShot=True, interval=180)
        self._timer.timeout.connect(self.refresh)
        self.q.textChanged.connect(lambda _t: self._timer.start())
        top.addWidget(self.q, 1)
        b = QPushButton("Browse file…")
        b.setIcon(icons.icon("file"))
        b.setToolTip("Any part file: Daedalus, STEP, IGES, BREP, STL")
        b.clicked.connect(self._browse)
        top.addWidget(b)
        b = QPushButton("Project folders…")
        b.setIcon(icons.icon("folder"))
        b.setToolTip("Folders scanned for Daedalus projects (and one level of sub-folders)")
        b.clicked.connect(self._folders)
        top.addWidget(b)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.src = QListWidget()
        self.src.setIconSize(QSize(16, 16))
        self.src.setMaximumWidth(210)
        for key in ORDER:
            it = QListWidgetItem(icons.icon(SRC_ICON[key]), "All sources" if key == "all" else importer.SOURCES[key])
            it.setData(Qt.UserRole, key)
            self.src.addItem(it)
        self.src.setCurrentRow(0)
        self.src.currentRowChanged.connect(lambda _r: self.refresh())
        split.addWidget(self.src)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Name", "From", "Details"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.list.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.list.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.list.currentItemChanged.connect(lambda *_: self._show())
        self.list.itemDoubleClicked.connect(lambda *_: self._import())
        split.addWidget(self.list)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(10, 0, 0, 0)
        self.title = QLabel("Pick something to import")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 11pt; font-weight: 600;")
        sv.addWidget(self.title)
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        sv.addWidget(self.sub)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        sv.addWidget(line)
        form = QFormLayout()
        self.length = QComboBox()
        self.length.setEditable(True)
        self.length.setToolTip("Screw / bolt length")
        form.addRow("Length", self.length)
        self.nut = QCheckBox("Add nut")
        self.washer = QCheckBox("Add washer")
        row = QHBoxLayout()
        row.addWidget(self.nut)
        row.addWidget(self.washer)
        row.addStretch(1)
        self.fast_row = QWidget()
        self.fast_row.setLayout(row)
        form.addRow("", self.fast_row)
        self.fast_box = QWidget()
        form.setContentsMargins(0, 0, 0, 0)
        self.fast_box.setLayout(form)
        sv.addWidget(self.fast_box)
        self.link = QCheckBox("Keep linked to the source")
        self.link.setChecked(True)
        self.link.setToolTip("Linked parts update when the source changes (on open or Assembly ▸ Links)")
        sv.addWidget(self.link)
        self.connect_box = QCheckBox("Connect to the picked face")
        self.connect_box.setToolTip("Flat face: sits the part on it (coincident). Hole or shaft: concentric. "
                                    "Fasteners/bearings: placed in the hole with their mates.")
        sv.addWidget(self.connect_box)
        self.pick_lab = QLabel()
        self.pick_lab.setObjectName("muted")
        self.pick_lab.setWordWrap(True)
        sv.addWidget(self.pick_lab)
        sv.addStretch(1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.go = QPushButton("Import")
        self.go.setDefault(True)
        self.go.setIcon(icons.icon("Import", theme.ON_ACCENT))
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._import)
        btns.addWidget(self.go)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        sv.addLayout(btns)
        split.addWidget(side)
        split.setSizes([180, 520, 280])
        v.addWidget(split, 1)
        self._sync_pick()
        self.refresh()
        self.q.setFocus()

    # ------------------------------------------------------------------ data
    def _dirs(self):
        dirs = list(self.settings.value("import/dirs", [], type=list) or [])
        if getattr(self.doc, "path", None):
            dirs.append(str(Path(self.doc.path).parent))
        last = self.settings.value("last_dir")
        if last:
            dirs.append(str(last))
        out = []
        for d in dirs:
            if d and d not in out:
                out.append(d)
        return out

    def _recent(self):
        return list(self.settings.value("recent_files", [], type=list) or [])

    def refresh(self):
        key = self.src.currentItem().data(Qt.UserRole) if self.src.currentItem() else "all"
        sources = None if key == "all" else {key}
        QApplicationOverride.on()
        try:
            self._items = importer.search(self.q.text(), sources, dirs=self._dirs(), recent=self._recent(),
                                          exclude=str(self.doc.path) if getattr(self.doc, "path", None) else None)
        finally:
            QApplicationOverride.off()
        if key == "all" and not self.q.text().strip():          # don't flood "All" with every fastener size
            self._items = [i for i in self._items if i["source"] != "fastener"]
        self.list.clear()
        for i in self._items[:800]:
            it = QTreeWidgetItem([i["name"], importer.SOURCES[i["source"]], i.get("detail", "")])
            it.setIcon(0, icons.icon(SRC_ICON.get(i["source"], "file")))
            it.setData(0, Qt.UserRole, i)
            it.setForeground(1, QBrush(QColor(theme.MUTED)))
            it.setForeground(2, QBrush(QColor(theme.MUTED)))
            self.list.addTopLevelItem(it)
        counts = {}
        for i in self._items:
            counts[i["source"]] = counts.get(i["source"], 0) + 1
        if self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0))
        else:
            hint = "Nothing found."
            if key in ("all", "project") and not self._dirs():
                hint += " Add a project folder to see your other projects."
            if key == "all" and not self.q.text().strip():
                hint += " Type to search fasteners too."
            self.title.setText(hint)
            self.sub.setText("")
        self._show()

    def current(self) -> dict | None:
        it = self.list.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _sync_pick(self):
        p = self.pick
        if p and p.get("object"):
            self.pick_lab.setText(f"Picked: {p['object']} · face {p['index']} ({p.get('type', 'face')})")
            self.connect_box.setEnabled(True)
            self.connect_box.setChecked(True)
        else:
            self.pick_lab.setText("Click a face in the model first to connect the part to it.")
            self.connect_box.setEnabled(False)
            self.connect_box.setChecked(False)

    def _show(self):
        i = self.current()
        self.go.setEnabled(i is not None)
        if i is None:
            self.fast_box.setVisible(False)
            self.length.setEnabled(False)
            return
        self.title.setText(i["name"])
        bits = [importer.SOURCES[i["source"]], i.get("detail", "")]
        if i.get("path"):
            bits.append(i["path"])
        self.sub.setText("\n".join(b for b in bits if b))
        self.link.setEnabled(bool(i.get("linkable")))
        if not i.get("linkable"):
            self.link.setChecked(False)
        elif not self.link.isChecked():
            self.link.setChecked(True)
        fast = i["source"] == "fastener" or i.get("via") == "fastener"
        kind = i.get("kind", "")
        needs_len = fast and kind in ("socket head cap screw", "hex bolt", "flat head screw")
        self.fast_row.setVisible(fast and kind in ("socket head cap screw", "hex bolt"))
        self.length.setEnabled(needs_len)
        self.fast_box.setVisible(fast)
        self.length.setVisible(needs_len)
        lab = self.fast_box.layout().labelForField(self.length)
        if lab is not None:
            lab.setVisible(needs_len)
        self.length.clear()
        if needs_len:
            from ..core import standards as S
            inch = S.ALL[i["size"]].system != "ISO"
            vals = S.INCH_LENGTHS if inch else S.METRIC_LENGTHS
            for val in vals:
                self.length.addItem(f'{val:g}"' if inch else f"{val:g} mm", val * S.IN if inch else float(val))
            want = i.get("length")
            if want:
                k = min(range(len(vals)), key=lambda j: abs((vals[j] * (S.IN if inch else 1)) - want))
                self.length.setCurrentIndex(k)
            else:
                self.length.setCurrentIndex(min(5, self.length.count() - 1))

    def _length_mm(self):
        d = self.length.currentData()
        if d is not None and self.length.currentText() == self.length.itemText(self.length.currentIndex()):
            return float(d)
        txt = self.length.currentText().replace("mm", "").replace('"', "").strip()
        try:
            val = float(txt)
        except ValueError:
            return None
        i = self.current() or {}
        from ..core import standards as S
        return val * S.IN if i.get("size") in S.INCH else val

    # ------------------------------------------------------------------ actions
    def _browse(self):
        last = self.settings.value("last_dir", str(Path.home()))
        p, _ = QFileDialog.getOpenFileName(self, "Import part file", last,
                                           "Parts (" + " ".join("*" + e for e in importer.PART_EXTS) + ")")
        if not p:
            return
        try:
            item = importer.file_item(p)
        except FeatureError as e:
            QMessageBox.warning(self, "Import", str(e))
            return
        self.settings.setValue("last_dir", str(Path(p).parent))
        self._do(item)

    def _folders(self):
        dirs = list(self.settings.value("import/dirs", [], type=list) or [])
        d = QFileDialog.getExistingDirectory(self, "Add a project folder", dirs[-1] if dirs else str(Path.home()))
        if d and d not in dirs:
            dirs.append(d)
            self.settings.setValue("import/dirs", dirs)
            self.src.setCurrentRow(ORDER.index("project"))
            self.refresh()

    def _import(self):
        i = self.current()
        if i is not None:
            self._do(i)

    def _do(self, item):
        length = None
        if item["source"] == "fastener" or item.get("via") == "fastener":
            if self.length.isEnabled():
                length = self._length_mm()
        target = self.pick if self.connect_box.isChecked() else None
        QApplicationOverride.on()
        try:
            r = importer.import_item(self.doc, item, link=self.link.isChecked(), target=target, length=length,
                                     nut=self.nut.isChecked() and self.fast_row.isVisible(),
                                     washer=self.washer.isChecked() and self.fast_row.isVisible())
        except FeatureError as e:
            QApplicationOverride.off()
            QMessageBox.warning(self, "Import", str(e))
            return
        finally:
            QApplicationOverride.off()
        self.doc._emit()
        r["item"] = item
        self.imported.emit(r)
        self.accept()


class QApplicationOverride:
    """Busy cursor around slow scans/imports."""
    depth = 0

    @classmethod
    def on(cls):
        from PySide6.QtWidgets import QApplication
        if cls.depth == 0:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        cls.depth += 1

    @classmethod
    def off(cls):
        from PySide6.QtWidgets import QApplication
        if cls.depth > 0:
            cls.depth -= 1
            if cls.depth == 0:
                QApplication.restoreOverrideCursor()
