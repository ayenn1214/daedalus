"""Report a bug / ask for a feature: a short form plus the system details a fix needs (version, commit, OS, Python,
Qt, loaded modules, data location), sent as a pre-filled GitHub issue or an email to the team's contact, or copied /
saved. A screenshot of the window can be saved next to the report to attach."""
from __future__ import annotations

import datetime as dt
import os
import platform
import sys
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QVBoxLayout,
)

from .. import __version__

REPO = "ayenn1214/daedalus"


def system_info(extra: dict | None = None) -> dict:
    info = {"Daedalus": __version__}
    try:
        from .. import updater as U
        cur = U.current()
        if cur.get("commit"):
            info["Commit"] = cur["commit"][:10]
        info["Install"] = "git checkout" if cur.get("git") else "zip / installer"
        info["Update channel"] = QSettings("Daedalus", "Daedalus").value("update/channel") or "releases"
    except Exception:  # noqa: BLE001
        pass
    import PySide6
    info.update({"OS": f"{platform.system()} {platform.release()} ({platform.version()[:40]})",
                 "Python": sys.version.split()[0], "Qt / PySide6": PySide6.__version__})
    mods = QSettings("Daedalus", "Daedalus").value("startup/modules")
    if mods:
        info["Startup modules"] = str(mods)
    info.update(extra or {})
    return info


class BugReportDialog(QDialog):
    def __init__(self, parent=None, context: dict | None = None, inv=None, commit=None):
        super().__init__(parent)
        self.inv, self.commit_fn = inv, commit
        self.setWindowTitle("Report a bug")
        self.resize(620, 640)
        self.win = parent
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Tell us what went wrong (or what you'd like). System details are added for you."))
        f = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(["Bug", "Feature request", "Question"])
        self.title = QLineEdit()
        self.title.setPlaceholderText("Short summary, e.g. 'Receiving a part order doesn't update stock'")
        self.what = QPlainTextEdit()
        self.what.setPlaceholderText("What happened? Include any error message.")
        self.steps = QPlainTextEdit()
        self.steps.setPlaceholderText("Steps to make it happen again (1. … 2. … 3. …)")
        self.expected = QLineEdit()
        self.expected.setPlaceholderText("What should have happened?")
        for w in (self.what, self.steps):
            w.setFixedHeight(90)
        f.addRow("Type", self.kind)
        f.addRow("Title", self.title)
        f.addRow("What happened", self.what)
        f.addRow("Steps", self.steps)
        f.addRow("Expected", self.expected)
        self.contact = QLineEdit((inv.syncMeta or {}).get("bugReportEmail", "") if inv is not None else
                                 QSettings("Daedalus", "Daedalus").value("bugreport/email") or "")
        self.contact.setPlaceholderText("Who gets emailed reports (shared with the team when on DIMS)")
        f.addRow("Send to", self.contact)
        v.addLayout(f)
        self.shot = QCheckBox("Save a screenshot of the window to attach")
        self.shot.setChecked(True)
        v.addWidget(self.shot)
        self.info = system_info(context)
        det = QLabel("<br>".join(f"<b>{k}:</b> {v}" for k, v in self.info.items()))
        det.setObjectName("muted")
        det.setWordWrap(True)
        v.addWidget(det)
        bb = QDialogButtonBox()
        for text, fn, tip in (("Email…", self.email, "Opens your mail app with the report filled in"),
                              ("GitHub issue…", self.github, "Opens a pre-filled issue on GitHub (needs a GitHub "
                                                              "account)"),
                              ("Copy", self.copy, "Copy the report to paste into Teams, Slack, etc."),
                              ("Save…", self.save, "Save the report (and screenshot) as files")):
            b = bb.addButton(text, QDialogButtonBox.ActionRole)
            b.setToolTip(tip)
            b.clicked.connect(fn)
        bb.addButton(QDialogButtonBox.Close).clicked.connect(self.reject)
        v.addWidget(bb)
        self._shot_path = None

    # ------------------------------------------------------------ content
    def text(self) -> str:
        parts = [f"[{self.kind.currentText()}] {self.title.text().strip() or '(no title)'}", ""]
        for lab, w in (("What happened", self.what), ("Steps", self.steps)):
            t = w.toPlainText().strip()
            if t:
                parts += [f"## {lab}", t, ""]
        if self.expected.text().strip():
            parts += ["## Expected", self.expected.text().strip(), ""]
        parts += ["## System"] + [f"- {k}: {v}" for k, v in self.info.items()]
        if self._shot_path:
            parts += ["", f"Screenshot: {self._shot_path.name} (attach it)"]
        return "\n".join(parts)

    def _ok(self):
        if not self.title.text().strip() and not self.what.toPlainText().strip():
            QMessageBox.information(self, "Report", "Add a title or describe what happened first.")
            return False
        if self.shot.isChecked() and self._shot_path is None and self.win is not None:
            d = report_dir()
            self._shot_path = d / f"screenshot-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
            self.win.grab().save(str(self._shot_path), "PNG")
        self._remember_contact()
        return True

    def _remember_contact(self):
        c = self.contact.text().strip()
        if self.inv is not None:
            if c != (self.inv.syncMeta or {}).get("bugReportEmail", ""):
                self.inv.syncMeta = {**(self.inv.syncMeta or {}), "bugReportEmail": c}
                if self.commit_fn:
                    self.commit_fn("Bug report contact saved for the team")
        else:
            QSettings("Daedalus", "Daedalus").setValue("bugreport/email", c)

    # ------------------------------------------------------------ outputs
    def email(self):
        if not self._ok():
            return
        to = self.contact.text().strip()
        if not to:
            QMessageBox.information(self, "Email", "Enter who should get reports in 'Send to'.")
            return
        subj = f"Daedalus {self.kind.currentText().lower()}: {self.title.text().strip()}"
        body = self.text()
        url = f"mailto:{urllib.parse.quote(to)}?subject={urllib.parse.quote(subj)}&body={urllib.parse.quote(body[:6000])}"
        QDesktopServices.openUrl(QUrl(url))
        self._reveal_shot()

    def github(self):
        if not self._ok():
            return
        labels = {"Bug": "bug", "Feature request": "enhancement", "Question": "question"}[self.kind.currentText()]
        q = urllib.parse.urlencode({"title": self.title.text().strip() or self.kind.currentText(),
                                    "body": self.text()[:6000], "labels": labels})
        QDesktopServices.openUrl(QUrl(f"https://github.com/{REPO}/issues/new?{q}"))
        self._reveal_shot()

    def copy(self):
        if not self._ok():
            return
        QGuiApplication.clipboard().setText(self.text())
        QMessageBox.information(self, "Copied", "Report copied." + (f"\nScreenshot saved: {self._shot_path}"
                                                                    if self._shot_path else ""))

    def save(self):
        if not self._ok():
            return
        p = report_dir() / f"report-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
        p.write_text(self.text(), encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    def _reveal_shot(self):
        if self._shot_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._shot_path.parent)))


def report_dir() -> Path:
    d = Path(os.path.expanduser("~")) / "Documents" / "Daedalus bug reports"
    d.mkdir(parents=True, exist_ok=True)
    return d
