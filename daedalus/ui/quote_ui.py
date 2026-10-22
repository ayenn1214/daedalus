"""Quote page (SHOPQUOTE inside Daedalus): CNC / sheet-laser quotes from the exact model, quantity breaks,
machine schedule with a capacity calendar, quote history, G-code back-plot."""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..manufacturing import quote as Q
from . import theme

CNC_FIELDS = [("setup_min", "Setup time", "min", 0, 1e4, 1), ("setups", "Setups", "", 1, 50, 0),
              ("tool_changes", "Tool changes", "", 0, 200, 0), ("tool_change_min", "Tool change", "min", 0, 60, 1),
              ("complexity", "Complexity", "×", 0.1, 10, 2), ("stock_margin", "Stock margin", "mm", 0, 100, 1),
              ("rate", "Machine rate", "$/h", 0, 1e4, 2), ("margin_pct", "Margin", "%", 0, 500, 1)]
SHEET_FIELDS = [("thickness", "Thickness", "mm", 0, 200, 2), ("cut_length", "Cut length", "mm", 0, 1e7, 1),
                ("pierces", "Pierces", "", 0, 1e5, 0), ("pierce_s", "Pierce time", "s", 0, 120, 1),
                ("setup_min", "Setup time", "min", 0, 1e4, 1), ("nest_util", "Nesting use", "×", 0.05, 1, 2),
                ("rate", "Machine rate", "$/h", 0, 1e4, 2), ("margin_pct", "Margin", "%", 0, 500, 1)]


def _btn(text, fn, tip="", primary=False):
    b = QPushButton(text)
    b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    if primary:
        b.setObjectName("primary")
    return b


def _h(text):
    lab = QLabel(text)
    lab.setObjectName("h2")
    return lab


def _card():
    f = QFrame()
    f.setObjectName("card")
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 10, 12, 12)
    v.setSpacing(6)
    return f, v


