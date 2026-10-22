"""History panel: ordered feature list with a draggable-feeling rollback bar,
suppress/delete/rename via context menu."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QInputDialog, QMenu, QMessageBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from . import theme
from .layers import swatch

GLYPH = {
    "sketch": "✎", "sketch3d": "⟋", "sweep": "➰", "frame": "⌗", "hss": "▣", "extrude": "▮", "revolve": "◐", "fillet": "◜", "chamfer": "◸",
    "shell": "▢", "hole": "◎", "linear_pattern": "⋯", "circular_pattern": "✻", "mirror": "⇋", "import": "⤓", "plane": "▱", "axis": "↕", "loft": "⏢", "rib": "◢",
    "draft": "⏥", "thread": "⌀", "sheet_metal": "⌐", "gusset": "◣",
    "helix": "➿", "spiral": "◌", "curve_xyz": "∿", "projected_curve": "⇣", "composite_curve": "⛓", "spring": "⌇",
    "surface_extrude": "▤", "surface_revolve": "◑", "surface_loft": "⏢", "surface_planar": "▱",
    "surface_offset": "⧈", "surface_fill": "◍", "knit": "⧓", "thicken": "▥", "delete_face": "⌫",
    "sketched_bend": "⌙", "convert_sheet_metal": "⬓", "form_tool": "◒",
}
ROLLBACK = "__rollback__"


TYPE_ICON = {
    "sketch": "Sketch", "sketch3d": "3D Sketch", "sweep": "Sweep", "frame": "Frame", "hss": "HSS member", "extrude": "Extrude",
    "revolve": "Revolve", "fillet": "Fillet", "chamfer": "Chamfer", "shell": "Shell", "hole": "Hole",
    "linear_pattern": "Linear", "circular_pattern": "Circular", "mirror": "Mirror", "import": "file-arrow-down",
    "plane": "Plane", "axis": "Axis", "loft": "Loft", "rib": "Rib", "draft": "Draft", "thread": "Thread",
    "sheet_metal": "Sheet metal", "gusset": "Gusset",
    "helix": "Helix", "spiral": "Spiral", "curve_xyz": "Curve XYZ", "projected_curve": "Projected curve",
    "composite_curve": "Composite curve", "spring": "Spring", "surface_extrude": "Extruded surface",
    "surface_revolve": "Revolved surface", "surface_loft": "Lofted surface", "surface_planar": "Planar surface",
    "surface_offset": "Offset surface", "surface_fill": "Filled surface", "knit": "Knit", "thicken": "Thicken",
    "delete_face": "Delete face", "sketched_bend": "Sketched bend", "convert_sheet_metal": "Convert to sheet metal",
    "form_tool": "Forming tool",
}
PLANES = [("Top", "XY"), ("Front", "XZ"), ("Right", "YZ")]


def _is_special(n) -> bool:
    return bool(n) and (n == ROLLBACK or str(n).startswith("__"))


class HistoryPanel(QWidget):
    """Design tree: document → mates, studies, material, reference planes, origin, centre of mass, then the
    feature history (single body) or one node per object with its own mates, material, origin and features."""
    feature_selected = Signal(object)   # feature name or None
    edit_sketch = Signal(str)
    export_dxf = Signal(str)
    action = Signal(str, str)           # (kind, arg): material, mass, mate, mates, study, view

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        from PySide6.QtWidgets import QLineEdit

        from . import icons
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter tree")
        self.filter.setClearButtonEnabled(True)
        ic = icons.icon("funnel", theme.MUTED)
        if not ic.isNull():
            self.filter.addAction(ic, QLineEdit.LeadingPosition)
        self.filter.textChanged.connect(lambda _t: self._apply_filter())
        lay.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemSelectionChanged.connect(self._sel)
        self.tree.itemDoubleClicked.connect(self._double)
        self.tree.itemExpanded.connect(lambda it: self._expanded.add(self._key(it)))
        self.tree.itemCollapsed.connect(lambda it: self._expanded.discard(self._key(it)))
        lay.addWidget(self.tree)
        self._select_after = None
        self._expanded = {"__doc__"}
        self._seen_objs = set()

    # ---------------------------------------------------------------- build
    @staticmethod
    def _key(it):
        return str(it.data(0, Qt.UserRole) or it.text(0))

    def _item(self, parent, text, key, icon=None, note="", muted=False, bold=False, tip=None):
        from . import icons
        it = QTreeWidgetItem([text, note])
        it.setData(0, Qt.UserRole, key)
        if icon:
            ic = icons.icon(icon, theme.MUTED if muted else theme.ICON)
            if not ic.isNull():
                it.setIcon(0, ic)
        it.setForeground(1, QBrush(QColor(theme.MUTED)))
        if muted:
            it.setForeground(0, QBrush(QColor(theme.MUTED)))
        if bold:
            f = QFont()
            f.setBold(True)
            it.setFont(0, f)
        if tip:
            it.setToolTip(0, tip)
        if parent is None:
            self.tree.addTopLevelItem(it)
        else:
            parent.addChild(it)
        return it

    def _feature_item(self, parent, f, i, rb, cur):
        from . import icons
        errors = self.doc.result.errors
        lay = self.doc.layer(f.layer) if self.doc.has_layer(f.layer) else None
        lock = "  (locked)" if lay and lay.locked else ""
        it = QTreeWidgetItem([f.name, f.summary() + lock])
        it.setData(0, Qt.UserRole, f.name)
        it.setForeground(1, QBrush(QColor(theme.MUTED)))
        dim = f.suppressed or (rb is not None and i >= rb) or (lay and (not lay.visible or not lay.in_model))
        err = f.name in errors
        tint = theme.ERROR if err else theme.DIM if dim else (
            lay.color if lay and lay is not self.doc.layers[0] else theme.ICON)
        key = TYPE_ICON.get(f.TYPE, "dot")
        if f.TYPE == "extrude" and f.params.get("operation") == "cut":
            key = "Cut"
        ic = icons.icon(key, tint)
        if not ic.isNull():
            it.setIcon(0, ic)
        else:
            it.setText(0, f"{GLYPH.get(f.TYPE, '•')}  {f.name}")
            if lay:
                it.setIcon(0, swatch(lay.color, 10))
        if lay:
            it.setToolTip(0, f"{f.TYPE.replace('_', ' ')} · layer {lay.name}" + (" (hidden)" if not lay.visible else "") +
                          (" (out of model)" if not lay.in_model else "") + (" (locked)" if lay.locked else ""))
        issues = self.doc.result.sketch_issues
        if err:
            it.setForeground(0, QBrush(QColor(theme.ERROR)))
            it.setToolTip(0, errors[f.name])
            it.setToolTip(1, errors[f.name])
        elif f.name in issues and f.name not in getattr(self, "_refs", ()):
            it.setToolTip(0, issues[f.name])
        elif dim:
            for c in (0, 1):
                it.setForeground(c, QBrush(QColor(theme.DIM)))
            if f.suppressed:
                font = it.font(0)
                font.setItalic(True)
                it.setFont(0, font)
        parent.addChild(it) if parent is not None else self.tree.addTopLevelItem(it)
        if f.name == cur:
            it.setSelected(True)
            self.tree.setCurrentItem(it)
        return it

    def _mate_items(self, parent, mates):
        for m in mates:
            self._item(parent, m.name, "__mate__:" + m.name, "link", f"{m.type} · {m.a['obj']} ↔ {m.b['obj']}",
                       muted=m.suppressed)

    def _signature(self):
        """Everything the tree shows — when it hasn't changed the tree isn't rebuilt (big assemblies: the rebuild
        costs more than the edit)."""
        doc = self.doc
        r = doc.result
        try:
            feats = tuple((f.name, f.TYPE, f.obj, f.suppressed, f.layer, f.summary(),
                           f.params.get("operation") if f.TYPE == "extrude" else None) for f in doc.features)
        except Exception:  # noqa: BLE001
            return None
        return (feats, doc.rollback, str(getattr(doc, "path", "")), doc.active_object, doc.material,
                tuple((o.name, o.color, o.visible, o.fixed, o.material, bool(o.std), str(o.source)) for o in doc.objects),
                tuple((m.name, m.type, m.suppressed, m.objects(), m.value) for m in doc.mates),
                tuple((st["name"], st.get("type"), st["name"] in getattr(doc, "sim_results", {})) for st in doc.studies),
                tuple(sorted(r.errors.items())), tuple(sorted(r.sketch_issues)),
                tuple((l.name, l.color, l.visible, l.locked, l.in_model) for l in doc.layers))

    def refresh(self, select: str | None = None):
        sig = self._signature()
        if sig is not None and sig == getattr(self, "_sig", None):
            if select and select != self.current():
                self._select_name(select)
            return
        self._sig = sig
        cur = select or self.current()
        self._refs = {r_ for g in self.doc.features for r_ in g.references()}
        scroll = self.tree.verticalScrollBar().value()
        self.tree.blockSignals(True)
        self.tree.clear()
        doc = self.doc
        rb = doc.rollback
        multi = len(doc.objects) > 1
        name = doc.path.stem if getattr(doc, "path", None) else "Untitled"
        root = self._item(None, name, "__doc__", "cube", "assembly" if multi else "part", bold=True)
        if doc.mates:
            mf = self._item(root, f"Mates ({len(doc.mates)})", "__mates__", "folder")
            self._mate_items(mf, doc.mates)
        if doc.studies:
            sf = self._item(root, f"Studies ({len(doc.studies)})", "__studies__", "folder")
            for st in doc.studies:
                done = st["name"] in getattr(doc, "sim_results", {})
                self._item(sf, st["name"], "__study__:" + st["name"], "Static", st.get("type", "") +
                           (" · solved" if done else ""))
        if not multi:
            self._item(root, doc.material_of(None).name, "__mat__:", "material", "material",
                       tip="Double-click to change the material")
        for label, pl in PLANES:
            self._item(root, f"{label} plane", "__plane__:" + label, "Plane", pl,
                       tip=f"{pl} · double-click to look at it")
        self._item(root, "Origin", "__origin__", "crosshair-simple", "0, 0, 0")
        self._item(root, "Center of mass", "__com__", "circle-half", "", tip="Double-click for mass properties")
        if not multi:
            for i, f in enumerate(doc.features):
                if rb is not None and i == rb:
                    root.addChild(self._rollback_item())
                self._feature_item(root, f, i, rb, cur)
            if rb is None:
                root.addChild(self._rollback_item())
        else:
            by_obj = {}
            for i, f in enumerate(doc.features):
                by_obj.setdefault(f.obj, []).append((i, f))
            mates_of = {}
            for m in doc.mates:
                for on in set(m.objects()):
                    mates_of.setdefault(on, []).append(m)
            for o in doc.objects:
                src = o.source or {}
                tags = [t for t, on in (("fixed", o.fixed), ("hidden", not o.visible), ("std", bool(o.std)),
                                        (src.get("system", ""), bool(src) and src.get("system") not in ("std",)),
                                        ("linked", bool(src.get("linked")))) if on]
                hd = self._item(root, ("▸ " if o.name == doc.active_object else "") + o.name, "__obj__:" + o.name,
                                None, " · ".join(tags), bold=o.name == doc.active_object,
                                tip="Double-click to make it the active object")
                from . import icons
                ic = icons.icon("cube", o.color)
                hd.setIcon(0, swatch(o.color, 11) if ic.isNull() else ic)
                if not o.visible:
                    hd.setForeground(0, QBrush(QColor(theme.DIM)))
                own = mates_of.get(o.name, [])
                if own:
                    mf = self._item(hd, f"Mates in {name} ({len(own)})", f"__omates__:{o.name}", "folder")
                    self._mate_items(mf, own)
                self._item(hd, doc.material_of(o.name).name, "__mat__:" + o.name, "material", "material",
                           tip="Double-click to change this object's material")
                self._item(hd, "Origin", f"__oorigin__:{o.name}", "crosshair-simple", "")
                for i, f in by_obj.get(o.name, []):
                    self._feature_item(hd, f, i, rb, cur)
                if o.name not in self._seen_objs:          # new objects start expanded
                    self._seen_objs.add(o.name)
                    self._expanded.add("__obj__:" + o.name)
            root.addChild(self._rollback_item())
        self._restore_expansion(root)
        self._apply_filter()
        self.tree.resizeColumnToContents(0)
        self.tree.blockSignals(False)
        self.tree.verticalScrollBar().setValue(scroll)

    def _restore_expansion(self, item):
        if self._key(item) in self._expanded:
            item.setExpanded(True)
        sel = item.isSelected()
        for k in range(item.childCount()):
            ch = item.child(k)
            self._restore_expansion(ch)
            if ch.isSelected() and not sel:          # keep the selected feature visible
                item.setExpanded(True)

    def _apply_filter(self):
        q = self.filter.text().strip().lower()

        def walk(it):
            vis_child = False
            for k in range(it.childCount()):
                vis_child |= walk(it.child(k))
            mine = not q or q in it.text(0).lower() or q in it.text(1).lower()
            show = mine or vis_child
            it.setHidden(not show)
            if q and vis_child:
                it.setExpanded(True)
            return show
        for k in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(k))

    def _rollback_item(self):
        it = QTreeWidgetItem(["━━ end of history", ""])
        it.setData(0, Qt.UserRole, ROLLBACK)
        it.setForeground(0, QBrush(QColor(theme.ACCENT_LINE)))
        f = QFont()
        f.setPointSize(8)
        it.setFont(0, f)
        it.setToolTip(0, "Right-click a feature → 'Roll back to here' to insert features earlier")
        return it

    def selected_names(self) -> list[str]:
        out = []
        for it in self.tree.selectedItems():
            n = it.data(0, Qt.UserRole)
            if n and not _is_special(n):
                out.append(n)
        return out

    def _select_name(self, name):
        from PySide6.QtWidgets import QTreeWidgetItemIterator
        itr = QTreeWidgetItemIterator(self.tree)
        while itr.value():
            it = itr.value()
            if it.data(0, Qt.UserRole) == name:
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(it)
                self.tree.blockSignals(False)
                return
            itr += 1

    def current(self) -> str | None:
        it = self.tree.currentItem()
        if it is None:
            return None
        n = it.data(0, Qt.UserRole)
        return n if n and not _is_special(n) else None

    def _sel(self):
        self.feature_selected.emit(self.current())

    def _double(self, it, _col):
        name = it.data(0, Qt.UserRole)
        if name and str(name).startswith("__obj__:"):
            self.doc.active_object = name.split(":", 1)[1]
            self.doc._emit()
            return
        if name == ROLLBACK:
            self.doc.set_rollback(None)
            return
        if self._special_action(name):
            return
        if name and self.doc.get(name).TYPE in ("sketch", "sketch3d"):
            self.edit_sketch.emit(name)

    def _special_action(self, name) -> bool:
        """Double-click on a non-feature node. Returns True if handled."""
        n = str(name or "")
        if not n.startswith("__"):
            return False
        kind, _, arg = n.partition(":")
        if kind == "__mat__":
            self.action.emit("material", arg)
        elif kind == "__com__":
            self.action.emit("mass", "")
        elif kind == "__mate__":
            self.action.emit("mate", arg)
        elif kind in ("__mates__", "__omates__"):
            self.action.emit("mates", "")
        elif kind == "__study__":
            self.action.emit("study", arg)
        elif kind == "__plane__":
            self.action.emit("view", arg)
        return True

    def _plane_code(self, label):
        return dict(PLANES).get(label, label)

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None:
            return
        name = it.data(0, Qt.UserRole)
        m = QMenu(self)
        n = str(name or "")
        if n.startswith("__") and not n.startswith("__obj__:") and n != ROLLBACK:
            kind, _, arg = n.partition(":")
            labels = {"__mat__": "Edit material…", "__com__": "Mass properties…", "__mate__": "Show in Mates",
                      "__mates__": "Open Mates", "__omates__": "Open Mates", "__study__": "Open study",
                      "__plane__": "Look at plane"}
            if kind in labels:
                m.addAction(labels[kind], lambda: self._special_action(name))
            if kind == "__plane__":
                m.addAction("Sketch on this plane", lambda: self.action.emit("sketch_on", self._plane_code(arg)))
            if kind == "__mate__":
                m.addAction("Drive…", lambda: self.action.emit("drive", arg))
            m.addSeparator()
            m.addAction("Expand all", self.tree.expandAll)
            m.addAction("Collapse all", lambda: (self.tree.collapseAll(),
                                                  self.tree.topLevelItem(0).setExpanded(True)))
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if name and str(name).startswith("__obj__:"):
            on = name.split(":", 1)[1]
            o = self.doc.obj(on)
            m.addAction("Make active", lambda: (setattr(self.doc, "active_object", on), self.doc._emit()))
            m.addAction("Unfix" if o.fixed else "Fix", lambda: self._guard(self.doc.set_object, on, fixed=not o.fixed))
            m.addAction("Show" if not o.visible else "Hide",
                        lambda: self._guard(self.doc.set_object, on, visible=not o.visible))
            if o.source:
                m.addSeparator()
                m.addAction("Open source", lambda: self.action.emit("open_source", on))
                if o.source.get("linked"):
                    m.addAction("Update from source", lambda: self.action.emit("update_links", on))
            m.addSeparator()
            m.addAction("Orient…", lambda: self.action.emit("orient", on))
            m.addAction("Drop to floor", lambda: self.action.emit("drop", on))
            sub = m.addMenu("Turn 90°")
            for i, ax in enumerate("XYZ"):
                for sgn in (1, -1):
                    axis = [0, 0, 0]
                    axis[i] = 1
                    sub.addAction(f"About {ax} {'+' if sgn > 0 else '−'}90°",
                                  lambda a=tuple(axis), s_=sgn: self._guard(self.doc.rotate_object, on, a, 90 * s_))
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if name == ROLLBACK:
            m.addAction("Roll forward to end", lambda: self.doc.set_rollback(None))
        elif name:
            f = self.doc.get(name)
            idx = self.doc.features.index(f)
            if f.TYPE in ("sketch", "sketch3d"):
                m.addAction("Edit sketch", lambda: self.edit_sketch.emit(name))
            if f.TYPE == "plane":
                m.addAction("Sketch on this plane", lambda: self.action.emit("sketch_on", name))
            if f.TYPE == "sketch":
                m.addAction("Export as DXF…", lambda: self.export_dxf.emit(name))
            m.addAction("Rename…", lambda: self._rename(name))
            sub = m.addMenu("Move to layer")
            names = self.selected_names() if name in self.selected_names() else [name]
            for l in self.doc.layers:
                a = sub.addAction(swatch(l.color, 10), l.name + ("  (locked)" if l.locked else ""),
                                  lambda ln=l.name, ns=names: self._move(ns, ln))
                a.setEnabled(not l.locked and any(self.doc.get(n).layer != l.name for n in names))
            m.addAction("Unsuppress" if f.suppressed else "Suppress",
                        lambda: self._guard(self.doc.set_suppressed, name, not f.suppressed))
            m.addSeparator()
            m.addAction("Roll back to here", lambda: self.doc.set_rollback(idx))
            m.addAction("Roll back to after this", lambda: self.doc.set_rollback(idx + 1))
            m.addSeparator()
            m.addAction("Move up", lambda: self._guard(self.doc.move, name, idx - 1)).setEnabled(idx > 0)
            m.addAction("Move down", lambda: self._guard(self.doc.move, name, idx + 1)).setEnabled(
                idx < len(self.doc.features) - 1)
            m.addSeparator()
            m.addAction("Delete", lambda: self._delete(name))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _move(self, names, layer):
        try:
            self.doc.move_to_layer(names, layer)
        except FeatureError as e:
            QMessageBox.warning(self, "Move to layer", str(e))

    def _rename(self, name):
        new, ok = QInputDialog.getText(self, "Rename feature", "Name:", text=name)
        if ok and new and new != name:
            try:
                self.doc.rename(name, new)
            except FeatureError as e:
                QMessageBox.warning(self, "Rename", str(e))

    def _guard(self, fn, *a):
        try:
            fn(*a)
        except FeatureError as e:
            QMessageBox.warning(self, "History", str(e))

    def _delete(self, name):
        try:
            self.doc._check_unlocked(self.doc.get(name))
        except FeatureError as e:
            QMessageBox.warning(self, "Delete", str(e))
            return
        users = [g.name for g in self.doc.features if name in g.references()]
        if users and QMessageBox.question(
                self, "Delete", f"{', '.join(users)} depend on {name} and will fail. Delete anyway?"
        ) != QMessageBox.Yes:
            return
        self.doc.delete(name)
