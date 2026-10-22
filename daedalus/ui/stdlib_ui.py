"""Standard parts library: browse / search / insert, and save objects or files as standard parts."""
from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

from ..stdparts import KINDS, LibraryError, StandardLibrary, get_library
from . import theme

SETTINGS = ("Daedalus", "Daedalus")
FILE_FILTER = ("Parts (*.daedalus *.strata *.step *.stp *.iges *.igs *.brep *.stl *.obj *.3mf *.sldprt *.sldasm);;"
               "STEP (*.step *.stp);;IGES (*.iges *.igs);;Meshes (*.stl *.obj *.3mf);;All files (*)")


def _placeholder(size=120) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(QColor(theme.PANEL_2))
    p = QPainter(pm)
    p.setPen(QColor(theme.MUTED))
    p.drawText(pm.rect(), Qt.AlignCenter, "no preview")
    p.end()
    return pm


def _thumb(info, size=120) -> QPixmap:
    if info.get("thumb"):
        pm = QPixmap(info["thumb"])
        if not pm.isNull():
            return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return _placeholder(size)


def swap_for_library_part(lib, doc, pid, obj, parent=None) -> str | None:
    """Insert library part `pid` linked, at `obj`'s position, then offer to remove `obj`. Returns the new object."""
    pose = list(doc.obj(obj).pose) if obj and doc is not None and doc.has_obj(obj) else None
    try:
        name = lib.insert(doc, pid, link=True)
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(parent, "Standard part", str(e))
        return None
    if pose is not None:
        doc.obj(name).pose = pose
        mates = [m.name for m in doc.mates if obj in m.objects()]
        msg = f"Inserted {name} where {obj} is.\n\nRemove {obj} now?"
        if mates:
            msg += f" Its {len(mates)} mate(s) go with it — re-mate {name} afterwards."
        if QMessageBox.question(parent, "Use existing standard part", msg) == QMessageBox.Yes:
            doc.delete_object(obj, checkpoint=False)
        doc.rebuild()
    return name


class InfoForm(QWidget):
    """Shared fields for saving / editing a standard part."""

    def __init__(self, lib: StandardLibrary, info: dict | None = None, parent=None):
        super().__init__(parent)
        info = info or {}
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.name = QLineEdit(info.get("name", ""))
        self.pn = QLineEdit(info.get("part_number", ""))
        self.pn.setPlaceholderText("e.g. RG-1001 — matches the inventory and BOM")
        self.cat = QComboBox()
        self.cat.setEditable(True)
        self.cat.addItems(lib.categories())
        self.cat.setCurrentText(info.get("category", ""))
        self.cat.lineEdit().setPlaceholderText("e.g. Brackets, Casters, Motors")
        self.kind = QComboBox()
        self.kind.addItems(KINDS)
        self.kind.setCurrentText(info.get("kind", "purchased"))
        from ..core import materials as matlib
        self.mat = QComboBox()
        self.mat.setEditable(True)
        self.mat.addItem("")
        self.mat.addItems([m.name for m in matlib.LIBRARY])
        self.mat.setCurrentText(info.get("material", ""))
        self.mat.setToolTip("Sets the density for mass. Free text that isn't in the material library is kept as "
                            "information only.")
        self.desc = QLineEdit(info.get("description", ""))
        self.desc.setPlaceholderText("BOM description")
        self.vendor = QLineEdit(info.get("vendor", ""))
        self.vpn = QLineEdit(info.get("vendor_part_number", ""))
        self.cost = QDoubleSpinBox()
        self.cost.setRange(0, 1e7)
        self.cost.setDecimals(2)
        self.cost.setPrefix("$ ")
        self.cost.setSpecialValueText("— (from material for manufactured)")
        self.cost.setValue(float(info.get("unit_cost") or 0))
        self.url = QLineEdit(info.get("url", ""))
        self.url.setPlaceholderText("Datasheet / product page")
        tags = info.get("tags") or []
        self.tags = QLineEdit(", ".join(tags) if isinstance(tags, list) else str(tags))
        self.tags.setPlaceholderText("comma separated")
        self.notes = QPlainTextEdit(info.get("notes", ""))
        self.notes.setMaximumHeight(70)
        for lab, w in (("Name", self.name), ("Part number", self.pn), ("Category", self.cat), ("Kind", self.kind),
                       ("Material", self.mat), ("Description", self.desc), ("Vendor", self.vendor), ("Vendor part #", self.vpn),
                       ("Unit cost", self.cost), ("Link", self.url), ("Tags", self.tags), ("Notes", self.notes)):
            f.addRow(lab, w)

    def values(self) -> dict:
        return {"name": self.name.text().strip(), "part_number": self.pn.text().strip(),
                "category": self.cat.currentText().strip(), "kind": self.kind.currentText(),
                "material": self.mat.currentText().strip(),
                "description": self.desc.text().strip(), "vendor": self.vendor.text().strip(),
                "vendor_part_number": self.vpn.text().strip(),
                "unit_cost": self.cost.value() if self.cost.value() > 0 else None, "url": self.url.text().strip(),
                "tags": self.tags.text(), "notes": self.notes.toPlainText().strip()}