class QuotePage(QWidget):
    closed = Signal()
    status = Signal(str)
    simulate_requested = Signal()

    def __init__(self, doc_fn, shop: Q.Shop | None = None, parent=None):
        super().__init__(parent)
        self.doc_fn = doc_fn
        self.shop = shop or Q.Shop()
        self.process = "cnc"
        self.params = {"cnc": {**Q.CNC_DEFAULTS, **self.shop.data.get("params", {}).get("cnc", {})},
                       "sheet": {**Q.SHEET_DEFAULTS, **self.shop.data.get("params", {}).get("sheet", {})}}
        self.geom = None
        self.last = None
        self._fields = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        top.setObjectName("qtop")
        top.setAttribute(Qt.WA_StyledBackground, True)
        top.setStyleSheet(f"#qtop{{background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};}}")
        h = QHBoxLayout(top)
        h.setContentsMargins(8, 6, 8, 6)
        h.addWidget(_btn("◀ 3D model", self.closed.emit))
        t = QLabel("Quote")
        t.setObjectName("h1")
        h.addWidget(t)
        sub = QLabel("CNC machining · sheet / laser · machine schedule")
        sub.setObjectName("muted")
        h.addWidget(sub)
        h.addStretch(1)
        h.addWidget(_btn("Simulate G-code…", self.simulate_requested.emit,
                         "Back-plot any .nc / .tap program in the 3D view with time and distance stats"))
        lay.addWidget(top)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._quote_tab(), "Quote")
        self.tabs.addTab(self._schedule_tab(), "Machine schedule")
        self.tabs.addTab(self._history_tab(), "History")
        self.tabs.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.tabs, 1)

    # ================================================================== quote tab
    def _quote_tab(self):
        outer = QWidget()
        h = QHBoxLayout(outer)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(12)
        # ---- left: inputs
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        c, v = _card()
        v.addWidget(_h("PART"))
        row = QHBoxLayout()
        self.part = QComboBox()
        self.part.setMinimumWidth(220)
        self.part.currentIndexChanged.connect(lambda _i: self.load_part())
        row.addWidget(self.part, 1)
        row.addWidget(_btn("Refresh", self.refresh))
        v.addLayout(row)
        self.geom_lab = QLabel("")
        self.geom_lab.setWordWrap(True)
        self.geom_lab.setObjectName("muted")
        v.addWidget(self.geom_lab)
        lv.addWidget(c)

        c, v = _card()
        v.addWidget(_h("PROCESS"))
        pr = QHBoxLayout()
        self.proc_group = QButtonGroup(self)
        self.proc_btns = {}
        for key, text in (("cnc", "CNC machining"), ("sheet", "Sheet / laser")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == "cnc")
            b.setStyleSheet(f"QPushButton:checked {{ background:{theme.ACCENT_SOFT}; border:1px solid {theme.ACCENT_LINE};"
                            f" color:{theme.TEXT}; font-weight:600; }}")
            b.clicked.connect(lambda _=False, k=key: self.set_process(k))
            self.proc_group.addButton(b)
            self.proc_btns[key] = b
            pr.addWidget(b)
        v.addLayout(pr)
        self.proc_hint = QLabel("")
        self.proc_hint.setObjectName("hint")
        v.addWidget(self.proc_hint)
        mrow = QHBoxLayout()
        self.mat_lab = QLabel("")
        mrow.addWidget(QLabel("Material"))
        mrow.addWidget(self.mat_lab, 1)
        self.mat_btn = _btn("Change…", self._pick_material)
        mrow.addWidget(self.mat_btn)
        v.addLayout(mrow)
        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("Quantity"))
        self.qty = QSpinBox()
        self.qty.setRange(1, 1_000_000)
        self.qty.setValue(1)
        self.qty.valueChanged.connect(lambda _v: self.recalc())
        qrow.addWidget(self.qty)
        qrow.addStretch(1)
        v.addLayout(qrow)
        self.use_cam = QCheckBox("Use the CAM cycle time when this part has CAM operations")
        self.use_cam.setChecked(True)
        self.use_cam.toggled.connect(lambda _v: self.recalc())
        v.addWidget(self.use_cam)
        lv.addWidget(c)

        c, v = _card()
        v.addWidget(_h("PARAMETERS"))
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.form_host)
        mrow = QHBoxLayout()
        self.machine = QComboBox()
        self.machine.setToolTip("Pick a machine to use its hourly rate (add machines on the Machine schedule tab)")
        self.machine.activated.connect(self._machine_rate)
        mrow.addWidget(QLabel("Machine"))
        mrow.addWidget(self.machine, 1)
        v.addLayout(mrow)
        rr = QHBoxLayout()
        rr.addWidget(_btn("Save as defaults", self._save_defaults, "Keep these parameters for new quotes"))
        rr.addWidget(_btn("Reset", self._reset_params))
        rr.addStretch(1)
        v.addLayout(rr)
        lv.addWidget(c)
        lv.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(left)
        sc.setMinimumWidth(400)
        sc.setMaximumWidth(520)
        h.addWidget(sc)

        # ---- right: result
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        c, v = _card()
        v.addWidget(_h("QUOTE"))
        self.block = QTableWidget(0, 2)
        self.block.horizontalHeader().setVisible(False)
        self.block.verticalHeader().setVisible(False)
        self.block.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.block.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.block.setEditTriggers(QTableWidget.NoEditTriggers)
        self.block.setSelectionMode(QTableWidget.NoSelection)
        self.block.setShowGrid(False)
        v.addWidget(self.block, 1)
        self.total = QLabel("$0.00")
        f = QFont(self.total.font())
        f.setPointSizeF(20)
        f.setBold(True)
        self.total.setFont(f)
        self.total.setStyleSheet(f"color:{theme.ACCENT_LINE}")
        trow = QHBoxLayout()
        trow.addWidget(QLabel("TOTAL"))
        trow.addStretch(1)
        trow.addWidget(self.total)
        v.addLayout(trow)
        self.unit = QLabel("")
        self.unit.setAlignment(Qt.AlignRight)
        self.unit.setObjectName("muted")
        v.addWidget(self.unit)
        br = QHBoxLayout()
        br.addWidget(_btn("Copy quote", self.copy_quote, "Plain-text quote to the clipboard"))
        br.addWidget(_btn("Save CSV…", self.save_csv))
        br.addWidget(_btn("Log to history", self.log_history, "Keep this quote in the History tab"))
        br.addStretch(1)
        v.addLayout(br)
        rv.addWidget(c, 3)

        c, v = _card()
        v.addWidget(_h("QUANTITY BREAKS"))
        self.breaks = QTableWidget(0, 3)
        self.breaks.setHorizontalHeaderLabels(["Qty", "Per part", "Batch"])
        self.breaks.verticalHeader().setVisible(False)
        self.breaks.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.breaks.setEditTriggers(QTableWidget.NoEditTriggers)
        self.breaks.setMaximumHeight(190)
        v.addWidget(self.breaks)
        rv.addWidget(c, 2)

        c, v = _card()
        v.addWidget(_h("SCHEDULE THIS JOB"))
        srow = QHBoxLayout()
        self.book_machine = QComboBox()
        srow.addWidget(self.book_machine, 1)
        self.book_date = QDateEdit(QDate.currentDate())
        self.book_date.setCalendarPopup(True)
        srow.addWidget(self.book_date)
        srow.addWidget(_btn("Next free", self._next_free, "Earliest day the machine has free hours"))
        srow.addWidget(_btn("Add to schedule", self.book, primary=True))
        v.addLayout(srow)
        self.book_lab = QLabel("")
        self.book_lab.setObjectName("hint")
        v.addWidget(self.book_lab)
        rv.addWidget(c)
        h.addWidget(right, 1)
        self._build_form()
        return outer

    # ------------------------------------------------------------------ data
    def doc(self):
        return self.doc_fn()

    def refresh(self):
        d = self.doc()
        cur = self.part.currentData()
        self.part.blockSignals(True)
        self.part.clear()
        for o in d.objects:
            if d.result.bodies.get(o.name) is not None:
                self.part.addItem(o.name, o.name)
        want = cur if cur and self.part.findData(cur) >= 0 else d.active_object
        i = self.part.findData(want)
        self.part.setCurrentIndex(max(i, 0))
        self.part.blockSignals(False)
        self._machines_to_combos()
        self.load_part()

    def load_part(self):
        d = self.doc()
        name = self.part.currentData()
        if not name:
            self.geom = None
            self.geom_lab.setText("No solid to quote — model or import a part first.")
            self.recalc()
            return
        try:
            self.geom = Q.part_geometry(d.result.bodies[name])
        except Exception as e:  # noqa: BLE001
            self.geom = None
            self.geom_lab.setText(f"Couldn't measure {name}: {e}")
            return
        g = self.geom
        self.cycle = Q.cam_cycle_min(d, name)
        bits = [f"{g['x']:.1f} × {g['y']:.1f} × {g['z']:.1f} mm", f"volume {g['volume'] / 1000:.2f} cm³",
                f"area {g['area'] / 100:.1f} cm²", f"{len(g['holes'])} vertical hole(s)"]
        if g.get("sheet"):
            bits.append(f"plate {g['thickness']:.2f} mm · cut {g['cut_length']:.0f} mm · {g['pierces']} pierce(s)")
        if self.cycle:
            bits.append(f"CAM cycle {self.cycle:.1f} min")
        self.geom_lab.setText(" · ".join(bits))
        self.set_process("sheet" if g.get("sheet") else "cnc", recalc=False)
        self.proc_hint.setText("Plate-like part → sheet / laser suggested" if g.get("sheet") else
                               "Solid part → CNC machining suggested")
        self.mat_lab.setText(d.material_of(name).name)
        self.recalc()

    def set_process(self, key, recalc=True):
        self.process = key
        b = self.proc_btns[key]
        if not b.isChecked():
            b.setChecked(True)
        self.use_cam.setVisible(key == "cnc")
        self._build_form()
        self._machines_to_combos()
        if recalc:
            self.recalc()

    def _build_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self._fields = {}
        p = self.params[self.process]
        fields = CNC_FIELDS if self.process == "cnc" else SHEET_FIELDS
        for key, label, unit, lo, hi, dec in fields:
            if key in ("thickness", "cut_length", "pierces"):
                val = p.get(key) or (self.geom or {}).get(key) or 0
            else:
                val = p.get(key, 0)
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(dec)
            sb.setValue(float(val))
            if unit:
                sb.setSuffix(f" {unit}")
            sb.valueChanged.connect(lambda v, k=key: self._param(k, v))
            if key in ("thickness", "cut_length", "pierces"):
                sb.setToolTip("Measured from the model — change to override")
            self.form.addRow(label, sb)
            self._fields[key] = sb

    def _param(self, key, v):
        self.params[self.process][key] = int(v) if key in ("setups", "tool_changes", "pierces") else v
        self.recalc()

    def _reset_params(self):
        self.params[self.process] = dict(Q.CNC_DEFAULTS if self.process == "cnc" else Q.SHEET_DEFAULTS)
        self._build_form()
        self.recalc()

    def _save_defaults(self):
        keep = {k: v for k, v in self.params[self.process].items() if k not in ("thickness", "cut_length", "pierces")}
        self.shop.data.setdefault("params", {})[self.process] = keep
        self.shop.save()
        self.status.emit("Quote parameters saved as defaults")

    def _pick_material(self):
        name = self.part.currentData()
        if not name:
            return
        from .materials_ui import MaterialDialog
        MaterialDialog(self.doc(), name, self).exec()
        self.mat_lab.setText(self.doc().material_of(name).name)
        self.recalc()

    def _machine_rate(self, _i):
        mid = self.machine.currentData()
        m = next((m for m in self.shop.machines if m["id"] == mid), None)
        if m and "rate" in self._fields:
            self._fields["rate"].setValue(float(m["rate"]))
            j = self.book_machine.findData(mid)
            if j >= 0:
                self.book_machine.setCurrentIndex(j)

    # ------------------------------------------------------------------ compute
    def current(self):
        if not self.geom:
            return None
        name = self.part.currentData()
        mat = self.doc().material_of(name)
        p = dict(self.params[self.process])
        if self.process == "cnc":
            p["use_cam"] = self.use_cam.isChecked()
        q = Q.quote(self.geom, mat, self.process, p, self.qty.value(),
                    getattr(self, "cycle", None) if self.process == "cnc" else None)
        q["object"] = name
        return q

    def recalc(self):
        q = self.last = self.current()
        self.block.setRowCount(0)
        self.breaks.setRowCount(0)
        if q is None:
            self.total.setText("$0.00")
            self.unit.setText("")
            return
        rows = [("Part", q["object"]), ("Process", "CNC machining" if q["process"] == "cnc" else "Sheet / laser"),
                ("Material", q["material"]), ("Quantity", str(q["qty"]))]
        if q["process"] == "cnc":
            rows += [("Stock", " × ".join(f"{v:.1f}" for v in q["stock_mm"]) + f" mm  ({q['stock_kg']:.2f} kg)"),
                     ("Roughing", f"{q['rough_min']:.1f} min" + ("  (CAM)" if q["time_source"].startswith("CAM")
                                                                else "")),
                     ("Finishing", f"{q['finish_min']:.1f} min"), ("Tool changes", f"{q['tool_min']:.1f} min"),
                     ("Time per part", f"{q['per_part_min']:.1f} min"), ("Setup (once)", f"{q['setup_min']:.0f} min")]
        else:
            rows += [("Thickness", f"{q['thickness']:.2f} mm"), ("Cut length", f"{q['cut_length']:.0f} mm"),
                     ("Cut speed", f"{q['cut_speed']:.0f} mm/min"), ("Pierces", str(q["pierces"])),
                     ("Time per part", f"{q['per_part_min']:.2f} min"),
                     ("Setup (once)", f"{q['setup_min']:.0f} min"), ("Sheet used", f"{q['sheet_kg']:.2f} kg")]
        rows += [("Batch machine time", f"{q['batch_hr']:.2f} h"), ("Machine cost", f"${q['machine_cost']:.2f}"),
                 ("Material cost", f"${q['material_cost']:.2f}"), ("Margin", f"${q['margin']:.2f}")]
        self.block.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            a = QTableWidgetItem(k)
            a.setForeground(QBrush(QColor(theme.MUTED)))
            b = QTableWidgetItem(v)
            b.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.block.setItem(i, 0, a)
            self.block.setItem(i, 1, b)
        self.total.setText(f"${q['total']:,.2f}")
        self.unit.setText(f"${q['unit']:,.2f} per part · {q['batch_hr']:.2f} h machine time")
        p = dict(self.params[self.process])
        brk = Q.qty_breaks(self.geom, self.doc().material_of(q["object"]), self.process, p,
                           getattr(self, "cycle", None) if self.process == "cnc" else None)
        self.breaks.setRowCount(len(brk))
        for i, (n, u) in enumerate(brk):
            for j, txt in enumerate((str(n), f"${u:,.2f}", f"${u * n:,.2f}")):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                if n == q["qty"]:
                    it.setForeground(QBrush(QColor(theme.ACCENT_LINE)))
                self.breaks.setItem(i, j, it)

    # ------------------------------------------------------------------ outputs
    def copy_quote(self):
        if self.last:
            QApplication.clipboard().setText(Q.text(self.last))
            self.status.emit("Quote copied to the clipboard")

    def save_csv(self):
        if not self.last:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save quote", f"quote_{self.last['object']}.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            for line in Q.text(self.last).splitlines():
                k, _, v = line.partition("  ")
                w.writerow([k.strip(), v.strip()])
            w.writerow([])
            w.writerow(["Qty", "Per part", "Batch"])
            for i in range(self.breaks.rowCount()):
                w.writerow([self.breaks.item(i, c).text() for c in range(3)])
        self.status.emit(f"Saved {Path(p).name}")

    def log_history(self):
        if self.last:
            self.shop.log(self.last)
            self._fill_history()
            self.status.emit("Quote logged")

    def book(self):
        if not self.last:
            return
        mid = self.book_machine.currentData()
        if not mid:
            QMessageBox.information(self, "Schedule", "Add a machine on the Machine schedule tab first.")
            return
        b = self.shop.book(mid, f"{self.last['object']} ×{self.last['qty']}", self.last["batch_hr"],
                           self.book_date.date().toString("yyyy-MM-dd"), round(self.last["total"], 2),
                           self.last["process"])
        self.shop.log(self.last)
        end = self.shop.finish_date(b)
        m = next(m for m in self.shop.machines if m["id"] == mid)
        self.book_lab.setText(f"Booked on {m['name']}: {b['start']} → finishes {end} ({b['hours']:.2f} h)")
        self.status.emit(f"Added to {m['name']} starting {b['start']}")
        self._fill_history()

    def _next_free(self):
        mid = self.book_machine.currentData()
        m = next((m for m in self.shop.machines if m["id"] == mid), None)
        if m and self.last:
            d = self.shop.next_free(m, self.last["batch_hr"])
            self.book_date.setDate(QDate.fromString(d, "yyyy-MM-dd"))

    # ================================================================== schedule tab
    def _schedule_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 10)
        c, cv = _card()
        cv.addWidget(_h("MACHINES"))
        add = QHBoxLayout()
        self.m_name = QLineEdit()
        self.m_name.setPlaceholderText("Name, e.g. Haas VF-2")
        self.m_type = QComboBox()
        self.m_type.addItem("CNC machining", "cnc")
        self.m_type.addItem("Sheet / laser", "sheet")
        self.m_rate = QDoubleSpinBox()
        self.m_rate.setRange(0, 1e4)
        self.m_rate.setValue(75)
        self.m_rate.setSuffix(" $/h")
        self.m_hours = QDoubleSpinBox()
        self.m_hours.setRange(0.5, 24)
        self.m_hours.setValue(8)
        self.m_hours.setSuffix(" h/day")
        for x in (self.m_name, self.m_type, self.m_rate, self.m_hours):
            add.addWidget(x)
        add.addWidget(_btn("+ Add machine", self._add_machine, primary=True))
        cv.addLayout(add)
        self.m_table = QTableWidget(0, 5)
        self.m_table.setHorizontalHeaderLabels(["Machine", "Type", "Rate", "Hours/day", ""])
        self.m_table.verticalHeader().setVisible(False)
        self.m_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.m_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.m_table.setMaximumHeight(170)
        cv.addWidget(self.m_table)
        v.addWidget(c)

        c, cv = _card()
        head = QHBoxLayout()
        head.addWidget(_h("CAPACITY — BOOKED / AVAILABLE HOURS"))
        head.addStretch(1)
        self.week = 0
        head.addWidget(_btn("◀", lambda: self._shift_week(-1)))
        head.addWidget(_btn("Today", lambda: self._shift_week(None)))
        head.addWidget(_btn("▶", lambda: self._shift_week(1)))
        cv.addLayout(head)
        self.cal = QTableWidget(0, 14)
        self.cal.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cal.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        cv.addWidget(self.cal, 1)
        v.addWidget(c, 2)

        c, cv = _card()
        cv.addWidget(_h("SCHEDULED JOBS"))
        self.b_table = QTableWidget(0, 7)
        self.b_table.setHorizontalHeaderLabels(["Job", "Machine", "Start", "Finish", "Hours", "Quote", ""])
        self.b_table.verticalHeader().setVisible(False)
        self.b_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.b_table.setEditTriggers(QTableWidget.NoEditTriggers)
        cv.addWidget(self.b_table, 1)
        v.addWidget(c, 2)
        return w

    def _add_machine(self):
        try:
            self.shop.add_machine(self.m_name.text(), self.m_type.currentData(), self.m_rate.value(),
                                  self.m_hours.value())
        except ValueError as e:
            QMessageBox.information(self, "Machine", str(e))
            return
        self.m_name.clear()
        self._fill_schedule()
        self._machines_to_combos()

    def _del_machine(self, mid):
        m = next((m for m in self.shop.machines if m["id"] == mid), None)
        if m and QMessageBox.question(self, "Delete machine", f"Delete {m['name']} and its bookings?") == \
                QMessageBox.Yes:
            self.shop.remove_machine(mid)
            self._fill_schedule()
            self._machines_to_combos()

    def _del_booking(self, bid):
        self.shop.remove_booking(bid)
        self._fill_schedule()

    def _shift_week(self, d):
        self.week = 0 if d is None else self.week + d
        self._fill_calendar()

    def _machines_to_combos(self):
        for combo, empty in ((self.machine, "— machine rate —"), (self.book_machine, "No machines yet")):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            ms = [m for m in self.shop.machines if m["type"] == self.process] or self.shop.machines
            if not ms:
                combo.addItem(empty, None)
            for m in ms:
                combo.addItem(f"{m['name']}  (${m['rate']:.0f}/h)", m["id"])
            i = combo.findData(cur)
            combo.setCurrentIndex(max(i, 0))
            combo.blockSignals(False)

    def _fill_schedule(self):
        ms = self.shop.machines
        self.m_table.setRowCount(len(ms))
        for i, m in enumerate(ms):
            for j, txt in enumerate((m["name"], "CNC" if m["type"] == "cnc" else "Sheet / laser",
                                     f"${m['rate']:.2f}/h", f"{m['daily_hours']:g}")):
                self.m_table.setItem(i, j, QTableWidgetItem(txt))
            self.m_table.setCellWidget(i, 4, _btn("Delete", lambda _=False, mid=m["id"]: self._del_machine(mid)))
        self._fill_calendar()
        bs = sorted(self.shop.bookings, key=lambda b: b["start"])
        names = {m["id"]: m["name"] for m in ms}
        self.b_table.setRowCount(len(bs))
        for i, b in enumerate(bs):
            vals = (b["job"], names.get(b["machine"], "?"), b["start"], self.shop.finish_date(b),
                    f"{b['hours']:.2f}", f"${b['total']:.2f}" if b.get("total") is not None else "")
            for j, txt in enumerate(vals):
                self.b_table.setItem(i, j, QTableWidgetItem(txt))
            self.b_table.setCellWidget(i, 6, _btn("Remove", lambda _=False, bid=b["id"]: self._del_booking(bid)))

    def _fill_calendar(self):
        ms = self.shop.machines
        start = date.today() + timedelta(days=7 * self.week)
        days = [start + timedelta(days=i) for i in range(14)]
        self.cal.setRowCount(len(ms))
        self.cal.setHorizontalHeaderLabels([d.strftime("%a\n%d %b") for d in days])
        self.cal.setVerticalHeaderLabels([m["name"] for m in ms])
        for i, m in enumerate(ms):
            used = self.shop.allocation(m)
            cap = float(m["daily_hours"])
            for j, d in enumerate(days):
                u = used.get(d.isoformat(), 0.0)
                it = QTableWidgetItem(f"{u:.1f}/{cap:g}" if u else "")
                it.setTextAlignment(Qt.AlignCenter)
                frac = u / cap if cap else 0
                col = theme.OK if frac < 0.5 else (theme.WARN if frac < 0.999 else theme.ERROR)
                if u:
                    c = QColor(col)
                    c.setAlpha(90)
                    it.setBackground(QBrush(c))
                it.setToolTip(f"{m['name']} · {d.isoformat()} · {u:.2f} of {cap:g} h booked")
                self.cal.setItem(i, j, it)

    # ================================================================== history tab
    def _history_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        row.addWidget(_h("QUOTE HISTORY"))
        row.addStretch(1)
        row.addWidget(_btn("Export CSV…", self._export_history))
        row.addWidget(_btn("Clear", self._clear_history))
        v.addLayout(row)
        self.h_table = QTableWidget(0, 8)
        self.h_table.setHorizontalHeaderLabels(["Time", "Job", "Process", "Material", "Qty", "Hours", "Per part",
                                                "Total"])
        self.h_table.verticalHeader().setVisible(False)
        self.h_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.h_table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.h_table, 1)
        return w

    def _fill_history(self):
        hs = list(reversed(self.shop.history))
        self.h_table.setRowCount(len(hs))
        for i, hrow in enumerate(hs):
            vals = (hrow["time"], hrow["job"], "CNC" if hrow["process"] == "cnc" else "Sheet", hrow["material"],
                    str(hrow["qty"]), f"{hrow['hours']:.2f}", f"${hrow['unit']:.2f}", f"${hrow['total']:.2f}")
            for j, txt in enumerate(vals):
                self.h_table.setItem(i, j, QTableWidgetItem(txt))

    def _export_history(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export quote history", "quote_history.csv", "CSV (*.csv)")
        if p:
            with open(p, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["time", "job", "process", "material", "qty", "hours", "unit",
                                                   "total"])
                w.writeheader()
                w.writerows(self.shop.history)

    def _clear_history(self):
        if QMessageBox.question(self, "History", "Clear the quote history?") == QMessageBox.Yes:
            self.shop.data["history"] = []
            self.shop.save()
            self._fill_history()

    def _tab_changed(self, i):
        if i == 1:
            self._fill_schedule()
        elif i == 2:
            self._fill_history()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self._fill_history()
