from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QSplitter, QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, FILE_EXT, __version__, is_native
from ..core.document import MATERIALS, Document
from ..core.features import FeatureError
from .copilot_panel import CopilotPanel
from .doodle import DoodlePage
from .history import HistoryPanel
from .inspector import Inspector
from .layers import LayersPanel
from .physics_ui import PhysicsPanel
from .sim_ui import StudiesPanel
from .assembly_ui import AssemblyController, DriveDialog, MatesPanel, ObjectsPanel
from ..core.layers import hex_to_rgb
from .sketch3d_editor import Sketch3DBar, Sketch3DController
from .sketcher import SketchEditor
from .viewport import DISPLAY_MODES, VIEWS, Viewport
from . import theme


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Daedalus", "Daedalus")
        from .units_ui import default_units
        Document.default_units = default_units(self.settings)
        self.doc = Document()
        self.setAcceptDrops(True)
        self.resize(1500, 900)

        # center
        self.view = Viewport(self)
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.toolbelt = self._toolbelt()
        cl.addWidget(self.toolbelt)
        self.s3d = Sketch3DController(self.view)
        self.s3d_bar = Sketch3DBar(self.s3d)
        self.s3d_bar.hide()
        self.s3d_bar.finished.connect(self._finish_sketch3d)
        self.s3d_bar.cancelled.connect(self._cancel_sketch3d)
        from .adaptive import h_scroller
        cl.addWidget(h_scroller(self.s3d_bar, follow=True))
        cl.addWidget(self._view_bar())
        self.section_bar = self._section_bar()
        cl.addWidget(h_scroller(self.section_bar, follow=True))
        from .gcode_view import GcodeBar
        self.gcode_bar = GcodeBar(self.view)
        cl.addWidget(h_scroller(self.gcode_bar, follow=True))
        cl.addWidget(self.view, 1)
        self.sketch_editor = SketchEditor()
        self.sketch_editor.finished.connect(self._finish_sketch)
        self.sketch_editor.cancelled.connect(self._cancel_sketch)
        self.stack = QStackedWidget()
        self.stack.addWidget(center)
        self.stack.addWidget(self.sketch_editor)
        self.doodle = DoodlePage()
        self.doodle.make_sketch.connect(self._doodle_make)
        self.doodle.ask_claude.connect(self._doodle_ask)
        self.doodle.closed.connect(lambda: self.stack.setCurrentIndex(0))
        self.stack.addWidget(self.doodle)
        from .drawing_ui import DrawingPage
        self.drawing_page = DrawingPage(lambda: self.doc)
        self.drawing_page.closed.connect(lambda: self.stack.setCurrentIndex(0))
        self.drawing_page.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
        self.stack.addWidget(self.drawing_page)          # index 3
        from .inventory_ui import InventoryPage
        self.inventory_page = InventoryPage()
        self.inventory_page.closed.connect(lambda: self.stack.setCurrentIndex(0))
        self.inventory_page.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
        self.inventory_page.send_bom_requested.connect(self.send_bom_to_inventory)
        self.inventory_page.insert_std_requested.connect(self.insert_std_from_inventory)
        self.stack.addWidget(self.inventory_page)        # index 4
        from .home import HomePage
        from .recent import Autosave, Recent
        self.recent = Recent(self.settings)
        self.home = HomePage(self.recent, self.settings, __version__)
        self.home.open_requested.connect(lambda p: self.open_path(p))
        self.home.action.connect(self._home_action)
        self.stack.addWidget(self.home)                  # index 5
        self.HOME = 5
        from .quote_ui import QuotePage
        self.quote_page = QuotePage(lambda: self.doc)
        self.quote_page.closed.connect(lambda: self.stack.setCurrentIndex(0))
        self.quote_page.status.connect(lambda t: self.statusBar().showMessage(t, 6000))
        self.quote_page.simulate_requested.connect(lambda: self.simulate_gcode())
        self.stack.addWidget(self.quote_page)            # index 6
        self.QUOTE = 6
        self._recent_key: str | None = None              # file the current model came from (for its thumbnail)
        self.autosave = Autosave()
        from .adaptive import page_scroller
        for i in range(2, self.stack.count()):             # no page may force the window to stay wide
            pg = self.stack.widget(i)
            self.stack.removeWidget(pg)
            self.stack.insertWidget(i, page_scroller(pg))
        self.setCentralWidget(self.stack)
        self._sketching: tuple[str, bool] | None = None

        # left: history over inspector
        self.history = HistoryPanel(self.doc)
        self.inspector = Inspector(self.doc)
        self.layers = LayersPanel(self.doc)
        self.layers.move_requested.connect(self._move_selected_to_layer)
        self.objects_panel = ObjectsPanel(self.doc)
        self.mates_panel = MatesPanel(self.doc)
        self.mates_panel.drive_requested.connect(self.drive)
        self.physics_panel = PhysicsPanel(self.doc, self.view)
        self.physics_panel.status.connect(lambda t: self.statusBar().showMessage(t, 6000))
        self.studies_panel = StudiesPanel(self.doc, self.view)
        self.studies_panel.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
        from .cam_ui import CamPanel
        self.cam_panel = CamPanel(self.doc, self.view)
        self.cam_panel.status.connect(lambda t: self.statusBar().showMessage(t, 8000))
        self.cam_panel.auto_requested.connect(self.auto_cam)
        from .islands import Sidebar
        self.sidebar = Sidebar([("tree", "Feature tree", self.history), ("objects", "Objects", self.objects_panel),
                                ("mates", "Mates", self.mates_panel), ("layers", "Layers", self.layers),
                                ("physics", "Physics", self.physics_panel), ("studies", "Studies", self.studies_panel),
                                ("cam", "CAM", self.cam_panel), ("props", "Properties", self.inspector)],
                               self.settings)
        self.left_split = self.sidebar.split
        left = QDockWidget("Design", self)
        left.setObjectName("history")
        left.setWidget(self.sidebar)
        left.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        left.setMinimumWidth(60)                          # any width: panels scroll rather than hold it open
        self.addDockWidget(Qt.LeftDockWidgetArea, left)
        self.design_dock = left
        # the Design dock (history, objects, inspector) only makes sense while modelling
        self.stack.currentChanged.connect(lambda i: left.setVisible(i in (0, 1)))
        self.stack.currentChanged.connect(self._stack_changed)

        # right: copilot
        self.copilot = CopilotPanel(self.doc)
        right = QDockWidget("Icarus", self)
        right.setObjectName("copilot")
        from .islands import scrolled
        right.setWidget(scrolled(self.copilot))
        right.setMinimumWidth(60)                         # any width (scrolls when very narrow)
        self.addDockWidget(Qt.RightDockWidgetArea, right)
        self.copilot_dock = right
        right.visibilityChanged.connect(lambda _v: self._sync_claude_toggle_safe())
        if not self.settings.value("ui/claude_visible", True, type=bool):
            right.hide()
        QTimer.singleShot(0, self._sync_claude_toggle)
        self.copilot.render_provider = self._renders_for_claude

        self.asm = AssemblyController(self.view, self.doc, self)
        self.asm.message.connect(lambda t: self.statusBar().showMessage(t, 0 if t else 1))
        self.asm.mode_changed.connect(self._asm_mode_changed)
        from PySide6.QtGui import QShortcut
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self._escape)

        self.tool_groups.append(("View", self._view_group()))
        self._load_view_prefs()
        self.view.selection_changed.connect(self._on_view_selection)
        self.view.entities_changed.connect(self._on_entities)
        self.view.context_requested.connect(self._view_context)
        self.view.view_wheel_requested.connect(self.show_view_wheel)
        self.objects_panel.selection_changed.connect(self._on_list_selection)
        from .hub import HubController
        self.hub = HubController(self, self.view, self.tool_groups, self.settings)
        from .canvas_pick import CanvasPick
        self.canvas_pick = CanvasPick(self, self.view, self.set_sel_mode)
        sm0 = self.settings.value("view/sel_mode", "part")
        QTimer.singleShot(0, lambda: self.set_sel_mode(sm0 if sm0 in ("part", "face", "edge", "any") else "part"))
        self._menus()
        self.status_rebuild = QLabel()
        self.status_sel = QLabel()
        self.status_mat = QLabel()
        self.status_asm = QLabel()
        from PySide6.QtWidgets import QProgressBar
        self.load_bar = QProgressBar()
        self.load_bar.setFixedWidth(160)
        self.load_bar.setMaximumHeight(12)
        self.load_bar.setTextVisible(False)
        self.load_bar.hide()
        self.statusBar().addPermanentWidget(self.load_bar)
        self.view.loading.connect(self._on_loading)
        for w in (self.status_sel, self.status_asm, self.status_mat, self.status_rebuild):
            self.statusBar().addPermanentWidget(w)
        from .units_ui import SystemCombo
        self.units_combo = SystemCombo()
        self.units_combo.picked.connect(lambda k: self.doc.set_units(name=k))
        self.statusBar().addPermanentWidget(self.units_combo)
        self.units_combo.show_system(self.doc.unit_system())

        self.history.feature_selected.connect(self.inspector.show_feature)
        self.history.edit_sketch.connect(self.edit_sketch)
        self.history.export_dxf.connect(self.export_sketch_dxf)
        self.history.action.connect(self._tree_action)
        self.inspector.edit_sketch.connect(self.edit_sketch)
        self.history.feature_selected.connect(lambda _n: self._doc_changed())
        self.view.face_picked.connect(self._on_pick)
        self._pending = False
        self.doc.subscribe(self._doc_changed)
        self._update_title()
        self._doc_changed()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start(int(self.settings.value("autosave/seconds", 120)) * 1000)
        g = self.settings.value("ui/geometry")
        if g is not None:
            try:
                self.restoreGeometry(g)
            except Exception:  # noqa: BLE001
                pass
        ds = self.settings.value("ui/dock_state")
        restored = False
        if ds is not None:                                # sidebar / Icarus widths as the user left them
            try:
                restored = bool(self.restoreState(ds))
            except Exception:  # noqa: BLE001
                restored = False
        if not restored:
            QTimer.singleShot(0, lambda: self.resizeDocks([self.design_dock, self.copilot_dock], [340, 360],
                                                          Qt.Horizontal))

    # ------------------------------------------------------------ home
    def show_home(self):
        if self._sketching:
            self.statusBar().showMessage("Finish or cancel the sketch first", 4000)
            return
        self.asm.set_mode(None) if self.asm.mode else None
        self.home.set_has_model(bool(self.doc.features or self.doc.path))
        try:
            self.home.set_recoverable(self.autosave.orphans())
        except Exception:  # noqa: BLE001
            pass
        self._capture_thumb()
        self.stack.setCurrentIndex(self.HOME)

    def _stack_changed(self, i):
        home = i == self.HOME
        if home:
            self._copilot_was = self.copilot_dock.isVisible()
            self.copilot_dock.hide()
        elif getattr(self, "_copilot_was", None) is not None:
            if self._copilot_was:
                self.copilot_dock.show()
            self._copilot_was = None

    def _leave_home(self):
        if self.stack.currentIndex() == self.HOME:
            self.stack.setCurrentIndex(0)

    def _home_action(self, key):
        if key.startswith(("recover:", "discard:")):
            kind, sid = key.split(":", 1)
            item = next((it for it in self.autosave.orphans() if it["id"] == sid), None)
            if kind == "recover" and item:
                if not self._confirm_discard():
                    return
                try:
                    d = Document.open(item["file"])
                    self._swap_doc(d)
                    self.doc.path = Path(item["path"]) if item.get("path") else None
                    self.doc.dirty = True
                    self._recent_key = item.get("path") or item.get("source")
                    self._update_title()
                    self.statusBar().showMessage("Recovered unsaved work — save it to keep it", 10000)
                except Exception as e:  # noqa: BLE001
                    QMessageBox.critical(self, "Recover", str(e))
                    return
                self._leave_home()
            self.autosave.discard(sid)
            self.home.set_recoverable(self.autosave.orphans())
            return
        if key == "back":
            self._leave_home()
            return
        if key == "new":
            if self._confirm_discard():
                self._swap_doc(Document())
                self._recent_key = None
                self._update_title()
                self._leave_home()
            return
        if key == "open":
            self.open_doc()
            return
        if key == "import":
            if not self._confirm_discard():
                return
            path, _ = QFileDialog.getOpenFileName(self, "Open CAD file", self._dir(), self.IMPORT_FILTER)
            if path:
                self.open_path(path, confirmed=True)
            return
        self._leave_home()
        fn = {"doodle": self.open_doodle, "drawing2d": self.open_2d, "std": self.show_std_library,
              "quote": self.show_quote, "gcode": lambda: self.simulate_gcode(),
              "inventory": self.show_inventory, "sw": self.solidworks_link,
              "fusion": lambda: self.solidworks_link(app="fusion")}.get(key)
        if fn:
            fn()

    def _capture_thumb(self):
        """Thumbnail of the current model for the Home screen (after it has finished displaying)."""
        key = self._recent_key or (str(self.doc.path) if self.doc.path else None)
        if not key or self.stack.currentIndex() != 0 or not self.view.isVisible():
            return
        if getattr(self.view, "_mesh_queue", None) or not getattr(self.doc.result, "bodies", None):
            return
        from .recent import save_thumb
        save_thumb(self.view, key)

    def _autosave_tick(self):
        if not self.doc.dirty or not self.doc.features:
            return
        if self.autosave.last_cost > 3 and getattr(self, "_autosave_skip", 0) < int(self.autosave.last_cost / 3):
            self._autosave_skip = getattr(self, "_autosave_skip", 0) + 1    # big models: autosave less often
            return
        self._autosave_skip = 0
        try:
            self.autosave.write(self.doc, self._recent_key)
        except Exception:  # noqa: BLE001
            pass

    def _fill_recent_menu(self, menu):
        menu.clear()
        rows = self.recent.entries()[:15]
        for r in rows:
            a = menu.addAction(("★ " if r["pinned"] else "") + r["name"])
            a.setToolTip(r["path"])
            a.setEnabled(r["exists"])
            a.triggered.connect(lambda _=False, p=r["path"]: self.open_path(p))
        if not rows:
            menu.addAction("(none)").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Home screen…", self.show_home)
        menu.addAction("Clear list", lambda: self.recent.clear(keep_pinned=True))

    # --------------------------------------------------------- toolbelt
    def _show_side(self, panel):
        """Bring the sidebar panel `panel` to the front of its island."""
        if not self.design_dock.isVisible() and self.stack.currentIndex() in (0, 1):
            self.design_dock.show()
        self.sidebar.show_panel(panel)

    @staticmethod
    def _scroll(w):
        from PySide6.QtWidgets import QFrame, QScrollArea
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(w)
        return sc

    def _toolbelt(self):
        from PySide6.QtCore import QSize

        from . import icons
        tabs = QTabWidget()
        tabs.setObjectName("toolbelt")
        tabs.setDocumentMode(True)
        tabs.setFixedHeight(92 if icons.available() else 84)

        groups = []

        self._ribbon_pages = []

        def fill(w):
            """Create a ribbon page's buttons (pages other than the first are filled when first shown — faster
            start-up)."""
            if w._filled:
                return
            w._filled = True
            h = w.layout()
            for label, glyph, tip, fn in w._spec:
                if label is None:
                    sep = QLabel()
                    sep.setObjectName("sep")
                    h.addWidget(sep)
                    continue
                b = QToolButton()
                b.setObjectName("tool")
                ic = icons.icon(label)
                if not ic.isNull():
                    b.setIcon(ic)
                    b.setIconSize(QSize(22, 22))
                    b.setText(label)
                    b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                else:
                    b.setText(f"{glyph}\n{label}")
                b.setToolTip(f"<b>{label}</b><br>{tip}")
                b.setMinimumWidth(62)
                b.setAutoRaise(True)
                b.clicked.connect(fn)
                h.addWidget(b)
                w._buttons.append(b)
            h.addStretch(1)

        def tab(buttons):
            groups.append(buttons)
            w = QWidget()
            w._buttons = []
            w._spec = buttons
            w._filled = False
            h = QHBoxLayout(w)
            h.setContentsMargins(8, 3, 8, 3)
            h.setSpacing(1)
            if not self._ribbon_pages:
                fill(w)
            from .adaptive import h_scroller
            sa = h_scroller(w)
            self._ribbon_pages.append((sa, w))
            return sa

        self._fill_ribbon = fill

        tabs.addTab(tab([
            ("Doodle", "✍", "Doodle (or photo) → model", self.open_doodle),
            (None, None, None, None),
            ("Sketch", "✎", "New sketch — choose the plane (origin planes, a face, a reference plane)",
             lambda: self.add("sketch")),
            (None, None, None, None),
            ("Extrude", "▮", "Add material from the last sketch", lambda: self.add("extrude")),
            ("Cut", "▯", "Remove material from the last sketch", lambda: self.add("extrude", cut=True)),
            ("Revolve", "◐", "Revolve the last sketch", lambda: self.add("revolve")),
            ("Sweep", "➰", "Sweep a profile sketch along a path", lambda: self.add("sweep")),
            (None, None, None, None),
            ("3D Sketch", "⟋", "Lines and splines in XYZ", lambda: self.add("sketch3d")),
            ("Frame", "⌗", "Tube / bar members along a 3D sketch", lambda: self.add("frame")),
            ("HSS", "▣", "HSS builder: standard AISC / CSA / EN hollow sections or custom sizes — straight member "
                         "or frame along a sketch, with section properties", self.hss_builder),
            (None, None, None, None),
            ("Hole", "◎", "Hole on a face", lambda: self.add("hole")),
            ("Fillet", "◜", "Round edges", lambda: self.add("fillet")),
            ("Chamfer", "◸", "Bevel edges", lambda: self.add("chamfer")),
            ("Shell", "▢", "Hollow out", lambda: self.add("shell")),
            (None, None, None, None),
            ("Linear", "⋯", "Linear pattern of the last cut/extrude/hole", lambda: self.add("linear_pattern")),
            ("Circular", "✻", "Circular pattern", lambda: self.add("circular_pattern")),
            ("Mirror", "⇋", "Mirror body", lambda: self.add("mirror")),
            (None, None, None, None),
            ("Equations", "ƒ", "Global variables, equations and configurations (design table)",
             self.open_equations),
            ("Configs", "☷", "Switch configuration", self.config_menu_popup),
        ]), "Model")
        tabs.addTab(tab([
            ("Plane", "▱", "Reference plane: offset, angled, on a picked face, or 3 points", lambda: self.add("plane")),
            ("Axis", "↕", "Reference axis: of a picked hole/shaft, 2 points or 2 planes", lambda: self.add("axis")),
            (None, None, None, None),
            ("Loft", "⏢", "Blend between the last two sketches", lambda: self.add("loft")),
            ("Rib", "◢", "Rib from an open sketch line", lambda: self.add("rib")),
            ("Draft", "⏥", "Taper the side faces", lambda: self.add("draft")),
            ("Thread", "⌀", "Thread on the picked hole/shaft — cosmetic, or 'modelled' in the Inspector",
             lambda: self.add("thread")),
            (None, None, None, None),
            ("Helix", "➿", "Helix / spiral curve (sweep path for springs, coils, threads)", lambda: self.add("helix")),
            ("Spiral", "◌", "Flat spiral curve", lambda: self.add("spiral")),
            ("Curve XYZ", "∿", "Curve through XYZ points", lambda: self.add("curve_xyz")),
            ("Projected", "⇣", "Project the last sketch onto the body", lambda: self.add("projected_curve")),
            ("Composite", "⛓", "Join curves end to end", lambda: self.add("composite_curve")),
            ("Spring", "⌇", "Compression spring (closed, ground ends)", lambda: self.add("spring")),
            (None, None, None, None),
            ("Sheet metal", "⌐", "Base flange from the last sketch (gauge, bend radius, K-factor, reliefs in the "
                                 "Inspector)", lambda: self.add("sheet_metal")),
            ("Edge flange", "⌙", "Flange on the picked edge face of a sheet-metal part (length, angle, hem)",
             self.edge_flange),
            ("Fold/Unfold", "⇅", "Toggle sheet-metal parts between folded and flat", self.toggle_flat),
            ("Flat DXF", "⬒", "Export a sheet-metal flat pattern", self.export_flat),
            ("Gusset", "◣", "Corner plates on a frame layout", lambda: self.add("gusset")),
            ("Miter", "◸", "Mitred corners where flanges meet (miter flange)", self.miter_corners),
            ("Sketched bend", "⌙", "Bend the plate along a sketched line", lambda: self.add("sketched_bend")),
            ("Convert", "⬓", "Convert a shelled / thin-walled solid to sheet metal (flat pattern)",
             lambda: self.add("convert_sheet_metal")),
            ("Form tool", "◒", "Dimple, rib, louver or extruded hole on a sheet-metal face",
             lambda: self.add("form_tool")),
        ]), "Features")
        tabs.addTab(tab([
            ("Extrude", "▤", "Surface from the last sketch's curves (open or closed)",
             lambda: self.add("surface_extrude")),
            ("Revolve", "◑", "Revolved surface", lambda: self.add("surface_revolve")),
            ("Loft", "⏢", "Lofted surface through the last two sketches / curves", lambda: self.add("surface_loft")),
            ("Planar", "▱", "Flat surface inside a closed sketch", lambda: self.add("surface_planar")),
            (None, None, None, None),
            ("Offset", "⧈", "Offset (or copy) the picked faces as a surface", lambda: self.add("surface_offset")),
            ("Fill", "◍", "Patch a closed boundary", lambda: self.add("surface_fill")),
            ("Delete face", "⌫", "Remove faces: heal the body, or keep the rest as a surface",
             lambda: self.add("delete_face")),
            (None, None, None, None),
            ("Knit", "⧓", "Sew surfaces; closed → solid", lambda: self.add("knit")),
            ("Thicken", "▥", "Surface → solid", lambda: self.add("thicken")),
        ]), "Surfaces")
        tabs.addTab(tab([
            ("Object", "⬚", "New object (body) — new features go into it", self.new_object),
            (None, None, None, None),
            ("Move", "✥", "Drag objects; mates carry connected ones along (Esc to finish)",
             lambda: self.asm.set_mode("move")),
            ("Rotate", "⟲", "Drag an object to spin it about its centre (Shift = 15° steps)",
             lambda: self.asm.set_mode("rotate")),
            ("Turntable", "◎", "Rotate about the floor: drag to turn an object about the vertical axis; it stays "
                               "on the floor", lambda: self.asm.set_mode("turntable")),
            ("Orient", "↻", "Exact angles, 90° turns, drop to floor, picked face down", self.orient_object),
            ("Drop", "⤓", "Drop the selected object onto the floor (z = 0)", self.drop_to_floor),
            ("Face down", "⇩", "Turn the selected object so the picked face points down, on the floor",
             self.face_to_floor),
            (None, None, None, None),
            ("Mate", "⧉", "Click a face on two objects to mate them — standard, advanced (limit, width, symmetric, "
                          "path) and mechanical (gear, rack, screw, cam, slot)", lambda: self.asm.set_mode("mate")),
            ("Subassembly", "⧈", "Group the selected objects (rigid or flexible)", self.make_subassembly),
            ("Pattern", "⋯", "Linear / circular pattern of the selected components", self.component_pattern_menu),
            ("Mirror", "⇋", "Mirror the selected components (opposite-hand copies)",
             lambda: self._v28("component_pattern", "mirror")),
            ("Drive", "▶", "Animate a distance/angle mate", lambda: self.drive("")),
            (None, None, None, None),
            ("Interference", "⚠", "Check objects for overlap at the current position", self.interference),
            ("Explode", "✳", "Toggle the exploded view", self.toggle_explode),
            (None, None, None, None),
            ("Library", "🔩", "ISO / ANSI fasteners and bearings — pick a hole first to place and mate",
             self.library),
            ("Std parts", "▦", "Standard parts library — your saved parts, for any project (pick a face to drop "
                               "it there)", self.show_std_library),
            ("Save std", "⇪", "Save the active object as a standard part", self.save_std_part),
            ("Import", "⇲", "Import a part from another project, the standard library, inventory, the fastener "
                            "library or a file — linked, and mated to the picked face", self.import_part),
            ("Links", "⟳", "Reload linked part files that changed", self.update_links),
            ("BOM", "☰", "Bill of materials with mass and cost · Excel export", self.bom),
            (None, None, None, None),
            ("Simulate", "⚛", "Physics: gravity, contacts, mates as joints, motors and springs", self.simulate),
        ]), "Assembly")
        tabs.addTab(tab([
            ("Static", "▥", "Static stress: fixtures + loads → stress, displacement, factor of safety",
             lambda: self.new_study("static")),
            ("Frequency", "∿", "Natural frequencies and mode shapes", lambda: self.new_study("frequency")),
            ("Buckling", "⌇", "Buckling load factors", lambda: self.new_study("buckling")),
            ("Thermal", "♨", "Steady / transient heat conduction", lambda: self.new_study("thermal")),
            ("Fatigue", "⟳", "S-N fatigue life from a static study", lambda: self.new_study("fatigue")),
            ("Harmonic", "≋", "Forced vibration response", lambda: self.new_study("harmonic")),
            ("Flow", "≈", "CFD: external or internal flow, drag, pressure drop", lambda: self.new_study("flow")),
            (None, None, None, None),
            ("Assembly", "⧉", "Static stress over several parts: bonded faces, bolt connectors",
             lambda: self.new_study("assembly")),
            ("Drop", "⤓", "Drop test onto a rigid floor: impact stress, g-load", lambda: self.new_study("drop")),
            ("Topology", "✣", "Topology optimisation: lightest shape for the fixtures and loads",
             lambda: self.new_study("topology")),
            ("Design study", "⇶", "Sweep a dimension and compare mass / stress / frequency", self.design_study),
            (None, None, None, None),
            ("Material", "◈", "Material of the part (used by every study)", self.pick_material),
            ("Run", "▶", "Run the selected study", self.run_study),
        ]), "Simulation")
        tabs.addTab(tab([
            ("Material", "◈", "Material library: standard materials, custom, per object", self.pick_material),
            ("Mass", "⚖", "Mass properties", self.mass_props),
            ("Measure", "📏", "Measure points, edges, faces and bodies: distances, angles, radii, areas, slope, "
                              "mass — in your units", lambda: self.asm.set_mode("measure")),
            ("Units", "㎜", "Unit system (SI, US customary, construction ft-in), per-measurement units, decimals, "
                           "fractions, and a unit converter", self.units_dialog),
            ("Cut list", "☰", "Frame members by section and length", self.cut_list),
            ("Design data", "📘", "Machine-design calculators (shafts, keys, gears, couplings, brakes, fatigue, "
                                  "columns, cylinders…) and your reference handbooks", self.design_data),
            ("Fit", "⤢", "Zoom to fit (F)", lambda: self.view.fit()),
        ]), "Evaluate")
        tabs.addTab(tab([
            ("Auto CAM", "⚡", "Analyse the part and generate the whole program — asks for your machine, stock, "
                                "end mills and drills", self.auto_cam),
            ("CAM", "⚙", "2.5D milling: facing, pockets, profile, drilling → G-code", self.show_cam),
            ("Facing", "▭", "Add a facing operation", lambda: (self.show_cam(), self.cam_panel.add_op("facing"))),
            ("Pocket", "▣", "Pocket every floor (largest tool that fits)",
             lambda: (self.show_cam(), self.cam_panel.add_op("pocket"))),
            ("Profile", "▢", "Outside contour", lambda: (self.show_cam(), self.cam_panel.add_op("profile"))),
            ("Drill", "⊙", "Drill (and tap) every vertical hole",
             lambda: (self.show_cam(), self.cam_panel.add_op("drill"))),
            ("G-code", "⎙", "Generate and save G-code", lambda: (self.show_cam(), self.cam_panel.save())),
            ("Backplot", "▶", "Back-plot a G-code file (.nc/.tap) in the 3D view with time and distance",
             lambda: self.simulate_gcode()),
            ("Quote", "$", "Shop quote: CNC or sheet/laser, quantity breaks, machine schedule", self.show_quote),
            (None, None, None, None),
            ("Draft", "◿", "Mould/cast draft and undercuts against a pull direction", lambda: self.check("draft")),
            ("Thickness", "▤", "Wall thickness (thin walls in red)", lambda: self.check("thickness")),
            ("Overhang", "⛰", "3D-print overhangs that need support", lambda: self.check("overhang")),
            ("Clear", "✕", "Hide the analysis colours", self.clear_check),
        ]), "Manufacture")
        tabs.addTab(tab([
            ("Drawing", "📐", "New drawing: standard views, dimensions, title block", self.new_drawing),
            ("Sheets", "▤", "Open the drawing editor", self.show_drawings),
            ("Open 2D", "⎘", "Open a DXF / DWG / SVG / HPGL file for editing", self.open_2d),
            ("Flat dwg", "⬒", "Drawing of a sheet-metal flat pattern", lambda: (self.show_drawings(),
                                                                                self.drawing_page.new_flat())),
            (None, None, None, None),
            ("Export opts", "⚙", "STEP schema, units, mesh quality, neutral copies on save", self.export_options),
        ]), "Drawing")
        tabs.addTab(tab([
            ("Inventory", "⬡", "Holocron inventory: parts, stock, projects, reorder, vendors", self.show_inventory),
            ("Send BOM", "⇢", "Add this design's BOM to an inventory project", self.send_bom_to_inventory),
            ("Stock check", "☑", "BOM with stock, bin locations and shortfalls", self.bom),
            (None, None, None, None),
            ("Reorder", "⚠", "Parts at or below their reorder point", lambda: (self.show_inventory(),
                                                                              self.inventory_page.goto("reorder"))),
            ("Shortages", "▼", "Demand across all projects vs stock", lambda: (self.show_inventory(),
                                                                              self.inventory_page.goto("shortages"))),
        ]), "Inventory")
        tabs.addTab(tab([
            ("Ask", "✦", "Ask Icarus", lambda: (self.copilot_dock.show(), self.copilot.input.setFocus())),
            ("Doodle", "✍", "Draw a rough sketch and let Icarus model it", self.open_doodle),
            ("Image", "📎", "Attach a photo or drawing for Icarus", lambda: (self.copilot_dock.show(),
                                                                          self.copilot.attach_image())),
        ]), "Icarus")
        self.tool_groups = [(tabs.tabText(i), groups[i]) for i in range(tabs.count())]
        from .adaptive import Adaptive, text_mode
        self._adaptive = []
        mode = self.settings.value("ui/toolbar_size", "auto")
        for sa, page in self._ribbon_pages:
            def apply(lvl, page=page):
                for b in page._buttons:
                    b.setMinimumWidth({0: 62, 1: 44}.get(lvl, 30))
                text_mode(page._buttons, lvl < 2, under=True)
            self._adaptive.append(Adaptive(sa, apply, 3, mode, {"full": 0, "compact": 1, "icons": 2}, content=page))

        def shown(i):
            if 0 <= i < len(self._ribbon_pages):
                page = self._ribbon_pages[i][1]
                if not page._filled:
                    fill(page)
                    ad = self._adaptive[i]
                    ad.level = -1
                    ad.fit()
        tabs.currentChanged.connect(shown)
        return tabs

    def _view_bar(self):
        from PySide6.QtCore import QSize

        from . import icons
        bar = QWidget()
        bar.setObjectName("viewbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 3, 8, 3)
        h.setSpacing(2)
        hb = QToolButton()
        hb.setObjectName("view")
        hb.setText("Home")
        if icons.available():
            hb.setIcon(icons.icon("home"))
            hb.setIconSize(QSize(16, 16))
            hb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        hb.setToolTip("Home — recent files, new, open (Ctrl+Shift+0)")
        hb.clicked.connect(self.show_home)
        h.addWidget(hb)
        h.addSpacing(8)
        from PySide6.QtWidgets import QMenu as _QMenu
        vmb = QToolButton()
        vmb.setObjectName("view")
        vmb.setText("Views")
        vmb.setToolTip("Standard views (Ctrl+1…8) — Space in the view opens the view wheel")
        if icons.available():
            vmb.setIcon(icons.icon("views"))
            vmb.setIconSize(QSize(16, 16))
            vmb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        vm = _QMenu(vmb)
        for name in ["Isometric", "Front", "Top", "Right", "Back", "Bottom", "Left", "Dimetric"]:
            vm.addAction(name, lambda n=name: self.view.set_view(n))
        vm.addSeparator()
        vm.addAction("Normal to face", self.normal_to_face)
        vm.addAction("Zoom to fit", lambda: self.view.fit(animate=True))
        vmb.setMenu(vm)
        vmb.setPopupMode(QToolButton.InstantPopup)
        vmb.hide()
        h.addWidget(vmb)
        self._vb = {"home": hb, "views_menu": vmb, "views": []}
        for name in ["Isometric", "Front", "Top", "Right", "Back", "Bottom", "Left"]:
            b = QToolButton()
            b.setObjectName("view")
            b.setText(name if name != "Isometric" else "Iso")
            if name == "Isometric":
                b.setIcon(icons.icon("views"))
                b.setIconSize(QSize(16, 16))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setToolTip(f"{name} view")
            b.clicked.connect(lambda _=False, n=name: self.view.set_view(n))
            h.addWidget(b)
            self._vb["views"].append(b)
        h.addSpacing(10)
        from PySide6.QtWidgets import QButtonGroup
        sl = QLabel("Select")
        sl.setObjectName("muted")
        h.addWidget(sl)
        self._vb["labels"] = [sl]
        self._sel_group = QButtonGroup(bar)
        self._sel_buttons = {}
        for key, text, tip, ic in (("part", "Part", "Click / box selects whole parts (Ctrl+Shift+P)", "cube"),
                                   ("face", "Face", "Click / box selects faces (Ctrl+Shift+F)", "square"),
                                   ("edge", "Edge", "Click / box selects edges — then Fillet / Chamfer uses them "
                                                    "(Ctrl+Shift+E)", "line-segment"),
                                   ("any", "Face+Edge", "Whatever is under the cursor: an edge when you're on one, "
                                                         "else the face (Ctrl+Shift+J)", "cursor")):
            b = QToolButton()
            b.setObjectName("view")
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(key == "part")
            b.setToolTip(tip)
            if icons.available():
                b.setIcon(icons.icon(ic))
                b.setIconSize(QSize(14, 14))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.clicked.connect(lambda _=False, k=key: self.set_sel_mode(k))
            self._sel_group.addButton(b)
            self._sel_buttons[key] = b
            h.addWidget(b)
        h.addStretch(1)
        from PySide6.QtWidgets import QMenu
        from . import snap_settings
        snap = QToolButton()
        snap.setText("Snaps")
        if icons.available():
            snap.setIcon(icons.icon("snaps"))
            snap.setIconSize(QSize(16, 16))
            snap.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        else:
            snap.setText("🧲 Snaps")
        snap.setToolTip("3D snaps & inference (move, measure, 3D sketch, SmartMates)")
        sm = QMenu(snap)
        on = sm.addAction("Snapping on")
        on.setCheckable(True)
        on.setChecked(snap_settings.load().get("enabled", True))
        on.toggled.connect(lambda v: snap_settings.save({**snap_settings.load(), "enabled": v}))
        sm.addAction("Snaps & inference…", lambda: snap_settings.SnapSettingsDialog(self).exec())
        snap_settings.bus.changed.connect(lambda: (on.blockSignals(True),
                                                   on.setChecked(snap_settings.load().get("enabled", True)),
                                                   on.blockSignals(False)))
        snap.setMenu(sm)
        snap.setPopupMode(QToolButton.InstantPopup)
        h.addWidget(snap)
        mode = QComboBox()
        mode.setToolTip("Display mode (Alt+1…6) — Space in the view opens the view wheel")
        mode.addItems(DISPLAY_MODES)
        mode.currentTextChanged.connect(self.set_display_mode)
        self.mode_box = mode
        h.addWidget(mode)
        lab = QLabel("Layer")
        lab.setObjectName("muted")
        h.addSpacing(6)
        h.addWidget(lab)
        self._vb["labels"].append(lab)
        self.layer_box = QComboBox()
        self.layer_box.setToolTip("Active layer — new features go here")
        self.layer_box.setMinimumWidth(120)
        self.layer_box.activated.connect(self._pick_active_layer)
        h.addWidget(self.layer_box)
        mat = QToolButton()
        mat.setToolTip("Material — library of standard materials, custom materials, per object")
        mat.clicked.connect(lambda: self.pick_material())
        mat.setText(self.doc.material)
        if icons.available():
            mat.setIcon(icons.icon("material"))
            mat.setIconSize(QSize(16, 16))
            mat.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.material_box = mat
        h.addWidget(mat)
        h.addSpacing(6)
        cl = QToolButton()
        cl.setObjectName("claudeToggle")
        cl.setText("Icarus")
        cl.setCheckable(True)
        cl.setToolTip("Show / hide Icarus, the AI copilot (Ctrl+Shift+K)")
        if icons.available():
            cl.setIcon(icons.icon("Ask"))
            cl.setIconSize(QSize(16, 16))
            cl.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        cl.setStyleSheet(f"QToolButton#claudeToggle:checked {{ color: {theme.ACCENT_LINE}; }}")
        cl.toggled.connect(lambda on: self.set_claude_visible(on))
        self.claude_btn = cl
        h.addWidget(cl)
        self._vb.update(snap=snap, mode=mode, mat=mat, copilot=cl)
        from .adaptive import Adaptive, text_mode

        def apply(lvl):
            v = self._vb
            for b in v["views"]:
                b.setVisible(lvl < 1)
            v["views_menu"].setVisible(lvl >= 1)
            text_mode([v["home"], v["views_menu"], v["snap"], v["mat"], v["copilot"],
                       *self._sel_buttons.values()], lvl < 2)
            for lab_ in v["labels"]:
                lab_.setVisible(lvl < 2)
            small = lvl >= 3
            v["mode"].setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon if small else
                                          QComboBox.AdjustToContents)
            v["mode"].setMinimumContentsLength(7 if small else 0)
            self.layer_box.setMinimumWidth(70 if small else 120)
            self.layer_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon if small else
                                               QComboBox.AdjustToContents)
            self.layer_box.setMinimumContentsLength(6 if small else 0)
        from .adaptive import h_scroller
        sa = h_scroller(bar)                      # last resort when even icons don't fit: scrolls sideways
        sa.setObjectName("viewbarScroll")
        sa.setFixedHeight(bar.sizeHint().height() + 2)
        self._vb_adaptive = Adaptive(sa, apply, 4, self.settings.value("ui/toolbar_size", "auto"),
                                     {"full": 0, "compact": 1, "icons": 3}, content=bar)
        return sa

    # ------------------------------------------------------- view tools
    def _view_group(self):
        return [
            ("Front", "▣", "Front view (Ctrl+1)", lambda: self.view.set_view("Front")),
            ("Back", "▣", "Back view (Ctrl+2)", lambda: self.view.set_view("Back")),
            ("Left", "◧", "Left view (Ctrl+3)", lambda: self.view.set_view("Left")),
            ("Right", "◨", "Right view (Ctrl+4)", lambda: self.view.set_view("Right")),
            ("Top", "⬒", "Top view (Ctrl+5)", lambda: self.view.set_view("Top")),
            ("Bottom", "⬓", "Bottom view (Ctrl+6)", lambda: self.view.set_view("Bottom")),
            ("Iso", "⬚", "Isometric view (Ctrl+7)", lambda: self.view.set_view("Isometric")),
            ("Normal to", "⊥", "Look straight at the selected face (Ctrl+8)", self.normal_to_face),
            ("Fit", "⤢", "Zoom to fit (F) — Shift+F zooms to the selection", lambda: self.view.fit(animate=True)),
            ("Previous", "↶", "Previous view (Alt+←)", lambda: self.view.previous_view()),
            (None, None, None, None),
            ("Shaded+E", "◼", "Shaded with edges (Alt+1)", lambda: self.set_display_mode("Shaded + edges")),
            ("Shaded", "◼", "Shaded (Alt+2)", lambda: self.set_display_mode("Shaded")),
            ("Uncoloured", "◻", "Uncoloured — plain grey, ignores part colours (Alt+3)",
             lambda: self.set_display_mode("Uncoloured")),
            ("Hidden line", "▱", "Hidden lines removed (Alt+4)", lambda: self.set_display_mode("Hidden line")),
            ("Wireframe", "⬡", "Wireframe (Alt+5)", lambda: self.set_display_mode("Wireframe")),
            ("Translucent", "◌", "Translucent (Alt+6)", lambda: self.set_display_mode("Translucent")),
            ("Section", "✂", "Section view (Ctrl+Shift+X)",
             lambda: self.toggle_section(not self.section_bar.isVisible())),
            ("Isolate", "◎", "Show only the selection (I) — again to show everything", self.isolate_selected),
        ]

    def _load_view_prefs(self):
        s = self.settings
        self.view.nav = s.value("view/nav", "solidworks")
        self.view.zoom_to_cursor = s.value("view/zoom_cursor", True, type=bool)
        self.view.animate_views = s.value("view/animate", True, type=bool)
        self.view.hover_highlight = s.value("view/hover", True, type=bool)
        self.view.grid_on = s.value("view/grid", True, type=bool)
        self.view.simplify_moving = s.value("view/simplify_moving", True, type=bool)
        aa = s.value("view/antialias", "fxaa")
        if aa != self.view.antialias:
            self.view.set_antialiasing(aa)
        m = s.value("view/mode", DISPLAY_MODES[0])
        if m in DISPLAY_MODES:
            self.set_display_mode(m, save=False)

    def set_toolbar_size(self, mode):
        """Auto fits the ribbon and view bar to the window; Full / Compact / Icons only fix the density."""
        self.settings.setValue("ui/toolbar_size", mode)
        for a in [*getattr(self, "_adaptive", []), getattr(self, "_vb_adaptive", None)]:
            if a is not None:
                a.set_mode(mode)

    def set_nav(self, key):
        self.view.nav = key
        self.settings.setValue("view/nav", key)
        self.statusBar().showMessage(
            "Mouse: click selects, left-drag box-selects, middle-drag rotates (Ctrl pans, Shift zooms), "
            "right-drag pans" if key == "solidworks" else
            "Mouse: left-drag rotates, Ctrl+left-drag box-selects, middle pans, right zooms", 8000)

    def show_setup_check(self):
        from .setup_dialog import SetupDialog
        SetupDialog(self).exec()
        try:                                             # a new model / a started server: Icarus picks it up
            self.copilot._provider_changed(self.copilot.provider.currentIndex())
        except Exception:  # noqa: BLE001
            pass

    def set_grid(self, on):
        self.view.set_grid(on)
        self.settings.setValue("view/grid", bool(on))
        a = getattr(self, "grid_action", None)
        if a is not None and a.isChecked() != bool(on):
            a.setChecked(bool(on))
        if on:
            major, _minor, _h = self.view.grid_spacing()
            self.statusBar().showMessage(f"Floor grid: {major:g} mm squares", 4000)

    def set_antialias(self, mode):
        self.view.set_antialiasing(mode)
        self.settings.setValue("view/antialias", self.view.antialias)

    def show_graphics_info(self):
        i = self.view.graphics_info()
        tip = ""
        if i.get("software"):
            tip = ("<p style='color:%s'><b>Software rendering.</b> Windows isn't giving Daedalus your graphics card, "
                   "which makes the 3D view slow. Update the graphics driver; on a laptop open Windows Settings ▸ "
                   "System ▸ Display ▸ Graphics, add python.exe (or Daedalus) and pick <i>High performance</i>.</p>"
                   % theme.WARN)
        elif i.get("frame_ms", 0) > 40:
            tip = ("<p>Frames are slow for this model. Try View ▸ Display quality ▸ Draft, View ▸ Performance ▸ "
                   "Anti-aliasing: off, or hide parts you don't need (H).</p>")
        QMessageBox.information(self, "Graphics info", (
            f"<b>Renderer</b> {i.get('renderer', '?')}<br><b>Vendor</b> {i.get('vendor', '?')}<br>"
            f"<b>OpenGL</b> {i.get('version', '?')}<br><br><b>Model</b> {i['objects']} parts · "
            f"{i['triangles']:,} triangles<br><b>Frame time</b> {i['frame_ms']} ms "
            f"(≈{1000 / max(i['frame_ms'], 0.1):.0f} fps)<br><b>Anti-aliasing</b> {self.view.antialias.upper()}" + tip))

    def set_display_mode(self, mode, save=True):
        if mode not in DISPLAY_MODES:
            return
        self.view.apply_display_mode(mode)
        box = getattr(self, "mode_box", None)
        if box is not None and box.currentText() != mode:
            box.blockSignals(True)
            box.setCurrentText(mode)
            box.blockSignals(False)
        for k, a in getattr(self, "_mode_actions", {}).items():
            a.setChecked(k == mode)
        if save:
            self.settings.setValue("view/mode", mode)

    def show_view_wheel(self):
        if self.stack.currentIndex() != 0:
            return
        from .view_wheel import ViewWheel
        pick = getattr(self, "_last_pick", None)
        w = ViewWheel(self, self.view.display_mode, can_normal=bool(pick and pick.get("normal")))
        w.chosen.connect(self._view_cmd)
        w.popup_at()

    def _view_cmd(self, key):
        if key.startswith("view:"):
            self.view.set_view(key[5:])
        elif key.startswith("mode:"):
            self.set_display_mode(key[5:])
        elif key == "fit":
            self.view.fit(animate=True)
        elif key == "normal":
            self.normal_to_face()
        elif key == "previous":
            self.view.previous_view()

    def _world_face(self, info):
        """(normal, centre) of a picked face in world coordinates."""
        from ..core.assembly import pose_matrix
        import numpy as _np
        o = self.doc.obj(info["object"]) if info.get("object") and self.doc.has_obj(info["object"]) else None
        M = pose_matrix(self.view.objs[info["object"]].pose) if info.get("object") in self.view.objs else \
            (pose_matrix(o.pose) if o else _np.eye(4))
        n = M[:3, :3] @ _np.array(info["normal"], float)
        c = M[:3, :3] @ _np.array(info.get("center") or info.get("local") or (0, 0, 0), float) + M[:3, 3]
        return n, c

    def normal_to_face(self):
        pick = getattr(self, "_last_pick", None)
        if not pick or not pick.get("normal"):
            self.statusBar().showMessage("Click a face first, then Normal to (Ctrl+8)", 5000)
            return
        n, c = self._world_face(pick)
        self.view.normal_to(n, c)

    def zoom_selection(self):
        self.view.fit(self.view.selected or None, animate=True)

    # faces / edges
    def _set_hover(self, on):
        self.view.hover_highlight = bool(on)
        self.settings.setValue("view/hover", bool(on))
        if not on:
            self.view._hover_set(None)

    def set_sel_mode(self, mode):
        self.view.set_sel_mode(mode)
        self.settings.setValue("view/sel_mode", self.view.sel_mode)
        b = self._sel_buttons.get(self.view.sel_mode)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        pick = getattr(self, "canvas_pick", None)
        if pick is not None:
            pick.sync(self.view.sel_mode)
        self.statusBar().showMessage({
            "part": "Selecting parts",
            "face": "Selecting faces — click, Ctrl toggles, Shift adds, drag a box. Shell uses selected faces.",
            "edge": "Selecting edges — click, Ctrl toggles, Shift adds, drag a box. Fillet / Chamfer use them.",
            "any": "Selecting faces and edges — an edge when the cursor is on one, else the face."}[
            self.view.sel_mode], 6000)

    def _on_entities(self, ents):
        if not ents:
            self._sel_status(self.view.selected)
            return
        objs = list(dict.fromkeys(e["object"] for e in ents))
        if objs[-1] != self.doc.active_object and self.doc.has_obj(objs[-1]):
            self.doc.active_object = objs[-1]
            self.objects_panel.refresh()
        faces = [e for e in ents if e["kind"] == "face"]
        edges = [e for e in ents if e["kind"] == "edge"]
        bits = []
        if faces:
            area = 0.0
            kinds = set()
            for e in faces:
                try:
                    f = self.view.objs[e["object"]].faces[e["index"]]
                    area += f.area
                    kinds.add(f.geom_type.name.lower())
                except Exception:  # noqa: BLE001
                    pass
            bits.append(f"{len(faces)} face{'s' if len(faces) != 1 else ''} · {area:.1f} mm²"
                        + (f" · {'/'.join(sorted(kinds))}" if kinds else ""))
        if edges:
            length = sum(self._edge_length(e) for e in edges)
            bits.append(f"{len(edges)} edge{'s' if len(edges) != 1 else ''} · {length:.2f} mm")
        if len(objs) > 1:
            bits.append(f"on {len(objs)} parts")
        self.status_sel.setText("  ·  ".join(bits))

    def _edge_length(self, e):
        """Exact length of a picked edge (B-rep edge nearest its recorded point; mesh length as fallback)."""
        try:
            from ..core.selectors import select_edges
            body = self.doc.result.bodies.get(e["object"]) if hasattr(self.doc.result, "bodies") else None
            if body is not None:
                return float(select_edges(body, "at({:.6g},{:.6g},{:.6g})".format(*e["local"]))[0].length)
        except Exception:  # noqa: BLE001
            pass
        try:
            return self.view.edge_length(e["object"], e["index"])
        except Exception:  # noqa: BLE001
            return 0.0

    def _picked(self, kind):
        """Selected faces/edges of one part (the active one if it has some) → (object, [local points])."""
        ents = [e for e in self.view.entities if e["kind"] == kind]
        if not ents:
            return None, []
        objs = list(dict.fromkeys(e["object"] for e in ents))
        name = self.doc.active_object if self.doc.active_object in objs else objs[-1]
        return name, [e["local"] for e in ents if e["object"] == name]

    # selection
    def _sel_status(self, names):
        if len(names) > 1:
            self.status_sel.setText(f"{len(names)} parts selected")
        elif not names and not getattr(self, "_last_pick", None):
            self.status_sel.setText("")

    def _on_view_selection(self, names):
        if names and names[-1] != self.doc.active_object and self.doc.has_obj(names[-1]):
            self.doc.active_object = names[-1]
            self.objects_panel.refresh()
        self.objects_panel.set_multi(names)
        self._sel_status(names)

    def _on_list_selection(self, names):
        self.view.set_selected(names, emit=False)
        if names and names[-1] != self.doc.active_object and self.doc.has_obj(names[-1]):
            self.doc.active_object = names[-1]
        self._sel_status(names)

    def _visible_objects(self):
        return [o.name for o in self.doc.objects if o.visible and o.name in self.view.objs
                and self.view.objs[o.name].pd is not None]

    def select_all(self):
        self.view.set_selected(self._visible_objects())

    def invert_selection(self):
        cur = set(self.view.selected)
        self.view.set_selected([n for n in self._visible_objects() if n not in cur])

    def select_instances(self, name=None):
        """Every part sharing this part's geometry (e.g. all 400 copies of one screw in an imported assembly)."""
        name = name or (self.view.selected[-1] if self.view.selected else self._manip_target())
        bodies = self.doc.result.bodies
        b = bodies.get(name)
        if b is None:
            return
        from ..io.formats import geometry_key
        from .tessellate import shape_key

        def key(body, n):
            o = self.doc.obj(n)
            std = (o.std or {}).get("id") if o is not None else None
            return geometry_key(body) or (("std", std) if std else None) or shape_key(body)
        k = key(b, name)
        same = [n for n, bb in bodies.items() if n in self.view.objs and key(bb, n) == k]
        self.view.set_selected(same)
        self.statusBar().showMessage(f"{len(same)} instance(s) of {name}", 5000)

    def hide_selected(self):
        names = list(self.view.selected) or ([self._manip_target()] if self._manip_target() else [])
        if not names:
            return
        self.doc.checkpoint()
        for n in names:
            try:
                self.doc.set_object(n, checkpoint=False, visible=False)
            except FeatureError:
                pass
        self.view.set_selected([])
        self.statusBar().showMessage(f"Hid {len(names)} part(s) — Shift+H shows all", 5000)

    def show_all(self):
        hidden = [o.name for o in self.doc.objects if not o.visible]
        if self.view.isolated:
            self.view.set_isolated([])
        if not hidden:
            return
        self.doc.checkpoint()
        for n in hidden:
            self.doc.set_object(n, checkpoint=False, visible=True)

    def isolate_selected(self):
        if self.view.isolated:
            self.view.set_isolated([])
            self.statusBar().showMessage("Showing everything", 3000)
            return
        names = list(self.view.selected)
        if not names:
            self.statusBar().showMessage("Select parts first, then Isolate (I)", 4000)
            return
        self.view.set_isolated(names)
        self.view.fit(names, animate=True)
        self.statusBar().showMessage(f"Isolated {len(names)} part(s) — I or Esc to show everything", 0)

    def delete_selected(self):
        names = [n for n in self.view.selected if self.doc.has_obj(n)]
        if not names:
            return
        what = names[0] if len(names) == 1 else f"{len(names)} parts"
        if QMessageBox.question(self, "Delete", f"Delete {what} with their features and mates?") != \
                QMessageBox.Yes:
            return
        self.doc.checkpoint()
        failed = []
        for n in names:
            try:
                self.doc.delete_object(n, checkpoint=False)
            except FeatureError as e:
                failed.append(f"{n}: {e}")
        self.view.set_selected([])
        if failed:
            QMessageBox.warning(self, "Delete", "\n".join(failed))

    def _escape(self):
        if self.asm.mode:
            self.asm.set_mode(None)
        elif self.view.entities:
            self.view.set_entities([])
            self.view.clear_selection()
            self.view.render()
        elif self.view.selected:
            self.view.set_selected([])
            self.view.clear_selection()
            self._last_pick = None
            self.status_sel.setText("")
            self.view.render()
        elif self.view.isolated:
            self.view.set_isolated([])
            self.statusBar().clearMessage()

    def _view_context(self, info, gpos):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        name = info.get("object") if info else None
        if self.view.entities:
            nf = sum(1 for e in self.view.entities if e["kind"] == "face")
            ne = len(self.view.entities) - nf
            head = m.addAction(" · ".join(x for x in (f"{nf} face(s)" if nf else "", f"{ne} edge(s)" if ne else "")
                                          if x))
            head.setEnabled(False)
            if ne:
                m.addAction("Fillet selected edges", lambda: self.add("fillet"))
                m.addAction("Chamfer selected edges", lambda: self.add("chamfer"))
            if nf:
                m.addAction("Shell — open selected faces", lambda: self.add("shell"))
                if info and info.get("normal"):
                    m.addAction("Normal to this face\tCtrl+8", self.normal_to_face)
            m.addAction("Clear selection\tEsc", self._escape)
            m.addSeparator()
        side = self._side_of(info)
        if side:
            m.addAction(f"Sketch on this face ({side})", lambda: self.add_guarded(lambda: self.new_sketch(plane=side)))
            m.addSeparator()
        sm = m.addMenu("Select")
        for k_, t_ in (("part", "Parts"), ("face", "Faces"), ("edge", "Edges"), ("any", "Faces and edges")):
            a_ = sm.addAction(t_, lambda kk=k_: self.set_sel_mode(kk))
            a_.setCheckable(True)
            a_.setChecked(self.view.sel_mode == k_)
        if name and self.view.sel_mode != "part":
            name = None                    # in face/edge mode a right-click doesn't change the part selection
        if name:
            if name not in self.view.selected:
                self.view.set_selected([name])
            if info.get("normal"):
                self._last_pick = info
            sel = self.view.selected
            head = m.addAction(name if len(sel) == 1 else f"{len(sel)} parts")
            head.setEnabled(False)
            m.addAction("Zoom to selection\tShift+F", self.zoom_selection)
            if info.get("normal"):
                m.addAction("Normal to this face\tCtrl+8", self.normal_to_face)
            m.addSeparator()
            m.addAction("Hide\tH", self.hide_selected)
            m.addAction("Isolate\tI", self.isolate_selected)
            m.addAction("Show all\tShift+H", self.show_all)
            m.addAction("Select all instances\tCtrl+Shift+A", lambda: self.select_instances(name))
            m.addSeparator()
            m.addAction("Orient…", lambda: self.orient_object(name))
            m.addAction("Drop to floor", lambda: self.drop_to_floor(name))
            if info.get("normal"):
                m.addAction("Put this face on the floor", self.face_to_floor)
            m.addAction("Material…", lambda: self.pick_material(name))
            m.addSeparator()
            m.addAction("Delete\tDel", self.delete_selected)
        else:
            vm = m.addMenu("View")
            for n in ("Front", "Back", "Left", "Right", "Top", "Bottom", "Isometric"):
                vm.addAction(n, lambda nn=n: self.view.set_view(nn))
            dm = m.addMenu("Display mode")
            for md in DISPLAY_MODES:
                a = dm.addAction(md, lambda mm=md: self.set_display_mode(mm))
                a.setCheckable(True)
                a.setChecked(md == self.view.display_mode)
            m.addAction("Zoom to fit\tF", lambda: self.view.fit(animate=True))
            m.addAction("Previous view\tAlt+←", lambda: self.view.previous_view())
            m.addSeparator()
            m.addAction("Select all\tCtrl+A", self.select_all)
            m.addAction("Show all\tShift+H", self.show_all)
            if self.view.isolated:
                m.addAction("Exit isolate\tI", self.isolate_selected)
            a = m.addAction("Section view\tCtrl+Shift+X", lambda: self.toggle_section(not self.section_bar.isVisible()))
            a.setCheckable(True)
            a.setChecked(self.section_bar.isVisible())
            m.addAction("Copy view to clipboard\tCtrl+Shift+C", self.copy_view_image)
        m.exec(gpos)

    # section
    def _section_bar(self):
        from PySide6.QtWidgets import QButtonGroup, QCheckBox, QPushButton, QSlider
        bar = QWidget()
        bar.setObjectName("viewbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 3, 8, 3)
        lab = QLabel("Section")
        lab.setObjectName("h2")
        h.addWidget(lab)
        self._sec_axis = QButtonGroup(bar)
        for i, t in enumerate(("X", "Y", "Z")):
            b = QPushButton(t)
            b.setCheckable(True)
            b.setFixedWidth(32)
            b.setChecked(i == 2)
            self._sec_axis.addButton(b, i)
            h.addWidget(b)
        self._sec_slider = QSlider(Qt.Horizontal)
        self._sec_slider.setRange(0, 1000)
        self._sec_slider.setValue(500)
        self._sec_slider.setMaximumWidth(320)
        h.addWidget(self._sec_slider, 1)
        self._sec_pos = QLabel("50%")
        self._sec_pos.setObjectName("muted")
        self._sec_pos.setFixedWidth(40)
        h.addWidget(self._sec_pos)
        self._sec_flip = QCheckBox("Flip")
        h.addWidget(self._sec_flip)
        h.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(lambda: self.toggle_section(False))
        h.addWidget(close)
        upd = self._apply_section
        self._sec_axis.idClicked.connect(lambda _i: upd())
        self._sec_slider.valueChanged.connect(lambda _v: upd())
        self._sec_flip.toggled.connect(lambda _v: upd())
        bar.hide()
        return bar

    def _apply_section(self):
        if not self.section_bar.isVisible():
            return
        v = self._sec_slider.value() / 1000
        self._sec_pos.setText(f"{v * 100:.0f}%")
        self.view.set_section(self._sec_axis.checkedId(), v, self._sec_flip.isChecked())

    def toggle_section(self, on):
        self.section_bar.setVisible(bool(on))
        if getattr(self, "section_action", None) is not None and self.section_action.isChecked() != bool(on):
            self.section_action.blockSignals(True)
            self.section_action.setChecked(bool(on))
            self.section_action.blockSignals(False)
        if on:
            self._apply_section()
        else:
            self.view.set_section(None)

    # images
    def _grab_view(self):
        import os
        import tempfile
        from PySide6.QtGui import QImage
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            self.view.screenshot(tmp)
            return QImage(tmp).copy()
        finally:
            os.unlink(tmp)

    def save_view_image(self):
        base = (self.doc.path.stem if self.doc.path else "view") + ".png"
        p, _ = QFileDialog.getSaveFileName(self, "Save view as image", str(Path(self._dir()) / base),
                                           "PNG image (*.png);;JPEG image (*.jpg)")
        if p:
            self._grab_view().save(p)
            self.statusBar().showMessage(f"Saved {Path(p).name}", 4000)

    def copy_view_image(self):
        QApplication.clipboard().setImage(self._grab_view())
        self.statusBar().showMessage("View copied to the clipboard", 3000)

    # ------------------------------------------------------- object placement
    def _manip_target(self):
        pick = getattr(self, "_last_pick", None)
        if pick and pick.get("object") and self.doc.has_obj(pick["object"]):
            return pick["object"]
        return self.doc.active_object

    def _manip(self, fn, *a, **k):
        try:
            fn(*a, **k)
        except FeatureError as e:
            QMessageBox.warning(self, "Object", str(e))

    def orient_object(self, name=None):
        from .assembly_ui import OrientDialog
        dlg = getattr(self, "_orient_dlg", None)
        if dlg is not None:
            dlg.close()
        self._orient_dlg = OrientDialog(self.doc, name or self._manip_target(),
                                        lambda: getattr(self, "_last_pick", None), self)
        self._orient_dlg.show()

    def drop_to_floor(self, name=None):
        n = name or self._manip_target()
        self._manip(self.doc.drop_to_floor, n)
        self.statusBar().showMessage(f"{n} set on the floor", 4000)

    def face_to_floor(self):
        pick = getattr(self, "_last_pick", None)
        if not pick or not pick.get("object"):
            self.statusBar().showMessage("Click a face first, then Face down", 6000)
            return
        self._manip(self.doc.face_to_floor, pick["object"], pick["index"], pick.get("local"))
        self.statusBar().showMessage(f"{pick['object']}: picked face is now on the floor", 4000)

    # ------------------------------------------------------------ SolidWorks
    def _sw_link(self, demo=False, app="solidworks"):
        from ..sw import SWError, SWLink, connect
        links = self.__dict__.setdefault("_cadlinks", {})
        cur = links.get((app, demo))
        if cur is not None:
            return cur
        try:
            links[(app, demo)] = SWLink(connect(demo=demo, app=app, lazy=True))
        except SWError as e:
            QMessageBox.warning(self, "CAD Link", f"{e}\n\nCAD Link ▸ … demo session shows every feature with a "
                                                  "simulated session.")
            return None
        return links[(app, demo)]

    def solidworks_link(self, demo=False, app="solidworks"):
        link = self._sw_link(demo, app)
        if link is None:
            return
        from ..sw.ui import LinkWindow
        win = getattr(self, "_swwin", None)
        if win is None:
            self._swwin = win = LinkWindow(link, self.settings, daedalus=self)
        else:
            key = (link.b.app, demo)
            win.links.setdefault(key, link)
            idx = next((i for i in range(win.cad.count()) if win.cad.itemData(i) == key), None)
            if idx is not None:
                win.cad.setCurrentIndex(idx)
                win._switch_cad(idx)
        win.show()
        win.raise_()

    def pull_from_solidworks(self, app="solidworks"):
        link = self._sw_link(app=app)
        if link is None:
            return
        try:
            path = link.to_daedalus()
        except Exception as e:  # noqa: BLE001
            from ..sw.backend import explain
            return QMessageBox.warning(self, "CAD Link", explain(e))
        self.open_path(path)

    def push_to_solidworks(self, app="solidworks"):
        link = self._sw_link(app=app)
        if link is None:
            return
        if not self.doc.result.bodies:
            return self._need("Nothing modelled yet.")
        try:
            out = link.from_daedalus(self.doc)
        except Exception as e:  # noqa: BLE001
            from ..sw.backend import explain
            return QMessageBox.warning(self, "CAD Link", explain(e))
        from ..sw.more_backends import label
        self.statusBar().showMessage(f"Opened {Path(out).name} in {label(app)}", 8000)

    def install_fusion_addin(self):
        from ..fusion import install
        try:
            dest = install()
        except OSError as e:
            return QMessageBox.warning(self, "Fusion add-in", str(e))
        QMessageBox.information(self, "Fusion add-in",
                                f"Installed to:\n{dest}\n\nIn Fusion: Utilities ▸ Add-Ins ▸ Scripts and Add-Ins ▸ "
                                "Add-Ins ▸ DaedalusLink ▸ Run, and tick Run on Startup.")

    # ------------------------------------------------------------ design tree
    def _tree_action(self, kind, arg):
        if kind == "material":
            self.pick_material(arg or None)
        elif kind == "mass":
            self.mass_props()
        elif kind == "view":
            self.view.set_view(arg)
        elif kind == "sketch_on":
            self.add_guarded(lambda: self.new_sketch(plane=arg))
        elif kind == "drive":
            self.drive(arg)
        elif kind == "orient":
            self.orient_object(arg)
        elif kind == "update_links":
            self.update_links()
        elif kind == "open_source":
            src = self.doc.obj(arg).source or {}
            sysm = src.get("system")
            if sysm == "inventory" and src.get("id"):
                self._open_inventory_target(src["id"])
            elif sysm == "std":
                self.show_std_library()
            elif src.get("path") and Path(src["path"]).exists():
                self.open_path(src["path"])
            else:
                self.statusBar().showMessage("Source not found", 5000)
        elif kind == "drop":
            self.drop_to_floor(arg)
        elif kind in ("mate", "mates"):
            self._show_side(self.mates_panel)
            t = self.mates_panel.tree
            for i in range(t.topLevelItemCount()):
                if t.topLevelItem(i).data(0, Qt.UserRole) == arg:
                    t.setCurrentItem(t.topLevelItem(i))
        elif kind == "study":
            self._show_side(self.studies_panel)
            self.studies_panel.combo.setCurrentText(arg)

    # ------------------------------------------------------------ claude toggle
    def set_claude_visible(self, on: bool):
        on = bool(on)
        if self.copilot_dock.isVisible() != on:
            self.copilot_dock.setVisible(on)
            if on:
                self.copilot_dock.raise_()
        self.settings.setValue("ui/claude_visible", on)
        self._sync_claude_toggle()

    def _sync_claude_toggle_safe(self):
        try:
            self._sync_claude_toggle()
        except RuntimeError:                                  # window already gone (shutdown)
            pass

    def _sync_claude_toggle(self):
        if self.isMinimized():
            return
        on = self.copilot_dock.isVisible() if self.isVisible() else \
            self.settings.value("ui/claude_visible", True, type=bool)
        for w in (getattr(self, "claude_btn", None), getattr(self, "claude_action", None)):
            if w is not None and w.isChecked() != on:
                w.blockSignals(True)
                w.setChecked(on)
                w.blockSignals(False)
        if self.isVisible():
            self.settings.setValue("ui/claude_visible", on)

    # ------------------------------------------------------------ menus
    def _menus(self):
        mb = self.menuBar()

        def act(menu, text, fn, key=None):
            a = QAction(text, self)
            if key:
                a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        act(f, "Home", self.show_home, "Ctrl+Shift+0")
        f.addSeparator()
        act(f, "New", self.new_doc, QKeySequence.New)
        act(f, "Open…", self.open_doc, QKeySequence.Open)
        rm = f.addMenu("Open Recent")
        rm.aboutToShow.connect(lambda m=rm: self._fill_recent_menu(m))
        self._fill_recent_menu(rm)
        act(f, "Save", self.save_doc, QKeySequence.Save)
        act(f, "Save As…", lambda: self.save_doc(as_=True), QKeySequence.SaveAs)
        f.addSeparator()
        act(f, "Import part (projects, library, inventory)…", self.import_part, "Ctrl+Shift+M")
        act(f, "Import geometry into this part…", self.import_dialog, "Ctrl+I")
        act(f, "Export…", self.export, "Ctrl+E")
        act(f, "Export sketch as DXF…", lambda: self.export_sketch_dxf(None))
        act(f, "Export options…", self.export_options)
        act(f, "Save view as image…", self.save_view_image)
        act(f, "Copy view to clipboard", self.copy_view_image, "Ctrl+Shift+C")
        f.addSeparator()
        act(f, "Open 2D drawing (DXF/DWG/SVG)…", self.open_2d)
        act(f, "Drawings…", self.show_drawings, "Ctrl+D")
        f.addSeparator()
        act(f, "Standard parts library…", self.show_std_library, "Ctrl+L")
        act(f, "Save object as standard part…", self.save_std_part, "Ctrl+Shift+L")
        f.addSeparator()
        act(f, "Inventory…", self.show_inventory, "Ctrl+Shift+I")
        act(f, "Quote…", self.show_quote, "Ctrl+Alt+Q")
        act(f, "Simulate G-code file…", lambda: self.simulate_gcode())
        act(f, "Send BOM to inventory…", self.send_bom_to_inventory)
        f.addSeparator()
        act(f, "Quit", self.close, QKeySequence.Quit)

        e = mb.addMenu("&Edit")
        act(e, "Undo", self._undo, QKeySequence.Undo)
        act(e, "Redo", self._redo, QKeySequence.Redo)
        act(e, "Rebuild", lambda: self.doc.rebuild(), "Ctrl+B")
        e.addSeparator()
        act(e, "Units…", self.units_dialog)
        act(e, "Unit converter…", lambda: self.units_dialog(tab=1))
        e.addSeparator()
        view_only = []
        view_only.append(act(e, "Select all parts", self.select_all, "Ctrl+A"))
        act(e, "Clear selection", lambda: self.view.set_selected([]))
        act(e, "Invert selection", self.invert_selection)
        act(e, "Select all instances of this part", self.select_instances, "Ctrl+Shift+A")
        act(e, "Which way is up…", lambda: self.ask_up_axis())
        rap = act(e, "Rapid Sketch (click a flat face to sketch on it)",
                  lambda on: (self.settings.setValue("sketch/rapid", on),
                              self.statusBar().showMessage("Rapid Sketch on: click a flat face (face selection) "
                                                           "to start a sketch there" if on else "Rapid Sketch off",
                                                           6000)))
        rap.setCheckable(True)
        rap.setChecked(self.settings.value("sketch/rapid", False, type=bool))
        ask_pl = act(e, "Ask for the sketch plane", lambda on: self.settings.setValue("sketch/ask_plane", on))
        ask_pl.setCheckable(True)
        ask_pl.setChecked(self.settings.value("sketch/ask_plane", True, type=bool))
        e.aboutToShow.connect(lambda: ask_pl.setChecked(self.settings.value("sketch/ask_plane", True, type=bool)))
        ask_up = act(e, "Ask which way is up on import", lambda on: self.settings.setValue("import/ask_up", on))
        ask_up.setCheckable(True)
        ask_up.setChecked(self.settings.value("import/ask_up", True, type=bool))
        e.addSeparator()
        act(e, "Select parts", lambda: self.set_sel_mode("part"), "Ctrl+Shift+P")
        act(e, "Select faces", lambda: self.set_sel_mode("face"), "Ctrl+Shift+F")
        act(e, "Select edges", lambda: self.set_sel_mode("edge"), "Ctrl+Shift+E")
        act(e, "Select faces and edges", lambda: self.set_sel_mode("any"), "Ctrl+Shift+J")
        hov = act(e, "Highlight under the cursor", lambda on: self._set_hover(on))
        hov.setCheckable(True)
        hov.setChecked(self.view.hover_highlight)
        e.addSeparator()
        view_only.append(act(e, "Hide selected", self.hide_selected, "H"))
        view_only.append(act(e, "Show all", self.show_all, "Shift+H"))
        view_only.append(act(e, "Isolate selected", self.isolate_selected, "I"))
        view_only.append(act(e, "Delete selected parts", self.delete_selected, QKeySequence.Delete))
        for a_ in view_only:                  # plain keys only while the 3D view has focus (never while typing)
            a_.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.view.addAction(a_)

        v = mb.addMenu("&View")
        vk = {"Front": "Ctrl+1", "Back": "Ctrl+2", "Left": "Ctrl+3", "Right": "Ctrl+4", "Top": "Ctrl+5",
              "Bottom": "Ctrl+6", "Isometric": "Ctrl+7"}
        orient = v.addMenu("Orientation")
        for name in VIEWS:
            act(orient, name, lambda _=False, n=name: self.view.set_view(n), vk.get(name))
        act(orient, "Normal to selected face", self.normal_to_face, "Ctrl+8")
        orient.addSeparator()
        scoped = [act(orient, "Rotate view 90° left", lambda: self.view.rotate_view(-90), "Ctrl+Left"),
                  act(orient, "Rotate view 90° right", lambda: self.view.rotate_view(90), "Ctrl+Right"),
                  act(orient, "Tilt view 90° up", lambda: self.view.rotate_view(0, 90), "Ctrl+Up"),
                  act(orient, "Tilt view 90° down", lambda: self.view.rotate_view(0, -90), "Ctrl+Down")]
        act(v, "View wheel…  (Space in the view)", self.show_view_wheel)
        act(v, "Previous view", lambda: self.view.previous_view(), "Alt+Left")
        scoped.append(act(v, "Zoom to selection", self.zoom_selection, "Shift+F"))
        for a_ in scoped:
            a_.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.view.addAction(a_)
        dm = v.addMenu("Display mode")
        from PySide6.QtGui import QActionGroup as _AG
        g_dm = _AG(self)
        self._mode_actions = {}
        for i, m_ in enumerate(DISPLAY_MODES):
            a_ = act(dm, m_, lambda _=False, m=m_: self.set_display_mode(m), f"Alt+{i + 1}")
            a_.setCheckable(True)
            a_.setChecked(m_ == self.view.display_mode)
            g_dm.addAction(a_)
            self._mode_actions[m_] = a_
        self.section_action = act(v, "Section view", lambda on: self.toggle_section(on), "Ctrl+Shift+X")
        self.section_action.setCheckable(True)
        nav = v.addMenu("Mouse")
        from .viewport import NAV_SCHEMES
        g_nav = _AG(self)
        for k_, lab_ in NAV_SCHEMES.items():
            a_ = nav.addAction(lab_)
            a_.setCheckable(True)
            a_.setChecked(self.view.nav == k_)
            a_.triggered.connect(lambda _=False, k=k_: self.set_nav(k))
            g_nav.addAction(a_)
        nav.addSeparator()
        zc = nav.addAction("Zoom toward the cursor")
        zc.setCheckable(True)
        zc.setChecked(self.view.zoom_to_cursor)
        zc.toggled.connect(lambda on: (setattr(self.view, "zoom_to_cursor", on),
                                       self.settings.setValue("view/zoom_cursor", on)))
        an = nav.addAction("Animate view changes")
        an.setCheckable(True)
        an.setChecked(self.view.animate_views)
        an.toggled.connect(lambda on: (setattr(self.view, "animate_views", on),
                                       self.settings.setValue("view/animate", on)))
        from .adaptive import SIZES
        sbm = v.addMenu("Sidebar")
        sbm.aboutToShow.connect(lambda: (sbm.clear(), self.sidebar.fill_menu(sbm),
                                         sbm.addSeparator(),
                                         sbm.addAction("Show sidebar" if not self.design_dock.isVisible()
                                                       else "Hide sidebar", lambda: self.design_dock.setVisible(
                                                           not self.design_dock.isVisible()))))
        sbm.addAction("…")                                 # filled when opened
        tsm = v.addMenu("Toolbar size")
        g_ts = _AG(self)
        cur_ts = self.settings.value("ui/toolbar_size", "auto")
        for k_, lab_ in SIZES.items():
            a_ = tsm.addAction(lab_)
            a_.setCheckable(True)
            a_.setChecked(k_ == cur_ts)
            a_.triggered.connect(lambda _=False, k=k_: self.set_toolbar_size(k))
            g_ts.addAction(a_)
        v.addSeparator()
        act(v, "Zoom to fit", lambda: self.sketch_editor.canvas.fit()
            if self._sketching and self.stack.currentIndex() == 1 else self.view.fit(), "F")
        persp = act(v, "Perspective", lambda on: self.view.set_perspective(on))
        persp.setCheckable(True)
        persp.setChecked(True)
        sk = act(v, "Show sketches", lambda on: self.view.set_sketches_visible(on))
        sk.setCheckable(True)
        sk.setChecked(True)
        gr = act(v, "Floor grid", lambda on: self.set_grid(on), "G")
        gr.setCheckable(True)
        gr.setChecked(self.view.grid_on)
        gr.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.view.addAction(gr)
        self.grid_action = gr
        pm = v.addMenu("Performance")
        g_aa = _AG(self)
        for k_, lab_ in (("fxaa", "Anti-aliasing: FXAA (fast, default)"), ("msaa", "Anti-aliasing: MSAA ×4 (smoothest)"),
                         ("off", "Anti-aliasing: off (fastest)")):
            a_ = pm.addAction(lab_)
            a_.setCheckable(True)
            a_.setChecked(k_ == self.view.antialias)
            a_.triggered.connect(lambda _=False, k=k_: self.set_antialias(k))
            g_aa.addAction(a_)
        pm.addSeparator()
        smv = pm.addAction("Simplify big models while the view moves")
        smv.setCheckable(True)
        smv.setChecked(self.view.simplify_moving)
        smv.toggled.connect(lambda on: (setattr(self.view, "simplify_moving", on),
                                        self.settings.setValue("view/simplify_moving", on)))
        pm.addAction("Graphics info…", self.show_graphics_info)
        self.claude_action = act(v, "Icarus panel", lambda on: self.set_claude_visible(on), "Ctrl+Shift+K")
        self.claude_action.setCheckable(True)
        self.claude_action.setChecked(self.copilot_dock.isVisible() or
                                      self.settings.value("ui/claude_visible", True, type=bool))
        from . import tessellate as _tess
        qm = v.addMenu("Display quality")
        cur_q = self.settings.value("view/quality", "normal")
        _tess.set_quality(cur_q if cur_q in _tess.QUALITY else "normal")
        self._quality_actions = {}
        for qk, ql in (("fine", "Fine (smooth curves)"), ("normal", "Normal"), ("draft", "Draft (big assemblies)")):
            qa = qm.addAction(ql)
            qa.setCheckable(True)
            qa.setChecked(qk == _tess.quality())
            qa.triggered.connect(lambda _=False, k=qk: self.set_display_quality(k))
            self._quality_actions[qk] = qa
        self.hub_action = act(v, "Tool hub", lambda on: self.hub.set_enabled(on), "Ctrl+Shift+H")
        self.hub_action.setCheckable(True)
        self.hub_action.setChecked(self.hub.enabled)
        v.addSeparator()
        from PySide6.QtGui import QActionGroup
        from . import theme as _theme
        tm = v.addMenu("Theme")
        grp = QActionGroup(self)
        for key, label in _theme.THEMES.items():
            a_ = tm.addAction(label)
            a_.setCheckable(True)
            a_.setChecked(key == _theme.MODE)
            grp.addAction(a_)
            a_.triggered.connect(lambda _=False, k=key: self.switch_theme(k))

        sw = mb.addMenu("&CAD Link")
        act(sw, "SolidWorks Link…", self.solidworks_link, "Ctrl+Shift+W")
        act(sw, "Fusion Link…", lambda: self.solidworks_link(app="fusion"))
        act(sw, "Rhino Link…", lambda: self.solidworks_link(app="rhino"))
        act(sw, "Inventor Link…", lambda: self.solidworks_link(app="inventor"))
        act(sw, "Solid Edge Link…", lambda: self.solidworks_link(app="solidedge"))
        sw.addSeparator()
        from ..sw.more_backends import APPS
        for app_, (lab_, _demo) in APPS.items():
            sub = sw.addMenu(lab_)
            act(sub, f"Pull active {lab_} model", lambda _=False, a=app_: self.pull_from_solidworks(app=a))
            act(sub, f"Send this part to {lab_}", lambda _=False, a=app_: self.push_to_solidworks(app=a))
        sw.addSeparator()
        act(sw, "Install Fusion add-in…", self.install_fusion_addin)
        act(sw, "Install Rhino link…", lambda: __import__("daedalus.sw.ui", fromlist=["install_rhino"])
            .install_rhino(self))
        demo = sw.addMenu("Demo sessions")
        for app_, (lab_, has_demo) in APPS.items():
            if has_demo:
                act(demo, f"{lab_} demo session", lambda _=False, a=app_: self.solidworks_link(demo=True, app=a))

        h = mb.addMenu("&Help")
        act(h, "Setup check (Python + Icarus)…", self.show_setup_check)
        act(h, "Design data calculators…", lambda: self.design_data(0))
        act(h, "Reference library (your handbooks)…", lambda: self.design_data(1))
        act(h, "Splash screen…", lambda: __import__("daedalus.ui.splash", fromlist=["editor"]).editor(self).exec())
        act(h, "About", lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME}</b> {__version__}<br>Parametric CAD on OpenCASCADE "
                            f"with Icarus, an AI modelling copilot (local Ollama models or Claude)."))

    # ----------------------------------------------------------- doc io
    def _doc_changed(self):
        # coalesce bursts (e.g. copilot tool calls) into one redraw
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._redraw)

    def _redraw(self):
        with self.view.batch():                 # one render for the whole redraw
            self._redraw_inner()

    def _redraw_inner(self):
        self._pending = False
        r = self.doc.result
        consumed = {ref for f in self.doc.features for ref in f.references()}
        sel = self.history.current()
        hidden = consumed - {sel}
        if self._sketching:
            hidden.add(self._sketching[0])
        invisible = {l.name for l in self.doc.layers if not l.visible}
        hidden |= {f.name for f in self.doc.features if f.layer in invisible}
        style = {l.name: (hex_to_rgb(l.color), l.visible) for l in self.doc.layers}
        poses = self.doc.display_poses()
        objs = [(o.name, poses.get(o.name, o.pose), o.visible, hex_to_rgb(o.color)) for o in self.doc.objects]
        self.view.show_result(r, hidden_sketches=hidden, layer_style=style, objects=objs)
        self.history.refresh()
        self.layers.refresh()
        self.objects_panel.refresh()
        self.mates_panel.refresh()
        self.physics_panel.refresh()
        self.studies_panel.refresh()
        self.cam_panel.refresh()
        self._sync_layer_box()
        self._update_empty_hint()
        res = r.assembly
        self.status_asm.setText(f"{len(self.doc.objects)} objects · {len(self.doc.mates)} mates · "
                                f"{res.dof} DOF" if res else f"{len(self.doc.objects)} object"
                                + ("s" if len(self.doc.objects) != 1 else ""))
        self.inspector.sync()
        us = self.doc.unit_system()
        if getattr(self, "_units_sig", None) != repr(self.doc.units):
            self._units_sig = repr(self.doc.units)
            self.units_combo.show_system(us)
            if getattr(self, "measure_panel", None) is not None:
                self.measure_panel.sync_units()
        err = f" · {len(r.errors)} error(s)" if r.errors else ""
        self.status_rebuild.setText(f"Rebuild {r.ms:.0f} ms{err} · mm")
        self.status_mat.setText(self.doc.material)
        self.material_box.setText(self.doc.material)
        self._update_title()

    def _sync_layer_box(self):
        names = [l.name for l in self.doc.layers]
        self.layer_box.blockSignals(True)
        if [self.layer_box.itemText(i) for i in range(self.layer_box.count())] != names:
            self.layer_box.clear()
            from .layers import swatch
            for l in self.doc.layers:
                self.layer_box.addItem(swatch(l.color, 10), l.name)
        self.layer_box.setCurrentText(self.doc.active_layer)
        self.layer_box.blockSignals(False)

    def _pick_active_layer(self, _i):
        name = self.layer_box.currentText()
        if name and name != self.doc.active_layer:
            self.doc.active_layer = name
            self.doc._emit()

    def _move_selected_to_layer(self, layer):
        names = self.history.selected_names()
        if not names:
            return self._need("Select features in History first (Ctrl/Shift-click for several).")
        try:
            self.doc.move_to_layer(names, layer)
        except FeatureError as e:
            QMessageBox.warning(self, "Layers", str(e))

    def _update_title(self):
        name = self.doc.path.name if self.doc.path else "Untitled"
        self.setWindowTitle(f"{'• ' if self.doc.dirty else ''}{name} — {APP_NAME}")

    def pick_material(self, obj=None):
        from .materials_ui import MaterialDialog
        MaterialDialog(self.doc, obj, self).exec()

    def _swap_doc(self, doc: Document):
        # keep the same object so panels stay wired; copy state in
        self.doc._load_dict(doc.to_dict())
        self.doc.path, self.doc.dirty = doc.path, False
        self.doc._undo.clear()
        self.doc._redo.clear()
        self.view._has_framed = False
        self.cam_panel.clear()                 # toolpaths and analysis colours belong to the old document
        self.clear_check()
        self.doc.rebuild()
        self.cam_panel.refresh()
        self.cam_panel.info.clear()

    def _update_empty_hint(self):
        """No start card over the 3D view — the Home screen covers getting started."""
        self.view.set_hint(None)

    def _confirm_discard(self) -> bool:
        if not self.doc.dirty:
            return True
        r = QMessageBox.question(self, APP_NAME, "Save changes first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_doc()
        return r == QMessageBox.Discard

    def new_doc(self):
        if self._confirm_discard():
            self._capture_thumb()
            self._swap_doc(Document())
            self._recent_key = None
            self.autosave.discard()
            self._update_title()
            self._leave_home()

    def open_doc(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", self._dir(), f"Daedalus part (*{FILE_EXT} *.strata);;" + self.IMPORT_FILTER.split(";;")[0])
        if path:
            self.open_path(path, confirmed=True)

    def open_path(self, path, confirmed=False, show_notes=True):
        """A .daedalus file opens; any other supported file starts a new document and imports it."""
        if not confirmed and not self._confirm_discard():
            return
        if path and not Path(path).exists():
            QMessageBox.warning(self, "Open", f"File not found:\n{path}")
            return
        self._capture_thumb()
        self._leave_home()
        self.autosave.discard()
        if str(path).lower().endswith((".dwg", ".svg", ".plt", ".hpgl")):
            self._swap_doc(Document())
            self.open_2d(str(path))
            return
        if not is_native(path):
            self._swap_doc(Document())
            self.doc.path = None
            self.doc._undo.clear()
            if self.import_path(path, show_notes=show_notes) is not None:
                self.recent.add(path)
                self._recent_key = str(path)
                QTimer.singleShot(1200, self._capture_thumb)
            self.doc.dirty = True
            self._update_title()
            return
        if path:
            try:
                d = Document.open(path)
                self._swap_doc(d)
                self._remember_dir(str(path))
                try:
                    got = self.doc.update_links()
                    if got:
                        self.statusBar().showMessage("Updated linked parts: " + ", ".join(got), 8000)
                except Exception:  # noqa: BLE001
                    pass
                self.doc.path = Path(path)
                self._remember_dir(path)
                self._recent_key = str(path)
                self._update_title()
                QTimer.singleShot(1200, self._capture_thumb)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Open", str(e))

    def save_doc(self, as_=False) -> bool:
        path = self.doc.path
        if as_ or path is None:
            p, _ = QFileDialog.getSaveFileName(self, "Save", self._dir(), f"Daedalus part (*{FILE_EXT})")
            if not p:
                return False
            path = Path(p if p.endswith(FILE_EXT) else p + FILE_EXT)
        self.doc.save(path)
        self._remember_dir(str(path))
        self._recent_key = str(path)
        self.autosave.discard()
        self._capture_thumb()
        self._sync_std_part(path)
        if getattr(self.doc, "exchange_files", None):
            self.statusBar().showMessage("Saved + " + ", ".join(Path(p).name for p in self.doc.exchange_files), 6000)
        self._update_title()
        return True

    # ------------------------------------------------------- import / export
    IMPORT_FILTER = ("CAD and mesh files (*.step *.stp *.iges *.igs *.brep *.stl *.obj *.3mf *.dxf "
                     "*.sldprt *.sldasm);;STEP (*.step *.stp);;IGES (*.iges *.igs);;BREP (*.brep);;"
                     "Meshes (*.stl *.obj *.3mf);;DXF 2D (*.dxf);;SolidWorks (*.sldprt *.sldasm);;All files (*)")
    EXPORT_FILTERS = [("STEP assembly — recommended (*.step)", ".step"), ("IGES (*.iges)", ".iges"),
                      ("BREP (*.brep)", ".brep"),
                      ("STL mesh (*.stl)", ".stl"), ("OBJ mesh (*.obj)", ".obj"), ("3MF for printing (*.3mf)", ".3mf"),
                      ("PLY mesh with colours (*.ply)", ".ply"), ("VRML (*.wrl)", ".wrl"),
                      ("glTF binary (*.glb)", ".glb"), ("glTF (*.gltf)", ".gltf"),
                      ("DXF of the selected sketch (*.dxf)", ".dxf")]

    def import_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import", self._dir(), self.IMPORT_FILTER)
        for p in paths:
            self.import_path(p)

    def set_display_quality(self, q):
        from . import tessellate as _tess
        _tess.set_quality(q)
        self.settings.setValue("view/quality", q)
        for k, a in self._quality_actions.items():
            a.setChecked(k == q)
        for oa in self.view.objs.values():
            oa._key = None                     # re-mesh at the new quality
        self._redraw()
        self.statusBar().showMessage(f"Display quality: {q}", 4000)

    def _on_loading(self, done, total):
        """Progressive display of big models: progress in the status bar."""
        if done >= total:
            QTimer.singleShot(300, self._capture_thumb)
            self.load_bar.hide()
            self.statusBar().showMessage(f"Displayed {total} parts", 3000)
            return
        self.load_bar.setMaximum(total)
        self.load_bar.setValue(done)
        self.load_bar.show()
        self.statusBar().showMessage(f"Displaying parts… {done}/{total}  (you can work meanwhile)")

    def _import_file(self, path):
        """Small files: read here. Big STEP/IGES/BREP/meshes: read in a worker process with a progress dialog
        (OCCT blocks Python while reading, which would freeze the window). Returns the summary, or None if
        cancelled."""
        from ..io import import_worker as W
        if not W.wants_background(path):
            return self.doc.import_file(path)
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QProgressDialog
        QApplication.restoreOverrideCursor()
        size = Path(path).stat().st_size / 1e6
        dlg = QProgressDialog(f"Reading {Path(path).name} ({size:.0f} MB)…", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Import")
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(420)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        job = W.ImportJob(path)
        job.start()
        loop = QEventLoop()
        out = {}

        def tick():
            if dlg.wasCanceled():
                job.cancel()
                out["cancel"] = True
                loop.quit()
                return
            r = job.poll()
            el = job.elapsed()
            if r is None or r[0] == "progress":
                stage, i, n = (r[1], r[2], r[3]) if r else (job.stage, 0, 0)
                if n:
                    dlg.setMaximum(n)
                    dlg.setValue(i)
                else:
                    dlg.setMaximum(0)
                dlg.setLabelText(f"{Path(path).name} ({size:.0f} MB)\n{stage}…  {el:.0f} s")
                return
            out[r[0]] = r[1]
            loop.quit()
        timer = QTimer(self, interval=100)
        timer.timeout.connect(tick)
        timer.start()
        dlg.show()
        loop.exec()
        timer.stop()
        if "cancel" in out:
            dlg.close()
            return None
        if "error" in out:
            dlg.close()
            raise FeatureError(out["error"])
        dlg.setMaximum(0)
        dlg.setLabelText(f"{Path(path).name}\nAdding {len(out['done'].parts)} parts…")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return self.doc.import_result(out["done"], path)
        finally:
            dlg.close()

    def import_path(self, path, quiet=False, show_notes=True) -> dict | None:
        path = str(path)
        self._remember_dir(path)
        if self._sketching:
            if not quiet:
                QMessageBox.information(self, "Import", "Finish or cancel the sketch first.")
            return None
        msg = "Converting with SolidWorks…" if path.lower().endswith((".sldprt", ".sldasm")) else "Importing…"
        self.statusBar().showMessage(f"{msg} {Path(path).name}")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            s = self._import_file(path)
            if s is None:                                      # cancelled
                QApplication.restoreOverrideCursor()
                self.statusBar().showMessage(f"Import of {Path(path).name} cancelled", 5000)
                return None
        except FeatureError as e:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
            if not quiet:
                QMessageBox.warning(self, "Import", str(e))
            return None
        except Exception as e:  # noqa: BLE001 — kernel/reader errors
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
            if not quiet:
                QMessageBox.warning(self, "Import", f"Couldn't import {Path(path).name}:\n{e}")
            return None
        QApplication.restoreOverrideCursor()
        self.view._has_framed = False
        self.doc.rebuild()
        bits = []
        if s["objects"]:
            bits.append(f"{len(s['objects'])} object" + ("s" if len(s["objects"]) != 1 else ""))
        if s["kind"] == "dxf":
            bits.append(f"sketch {s['features'][0]}")
        if s["mates"]:
            bits.append(f"{len(s['mates'])} mates")
        self.statusBar().showMessage(f"Imported {Path(path).name}: " + ", ".join(bits), 8000)
        if s["notes"] and not quiet and show_notes:
            QMessageBox.information(self, "Import", f"Imported {Path(path).name}: " + ", ".join(bits)
                                    + "\n\n" + "\n".join("• " + n for n in s["notes"]))
        if not quiet and s["kind"] != "dxf" and s["objects"] and \
                self.settings.value("import/ask_up", True, type=bool):
            feats = set(s.get("features") or [])
            fresh = [n for n in s["objects"] if self.doc.has_obj(n) and
                     all(f.name in feats for f in self.doc.features if f.obj == n)]
            if fresh:
                QTimer.singleShot(0, lambda: self.ask_up_axis(fresh, path))
        return s

    def ask_up_axis(self, names=None, path=None):
        """'Which way is up?' — for just-imported parts, or the selection / every part (Edit menu)."""
        from .up_axis import UpAxisDialog, source_program
        names = [n for n in (names or self.view.selected or [o.name for o in self.doc.objects])
                 if self.doc.has_obj(n) and self.doc.result.bodies.get(n) is not None]
        if not names:
            return
        prog = source_program(path) if path else ""
        dlg = UpAxisDialog(self.doc, self.view, names, prog, self.settings, self)
        if dlg.exec():
            try:
                if dlg.apply() is not None:
                    self.view.fit()
                    self.statusBar().showMessage(f"{dlg.up()} is now up" + (" · on the floor" if
                                                 dlg.floor.isChecked() else ""), 5000)
            except FeatureError as e:
                QMessageBox.warning(self, "Which way is up", str(e))

    def export(self):
        filt = ";;".join(f for f, _ in self.EXPORT_FILTERS)
        base = str(Path(self._dir()) / ((self.doc.path.stem if self.doc.path else "model")))
        p, chosen = QFileDialog.getSaveFileName(self, "Export", base, filt)
        if not p:
            return
        ext = dict(self.EXPORT_FILTERS).get(chosen, ".step")
        if not Path(p).suffix:
            p += ext
        if p.lower().endswith(".dxf"):
            return self.export_sketch_dxf(None, p)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.doc.export(p)
            self._remember_dir(p)
            self.statusBar().showMessage(f"Exported {p}", 6000)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Export", str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def library(self):
        from .production_ui import LibraryDialog
        d = LibraryDialog(self.doc, getattr(self, "_last_pick", None), self)
        if d.exec():
            try:
                r = d.insert()
            except FeatureError as e:
                return QMessageBox.warning(self, "Library", str(e))
            self.statusBar().showMessage(f"Inserted {', '.join(r['objects'])}"
                                         + (f" · {len(r['mates'])} mates" if r["mates"] else ""), 8000)

    def bom(self):
        from .production_ui import BomDialog
        if not self.doc.result.bodies:
            return self._need("Nothing modelled yet.")
        BomDialog(self.doc, self, on_send=self.send_bom_to_inventory).exec()

    # ------------------------------------------------------ standard parts
    def show_std_library(self):
        from .stdlib_ui import StandardLibraryDialog
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        pick = getattr(self, "_last_pick", None)
        at = list(pick["point"]) if pick and pick.get("point") is not None else None
        d = StandardLibraryDialog(self.doc, self, at=at)
        d.inserted.connect(lambda n: (self.objects_panel.refresh(), self.history.refresh(),
                                      self.statusBar().showMessage(f"Inserted standard part {n}", 6000)))
        d.open_requested.connect(lambda p: self.open_path(p))
        d.open_inventory.connect(self._open_inventory_target)
        d.exec()

    def _open_inventory_target(self, key):
        """'project:<id>' → Projects tab with it expanded; else an inventory part id → its editor."""
        self.show_inventory()
        page = self.inventory_page
        if key.startswith("project:"):
            page.collapsed.discard(key[8:])
            page.goto("projects")
        else:
            page.goto("parts")
            page.edit_part(key)

    def insert_std_from_inventory(self, std_id):
        from ..stdparts import LibraryError, get_library
        try:
            n = get_library().insert(self.doc, std_id, link=True)
        except LibraryError as e:
            return QMessageBox.warning(self, "Standard part", str(e))
        self.stack.setCurrentIndex(0)
        self.objects_panel.refresh()
        self.history.refresh()
        self.statusBar().showMessage(f"Inserted standard part {n} — mate it with SmartMates (Alt-drag)", 8000)

    def save_std_part(self):
        from ..stdparts import get_library
        from .stdlib_ui import SaveStandardDialog
        if not self.doc.result.bodies:
            return self._need("Model or import something first.")
        lib = get_library()
        d = SaveStandardDialog(lib, self.doc, self.doc.active_object, parent=self)

        def use(pid, obj):
            from .stdlib_ui import swap_for_library_part
            n = swap_for_library_part(lib, self.doc, pid, obj, self)
            if n:
                self.objects_panel.refresh()
                self.history.refresh()
                self.statusBar().showMessage(f"Using standard part {n} instead of a duplicate", 8000)
        d.use_existing.connect(use)
        if d.exec() and d.result_info:
            i = d.result_info
            self.statusBar().showMessage(f"Saved {i['name']} to the standard library (v{i.get('version', 1)})", 8000)

    def _sync_std_part(self, path):
        """Saving a part opened from the standard library refreshes its preview, size and version; saving any
        project records which standard parts it uses (library ▸ Used in)."""
        from ..stdparts import get_library
        from ..stdparts.usage import record_doc_usage
        try:
            record_doc_usage(get_library(), self.doc, path)
        except Exception:  # noqa: BLE001
            pass
        try:
            lib = get_library()
            p = Path(path).resolve()
            if p.name == "part.daedalus" and p.parent.parent == lib.root.resolve():
                info = lib.refresh_from_file(p.parent.name)
                self.statusBar().showMessage(f"Standard part {info['name']} updated to v{info['version']} — "
                                             "linked projects pick it up on open / Links", 8000)
        except Exception:  # noqa: BLE001
            pass

    def switch_theme(self, mode):
        """Save the theme and offer to restart (colours are read at start-up)."""
        import sys

        from PySide6.QtCore import QProcess

        from . import theme
        if mode == theme.MODE:
            return
        theme.set_mode(mode)
        r = QMessageBox.question(self, "Theme", f"{theme.THEMES[mode]} applies after a restart.\n\nRestart Daedalus now?",
                                 QMessageBox.Yes | QMessageBox.No)
        if r != QMessageBox.Yes or not self._confirm_discard():
            self.statusBar().showMessage(f"{theme.THEMES[mode]} will be used next time Daedalus starts", 8000)
            return
        argv = list(getattr(sys, "orig_argv", [sys.executable, "-m", "daedalus"]))
        if self.doc.path:
            argv = [a for a in argv if not str(a).lower().endswith((".daedalus", ".strata"))] + [str(self.doc.path)]
        self.doc.dirty = False
        QProcess.startDetached(argv[0], argv[1:])
        QApplication.quit()

    def show_inventory(self):
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        self.inventory_page.store.check_external()
        self.inventory_page.refresh()
        self.stack.setCurrentIndex(4)

    def auto_cam(self):
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        if not any(self.doc.result.bodies.get(o.name) is not None for o in self.doc.objects):
            return self._need("Model or import a part first.")
        from .autocam_ui import AutoCamDialog
        d = AutoCamDialog(self.doc, self.settings, self)
        d.backplot_requested.connect(lambda p: (d.accept(), self.simulate_gcode(p)))
        d.quote_requested.connect(self.show_quote)
        d.exec()
        if self.doc.cam and self.doc.cam.get("ops"):
            self.cam_panel.refresh()
            if getattr(self.doc, "last_cam", None) and self.doc.cam.get("orient", "+Z") == "+Z":
                self.cam_panel.show_paths(self.doc.last_cam)

    def show_quote(self):
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        self.asm.set_mode(None) if self.asm.mode else None
        self.stack.setCurrentIndex(self.QUOTE)

    def simulate_gcode(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Simulate G-code", self._dir(),
                                                  "G-code (*.nc *.tap *.ngc *.gcode *.cnc *.txt *.mpf *.eia);;"
                                                  "All files (*)")
        if not path:
            return
        self.stack.setCurrentIndex(0)
        cam = getattr(self.doc, "last_cam", None)
        offset = tuple(cam["origin"]) if cam and cam.get("origin") and \
            cam.get("setup", {}).get("orient", "+Z") == "+Z" else None
        try:
            self.gcode_bar.load(path, offset)
            if offset:
                self.statusBar().showMessage("Program zero placed at this model's CAM work offset (G54)", 6000)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Simulate G-code", str(e))

    def send_bom_to_inventory(self):
        from ..inventory.cad import send_bom
        from .inventory_dialogs import SendBomDialog
        if not self.doc.result.bodies:
            return self._need("Nothing modelled yet.")
        page = self.inventory_page
        page.store.check_external()
        name = self.doc.path.stem if getattr(self.doc, "path", None) else "Daedalus design"
        d = SendBomDialog(page.inv, name, self)
        if not d.exec():
            return
        before = page.inv.dump_values()
        try:
            r = send_bom(page.inv, self.doc, d.name.currentText().strip(), d.units.value(), d.weld.isChecked())
        except ValueError as e:
            return QMessageBox.warning(self, "Inventory", str(e))

        def undo():
            page.inv.load_values(before)
            page.commit("Send BOM undone")
        page.commit(f"BOM → {r['project']}: {len(r['added'])} new parts, {len(r['linked'])} linked", undo)

    def insert_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Insert part file", self._dir(),
                                           f"Parts (*{FILE_EXT} *.strata *.step *.stp *.iges *.igs *.brep *.stl)")
        if not p:
            return
        link = QMessageBox.question(self, "Insert", "Link to the file (updates when it changes)?\n"
                                    "No = embed a copy.") == QMessageBox.Yes
        try:
            n = self.doc.insert_part_file(p, link=link)
        except FeatureError as e:
            return QMessageBox.warning(self, "Insert", str(e))
        self.statusBar().showMessage(f"Inserted {n}" + (" (linked)" if link else ""), 6000)

    def import_part(self):
        from .import_ui import ImportDialog
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        self.stack.setCurrentIndex(0)
        d = ImportDialog(self.doc, getattr(self, "_last_pick", None), self, self.settings)

        def done(r):
            self.objects_panel.refresh()
            self.history.refresh()
            names = ", ".join(r["objects"])
            how = f" · connected ({len(r['mates'])} mate{'s' if len(r['mates']) != 1 else ''})" if r["connected"] else ""
            link = " · linked" if any((self.doc.obj(n).source or {}).get("linked") for n in r["objects"][:1]) else ""
            self.statusBar().showMessage(f"Imported {names}{link}{how}" + (f" — {r['message']}" if r.get("message")
                                                                         else ""), 10000)
        d.imported.connect(done)
        d.exec()

    def update_links(self):
        try:
            got = self.doc.update_links()
        except FeatureError as e:
            return QMessageBox.warning(self, "Links", str(e))
        self.statusBar().showMessage(("Updated " + ", ".join(got)) if got else "Linked parts are up to date", 6000)

    def toggle_explode(self):
        if self.doc.exploded:
            self.doc.exploded = False
            self.doc._emit()
        elif self.doc.explode:
            self.doc.exploded = True
            self.doc._emit()
        else:
            self.doc.auto_explode()

    def design_study(self):
        from .design_study_ui import DesignStudyDialog
        if not self.doc.features:
            return self._need("Model something first.")
        DesignStudyDialog(self.doc, self).exec()

    def edge_flange(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout
        sms = [f for f in self.doc.features if f.TYPE == "sheet_metal"]
        if not sms:
            return self._need("Make a sheet-metal base flange first (Sheet metal).")
        pick = getattr(self, "_last_pick", None)
        if not pick:
            return self._need("Click the thin side face of the edge to flange, then Edge flange.")
        cands = [f for f in sms if f.obj == pick.get("object")] or sms
        feat = cands[-1]
        if feat.params.get("state") == "flat":
            return self._need("Fold the part first (Fold/Unfold).")
        d = QDialog(self)
        d.setWindowTitle(f"Edge flange · {feat.name}")
        fm = QFormLayout(d)

        def spin(v, lo, hi, suf):
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setDecimals(2)
            w.setValue(v)
            w.setSuffix(suf)
            return w
        L, A = spin(20, 0.1, 5000, " mm"), spin(90, 1, 180, "°")
        down = QCheckBox("Bend the other way")
        ref = QComboBox()
        ref.addItems(["outer", "inner", "tangent"])
        ref.setCurrentText(feat.params.get("length_ref", "outer"))
        pos = QComboBox()
        pos.addItems(["material_inside", "material_outside", "bend_outside", "bend_centerline"])
        pos.setCurrentText(feat.params.get("position", "material_inside"))
        so, eo = spin(0, 0, 5000, " mm"), spin(0, 0, 5000, " mm")
        hem = QComboBox()
        hem.addItems(["none", "closed", "open"])
        hl, hg = spin(0, 0, 500, " mm (0 = 4t)"), spin(0, 0, 50, " mm (open)")
        for lab, w in (("Length", L), ("Angle", A), ("", down), ("Length to", ref), ("Position", pos),
                       ("Start offset", so), ("End offset", eo), ("Hem", hem), ("Hem length", hl), ("Hem gap", hg)):
            fm.addRow(lab, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        fm.addRow(bb)
        if d.exec() != QDialog.Accepted:
            return
        opts = {"length_ref": ref.currentText(), "position": pos.currentText()}
        if so.value():
            opts["start_offset"] = so.value()
        if eo.value():
            opts["end_offset"] = eo.value()
        if hem.currentText() != "none":
            opts.update(hem=hem.currentText(), hem_length=hl.value(), hem_gap=hg.value())
        try:
            fl = self.doc.add_flange(feat.name, pick["point"], L.value(), A.value(), down.isChecked(), **opts)
        except FeatureError as e:
            return QMessageBox.warning(self, "Edge flange", str(e))
        self.statusBar().showMessage(f"Flange on edge {fl['edge']} of {feat.name}", 6000)

    def toggle_flat(self):
        sms = [f for f in self.doc.features if f.TYPE == "sheet_metal"]
        if not sms:
            return self._need("No sheet-metal parts.")
        state = "folded" if any(f.params.get("state") == "flat" for f in sms) else "flat"
        try:
            self.doc.set_sheet_metal_state(state)
        except FeatureError as e:
            return QMessageBox.warning(self, "Sheet metal", str(e))
        self.statusBar().showMessage("Flat pattern shown" if state == "flat" else "Folded", 5000)

    def show_cam(self):
        self._show_side(self.cam_panel)

    def check(self, kind):
        from PySide6.QtWidgets import QInputDialog
        if not self.doc.result.bodies:
            return self._need("Nothing modelled yet.")
        dirs = {"+Z (top)": (0, 0, 1), "-Z": (0, 0, -1), "+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0),
                "-Y": (0, -1, 0)}
        kw = {}
        if kind in ("draft", "overhang"):
            lab = "Pull direction:" if kind == "draft" else "Build direction (up):"
            d, ok = QInputDialog.getItem(self, kind.title(), lab, list(dirs), 0, False)
            if not ok:
                return
            kw["direction"] = dirs[d]
        if kind == "draft":
            v, ok = QInputDialog.getDouble(self, "Draft", "Minimum draft (°):", 1.0, 0.0, 15.0, 1)
            if not ok:
                return
            kw["min_draft"] = v
        elif kind == "thickness":
            v, ok = QInputDialog.getDouble(self, "Thickness", "Minimum wall (mm):", 1.0, 0.05, 100.0, 2)
            if not ok:
                return
            kw["min_wall"] = v
        else:
            v, ok = QInputDialog.getDouble(self, "Overhang", "Self-supporting angle from vertical (°):", 45.0, 10.0,
                                           80.0, 0)
            if not ok:
                return
            kw["max_overhang"] = v
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            s = self.doc.check_manufacturability(kind, **kw)
        except FeatureError as e:
            QApplication.restoreOverrideCursor()
            return QMessageBox.warning(self, "Check", str(e))
        QApplication.restoreOverrideCursor()
        r = self.doc.last_check
        ov = self.studies_panel.overlay
        ov.show_field(r["p"], r["tris"], r["values"], r["label"], r["unit"], rng=r["rng"], reverse=r["reverse"],
                      title=f"{r['object']} · {r['label']}", hide={r["object"]})
        self.view.render()
        self.statusBar().showMessage(f"{r['label']}: {s['verdict']}", 0)

    def clear_check(self):
        self.studies_panel.overlay.clear()
        self.cam_panel.clear()
        self.view.render()
        self.statusBar().clearMessage()

    def export_options(self):
        from .export_ui import ExportOptionsDialog
        d = ExportOptionsDialog(self.doc, self)
        if d.exec():
            self.doc.set_export_options(**d.values())
            self.statusBar().showMessage("Export options saved with the document", 5000)

    def show_drawings(self):
        if self._sketching:
            return self._need("Finish or cancel the sketch first.")
        self.stack.setCurrentIndex(3)

    def new_drawing(self):
        if not self.doc.result.bodies:
            return self._need("Model something first — drawings are made from the 3D model.")
        self.show_drawings()
        self.drawing_page.new_drawing()

    def open_2d(self, path=None):
        self.show_drawings()
        self.drawing_page.open_2d(path)

    def export_flat(self):
        flats = list(self.doc.result.flats)
        if not flats:
            return self._need("No sheet-metal feature yet.")
        name = flats[-1]
        cur = self.history.current()
        if cur in flats:
            name = cur
        path, _ = QFileDialog.getSaveFileName(self, f"Flat pattern of {name}",
                                              str(Path(self._dir()) / f"{name}_flat.dxf"), "DXF (*.dxf)")
        if path:
            try:
                info = self.doc.export_flat_dxf(name, path)
                self.statusBar().showMessage(f"Flat pattern {info['size'][0]:g} × {info['size'][1]:g} mm, "
                                             f"{info['bends']} bends → {path}", 8000)
            except FeatureError as e:
                QMessageBox.warning(self, "Flat pattern", str(e))

    def export_sketch_dxf(self, name=None, path=None):
        name = name or self.history.current()
        if not name or name not in {f.name for f in self.doc.features} or self.doc.get(name).TYPE != "sketch":
            sk = [f.name for f in self.doc.features if f.TYPE == "sketch"]
            if len(sk) != 1:
                QMessageBox.information(self, "Export DXF", "Select a 2D sketch in the history first.")
                return
            name = sk[0]
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, f"Export {name} as DXF",
                                                  str(Path(self._dir()) / f"{name}.dxf"), "DXF (*.dxf)")
            if not path:
                return
            if not path.lower().endswith(".dxf"):
                path += ".dxf"
        try:
            n = self.doc.export_sketch_dxf(name, path)
            self._remember_dir(path)
            self.statusBar().showMessage(f"Exported {name}: {n} curves → {path}", 6000)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Export DXF", str(e))

    # drag & drop files onto the window
    def dragEnterEvent(self, e):
        from ..io import IMPORT_EXT
        urls = e.mimeData().urls() if e.mimeData().hasUrls() else []
        if any(Path(u.toLocalFile()).suffix.lower() in IMPORT_EXT or is_native(u.toLocalFile())
               for u in urls):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if is_native(p):
                self.open_path(p)
            elif p.lower().endswith((".dwg", ".svg", ".plt", ".hpgl")):
                self.open_2d(p)
            elif p:
                self.import_path(p)
        e.acceptProposedAction()

    def new_study(self, t):
        if self._sketching:
            return
        self._show_side(self.studies_panel)
        self.studies_panel.new_study(t)

    def run_study(self):
        self._show_side(self.studies_panel)
        self.studies_panel.run()

    def simulate(self):
        if self._sketching:
            return
        self.asm.set_mode(None) if self.asm.mode else None
        self._show_side(self.physics_panel)
        self.physics_panel.run()

    def _dir(self):
        return self.settings.value("last_dir", str(Path.home()))

    def _remember_dir(self, path):
        self.settings.setValue("last_dir", str(Path(path).parent))
        if str(path).lower().endswith((".daedalus", ".strata")):
            self.recent.add(path)

    # -------------------------------------------------------- features
    def add_guarded(self, fn):
        try:
            return fn()
        except FeatureError as e:
            QMessageBox.warning(self, "Can't add feature", str(e))

    def add(self, ftype: str, cut: bool = False):
        try:
            return self._add(ftype, cut)
        except FeatureError as e:
            QMessageBox.warning(self, "Can't add feature", str(e))

    def _add(self, ftype: str, cut: bool = False):
        d = self.doc
        params: dict = {}
        from . import v28_ui
        if ftype in v28_ui.NEW_FEATURES:
            params = v28_ui.default_params(self, ftype)
            if params is None:
                return
            f = d.add(ftype, params)
            self.history.refresh(select=f.name)
            self.inspector.show_feature(f.name)
            if f.name in d.result.errors:
                self.statusBar().showMessage(f"{f.name}: {d.result.errors[f.name]}", 10000)
            return f
        if ftype == "sketch":
            return self.new_sketch()
        elif ftype in ("extrude", "revolve"):
            sk = d.last_of("sketch")
            if not sk:
                return self._need("Create a sketch first.")
            params = {"sketch": sk}
            if ftype == "extrude":
                params.update({"depth": 20.0})
                if cut:
                    params.update({"operation": "cut", "end": "through_all"})
            else:
                params.update({"axis": "Z"})
        elif ftype == "sketch3d":
            f = d.add("sketch3d", {})
            self.history.refresh(select=f.name)
            self.edit_sketch(f.name, new=True)
            return
        elif ftype == "sweep":
            prof = d.last_of("sketch")
            path = d.last_of("sketch3d") or next((x.name for x in reversed(d.features)
                                                   if x.TYPE == "sketch" and x.name != prof), None)
            if not prof or not path:
                return self._need("Sweep needs a profile sketch and a path (3D sketch or another sketch).")
            params = {"profile": prof, "path": path}
        elif ftype == "frame":
            path = d.last_of("sketch3d") or d.last_of("sketch")
            if not path:
                return self._need("Draw a 3D sketch for the frame layout first.")
            params = {"path": path}
        elif ftype in ("fillet", "chamfer", "shell") and self.view.entities:
            from ..core.selectors import at_expr
            kind = "face" if ftype == "shell" else "edge"
            name, pts = self._picked(kind)
            if pts:
                if name != d.active_object and d.has_obj(name):
                    d.active_object = name
                    self.objects_panel.refresh()
                if ftype == "shell":
                    params = {"open_faces": ",".join(at_expr([p]) for p in pts)}
                else:
                    params = {"edges": at_expr(pts)}
                    others = sum(1 for e in self.view.entities if e["kind"] == "edge" and e["object"] != name)
                    if others:
                        self.statusBar().showMessage(f"{ftype.title()} applies to {name}; {others} edge(s) on "
                                                     "other parts skipped", 8000)
        elif ftype == "hole":
            params = {"face": self._picked_side() or "top"}
        elif ftype in ("plane", "axis", "thread"):
            info = getattr(self, "_last_pick", None)
            local = list(info["local"]) if info and info.get("object") == d.active_object else None
            if ftype == "plane":
                params = ({"mode": "face", "point": local, "offset": 0.0} if local and info["type"] == "plane"
                          else {"mode": "offset", "base": "XY", "offset": 10.0})
            elif ftype == "axis":
                params = ({"mode": "cylinder", "point": local} if local and info["type"] in ("cylinder", "cone")
                          else {"mode": "points"})
            else:
                if not (local and info["type"] == "cylinder"):
                    return self._need("Click a hole or shaft face first, then Thread.")
                params = {"point": local, "size": "M8"}
        elif ftype == "loft":
            sks = [f.name for f in d.features if f.TYPE == "sketch" and f.obj == d.active_object][-2:]
            if len(sks) < 2:
                return self._need("Loft needs two sketches (profiles on different planes).")
            params = {"sections": ", ".join(sks)}
        elif ftype in ("rib", "sheet_metal"):
            sk = d.last_of("sketch")
            if not sk:
                return self._need("Create a sketch first.")
            params = {"sketch": sk}
            if ftype == "sheet_metal":
                params["flanges"] = [{"edge": "+X", "length": 20}, {"edge": "-X", "length": 20}]
        elif ftype == "gusset":
            path = d.last_of("sketch3d") or d.last_of("sketch")
            if not path:
                return self._need("Gussets go on a frame layout sketch.")
            params = {"path": path}
        elif ftype in ("linear_pattern", "circular_pattern"):
            src = next((f.name for f in reversed(d.features) if f.TYPE in ("hole", "extrude", "revolve")), None)
            if not src:
                return self._need("Nothing to pattern yet.")
            params = {"feature": src}
        try:
            f = d.add(ftype, params)
        except FeatureError as e:
            QMessageBox.warning(self, "Can't add feature", str(e))
            return
        self.history.refresh(select=f.name)
        self.inspector.show_feature(f.name)

    def new_sketch(self, plane=None, offset=0.0, ask=None):
        """Start a sketch. With no plane given, ask which one (origin planes, the part's flat sides, reference
        planes — previewed in the view); the clicked face is the default."""
        picked = self._picked_side()
        if ask is None:
            ask = plane is None and self.settings.value("sketch/ask_plane", True, type=bool)
        if plane is None and ask:
            from .plane_picker import PlanePicker
            default = picked or self.settings.value("sketch/last_plane", "XY")
            dlg = PlanePicker(self.doc, self.view, default=default, settings=self.settings, parent=self)
            if not dlg.exec():
                return None
            plane, offset = dlg.choice()
        plane = plane or picked or "XY"
        params = {"plane": plane}
        if offset:
            params["offset"] = float(offset)
        f = self.doc.add("sketch", params)
        if plane in ("XY", "XZ", "YZ"):
            self.settings.setValue("sketch/last_plane", plane)
        self.history.refresh(select=f.name)
        self.edit_sketch(f.name, new=True)
        return f

    def _picked_side(self):
        return self._side_of(getattr(self, "_last_pick", None))

    def _side_of(self, info):
        """'top' / 'front' / … when `info` is a flat face of the active part square to an axis."""
        if not info or info.get("type") != "plane" or not info.get("normal"):
            return None
        if info.get("object") not in (None, self.doc.active_object):
            return None
        nx, ny, nz = info["normal"]
        axes = {"right": nx, "left": -nx, "back": ny, "front": -ny, "top": nz, "bottom": -nz}
        side, val = max(axes.items(), key=lambda kv: kv[1])
        return side if val > 0.999 else None

    # ------------------------------------------------------------- v2.8
    def _v28(self, fn, *a):
        from . import v28_ui
        return getattr(v28_ui, fn)(self, *a)

    def open_equations(self):
        from .v28_ui import ConfigDialog
        ConfigDialog(self.doc, self).exec()
        self.history.refresh()

    def config_menu_popup(self):
        from PySide6.QtGui import QCursor
        from . import v28_ui
        m = QMenu(self)
        v28_ui.config_menu(self, m)
        m.exec(QCursor.pos())

    def make_subassembly(self):
        self._v28("make_subassembly")

    def component_pattern_menu(self):
        from PySide6.QtGui import QCursor
        m = QMenu(self)
        m.addAction("Linear pattern…", lambda: self._v28("component_pattern", "linear"))
        m.addAction("Circular pattern…", lambda: self._v28("component_pattern", "circular"))
        m.addAction("Mirror components…", lambda: self._v28("component_pattern", "mirror"))
        m.addSeparator()
        m.addAction("Delete a pattern…", lambda: self._v28("delete_pattern"))
        m.addAction("Dissolve a subassembly…", lambda: self._v28("dissolve_subassembly"))
        m.addAction("Save a subassembly as a file…", lambda: self._v28("save_subassembly"))
        m.exec(QCursor.pos())

    def design_data(self, tab=0):
        from .design_ui import DesignDataDialog
        DesignDataDialog(self.doc, self, tab).exec()

    def miter_corners(self):
        sm = [f.name for f in self.doc.features if f.TYPE == "sheet_metal"]
        if not sm:
            return self._need("Make a sheet-metal part first.")
        f = self.doc.get(sm[-1])
        new = "open" if f.params.get("corners") == "miter" else "miter"
        self.doc.edit(f.name, {"corners": new})
        self.statusBar().showMessage(f"{f.name}: {new} corners", 5000)

    def _need(self, msg):
        self.statusBar().showMessage(msg, 4000)

    def _on_pick(self, info):
        if self.studies_panel.picking:
            self.studies_panel.on_pick(info)
            return
        self._last_pick = info
        if (info and self.settings.value("sketch/rapid", False, type=bool) and not self._sketching
                and self.stack.currentIndex() == 0):
            side = self._side_of(info)
            if side:                                  # Rapid Sketch: a click on a flat face opens a sketch there
                QTimer.singleShot(0, lambda s_=side: self.add_guarded(lambda: self.new_sketch(plane=s_)))
                return
        if not info:
            self.status_sel.setText("")
            return
        n = info["normal"]
        side = self._picked_side()
        tag = f" · {side} face — Sketch/Hole will use it" if side else ""
        if info.get("object") and info["object"] != self.doc.active_object and len(self.doc.objects) > 1:
            self.doc.active_object = info["object"]
            self.objects_panel.refresh()
        self.status_sel.setText(f"{info.get('object', '')} · {info['type']} face · {info['area']:.1f} mm² · "
                                f"n=({n[0]:.2f}, {n[1]:.2f}, {n[2]:.2f}){tag}")

    # ---------------------------------------------------------- sketch
    def edit_sketch(self, name: str, new: bool = False):
        if self._sketching:
            return
        f = self.doc.get(name)
        if f.TYPE == "sketch3d":
            return self._edit_sketch3d(name, new)
        self._sketching = (name, new)
        self.sketch_editor.load(name, f.params["plane"], f.params.get("geometry"),
                                self.doc.sketch_underlay(name))
        self.stack.setCurrentIndex(1)
        self.statusBar().showMessage("Sketching — Finish (Ctrl+Enter) to rebuild the part", 5000)

    def _finish_sketch(self, geom: dict):
        name, new = self._sketching
        self._sketching = None
        self.stack.setCurrentIndex(0)
        try:
            # a new sketch + its drawing = one undo step
            self.doc.edit(name, {"geometry": geom}, checkpoint=not new)
        except FeatureError as e:
            self._need(str(e))
        self.history.refresh(select=name)
        self.inspector.show_feature(name)

    def _cancel_sketch(self):
        name, new = self._sketching
        self._sketching = None
        self.stack.setCurrentIndex(0)
        if new:
            self.doc.undo()

    def _edit_sketch3d(self, name, new):
        self._sketching = (name, new)
        self._s3d_prev_mode = self.view.display_mode
        ctx = self.doc.context_before(name)
        self.toolbelt.hide()
        self.s3d_bar.title.setText(f"⟋  {name}")
        self.s3d_bar.show()
        self._redraw()
        self.view.apply_display_mode("Translucent")
        f3 = self.doc.get(name)
        self.s3d.activate(f3.params.get("geometry"), ctx.body, self.doc.obj(f3.obj).pose)
        self.s3d_bar.sync()
        if new:
            self.view.set_view("Isometric")
        self.view.vtk.setFocus()
        self.statusBar().showMessage("3D sketch — click to place points · Tab changes plane · X/Y/Z lock an axis · "
                                     "middle-drag rotates", 8000)

    def _end_sketch3d(self):
        self.s3d.deactivate()
        self.s3d_bar.hide()
        self.toolbelt.show()
        self.view.apply_display_mode(getattr(self, "_s3d_prev_mode", "Shaded + edges"))

    def _finish_sketch3d(self, geom):
        name, new = self._sketching
        self._sketching = None
        self._end_sketch3d()
        try:
            self.doc.edit(name, {"geometry": geom}, checkpoint=not new)
        except FeatureError as e:
            self._need(str(e))
        self.history.refresh(select=name)
        self.inspector.show_feature(name)

    def _cancel_sketch3d(self):
        name, new = self._sketching
        self._sketching = None
        self._end_sketch3d()
        if new:
            self.doc.undo()
        else:
            self._redraw()

    # ---------------------------------------------------------- doodle
    def open_doodle(self):
        if self._sketching:
            return
        self.stack.setCurrentIndex(2)
        self.doodle.canvas.setFocus()

    def _doodle_make(self, geom, plane, depth):
        if self.doc.layer(self.doc.active_layer).locked:
            QMessageBox.warning(self, "Doodle", f"Active layer '{self.doc.active_layer}' is locked.")
            return
        self.doc.checkpoint()
        f = self.doc.add("sketch", {"plane": plane, "geometry": geom}, checkpoint=False)
        if depth > 0:
            self.doc.add("extrude", {"sketch": f.name, "depth": depth}, checkpoint=False)
        self.stack.setCurrentIndex(0)
        self.view._has_framed = False
        self._redraw()
        self.view.set_view("Isometric")
        self.history.refresh(select=f.name)
        self.inspector.show_feature(f.name)
        st = self.doc.result.sketch_status.get(f.name, {})
        self.statusBar().showMessage(
            f"Doodle → {f.name}" + (f" + extrude {depth:g} mm" if depth > 0 else "") +
            (f" · {st.get('dof')} DOF left — double-click the sketch to dimension it" if st.get("dof") else ""), 8000)

    def _doodle_ask(self, prompt, png):
        self.stack.setCurrentIndex(0)
        self.copilot_dock.show()
        self.copilot.ask_with_image(prompt, png)

    def _renders_for_claude(self) -> list[bytes]:
        """Isometric + top renders of the current model, as PNG bytes."""
        import os
        import tempfile
        out = []
        cam = self.view.ren.GetActiveCamera()
        saved = (cam.GetPosition(), cam.GetFocalPoint(), cam.GetViewUp())
        with tempfile.TemporaryDirectory() as td:
            for name in ("Isometric", "Top"):
                self.view.set_view(name, animate=False)
                path = os.path.join(td, f"{name}.png")
                self.view.screenshot(path)
                with open(path, "rb") as f:
                    out.append(f.read())
        cam.SetPosition(*saved[0])
        cam.SetFocalPoint(*saved[1])
        cam.SetViewUp(*saved[2])
        self.view.render()
        return out

    # -------------------------------------------------------- assembly
    def new_object(self):
        try:
            o = self.doc.add_object()
        except FeatureError as e:
            return QMessageBox.warning(self, "Object", str(e))
        self._show_side(self.objects_panel)
        self.statusBar().showMessage(f"{o.name} is active — new sketches and features go into it.", 6000)

    def _asm_mode_changed(self, mode):
        self.toolbelt.setEnabled(True)
        if mode == "mate":
            self._show_side(self.mates_panel)
        if mode == "measure":
            self._measure_dock().show()
            self.measure_panel.clear()
        elif getattr(self, "_mdock", None) is not None:
            self._mdock.hide()

    def _measure_dock(self):
        if getattr(self, "_mdock", None) is None:
            from .measure_ui import MeasurePanel
            self.measure_panel = MeasurePanel(self.doc)
            self.measure_panel.system_picked.connect(lambda k: self.doc.set_units(name=k))
            self.measure_panel.units_requested.connect(self.units_dialog)
            self.measure_panel.cleared.connect(lambda: (self.asm._clear_measure(), self.view.render()))
            d = QDockWidget("Measure", self)
            d.setObjectName("measure")
            d.setWidget(self.measure_panel)
            d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                          QDockWidget.DockWidgetClosable)
            self.addDockWidget(Qt.RightDockWidgetArea, d)
            d.visibilityChanged.connect(lambda vis: (not vis and self.asm.mode == "measure" and
                                                     not self.isMinimized() and self.asm.set_mode(None)))
            self._mdock = d
        return self._mdock

    def units_dialog(self, tab=0):
        from .units_ui import UnitsDialog
        UnitsDialog(self.doc, self.settings, self, tab=tab if isinstance(tab, int) else 0).exec()

    def hss_builder(self):
        from .hss_ui import HSSBuilder
        sel = self.history.current()
        edit = next((f.name for f in self.doc.features if f.name == sel and f.TYPE in ("hss", "frame")), None)
        dlg = HSSBuilder(self.doc, self, edit=edit)
        if dlg.exec() and getattr(dlg, "created", None):
            self.history.refresh(select=dlg.created)
            self.inspector.show_feature(dlg.created)

    def drive(self, mate_name=""):
        if not self.doc.mates:
            return self._need("Add mates first (Assembly ▸ Mate).")
        dlg = DriveDialog(self.doc, self.view, mate_name, self)
        dlg.show()
        self._drive_dlg = dlg

    def interference(self):
        hits = self.doc.interference()
        if not hits:
            QMessageBox.information(self, "Interference", "No overlapping objects at the current position.")
        else:
            QMessageBox.warning(self, "Interference", "<br>".join(
                f"<b>{a}</b> × <b>{b}</b>: {v:g} mm³" for a, b, v in hits))

    def cut_list(self):
        rows = self.doc.cut_list()
        if not rows:
            return self._need("No Frame features yet.")
        from ..core import units as U
        body = "".join(f"<tr><td>{r['qty']}</td><td>{r['section']}</td><td align=right>{U.fmt(r['length'])}</td>"
                       f"<td align=right>{U.fmt(r['long_point'])}</td>"
                       f"<td align=right>{U.fmt(r['total_kg'], 'mass') if r.get('total_kg') else ''}</td></tr>"
                       for r in rows)
        total = sum(r["qty"] * r["length"] for r in rows)
        wt = sum(r.get("total_kg") or 0 for r in rows)
        QMessageBox.information(
            self, "Cut list",
            "<table cellspacing=6><tr><th>Qty</th><th>Section</th><th>Centerline</th><th>Long point</th>"
            "<th>Weight</th></tr>"
            f"{body}</table><p>Total centerline: {U.fmt(total)}" +
            (f" · total weight {U.fmt(wt, 'mass')}" if wt else "") + "</p>"
            f"<p style='color:{theme.MUTED}'>Long point includes mitre allowance at chain corners; "
            "members meeting at junctions overlap and are joined.</p>")

    def _undo(self):
        if self._sketching and self.doc.get(self._sketching[0]).TYPE == "sketch3d":
            self.s3d.undo()
        elif self._sketching:
            self.sketch_editor.canvas.undo()
        else:
            self.doc.undo()

    def _redo(self):
        if self._sketching and self.doc.get(self._sketching[0]).TYPE == "sketch3d":
            self.s3d.redo()
        elif self._sketching:
            self.sketch_editor.canvas.redo()
        else:
            self.doc.redo()

    def mass_props(self):
        mp = self.doc.mass_properties()
        if not mp:
            return self._need("No body.")
        from ..core import units as U
        q = {"volume_mm3": ("Volume", "volume"), "area_mm2": ("Surface area", "area"), "mass_kg": ("Mass", "mass")}

        def val(k, v):
            if k in q:
                return q[k][0], U.fmt(v, q[k][1])
            if k in ("bbox_mm", "bbox_min", "bbox_max", "center_of_mass") and isinstance(v, list):
                lab = {"bbox_mm": "Bounding box", "bbox_min": "Box min", "bbox_max": "Box max",
                       "center_of_mass": "Center of mass"}[k]
                return lab, " × ".join(U.fmt(x) for x in v) if k == "bbox_mm" else \
                    "(" + ", ".join(U.fmt(x) for x in v) + ")"
            if k == "objects" and isinstance(v, dict):
                return "Objects", "<br>".join(f"{n}: {U.fmt(o['mass_kg'], 'mass')} · {o['material']}"
                                              for n, o in v.items())
            return k.replace("_", " ").capitalize(), v
        rows = "".join(f"<tr><td style='padding-right:14px;color:{theme.MUTED}'>{a}</td><td>{b}</td></tr>"
                       for a, b in (val(k, v) for k, v in mp.items()))
        if "mass_kg" in mp:
            rows += (f"<tr><td style='padding-right:14px;color:{theme.MUTED}'>Weight</td>"
                     f"<td>{U.fmt(mp['mass_kg'] * 9.80665, 'force')}</td></tr>")
        QMessageBox.information(self, "Mass properties", f"<table>{rows}</table>")

    def closeEvent(self, ev):
        if self._confirm_discard():
            self._capture_thumb()
            self.autosave.discard()
            self.settings.setValue("ui/geometry", self.saveGeometry())
            self.settings.setValue("ui/dock_state", self.saveState())
            if self.doc.path:
                self.settings.setValue("home/last", str(self.doc.path))
            elif self._recent_key:
                self.settings.setValue("home/last", self._recent_key)
            ev.accept()
        else:
            ev.ignore()
