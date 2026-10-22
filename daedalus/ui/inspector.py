"""Inspector: parameter form generated from the feature's PARAMS spec.
Scalar edits rebuild immediately; table editors apply with a button."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..core.features import FEATURE_TYPES, FeatureError
from ..core.selectors import EDGE_TERMS

ENTITY_COLS = ["type", "x", "y", "w", "h", "r", "x2", "y2", "angle", "points", "mode"]
ENTITY_DEFAULTS = {
    "rect": {"type": "rect", "x": 0, "y": 0, "w": 50, "h": 30, "mode": "add"},
    "circle": {"type": "circle", "x": 0, "y": 0, "r": 10, "mode": "add"},
    "slot": {"type": "slot", "x": -20, "y": 0, "x2": 20, "y2": 0, "w": 10, "mode": "add"},
    "polygon": {"type": "polygon", "points": [[0, 0], [40, 0], [0, 30]], "mode": "add"},
}
EDGE_PRESETS = ["vertical", "vertical&linear", "top", "bottom", "all", "top&circular",
                "top|bottom", "circular", "parallel_x", "parallel_y"]


class Inspector(QWidget):
    edit_sketch = Signal(str)

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.name: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel("Select a feature")
        self.title.setObjectName("h1")
        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.err = QLabel("")
        self.err.setObjectName("error")
        self.err.setWordWrap(True)
        outer.addWidget(self.title)
        outer.addWidget(self.hint)
        outer.addWidget(self.err)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(self.scroll, 1)
        self._body = QWidget()
        self.scroll.setWidget(self._body)

    # ------------------------------------------------------------ build
    def show_feature(self, name: str | None):
        self.name = name
        body = QWidget()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        if not name:
            self.title.setText("Select a feature")
            self.hint.setText("Pick an item in History, or add one from the toolbelt.")
            self.err.setText("")
            self.scroll.setWidget(body)
            return
        try:
            f = self.doc.get(name)
        except FeatureError:
            self.show_feature(None)
            return
        self.title.setText(f"{f.LABEL} — {f.name}")
        self.hint.setText(f.DESCRIPTION)
        self.err.setText(self._issue(name))
        layer_box = QComboBox()
        for l in self.doc.layers:
            layer_box.addItem(l.name + ("  (locked)" if l.locked else ""), l.name)
        layer_box.setCurrentIndex(max(0, [l.name for l in self.doc.layers].index(f.layer))
                                  if self.doc.has_layer(f.layer) else 0)
        layer_box.currentIndexChanged.connect(lambda _i, b=layer_box: self._set_layer(b.currentData()))
        form.addRow("Layer", layer_box)
        if len(self.doc.objects) > 1:
            ol = QLabel(f.obj)
            ol.setObjectName("muted")
            form.addRow("Object", ol)
        locked = self.doc.has_layer(f.layer) and self.doc.layer(f.layer).locked
        if locked:
            note = QLabel(f"Layer '{f.layer}' is locked — unlock it in Layers to edit.")
            note.setObjectName("muted")
            note.setWordWrap(True)
            form.addRow(note)
            layer_box.setEnabled(False)
        params = list(f.PARAMS)
        if f.TYPE == "sketch":
            params.sort(key=lambda p: {"geometry": 0, "plane": 1, "offset": 2}.get(p.name, 3))
        for p in params:
            if p.name in f.HIDDEN:
                continue
            if f.TYPE == "import" and p.name in ("source", "format"):
                lab = QLabel(str(f.params[p.name]) or "—")
                lab.setWordWrap(True)
                form.addRow(p.name.title(), lab)
                continue
            if f.TYPE == "sketch" and p.name == "entities" and not f.params["entities"]:
                more = QPushButton("+ Quick shapes table (no constraints)")
                more.clicked.connect(lambda _=False, fl=form, b=None: self._show_quick(fl, f, p))
                form.addRow(more)
                self._quick_btn = more
                continue
            w = self._editor(f, p)
            w.setToolTip(p.help)
            if locked:
                w.setEnabled(False)
            drv = getattr(self.doc, "driven_params", lambda: {})().get(f.name, {})
            cfg = getattr(self.doc, "active_config", "Default")
            cfg_over = (self.doc.config().get("params", {}) if cfg != "Default" else {}).get(f"{f.name}.{p.name}")
            if p.name in drv:
                w.setEnabled(False)
                val = getattr(self.doc, "eq_values", {}).get(f"{f.name}.{p.name}")
                w.setToolTip(f"Driven by the equation  = {drv[p.name]}" + (f"  → {val:g}" if isinstance(val, (int, float))
                                                                           else ""))
            elif cfg_over is not None:
                w.setToolTip(f"Configuration '{cfg}' overrides this: {cfg_over}  (edit it in Equations & "
                             "Configurations)")
                w.setEnabled(False)
            if p.kind == "json":
                form.addRow(QLabel("Constrained geometry" if p.name == "geometry" else
                                   "Quick shapes" if p.name == "entities" else p.name.replace("_", " ").title()))
                form.addRow(w)
            else:
                form.addRow(p.name.replace("_", " ").title(), w)
        self.scroll.setWidget(body)

    def _set_layer(self, layer):
        if not self.name or not layer:
            return
        f = self.doc.get(self.name)
        if f.layer == layer:
            return
        self._editing = True
        try:
            self.doc.move_to_layer([self.name], layer)
        except FeatureError as e:
            self.err.setText(str(e))
        finally:
            self._editing = False

    def _issue(self, name):
        r = self.doc.result
        if name in r.errors:
            return r.errors[name]
        if name in r.sketch_issues:
            return "Note: " + r.sketch_issues[name]
        return ""

    def _show_quick(self, form, f, p):
        self._quick_btn.hide()
        form.addRow(QLabel("Quick shapes"))
        form.addRow(self._editor(f, p))

    def refresh_errors(self):
        if self.name:
            self.err.setText(self._issue(self.name))

    def _commit(self, key, value):
        if not self.name:
            return
        f = self.doc.get(self.name)
        if f.params.get(key) == value:
            return
        self._editing = True
        try:
            self.doc.edit(self.name, {key: value})
        except FeatureError as e:
            self.err.setText(str(e))
        finally:
            self._editing = False

    def sync(self):
        """Document changed. Rebuild the form unless the change came from this form."""
        if getattr(self, "_editing", False):
            self.refresh_errors()
        else:
            self.show_feature(self.name)

    def _editor(self, f, p):
        v = f.params[p.name]
        k = p.kind
        if k == "float":
            from ..core.units import param_quantity
            from .units_ui import UnitSpin
            q = param_quantity(p)
            if q:
                w = UnitSpin(q)
            else:
                w = QDoubleSpinBox()
                w.setDecimals(4)
                w.setSingleStep(0.1)
            w.setRange(p.minimum if p.minimum is not None else -1e9, 1e9)
            w.setValue(v)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda val, key=p.name: self._commit(key, val))
            return w
        if k == "int":
            w = QSpinBox()
            w.setRange(int(p.minimum or 0), 10000)
            w.setValue(v)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda val, key=p.name: self._commit(key, val))
            return w
        if k == "bool":
            w = QCheckBox()
            w.setChecked(v)
            w.toggled.connect(lambda val, key=p.name: self._commit(key, val))
            return w
        if k in ("choice", "sketch_ref", "feature_ref", "edges", "plane_ref", "axis_ref"):
            w = QComboBox()
            if k == "plane_ref":
                from ..core.features import BASE_PLANES, FACE_SELECTORS
                items = BASE_PLANES + FACE_SELECTORS + self._earlier(f, {"plane"})
                w.setEditable(True)
            elif k == "axis_ref":
                items = ["X", "Y", "Z"] + self._earlier(f, {"axis"})
                w.setEditable(True)
            elif k == "choice":
                items = p.choices
            elif k == "sketch_ref":
                items = self._earlier(f, {"sketch", "sketch3d"} if p.name == "path" else {"sketch"})
            elif k == "feature_ref":
                items = self._earlier(f, {"extrude", "revolve", "hole"})
            else:
                items = EDGE_PRESETS
                w.setEditable(True)
                w.setToolTip("Terms: " + ", ".join(EDGE_TERMS) + ". Combine with & and |")
            w.addItems(items)
            if v and v not in items:
                w.addItem(v)
            w.setCurrentText(v)
            if k in ("edges", "plane_ref", "axis_ref"):
                w.lineEdit().editingFinished.connect(lambda key=p.name, w=w: self._commit(key, w.currentText()))
                w.activated.connect(lambda _i, key=p.name, w=w: self._commit(key, w.currentText()))
            else:
                w.currentTextChanged.connect(lambda val, key=p.name: self._commit(key, val))
            return w
        if k == "json" and p.name == "geometry":
            return SketchDims(self, f)
        if k == "json" and p.name == "entities":
            return EntityTable(v, lambda val: self._commit("entities", val))
        if k == "json" and p.name == "positions":
            return PointTable(v, lambda val: self._commit("positions", val))
        w = QLineEdit(v if isinstance(v, str) else json.dumps(v))
        w.editingFinished.connect(lambda key=p.name, w=w: self._commit(key, w.text()))
        return w

    def _earlier(self, f, types):
        out = []
        for g in self.doc.features:
            if g is f:
                break
            if g.TYPE in types:
                out.append(g.name)
        return out


class SketchDims(QWidget):
    """Sketch status, 'Edit sketch' button and the driving dimensions as live fields."""

    def __init__(self, inspector: "Inspector", feature, parent=None):
        super().__init__(parent)
        self.insp, self.f = inspector, feature
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        st = inspector.doc.result.sketch_status.get(feature.name)
        geom = feature.params.get("geometry") or {}
        n = len(geom.get("curves", []))
        txt = "No constrained geometry yet" if not n else \
            f"{n} curves · " + ("fully defined" if st and st["dof"] == 0 else
                                f"under-defined, {st['dof']} DOF" if st else "see error")
        info = QLabel(txt)
        info.setObjectName("muted")
        lay.addWidget(info)
        b = QPushButton("✎  Edit 3D sketch" if feature.TYPE == "sketch3d" else "✎  Edit sketch")
        b.setObjectName("primary")
        b.clicked.connect(lambda: inspector.edit_sketch.emit(feature.name))
        lay.addWidget(b)
        dims = [(i, k) for i, k in enumerate(geom.get("constraints", [])) if "value" in k]
        if dims:
            form = QFormLayout()
            for i, k in dims:
                from .units_ui import UnitSpin
                w = UnitSpin("angle" if k["type"] == "angle" else "length")
                w.setRange(0.0, 1e9)
                w.setValue(float(k["value"]))
                w.setKeyboardTracking(False)
                w.valueChanged.connect(lambda v, i=i: self._set(i, v))
                label = {"length": "L", "distance": "D", "hdistance": "H", "vdistance": "V",
                         "radius": "R", "diameter": "Ø", "angle": "∠", "dx": "ΔX", "dy": "ΔY",
                         "dz": "ΔZ"}.get(k["type"], k["type"])
                form.addRow(f"{label} {' · '.join(k['e'])}", w)
            lay.addLayout(form)

    def _set(self, i, v):
        g = json.loads(json.dumps(self.f.params.get("geometry") or {}))
        g["constraints"][i]["value"] = float(v)
        self.insp._commit("geometry", g)


class EntityTable(QWidget):
    def __init__(self, entities, on_apply, parent=None):
        super().__init__(parent)
        self.on_apply = on_apply
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(ENTITY_COLS))
        self.table.setHorizontalHeaderLabels(ENTITY_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(170)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for t in ENTITY_DEFAULTS:
            b = QPushButton(f"+ {t}")
            b.clicked.connect(lambda _=False, t=t: self._add(ENTITY_DEFAULTS[t]))
            row.addWidget(b)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove)
        row.addWidget(rm)
        lay.addLayout(row)
        apply = QPushButton("Apply sketch")
        apply.setObjectName("primary")
        apply.clicked.connect(lambda: self.on_apply(self.value()))
        lay.addWidget(apply)
        for e in entities:
            self._add(e)

    def _add(self, e):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, col in enumerate(ENTITY_COLS):
            val = e.get(col, "")
            if col == "points" and val:
                val = "; ".join(f"{p[0]:g},{p[1]:g}" for p in val)
            self.table.setItem(r, c, QTableWidgetItem("" if val == "" else str(val)))

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows or [self.table.rowCount() - 1]:
            if r >= 0:
                self.table.removeRow(r)

    def value(self):
        out = []
        for r in range(self.table.rowCount()):
            e = {}
            for c, col in enumerate(ENTITY_COLS):
                it = self.table.item(r, c)
                txt = it.text().strip() if it else ""
                if not txt:
                    continue
                if col in ("type", "mode"):
                    e[col] = txt
                elif col == "points":
                    e[col] = [[float(a) for a in pt.split(",")] for pt in txt.split(";") if pt.strip()]
                else:
                    e[col] = float(txt)
            if e:
                out.append(e)
        return out


class PointTable(QWidget):
    def __init__(self, points, on_apply, parent=None):
        super().__init__(parent)
        self.on_apply = on_apply
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["x", "y"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(130)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("+ point")
        add.clicked.connect(lambda: self._add([0, 0]))
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self.table.removeRow(max(self.table.currentRow(), 0)))
        apply = QPushButton("Apply")
        apply.setObjectName("primary")
        apply.clicked.connect(lambda: self.on_apply(self.value()))
        for b in (add, rm, apply):
            row.addWidget(b)
        lay.addLayout(row)
        for p in points:
            self._add(p)

    def _add(self, p):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(f"{p[0]:g}"))
        self.table.setItem(r, 1, QTableWidgetItem(f"{p[1]:g}"))

    def value(self):
        out = []
        for r in range(self.table.rowCount()):
            try:
                out.append([float(self.table.item(r, 0).text()), float(self.table.item(r, 1).text())])
            except (AttributeError, ValueError):
                continue
        return out


__all__ = ["Inspector", "FEATURE_TYPES"]
