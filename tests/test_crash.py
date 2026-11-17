"""Crash reports: unhandled errors are saved and shown once; hard crashes are offered next start; email / GitHub links."""
import os
import sys
import threading
from urllib.parse import unquote

import pytest

from daedalus import crash as C


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path))
    monkeypatch.setitem(C._state, "seen", set())
    monkeypatch.setitem(C._state, "ui", None)
    monkeypatch.setitem(C._state, "fatal_path", None)
    yield tmp_path


def _boom():
    try:
        {}["missing part"]
    except KeyError:
        return sys.exc_info()


def test_unhandled_error_saves_a_report_and_shows_it_once():
    shown = []
    C._state["ui"] = lambda title, text, path: shown.append((title, text, path))
    p = C.handle(*_boom())
    assert p.exists() and p.parent == C.crash_dir()
    text = p.read_text(encoding="utf-8")
    assert "Daedalus crash report" in text and "KeyError" in text and "test_crash.py" in text
    assert "Daedalus:" in text and "Python:" in text
    C.handle(*_boom())                                         # same error again: saved, not shown again
    assert len(shown) == 1 and shown[0][0].startswith("KeyError")
    assert len(list(C.crash_dir().glob("crash-*.txt"))) == 2


def test_thread_errors_are_caught():
    shown = []
    C._state["ui"] = lambda title, text, path: shown.append(text)
    old = threading.excepthook
    threading.excepthook = C._thread_hook
    try:
        t = threading.Thread(target=lambda: 1 / 0, name="worker")
        t.start()
        t.join()
    finally:
        threading.excepthook = old
    assert shown and "ZeroDivisionError" in shown[0] and "thread worker" in shown[0]


def test_old_reports_are_pruned(monkeypatch):
    monkeypatch.setattr(C, "KEEP", 3)
    for _ in range(5):
        C.save_report("x")
    assert len(list(C.crash_dir().glob("crash-*.txt"))) == 3


def test_hard_crash_from_last_session_is_offered_once():
    dead = C.crash_dir() / "session-999999.log"
    dead.write_text('Fatal Python error: Segmentation fault\n  File "occ.py", line 12 in fillet\n')
    (C.crash_dir() / "session-1.log").write_text("")      # clean session file: nothing to report
    got = C.last_fatal()
    assert got and "Segmentation fault" in got[1] and "hard crash" in got[1] and got[0].exists()
    assert not dead.exists() and C.last_fatal() is None


def test_running_session_file_is_left_alone():
    mine = C.crash_dir() / f"session-{os.getppid()}.log"       # a live process
    mine.write_text("Fatal Python error: x")
    if sys.platform != "win32":
        assert C.last_fatal() is None and mine.exists()


def test_email_goes_to_the_report_address_and_fits_a_mail_link():
    p = C.handle(*_boom())
    text = p.read_text(encoding="utf-8") + "\n" + "x" * 5000
    url = C.mail_url("KeyError: 'missing part'", text, p)
    assert url.startswith(f"mailto:{C.REPORT_TO}?") and C.REPORT_TO == "narula.ayenn@gmail.com"
    assert len(unquote(url)) < 2200 and "Full report:" in unquote(url) and "KeyError" in unquote(url)
    gh = unquote(C.issue_url("KeyError", text))
    assert "github.com/ayenn1214/daedalus/issues/new" in gh and "Crash: KeyError" in gh


def test_crash_window_builds(qapp=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton
    app = QApplication.instance() or QApplication([])
    from daedalus.ui.crash_ui import CrashDialog
    p = C.handle(*_boom())
    d = CrashDialog("KeyError: 'missing part'", p.read_text(encoding="utf-8"), p)
    labels = [b.text() for b in d.findChildren(QPushButton)]
    assert {"Email report", "Show details", "Copy report", "Open folder", "Report…", "Continue"} <= set(labels)
    d.copy()
    assert "KeyError" in app.clipboard().text()
