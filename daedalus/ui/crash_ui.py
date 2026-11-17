"""Crash report window — shown for any unhandled error (any thread) and, at start-up, for a hard crash last time."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from .. import crash as C


class CrashDialog(QDialog):
    def __init__(self, title: str, text: str, path, parent=None, hard: bool = False):
        super().__init__(parent)
        self.text, self.path = text, path
        self.setWindowTitle("Daedalus — crash report")
        self.resize(640, 420)
        v = QVBoxLayout(self)
        head = QLabel("<b>Daedalus closed unexpectedly last time.</b>" if hard else
                      "<b>Something went wrong.</b> Daedalus is still running — save your work.")
        v.addWidget(head)
        what = QLabel(title)
        what.setWordWrap(True)
        what.setTextInteractionFlags(what.textInteractionFlags() | what.textInteractionFlags().TextSelectableByMouse)
        v.addWidget(what)
        saved = QLabel(f"Report saved: {path}")
        saved.setWordWrap(True)
        saved.setStyleSheet("color: gray")
        v.addWidget(saved)
        self.details = QPlainTextEdit(text)
        self.details.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        self.details.setFont(f)
        self.details.setVisible(False)
        v.addWidget(self.details, 1)
        row = QHBoxLayout()
        self.more = QPushButton("Show details")
        self.more.clicked.connect(self.toggle)
        self.copy_btn = QPushButton("Copy report")
        self.copy_btn.clicked.connect(self.copy)
        folder = QPushButton("Open folder")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(C.crash_dir()))))
        gh = QPushButton("Report…")
        gh.setToolTip("The bug report form with this crash filled in — email the team or open a GitHub issue")
        gh.clicked.connect(lambda: self.report(title))
        mail = QPushButton("Email report")
        mail.setToolTip(f"Opens your mail app with the report to {C.REPORT_TO} — attach the saved file if asked")
        mail.clicked.connect(lambda: (QDesktopServices.openUrl(QUrl(C.mail_url(title, text, path))),
                                      QDesktopServices.openUrl(QUrl.fromLocalFile(str(C.crash_dir())))))
        ok = QPushButton("Continue" if not hard else "Close")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        for b in (mail, self.more, self.copy_btn, folder, gh):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(ok)
        v.addLayout(row)

    def report(self, title):
        """Hand the crash to Help ▸ Report a bug (email / GitHub / copy / save)."""
        try:
            from .bug_report import BugReportDialog
            win = self.parent()
            page = getattr(win, "inventory_page", None)
            inv = getattr(page, "inv", None)
            d = BugReportDialog(win, context={"Crash report": str(self.path)}, inv=inv,
                                commit=(lambda m: page.commit(m)) if page is not None else None)
            d.title.setText(f"Crash: {title}"[:120])
            d.what.setPlainText("What was I doing:\n\n\n---\n" + self.text)
            d.shot.setChecked(False)
            if not d.contact.text().strip():
                d.contact.setText(C.REPORT_TO)
            d.exec()
        except Exception:  # noqa: BLE001
            QDesktopServices.openUrl(QUrl(C.issue_url(title, self.text)))

    def toggle(self):
        on = not self.details.isVisible()
        self.details.setVisible(on)
        self.more.setText("Hide details" if on else "Show details")

    def copy(self):
        QGuiApplication.clipboard().setText(self.text)
        self.copy_btn.setText("Copied ✓")


class _Bridge(QObject):
    show = Signal(str, str, str)


_bridge = None


def install(window_fn=lambda: None):
    """Hook crash reporting into Qt: errors from any thread open the window on the UI thread."""
    global _bridge
    if _bridge is None:
        _bridge = _Bridge(QApplication.instance())

        def show(title, text, path):
            if C._state.get("showing"):
                return
            C._state["showing"] = True
            try:
                CrashDialog(title, text, path, window_fn()).exec()
            finally:
                C._state["showing"] = False
        _bridge.show.connect(show)
    C.install(ui=lambda title, text, path: _bridge.show.emit(title, text, str(path)))
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(C.close)


def offer_last_crash(window):
    """At start-up: a hard crash in an earlier session → the report window."""
    def go():
        try:
            got = C.last_fatal()
        except Exception:  # noqa: BLE001
            return
        if got:
            path, text = got
            CrashDialog("The app stopped without an error message (a crash inside Qt or the geometry kernel).",
                        text, path, window, hard=True).exec()
    QTimer.singleShot(1500, go)
