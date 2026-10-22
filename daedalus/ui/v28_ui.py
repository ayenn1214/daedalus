"""v2.8 UI: Equations & Configurations dialog, defaults for the new features (curves, surfaces, sheet-metal tools),
subassemblies, component patterns / mirror, and the extra mate pick flows."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.doc_v28 import DEFAULT_CONFIG
from ..core.features import FEATURE_TYPES, FeatureError
from . import theme

NEW_FEATURES = {"helix", "spiral", "curve_xyz", "projected_curve", "composite_curve", "spring", "surface_extrude",
                "surface_revolve", "surface_loft", "surface_planar", "surface_offset", "surface_fill", "knit",
                "thicken", "delete_face", "sketched_bend", "convert_sheet_metal", "form_tool"}


# ===================================================================== defaults ===

def default_params(win, ftype):
    """Sensible starting parameters for a v2.8 feature (from the latest sketch / pick), or None to abort."""
    d = win.doc
    last_sk = d.last_of("sketch")
    picks = [e for e in getattr(win.view, "entities", []) if e.get("kind") == "face"]
    face_at = "|".join(f"at({p[0]:.4g},{p[1]:.4g},{p[2]:.4g})" for p in
                       (e.get("local") or e.get("point") for e in picks) if p)
    surfs = [f.name for f in d.features if f.TYPE.startswith("surface_") or f.TYPE in ("knit", "delete_face")]
    sm = [f.name for f in d.features if f.TYPE in ("sheet_metal", "convert_sheet_metal")]
    if ftype in ("helix", "spiral", "curve_xyz", "spring"):
        return {}
    if ftype in ("projected_curve", "surface_extrude", "surface_revolve", "surface_planar"):
        if not last_sk:
            win._need("Create a sketch first.")
            return None
        return {"sketch": last_sk} | ({"axis": "Y"} if ftype == "surface_revolve" else {})
    if ftype in ("composite_curve", "surface_loft"):
        names = [f.name for f in d.features if f.TYPE in ("sketch", "sketch3d", "helix", "spiral", "curve_xyz")
                 and f.obj == d.active_object][-2:]
        if len(names) < 2:
            win._need("Needs two sketches / curves.")
            return None
        return {"curves" if ftype == "composite_curve" else "sections": ", ".join(names)}
    if ftype == "surface_offset":
        return {"faces": face_at or "top", "distance": 5.0}
    if ftype == "delete_face":
        return {"faces": face_at or "top", "patch": True}
    if ftype == "surface_fill":
        return {"boundary": last_sk or ""}
    if ftype == "knit":
        if len(surfs) < 1:
            win._need("Make surfaces first (Surfaces tab).")
            return None
        return {"surfaces": ", ".join(surfs[-4:])}
    if ftype == "thicken":
        if not surfs:
            win._need("Make a surface first.")
            return None
        return {"surface": surfs[-1], "thickness": 2.0}
    if ftype == "sketched_bend":
        if not sm or not last_sk:
            win._need("Needs a sheet-metal part and a sketch with the bend line on it.")
            return None
        return {"sheet": sm[-1], "sketch": last_sk}
    if ftype == "convert_sheet_metal":
        return {"fixed_face": face_at.split("|")[0] if face_at else "bottom"}
    if ftype == "form_tool":
        tool, ok = QInputDialog.getItem(win, "Forming tool", "Tool:", ["dimple", "rib", "louver", "extruded_hole"],
                                        0, False)
        if not ok:
            return None
        return {"sheet": sm[-1] if sm else "", "tool": tool, "face": win._picked_side() or "top"}
    return {}


# ============================================================ equations & configs ===

class ConfigDialog(QDialog):
    """Global variables, equations and configurations (with a design-table grid)."""

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("Equations & Configurations")
        self.resize(820, 520)
        v = QVBoxLayout(self)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs)
        self.tabs.addTab(self._vars_tab(), "Global variables")
        self.tabs.addTab(self._eq_tab(), "Equations")
        self.tabs.addTab(self._cfg_tab(), "Configurations")
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.refresh()

    # ----------------------------------------------------------- variables
    def _vars_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        tip = QLabel("Names you can use in equations (W, H, Thk…). Values can be expressions; trig is in degrees.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.var_table = QTableWidget(0, 3)
        self.var_table.setHorizontalHeaderLabels(["Name", "Expression", "Value"])
        self.var_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.var_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.var_table.cellDoubleClicked.connect(lambda r, _c: self.edit_var(r))
        v.addWidget(self.var_table)
        row = QHBoxLayout()
        for text, fn in (("+ Variable", lambda: self.edit_var(None)), ("Edit", lambda: self.edit_var(
                self.var_table.currentRow())), ("Delete", self.del_var)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def edit_var(self, r):
        names = list(self.doc.variables)
        old = names[r] if r is not None and 0 <= r < len(names) else None
        name, ok = QInputDialog.getText(self, "Variable", "Name:", text=old or "")
        if not ok or not name.strip():
            return
        expr, ok = QInputDialog.getText(self, "Variable", f"{name} =", text=self.doc.variables.get(old or name, "10"))
        if not ok:
            return
        try:
            if old and old != name.strip():
                self.doc.delete_variable(old)
            self.doc.set_variable(name.strip(), expr.strip())
        except FeatureError as e:
            QMessageBox.warning(self, "Variable", str(e))
        self.refresh()

    def del_var(self):
        names = list(self.doc.variables)
        r = self.var_table.currentRow()
        if 0 <= r < len(names):
            self.doc.delete_variable(names[r])
            self.refresh()

    # ----------------------------------------------------------- equations
    def _eq_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        tip = QLabel("Drive a parameter from an expression: Extrude1.depth = W * 2, Sketch1.D3 = Hole1.diameter + 1. "
                     "Sketch dimensions are D0, D1… (their order in the sketch). Driven values are greyed out in "
                     "the Inspector.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.eq_table = QTableWidget(0, 4)
        self.eq_table.setHorizontalHeaderLabels(["Driven", "= Expression", "Value", "Status"])
        self.eq_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.eq_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.eq_table.cellDoubleClicked.connect(lambda r, _c: self.edit_eq(r))
        v.addWidget(self.eq_table)
        row = QHBoxLayout()
        for text, fn in (("+ Equation", lambda: self.edit_eq(None)), ("Edit", lambda: self.edit_eq(
                self.eq_table.currentRow())), ("Suppress / unsuppress", self.toggle_eq), ("Delete", self.del_eq)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _targets(self):
        out = []
        for f in self.doc.features:
            for p in f.PARAMS:
                if p.kind in ("float", "int") and p.name not in f.HIDDEN:
                    out.append(f"{f.name}.{p.name}")
            if f.TYPE in ("sketch", "sketch3d"):
                for i, c in enumerate((f.params.get("geometry") or {}).get("constraints", [])):
                    if "value" in c:
                        out.append(f"{f.name}.D{i}")
        return out

    def edit_eq(self, r):
        eqs = self.doc.equations
        old = eqs[r] if r is not None and 0 <= r < len(eqs) else None
        if old is None:
            tgts = self._targets()
            if not tgts:
                return QMessageBox.information(self, "Equation", "No numeric parameters yet.")
            target, ok = QInputDialog.getItem(self, "Equation", "Drive which value?", tgts, 0, True)
            if not ok:
                return
        else:
            target = old["target"]
        expr, ok = QInputDialog.getText(self, "Equation", f"{target} =", text=(old or {}).get("expr", ""))
        if not ok or not expr.strip():
            return
        try:
            if old:
                self.doc.set_equation(target, expr.strip())
            else:
                self.doc.add_equation(target, expr.strip())
        except FeatureError as e:
            QMessageBox.warning(self, "Equation", str(e))
        self.refresh()

    def toggle_eq(self):
        r = self.eq_table.currentRow()
        if 0 <= r < len(self.doc.equations):
            e = self.doc.equations[r]
            self.doc.set_equation(e["target"], suppressed=not e.get("suppressed"))
            self.refresh()

    def del_eq(self):
        r = self.eq_table.currentRow()
        if 0 <= r < len(self.doc.equations):
            self.doc.delete_equation(self.doc.equations[r]["target"])
            self.refresh()

    # ------------------------------------------------------ configurations
    def _cfg_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("Active:"))
        self.cfg_box = QComboBox()
        self.cfg_box.activated.connect(lambda _i: self._activate(self.cfg_box.currentText()))
        top.addWidget(self.cfg_box, 1)
        for text, fn in (("+ Configuration", self.add_cfg), ("Rename", self.rename_cfg), ("Delete", self.del_cfg),
                         ("+ Row…", self.add_row), ("Import table…", self.import_table),
                         ("Export table…", self.export_table)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        v.addLayout(top)
        tip = QLabel("Modify Configurations: rows are parameters (Feature.param), suppression ($STATE@Feature: S/U), "
                     "variables ($VAR:name) or $MATERIAL; columns are configurations. A blank cell uses the model's "
                     "value. Edit a cell to override it.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.grid = QTableWidget(0, 0)
        self.grid.itemChanged.connect(self._grid_changed)
        v.addWidget(self.grid, 1)
        self._rows_extra = []
        return w

    def _activate(self, name):
        self.doc.activate_configuration(name, checkpoint=True)
        self.refresh()

    def add_cfg(self):
        name, ok = QInputDialog.getText(self, "Configuration", "Name:")
        if ok and name.strip():
            try:
                self.doc.add_configuration(name.strip(), copy_from=self.doc.active_config
                                           if self.doc.active_config != DEFAULT_CONFIG else None)
            except FeatureError as e:
                QMessageBox.warning(self, "Configuration", str(e))
            self.refresh()

    def rename_cfg(self):
        cur = self.doc.active_config
        if cur == DEFAULT_CONFIG:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=cur)
        if ok and name.strip():
            self.doc.set_configuration(cur, new_name=name.strip())
            self.refresh()

    def del_cfg(self):
        cur = self.doc.active_config
        if cur != DEFAULT_CONFIG:
            self.doc.delete_configuration(cur)
            self.refresh()

    def add_row(self):
        opts = self._targets() + [f"$STATE@{f.name}" for f in self.doc.features] + \
            [f"$VAR:{k}" for k in self.doc.variables] + ["$MATERIAL"]
        key, ok = QInputDialog.getItem(self, "Row", "Parameter / suppression / variable:", opts, 0, True)
        if ok and key and key not in self._rows_extra:
            self._rows_extra.append(key)
            self.refresh()

    def import_table(self):
        path, _ = QFileDialog.getOpenFileName(self, "Design table", "", "Design table (*.xlsx *.csv)")
        if path:
            try:
                got = self.doc.read_config_table(path)
                QMessageBox.information(self, "Design table", f"Configurations updated: {', '.join(got) or 'none'}")
            except (FeatureError, OSError, KeyError, ValueError) as e:
                QMessageBox.warning(self, "Design table", str(e))
            self.refresh()

    def export_table(self):
        path, _ = QFileDialog.getSaveFileName(self, "Design table", "design_table.xlsx",
                                              "Excel (*.xlsx);;CSV (*.csv)")
        if path:
            try:
                self.doc.export_config_table(path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Design table", str(e))

    def _grid_changed(self, item):
        if getattr(self, "_filling", False):
            return
        r, c = item.row(), item.column()
        key = self.grid.verticalHeaderItem(r).text()
        cfg = self.grid.horizontalHeaderItem(c).text()
        if cfg == DEFAULT_CONFIG:
            return self.refresh()
        val = item.text().strip()
        try:
            if key.startswith("$STATE@"):
                self.doc.set_configuration(cfg, suppress={key[7:]: (val.upper().startswith("S") if val else None)})
            elif key.startswith("$VAR:"):
                self.doc.set_configuration(cfg, variables={key[5:]: val or None})
            elif key == "$MATERIAL":
                self.doc.set_configuration(cfg, material=val)
            else:
                v = None
                if val:
                    try:
                        v = float(val)
                    except ValueError:
                        v = val
                self.doc.set_configuration(cfg, params={key: v})
        except FeatureError as e:
            QMessageBox.warning(self, "Configuration", str(e))
        self.refresh()

    # ------------------------------------------------------------- refresh
    def refresh(self):
        d = self.doc
        vals = d.variable_values()
        self.var_table.setRowCount(len(d.variables))
        for i, (k, e) in enumerate(d.variables.items()):
            for j, t in enumerate((k, e, f"{vals.get(k):g}" if isinstance(vals.get(k), (int, float)) else "?")):
                self.var_table.setItem(i, j, QTableWidgetItem(t))
        d.effective_features()
        self.eq_table.setRowCount(len(d.equations))
        for i, e in enumerate(d.equations):
            v = d.eq_values.get(e["target"])
            st = "suppressed" if e.get("suppressed") else (d.eq_errors.get(e["target"]) or "ok")
            for j, t in enumerate((e["target"], e["expr"], f"{v:g}" if isinstance(v, (int, float)) else "", st)):
                it = QTableWidgetItem(t)
                if j == 3 and st not in ("ok", "suppressed"):
                    it.setForeground(Qt.red)
                self.eq_table.setItem(i, j, it)
        self.cfg_box.clear()
        self.cfg_box.addItems(d.config_names())
        self.cfg_box.setCurrentText(d.active_config)
        cols, rows = d.config_table()
        keys = cols[2:] + [k for k in self._rows_extra if k not in cols]
        self._filling = True
        self.grid.clear()
        self.grid.setRowCount(len(keys))
        self.grid.setColumnCount(len(rows))
        self.grid.setHorizontalHeaderLabels([r["Configuration"] for r in rows])
        self.grid.setVerticalHeaderLabels(keys)
        for c, row in enumerate(rows):
            cfg = d.config(row["Configuration"])
            for r, k in enumerate(keys):
                over = None
                if k.startswith("$STATE@"):
                    over = cfg.get("suppress", {}).get(k[7:])
                    over = None if over is None else ("S" if over else "U")
                elif k.startswith("$VAR:"):
                    over = cfg.get("variables", {}).get(k[5:])
                elif k == "$MATERIAL":
                    over = cfg.get("material")
                else:
                    over = cfg.get("params", {}).get(k)
                it = QTableWidgetItem("" if over is None else str(over))
                if row["Configuration"] == DEFAULT_CONFIG:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    base = row.get(k, "")
                    it.setText("" if base is None else str(base))
                    it.setForeground(Qt.gray)
                self.grid.setItem(r, c, it)
        self._filling = False


# ======================================================================== assembly ===

def make_subassembly(win):
    d = win.doc
    names = [n for n in win.view.selected if d.has_obj(n)] or list(win.objects_panel.multi)
    if not names:
        return QMessageBox.information(win, "Subassembly", "Select the objects first (view or Objects list).")
    name, ok = QInputDialog.getText(win, "Subassembly", f"Name for {len(names)} object(s):")
    if not ok:
        return
    rigid = QMessageBox.question(win, "Subassembly", "Rigid (moves as one)?\nNo = flexible: members keep moving by "
                                                      "their mates.") == QMessageBox.Yes
    try:
        d.make_subassembly(name.strip() or None, names, rigid=rigid)
    except FeatureError as e:
        QMessageBox.warning(win, "Subassembly", str(e))
    win.objects_panel.refresh()


def dissolve_subassembly(win):
    d = win.doc
    subs = [s["name"] for s in d.subassemblies]
    if not subs:
        return QMessageBox.information(win, "Subassembly", "No subassemblies.")
    name, ok = QInputDialog.getItem(win, "Dissolve", "Subassembly:", subs, 0, False)
    if ok:
        d.dissolve_subassembly(name)
        win.objects_panel.refresh()


def save_subassembly(win):
    d = win.doc
    subs = [s["name"] for s in d.subassemblies]
    if not subs:
        return QMessageBox.information(win, "Subassembly", "No subassemblies.")
    name, ok = QInputDialog.getItem(win, "Save subassembly", "Subassembly:", subs, 0, False)
    if not ok:
        return
    path, _ = QFileDialog.getSaveFileName(win, "Save subassembly", f"{name}.daedalus", "Daedalus (*.daedalus)")
    if path:
        d.save_subassembly(name, path)


class PatternDialog(QDialog):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setWindowTitle({"linear": "Linear component pattern", "circular": "Circular component pattern",
                             "mirror": "Mirror components"}[kind])
        f = QFormLayout(self)
        self.w = {}

        def dsb(v, lo=-1e6, hi=1e6):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(3)
            s.setValue(v)
            return s
        if kind == "linear":
            for k, lab, v in (("dir1", "Direction 1", "X"), ("dir2", "Direction 2", "Y")):
                c = QComboBox()
                c.addItems(["X", "Y", "Z", "-X", "-Y", "-Z"])
                c.setCurrentText(v)
                self.w[k] = c
                f.addRow(lab, c)
                sp = dsb(50.0)
                self.w["spacing" + k[-1]] = sp
                f.addRow("  spacing (mm)", sp)
                n = QSpinBox()
                n.setRange(1, 400)
                n.setValue(3 if k == "dir1" else 1)
                self.w["count" + k[-1]] = n
                f.addRow("  instances", n)
        elif kind == "circular":
            c = QComboBox()
            c.addItems(["Z", "X", "Y"])
            self.w["axis"] = c
            f.addRow("Axis (through the origin)", c)
            n = QSpinBox()
            n.setRange(2, 400)
            n.setValue(4)
            self.w["count"] = n
            f.addRow("Instances", n)
            self.w["angle"] = dsb(360.0, 0.1, 360.0)
            f.addRow("Total angle (°)", self.w["angle"])
        else:
            c = QComboBox()
            c.addItems(["YZ", "XZ", "XY"])
            self.w["plane"] = c
            f.addRow("Mirror plane", c)
            self.w["offset"] = dsb(0.0)
            f.addRow("Plane offset (mm)", self.w["offset"])
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def params(self):
        w = self.w
        if self.kind == "linear":
            return {"dir1": w["dir1"].currentText(), "spacing1": w["spacing1"].value(), "count1": w["count1"].value(),
                    "dir2": w["dir2"].currentText(), "spacing2": w["spacing2"].value(), "count2": w["count2"].value()}
        if self.kind == "circular":
            return {"axis": w["axis"].currentText(), "count": w["count"].value(), "angle": w["angle"].value()}
        n = {"YZ": [1, 0, 0], "XZ": [0, 1, 0], "XY": [0, 0, 1]}[w["plane"].currentText()]
        return {"plane": {"p": [v * w["offset"].value() for v in n], "n": n}}


def component_pattern(win, kind):
    d = win.doc
    names = [n for n in win.view.selected if d.has_obj(n)] or list(win.objects_panel.multi) or \
        ([d.active_object] if d.active_object else [])
    if not names:
        return QMessageBox.information(win, "Pattern", "Select the components first.")
    dlg = PatternDialog(kind, win)
    if not dlg.exec():
        return
    try:
        p = d.add_component_pattern(kind, names, **dlg.params())
        win.statusBar().showMessage(f"{p['name']}: {len(p['instances'])} instance(s) — they follow the seed", 8000)
    except FeatureError as e:
        QMessageBox.warning(win, "Pattern", str(e))
    win.objects_panel.refresh()


def delete_pattern(win):
    d = win.doc
    pats = [p["name"] for p in d.comp_patterns]
    if not pats:
        return QMessageBox.information(win, "Patterns", "No component patterns.")
    name, ok = QInputDialog.getItem(win, "Delete pattern", "Pattern:", pats, 0, False)
    if ok:
        d.delete_component_pattern(name)
        win.objects_panel.refresh()


def config_menu(win, menu):
    """Fill a menu with the configurations (radio) + the dialog."""
    menu.clear()
    d = win.doc
    for n in d.config_names():
        a = menu.addAction(("● " if n == d.active_config else "   ") + n)
        a.triggered.connect(lambda _=False, n=n: (d.activate_configuration(n, checkpoint=True),
                                                  win.statusBar().showMessage(f"Configuration: {n}", 4000)))
    menu.addSeparator()
    menu.addAction("Equations && Configurations…", lambda: ConfigDialog(d, win).exec())


# ============================================================================ mates ===

MATE_GLYPH = {"tangent": "◠", "limit_distance": "↹", "limit_angle": "∡", "width": "⟷", "symmetric": "⇹",
              "path": "〰", "screw": "⧖", "cam": "⬮", "slot": "⊂"}
MATE_HELP = {"tangent": "cylinder touches a face / another cylinder", "limit_distance": "free between a min and max",
             "limit_angle": "free between two angles", "width": "centred between two faces (pick 2 more)",
             "symmetric": "mirror images about a plane (pick the plane next)",
             "path": "a point / axis follows an edge of the other part", "screw": "turning drives travel (lead mm/rev)",
             "cam": "follower rides on the cam's outline", "slot": "pin slides along a slot"}


def special_options(ra, rb):
    """Extra mate choices that reinterpret the two picks (path / cam / slot / width / symmetric)."""
    out = []
    if rb["kind"] in ("point", "axis"):
        out += ["path", "cam", "slot"]
    if ra["kind"] == rb["kind"] == "plane":
        out += ["width"]
    if ra["kind"] == rb["kind"] and ra["kind"] in ("plane", "point", "axis"):
        out += ["symmetric"]
    return out
