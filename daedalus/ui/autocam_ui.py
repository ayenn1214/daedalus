"""Auto CAM dialog: analyse the part, ask what the machine / shop needs, generate the program."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..manufacturing import autocam as A
from ..manufacturing import cam
from . import theme

ORIENT_TEXT = {"+Z": "+Z — top as modelled", "-Z": "−Z — flipped (bottom up)", "+Y": "+Y (back face up)",
               "-Y": "−Y (front face up)", "+X": "+X (right face up)", "-X": "−X (left face up)"}


class AutoCamDialog(QDialog):
    backplot_requested = Signal(str)
    quote_requested = Signal()

    def __init__(self, doc, settings, parent=None):
        super().__init__(parent)
        self.doc, self.settings = doc, settings
        self.setWindowTitle("Auto CAM — analyse and generate G-code")
        self.resize(1080, 720)
        self.result = None
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        # ---- left: analysis
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Part"))
        self.part = QComboBox()
        for o in doc.objects:
            if doc.result.bodies.get(o.name) is not None:
                self.part.addItem(o.name, o.name)
        i = self.part.findData(doc.active_object)
        self.part.setCurrentIndex(max(i, 0))
        self.part.currentIndexChanged.connect(lambda _i: self.analyse())
        row.addWidget(self.part, 1)
        lv.addLayout(row)
        self.report = QTextBrowser()
        self.report.setMinimumWidth(300)
        lv.addWidget(self.report, 1)
        split.addWidget(left)

        # ---- right: questions
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        f = QFormLayout()
        s = settings

        def val(key, default):
            v = s.value(f"autocam/{key}", default)
            return type(default)(v) if not isinstance(default, bool) else (v in (True, "true", "1", 1))

        self.orient = QComboBox()
        for k, t in ORIENT_TEXT.items():
            self.orient.addItem(t, k)
        self.orient.currentIndexChanged.connect(lambda _i: self._show_side())
        f.addRow("Machine from", self.orient)
        self.post = QComboBox()
        for k, v in cam.POSTS.items():
            self.post.addItem(v, k)
        self.post.setCurrentIndex(max(0, self.post.findData(val("post", "fanuc"))))
        f.addRow("Controller", self.post)
        self.rpm = QSpinBox()
        self.rpm.setRange(500, 60000)
        self.rpm.setSingleStep(500)
        self.rpm.setValue(val("max_rpm", 10000))
        self.rpm.setSuffix(" rpm max")
        f.addRow("Spindle", self.rpm)
        self.units = QComboBox()
        self.units.addItems(["mm", "in"])
        self.units.setCurrentText(val("units", "mm"))
        self.origin = QComboBox()
        self.origin.addItem("Stock top, front-left corner", "corner")
        self.origin.addItem("Stock top, centre", "center")
        self.origin.setCurrentIndex(max(0, self.origin.findData(val("origin", "corner"))))
        ou = QHBoxLayout()
        ou.addWidget(self.origin, 1)
        ou.addWidget(self.units)
        f.addRow("G54 / units", ou)
        mr = QHBoxLayout()
        self.mxy = QDoubleSpinBox()
        self.mxy.setRange(0, 100)
        self.mxy.setValue(val("margin_xy", 3.0))
        self.mxy.setSuffix(" mm sides")
        self.mz = QDoubleSpinBox()
        self.mz.setRange(0, 50)
        self.mz.setValue(val("margin_z", 1.0))
        self.mz.setSuffix(" mm top")
        mr.addWidget(self.mxy)
        mr.addWidget(self.mz)
        f.addRow("Stock allowance", mr)
        self.mills = QLineEdit(val("endmills", "16, 12, 10, 8, 6, 4, 3"))
        self.mills.setToolTip("End mill diameters you have (mm), any order")
        f.addRow("End mills (Ø mm)", self.mills)
        self.drills = QLineEdit(val("drills", "2.5, 3.3, 4.2, 5, 5.5, 6, 6.6, 6.8, 8, 8.5, 9, 10, 10.2, 11, 12, "
                                              "13.5, 14, 16, 17.5, 18, 20"))
        self.drills.setToolTip("Drill diameters you have (mm). Missing sizes get the next smaller drill + a "
                               "warning to ream / bore")
        f.addRow("Drills (Ø mm)", self.drills)
        self.facemill = QDoubleSpinBox()
        self.facemill.setRange(0, 200)
        self.facemill.setValue(val("face_mill", 50.0))
        self.facemill.setSuffix(" mm (0 = use the largest end mill)")
        f.addRow("Face mill", self.facemill)
        rv.addLayout(f)
        rv.addWidget(QLabel("<b>Operations</b>"))
        grid = QHBoxLayout()
        self.chk = {}
        for k, t in (("facing", "Face top"), ("pockets", "Pockets"), ("spot", "Spot drill"),
                     ("drilling", "Drill holes"), ("tapping", "Tap threads"), ("profile", "Cut out (profile)")):
            c = QCheckBox(t)
            c.setChecked(val(k, True))
            self.chk[k] = c
            grid.addWidget(c)
        rv.addLayout(grid)
        cy = QHBoxLayout()
        self.cycle = QComboBox()
        for k, v in cam.CYCLES.items():
            self.cycle.addItem(f"{k} · {v}" if k != "auto" else v, k)
        self.cycle.setCurrentIndex(max(0, self.cycle.findData(val("cycle", "auto"))))
        self.retract = QComboBox()
        self.retract.addItem("G98 initial", "initial")
        self.retract.addItem("G99 R plane", "r")
        self.retract.setCurrentIndex(max(0, self.retract.findData(val("retract", "initial"))))
        self.coolant = QCheckBox("Coolant")
        self.coolant.setChecked(val("coolant", True))
        self.prog = QSpinBox()
        self.prog.setRange(1, 99999)
        self.prog.setValue(val("program", 1000))
        self.prog.setPrefix("O")
        cy.addWidget(QLabel("Drill cycle"))
        cy.addWidget(self.cycle, 1)
        cy.addWidget(self.retract)
        cy.addWidget(self.coolant)
        cy.addWidget(self.prog)
        rv.addLayout(cy)
        gen = QPushButton("Generate program")
        gen.setObjectName("primary")
        gen.clicked.connect(self.generate)
        rv.addWidget(gen)
        self.ops = QTableWidget(0, 6)
        self.ops.setHorizontalHeaderLabels(["Operation", "Tool", "rpm", "Feed", "Holes", "Min"])
        self.ops.verticalHeader().setVisible(False)
        self.ops.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ops.setEditTriggers(QTableWidget.NoEditTriggers)
        rv.addWidget(self.ops, 1)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setTextFormat(Qt.RichText)
        rv.addWidget(self.warn)
        out = QHBoxLayout()
        self.save_btn = QPushButton("Save G-code…")
        self.save_btn.clicked.connect(self.save)
        self.plot_btn = QPushButton("Backplot")
        self.plot_btn.clicked.connect(self.backplot)
        self.second_btn = QPushButton("Second setup →")
        self.second_btn.setToolTip("Program the other side (features this side can't reach)")
        self.second_btn.clicked.connect(self.second_setup)
        self.quote_btn = QPushButton("Quote")
        self.quote_btn.clicked.connect(lambda: (self.quote_requested.emit(), self.accept()))
        for b in (self.save_btn, self.plot_btn, self.second_btn, self.quote_btn):
            b.setEnabled(False)
            out.addWidget(b)
        out.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        out.addWidget(close)
        rv.addLayout(out)
        split.addWidget(right)
        split.setSizes([380, 700])
        self.analyse()

    # ------------------------------------------------------------------ analysis
    def analyse(self):
        name = self.part.currentData()
        if not name:
            self.report.setHtml("<p>No solid part to machine.</p>")
            return
        body = self.doc.placed_bodies().get(name)
        threads = [t for t in self.doc.result.threads.values() if t.get("object", name) == name]
        try:
            self.rec = A.recommend(body, threads)
        except Exception as e:  # noqa: BLE001
            self.report.setHtml(f"<p>Couldn't analyse {name}: {e}</p>")
            return
        best = self.rec["best"]["orient"]
        self.orient.blockSignals(True)
        self.orient.setCurrentIndex(self.orient.findData(best))
        self.orient.blockSignals(False)
        self._show_side()

    def _show_side(self):
        o = self.orient.currentData()
        a = next(x for x in self.rec["all"] if x["orient"] == o)
        mat = self.doc.material_of(self.part.currentData()).name
        best = self.rec["best"]["orient"]
        html = [f"<h3>{self.part.currentData()}</h3><p style='color:{theme.MUTED}'>{mat}</p>"]
        html.append("<p><b>Recommended:</b> machine from " + ORIENT_TEXT[best] +
                    (" — this side" if o == best else f" (you picked {o})") + "</p>")
        html.append("<ul>" + "".join(f"<li>{x}</li>" for x in A.describe(a)) + "</ul>")
        if self.rec["second"] is not None and o == best:
            sec = self.rec["second"]
            only = [h for h in sec["holes"] if not h["through"]]
            html.append(f"<p style='color:{theme.WARN}'><b>Needs a second setup</b> from {sec['orient']}: "
                        f"{len(only)} hole(s), {len(sec['floors'])} floor(s) only reachable from there.</p>")
        html.append("<p><b>Answer on the right</b> — controller, spindle, stock allowance, the end mills and "
                    "drills you have — then <b>Generate program</b>.</p>")
        self.report.setHtml("".join(html))

    # ------------------------------------------------------------------ generate
    def options(self):
        return {"orient": self.orient.currentData(), "post": self.post.currentData(), "max_rpm": self.rpm.value(),
                "units": self.units.currentText(), "origin": self.origin.currentData(),
                "margin": [self.mxy.value(), self.mxy.value(), self.mz.value()], "endmills": self.mills.text(),
                "drills": self.drills.text(), "face_mill": self.facemill.value(),
                "cycle": self.cycle.currentData(), "retract": self.retract.currentData(),
                "coolant": self.coolant.isChecked(), "program": self.prog.value(),
                "second": self.orient.currentData() != self.rec["best"]["orient"] and
                self.rec.get("second") is not None and self.orient.currentData() == self.rec["second"]["orient"],
                **{k: c.isChecked() for k, c in self.chk.items()}}

    def _remember(self, o):
        for k, v in (("post", o["post"]), ("max_rpm", o["max_rpm"]), ("units", o["units"]),
                     ("origin", o["origin"]), ("margin_xy", self.mxy.value()), ("margin_z", self.mz.value()),
                     ("endmills", o["endmills"]), ("drills", o["drills"]), ("face_mill", o["face_mill"]),
                     ("cycle", o["cycle"]), ("retract", o["retract"]), ("coolant", o["coolant"]),
                     ("program", o["program"]), *[(k, c.isChecked()) for k, c in self.chk.items()]):
            self.settings.setValue(f"autocam/{k}", v)

    def generate(self):
        o = self.options()
        self._remember(o)
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.result = A.build(self.doc, self.part.currentData(), o)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Auto CAM", str(e))
            return
        QApplication.restoreOverrideCursor()
        s = self.result["summary"]
        self.ops.setRowCount(len(s["operations"]))
        for i, op in enumerate(s["operations"]):
            vals = (op["name"], op["tool"], str(op["rpm"]), f"{op['feed_mm_min']:g}",
                    str(op["holes"] or ""), f"{op['time_min']:.1f}")
            for j, v in enumerate(vals):
                self.ops.setItem(i, j, QTableWidgetItem(v))
        w = self.result["warnings"]
        self.warn.setText(f"<b>Cycle ≈ {s['cycle_time_min']:.1f} min</b> · stock "
                          f"{' × '.join(f'{v:g}' for v in s['stock_mm'])} mm · {len(self.result['tools'])} tools"
                          + ("".join(f"<br><span style='color:{theme.WARN}'>⚠ {x}</span>" for x in w) if w else
                             f"<br><span style='color:{theme.OK}'>✔ Everything reachable from this side is "
                             "programmed</span>"))
        for b in (self.save_btn, self.plot_btn, self.quote_btn):
            b.setEnabled(True)
        self.second_btn.setEnabled(self.rec.get("second") is not None)

    def save(self):
        if not self.result:
            return
        stem = (self.doc.path.stem if self.doc.path else self.part.currentData()) + \
            ("" if self.orient.currentData() == "+Z" else f"_{self.orient.currentData().replace('+', 'p').replace('-', 'm')}")
        ext = ".mpf" if self.post.currentData() == "siemens" else ".nc"
        p, _ = QFileDialog.getSaveFileName(self, "Save G-code", stem + ext,
                                           "G-code (*.nc *.tap *.ngc *.mpf *.gcode)")
        if p:
            s = self.doc.export_gcode(p)
            self._saved = p
            self.warn.setText(self.warn.text() + f"<br>Saved {Path(p).name} · {s['lines']} lines")
            return p
        return None

    def backplot(self):
        import tempfile
        p = getattr(self, "_saved", None)
        if not p:
            p = str(Path(tempfile.mkdtemp()) / "autocam.nc")
            self.doc.export_gcode(p)
        self.backplot_requested.emit(p)

    def second_setup(self):
        sec = self.rec.get("second")
        if sec is None:
            return
        self.orient.setCurrentIndex(self.orient.findData(sec["orient"]))
        self.chk["facing"].setChecked(True)
        self.chk["profile"].setChecked(False)
        self.prog.setValue(self.prog.value() + 1)
        self.generate()