class SaveStandardDialog(QDialog):
    """Save an object of the open document (or a file) as a standard part, checking the library for
    similar parts first (geometry fingerprint + part number / vendor part number / name)."""
    use_existing = Signal(str, str)          # library part id, object it should stand in for ('' for files)

    def __init__(self, lib: StandardLibrary, doc=None, obj=None, file=None, parent=None):
        super().__init__(parent)
        self.lib, self.doc, self.file = lib, doc, file
        self.setWindowTitle("Save as standard part")
        self.setMinimumWidth(980)
        outer = QHBoxLayout(self)
        left = QWidget()
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(left, 3)
        top = QFormLayout()
        self.obj = None
        if doc is not None:
            self.obj = QComboBox()
            for o in doc.objects:
                if o.name in doc.result.bodies:
                    self.obj.addItem(o.name)
            if obj:
                self.obj.setCurrentText(obj)
            top.addRow("Object", self.obj)
        else:
            top.addRow("File", QLabel(Path(file).name))
        lay.addLayout(top)
        start = {"name": Path(file).stem if file else (obj or ""), "kind": "manufactured" if doc else "purchased"}
        self.found = {}
        if file:
            self.found = lib.file_info(file)
            start.update({k: v for k, v in self.found.items() if not k.startswith("_")})
        elif doc is not None and obj:
            start["material"] = doc.material_of(obj).name
        if doc is not None and obj:
            o = doc.obj(obj)
            if o.part_no:
                start.update(part_number=o.part_no, description=o.description or "",
                             unit_cost=o.unit_cost, kind=(o.std or {}).get("kind", "purchased"))
        self.form = InfoForm(lib, start)
        if self.found:
            from ..stdparts.metadata import write_props_note
            bits = [write_props_note(self.found)]
            if self.found.get("_material_text"):
                m = self.found.get("material")
                bits.append(f"material “{html.escape(self.found['_material_text'])}”" +
                            (f" → {html.escape(m)}" if m else " (not in the material library — pick one for mass)"))
            if self.found.get("_note"):
                bits.append(html.escape(self.found["_note"]))
            if self.found.get("_attributes"):
                extra = ", ".join(f"{html.escape(k)} = {html.escape(str(v))}"
                                  for k, v in list(self.found["_attributes"].items())[:8])
                bits.append(f"<span style='color:{theme.MUTED}'>{extra}</span>")
            txt = "<br>".join(b for b in bits if b) or "Nothing extra found in the file — fill in what you know."
            note = QLabel(txt)
            note.setWordWrap(True)
            note.setTextFormat(Qt.RichText)
            note.setStyleSheet(f"color:{theme.OK}" if self.found.get("_found") else f"color:{theme.MUTED}")
            lay.addWidget(note)
        lay.addWidget(self.form)
        self.replace_lab = QLabel()
        self.replace_lab.setWordWrap(True)
        self.replace_lab.setTextFormat(Qt.RichText)
        self.replace_lab.setStyleSheet(f"color:{theme.ACCENT_LINE}")
        self.replace_lab.linkActivated.connect(lambda *_: self._clear_replace())
        lay.addWidget(self.replace_lab)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        note = QLabel(f"Library: {lib.root}")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)

        # similar parts panel
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 0, 0, 0)
        outer.addWidget(right, 2)
        h = QLabel("Similar in library")
        h.setObjectName("h1")
        rv.addWidget(h)
        self.sim_info = QLabel()
        self.sim_info.setWordWrap(True)
        self.sim_info.setObjectName("muted")
        rv.addWidget(self.sim_info)
        self.sim = QListWidget()
        self.sim.setIconSize(QSize(64, 64))
        self.sim.setWordWrap(True)
        self.sim.currentItemChanged.connect(lambda *_: self._sel_sim())
        rv.addWidget(self.sim, 1)
        self.sim_why = QLabel()
        self.sim_why.setWordWrap(True)
        self.sim_why.setTextFormat(Qt.RichText)
        rv.addWidget(self.sim_why)
        row = QHBoxLayout()
        self.b_replace = QPushButton("Replace it (new version)")
        self.b_replace.setToolTip("Save over the selected library part; linked projects pick up the change")
        self.b_replace.clicked.connect(self._choose_replace)
        self.b_use = QPushButton("Use existing instead")
        self.b_use.setToolTip("Don't save a duplicate" + (": insert the library part in place of this object"
                                                           if doc is not None else ""))
        self.b_use.clicked.connect(self._use_existing)
        row.addWidget(self.b_replace)
        row.addWidget(self.b_use)
        rv.addLayout(row)

        self.replace_id = None          # chosen in the panel, or same part number
        self.chosen = None
        self.matches: list = []
        self.result_info = None
        self._dupe_ok = False
        self._fp_cache: dict = {}
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.refresh_similar)
        for w in (self.form.name, self.form.pn, self.form.vpn, self.form.vendor):
            w.textChanged.connect(lambda *_: (self._check(), self._timer.start()))
        if self.obj is not None:
            self.obj.currentTextChanged.connect(lambda *_: self.refresh_similar())
        self._check()
        self.refresh_similar()

    # --------------------------------------------------------------- checks
    def _impact(self, pid) -> str:
        """'Used in 3 projects — 1 in production (Lift cart LC-200, In Fab) · 2 saved files'."""
        from ..stdparts.usage import files_using, std_status
        try:
            info = self.lib.get(pid)
        except LibraryError:
            return ""
        bits = []
        store = getattr(self, "_store", None)
        if store is None:
            from .production_ui import app_inventory
            store = self._store = app_inventory()
        if store is not None:
            st = std_status(store.inv, info)
            n = len({p["project_id"] for p in st["projects"]})
            if n:
                txt = f"used in {n} project{'s' if n > 1 else ''}"
                prod = {p["project"]: ", ".join(o["status"] for o in p["production"] if o["active"])
                        for p in st["in_production"]}
                if prod:
                    txt += f" — <b>{len(prod)} in production</b> (" + "; ".join(
                        f"{html.escape(k)}: {html.escape(v)}" for k, v in prod.items()) + ")"
                bits.append(txt)
        f = files_using(self.lib, pid)
        if f:
            bits.append(f"{len(f)} saved file{'s' if len(f) > 1 else ''}"
                        f" ({sum(1 for x in f if x['linked'])} linked)")
        return " · ".join(bits)

    def _check(self, *_):
        hit = self.lib.by_part_number(self.form.pn.text())
        if self.chosen:
            info = next((m for m in self.matches if m["id"] == self.chosen), None) or {"name": self.chosen}
            self.replace_id = self.chosen
            imp = self._impact(self.chosen)
            self.replace_lab.setText(f"Saving replaces <b>{html.escape(info['name'])}</b> with a new version — "
                                     "linked projects pick it up." + (f"<br>It is {imp}." if imp else "") +
                                     " <a href='x' style='color:%s'>Save as new instead</a>" % theme.MUTED)
            return
        self.replace_id = hit["id"] if hit else None
        imp = self._impact(hit["id"]) if hit else ""
        self.replace_lab.setText(f"Part number already in the library ({html.escape(hit['name'])}, "
                                 f"v{hit.get('version', 1)}) — saving replaces it with a new version; linked "
                                 "projects pick it up." + (f"<br>It is {imp}." if imp else "") if hit else "")

    def _clear_replace(self):
        self.chosen = None
        self._check()

    def _bodies(self):
        if self.doc is not None:
            o = self.obj.currentText()
            b = self.doc.result.bodies.get(o)
            return [b] if b is not None else None
        return "file"

    def refresh_similar(self):
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        info = self.form.values()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            b = self._bodies()
            if b == "file":
                self.matches = self.lib.similar_to_file(self.file, info, threshold=0.8)
            else:
                self.matches = self.lib.similar(b, info, threshold=0.8)
        except Exception as e:  # noqa: BLE001
            self.matches = []
            self.sim_info.setText(f"Couldn't compare: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        self.sim.clear()
        col = {"identical": theme.ERROR, "very similar": theme.ACCENT_LINE, "similar": theme.MUTED}
        for m in self.matches:
            mt = m["match"]
            it = QListWidgetItem(QIcon(_thumb(m, 64)), f"{m['name']}  ·  {m.get('part_number') or '—'}\n"
                                                     f"{mt['level']} · {mt['score'] * 100:.0f} %")
            it.setData(Qt.UserRole, m)
            it.setForeground(QColor(col.get(mt["level"], theme.TEXT)))
            self.sim.addItem(it)
        if self.matches:
            top = self.matches[0]["match"]["level"]
            self.sim_info.setText("Possible duplicate — check before saving." if top == "identical" else
                                  "Parts that look alike. Reuse one if it fits.")
            self.sim.setCurrentRow(0)
        else:
            self.sim_info.setText("No similar parts in the library.")
            self.sim_why.setText("")
        on = bool(self.matches)
        self.b_replace.setEnabled(on)
        self.b_use.setEnabled(on)

    def _sel_sim(self):
        it = self.sim.currentItem()
        if not it:
            return
        m = it.data(Qt.UserRole)
        mt = m["match"]
        e = html.escape
        size = " × ".join(f"{v:g}" for v in m.get("size_mm", [])) + " mm" if m.get("size_mm") else ""
        self.sim_why.setText(f"<b>{e(m['name'])}</b> v{m.get('version', 1)} · {e(m.get('category') or '')}<br>"
                             f"{e(size)} · {e(m.get('material') or '')}<br>"
                             f"<span style='color:{theme.MUTED}'>{e(', '.join(mt['reasons']) or 'similar')}</span><br>"
                             f"<span style='color:{theme.MUTED}'>geometry {mt['geometry'] * 100:.0f} % · "
                             f"info {mt['text'] * 100:.0f} %</span>")

    def _current_match(self):
        it = self.sim.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _choose_replace(self):
        m = self._current_match()
        if m:
            self.chosen = m["id"]
            self._check()

    def _use_existing(self):
        m = self._current_match()
        if m:
            self.use_existing.emit(m["id"], self.obj.currentText() if self.obj is not None else "")
            self.reject()

    # ----------------------------------------------------------------- save
    def _save(self):
        v = self.form.values()
        if not v["name"]:
            QMessageBox.warning(self, "Standard part", "Give the standard part a name")
            return
        top = self.matches[0] if self.matches else None
        if top and top["match"]["level"] == "identical" and self.replace_id is None and not self._dupe_ok:
            mb = QMessageBox(self)
            mb.setIcon(QMessageBox.Warning)
            mb.setWindowTitle("Possible duplicate")
            mb.setText(f"<b>{html.escape(top['name'])}</b> ({html.escape(top.get('part_number') or 'no part number')})"
                       f" is already in the library — {html.escape(', '.join(top['match']['reasons']))}.")
            imp = self._impact(top["id"])
            mb.setInformativeText("Reuse it, replace it with this one, or save a separate part?" +
                                  (f"\n\nThat part is {re.sub('<[^>]+>', '', imp)}." if imp else ""))
            b_use = mb.addButton("Use existing", QMessageBox.AcceptRole)
            b_rep = mb.addButton("Replace it", QMessageBox.ActionRole)
            b_new = mb.addButton("Save as new", QMessageBox.DestructiveRole)
            mb.addButton(QMessageBox.Cancel)
            mb.exec()
            c = mb.clickedButton()
            if c is b_use:
                self.use_existing.emit(top["id"], self.obj.currentText() if self.obj is not None else "")
                self.reject()
                return
            if c is b_rep:
                self.chosen = top["id"]
                self._check()
            elif c is b_new:
                self._dupe_ok = True
            else:
                return
        if self.replace_id:
            imp = self._impact(self.replace_id)
            if "in production" in imp and QMessageBox.question(
                    self, "Replace a part in production",
                    f"{self.lib.get(self.replace_id)['name']} is {re.sub('<[^>]+>', '', imp)}.\n\nLinked "
                    "projects will update to the new version. Replace it?") != QMessageBox.Yes:
                return
        try:
            if self.doc is not None:
                self.result_info = self.lib.add_object(self.doc, self.obj.currentText(), v, replace=self.replace_id)
            else:
                self.result_info = self.lib.add_file(self.file, v, replace=self.replace_id)
        except (LibraryError, OSError) as e:
            QMessageBox.warning(self, "Standard part", str(e))
            return
        self.accept()


class EditInfoDialog(QDialog):
    def __init__(self, lib, info, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {info['name']}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        self.form = InfoForm(lib, info)
        lay.addWidget(self.form)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class StandardLibraryDialog(QDialog):
    """Browse the library; insert into the open document."""
    inserted = Signal(str)
    open_requested = Signal(str)
    open_inventory = Signal(str)          # inventory part id, or "project:<id>"

    def __init__(self, doc, parent=None, at=None, lib: StandardLibrary | None = None, store=None):
        super().__init__(parent)
        self.doc, self.at = doc, at
        self.lib = lib or get_library()
        if store is None:
            from .production_ui import app_inventory
            store = app_inventory()
        self.store = store
        self.setWindowTitle("Standard parts library")
        self.resize(1180, 720)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search name, part number, vendor, tags…")
        self.q.textChanged.connect(self.fill)
        self.cat = QComboBox()
        self.kind = QComboBox()
        self.kind.addItems(["All", *KINDS])
        self.cat.currentTextChanged.connect(self.fill)
        self.kind.currentTextChanged.connect(self.fill)
        top.addWidget(self.q, 1)
        top.addWidget(QLabel("Category"))
        top.addWidget(self.cat)
        top.addWidget(QLabel("Kind"))
        top.addWidget(self.kind)
        self.only_prod = QCheckBox("In production")
        self.only_prod.setToolTip("Only parts used by projects with an active production order")
        self.only_used = QCheckBox("Used in projects")
        self.only_low = QCheckBox("Low stock")
        for c in (self.only_prod, self.only_used, self.only_low):
            c.toggled.connect(self.fill)
            top.addWidget(c)
            c.setEnabled(self.store is not None)
        lay.addLayout(top)
        sp = QSplitter()
        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setIconSize(QSize(120, 120))
        self.list.setGridSize(QSize(170, 196))
        self.list.setResizeMode(QListView.Adjust)
        self.list.setMovement(QListView.Static)
        self.list.setWordWrap(True)
        self.list.setSpacing(4)
        self.list.currentItemChanged.connect(lambda *_: self.show_info())
        self.list.itemDoubleClicked.connect(lambda _it: self.insert(True))
        sp.addWidget(self.list)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(8, 0, 0, 0)
        self.big = QLabel()
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setMinimumSize(200, 200)
        sv.addWidget(self.big)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setOpenExternalLinks(True)
        self.info.setAlignment(Qt.AlignTop)
        from PySide6.QtWidgets import QScrollArea
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.info)
        self.tabs.addTab(sc, "Details")
        self.used = QTreeWidget()
        self.used.setHeaderLabels(["Where", "Qty/unit", "Units", "Needed", "Status"])
        self.used.setRootIsDecorated(True)
        self.used.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            self.used.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.used.itemDoubleClicked.connect(self._used_open)
        self.tabs.addTab(self.used, "Used in")
        invw = QWidget()
        iv = QVBoxLayout(invw)
        iv.setContentsMargins(4, 6, 4, 4)
        self.inv_lab = QLabel()
        self.inv_lab.setWordWrap(True)
        self.inv_lab.setTextFormat(Qt.RichText)
        self.inv_lab.setAlignment(Qt.AlignTop)
        iv.addWidget(self.inv_lab, 1)
        ir = QHBoxLayout()
        self.b_inv_add = QPushButton("Add to inventory")
        self.b_inv_add.setToolTip("Create the inventory part (0 in stock), linked to this standard part")
        self.b_inv_add.clicked.connect(self.inv_add)
        self.b_inv_link = QPushButton("Link existing…")
        self.b_inv_link.setToolTip("Link an inventory part you already have")
        self.b_inv_link.clicked.connect(self.inv_link)
        self.b_inv_sync = QPushButton("Sync info")
        self.b_inv_sync.setToolTip("Copy name, part number, category, cost and vendor to the inventory part "
                                   "(stock, bins and projects untouched)")
        self.b_inv_sync.clicked.connect(self.inv_sync)
        self.b_inv_open = QPushButton("Open in inventory")
        self.b_inv_open.clicked.connect(self.inv_open)
        for b in (self.b_inv_add, self.b_inv_link, self.b_inv_sync, self.b_inv_open):
            ir.addWidget(b)
        iv.addLayout(ir)
        self.tabs.addTab(invw, "Inventory")
        sv.addWidget(self.tabs, 1)
        row = QHBoxLayout()
        self.b_link = QPushButton("Insert (linked)")
        self.b_link.setObjectName("primary")
        self.b_link.setToolTip("Stays linked: editing the library part updates this project")
        self.b_link.clicked.connect(lambda: self.insert(True))
        self.b_copy = QPushButton("Insert copy")
        self.b_copy.setToolTip("Embedded copy, independent of later library changes")
        self.b_copy.clicked.connect(lambda: self.insert(False))
        row.addWidget(self.b_link)
        row.addWidget(self.b_copy)
        sv.addLayout(row)
        row2 = QHBoxLayout()
        for text, fn, tip in (("Open part", self.open_part, "Edit the part itself; Save updates the library"),
                              ("Edit info…", self.edit_info, "Name, part number, category, vendor, cost"),
                              ("Delete", self.delete, "Move to the library's .trash folder")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row2.addWidget(b)
        sv.addLayout(row2)
        sp.addWidget(side)
        sp.setStretchFactor(0, 3)
        sp.setStretchFactor(1, 2)
        lay.addWidget(sp, 1)
        bot = QHBoxLayout()
        b = QPushButton("+ Save object from this project…")
        b.clicked.connect(self.save_object)
        b.setEnabled(bool(doc and doc.result.bodies))
        bot.addWidget(b)
        b = QPushButton("+ Add from file (STEP, IGES, STL…)…")
        b.clicked.connect(self.add_file)
        bot.addWidget(b)
        b = QPushButton("Find duplicates…")
        b.setToolTip("Library parts with the same geometry or part number")
        b.clicked.connect(self.find_duplicates)
        bot.addWidget(b)
        bot.addStretch(1)
        self.where = QLabel()
        self.where.setObjectName("muted")
        bot.addWidget(self.where)
        b = QPushButton("Library folder…")
        b.setToolTip("Point at a shared drive to share standard parts with the team")
        b.clicked.connect(self.choose_folder)
        bot.addWidget(b)
        b = QPushButton("Close")
        b.clicked.connect(self.reject)
        bot.addWidget(b)
        lay.addLayout(bot)
        hint = "Picked point: inserts there. " if at is not None else "Tip: pick a face first to drop the part there. "
        self.hint = QLabel(hint + "Mate it with SmartMates (Alt-drag) or the Mate tool.")
        self.hint.setObjectName("muted")
        lay.addWidget(self.hint)
        self.reload()

    # ---------------------------------------------------------------- data
    def reload(self, select=None):
        cur = self.cat.currentText()
        self.cat.blockSignals(True)
        self.cat.clear()
        self.cat.addItems(["All", *self.lib.categories()])
        self.cat.setCurrentText(cur if cur else "All")
        self.cat.blockSignals(False)
        self.where.setText(f"{self.lib.root}")
        self.where.setToolTip(str(self.lib.root))
        self.fill(select=select)

    def fill(self, *_a, select=None):
        sel = select or (self.current() or {}).get("id")
        self.list.clear()
        parts = self.lib.parts(self.q.text(), self.cat.currentText(), self.kind.currentText())
        inv = self.store.inv if self.store is not None else None
        if self.store is not None:
            self.store.check_external()
        from ..stdparts.usage import std_status, summary_line
        keep = []
        for info in parts:
            st = std_status(inv, info) if inv is not None else None
            if st is not None:
                if self.only_prod.isChecked() and not st["in_production"]:
                    continue
                if self.only_used.isChecked() and not st["projects"]:
                    continue
                if self.only_low.isChecked() and not any(r["low"] for r in st["parts"]):
                    continue
            keep.append((info, st))
        parts = [i for i, _ in keep]
        for info, st in keep:
            extra = summary_line(st) if st else ""
            it = QListWidgetItem(QIcon(_thumb(info)), f"{info['name']}\n{info.get('part_number') or ''}"
                                                      + (f"\n{extra}" if extra else ""))
            if st and any(r["low"] for r in st["parts"]):
                it.setForeground(QColor(theme.ERROR))
            elif st and st["in_production"]:
                it.setForeground(QColor(theme.ACCENT_LINE))
            it.setData(Qt.UserRole, info)
            it.setToolTip(info.get("description") or info["name"])
            it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.list.addItem(it)
            if info["id"] == sel:
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        if not parts:
            self.big.setPixmap(QPixmap())
            self.info.setText("<p style='color:%s'>%s</p>" % (
                theme.MUTED, "No standard parts yet.<br><br>Save an object from this project, or add a STEP/IGES/STL "
                             "file — it can then be pulled into any project." if not self.lib.parts() else
                "Nothing matches."))
        self._enable()

    def current(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _enable(self):
        on = self.current() is not None
        for b in (self.b_link, self.b_copy):
            b.setEnabled(on and self.doc is not None)

    def show_info(self):
        info = self.current()
        self._enable()
        if not info:
            return
        self.big.setPixmap(_thumb(info, 200))
        e = html.escape
        rows = [("Part number", info.get("part_number")), ("Category", info.get("category")),
                ("Kind", info.get("kind")), ("Description", info.get("description")),
                ("Material", info.get("material")), ("Vendor", info.get("vendor")),
                ("Vendor part #", info.get("vendor_part_number")),
                ("Unit cost", f"${info['unit_cost']:,.2f}" if info.get("unit_cost") else None),
                ("Size", " × ".join(f"{v:g}" for v in info.get("size_mm", [])) + " mm" if info.get("size_mm") else None),
                ("Mass", f"{info['mass_kg']:.3f} kg" if info.get("mass_kg") is not None else None),
                ("Tags", ", ".join(info.get("tags") or [])),
                ("Version", f"v{info.get('version', 1)}"), ("Source", info.get("source"))]
        body = "".join(f"<tr><td style='color:{theme.MUTED};padding-right:10px'>{k}</td><td>{e(str(v))}</td></tr>"
                       for k, v in rows if v)
        link = f"<p><a style='color:{theme.ACCENT_LINE}' href='{e(info['url'])}'>{e(info['url'])}</a></p>" \
            if info.get("url") else ""
        notes = f"<p>{e(info['notes'])}</p>" if info.get("notes") else ""
        sim = ""
        try:
            from ..stdparts.similarity import compare
            sig = self.lib.signature(info)
            alike = []
            for o in self.lib.parts():
                if o["id"] != info["id"]:
                    self.lib.signature(o)
                    m = compare(sig, info, o)
                    if m["score"] >= 0.9 and m["level"]:
                        alike.append((m["score"], o["name"], m["level"]))
            if alike:
                sim = "<p style='color:%s'>Similar in library: %s</p>" % (theme.ACCENT_LINE, ", ".join(
                    f"{e(n)} ({lv})" for _s, n, lv in sorted(alike, reverse=True)[:3]))
        except Exception:  # noqa: BLE001
            pass
        used = ""
        if self.doc is not None:
            n = sum(1 for o in self.doc.objects if (o.std or {}).get("id") == info["id"])
            if n:
                used = f"<p style='color:{theme.ACCENT_LINE}'>Used {n}× in this project</p>"
        self.info.setText(f"<h3>{e(info['name'])}</h3><table>{body}</table>{link}{notes}{sim}{used}")
        self.show_usage(info)

    # ---------------------------------------------------------- where used
    def show_usage(self, info):
        from ..stdparts.usage import files_using, std_status
        e = html.escape
        self.used.clear()
        st = std_status(self.store.inv, info) if self.store is not None else None
        files = files_using(self.lib, info["id"])
        bold = self.used.font()
        bold.setBold(True)

        def group(title, color=None):
            g = QTreeWidgetItem([title])
            g.setFont(0, bold)
            g.setFlags(Qt.ItemIsEnabled)
            if color:
                g.setForeground(0, QColor(color))
            self.used.addTopLevelItem(g)
            g.setExpanded(True)
            return g

        def proj_row(parent, pr):
            via = f"  (in {pr['via']})" if pr.get("via") else ""
            act = [o for o in pr["production"] if o["active"]]
            status = ("Archived" if pr["archived"] else
                      ", ".join(o["status"] for o in act) if act else
                      "Scheduled" if pr["production"] else "")
            r = QTreeWidgetItem([pr["project"] + via, f"{pr['qty_per_unit']:g}", f"{pr['units']:g}",
                                 f"{pr['needed']:g}", status])
            r.setData(0, Qt.UserRole, "project:" + pr["project_id"])
            for c in (1, 2, 3):
                r.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            if act:
                r.setForeground(4, QColor(theme.ACCENT_LINE))
            parent.addChild(r)
            for o in pr["production"]:
                due = f"due {o['due']}" if o["due"] else "no due date"
                fab = f" · fab {o['fab'][0] or '?'} → {o['fab'][1] or '?'}" if any(o["fab"]) else ""
                c = QTreeWidgetItem([f"Production order · {o['qty'] or '?'} units · {due}{fab}", "", "", "",
                                     o["status"]])
                c.setText(0, f"{o['status']} · production order · {o['qty'] or '?'} units · {due}{fab}")
                c.setText(4, "")
                c.setForeground(0, QColor(theme.ACCENT_LINE if o["active"] else theme.MUTED))
                c.setData(0, Qt.UserRole, "project:" + pr["project_id"])
                r.addChild(c)
                c.setFirstColumnSpanned(True)
            r.setExpanded(bool(act))

        n_used = 0
        if st is not None and st["projects"]:
            prod_ids = {p["project_id"] for p in st["in_production"]}
            if st["in_production"]:
                g = group(f"In production ({len(prod_ids)})", theme.ACCENT_LINE)
                for pr in st["in_production"]:
                    proj_row(g, pr)
            rest = [p for p in st["projects"] if p["project_id"] not in prod_ids]
            if rest:
                g = group(f"Other projects ({len({p['project_id'] for p in rest})})")
                for pr in rest:
                    proj_row(g, pr)
            n_used += len({p["project_id"] for p in st["projects"]})
        if files:
            g = group(f"Daedalus files ({len(files)})")
            for f in files:
                import datetime as _dt
                when = _dt.datetime.fromtimestamp(f["saved"]).strftime("%Y-%m-%d") if f.get("saved") else ""
                r = QTreeWidgetItem([f["name"], f"{f['count']}", "", "",
                                     ("linked" if f["linked"] else "copy") + (f" · saved {when}" if when else "")
                                     + ("" if f["exists"] else " · file missing")])
                r.setToolTip(0, f["path"])
                r.setData(0, Qt.UserRole, "file:" + f["path"])
                if not f["exists"]:
                    r.setForeground(0, QColor(theme.MUTED))
                g.addChild(r)
            n_used += len(files)
        if not self.used.topLevelItemCount():
            g = group("Not used in any project or saved file yet")
            g.setForeground(0, QColor(theme.MUTED))
        self.tabs.setTabText(1, f"Used in · {n_used}" if n_used else "Used in")

        # inventory tab
        if self.store is None:
            self.inv_lab.setText(f"<p style='color:{theme.MUTED}'>Inventory not available.</p>")
            self.tabs.setTabText(2, "Inventory")
            for b in (self.b_inv_add, self.b_inv_link, self.b_inv_sync, self.b_inv_open):
                b.setEnabled(False)
            return
        rows = st["parts"]
        if rows:
            body = "".join(
                f"<tr><td><b>{e(r['name'])}</b>{' <span style=\'color:%s\'>linked</span>' % theme.OK if r['linked'] else ''}"
                f"<br><span style='color:{theme.MUTED}'>{e(r['partNumber'])}</span></td>"
                f"<td style='padding-left:12px;color:{theme.ERROR if r['low'] else theme.TEXT}'>{r['stock']:g} {e(r['unit'])}"
                f"{' · LOW' if r['low'] else ''}<br><span style='color:{theme.MUTED}'>reorder at {r['reorderPoint']:g}"
                f"</span></td><td style='padding-left:12px'>{e(r['location'] or 'no bin')}"
                f"<br><span style='color:{theme.MUTED}'>{('%g on order' % r['on_order']) if r['on_order'] else ''}"
                f"</span></td></tr>" for r in rows)
            short = (f"<p style='color:{theme.ERROR}'>Short {st['short']:g} for active projects "
                     f"(need {st['needed']:g}, have {st['stock']:g}"
                     + (f", {st['on_order']:g} on order" if st["on_order"] else "") + ")</p>") if st["short"] else \
                (f"<p style='color:{theme.OK}'>Covers active projects (need {st['needed']:g}, have "
                 f"{st['stock']:g})</p>" if st["needed"] else "")
            self.inv_lab.setText(f"<table>{body}</table>{short}")
            self.tabs.setTabText(2, f"Inventory · {st['stock']:g}")
        else:
            self.inv_lab.setText(f"<p style='color:{theme.MUTED}'>Not in the inventory. <b>Add to inventory</b> "
                                 "creates it (0 in stock) linked to this standard part; <b>Link existing…</b> "
                                 "connects one you already track.</p><p style='color:" + theme.MUTED +
                                 "'>Send BOM also adds and links it.</p>")
            self.tabs.setTabText(2, "Inventory")
        self.b_inv_add.setEnabled(not rows)
        self.b_inv_sync.setEnabled(bool(rows))
        self.b_inv_open.setEnabled(bool(rows))
        self.b_inv_link.setEnabled(True)

    def _used_open(self, item, _col):
        key = item.data(0, Qt.UserRole) or ""
        if key.startswith("file:"):
            p = key[5:]
            if Path(p).exists():
                self.open_requested.emit(p)
                self.accept()
        elif key.startswith("project:"):
            self.open_inventory.emit(key)
            self.accept()

    # ------------------------------------------------------- inventory actions
    def _commit_inv(self, msg):
        try:
            self.store.save()
        except OSError as e:
            QMessageBox.warning(self, "Inventory", f"Couldn't save the inventory:\n{e}")
        self.fill(select=(self.current() or {}).get("id"))
        self.window().statusBar().showMessage(msg, 6000) if hasattr(self.window(), "statusBar") else None

    def inv_add(self):
        from ..stdparts.usage import add_to_inventory
        info = self.current()
        if info and self.store is not None:
            p = add_to_inventory(self.store.inv, info)
            self._commit_inv(f"Added {p['name']} to the inventory")
            self.tabs.setCurrentIndex(2)

    def inv_link(self):
        from PySide6.QtWidgets import QInputDialog

        from ..stdparts.usage import link
        info = self.current()
        if not info or self.store is None:
            return
        inv = self.store.inv
        parts = sorted(inv.parts, key=lambda p: p["name"].lower())
        labels = [f"{p['name']}  ·  {p.get('partNumber') or '—'}  ·  {p.get('type')}" for p in parts]
        if not labels:
            QMessageBox.information(self, "Link", "The inventory has no parts yet — use Add to inventory.")
            return
        lab, ok = QInputDialog.getItem(self, "Link inventory part", f"Inventory part for {info['name']}:", labels, 0,
                                       False)
        if ok:
            p = link(inv, parts[labels.index(lab)]["id"], info)
            self._commit_inv(f"Linked {p['name']} to {info['name']}")
            self.tabs.setCurrentIndex(2)

    def inv_sync(self):
        from ..stdparts.usage import sync_to_inventory
        info = self.current()
        if info and self.store is not None:
            done = sync_to_inventory(self.store.inv, info)
            self._commit_inv(f"Synced {', '.join(done)}")

    def inv_open(self):
        from ..stdparts.usage import inventory_parts_for
        info = self.current()
        if info and self.store is not None:
            ps = inventory_parts_for(self.store.inv, info)
            if ps:
                self.open_inventory.emit(ps[0]["id"])
                self.accept()

    # ------------------------------------------------------------- actions
    def insert(self, link=True):
        info = self.current()
        if not info or self.doc is None:
            return
        try:
            name = self.lib.insert(self.doc, info["id"], link=link, at=self.at)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Insert", str(e))
            return
        self.inserted.emit(name)
        self.accept()

    def open_part(self):
        info = self.current()
        if info:
            self.open_requested.emit(info["path"])
            self.accept()

    def edit_info(self):
        info = self.current()
        if not info:
            return
        d = EditInfoDialog(self.lib, info, self)
        if d.exec():
            try:
                self.lib.update_info(info["id"], d.form.values())
            except LibraryError as e:
                QMessageBox.warning(self, "Standard part", str(e))
            self.reload(select=info["id"])

    def delete(self):
        info = self.current()
        if not info:
            return
        if QMessageBox.question(self, "Delete standard part",
                                f"Remove {info['name']} from the library?\n\nProjects that use it keep their copy "
                                "of the geometry. The files go to the library's .trash folder.") != QMessageBox.Yes:
            return
        self.lib.remove(info["id"])
        self.reload()

    def save_object(self):
        d = SaveStandardDialog(self.lib, self.doc, self.doc.active_object, parent=self)
        d.use_existing.connect(self._use_existing)
        if d.exec() and d.result_info:
            self.reload(select=d.result_info["id"])

    def _use_existing(self, pid, obj):
        if obj and self.doc is not None:
            name = swap_for_library_part(self.lib, self.doc, pid, obj, self)
            if name:
                self.inserted.emit(name)
                self.accept()
                return
        self.reload(select=pid)

    def find_duplicates(self):
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            groups = self.lib.duplicates()
        finally:
            QApplication.restoreOverrideCursor()
        if not groups:
            QMessageBox.information(self, "Duplicates", "No duplicates — every part in the library is distinct.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Duplicates ({len(groups)} group{'s' if len(groups) > 1 else ''})")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        lab = QLabel("Same geometry (or mirror image) or same part number. Double-click to select it; keep one "
                     "and delete the others.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        lst = QListWidget()
        lst.setIconSize(QSize(48, 48))
        for i, g in enumerate(groups, 1):
            hdr = QListWidgetItem(f"Group {i}")
            hdr.setFlags(Qt.NoItemFlags)
            hdr.setForeground(QColor(theme.ACCENT_LINE))
            lst.addItem(hdr)
            for p in g:
                it = QListWidgetItem(QIcon(_thumb(p, 48)), f"{p['name']} · {p.get('part_number') or '—'} · "
                                                         f"v{p.get('version', 1)} · {p.get('source', '')}")
                it.setData(Qt.UserRole, p["id"])
                lst.addItem(it)
        lst.itemDoubleClicked.connect(lambda it: (self.q.clear(), self.cat.setCurrentText("All"),
                                                  self.kind.setCurrentText("All"),
                                                  self.reload(select=it.data(Qt.UserRole)), dlg.accept()))
        v.addWidget(lst, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()

    def add_file(self):
        st = QSettings(*SETTINGS)
        p, _ = QFileDialog.getOpenFileName(self, "Add a part file to the library", st.value("last_dir", ""),
                                           FILE_FILTER)
        if not p:
            return
        d = SaveStandardDialog(self.lib, file=p, parent=self)
        d.use_existing.connect(lambda pid, _o: self.reload(select=pid))
        if d.exec() and d.result_info:
            self.reload(select=d.result_info["id"])

    def choose_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Standard parts library folder", str(self.lib.root))
        if not p:
            return
        QSettings(*SETTINGS).setValue("stdparts/path", p)
        self.lib = StandardLibrary(p)
        self.reload()
