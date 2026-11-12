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


def test_check_throttles_and_skips(monkeypatch):
    calls = []
    fake_github(monkeypatch, calls=calls)
    monkeypatch.setattr(U, "current", lambda: {"version": "2.9.0", "commit": "", "git": False, "git_dir": False})
    s = S()
    info = U.check(s)
    assert info["version"] == "2.10.0" and len(calls) == 1
    assert U.check(s)["version"] == "2.10.0" and len(calls) == 1            # within 20 h: cached, no network
    s["update/skip"] = "2.10.0"
    assert U.check(s) is None
    assert U.check(s, force=True)["version"] == "2.10.0" and len(calls) == 2   # Help ▸ Check: always asks
    s["update/last_check"] = time.time() - 21 * 3600
    assert U.check(s) is None and len(calls) == 3


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
