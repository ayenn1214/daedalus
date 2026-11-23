"""Auto-update from GitHub: release / main channels, daily throttle and skip, safe download, file swap with
backup, the after-exit helper, git checkouts."""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from daedalus import updater as U


class S(dict):
    def value(self, k, d=None, type=None):  # noqa: A002
        v = self.get(k, d)
        return type(v) if type and v is not None else v

    def setValue(self, k, v):  # noqa: N802
        self[k] = v


def fake_github(monkeypatch, tag="v2.10.0", sha="abc123", ver_main="2.10.0", zipdata=b"", calls=None):
    def get(url, token=None, raw=False, timeout=20):
        if calls is not None:
            calls.append(url)
        if url.endswith("/releases/latest"):
            return {"tag_name": tag, "body": "## New\n- faster", "html_url": "https://github.com/x/y/releases/tag/" + tag}
        if url.endswith("/commits/main"):
            return {"sha": sha, "commit": {"message": "Fix things"}, "html_url": "https://github.com/x/y/commit/" + sha}
        if "raw.githubusercontent.com" in url:
            return f'__version__ = "{ver_main}"\n'.encode()
        if "codeload.github.com" in url:
            return zipdata
        raise U.UpdateError("unexpected " + url)
    monkeypatch.setattr(U, "_get", get)


def test_versions_and_channels(monkeypatch):
    assert U.vtuple("v2.10.0") > U.vtuple("2.9.0") > U.vtuple("2.8.12")
    fake_github(monkeypatch)
    rel = U.latest("x/y", "releases")
    assert rel["version"] == "2.10.0" and rel["zip"].endswith("/zip/refs/tags/v2.10.0") and "faster" in rel["notes"]
    assert U.is_newer(rel, {"version": "2.9.0", "commit": ""})
    assert not U.is_newer(rel, {"version": "2.10.0", "commit": ""})
    m = U.latest("x/y", "main")
    assert m["commit"] == "abc123" and U.is_newer(m, {"version": "2.10.0", "commit": "zzz"})
    assert not U.is_newer(m, {"version": "2.10.0", "commit": "abc123"})


def test_release_tagged_on_older_code_is_not_offered(monkeypatch):
    """A v3.0.18 release made before the 3.0.18 commit was pushed still holds 3.0.17 code: don't loop on it."""
    fake_github(monkeypatch, tag="v3.0.18", ver_main="3.0.17")
    rel = U.latest("x/y", "releases")
    assert rel["version"] == "3.0.17" and rel["tag"] == "v3.0.18"
    assert not U.is_newer(rel, {"version": "3.0.17", "commit": ""})


def test_check_throttles_and_skips(monkeypatch):
    calls = []
    fake_github(monkeypatch, calls=calls)
    monkeypatch.setattr(U, "current", lambda: {"version": "2.9.0", "commit": "", "git": False, "git_dir": False})
    s = S()
    info = U.check(s)
    api = lambda: [c for c in calls if "raw.githubusercontent" not in c]       # noqa: E731 (version file is extra)
    assert info["version"] == "2.10.0" and len(api()) == 1
    assert U.check(s)["version"] == "2.10.0" and len(api()) == 1            # within 20 h: cached, no network
    s["update/skip"] = "2.10.0"
    assert U.check(s) is None
    assert U.check(s, force=True)["version"] == "2.10.0" and len(api()) == 2   # Help ▸ Check: always asks
    s["update/last_check"] = time.time() - 21 * 3600
    assert U.check(s) is None and len(api()) == 3


def _zip(files: dict, top="ayenn1214-daedalus-abc/") -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, t in files.items():
            z.writestr(top + n, t)
    return b.getvalue()


def _install(tmp: Path) -> Path:
    t = tmp / "install"
    (t / "daedalus" / "ui").mkdir(parents=True)
    (t / "daedalus" / "__init__.py").write_text('__version__ = "2.9.0"\n')
    (t / "daedalus" / "ui" / "old_module.py").write_text("x = 1\n")
    (t / "daedalus" / "same.py").write_text("same\n")
    (t / "requirements.txt").write_text("numpy\n")
    (t / ".venv").mkdir()
    (t / ".venv" / "keep.txt").write_text("venv\n")
    return t


