"""CAD Link window: Daedalus subsystems on the open SolidWorks and/or Fusion document, the δ hotbar floating over
the CAD window, transfers between the two, and the full Daedalus window (same process) for simulation, CAM,
physics and drawings."""
from __future__ import annotations

import html
import sys
from pathlib import Path

from PySide6.QtCore import QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from ..ui import icons, theme
from .backend import KIND_NAME, SWError
from .link import SWLink

SW_ICONS = {"Isometric": "cube", "Front": "square", "Top": "square", "Right": "square", "Fit": "corners-out",
            "Rebuild": "arrows-clockwise", "Save": "floppy-disk", "STEP": "export", "PDF": "file-pdf",
            "Send BOM": "paper-plane-right", "Stock check": "check-square-offset", "Inventory": "package",
            "Std parts": "books", "Fasteners": "nut", "Save to library": "archive-tray",
            "Open in Daedalus": "arrow-square-out", "Simulate": "atom", "CAM": "gear-six", "Drawing": "compass",
            "Physics": "play", "Properties": "tag", "Ask": "sparkle", "Run macro…": "terminal",
            "Macro folder…": "folder-open", "From Daedalus": "download-simple"}
for _k, _v in SW_ICONS.items():
    icons.TOOL.setdefault(_k, _v)
from ..ui import hub as _hub  # noqa: E402
_hub.CAT_ICON.update({"Views": "eye", "CAD": "cube", "Library": "books", "Analysis": "atom",
                      "Macros": "terminal", "Properties": "tag"})

EXAMPLES = [
    "What's short to build 3 of this assembly?",
    "Add an M8 x 25 socket head screw in the selected hole",
    "Fill in missing part numbers and descriptions",
    "Send this BOM to inventory as project P25605",
]


