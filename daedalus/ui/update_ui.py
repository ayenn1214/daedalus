"""Updates from the GitHub repo: background check at start, a status-bar button when one is out, the update dialog
(release notes, update now / skip / later), Help ▸ Check for updates…, roll back."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton,
    QTextBrowser, QVBoxLayout,
)

from .. import __version__
from .. import updater as U


class _Bridge(QObject):
    done = Signal(object)


def _in_thread(fn, on_done):
    """Run fn on a thread; on_done(result_or_exception) on the UI thread."""
    br = _Bridge()
    br.done.connect(on_done)

    def work():
        try:
            br.done.emit(fn())
        except Exception as e:  # noqa: BLE001
            br.done.emit(e)
    t = threading.Thread(target=work, daemon=True)
    t._bridge = br                                   # keep the bridge alive
    t.start()
    return t


def settings():
    return QSettings("Daedalus", "Daedalus")


def start_auto_check(window):
    """Called once per window at start: quiet background check (at most daily); a status-bar button if there's
    an update."""
    s = settings()
    if not s.value("update/auto", True, type=bool):
        return
    window._update_thread = _in_thread(lambda: U.check(s), lambda r: _offer(window, r))


def _offer(window, res):
    if not isinstance(res, dict):
        return
    sb = window.statusBar()
    btn = getattr(window, "_update_btn", None)
    if btn is None:
        btn = QPushButton()
        btn.setFlat(True)
        btn.setCursor(Qt.PointingHandCursor)
        sb.addPermanentWidget(btn)
        window._update_btn = btn
    btn.setText(f"⬆ Update: Daedalus {res['version']}")
    btn.setToolTip("A newer Daedalus is on GitHub — click for what's new")
    window._update_info = res
    if not getattr(btn, "_wired", False):
        btn.clicked.connect(lambda: UpdateDialog(window._update_info, window).exec())
        btn._wired = True
    btn.show()


def check_now(window):
    """Help ▸ Check for updates…"""
    s = settings()
    QApplication.setOverrideCursor(Qt.WaitCursor)

    def done(res):
        QApplication.restoreOverrideCursor()
        if isinstance(res, Exception):
            return QMessageBox.warning(window, "Updates", str(res))
        if res:
            _offer(window, res)
            return UpdateDialog(res, window).exec()
        cur = U.current()
        msg = f"You have the latest Daedalus ({__version__}" + (f", {cur['commit'][:7]}" if cur["commit"] else "") + ")."
        box = QMessageBox(QMessageBox.Information, "Updates", msg, QMessageBox.Ok, window)
        backs = U.rollback_folders()
        if backs:
            rb = box.addButton(f"Roll back to {backs[0].name.removeprefix('backup-')}…", QMessageBox.ActionRole)
        box.exec()
        if backs and box.clickedButton() is rb:
            _rollback(window, backs[0])
    window._update_thread = _in_thread(lambda: U.check(s, force=True), done)


def _rollback(window, folder):
    if QMessageBox.question(window, "Roll back", f"Put back the files the last update replaced "
                            f"({folder.name.removeprefix('backup-')})? Daedalus restarts.") != QMessageBox.Yes:
        return
    U.rollback(folder)
    _quit(window)


def _quit(window):
    for w in QApplication.topLevelWidgets():
        if w.isVisible() and w is not window:
            w.close()
    if window.close():
        QApplication.quit()


class UpdateDialog(QDialog):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.s = settings()
        self.setWindowTitle("Daedalus update")
        self.resize(560, 460)
        v = QVBoxLayout(self)
        cur = U.current()
        head = QLabel(f"<b>Daedalus {info['version']}</b> is available — you have {__version__}"
                      + (f" ({cur['commit'][:7]})" if cur["commit"] else "") + ".")
        v.addWidget(head)
        self.desktop_only = cur["git_dir"] and not cur["git"]
        how = QLabel("This copy is a git checkout: updating runs git pull." if cur["git"] else
                     "This copy is managed by GitHub Desktop: open it there and click Fetch origin ▸ Pull origin, "
                     "then restart Daedalus." if self.desktop_only else
                     "Daedalus downloads the update, closes, swaps the files in (your settings, data and Python "
                     "environment stay), installs any new packages and restarts. The replaced files are kept so "
                     "you can roll back.")
        how.setWordWrap(True)
        how.setObjectName("muted")
        v.addWidget(how)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "_No release notes._")
        v.addWidget(notes, 1)
        if info.get("url"):
            link = QLabel(f'<a href="{info["url"]}">See it on GitHub</a>')
            link.setOpenExternalLinks(True)
            v.addWidget(link)
        row = QHBoxLayout()
        row.addWidget(QLabel("Channel"))
        self.channel = QComboBox()
        self.channel.addItem("Releases (stable)", "releases")
        self.channel.addItem("Every commit on main (testing)", "main")
        self.channel.setCurrentIndex(max(0, self.channel.findData(self.s.value("update/channel") or "releases")))
        self.channel.activated.connect(lambda _i: self.s.setValue("update/channel", self.channel.currentData()))
        row.addWidget(self.channel)
        self.auto = QCheckBox("Check at start")
        self.auto.setChecked(self.s.value("update/auto", True, type=bool))
        self.auto.toggled.connect(lambda on: self.s.setValue("update/auto", on))
        row.addWidget(self.auto)
        row.addStretch(1)
        v.addLayout(row)
        b = QHBoxLayout()
        go = QPushButton("Update now")
        go.setDefault(True)
        go.clicked.connect(self.update_now)
        go.setEnabled(not self.desktop_only)
        skip = QPushButton("Skip this version")
        skip.clicked.connect(lambda: (self.s.setValue("update/skip", info["version"]), self._hide_btn(),
                                      self.reject()))
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        for w in (go, skip, later):
            b.addWidget(w)
        v.addLayout(b)

    def _hide_btn(self):
        btn = getattr(self.parent(), "_update_btn", None)
        if btn is not None:
            btn.hide()

    def update_now(self):
        cur = U.current()
        win = self.parent()
        token = self.s.value("update/token") or None
        prog = QProgressDialog("Updating Daedalus…", None, 0, 0, self)
        prog.setWindowModality(Qt.WindowModal)
        prog.show()

        if cur["git"]:
            def done(res):
                prog.close()
                if isinstance(res, Exception):
                    return QMessageBox.warning(self, "Update", str(res))
                self.accept()
                if QMessageBox.question(win, "Update", f"Updated to {self.info['version']}. Restart Daedalus now?") \
                        == QMessageBox.Yes:
                    import subprocess
                    import sys
                    subprocess.Popen([sys.executable, "-m", "daedalus"], cwd=cur["root"])
                    _quit(win)
            self._t = _in_thread(lambda: U.git_update(self.info), done)
            return

        def done(res):
            prog.close()
            if isinstance(res, Exception):
                return QMessageBox.warning(self, "Update", str(res))
            self.accept()
            U.launch_helper(res, self.info)
            _quit(win)
        self._t = _in_thread(lambda: U.download(self.info, token), done)