def test_download_and_apply(tmp_path, monkeypatch):
    t = _install(tmp_path)
    data = _zip({"daedalus/__init__.py": '__version__ = "2.10.0"\n', "daedalus/same.py": "same\n",
                 "daedalus/new_feature.py": "y = 2\n", "requirements.txt": "numpy\nscipy\n", "README.md": "hi"})
    fake_github(monkeypatch, zipdata=data)
    monkeypatch.setattr(U, "root", lambda: t)
    info = U.latest("x/y", "releases")
    stage = U.download(info)
    assert (stage / "daedalus" / "new_feature.py").exists() and stage.parent == t / ".update"
    res = U.apply_now(stage, t)
    assert res == {"copied": 4, "removed": 1, "requirements_changed": True}
    assert '2.10.0' in (t / "daedalus" / "__init__.py").read_text()
    assert not (t / "daedalus" / "ui" / "old_module.py").exists()               # deleted upstream
    back = t / ".update" / "backup-2.9.0"
    assert (back / "daedalus" / "ui" / "old_module.py").exists() and '2.9.0' in (back / "daedalus" / "__init__.py").read_text()
    assert (t / ".venv" / "keep.txt").exists()                                  # environment untouched
    # roll back: the backup is applied as a partial update
    res2 = U._apply(str(back), str(t), "2.10.0", True)
    assert '2.9.0' in (t / "daedalus" / "__init__.py").read_text() and res2["removed"] == 0
    assert (t / "daedalus" / "ui" / "old_module.py").exists()


def test_download_rejects_bad_zips(tmp_path, monkeypatch):
    t = _install(tmp_path)
    monkeypatch.setattr(U, "root", lambda: t)
    fake_github(monkeypatch, zipdata=_zip({"../../evil.py": "x"}))
    with pytest.raises(U.UpdateError, match="Unsafe"):
        U.download(U.latest("x/y"))
    fake_github(monkeypatch, zipdata=_zip({"README.md": "not daedalus"}))
    with pytest.raises(U.UpdateError, match="look like Daedalus"):
        U.download(U.latest("x/y"))
    fake_github(monkeypatch, zipdata=b"not a zip")
    with pytest.raises(U.UpdateError, match="valid zip"):
        U.download(U.latest("x/y"))


def test_helper_applies_after_exit(tmp_path, monkeypatch):
    t = _install(tmp_path)
    (t / "requirements.txt").write_text("")
    stage = tmp_path / "stage"
    (stage / "daedalus").mkdir(parents=True)
    (stage / "daedalus" / "__init__.py").write_text('__version__ = "2.10.0"\n')
    (stage / "requirements.txt").write_text("")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    import inspect
    script = tmp_path / "apply_update.py"
    script.write_text(U.HELPER)
    (t / ".update").mkdir()
    args = {"pid": dead.pid, "stage": str(stage), "target": str(t), "old": "2.9.0", "new": "2.10.0",
            "commit": "", "apply_src": inspect.getsource(U._apply), "python": sys.executable,
            "python_gui": sys.executable, "relaunch": False, "relaunch_args": []}
    subprocess.run([sys.executable, "-I", str(script), json.dumps(args)], check=True, timeout=60)
    assert "ok" in (t / ".update" / "last-update.log").read_text()
    assert '2.10.0' in (t / "daedalus" / "__init__.py").read_text()
    assert json.loads((t / ".update" / "build.json").read_text())["version"] == "2.10.0"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_checkout_updates_with_pull(tmp_path, monkeypatch):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}

    def git(cwd, *a):
        return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=env).stdout
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "daedalus").mkdir()
    (origin / "daedalus" / "__init__.py").write_text('__version__ = "2.9.0"\n')
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "one")
    git(origin, "tag", "v2.9.0")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (origin / "daedalus" / "__init__.py").write_text('__version__ = "2.10.0"\n')
    git(origin, "commit", "-qam", "two")
    git(origin, "tag", "v2.10.0")
    monkeypatch.setattr(U, "root", lambda: clone)
    assert U.current()["git"]
    head = U.git_update({"ref": "v2.10.0", "channel": "releases"})
    assert '2.10.0' in (clone / "daedalus" / "__init__.py").read_text() and head == git(origin, "rev-parse", "HEAD").strip()
    (clone / "daedalus" / "__init__.py").write_text("local edit\n")
    with pytest.raises(U.UpdateError, match="local changes"):
        U.git_update({"ref": "v2.10.0", "channel": "releases"})