def _item(text, muted=False, color=None):
    it = QTableWidgetItem("" if text is None else str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if muted:
        it.setForeground(QBrush(QColor(theme.MUTED)))
    if color:
        it.setForeground(QBrush(QColor(color)))
    return it


CADS = [("solidworks", False, "SolidWorks"), ("fusion", False, "Fusion"), ("rhino", False, "Rhino"),
        ("inventor", False, "Inventor"), ("solidedge", False, "Solid Edge"),
        ("solidworks", True, "SolidWorks (demo)"), ("fusion", True, "Fusion (demo)"), ("rhino", True, "Rhino (demo)")]


def sw_window_rect(prefix: str = "SOLIDWORKS") -> QRect | None:
    """The CAD program's main window in Qt (logical) coordinates, Windows only."""
    if sys.platform != "win32":
        return None
    try:
        import win32gui
        found = []

        contains = prefix.startswith("*")              # "*Rhino": anywhere in the title ("Untitled - Rhino 8")
        key = prefix.lstrip("*").upper()

        def cb(h, _):
            t = win32gui.GetWindowText(h).upper()
            if win32gui.IsWindowVisible(h) and ((key in t) if contains else t.startswith(key)):
                found.append(h)
        win32gui.EnumWindows(cb, None)
        if not found:
            return None
        l, t, r, b = win32gui.GetWindowRect(found[0])
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.screenAt(QRect(l, t, 1, 1).center()) or QGuiApplication.primaryScreen()
        k = scr.devicePixelRatio() or 1.0
        rect = QRect(int(l / k), int(t / k), int((r - l) / k), int((b - t) / k))
        return rect.adjusted(40, 140, -40, -40)                 # keep clear of the ribbon and frame
    except Exception:  # noqa: BLE001
        return None


class LinkWindow(QMainWindow):
    daedalus_opened = Signal(object)

    def __init__(self, link: SWLink, settings: QSettings | None = None, daedalus=None):
        super().__init__()
        self.link = link
        self.links = {self._key(link): link}      # every connected CAD session (SolidWorks and/or Fusion)
        self.settings = settings or QSettings("Daedalus", "Daedalus")
        self._daedalus = daedalus                 # existing MainWindow when started from Daedalus
        self.setWindowTitle("Daedalus · CAD Link")
        self.resize(560, 820)
        if self.settings.value("swlink/on_top", False, type=bool):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(10, 10, 10, 6)
        v.setSpacing(8)
        # header
        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.state = QLabel("Not connected")
        self.state.setStyleSheet("font-weight:600")
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(self.state)
        col.addWidget(self.sub)
        head.addWidget(self.dot)
        head.addLayout(col, 1)
        self.cad = QComboBox()
        self.cad.setToolTip("Which CAD program the Link works on — connect both to move parts between them")
        for app_, demo, label in CADS:
            self.cad.addItem(label, (app_, demo))
        self.cad.setCurrentIndex(next(i for i, (a_, d_, _l) in enumerate(CADS)
                                      if a_ == link.b.app and d_ == ("demo" in link.b.name)))
        self.cad.activated.connect(self._switch_cad)
        head.addWidget(self.cad)
        more = QPushButton("")
        more.setIcon(icons.icon("more"))
        more.setToolTip("Fusion add-in, Rhino link, macro folder")
        from PySide6.QtWidgets import QMenu
        mm = QMenu(more)
        mm.addAction("Install Fusion add-in…", self.install_fusion)
        mm.addAction("Install Rhino link…", lambda: install_rhino(self))
        mm.addAction("Macro folder (SolidWorks)…", lambda: self._macro_folder())
        mm.addAction("Show δ hotbar", lambda: self.hub.set_enabled(True))
        mm.addAction("Reset δ hotbar position", self._reset_hub)
        more.setMenu(mm)
        head.addWidget(more)
        rb = QPushButton("Refresh")
        rb.setIcon(icons.icon("arrows-clockwise"))
        rb.setToolTip("Reconnect and reload the active document")
        rb.clicked.connect(self.reconnect)
        head.addWidget(rb)
        dg = QPushButton("Diagnose")
        dg.setIcon(icons.icon("help"))
        dg.setToolTip("Check why the Link can't reach SolidWorks / Fusion, with the fix")
        dg.clicked.connect(self.diagnose)
        head.addWidget(dg)
        hb = QPushButton("δ")
        hb.setCheckable(True)
        hb.setFixedWidth(34)
        hb.setToolTip("Show / hide the δ radial hotbar over the CAD window (Ctrl+Shift+H)")
        hb.toggled.connect(lambda on: self.hub.set_enabled(on) if hasattr(self, "hub") else None)
        self.hub_btn = hb
        head.addWidget(hb)
        top = QCheckBox("On top")
        top.setChecked(self.settings.value("swlink/on_top", False, type=bool))
        top.toggled.connect(self._on_top)
        head.addWidget(top)
        v.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._doc_tab(), icons.icon("tag"), "Document")
        self.tabs.addTab(self._bom_tab(), icons.icon("package"), "BOM && Inventory")
        self.tabs.addTab(self._lib_tab(), icons.icon("books"), "Library")
        self.tabs.addTab(self._copilot_tab(), icons.icon("sparkle"), "Icarus")
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"Backend: {self.link.b.name}")

        self.hub = _hub.HubController(self, None, self._hub_groups(), self.settings, floating=True,
                                      area_fn=self._hub_area, key="swhub")
        QTimer.singleShot(0, self.hub.sync)
        self.hub.main = self                                   # keeps the δ button in step with the hub
        self.hub_action = self.hub_btn
        self.hub_btn.blockSignals(True)
        self.hub_btn.setChecked(self.hub.enabled)
        self.hub_btn.blockSignals(False)
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+Shift+H"), self, activated=lambda: self.hub.set_enabled(not self.hub.enabled))
        self._timer = QTimer(self, interval=4000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._last_title = None
        self.refresh()

    # ------------------------------------------------------------------ CAD sessions
    @staticmethod
    def _key(link):
        return (link.b.app, "demo" in link.b.name)

    def app_name(self, link=None):
        from .more_backends import label
        return label((link or self.link).b.app)

    def others(self):
        """Connected sessions of the other CAD program."""
        return [l for k, l in self.links.items() if l is not self.link and l.b.app != self.link.b.app]

    def _connect(self, app, demo):
        from .backend import connect
        key = (app, demo)
        if key in self.links:
            return self.links[key]
        link = SWLink(connect(demo=demo, app=app, lazy=True), library=self.link._library, store=self.link._store)
        self.links[key] = link
        return link

    def _switch_cad(self, i):
        app, demo = self.cad.itemData(i)
        try:
            link = self._connect(app, demo)
        except SWError as e:
            QMessageBox.warning(self, "CAD Link", str(e))
            self.cad.setCurrentIndex(next(j for j in range(self.cad.count())
                                          if self.cad.itemData(j) == self._key(self.link)))
            return
        self.link = link
        if hasattr(self.copilot, "executor"):
            self.copilot.executor.link = link
        self.statusBar().showMessage(f"Backend: {link.b.name}"
                                     + (f" · also connected: {', '.join(l.b.name for l in self.others())}"
                                        if self.others() else ""), 8000)
        self._last_title = None
        self.refresh()
        self.refresh_lib()
        self.hub.reposition()

    def send_across(self):
        others = self.others()
        if not others:
            return QMessageBox.information(
                self, "Send across", f"Connect the other CAD program too (pick it in the selector at the top), "
                                     f"then send the active {self.app_name()} document across.")
        target = others[0]
        r = self._guard(self.link.transfer_to, target)
        if r:
            where = (f"into {r['into']} as {r['component']}" if r["mode"] == "inserted"
                     else f"as a new document ({r['title']})")
            self.statusBar().showMessage(f"{r['from']} → {self.app_name(target)} {where}", 10000)

    def install_fusion(self):
        from ..fusion import install
        try:
            dest = install()
        except OSError as e:
            return QMessageBox.warning(self, "Fusion add-in", str(e))
        QMessageBox.information(
            self, "Fusion add-in",
            f"Installed to:\n{dest}\n\nIn Fusion: Utilities ▸ Add-Ins ▸ Scripts and Add-Ins ▸ Add-Ins ▸ "
            "DaedalusLink ▸ Run, and tick Run on Startup. Then pick Fusion in the CAD selector here.")

    # ------------------------------------------------------------------ helpers
    def _on_top(self, on):
        self.settings.setValue("swlink/on_top", bool(on))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()

    def _hub_area(self):
        r = sw_window_rect(self.link.b.window_prefix)
        if r is not None:
            return r
        from PySide6.QtGui import QGuiApplication
        return (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()

    def _guard(self, fn, *a, ok=None, **k):
        try:
            r = fn(*a, **k)
        except (SWError, FeatureError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "CAD Link", str(e))
            return None
        except Exception as e:  # noqa: BLE001
            from .backend import explain
            QMessageBox.warning(self, "CAD Link", explain(e))
            return None
        if ok:
            self.statusBar().showMessage(ok if isinstance(ok, str) else ok(r), 8000)
        return r

    def _poll(self):
        """Follow the active document; connect by itself once the CAD program is up."""
        if not getattr(self, "_was_connected", False):
            try:
                ok = self.link.b.connected()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                self.refresh()
                self.statusBar().showMessage(f"Connected to {self.app_name()}", 6000)
            return
        try:
            d = self.link.b.active_doc()
        except Exception:  # noqa: BLE001
            d = None
            self._was_connected = False
            self.refresh()
            return
        title = d.title if d else None
        if title != self._last_title:
            self.refresh()

    def _reset_hub(self):
        self.hub.offset = None
        self.hub.set_enabled(True)
        self.hub.reposition()

    def reconnect(self):
        b = self.link.b
        if hasattr(b, "reconnect"):
            try:
                b.reconnect()
            except Exception:  # noqa: BLE001
                pass
        self.refresh()

    def diagnose(self):
        from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QTextBrowser
        from .backend import diagnose
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rows = diagnose(self.link.b.app)
        except Exception as e:  # noqa: BLE001
            rows = [("Diagnose", False, f"{e.__class__.__name__}: {e}")]
        finally:
            QApplication.restoreOverrideCursor()
        mark = {True: f"<span style='color:{theme.OK}'>✔</span>", False: f"<span style='color:{theme.ERROR}'>✖</span>",
                None: f"<span style='color:{theme.MUTED}'>•</span>"}
        html = "<table cellspacing=6>" + "".join(
            f"<tr><td>{mark[ok]}</td><td><b>{k}</b></td><td>{v}</td></tr>" for k, ok, v in rows) + "</table>"
        text = "\n".join(f"[{'OK' if ok else 'FAIL' if ok is False else '--'}] {k}: {v}" for k, ok, v in rows)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{self.app_name()} connection")
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setHtml(html)
        tb.setMinimumSize(640, 340)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        cp = bb.addButton("Copy report", QDialogButtonBox.ActionRole)
        cp.clicked.connect(lambda: QApplication.clipboard().setText(text))
        rc = bb.addButton("Connect now", QDialogButtonBox.ActionRole)
        rc.clicked.connect(lambda: (self.reconnect(), dlg.accept()))
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    # ------------------------------------------------------------------ status
    def refresh(self):
        st = self.link.status()
        ok = st.get("connected")
        self.dot.setStyleSheet(f"color:{theme.OK if ok else theme.ERROR}; font-size:14pt")
        self._was_connected = bool(ok)
        if not ok:
            self.state.setText(f"{self.app_name()} not connected")
            why = getattr(self.link.b, "last_error", "") or f"Start {self.app_name()} — the Link connects by itself"
            self.sub.setText(why + "  ·  Diagnose shows details")
            self.sub.setWordWrap(True)
            self.sub.setToolTip(why)
            self._last_title = None
            return
        app = st["app"]
        doc = st.get("document")
        self._last_title = doc["title"] if doc else None
        self.state.setText(f"{app['name']} {app.get('year') or ''}".strip())
        if doc:
            cfg = doc["configuration"] or ("Default" if self.link.b.app == "solidworks" else "")
            self.sub.setText(" · ".join(b for b in (doc["title"], doc["kind"], cfg) if b))
        else:
            self.sub.setText("No document open")
        self._load_doc(doc)
        self.across_btn.setEnabled(bool(self.others()))
        self.across_btn.setText(f"Send to {self.app_name(self.others()[0])}" if self.others() else "Send across")
        if self.tabs.currentIndex() == 1:
            self.refresh_bom()

    # ------------------------------------------------------------------ document tab
    def _doc_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.doc_info = QLabel("")
        self.doc_info.setWordWrap(True)
        self.doc_info.setObjectName("muted")
        v.addWidget(self.doc_info)
        self.props = QTableWidget(0, 2)
        self.props.setHorizontalHeaderLabels(["Property", "Value"])
        self.props.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.props.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.props.verticalHeader().setVisible(False)
        v.addWidget(self.props, 1)
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.setIcon(icons.icon("add"))
        add.clicked.connect(lambda: (self.props.insertRow(self.props.rowCount()),
                                     self.props.setItem(self.props.rowCount() - 1, 0, QTableWidgetItem("")),
                                     self.props.editItem(self.props.item(self.props.rowCount() - 1, 0))))
        row.addWidget(add)
        row.addStretch(1)
        apply = QPushButton("Write to CAD")
        apply.setToolTip("Write these custom properties to the active document")
        apply.setObjectName("primary")
        apply.clicked.connect(self._write_props)
        row.addWidget(apply)
        v.addLayout(row)
        acts = QHBoxLayout()
        for label, fn in (("Rebuild", lambda: self._guard(self.link.command, "rebuild", ok="Rebuilt")),
                          ("Save", lambda: self._guard(self.link.command, "save", ok="Saved")),
                          ("STEP", lambda: self._guard(self.link.command, "export", "step",
                                                       ok=lambda p: f"Exported {p}")),
                          ("PDF", lambda: self._guard(self.link.command, "export", "pdf",
                                                      ok=lambda p: f"Exported {p}"))):
            b = QPushButton(label)
            b.setIcon(icons.icon(label))
            b.clicked.connect(fn)
            acts.addWidget(b)
        v.addLayout(acts)
        rt = QHBoxLayout()
        b = QPushButton("Open in Daedalus")
        b.setIcon(icons.icon("Open in Daedalus"))
        b.setToolTip("Geometry, mates and properties → Daedalus for simulation, CAM, physics, drawings")
        b.clicked.connect(lambda: self.open_in_daedalus())
        rt.addWidget(b)
        b = QPushButton("From Daedalus…")
        b.setIcon(icons.icon("From Daedalus"))
        b.setToolTip("Open a Daedalus part in the CAD program (as STEP)")
        b.clicked.connect(self.from_daedalus)
        rt.addWidget(b)
        self.across_btn = QPushButton("Send across")
        self.across_btn.setIcon(icons.icon("arrows-left-right"))
        self.across_btn.clicked.connect(self.send_across)
        rt.addWidget(self.across_btn)
        v.addLayout(rt)
        return w

    def _load_doc(self, doc):
        self.props.setRowCount(0)
        if not doc:
            self.doc_info.setText(f"Open a part or assembly in {self.app_name()}.")
            return
        bits = [doc["path"] or doc["title"]]
        if doc.get("material"):
            bits.append(f"Material: {doc['material']}")
        if doc.get("mass_kg"):
            bits.append(f"Mass: {doc['mass_kg']:.3f} kg")
        self.doc_info.setText(" · ".join(bits))
        for k, val in sorted(doc.get("properties", {}).items()):
            r = self.props.rowCount()
            self.props.insertRow(r)
            self.props.setItem(r, 0, QTableWidgetItem(k))
            self.props.setItem(r, 1, QTableWidgetItem(val))

    def _write_props(self):
        props = {}
        for r in range(self.props.rowCount()):
            k = (self.props.item(r, 0).text() if self.props.item(r, 0) else "").strip()
            val = self.props.item(r, 1).text() if self.props.item(r, 1) else ""
            if k:
                props[k] = val
        self._guard(self.link.set_properties, props, ok=f"Wrote {len(props)} properties")
        self.refresh()

    # ------------------------------------------------------------------ BOM tab
    def _bom_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Builds"))
        self.units = QDoubleSpinBox()
        self.units.setRange(1, 10000)
        self.units.setDecimals(0)
        self.units.setValue(1)
        self.units.valueChanged.connect(lambda _v: self.refresh_bom())
        row.addWidget(self.units)
        row.addStretch(1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_bom)
        row.addWidget(b)
        v.addLayout(row)
        self.bom = QTableWidget(0, 8)
        self.bom.setHorizontalHeaderLabels(["#", "Part", "Inventory part", "Qty", "Need", "Stock", "Location", "Status"])
        self.bom.verticalHeader().setVisible(False)
        self.bom.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.bom.setAlternatingRowColors(True)
        v.addWidget(self.bom, 1)
        self.bom_note = QLabel("")
        self.bom_note.setObjectName("muted")
        v.addWidget(self.bom_note)
        row = QHBoxLayout()
        b = QPushButton("Open inventory")
        b.setIcon(icons.icon("Inventory"))
        b.clicked.connect(self.open_inventory)
        row.addWidget(b)
        row.addStretch(1)
        b = QPushButton("Send BOM to inventory…")
        b.setIcon(icons.icon("Send BOM", theme.ON_ACCENT))
        b.setObjectName("primary")
        b.clicked.connect(self.send_bom)
        row.addWidget(b)
        v.addLayout(row)
        self.tabs_bom_loaded = False
        return w

    def refresh_bom(self):
        try:
            rep = self.link.stock_report(self.units.value())
        except Exception as e:  # noqa: BLE001
            self.bom.setRowCount(0)
            self.bom_note.setText(str(e))
            return
        self.bom.setRowCount(len(rep))
        short = 0
        for i, r in enumerate(rep):
            col = {"ok": theme.OK, "short": theme.ERROR}.get(r["status"], theme.WARN)
            short += r["status"] == "short"
            vals = [r["item"], r["part"], r.get("inventory_part") or "—", f"{r['qty_per_unit']:g}",
                    f"{r['needed']:g}", "" if r["in_stock"] is None else f"{r['in_stock']:g}", r["location"],
                    r["status"] + (f" ({r['short']:g})" if r["status"] == "short" else "")]
            for c, val in enumerate(vals):
                self.bom.setItem(i, c, _item(val, muted=c in (0, 6), color=col if c == 7 else None))
        self.bom.resizeColumnsToContents()
        self.bom.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        missing = sum(1 for r in rep if r["status"] == "not in inventory")
        self.bom_note.setText(f"{len(rep)} parts · {short} short · {missing} not in inventory "
                              f"for {self.units.value():g} build{'s' if self.units.value() != 1 else ''}")

    def send_bom(self):
        doc = self.link.b.active_doc()
        default = (doc.properties.get("Project") if doc else "") or (Path(doc.title).stem if doc else "")
        name, ok = QInputDialog.getText(self, "Send BOM to inventory", "Inventory project:", text=default)
        if not ok or not name.strip():
            return
        rep = self._guard(self.link.send_bom, name.strip(), self.units.value())
        if rep:
            msg = (f"Project {rep['project']}: {len(rep['added'])} new parts, {len(rep['linked'])} linked; "
                   f"HolocronId written to {rep['written_back']} {self.app_name()} part(s)")
            if rep["write_failed"]:
                msg += f" — {len(rep['write_failed'])} failed"
            self.statusBar().showMessage(msg, 12000)
            self.refresh_bom()

    def open_inventory(self):
        from ..ui.inventory_ui import InventoryPage
        if getattr(self, "_inv", None) is None:
            self._inv = InventoryPage()
            self._inv.setWindowTitle("Daedalus · Inventory")
            self._inv.resize(1200, 800)
            self._inv.closed.connect(self._inv.close)
            self._inv.insert_std_requested.connect(
                lambda sid: self._guard(self.link.insert_std, sid, ok=lambda r: f"Inserted {r['component']}"))
            self._inv.send_bom_requested.connect(self.send_bom)
        self._inv.show()
        self._inv.raise_()

    # ------------------------------------------------------------------ library tab
    def _lib_tab(self):
        from ..core import importer
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.lib_src = QComboBox()
        self.lib_src.addItem("Standard parts", "std")
        self.lib_src.addItem("Fasteners & bearings", "fastener")
        self.lib_src.currentIndexChanged.connect(lambda _i: self.refresh_lib())
        row.addWidget(self.lib_src)
        self.lib_q = QLineEdit()
        self.lib_q.setPlaceholderText("Search…  e.g. shcs m8, pivot pin")
        self.lib_q.setClearButtonEnabled(True)
        self.lib_q.textChanged.connect(lambda _t: self.refresh_lib())
        row.addWidget(self.lib_q, 1)
        v.addLayout(row)
        self.lib = QTreeWidget()
        self.lib.setHeaderLabels(["Name", "Details"])
        self.lib.setRootIsDecorated(False)
        self.lib.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.lib.currentItemChanged.connect(lambda *_: self._lib_sel())
        self.lib.itemDoubleClicked.connect(lambda *_: self.insert_selected())
        v.addWidget(self.lib, 1)
        form = QFormLayout()
        self.lib_len = QComboBox()
        self.lib_len.setEditable(True)
        form.addRow("Length", self.lib_len)
        v.addLayout(form)
        self.lib_mate = QCheckBox("Mate to the face / hole selected in the CAD program")
        self.lib_mate.setChecked(True)
        v.addWidget(self.lib_mate)
        row = QHBoxLayout()
        b = QPushButton("Save active part to library…")
        b.setIcon(icons.icon("Save to library"))
        b.clicked.connect(self.save_to_library)
        row.addWidget(b)
        row.addStretch(1)
        b = QPushButton("Insert into CAD")
        b.setObjectName("primary")
        b.clicked.connect(self.insert_selected)
        row.addWidget(b)
        v.addLayout(row)
        self._importer = importer
        QTimer.singleShot(0, self.refresh_lib)
        return w

    def refresh_lib(self):
        src = self.lib_src.currentData()
        q = self.lib_q.text()
        items = self._importer.std_items(q) if src == "std" else self._importer.fastener_items(q)
        self.lib.clear()
        for i in items[:500]:
            it = QTreeWidgetItem([i["name"], i.get("detail", "")])
            it.setData(0, Qt.UserRole, i)
            it.setIcon(0, icons.icon("books" if src == "std" else "nut"))
            self.lib.addTopLevelItem(it)
        if self.lib.topLevelItemCount():
            self.lib.setCurrentItem(self.lib.topLevelItem(0))
        self._lib_sel()

    def _lib_sel(self):
        it = self.lib.currentItem()
        i = it.data(0, Qt.UserRole) if it else None
        self.lib_len.clear()
        need = bool(i) and i.get("source") == "fastener" and i.get("needs_length")
        self.lib_len.setEnabled(need)
        if need:
            from ..core import standards as S
            inch = S.ALL[i["size"]].system != "ISO"
            for val in (S.INCH_LENGTHS if inch else S.METRIC_LENGTHS):
                self.lib_len.addItem(f'{val:g}"' if inch else f"{val:g} mm", val * S.IN if inch else float(val))
            self.lib_len.setCurrentIndex(min(6, self.lib_len.count() - 1))

    def insert_selected(self):
        it = self.lib.currentItem()
        if it is None:
            return
        i = it.data(0, Qt.UserRole)
        mate = self.lib_mate.isChecked()
        if i["source"] == "fastener":
            L = float(self.lib_len.currentData() or 0) if self.lib_len.isEnabled() else 0.0
            r = self._guard(self.link.insert_fastener, i["kind"], i["size"], L, mate)
        else:
            r = self._guard(self.link.insert_std, i["id"], mate)
        if r:
            msg = f"Inserted {r['component']}" + (f" · mated ({', '.join(r['mates'])})" if r["mates"] else "")
            if r.get("note"):
                msg += f" — {r['note']}"
            self.statusBar().showMessage(msg, 10000)

    def save_to_library(self):
        d = self.link.b.active_doc()
        if d is None:
            return QMessageBox.information(self, "Library", f"Open a part in {self.app_name()} first.")
        from .backend import DESC_KEYS, PN_KEYS, first_prop
        dlg = QDialog(self)
        dlg.setWindowTitle("Save to standard library")
        f = QFormLayout(dlg)
        name = QLineEdit(Path(d.title).stem)
        pn = QLineEdit(first_prop(d.properties, PN_KEYS))
        cat = QLineEdit()
        kind = QComboBox()
        kind.addItems(["manufactured", "purchased"])
        desc = QLineEdit(first_prop(d.properties, DESC_KEYS))
        vendor = QLineEdit(d.properties.get("Vendor", ""))
        for lab, wdg in (("Name", name), ("Part number", pn), ("Category", cat), ("Kind", kind),
                         ("Description", desc), ("Vendor", vendor)):
            f.addRow(lab, wdg)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        info = {"name": name.text().strip(), "part_number": pn.text().strip(), "category": cat.text().strip(),
                "kind": kind.currentText(), "description": desc.text().strip(), "vendor": vendor.text().strip()}
        r = self._guard(self.link.save_active_to_std, info,
                        ok=lambda s: f"Saved {s['name']} to the standard library (v{s.get('version', 1)})")
        if r:
            self.refresh_lib()

    # ------------------------------------------------------------------ copilot tab
    def _copilot_tab(self):
        from ..ui.copilot_panel import CopilotPanel
        from .tools import SW_SYSTEM, SW_TOOLS, SWToolExecutor
        self.copilot = CopilotPanel(None, executor=SWToolExecutor(self.link, self._open_marker_later, peers=self.others),
                                    tools=SW_TOOLS, system=SW_SYSTEM, examples=EXAMPLES,
                                    welcome=("What do you need in your CAD model?",
                                             "Icarus ({who}) works on the open SolidWorks / Fusion document — BOM, "
                                             "properties, fasteners, standard parts, inventory. Try one:"))
        return self.copilot

    def _open_marker_later(self, path):
        QTimer.singleShot(0, lambda: self._open_marker(path))

    # ------------------------------------------------------------------ Daedalus round trip
    def daedalus(self):
        if self._daedalus is None:
            from ..ui.main_window import MainWindow
            self._daedalus = MainWindow()
            self._daedalus.resize(1500, 900)
        self._daedalus.show()
        self._daedalus.raise_()
        try:
            self._daedalus.view.start()
        except Exception:  # noqa: BLE001
            pass
        return self._daedalus

    def _open_marker(self, path, page=None):
        w = self.daedalus()
        w.open_path(path, confirmed=True, show_notes=False)
        if page == "simulation":
            w._show_side(w.studies_panel)
        elif page == "cam":
            w.show_cam()
        elif page == "drawing":
            w.new_drawing()
        elif page == "physics":
            w._show_side(w.physics_panel)
        self.daedalus_opened.emit(w)

    def open_in_daedalus(self, page=None):
        path = self._guard(self.link.to_daedalus)
        if path:
            self._open_marker(path, page)
            self.statusBar().showMessage("Opened in Daedalus", 6000)

    def from_daedalus(self):
        p, _ = QFileDialog.getOpenFileName(self, f"Open a Daedalus part in {self.app_name()}",
                                           self.settings.value("last_dir", str(Path.home())), "Daedalus (*.daedalus)")
        if p:
            self._guard(self.link.from_daedalus, p, ok=lambda s: f"Opened {Path(s).name} in {self.app_name()}")
            self.refresh()

    # ------------------------------------------------------------------ hotbar
    def _macros(self):
        folder = self.settings.value("swlink/macros", "")
        out = []
        if folder and Path(folder).is_dir():
            for m in sorted(Path(folder).glob("*.swp"))[:20]:
                out.append((m.stem, "▶", f"Run macro {m.name}",
                            lambda _=False, p=str(m): self._guard(self.link.command, "macro", p,
                                                                  ok=f"Ran {Path(p).name}")))
        cmds = self.settings.value("swlink/commands", [], type=list) or []
        for c in cmds:                                    # "Label=1234"
            label, _, cid = str(c).partition("=")
            if cid.strip().isdigit():
                out.append((label.strip() or cid, "⌘", f"SolidWorks command {cid}",
                            lambda _=False, i=int(cid): self._guard(self.link.command, "command", i)))
        out.append(("Run macro…", "▶", "Pick a .swp macro to run", self._pick_macro))
        out.append(("Macro folder…", "📁", "Folder whose macros appear here", self._macro_folder))
        return out

    def _pick_macro(self):
        p, _ = QFileDialog.getOpenFileName(self, "Run macro", self.settings.value("swlink/macros", ""),
                                           "SolidWorks macros (*.swp *.swb)")
        if p:
            self._guard(self.link.command, "macro", p, ok=f"Ran {Path(p).name}")

    def _macro_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Macro folder", self.settings.value("swlink/macros", ""))
        if d:
            self.settings.setValue("swlink/macros", d)
            self.hub.groups["Macros"] = self._macros()
            if "Macros" in self.hub.bars:
                self.hub.close("Macros")
                self.hub.toggle("Macros")

    def _hub_groups(self):
        cmd = lambda *a, ok=None: (lambda: self._guard(self.link.command, *a, ok=ok))  # noqa: E731
        return [
            ("Views", [(n, "◻", f"{n} view", cmd("view", n)) for n in ("Isometric", "Front", "Top", "Right")]
             + [("Fit", "⤢", "Zoom to fit", cmd("fit"))]),
            ("CAD", [("Rebuild", "⟳", "Rebuild", cmd("rebuild", ok="Rebuilt")),
                            ("Save", "💾", "Save", cmd("save", ok="Saved")),
                            ("STEP", "⇪", "Export STEP next to the file", cmd("export", "step", ok="Exported STEP")),
                            ("PDF", "⇪", "Export PDF (drawings)", cmd("export", "pdf", ok="Exported PDF")),
                            ("Properties", "≡", "Edit custom properties",
                             lambda: (self._front(), self.tabs.setCurrentIndex(0)))]),
            ("Inventory", [("Send BOM", "⇢", "BOM → inventory project", lambda: (self._front(), self.send_bom())),
                           ("Stock check", "☑", "BOM vs stock", lambda: (self._front(), self.tabs.setCurrentIndex(1),
                                                                         self.refresh_bom())),
                           ("Inventory", "⬡", "Open Holocron inventory", self.open_inventory)]),
            ("Library", [("Std parts", "▦", "Insert a standard part",
                          lambda: (self._front(), self.tabs.setCurrentIndex(2), self.lib_src.setCurrentIndex(0))),
                         ("Fasteners", "⚙", "Insert a fastener into the selected hole",
                          lambda: (self._front(), self.tabs.setCurrentIndex(2), self.lib_src.setCurrentIndex(1))),
                         ("Save to library", "⇪", "Save the active part as a standard part", self.save_to_library)]),
            ("Analysis", [("Open in Daedalus", "↗", "Geometry + mates → Daedalus", lambda: self.open_in_daedalus()),
                          ("Simulate", "⚛", "FEA / frequency / buckling / thermal in Daedalus",
                           lambda: self.open_in_daedalus("simulation")),
                          ("Physics", "▶", "Motion with gravity and contacts", lambda: self.open_in_daedalus("physics")),
                          ("CAM", "⚙", "2.5D milling → G-code", lambda: self.open_in_daedalus("cam")),
                          ("Drawing", "📐", "Daedalus 2D drawing", lambda: self.open_in_daedalus("drawing")),
                          ("From Daedalus", "⇩", "Daedalus part → CAD", self.from_daedalus),
                          ("Send across", "⇄", "Active document → the other connected CAD program",
                           self.send_across)]),
            ("Macros", self._macros()),
            ("Icarus", [("Ask", "✦", "Ask Icarus", lambda: (self._front(), self.tabs.setCurrentIndex(3),
                                                                   self.copilot.input.setFocus()))]),
        ]

    def _front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(50, self.hub.sync)

    def closeEvent(self, e):
        for w in [self.hub.disk, *self.hub.bars.values()]:
            w.hide()
        super().closeEvent(e)


def install_rhino(parent=None):
    """Copy the Rhino link script and show the command that starts it in Rhino."""
    from PySide6.QtWidgets import QApplication
    from ..rhino import install, startup_command
    try:
        dest = install()
    except OSError as e:
        return QMessageBox.warning(parent, "Rhino link", str(e))
    cmd = startup_command(dest)
    QApplication.clipboard().setText(cmd)
    QMessageBox.information(
        parent, "Rhino link",
        f"Installed to:\n{dest}\n\nIn Rhino, paste this into the command line (it's on the clipboard):\n\n{cmd}\n\n"
        "To start it with Rhino every time: Tools ▸ Options ▸ General ▸ \"Run these commands every time Rhino "
        "starts\" — paste the same line there. Then pick Rhino in the CAD Link selector.")
    return dest


def run(demo=False, app="solidworks"):
    from PySide6.QtWidgets import QApplication

    from .. import APP_NAME
    from .backend import connect
    qapp = QApplication.instance() or QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_NAME)
    theme.apply(qapp)
    try:
        backend = connect(demo=demo, app=app, lazy=True)       # opens even before the CAD program is up
    except SWError as e:                                       # not fixable by waiting (no Windows / pywin32)
        from .more_backends import label
        name = label(app)
        r = QMessageBox.question(None, "CAD Link", f"{e}\n\nOpen the {name} demo session instead?")
        if r != QMessageBox.Yes:
            return 1
        backend = connect(demo=True, app=app)
    w = LinkWindow(SWLink(backend))
    w.show()
    return qapp.exec()
