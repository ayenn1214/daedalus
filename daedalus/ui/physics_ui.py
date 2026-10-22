"""Physics panel: settings, motors/springs on mates, run (background), playback, trace plot."""
from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.features import FeatureError
from ..core.physics import DRIVE_KINDS
from .theme import ACCENT, BORDER, MUTED, PANEL, TEXT

GRAVITY = {"−Z (down)": [0, 0, -9.81], "−Y": [0, -9.81, 0], "−X": [-9.81, 0, 0], "Off": [0, 0, 0]}
COLL = {"Exact shapes": "decompose", "Convex hulls (fast)": "hull", "No contacts": "off"}


def drive_units(mate_type, kind):
    rot = mate_type in ("hinge", "concentric", "angle")
    return {"speed": "deg/s" if rot else "mm/s", "torque": "N·m" if rot else "N",
            "position": "deg" if rot else "mm", "spring": "N·m/deg" if rot else "N/mm"}[kind]


class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, doc):
        super().__init__()
        self.doc = doc
        self.stop = False

    def run(self):
        try:
            r = self.doc.simulate(progress=self.progress.emit, cancel=lambda: self.stop)
            self.done.emit(r)
        except FeatureError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e.__class__.__name__}: {e}")


class TracePlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.t, self.y, self.cursor, self.title = [], [], 0.0, ""
        self.xunit = "s"
        self.empty = "Run a simulation to see results"

    def set_data(self, t, y, title):
        self.t, self.y, self.title = list(t), list(y), title
        self.update()

    def set_cursor(self, t):
        self.cursor = t
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(44, 8, -8, -18)
        p.fillRect(self.rect(), QColor(PANEL))
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawRect(r)
        if len(self.t) < 2:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, self.empty)
            return
        t0, t1 = self.t[0], self.t[-1] or 1.0
        lo, hi = min(self.y), max(self.y)
        if hi - lo < 1e-12:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

        def X(t):
            return r.left() + (t - t0) / ((t1 - t0) or 1) * r.width()

        def Y(v):
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        p.setPen(QColor(MUTED))
        f = p.font()
        f.setPointSizeF(8)
        p.setFont(f)
        for v in (lo + pad, hi - pad):
            p.drawText(2, int(Y(v)) + 4, f"{v:.3g}")
        u = f" {self.xunit}" if self.xunit else ""
        p.drawText(r.left(), self.height() - 4, f"{t0:.4g}{u}")
        p.drawText(r.right() - 60, self.height() - 4, f"{t1:.4g}{u}")
        p.drawText(r.left() + 40, r.top() + 12, self.title)
        if lo < 0 < hi:
            p.setPen(QPen(QColor(BORDER), 1, Qt.DashLine))
            p.drawLine(r.left(), int(Y(0)), r.right(), int(Y(0)))
        path = QPainterPath(QPointF(X(self.t[0]), Y(self.y[0])))
        for t, v in zip(self.t[1:], self.y[1:]):
            path.lineTo(QPointF(X(t), Y(v)))
        p.setPen(QPen(QColor(ACCENT), 1.6))
        p.drawPath(path)
        p.setPen(QPen(QColor(TEXT), 1))
        x = X(self.cursor)
        p.drawLine(int(x), r.top(), int(x), r.bottom())


