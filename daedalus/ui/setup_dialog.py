"""Help ▸ Setup check…: the Python and Icarus-agent report, with buttons that fix what they can
(install missing packages, install / start Ollama, download the model)."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
    QWidget,
)

from .. import setup_check as SC
from . import theme

BUTTON = {"packages": "Install packages", "ollama": "Install Ollama", "start": "Start Ollama",
          "pull": "Download model"}


class _Sig(QObject):
    report = Signal(object)
    log = Signal(str)
    progress = Signal(str, int, int)
    done = Signal(str, bool)


class SetupDialog(QDialog):
    def __init__(self, parent=None, agent=True):
        super().__init__(parent)
        self.setWindowTitle("Setup check")
        self.resize(640, 560)
        self.sig = _Sig(self)
        self.sig.report.connect(self._show)
        self.sig.log.connect(lambda t: self.log.appendPlainText(t.rstrip()))
        self.sig.progress.connect(self._progress)
        self.sig.done.connect(self._done)
        self._busy = False
        self._stop = False
        v = QVBoxLayout(self)
        intro = QLabel("Checks that this Python can run Daedalus and that Icarus (the local AI copilot) is ready. "
                       "Buttons fix what they can; install.bat does all of it from scratch.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        v.addWidget(intro)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setColumnStretch(2, 1)
        v.addWidget(self.body)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Icarus model"))
        self.model = QComboBox()
        self.model.setEditable(True)
        rec = SC.recommended_model()
        for _need, m in SC.MODEL_BY_RAM:
            self.model.addItem(m + ("  (recommended for this PC)" if m == rec else ""), m)
        _h, cur = SC.agent_config()
        i = self.model.findData(cur)
        if i >= 0:
            self.model.setCurrentIndex(i)
        else:
            self.model.setEditText(cur)
        mrow.addWidget(self.model, 1)
        use = QPushButton("Use this model")
        use.clicked.connect(self._use_model)
        mrow.addWidget(use)
        v.addLayout(mrow)
        self.bar = QProgressBar()
        self.bar.hide()
        v.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Install / download output appears here")
        v.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.recheck = QPushButton("Check again")
        self.recheck.clicked.connect(self.refresh)
        self.cancel = QPushButton("Cancel download")
        self.cancel.clicked.connect(lambda: setattr(self, "_stop", True))
        self.cancel.hide()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.recheck)
        row.addStretch(1)
        row.addWidget(self.cancel)
        row.addWidget(close)
        v.addLayout(row)
        self.agent = agent
        self.refresh()

    # ------------------------------------------------------------------ report
    def chosen_model(self):
        d = self.model.currentData()
        txt = self.model.currentText().split("  (")[0].strip()
        return txt if (d is None or txt != d) else d

    def refresh(self):
        self._clear()
        self.grid.addWidget(QLabel("Checking…"), 0, 0)

        def run():
            rep = SC.check_python()
            if self.agent:
                SC.check_agent(rep, model=self.chosen_model())
            self.sig.report.emit(rep)
        threading.Thread(target=run, daemon=True).start()

    def _clear(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def _show(self, rep):
        self._clear()
        self.report = rep
        r, last = 0, None
        for row in rep.rows:
            if row.group != last:
                h = QLabel(row.group)
                h.setStyleSheet("font-weight:600; margin-top:6px;")
                self.grid.addWidget(h, r, 0, 1, 4)
                r += 1
                last = row.group
            mark = QLabel({True: "✓", False: "✗" if row.required else "–", None: "·"}[row.ok])
            mark.setStyleSheet(f"color:{ {True: theme.OK, False: theme.ERROR if row.required else theme.WARN, None: theme.MUTED}[row.ok] };"
                               "font-weight:700;")
            self.grid.addWidget(mark, r, 0)
            self.grid.addWidget(QLabel(row.name), r, 1)
            d = QLabel(row.detail + (f"<br><span style='color:{theme.MUTED}'>{row.fix}</span>"
                                     if row.ok is False and row.fix else ""))
            d.setWordWrap(True)
            d.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.grid.addWidget(d, r, 2)
            if row.ok is False and row.action in BUTTON:
                b = QPushButton(BUTTON[row.action])
                b.setObjectName("primary" if row.required else "")
                b.clicked.connect(lambda _=False, a=row.action: self.run_action(a))
                b.setEnabled(not self._busy)
                self.grid.addWidget(b, r, 3)
            r += 1
        tail = QLabel("All set — Icarus is ready." if rep.ok else "")
        tail.setStyleSheet(f"color:{theme.OK}")
        self.grid.addWidget(tail, r, 0, 1, 4)

    # ------------------------------------------------------------------ actions
    def run_action(self, action):
        if self._busy:
            return
        self._busy, self._stop = True, False
        self.recheck.setEnabled(False)
        model = self.chosen_model()
        log = self.sig.log.emit
        if action == "pull":
            self.bar.setRange(0, 0)
            self.bar.show()
            self.cancel.show()

        def run():
            ok = False
            try:
                if action == "packages":
                    log("Installing packages into this Python…")
                    ok = SC.install_packages(log)
                    if ok:
                        log("Done — restart Daedalus so it loads them.")
                elif action == "ollama":
                    log("Installing Ollama…")
                    ok = SC.install_ollama(log) and SC.start_ollama(log=log)
                elif action == "start":
                    log("Starting Ollama…")
                    ok = SC.start_ollama(log=log)
                elif action == "pull":
                    log(f"Downloading {model} (several GB)…")
                    SC.pull_model(model, progress=self.sig.progress.emit, stop=lambda: self._stop)
                    SC.set_model(model)
                    log(f"{model} ready — Icarus uses it now.")
                    ok = True
            except Exception as e:  # noqa: BLE001
                log(f"Failed: {e}")
            self.sig.done.emit(action, ok)
        threading.Thread(target=run, daemon=True).start()

    def _progress(self, status, done, total):
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done / total * 1000))
            self.bar.setFormat(f"{status} — {done / 1e9:.2f} / {total / 1e9:.2f} GB")
            self.bar.setTextVisible(True)
        else:
            self.bar.setRange(0, 0)
            self.bar.setFormat(status)

    def _done(self, action, ok):
        self._busy = False
        self.recheck.setEnabled(True)
        self.bar.hide()
        self.cancel.hide()
        self.refresh()

    def _use_model(self):
        m = self.chosen_model()
        SC.set_model(m)
        self.log.appendPlainText(f"Icarus will use {m}.")
        self.refresh()
