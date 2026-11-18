"""Project customizer: option groups (pick one / add-ons) whose choices add parts to a build, and the picker an
order line, the Project BOM window and the Projects tab use to choose a configuration."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..inventory import configs as C
from ..inventory import model as M

KINDS = [(C.ONE, "Pick one (e.g. length)"), (C.ANY, "Add-ons (any, on / off)")]


class ConfigPicker(QWidget):
    """One combo per 'pick one' group, a check box per add-on."""
    changed = Signal(dict)

    def __init__(self, project, config=None, parent=None, inline=True):
        super().__init__(parent)
        self.project = project
        cfg = C.normalize(project, config)
        lay = QHBoxLayout(self) if inline else QFormLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combos, self.boxes = {}, {}
        for g in C.groups(project):
            ch = g.get("choices") or []
            if not ch:
                continue
            if g.get("kind", C.ONE) == C.ONE:
                cb = QComboBox()
                for c in ch:
                    cb.addItem(c["name"], c["id"])
                cb.setCurrentIndex(max(0, cb.findData((cfg.get(g["id"]) or [None])[0])))
                cb.currentIndexChanged.connect(lambda _i: self.changed.emit(self.value()))
                cb.setToolTip(g["name"])
                self.combos[g["id"]] = cb
                if inline:
                    lay.addWidget(QLabel(g["name"]))
                    lay.addWidget(cb)
                else:
                    lay.addRow(g["name"], cb)
            else:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                for c in ch:
                    b = QCheckBox(c["name"])
                    b.setChecked(c["id"] in cfg.get(g["id"], []))
                    b.toggled.connect(lambda _on: self.changed.emit(self.value()))
                    self.boxes[(g["id"], c["id"])] = b
                    rl.addWidget(b)
                rl.addStretch(1)
                if inline:
                    lay.addWidget(row)
                else:
                    lay.addRow(g["name"], row)
        if inline:
            lay.addStretch(1)

    def value(self) -> dict:
        cfg = {gid: [cb.currentData()] for gid, cb in self.combos.items()}
        for (gid, cid), b in self.boxes.items():
            cfg.setdefault(gid, [])
            if b.isChecked():
                cfg[gid].append(cid)
        return C.normalize(self.project, cfg)


class ConfigDialog(QDialog):
    """Configure one order line: how many of the project's top-level MFG parts (weldments / assemblies and MFG
    parts that aren't inside one) per unit, and its custom options. Edit options… defines the project's option
    groups (pick one / add-ons)."""

    def __init__(self, inv, pid, config=None, parent=None, units=None):
        super().__init__(parent)
        self.inv, self.pid = inv, pid
        self.pr = pr = inv.project(pid)
        self.options_changed = False
        self.setWindowTitle(f"Configure {pr['name']}")
        self.resize(760, 560)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.units = None
        if units is not None:
            from .inventory_dialogs import _qty
            top.addWidget(QLabel("Units"))
            self.units = _qty(units, 1)
            top.addWidget(self.units)
        top.addStretch(1)
        reset = QPushButton("Standard quantities")
        reset.setToolTip("Every part back to the project's qty per unit")
        reset.clicked.connect(self.reset)
        top.addWidget(reset)
        v.addLayout(top)
        cap = QLabel("<b>MFG parts</b> — top level: weldments / assemblies and parts not in one. Qty per unit.")
        v.addWidget(cap)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Part", "Type", "Standard", "This order"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)
        cfg = C.normalize(pr, config)
        changes = C.qty_changes(cfg)
        self.spins = {}
        self.base = {}
        for p, q in C.top_mfg(inv, pid):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(M.label(p)))
            kind = "Weldment" if p.get("isWeldment") else "Assembly" if p.get("components") else "Part"
            self.table.setItem(r, 1, QTableWidgetItem(kind))
            it = QTableWidgetItem(f"{q:g}")
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 2, it)
            sp = QDoubleSpinBox()
            sp.setDecimals(0 if float(q).is_integer() else 2)
            sp.setRange(0, 100000)
            sp.setValue(max(0.0, q + changes.get(p["id"], 0.0)))
            sp.setFixedWidth(90)
            sp.valueChanged.connect(lambda _v: self._show())
            self.table.setCellWidget(r, 3, sp)
            self.spins[p["id"]] = sp
            self.base[p["id"]] = q
        if not self.spins:
            self.table.hide()
            cap.setText("<b>MFG parts</b> — none on this project yet.")
        self.opt_row = QHBoxLayout()
        self.opt_cap = QLabel("<b>Custom options</b>")
        self.opt_row.addWidget(self.opt_cap)
        self.opt_row.addStretch(1)
        ed = QPushButton("Edit options…")
        ed.setToolTip("Define the project's options: pick-one groups (e.g. length) and add-ons (e.g. 4th beam), "
                      "each adding its own parts")
        ed.clicked.connect(self.edit_options)
        self.opt_row.addWidget(ed)
        v.addLayout(self.opt_row)
        self.picker_box = QVBoxLayout()
        v.addLayout(self.picker_box)
        self.picker = None
        self._make_picker(cfg)
        self.adds = QLabel()
        self.adds.setObjectName("muted")
        self.adds.setWordWrap(True)
        v.addWidget(self.adds)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._show()

    def _make_picker(self, cfg):
        while self.picker_box.count():
            w = self.picker_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        if C.has_options(self.pr):
            self.picker = ConfigPicker(self.pr, cfg, self, inline=False)
            self.picker.changed.connect(lambda _c: self._show())
            self.picker_box.addWidget(self.picker)
        else:
            self.picker = None
            n = QLabel("No options on this project — Edit options… adds them.")
            n.setObjectName("muted")
            self.picker_box.addWidget(n)

    def edit_options(self):
        cur = self.value()
        d = ProjectCustomizerDialog(self.inv, self.pid, self)
        if d.exec() != QDialog.Accepted:
            return
        self.pr["options"] = d.result_options
        self.options_changed = True
        self._make_picker(cur)
        self._show()

    def reset(self):
        for k, sp in self.spins.items():
            sp.setValue(self.base[k])

    def _show(self):
        cfg = self.value()
        bits = []
        q = C.qty_label(self.inv, cfg)
        if q:
            bits.append("Qty changes: " + q)
        ln = {k: v for k, v in C.lines(self.pr, {k: v for k, v in cfg.items() if k != C.QTY}).items()}
        if ln:
            bits.append("Options add per unit: " + ", ".join(f"{M.label(self.inv.part(k))} ×{n:g}"
                                                             for k, n in ln.items() if self.inv.part(k)))
        self.adds.setText(" · ".join(bits) or "Standard build — nothing changed.")

    def value(self):
        cfg = self.picker.value() if self.picker is not None else {}
        ch = {k: sp.value() - self.base[k] for k, sp in self.spins.items() if abs(sp.value() - self.base[k]) > 1e-9}
        if ch:
            cfg[C.QTY] = ch
        return C.normalize(self.pr, cfg)


class ProjectCustomizerDialog(QDialog):
    """Options of a project: groups → choices → the parts each choice adds per unit."""

    def __init__(self, inv, pid, parent=None):
        super().__init__(parent)
        self.inv, self.pid = inv, pid
        self.pr = inv.project(pid)
        self.opts = copy.deepcopy(C.groups(self.pr))
        self.setWindowTitle(f"Customize {self.pr['name']}")
        self.resize(1000, 620)
        v = QVBoxLayout(self)
        intro = QLabel("The project's own parts are common to every build. Put what changes between builds here: "
                       "a <b>pick-one</b> option (e.g. Length: 1006 / 1308 / Short) or <b>add-ons</b> (e.g. 4th "
                       "beam). Each choice lists the parts it adds per unit. Orders pick a configuration per line.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        v.addWidget(intro)
        sp = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(lambda *_: self._show())
        lv.addWidget(self.tree, 1)
        bl = QHBoxLayout()
        for lab, fn in (("+ Option", self.add_group), ("+ Choice", self.add_choice), ("Remove", self.remove)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            bl.addWidget(b)
        lv.addLayout(bl)
        sp.addWidget(left)
        self.right = QWidget()
        self.rv = QVBoxLayout(self.right)
        sp.addWidget(self.right)
        sp.setSizes([300, 700])
        v.addWidget(sp, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()
        if not self.opts:
            self.add_group()

    # ------------------------------------------------------------ tree
    def _fill(self, select=None):
        self.tree.blockSignals(True)
        self.tree.clear()
        pick = None
        for g in self.opts:
            gi = QTreeWidgetItem([f"{g['name']}  ({'pick one' if g.get('kind', C.ONE) == C.ONE else 'add-ons'})"])
            gi.setData(0, Qt.UserRole, ("g", g["id"]))
            f = gi.font(0)
            f.setBold(True)
            gi.setFont(0, f)
            self.tree.addTopLevelItem(gi)
            if select == g["id"]:
                pick = gi
            for c in g.get("choices") or []:
                n = len(c.get("lines") or [])
                ci = QTreeWidgetItem([f"{c['name']}{'  ★' if c.get('default') else ''}  — {n} part{'s' if n != 1 else ''}"])
                ci.setData(0, Qt.UserRole, ("c", c["id"]))
                gi.addChild(ci)
                if select == c["id"]:
                    pick = ci
            gi.setExpanded(True)
        self.tree.blockSignals(False)
        if pick is not None:
            self.tree.setCurrentItem(pick)
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._show()

    def _sel(self):
        it = self.tree.currentItem()
        if it is None:
            return None, None
        kind, id_ = it.data(0, Qt.UserRole)
        if kind == "g":
            return next((g for g in self.opts if g["id"] == id_), None), None
        for g in self.opts:
            for c in g.get("choices") or []:
                if c["id"] == id_:
                    return g, c
        return None, None

    def add_group(self):
        g = C.new_group("Length" if not self.opts else "Add-ons", C.ONE if not self.opts else C.ANY)
        self.opts.append(g)
        self._fill(g["id"])

    def add_choice(self):
        g, _c = self._sel()
        if g is None:
            return
        c = C.new_choice(f"Choice {len(g.get('choices') or []) + 1}", default=not g.get("choices") and g.get("kind") == C.ONE)
        g.setdefault("choices", []).append(c)
        self._fill(c["id"])

    def remove(self):
        g, c = self._sel()
        if g is None:
            return
        if c is not None:
            g["choices"] = [x for x in g["choices"] if x["id"] != c["id"]]
            self._fill(g["id"])
        else:
            self.opts = [x for x in self.opts if x["id"] != g["id"]]
            self._fill()

    # ------------------------------------------------------------ detail
    def _clear(self, lay=None):
        lay = lay or self.rv
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()                      # gone now — deleteLater only runs after this event
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                self._clear(it.layout())
                it.layout().deleteLater()

    def _show(self):
        self._clear()
        g, c = self._sel()
        if g is None:
            self.rv.addWidget(QLabel("Add an option to start."))
            self.rv.addStretch(1)
            return
        if c is None:
            f = QFormLayout()
            name = QLineEdit(g["name"])
            name.editingFinished.connect(lambda: (g.__setitem__("name", name.text().strip() or "Option"),
                                                  self._relabel()))
            kind = QComboBox()
            for k, lab in KINDS:
                kind.addItem(lab, k)
            kind.setCurrentIndex(max(0, kind.findData(g.get("kind", C.ONE))))
            kind.currentIndexChanged.connect(lambda _i: (g.__setitem__("kind", kind.currentData()), self._relabel()))
            f.addRow("Option", name)
            f.addRow("Kind", kind)
            self.rv.addLayout(f)
            hint = QLabel("Add its choices with “+ Choice”, then select a choice to list the parts it adds.")
            hint.setObjectName("muted")
            self.rv.addWidget(hint)
            self.rv.addStretch(1)
            return
        f = QFormLayout()
        name = QLineEdit(c["name"])
        name.editingFinished.connect(lambda: (c.__setitem__("name", name.text().strip() or "Choice"), self._relabel()))
        f.addRow(f"{g['name']} choice", name)
        dflt = QCheckBox("Default for new orders" if g.get("kind", C.ONE) == C.ONE else "On by default")
        dflt.setChecked(bool(c.get("default")))

        def set_default(on):
            if on and g.get("kind", C.ONE) == C.ONE:
                for x in g["choices"]:
                    x["default"] = False
            c["default"] = bool(on)
            self._relabel()
        dflt.toggled.connect(set_default)
        f.addRow("", dflt)
        self.rv.addLayout(f)
        self.rv.addWidget(QLabel(f"<b>Parts this choice adds, per unit</b>"))
        from .inventory_mrp_ui import PartPicker
        t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["Part", "Qty / unit", ""])
        t.verticalHeader().hide()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        lines = c.setdefault("lines", [])

        def fill():
            t.setRowCount(0)
            for i, ln in enumerate(lines):
                t.insertRow(i)
                pk = PartPicker(self.inv, None, t)
                pk.set_part(ln.get("partId"))
                pk.activated.connect(lambda _x, ln=ln, pk=pk: ln.__setitem__("partId", pk.part_id()))
                pk.currentIndexChanged.connect(lambda _x, ln=ln, pk=pk: ln.__setitem__("partId", pk.part_id()))
                t.setCellWidget(i, 0, pk)
                q = QDoubleSpinBox()
                q.setRange(0, 100000)
                q.setDecimals(3)
                q.setValue(M.num(ln.get("qty")) or 1)
                q.valueChanged.connect(lambda val, ln=ln: ln.__setitem__("qty", val))
                t.setCellWidget(i, 1, q)
                rm = QPushButton("Remove")
                rm.clicked.connect(lambda _=False, ln=ln: (lines.remove(ln), fill(), self._relabel()))
                t.setCellWidget(i, 2, rm)
            t.resizeRowsToContents()
        fill()
        self.rv.addWidget(t, 1)
        add = QPushButton("+ Add part")
        add.clicked.connect(lambda: (lines.append({"partId": None, "qty": 1}), fill(), self._relabel()))
        h = QHBoxLayout()
        h.addWidget(add)
        h.addStretch(1)
        self.rv.addLayout(h)

    def _relabel(self):
        g, c = self._sel()
        keep = (c or g or {}).get("id")
        it = self.tree.currentItem()
        if it is None:
            return
        # labels only — the detail panel stays as it is
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            gi = self.tree.topLevelItem(i)
            gg = next((x for x in self.opts if x["id"] == gi.data(0, Qt.UserRole)[1]), None)
            if gg is None:
                continue
            gi.setText(0, f"{gg['name']}  ({'pick one' if gg.get('kind', C.ONE) == C.ONE else 'add-ons'})")
            for j in range(gi.childCount()):
                ci = gi.child(j)
                cc = next((x for x in gg.get("choices") or [] if x["id"] == ci.data(0, Qt.UserRole)[1]), None)
                if cc:
                    n = len([ln for ln in cc.get("lines") or [] if ln.get("partId")])
                    ci.setText(0, f"{cc['name']}{'  ★' if cc.get('default') else ''}  — {n} part{'s' if n != 1 else ''}")
        self.tree.blockSignals(False)
        _ = keep

    # ------------------------------------------------------------ save
    def options(self) -> list:
        out = []
        for g in self.opts:
            g = copy.deepcopy(g)
            for c in g.get("choices") or []:
                c["lines"] = [ln for ln in c.get("lines") or [] if ln.get("partId") and M.num(ln.get("qty")) > 0]
            if g.get("kind", C.ONE) == C.ONE and g.get("choices") and not any(c.get("default") for c in g["choices"]):
                g["choices"][0]["default"] = True
            out.append(g)
        return out

    def _ok(self):
        opts = self.options()
        empty = [g["name"] for g in opts if not g.get("choices")]
        if empty and QMessageBox.question(self, "Customize", f"{', '.join(empty)} has no choices and will be "
                                          "dropped. Save anyway?") != QMessageBox.Yes:
            return
        self.result_options = [g for g in opts if g.get("choices")]
        self.accept()
