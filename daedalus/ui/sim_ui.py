"""Studies panel: FEA / fatigue / CFD studies — define, pick faces, run in the background, view results."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QSlider, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from ..sim.studies import OPTIONS, TYPES, summary_text
from .physics_ui import TracePlot
from .result_view import ResultOverlay
from . import theme

FIXTURE_HELP = {
    "fixed": "Fixed — face can't move at all",
    "roller": "Roller / slider — face slides in its plane, can't move off it (also: symmetry plane)",
    "pin": "Pin — hole/shaft face can rotate about its axis, nothing else",
    "elastic": "Elastic support — springs under the face (N/mm per mm²)",
}
LOAD_HELP = {
    "force": "Force — total N on the faces", "pressure": "Pressure — MPa, pushing into the faces",
    "torque": "Torque — N·m about the face's axis", "bearing": "Bearing load — shaft pushing on a hole (N)",
    "gravity": "Gravity — whole part, m/s²", "centrifugal": "Rotation — whole part spinning, rpm",
}
THERMAL_HELP = {
    "temperature": "Temperature — fixed °C", "convection": "Convection — h W/(m²·K) to ambient °C",
    "heat_flux": "Heat flux — W/m² into the faces", "heat_power": "Heat power — W into the faces",
    "radiation": "Radiation — emissivity to ambient °C", "heat_volume": "Heat generation — W in the whole part",
}
PARAMS = {  # itype -> [(param, label, default, unit)]
    "pin": [("slide", "Free to slide along axis", False, "")],
    "elastic": [("stiffness", "Stiffness", 10.0, "N/mm³"), ("normal_only", "Normal direction only", False, "")],
    "force": [("value", "Force", 100.0, "N"), ("direction", "Direction", "normal", "")],
    "pressure": [("value", "Pressure", 1.0, "MPa")],
    "torque": [("value", "Torque", 10.0, "N·m")],
    "bearing": [("value", "Load", 500.0, "N"), ("direction", "Direction", "-Z", "")],
    "gravity": [("value", "Acceleration", 9.81, "m/s²"), ("direction", "Direction", "-Z", "")],
    "centrifugal": [("value", "Speed", 3000.0, "rpm"), ("direction", "Axis", "+Z", "")],
    "temperature": [("value", "Temperature", 100.0, "°C")],
    "convection": [("h", "Coefficient h", 25.0, "W/(m²·K)"), ("ambient", "Ambient", 20.0, "°C")],
    "heat_flux": [("value", "Flux", 1000.0, "W/m²")],
    "heat_power": [("value", "Power", 10.0, "W")],
    "radiation": [("emissivity", "Emissivity", 0.9, ""), ("ambient", "Ambient", 20.0, "°C")],
    "heat_volume": [("value", "Power", 10.0, "W")],
}
DIRS = ["normal", "+X", "-X", "+Y", "-Y", "+Z", "-Z"]
DEFORM = {"Auto": None, "True scale": 1.0, "Off": 0.0, "×10": 10.0, "×100": 100.0, "×1000": 1000.0}


class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, doc, name):
        super().__init__()
        self.doc, self.name, self.stop = doc, name, False

    def run(self):
        try:
            self.done.emit(self.doc.run_study(self.name, progress=self.progress.emit, cancel=lambda: self.stop))
        except FeatureError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.failed.emit(f"{e.__class__.__name__}: {e}")


class StudiesPanel(QWidget):
    status = Signal(str)

    def __init__(self, doc, viewport, parent=None):
        super().__init__(parent)
        self.doc, self.vp = doc, viewport
        self.overlay = ResultOverlay(viewport)
        self.picking = None              # (kind, itype)
        self.picked: list[int] = []
        self.thread = self.worker = None
        self.result = None
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._animate)
        self._phase = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(5)

        r1 = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.currentTextChanged.connect(self._select)
        newb = QPushButton("+ Study")
        nm = QMenu(newb)
        for t, label in TYPES.items():
            nm.addAction(label, lambda tt=t: self.new_study(tt))
        newb.setMenu(nm)
        delb = QPushButton("Delete")
        delb.setToolTip("Delete study")
        delb.clicked.connect(self.delete_study)
        r1.addWidget(self.combo, 1)
        r1.addWidget(newb)
        r1.addWidget(delb)
        lay.addLayout(r1)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMaximumHeight(150)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemDoubleClicked.connect(self._edit_item)
        self.tree.currentItemChanged.connect(self._show_item_faces)
        lay.addWidget(self.tree)

        r2 = QHBoxLayout()
        self.fix_btn = self._menu_btn("+ Fixture", FIXTURE_HELP, "fixtures")
        self.load_btn = self._menu_btn("+ Load", LOAD_HELP, "loads")
        self.th_btn = self._menu_btn("+ Thermal", THERMAL_HELP, "thermal")
        opt = QPushButton("Options…")
        opt.clicked.connect(self.options)
        self.bolt_btn = QPushButton("+ Bolt")
        self.bolt_btn.setToolTip("Bolt connector: click a hole on one part, then the matching hole on the other")
        self.bolt_btn.clicked.connect(self.start_bolt)
        self.body_btn = QPushButton("Create body")
        self.body_btn.setToolTip("Turn the optimised shape into a new object")
        self.body_btn.clicked.connect(self.create_body)
        for b in (self.fix_btn, self.load_btn, self.th_btn, self.bolt_btn, self.body_btn, opt):
            r2.addWidget(b)
        lay.addLayout(r2)
        self.bolt_pick = None

        # face picking bar
        self.pick_box = QWidget()
        pl = QVBoxLayout(self.pick_box)
        pl.setContentsMargins(6, 6, 6, 6)
        self.pick_box.setStyleSheet(f"QWidget#pickbox{{border:1px solid {theme.ACCENT_LINE}; border-radius:4px;}}")
        self.pick_box.setObjectName("pickbox")
        self.pick_label = QLabel()
        self.pick_label.setWordWrap(True)
        pl.addWidget(self.pick_label)
        self.pick_form = QFormLayout()
        pl.addLayout(self.pick_form)
        pr = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self._commit_pick)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_pick)
        pr.addStretch(1)
        pr.addWidget(cancel)
        pr.addWidget(self.add_btn)
        pl.addLayout(pr)
        self.pick_box.hide()
        lay.addWidget(self.pick_box)

        r3 = QHBoxLayout()
        self.run_btn = QPushButton("▶ Run study")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QPushButton("■")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        r3.addWidget(self.run_btn, 1)
        r3.addWidget(self.stop_btn)
        lay.addLayout(r3)
        self.msg = QLabel("")
        self.msg.setObjectName("muted")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)

        # results
        self.res_box = QWidget()
        rl = QVBoxLayout(self.res_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        f1 = QFormLayout()
        self.field = QComboBox()
        self.field.currentTextChanged.connect(lambda *_: self.redraw())
        self.deform = QComboBox()
        self.deform.addItems(list(DEFORM))
        self.deform.currentTextChanged.connect(lambda *_: self.redraw())
        self.mode = QComboBox()
        self.mode.currentIndexChanged.connect(lambda *_: self.redraw())
        self.animate = QCheckBox("Animate")
        self.animate.toggled.connect(self._toggle_anim)
        mrow = QHBoxLayout()
        mrow.addWidget(self.mode, 1)
        mrow.addWidget(self.animate)
        self.time = QSlider(Qt.Horizontal)
        self.time.valueChanged.connect(lambda *_: self.redraw())
        self.axis = QComboBox()
        self.axis.addItems(["Z slice", "Y slice", "X slice"])
        self.axis.currentIndexChanged.connect(lambda *_: self.redraw())
        self.pos = QSlider(Qt.Horizontal)
        self.pos.setRange(0, 100)
        self.pos.setValue(50)
        self.pos.valueChanged.connect(lambda *_: self.redraw())
        self.stream = QCheckBox("Streamlines")
        self.stream.setChecked(True)
        self.stream.toggled.connect(lambda *_: self.redraw())
        srow = QHBoxLayout()
        srow.addWidget(self.axis)
        srow.addWidget(self.pos, 1)
        srow.addWidget(self.stream)
        def wrap(*ws):
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            for w_, stretch in ws:
                h.addWidget(w_, stretch)
            return box
        self.rows = {}
        for key, label, box in (("field", "Plot", wrap((self.field, 1))), ("deform", "Deformation", wrap((self.deform, 1))),
                                ("mode", "Mode", wrap((self.mode, 1), (self.animate, 0))),
                                ("time", "Time", wrap((self.time, 1))),
                                ("slice", "Slice", wrap((self.axis, 0), (self.pos, 1), (self.stream, 0)))):
            lab = QLabel(label)
            f1.addRow(lab, box)
            self.rows[key] = (lab, box)
        self.form = f1
        rl.addLayout(f1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(self.summary)
        crow = QHBoxLayout()
        self.curve = QComboBox()
        self.curve.currentTextChanged.connect(self._plot)
        hide = QPushButton("Show model")
        hide.clicked.connect(self.clear_results)
        crow.addWidget(self.curve, 1)
        crow.addWidget(hide)
        rl.addLayout(crow)
        self.plot = TracePlot()
        rl.addWidget(self.plot, 1)
        self.res_box.hide()
        lay.addWidget(self.res_box, 1)
        lay.addStretch(0)
        self.refresh()

    def _menu_btn(self, text, helps, kind):
        b = QPushButton(text)
        m = QMenu(b)
        for t, h in helps.items():
            m.addAction(h, lambda tt=t: self.start_pick(kind, tt))
        b.setMenu(m)
        return b

    # -------------------------------------------------------------- doc
    def current(self):
        n = self.combo.currentText()
        try:
            return self.doc.study(n) if n else None
        except FeatureError:
            return None

    def refresh(self):
        names = [s["name"] for s in self.doc.studies]
        cur = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(names)
        if cur in names:
            self.combo.setCurrentText(cur)
        self.combo.blockSignals(False)
        show = getattr(self.doc, "sim_show", None)
        if show and show in names and show in self.doc.sim_results:
            self.doc.sim_show = None                         # e.g. Claude ran it: show it
            self.combo.blockSignals(True)
            self.combo.setCurrentText(show)
            self.combo.blockSignals(False)
            self._fill()
            self.show_result(self.doc.sim_results[show])
            return
        self._fill()

    def _select(self, *_):
        self.cancel_pick()
        self._fill()
        st = self.current()
        if st and st["name"] in self.doc.sim_results:
            self.show_result(self.doc.sim_results[st["name"]])
        else:
            self.clear_results()

    def _fill(self):
        st = self.current()
        self.tree.clear()
        has = st is not None
        for w in (self.fix_btn, self.load_btn, self.th_btn, self.run_btn):
            w.setEnabled(has and self.thread is None)
        if not has:
            self.info.setText("No studies yet — + Study to add one (static, frequency, buckling, thermal, "
                              "fatigue, harmonic, flow).")
            return
        t = st["type"]
        self.fix_btn.setVisible(t in ("static", "frequency", "buckling", "harmonic", "assembly", "topology"))
        self.load_btn.setVisible(t in ("static", "buckling", "harmonic", "assembly", "topology"))
        self.bolt_btn.setVisible(t == "assembly")
        self.body_btn.setVisible(t == "topology" and st["name"] in self.doc.sim_results)
        self.th_btn.setVisible(t == "thermal")
        mat = self.doc.material_of(st["object"]).name
        mesh = f"{st['mesh_size']:g} mm" if st.get("mesh_size") else "auto"
        fresh = self.doc.study_result_fresh(st["name"])
        stale = st["name"] in self.doc.sim_results and not fresh
        if t == "flow":
            o = st["options"]
            self.info.setText(f"{TYPES[t]} · {o['mode']} · {o['fluid']} {o['velocity']:g} m/s {o['direction']}"
                              + (" · results out of date" if stale else ""))
        else:
            on = "the assembly" if t == "assembly" else st["object"]
            self.info.setText(f"{TYPES[t]} on {on} · {mat if t != 'assembly' else 'per-part materials'} · mesh {mesh}"
                              + (" · results out of date" if stale else ""))
        for kind, title in (("fixtures", "Fixtures"), ("loads", "Loads"), ("thermal", "Thermal"),
                            ("connectors", "Bolts")):
            items = st.get(kind) or []
            if kind == "connectors" and t != "assembly":
                continue
            if not items and kind != "connectors" and \
                    not (kind == "fixtures" and t in ("static", "buckling", "harmonic", "assembly", "topology")) and \
                    not (kind == "loads" and t in ("static", "buckling", "harmonic", "assembly", "topology")) and \
                    not (kind == "thermal" and t == "thermal"):
                continue
            top = QTreeWidgetItem([f"{title} ({len(items)})"])
            top.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(top)
            for i, it in enumerate(items):
                c = QTreeWidgetItem([self._describe(it)])
                c.setData(0, Qt.UserRole, (kind, i))
                top.addChild(c)
            top.setExpanded(True)
        opts = st.get("options") or {}
        if opts:
            top = QTreeWidgetItem(["Options"])
            top.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(top)
            for k, v in opts.items():
                if t == "thermal" and not opts.get("transient") and k in ("duration", "steps", "initial"):
                    continue
                c = QTreeWidgetItem([f"{k.replace('_', ' ')}: {v}"])
                c.setData(0, Qt.UserRole, ("option", k))
                top.addChild(c)

    @staticmethod
    def _describe(it):
        parts = [it.get("name", it["type"])]
        if it.get("type") == "bolt":
            return f"{it['name']} · {it['a_object']} ↔ {it['b_object']}" + (f" · preload {it['preload']:g} N"
                                                                          if it.get("preload") else "")
        if it.get("object"):
            parts.append(it["object"])
        for k in ("value", "h", "ambient", "emissivity", "stiffness"):
            if k in it:
                parts.append(f"{k}={it[k]:g}" if isinstance(it[k], (int, float)) else f"{k}={it[k]}")
        if "direction" in it:
            d = it["direction"]
            parts.append(d if isinstance(d, str) else "dir=(" + ", ".join(f"{x:g}" for x in d) + ")")
        if "refs" in it:
            parts.append(f"{len(it['refs'])} face" + ("s" if len(it["refs"]) != 1 else ""))
        return " · ".join(parts)

    # ---------------------------------------------------------- studies
    def new_study(self, t):
        obj = self.doc.active_object
        try:
            st = self.doc.add_study(t, obj=obj)
        except FeatureError as e:
            QMessageBox.warning(self, "Study", str(e))
            return
        self.refresh()
        self.combo.setCurrentText(st["name"])

    def start_bolt(self):
        self.bolt_pick = []
        self.status.emit("Bolt: click the hole on the first part")

    def create_body(self):
        st = self.current()
        if not st:
            return
        try:
            n = self.doc.topology_to_body(st["name"])
        except FeatureError as e:
            QMessageBox.warning(self, "Topology", str(e))
            return
        self.status.emit(f"Created {n} from the optimised shape")

    def delete_study(self):
        st = self.current()
        if st and QMessageBox.question(self, "Delete study", f"Delete {st['name']}?") == QMessageBox.Yes:
            self.doc.delete_study(st["name"])
            self.clear_results()
            self.refresh()

    def options(self):
        st = self.current()
        if not st:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{st['name']} options")
        form = QFormLayout(dlg)
        widgets = {}
        if st["type"] != "flow":
            ms = QDoubleSpinBox()
            ms.setRange(0, 1000)
            ms.setDecimals(2)
            ms.setSpecialValueText("auto")
            ms.setSuffix(" mm")
            ms.setValue(st.get("mesh_size") or 0)
            form.addRow("Element size", ms)
            widgets["mesh_size"] = ms
            ob = QComboBox()
            ob.addItems([o.name for o in self.doc.objects])
            ob.setCurrentText(st["object"])
            form.addRow("Object", ob)
            widgets["object"] = ob
        for k, (default, help_) in OPTIONS.get(st["type"], {}).items():
            v = st["options"].get(k, default)
            if isinstance(default, bool):
                w = QCheckBox()
                w.setChecked(bool(v))
            elif isinstance(default, (int, float)):
                w = QLineEdit(f"{v:g}")
            else:
                w = QComboBox()
                w.setEditable(True)
                choices = []
                if "|" in help_:
                    choices = [c.strip() for c in help_.split("|")]
                    choices[0] = choices[0].split()[-1]
                if k in ("source", "temperature_from"):
                    want = "static" if k == "source" else "thermal"
                    choices = [""] + [s["name"] for s in self.doc.studies if s["type"] == want]
                if k == "direction":
                    choices = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
                w.addItems(choices)
                w.setCurrentText(str(v))
            w.setToolTip(help_)
            form.addRow(k.replace("_", " "), w)
            widgets[k] = w
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        props = {}
        for k, w in widgets.items():
            if isinstance(w, QCheckBox):
                props[k] = w.isChecked()
            elif isinstance(w, QDoubleSpinBox):
                props[k] = w.value() or None
            elif isinstance(w, QComboBox):
                props[k] = w.currentText()
            else:
                props[k] = w.text()
        try:
            self.doc.set_study(st["name"], **props)
        except (FeatureError, ValueError) as e:
            QMessageBox.warning(self, "Options", str(e))
        self._fill()

    # ------------------------------------------------------------- items
    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        data = it.data(0, Qt.UserRole) if it else None
        if not data or data[0] == "option":
            return
        m = QMenu(self)
        m.addAction("Edit…", lambda: self._edit_item(it))
        m.addAction("Delete", lambda: self._remove(data))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _remove(self, data):
        st = self.current()
        self.doc.remove_study_item(st["name"], data[0], data[1])
        self.vp.clear_selection()
        self._fill()

    def _edit_item(self, it, *_):
        data = it.data(0, Qt.UserRole) if it else None
        st = self.current()
        if not data or not st:
            return
        if data[0] == "option":
            return self.options()
        item = st[data[0]][data[1]]
        params = PARAMS.get(item["type"], [])
        if not params:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(item.get("name", item["type"]))
        form = QFormLayout(dlg)
        ws = self._param_widgets(form, params, item)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            self.doc.checkpoint()
            item.update(self._param_values(ws))
            self.doc.dirty = True
            self.doc._emit()
            self._fill()

    def _show_item_faces(self, cur, *_):
        st = self.current()
        data = cur.data(0, Qt.UserRole) if cur else None
        if not st or not data or data[0] == "option" or self.picking:
            return
        item = st[data[0]][data[1]]
        if "refs" in item:
            body = self.doc.result.bodies.get(st["object"])
            from ..sim.studies import resolve_faces
            self.vp.highlight_faces(st["object"], resolve_faces(body, item["refs"]) if body is not None else [],
                                    color=(0.2, 0.55, 0.95) if data[0] == "fixtures" else (0.95, 0.45, 0.15))

    # ------------------------------------------------------------ picking
    def _param_widgets(self, form, params, values=None):
        ws = {}
        for key, label, default, unit in params:
            v = (values or {}).get(key, default)
            if isinstance(default, bool):
                w = QCheckBox()
                w.setChecked(bool(v))
            elif key == "direction":
                w = QComboBox()
                w.setEditable(True)
                w.addItems(DIRS if default == "normal" else DIRS[1:])
                if isinstance(v, (list, tuple)):
                    v = ", ".join(f"{x:g}" for x in v)
                w.setCurrentText(str(v))
                w.setToolTip("normal, ±X/±Y/±Z, or 'x, y, z'")
            else:
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(4)
                w.setValue(float(v))
                if unit:
                    w.setSuffix(" " + unit)
            form.addRow(label, w)
            ws[key] = w
        return ws

    @staticmethod
    def _param_values(ws):
        out = {}
        for k, w in ws.items():
            if isinstance(w, QCheckBox):
                out[k] = w.isChecked()
            elif isinstance(w, QComboBox):
                t = w.currentText().strip()
                if "," in t:
                    out[k] = [float(x) for x in t.split(",")]
                else:
                    out[k] = t if t == "normal" else {"+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0],
                                                       "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1]}.get(
                        t.upper(), [0, 0, -1])
            else:
                out[k] = w.value()
        return out

    def start_pick(self, kind, itype):
        st = self.current()
        if not st:
            return
        self.clear_results()
        self.picking = (kind, itype)
        self.picked = []
        while self.pick_form.rowCount():
            self.pick_form.removeRow(0)
        self.pick_ws = self._param_widgets(self.pick_form, PARAMS.get(itype, []))
        needs = itype not in ("gravity", "centrifugal", "heat_volume")
        helps = {**FIXTURE_HELP, **LOAD_HELP, **THERMAL_HELP}
        self.pick_label.setText(f"<b>{helps[itype]}</b><br>" + (f"Click faces of <b>{st['object']}</b> "
                                                                 "(click again to unpick)." if needs else ""))
        self.add_btn.setEnabled(not needs)
        self.pick_box.show()
        self._update_pick_label()

    def on_pick(self, info):
        """Face clicked in the viewport while picking."""
        st = self.current()
        if self.bolt_pick is not None and st and info:
            if info.get("type") != "cylinder":
                self.status.emit("Click the hole (cylindrical face)")
                return
            self.bolt_pick.append((info["object"], info["index"]))
            self.vp.highlight(info["object"], info["index"], color=(0.95, 0.75, 0.1))
            self.vp.render()
            if len(self.bolt_pick) == 1:
                self.status.emit(f"Bolt: hole on {info['object']} picked — now the hole on the other part")
                return
            (oa, fa), (ob, fb) = self.bolt_pick
            self.bolt_pick = None
            if oa == ob:
                self.status.emit("The two holes must be on different parts")
                return
            from PySide6.QtWidgets import QInputDialog
            pre, ok = QInputDialog.getDouble(self, "Bolt", "Preload (N, 0 = none):", 0.0, 0.0, 1e6, 0)
            if not ok:
                return
            grade, ok = QInputDialog.getItem(self, "Bolt", "Property class:", ["8.8", "10.9", "12.9", "A2-70"], 0,
                                             False)
            try:
                self.doc.add_bolt(st["name"], oa, fa, ob, fb, preload=pre, grade=grade if ok else "8.8")
            except FeatureError as e:
                QMessageBox.warning(self, "Bolt", str(e))
            self.vp.clear_selection()
            self._fill()
            return
        if not self.picking or not st or not info:
            return
        if st["type"] == "assembly":
            if self.picked and info["object"] != getattr(self, "pick_obj", None):
                self.status.emit(f"One part per item — finish the faces on {self.pick_obj} first")
                return
            self.pick_obj = info["object"]
        elif info["object"] != st["object"]:
            self.status.emit(f"Pick faces on {st['object']} (this study's object)")
            self.vp.highlight_faces(st["object"], self.picked)
            return
        fi = info["index"]
        if fi in self.picked:
            self.picked.remove(fi)
        else:
            self.picked.append(fi)
        self.vp.highlight_faces(getattr(self, "pick_obj", None) if st["type"] == "assembly" else st["object"],
                                self.picked)
        self._update_pick_label()

    def _update_pick_label(self):
        if not self.picking:
            return
        needs = self.picking[1] not in ("gravity", "centrifugal", "heat_volume")
        self.add_btn.setEnabled(bool(self.picked) or not needs)
        self.add_btn.setText(f"Add ({len(self.picked)} face{'s' if len(self.picked) != 1 else ''})" if needs else "Add")

    def _commit_pick(self):
        st = self.current()
        kind, itype = self.picking
        try:
            extra = {"object": self.pick_obj} if st["type"] == "assembly" and getattr(self, "pick_obj", None) else {}
            self.doc.add_study_item(st["name"], kind, itype, list(self.picked), **extra,
                                    **self._param_values(self.pick_ws))
        except (FeatureError, ValueError) as e:
            QMessageBox.warning(self, "Study", str(e))
            return
        self.cancel_pick()
        self._fill()

    def cancel_pick(self):
        self.picking = None
        self.picked = []
        self.pick_box.hide()
        self.vp.clear_selection()
        self.vp.render()

    # ---------------------------------------------------------------- run
    def run(self):
        st = self.current()
        if not st or self.thread is not None:
            return
        self.cancel_pick()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.msg.setText("Starting…")
        self.thread = QThread(self)
        self.worker = _Worker(self.doc, st["name"])
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.msg.setText)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def stop(self):
        if self.worker:
            self.worker.stop = True

    def _end(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait(10000)
        self.thread = self.worker = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _failed(self, m):
        self._end()
        self.msg.setText("⚠ " + m)
        self.status.emit("Study failed")
        self._fill()

    def _done(self, res):
        self._end()
        self.msg.setText(f"✓ Solved in {res.ms / 1000:.1f} s")
        self._fill()
        self.show_result(res)
        self.status.emit(f"{res.study}: {summary_text(res)}")

    # ------------------------------------------------------------ results
    def show_result(self, res):
        self.result = res
        self.res_box.show()
        kind = res.kind
        self.field.blockSignals(True)
        self.field.clear()
        if kind == "flow":
            self.field.addItems(["velocity", "pressure"])
        else:
            self.field.addItems(list(res.scalars))
        self.field.blockSignals(False)
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItems([m["label"] for m in res.modes])
        self.mode.blockSignals(False)
        frames = getattr(res, "frames", [])
        self.time.blockSignals(True)
        self.time.setRange(0, max(0, len(frames) - 1))
        self.time.setValue(max(0, len(frames) - 1))
        self.time.blockSignals(False)
        self._row_visible("deform", kind in ("static", "harmonic"))
        self._row_visible("mode", bool(res.modes) and kind in ("frequency", "buckling"))
        self._row_visible("time", len(frames) > 1)
        self._row_visible("slice", kind == "flow")
        self.curve.clear()
        self.curve.addItems(list(res.curves))
        self.curve.setVisible(bool(res.curves))
        self.plot.setVisible(bool(res.curves))
        notes = "".join(f"<br>• {n}" for n in res.notes[:4])
        self.summary.setText(f"<b>{summary_text(res)}</b>{notes}")
        if kind in ("frequency", "buckling") and res.modes:
            self.animate.setChecked(True)
        self.redraw()

    def _row_visible(self, key, on):
        lab, box = self.rows[key]
        lab.setVisible(on)
        box.setVisible(on)

    def redraw(self):
        r = self.result
        if r is None:
            return
        st_obj = getattr(r, "object", None)
        pose = self.doc.obj(st_obj).pose if st_obj and self.doc.has_obj(st_obj) else [0.0] * 6
        if r.kind == "flow":
            axis = {0: 2, 1: 1, 2: 0}[self.axis.currentIndex()]
            self.overlay.show_flow(r, "speed" if self.field.currentText() == "velocity" else "pressure", axis,
                                   self.pos.value() / 100.0, self.stream.isChecked(),
                                   title=f"{r.study} · {self.field.currentText()}")
            self.vp.render()
            return
        name = self.field.currentText()
        vectors, scale, values = None, 0.0, None
        unit = r.units.get(name, "")
        if r.modes and r.kind in ("frequency", "buckling"):
            i = max(0, self.mode.currentIndex())
            shape = r.modes[i]["shape"]
            vectors = shape
            size = float(np.ptp(r.p, axis=0).max())
            scale = 0.08 * size
            values = np.linalg.norm(shape, axis=1)
            name = "mode shape"
            unit = "relative"
        elif getattr(r, "frames", None) and len(r.frames) > 1 and name in r.frames[0]["scalars"]:
            fr = r.frames[self.time.value()]
            values = fr["scalars"][name]
            name = f"{name} at {fr['t']:.3g} s"
        else:
            values = r.scalars.get(name)
            if "displacement" in r.vectors and r.kind in ("static", "harmonic"):
                vectors = r.vectors["displacement"]
                choice = DEFORM[self.deform.currentText()]
                if choice is None:
                    dmax = float(np.linalg.norm(vectors, axis=1).max())
                    size = float(np.ptp(r.p, axis=0).max())
                    scale = 0.05 * size / dmax if dmax > 0 else 0.0
                else:
                    scale = choice
        if values is None:
            return
        log = name.startswith("life")
        reverse = name.startswith(("factor of safety", "fatigue factor", "life"))
        rng = None
        if name == "density":
            rng = (0.0, 1.0)
        if name.startswith(("factor of safety", "fatigue factor")):
            rng = (0.0, float(min(np.nanmax(values), max(5.0, float(np.nanmin(values)) * 3))))
        if log:
            v = np.asarray(values, float)
            rng = (float(max(v.min(), 1e3)), float(min(v.max(), 1e12)))
        title = f"{r.study} · " + (r.modes[max(0, self.mode.currentIndex())]["label"] if name == "mode shape" else name) \
            + (f" · deformation ×{scale:.3g}" if scale and r.kind == "static" else "")
        self.overlay.show_field(r.p, r.tris, values, name, unit, vectors=vectors, scale=scale, pose=pose,
                                rng=rng, log=log, reverse=reverse, title=title,
                                hide=set(getattr(r, "hide_objects", ()) or ({st_obj} if st_obj else ())))
        self._scale = scale
        self.vp.render()

    def _toggle_anim(self, on):
        if on:
            self.anim.start(40)
        else:
            self.anim.stop()
            self.overlay.set_scale(getattr(self, "_scale", 0.0), 1.0)

    def _animate(self):
        if not self.overlay.active or not self.result or not self.result.modes:
            self.anim.stop()
            return
        self._phase += 0.18
        self.overlay.set_scale(getattr(self, "_scale", 0.0), math.sin(self._phase))

    def _plot(self, name):
        r = self.result
        if r and name in r.curves:
            x, y, xl, yl = r.curves[name]
            self.plot.set_data(x, y, f"{name} ({yl})")
            self.plot.set_cursor(x[-1] if x else 0)

    def clear_results(self):
        self.anim.stop()
        self.animate.blockSignals(True)
        self.animate.setChecked(False)
        self.animate.blockSignals(False)
        self.overlay.clear()
        self.res_box.hide()
        self.result = None
        self.vp.render()
