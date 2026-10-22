import json
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dh"))
    from PySide6.QtCore import QSettings
    s = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    return s


def test_recent_pins_search_and_missing(settings, tmp_path):
    from daedalus.ui.recent import Recent
    r = Recent(settings)
    a, b = tmp_path / "a.daedalus", tmp_path / "gear.step"
    a.write_text("{}")
    b.write_text("x")
    r.add(a)
    r.add(b)
    r.add(tmp_path / "gone.step")
    r.add(a)                                   # moves to top, no duplicate
    assert [e["name"] for e in r.entries()] == ["a.daedalus", "gone.step", "gear.step"]
    r.set_pinned(b, True)
    rows = r.entries()
    assert rows[0]["name"] == "gear.step" and rows[0]["pinned"]
    assert not next(e for e in rows if e["name"] == "gone.step")["exists"]
    assert [e["name"] for e in r.entries("gear")] == ["gear.step"]
    r.clear(keep_pinned=True)
    assert [e["name"] for e in r.entries()] == ["gear.step"]
    r.remove(b)
    assert r.entries() == []


def test_autosave_orphans_and_snapshot(settings, tmp_path):
    from daedalus.core.document import Document
    from daedalus.ui.recent import Autosave
    d = Document()
    d.add_sketch("S1", "XY") if hasattr(d, "add_sketch") else None
    d.dirty = True
    a = Autosave()
    a.write(d, "src.step")
    assert d.dirty and d.path is None          # snapshot leaves the document alone
    assert a.orphans() == []                   # own session is not an orphan
    b = Autosave()
    meta = json.loads(a._paths()[1].read_text())
    meta["pid"] = 2 ** 22 + 12345              # a process that is not running
    a._paths()[1].write_text(json.dumps(meta))
    got = b.orphans()
    assert [o["id"] for o in got] == [a.session]
    e = Document.open(got[0]["file"])
    assert len(e.features) == len(d.features)
    b.discard(a.session)
    assert b.orphans() == []


def test_ago_and_size():
    from daedalus.ui.recent import ago, human_size
    assert ago(100, now=100) == "just now"
    assert ago(0) == ""
    assert ago(100, now=100 + 7200) == "2 h ago"
    assert human_size(512) == "512 B"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_lazy_sklearn():
    import subprocess
    code = ("import daedalus, sys, build123d; print('sklearn' in sys.modules); "
            "from build123d import brep_from_stl as b; print(b.DBSCAN.__name__)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__))).stdout.split()
    assert out[0] == "False"


def test_view_wheel_sectors():
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui.view_wheel import ViewWheel
    w = ViewWheel(None, "Shaded", can_normal=False)
    c = w.c
    assert w._hit(c) == "fit"
    assert w._hit(c + QPointF(0, -80)) == "view:Top"
    assert w._hit(c + QPointF(80, 0)) == "view:Right"
    assert w._hit(c + QPointF(0, 80)) == "view:Bottom"
    assert w._hit(c + QPointF(-80, 0)) == "view:Left"
    assert w._hit(c + QPointF(-60, -60)) is None            # "Normal to" needs a picked face
    r = dict(w._chip_rects())["Wireframe"]
    assert w._hit(r.center()) == "mode:Wireframe"
    w.close()


def test_splash_draws():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui import splash
    s = splash.Splash("x")
    s.showMessage("Loading…")
    assert not s.w.pixmap().isNull()
    s.w.close()
    pm = splash.render({"glyph": "Δ", "size": 200, "clip": False, "rotate": 10})
    assert pm.width() == splash.W
    from PySide6.QtCore import QSettings
    import tempfile
    st = QSettings(tempfile.mktemp(suffix=".ini"), QSettings.IniFormat)
    splash.save({**splash.DEFAULTS, "size": 90}, st)
    assert splash.load(st)["size"] == 90 and splash.load(st)["glyph"] == "δ"
    d = splash.editor()
    d.cfg["size"] = 120
    d.close()
