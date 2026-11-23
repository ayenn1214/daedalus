"""Settings window: every section builds, changes save and apply live, search, export / import, reset."""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit  # noqa: E402

from daedalus.ui import settings_ui as S  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def clean(qapp, tmp_path, monkeypatch):
    s = QSettings("Daedalus", "Daedalus")
    keep = {k: s.value(k) for k in s.allKeys()}
    s.clear()
    yield s
    s.clear()
    for k, v in keep.items():
        s.setValue(k, v)


class FakeView:
    zoom_to_cursor = True
    animate_views = True


class FakeWindow:
    def __init__(self):
        self.view = FakeView()
        self.calls = []

    def set_nav(self, v):
        self.calls.append(("nav", v))

    def switch_theme(self, v):
        self.calls.append(("theme", v))


def test_every_section_builds(clean):
    d = S.SettingsDialog(None)
    for i, sec in enumerate(d.secs):
        d.nav.setCurrentRow(i)
        assert sec["key"] in d.pages
    assert {s["key"] for s in d.secs} >= {"general", "view", "sketch", "units", "icarus", "claude", "dims",
                                         "company", "folders", "cadlink", "updates", "advanced"}


def test_change_saves_and_applies_live(clean):
    w = FakeWindow()
    d = S.SettingsDialog(w, "view")
    opts = {o.key: o for sec in d.secs for o in sec["opts"]}
    d.set(opts["view/zoom_cursor"], False)
    assert clean.value("view/zoom_cursor") in (False, "false") and w.view.zoom_to_cursor is False
    d.set(opts["view/nav"], "blender")
    assert ("nav", "blender") in w.calls and clean.value("view/nav") == "blender"
    d.set(opts["ollama/host"], "")
    assert not clean.contains("ollama/host")


def test_rows_reflect_saved_values(clean):
    clean.setValue("sketch/rapid", True)
    clean.setValue("ollama/model", "qwen2.5:7b")
    d = S.SettingsDialog(None, "sketch")
    row = next(r for r, o in d.rows["sketch"] if o.key == "sketch/rapid")
    assert row.findChild(QCheckBox).isChecked()
    d.nav.setCurrentRow(next(i for i, s in enumerate(d.secs) if s["key"] == "icarus"))
    row = next(r for r, o in d.rows["icarus"] if o.key == "ollama/model")
    assert row.findChild(QLineEdit).text() == "qwen2.5:7b"
    row = next(r for r, o in d.rows["icarus"] if o.key == "ollama/num_ctx")
    assert row.findChild(QComboBox).currentData() == "16384"


def test_search_filters_sections_and_rows(clean):
    d = S.SettingsDialog(None, "general")
    d.search.setText("ollama")
    shown = [d.secs[i]["key"] for i in range(d.nav.count()) if not d.nav.item(i).isHidden()]
    assert shown == ["icarus"]
    assert d.secs[d.nav.currentRow()]["key"] == "icarus"
    vis = {o.key for r, o in d.rows["icarus"] if not r.isHidden()}
    assert "ollama/model" in vis and "api_key" not in vis
    d.search.setText("logo")
    assert [d.secs[i]["key"] for i in range(d.nav.count()) if not d.nav.item(i).isHidden()] == ["company"]
    d.search.setText("")
    assert all(not d.nav.item(i).isHidden() for i in range(d.nav.count()))


def test_export_import_skips_secrets(clean, tmp_path):
    clean.setValue("ui/theme", "light")
    clean.setValue("api_key", "sk-secret")
    clean.setValue("update/token", "ghp_x")
    clean.setValue("ui/geometry", b"\x01")
    p = tmp_path / "s.json"
    n = S.export_settings(p)
    data = json.loads(p.read_text())["settings"]
    assert n == len(data) and data["ui/theme"] == "light"
    assert "api_key" not in data and "update/token" not in data and "ui/geometry" not in data
    clean.clear()
    data["api_key"] = "sneaky"
    p.write_text(json.dumps({"daedalus_settings": 1, "settings": data}))
    S.import_settings(p)
    assert clean.value("ui/theme") == "light" and not clean.contains("api_key")
    p.write_text("[1, 2]")
    with pytest.raises(ValueError):
        S.import_settings(p)


def test_reset_section_keeps_secrets(clean):
    clean.setValue("view/grid", False)
    clean.setValue("api_key", "sk")
    clean.setValue("ollama/model", "x")
    S.reset_section(next(s for s in S.sections() if s["key"] == "view"))
    S.reset_section(next(s for s in S.sections() if s["key"] == "icarus"))
    assert not clean.contains("view/grid") and not clean.contains("ollama/model")
    assert clean.value("api_key") == "sk"


def test_main_and_lite_windows_open_settings(clean, monkeypatch):
    from daedalus.ui.lite_window import LiteWindow
    from daedalus.ui.main_window import MainWindow
    opened = []
    monkeypatch.setattr(S, "open_settings", lambda w, sec=None: opened.append((type(w).__name__, sec)))
    for w in (MainWindow(), LiteWindow({"inventory"})):
        acts = [a for m in w.menuBar().actions() if m.menu() for a in m.menu().actions() if a.text() == "Settings…"]
        assert acts and acts[0].shortcut().toString() == "Ctrl+,"
        acts[0].trigger()
    assert [o[0] for o in opened] == ["MainWindow", "LiteWindow"]


def test_real_window_pages(clean):
    from daedalus.ui.main_window import MainWindow
    w = MainWindow()
    d = S.SettingsDialog(w)
    for i in range(len(d.secs)):
        d.nav.setCurrentRow(i)
    text = " ".join(l.text() for l in d.findChildren(S.QLabel))
    assert "Couldn't show this part" not in text