def test_update_ui(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMainWindow
    QApplication.instance() or QApplication([])
    from daedalus.ui import update_ui as UI
    w = QMainWindow()
    info = {"version": "2.10.0", "notes": "## New", "url": "https://github.com/x", "channel": "releases",
            "ref": "v2.10.0", "commit": ""}
    UI._offer(w, info)
    assert w._update_btn.text().endswith("2.10.0")
    UI._offer(w, None)
    d = UI.UpdateDialog(info, w)
    from PySide6.QtWidgets import QLabel
    assert any("2.10.0" in lab.text() for lab in d.findChildren(QLabel))


def test_restart_closes_first_and_never_starts_twice(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMainWindow
    QApplication.instance() or QApplication([])
    from daedalus.ui import restart as RS

    class W(QMainWindow):
        ok = True

        def closeEvent(self, e):
            e.accept() if self.ok else e.ignore()
    started, quit_ = [], []
    monkeypatch.setattr(RS.QProcess, "startDetached", lambda *a: started.append(a) or True)
    monkeypatch.setattr(RS, "hard_quit", lambda *a: quit_.append(1))
    a, b = W(), W()
    a.show()
    b.show()
    b.ok = False                                         # e.g. "Save changes?" → Cancel
    assert not RS.restart(a) and not started and not quit_ and a.isVisible()
    b.ok = True
    monkeypatch.setattr(RS.sys, "argv", ["daedalus", "--only", "inventory"])
    assert RS.restart(a) and not a.isVisible() and not b.isVisible() and quit_ == [1]
    exe, args, cwd = started[0]
    assert args == ["-m", "daedalus", "--only", "inventory"] and os.path.isdir(cwd)


def test_helper_leaves_files_alone_if_daedalus_stays_open(tmp_path):
    t = _install(tmp_path)
    stage = tmp_path / "stage"
    (stage / "daedalus").mkdir(parents=True)
    (stage / "daedalus" / "__init__.py").write_text('__version__ = "2.10.0"\n')
    script = tmp_path / "apply_update.py"
    script.write_text(U.HELPER.replace("range(3600)", "range(2)"))      # don't wait 30 min in a test
    import inspect
    args = {"pid": os.getpid(), "stage": str(stage), "target": str(t), "old": "2.9.0", "new": "2.10.0",
            "commit": "", "apply_src": inspect.getsource(U._apply), "python": sys.executable,
            "python_gui": sys.executable, "relaunch": True, "relaunch_args": []}
    subprocess.run([sys.executable, "-I", str(script), json.dumps(args)], check=True, timeout=60)
    assert "not applied" in (t / ".update" / "last-update.log").read_text()
    assert '2.9.0' in (t / "daedalus" / "__init__.py").read_text()


def test_running_app_rechecks_every_few_hours(monkeypatch):
    calls = []
    fake_github(monkeypatch, calls=calls)
    monkeypatch.setattr(U, "current", lambda: {"version": "2.9.0", "commit": "", "git": False, "git_dir": False})
    s = S()
    U.check(s)
    api = lambda: [c for c in calls if "raw.githubusercontent" not in c]       # noqa: E731
    U.check(s, max_age=90 * 60)
    assert len(api()) == 1                                                  # answer is fresh: no new request
    s["update/last_check"] = time.time() - 2 * 3600
    assert U.check(s, max_age=90 * 60)["version"] == "2.10.0" and len(api()) == 2


def test_recheck_timer_offers_the_update(monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMainWindow
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import update_ui as UU
    info = {"version": "9.9.9", "channel": "releases", "notes": "", "url": "", "zip": ""}
    monkeypatch.setattr(U, "check", lambda s, force=False, max_age=0: info)
    monkeypatch.setattr(UU, "_in_thread", lambda fn, done: done(fn()))
    w = QMainWindow()
    UU.start_auto_check(w)
    assert w._update_timer.isActive() and w._update_timer.interval() == UU.RECHECK_MS
    assert "9.9.9" in w._update_btn.text()
    info = {**info, "version": "9.9.10"}
    monkeypatch.setattr(U, "check", lambda s, force=False, max_age=0: info)
    UU.recheck(w)
    assert "9.9.10" in w._update_btn.text() and "9.9.10" in w.statusBar().currentMessage()
    w.close()


def test_notes_for_every_version_since_yours(monkeypatch):
    """3.0.27 → 3.0.30: release notes where a release exists, the README history line where not; newest first."""
    readme = ("# Daedalus\\n| Version | Changes |\\n|---|---|\\n"
              "| **3.0.31** | not yet |\\n| **3.0.30** | Speed · Fix: tall rows |\\n| **3.0.29** | Project Profile |\\n"
              "| **3.0.28** | Crash reports · PDF editing |\\n| **3.0.27** | already have it |\\n| 2.6 – 2.6.2 | old |\\n")

    def get(url, token=None, raw=False, timeout=20):
        if url.endswith("/releases?per_page=100"):
            return [{"tag_name": "v3.0.30", "body": "## Daedalus 3.0.30\n\n### Speed\n- faster saves"},
                    {"tag_name": "v3.0.28", "body": "- crash reports by email"},
                    {"tag_name": "v3.0.29", "body": "", "draft": True}]
        if url.endswith("/README.md"):
            return readme.replace("\\n", "\n").encode()
        raise U.UpdateError("unexpected " + url)
    monkeypatch.setattr(U, "_get", get)
    res = U.notes_between({"version": "3.0.30", "ref": "v3.0.30"}, "3.0.27", "x/y")
    md = res["markdown"]
    assert res["versions"] == ["3.0.30", "3.0.29", "3.0.28"]
    assert md.index("Daedalus 3.0.30") < md.index("Daedalus 3.0.29") < md.index("Daedalus 3.0.28")
    assert "faster saves" in md and "- Project Profile" in md and "crash reports by email" in md
    assert "already have it" not in md and "not yet" not in md
    assert U.readme_history("| 3.0.3 | a · b |")["3.0.3"] == "a · b"
    # nothing reachable: falls back to the update's own notes
    monkeypatch.setattr(U, "_get", lambda *a, **k: (_ for _ in ()).throw(U.UpdateError("offline")))
    assert U.notes_between({"version": "3.0.30", "notes": "just this"}, "3.0.27")["markdown"] == "just this"


def test_update_dialog_shows_all_notes(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import update_ui as UU
    monkeypatch.setattr(U, "notes_between", lambda info, cur, repo=None, token=None:
                        {"markdown": "## Daedalus 3.0.36\n\n- Icarus\n\n---\n\n## Daedalus 3.0.35\n\n- Projects",
                         "versions": ["3.0.36", "3.0.35"]})
    d = UU.UpdateDialog({"version": "3.0.36", "notes": "only the last", "url": ""})
    for _ in range(100):
        app.processEvents()
        if "3.0.35" in d.notes.toPlainText():
            break
        time.sleep(0.01)
    assert "Icarus" in d.notes.toPlainText() and "Projects" in d.notes.toPlainText()
    assert "2 updates" in d.notes_head.text()
    d.close()
