"""Startup modules: boot only 3D / simulation / drawings / inventory; drawings and inventory without the kernel."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from daedalus import modules as MOD
from daedalus.errors import FeatureError

ROOT = Path(__file__).resolve().parents[1]
HEAVY = ("build123d", "OCP", "vtkmodules", "mujoco", "gmsh", "skfem", "numba")


def test_parse_and_flags():
    assert MOD.parse("drawing,inventory") == {"drawing", "inventory"}
    assert MOD.parse("Simulation") == {"sim"} and MOD.parse("holocron 2d") == {"inventory", "drawing"}
    assert MOD.parse("all") == MOD.ALL and MOD.parse("") == MOD.ALL
    with pytest.raises(MOD.ModuleError):
        MOD.parse("cam")
    argv = ["--only", "inventory", "part.daedalus"]
    assert MOD.from_argv(argv) == {"inventory"} and MOD.strip_args(argv) == ["part.daedalus"]
    assert MOD.from_argv(["--modules=sim,3d"]) == {"sim", "3d"} and MOD.from_argv(["x.step"]) is None
    assert not MOD.needs_3d({"drawing", "inventory"}) and MOD.needs_3d({"sim"})
    assert MOD.hidden_tabs({"sim"}) >= {"Model", "Features", "Drawing", "Inventory"}
    assert "Simulation" not in MOD.hidden_tabs({"sim"}) and MOD.hidden_islands({"3d"}) == {"physics", "studies"}


def test_settings_round_trip():
    class S(dict):
        def value(self, k, d=None):
            return self.get(k, d)

        def setValue(self, k, v):          # noqa: N802
            self[k] = v
    s = S()
    assert MOD.load(s) == MOD.ALL
    MOD.save(s, {"inventory", "drawing"})
    assert s[MOD.SETTINGS_KEY] == "drawing,inventory" and MOD.load(s) == {"drawing", "inventory"}
    s[MOD.SETTINGS_KEY] = "bogus"
    assert MOD.load(s) == MOD.ALL


def _run(code: str) -> str:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, env=env,
                       timeout=240, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip().splitlines()[-1]


def test_light_modules_never_load_the_kernel():
    out = _run(f"""
        import sys
        from daedalus.drawing.doc2d import Doc2D
        from daedalus.ui import lite_window, drawing_ui, inventory_ui
        print(sorted(m for m in {HEAVY!r} if m in sys.modules))
    """)
    assert out == "[]"


def test_main_boots_light_window_for_inventory():
    out = _run("""
        import sys
        from PySide6.QtWidgets import QApplication
        def fake_exec(self):
            wins = [type(w).__name__ for w in QApplication.topLevelWidgets() if w.isVisible()]
            print("KERNEL" if "build123d" in sys.modules else "LIGHT", wins)
            return 0
        QApplication.exec = fake_exec
        sys.argv = ["daedalus", "--only", "inventory"]
        from daedalus.__main__ import main
        try:
            main()
        except SystemExit:
            pass
    """)
    assert out.startswith("LIGHT") and "LiteWindow" in out


def test_doc2d_drawings_without_model(tmp_path):
    from daedalus.drawing.doc2d import Doc2D
    d = Doc2D()
    dr = d.new_drawing(size="A3")
    d.add_drawing_item({"t": "line", "a": [10, 10], "b": [100, 10]})
    d.add_drawing_item({"t": "circle", "c": [50, 50], "r": 20})
    d.add_revision("First issue")
    assert len(dr.sheets[0].items) == 2 and dr.sheets[0].revisions[0]["rev"] == "A"
    assert d.undo() and len(d.drawing().sheets[0].revisions) == 0
    assert d.redo() and len(d.drawing().sheets[0].revisions) == 1
    out = d.export_drawing(tmp_path / "a.dxf")
    assert Path(out).exists()
    back = d.open_2d(out)
    assert back.name != d.drawings[0].name and any(back.sheets[0].items)
    for bad in (lambda: d.new_drawing(views=("front",)), lambda: d.add_drawing_view(kind="projected"),
                lambda: d.new_drawing(flat="SheetMetal1")):
        with pytest.raises(FeatureError, match="3D workspace"):
            bad()
    f = tmp_path / "part.daedalus"
    f.write_text(json.dumps({"drawings": [x.to_dict() for x in d.drawings]}))
    d2 = Doc2D()
    assert len(d2.load_drawings(f)) == 2
    f.write_text("{}")
    with pytest.raises(FeatureError):
        d2.load_drawings(f)


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_lite_window_pages(qapp):
    from daedalus.ui.lite_window import LiteWindow
    w = LiteWindow({"drawing", "inventory"}, open_full=lambda: "FULL")
    assert w.tabs.count() == 2 and w.drawing_page.lite and w.inventory_page.lite
    assert w.doc.new_drawing().name == "Drawing1"
    w.open_full()
    assert w.full == "FULL"
    w.doc.dirty = False
    w.close()
    one = LiteWindow({"inventory"})
    assert one.tabs.count() == 1 and not one.tabs.tabBar().isVisible() and one.drawing_page is None


def test_main_window_simulation_only(qapp):
    from daedalus.ui.main_window import MainWindow
    w = MainWindow(modules={"sim"})
    tb = w.toolbelt
    vis = {tb.tabText(i) for i in range(tb.count()) if tb.isTabVisible(i)}
    assert "Simulation" in vis and "Evaluate" in vis and not vis & {"Model", "Features", "Drawing", "Inventory"}
    assert tb.tabText(tb.currentIndex()) in vis
    assert {"physics", "studies"} <= set(w.sidebar.keys) and not {"mates", "cam"} & set(w.sidebar.keys)
    assert {g for g, _ in w.tool_groups}.isdisjoint({"Model", "Drawing", "Inventory"})
    w.show_inventory()
    assert w.stack.currentIndex() != 4 and "Inventory isn't loaded" in w.statusBar().currentMessage()
    assert "Simulation" in w.windowTitle()
    w.doc.dirty = False
    w.close()


def test_main_window_all_modules_unchanged(qapp):
    from daedalus.ui.main_window import MainWindow
    w = MainWindow()
    tb = w.toolbelt
    assert all(tb.isTabVisible(i) for i in range(tb.count()))
    assert {"mates", "physics", "studies", "cam"} <= set(w.sidebar.keys)
    w.doc.dirty = False
    w.close()