class PhysicsPanel(QWidget):
    status = Signal(str)

    def __init__(self, doc, viewport, parent=None):
        super().__init__(parent)
        self.doc, self.vp = doc, viewport
        self.result = None
        self._seen = None
        self.i = 0
        self.thread = None
        self.worker = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(5)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.gravity = QComboBox()
        self.gravity.addItems(list(GRAVITY) + ["Custom"])
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.05, 120)
        self.duration.setSuffix(" s")
        self.duration.setDecimals(2)
        self.friction = QDoubleSpinBox()
        self.friction.setRange(0, 2)
        self.friction.setSingleStep(0.05)
        self.coll = QComboBox()
        self.coll.addItems(list(COLL))
        row = QHBoxLayout()
        row.addWidget(self.gravity, 2)
        row.addWidget(QLabel("for"))
        row.addWidget(self.duration, 1)
        form.addRow("Gravity", row)
        row2 = QHBoxLayout()
        row2.addWidget(self.coll, 2)
        row2.addWidget(QLabel("μ"))
        row2.addWidget(self.friction, 1)
        form.addRow("Contacts", row2)
        lay.addLayout(form)
        self.gravity.activated.connect(self._commit_settings)
        self.duration.editingFinished.connect(self._commit_settings)
        self.friction.editingFinished.connect(self._commit_settings)
        self.coll.activated.connect(self._commit_settings)

        hdr = QLabel("Motors & springs on mates")
        hdr.setObjectName("muted")
        lay.addWidget(hdr)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Mate", "Drive", "Value", ""])
        self.table.verticalHeader().hide()
        self.table.setMaximumHeight(120)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self.table)
        r3 = QHBoxLayout()
        self.add_mate = QComboBox()
        add = QPushButton("+ Drive")
        add.setToolTip("Motor (speed / torque / position) or spring on the chosen mate")
        add.clicked.connect(self._add_drive)
        r3.addWidget(self.add_mate, 1)
        r3.addWidget(add)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        self.run_btn = QPushButton("▶ Simulate")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QPushButton("■")
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(self.stop)
        r4.addWidget(self.run_btn, 1)
        r4.addWidget(self.stop_btn)
        lay.addLayout(r4)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        r5 = QHBoxLayout()
        self.play_btn = QPushButton("⏵")
        self.play_btn.setFixedWidth(34)
        self.play_btn.clicked.connect(self.toggle_play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self._seek)
        self.tlabel = QLabel("0.00 s")
        self.speed = QComboBox()
        self.speed.addItems(["¼×", "½×", "1×", "2×"])
        self.speed.setCurrentIndex(2)
        self.loop = QCheckBox("Loop")
        r5.addWidget(self.play_btn)
        r5.addWidget(self.slider, 1)
        r5.addWidget(self.tlabel)
        lay.addLayout(r5)
        r6 = QHBoxLayout()
        r6.addWidget(self.speed)
        r6.addWidget(self.loop)
        r6.addStretch(1)
        self.keep_btn = QPushButton("Keep position")
        self.keep_btn.setToolTip("Move the objects to the frame shown (undoable)")
        self.keep_btn.clicked.connect(self.keep)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Show the objects where they are in the model")
        self.reset_btn.clicked.connect(self.reset_view)
        r6.addWidget(self.keep_btn)
        r6.addWidget(self.reset_btn)
        lay.addLayout(r6)
        self.trace = QComboBox()
        self.trace.currentTextChanged.connect(self._plot)
        lay.addWidget(self.trace)
        self.plot = TracePlot()
        lay.addWidget(self.plot, 1)
        self._playback_enabled(False)
        self.stop_btn.setEnabled(False)
        self.refresh()

    # ---------------------------------------------------------------- doc
    def refresh(self):
        ph = self.doc.physics
        g = [float(v) for v in ph.gravity]
        name = next((k for k, v in GRAVITY.items() if v == g), "Custom")
        self.gravity.setCurrentText(name)
        self.duration.setValue(ph.duration)
        self.friction.setValue(ph.friction)
        self.coll.setCurrentText(next(k for k, v in COLL.items() if v == ph.collisions))
        mates = {m.name: m for m in self.doc.mates}
        self.add_mate.clear()
        self.add_mate.addItems([n for n in mates if not any(d.mate == n for d in ph.drives)])
        self.table.setRowCount(0)
        for d in ph.drives:
            m = mates.get(d.mate)
            if m is None:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = QTableWidgetItem(d.mate)
            it.setToolTip(f"{m.type} mate: {m.a['obj']} ↔ {m.b['obj']}")
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, it)
            kind = QComboBox()
            kind.addItems(list(DRIVE_KINDS))
            kind.setCurrentText(d.kind)
            kind.setMinimumWidth(84)
            kind.activated.connect(lambda _i, mn=d.mate, w=kind: self._set(mn, kind=w.currentText()))
            self.table.setCellWidget(r, 1, kind)
            val = QDoubleSpinBox()
            val.setRange(-1e7, 1e7)
            val.setDecimals(3)
            val.setValue(d.value)
            val.setSuffix(" " + drive_units(m.type, d.kind))
            val.setMinimumWidth(104)
            val.setKeyboardTracking(False)
            val.valueChanged.connect(lambda v, mn=d.mate: self._set(mn, value=v))
            self.table.setCellWidget(r, 2, val)
            rm = QPushButton("✕")
            rm.setFixedWidth(26)
            rm.clicked.connect(lambda _=False, mn=d.mate: self._remove(mn))
            self.table.setCellWidget(r, 3, rm)
        if self.doc.last_sim is not None and self.doc.last_sim is not self._seen and self.thread is None:
            self.load(self.doc.last_sim)               # e.g. Claude ran one: play it
            self.play()

    def _commit_settings(self, *_):
        g = GRAVITY.get(self.gravity.currentText())
        props = dict(duration=self.duration.value(), friction=self.friction.value(),
                     collisions=COLL[self.coll.currentText()])
        if g is not None:
            props["gravity"] = g
        cur = self.doc.physics
        if (props["duration"], props["friction"], props["collisions"], props.get("gravity", cur.gravity)) == \
                (cur.duration, cur.friction, cur.collisions, [float(v) for v in cur.gravity]):
            return
        self.doc.set_physics(**props)

    def _set(self, mate, kind=None, value=None):
        d = next(x for x in self.doc.physics.drives if x.mate == mate)
        self.doc.set_drive(mate, kind or d.kind, d.value if value is None else value, d.damping, d.max)

    def _remove(self, mate):
        self.doc.set_drive(mate, None)

    def _add_drive(self):
        n = self.add_mate.currentText()
        if not n:
            self.info.setText("Add mates first (Assembly ▸ Mate), then drive one.")
            return
        m = self.doc.mate(n)
        self.doc.set_drive(n, "speed", 90.0 if m.type in ("hinge", "concentric", "angle") else 50.0)

    # ---------------------------------------------------------------- run
    def run(self):
        if self.thread is not None:
            return
        self.pause()
        self.reset_view()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.info.setText("Preparing…")
        self.thread = QThread(self)
        self.worker = _Worker(self.doc)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.info.setText)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def stop(self):
        if self.worker is not None:
            self.worker.stop = True
        self.pause()

    def _finish_thread(self):
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(5000)
        self.thread = self.worker = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _failed(self, msg):
        self._finish_thread()
        self.info.setText("⚠ " + msg)
        self.status.emit("Simulation failed")

    def _done(self, r):
        self._finish_thread()
        self.doc.last_sim = r
        self.load(r)
        self.play()

    def load(self, r):
        self._seen = r
        self.result = r
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(r.frames) - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.i = 0
        s = r.summary
        lines = []
        if s.get("contacts_between"):
            lines.append("Contacts: " + ", ".join(s["contacts_between"][:4]))
        moved = s.get("moved_mm", {})
        if moved:
            top = sorted(moved.items(), key=lambda kv: -kv[1])[:3]
            lines.append("Moved: " + ", ".join(f"{n} {v:.1f} mm" for n, v in top))
        lines += r.notes[:3]
        self.info.setText(("✓ " if r.ok else "⚠ ") + f"{s.get('duration_s', 0):.2f} s simulated · "
                          + ("settled" if s.get("settled") else "still moving") + ("\n" + "\n".join(lines)
                                                                                   if lines else ""))
        cur = self.trace.currentText()
        self.trace.blockSignals(True)
        self.trace.clear()
        names = list(r.traces)
        names = [n for n in names if not n.startswith("contacts")] + ["contacts"]
        self.trace.addItems(names)
        pref = next((n for n in names if "position" in n), names[0] if names else "")
        self.trace.setCurrentText(cur if cur in names else pref)
        self.trace.blockSignals(False)
        self._plot(self.trace.currentText())
        self._playback_enabled(True)
        self._show(0)
        self.status.emit(f"Simulated {s.get('duration_s', 0):.2f} s")

    def _plot(self, name):
        if self.result and name in self.result.traces:
            self.plot.set_data(self.result.times, self.result.traces[name], name)

    # ----------------------------------------------------------- playback
    def _playback_enabled(self, on):
        for w in (self.play_btn, self.slider, self.keep_btn, self.reset_btn, self.speed, self.loop):
            w.setEnabled(on)

    def toggle_play(self):
        if self.timer.isActive():
            self.pause()
        else:
            self.play()

    def play(self):
        if not self.result:
            return
        if self.i >= len(self.result.frames) - 1:
            self.i = 0
        self.timer.start(1000 // 60)
        self._clock = 0.0
        self.play_btn.setText("⏸")

    def pause(self):
        self.timer.stop()
        self.play_btn.setText("⏵")

    def _tick(self):
        r = self.result
        k = [0.25, 0.5, 1.0, 2.0][self.speed.currentIndex()]
        t = r.times[self.i] + k / 60.0
        j = self.i
        while j < len(r.times) - 1 and r.times[j + 1] <= t + 1e-9:
            j += 1
        if j == self.i and self.i < len(r.times) - 1:
            j += 1
        if j >= len(r.times) - 1:
            j = len(r.times) - 1
            if self.loop.isChecked():
                self._show(j)
                self.i = 0
                return
            self.pause()
        self._show(j)

    def _seek(self, v):
        if self.result:
            self._show(v, from_slider=True)

    def _show(self, i, from_slider=False):
        r = self.result
        self.i = i
        self.vp.update_poses(r.frames[i])
        self.tlabel.setText(f"{r.times[i]:.2f} s")
        self.plot.set_cursor(r.times[i])
        if not from_slider:
            self.slider.blockSignals(True)
            self.slider.setValue(i)
            self.slider.blockSignals(False)

    def keep(self):
        if self.result:
            self.pause()
            self.doc.apply_frame(self.result.frames[self.i])
            self.status.emit(f"Kept positions at {self.result.times[self.i]:.2f} s (Ctrl+Z to undo)")

    def reset_view(self):
        self.pause()
        self.vp.update_poses({o.name: o.pose for o in self.doc.objects})
        if self.result:
            self.slider.blockSignals(True)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self.i = 0
            self.tlabel.setText("0.00 s")
            self.plot.set_cursor(0)
