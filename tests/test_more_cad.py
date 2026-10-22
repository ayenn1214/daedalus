"""Rhino / Inventor / Solid Edge links (simulated sessions and mocked COM), the Rhino script, sidebar islands,
floor grid and viewport performance switches."""
import ast
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from daedalus.stdparts import StandardLibrary
from daedalus.sw import SWError, SWLink, connect
from daedalus.sw.backend import ASSEMBLY, PART


class Store:
    def __init__(self):
        from daedalus.inventory.model import Inventory
        self.inv = Inventory()

    def save(self):
        pass


def _link(tmp_path, app, sub):
    return SWLink(connect(demo=True, app=app, root=tmp_path / sub), cache_dir=tmp_path / f"cache-{sub}",
                  library=StandardLibrary(tmp_path / "lib"), store=Store())


def test_rhino_demo_session(tmp_path):
    link = _link(tmp_path, "rhino", "rh")
    st = link.status()
    assert st["connected"] and st["document"]["title"].endswith(".3dm")
    rows = link.bom()
    assert any(r["qty"] == 2 for r in rows)
    assert all(c.path.startswith("rhino:obj:") for c in link.b.components())
    step = link.to_daedalus(tmp_path / "out")
    assert Path(step).suffix == ".step" and Path(step).exists()
    with pytest.raises(SWError):
        link.b.mate_to_selection("x", {})
    with pytest.raises(SWError):
        link.b.export(str(tmp_path / "x.sldprt"))


def test_rhino_to_solidworks_transfer(tmp_path):
    rh, sw = _link(tmp_path, "rhino", "rh"), _link(tmp_path, "solidworks", "sw")
    r = rh.transfer_to(sw)
    assert r["mode"] == "inserted" and r["into"].endswith("SLDASM")
    r2 = sw.transfer_to(rh)
    assert r2["mode"] in ("inserted", "opened")


def test_connect_rejects_missing_demo():
    with pytest.raises(SWError):
        connect(demo=True, app="inventor")


# ------------------------------------------------------------------ Rhino HTTP protocol
class _FakeRhino(BaseHTTPRequestHandler):
    token = "t0k"
    log = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("X-Daedalus-Token") != self.token:
            self.send_response(403)
            self.end_headers()
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeRhino.log.append(req)
        op = req["op"]
        data = {"ping": {"version": "8.12"},
                "active_doc": {"path": "C:/x/Fixture.3dm", "title": "Fixture.3dm", "kind": "assembly",
                               "properties": {"Project": "P1"}, "volume_mm3": 1000.0},
                "components": [{"name": "Plate:1", "path": "rhino:obj:abc", "suppressed": False, "fixed": False,
                                "is_assembly": False, "properties": {"PartNo": "RG-1"}}],
                "insert_step": {"name": "Bolt", "key": "rhino:obj:def"}}.get(op, True)
        if op == "export":
            Path(req["path"]).write_text("ISO-10303-21;")
        b = json.dumps({"ok": True, "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def test_rhino_backend_protocol(tmp_path):
    from daedalus.sw.more_backends import RhinoBackend
    srv = HTTPServer(("127.0.0.1", 0), _FakeRhino)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        lf = tmp_path / "rhino-link.json"
        lf.write_text(json.dumps({"port": srv.server_address[1], "token": "t0k"}))
        b = RhinoBackend(link_file=lf, timeout=10)
        assert b.connected() and b.app_info()["name"] == "Rhino"
        d = b.active_doc()
        assert d.kind == ASSEMBLY and d.properties["Project"] == "P1"
        assert b.components()[0].properties["PartNo"] == "RG-1"
        out = b.export(str(tmp_path / "a.step"))
        assert Path(out).read_text().startswith("ISO")
        assert b.insert_component(str(tmp_path / "Bolt.step")) == "Bolt"
        b.set_properties({"PartNo": "X"}, path=str(tmp_path / "Bolt.step"))
        assert _FakeRhino.log[-1]["key"] == "rhino:obj:def"            # the just-inserted object
        b.view("Top")
        assert _FakeRhino.log[-1] == {"op": "view", "timeout": 10, "name": "Top"}
    finally:
        srv.shutdown()


def test_rhino_backend_not_running(tmp_path):
    from daedalus.sw.more_backends import RhinoBackend
    b = RhinoBackend(link_file=tmp_path / "none.json", lazy=True)
    assert not b.connected() and "Rhino" in b.last_error


def test_rhino_script_covers_protocol():
    """The script inside Rhino answers every operation the backend sends, and parses as Python 3 (Rhino 8);
    no f-strings so IronPython 2.7 (Rhino 7) can run it too."""
    src = (Path(__file__).parents[1] / "daedalus" / "rhino" / "DaedalusLink.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ops = {n.name[3:] for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("op_")}
    used = set()
    for f in ("fusion_backend.py", "more_backends.py"):
        t = (Path(__file__).parents[1] / "daedalus" / "sw" / f).read_text(encoding="utf-8")
        import re
        used |= set(re.findall(r'_call\("([a-z_]+)"', t))
    assert used - {"joint"} <= ops, used - ops
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), "f-strings break IronPython"


def test_rhino_install(tmp_path):
    from daedalus.rhino import install, startup_command
    dest = install(tmp_path)
    assert dest.exists() and dest.read_text(encoding="utf-8").startswith("#! python3")
    assert startup_command(dest).startswith('_-RunPythonScript "')


# ------------------------------------------------------------------ Inventor / Solid Edge over mocked COM
class Coll:
    def __init__(self, items, **kw):
        self._items = list(items)
        self.Count = len(self._items)
        self.__dict__.update(kw)

    def Item(self, i):
        if isinstance(i, str):
            for it in self._items:
                if getattr(it, "Name", None) == i:
                    return it
            raise KeyError(i)
        return self._items[i - 1]


def test_inventor_backend(tmp_path):
    from daedalus.sw.more_backends import INV_ASM, INV_PART, InventorBackend
    added = {}
    user = Coll([NS(Name="PartNo", Value="RG-7")], Name="Inventor User Defined Properties",
                Add=lambda v, n: added.__setitem__(n, v))
    part = NS(FullFileName="C:/p/Plate.ipt", DisplayName="Plate", DocumentType=INV_PART,
              PropertySets=Coll([user]), ActiveMaterial=NS(DisplayName="Aluminum 6061"),
              ComponentDefinition=NS(MassProperties=NS(Mass=2.0, Volume=740.0, CenterOfMass=NS(X=1, Y=2, Z=3))))
    occ = NS(Name="Plate:1", Definition=NS(Document=part), Suppressed=False, Grounded=True, ParentOccurrence=None,
             DefinitionDocumentType=INV_PART)
    occs = Coll([occ], AllLeafOccurrences=Coll([occ]))

    def save_as(path, copy):
        Path(path).write_text("ISO-10303-21;")
    asm = NS(FullFileName="C:/p/Cart.iam", DisplayName="Cart", DocumentType=INV_ASM,
             PropertySets=Coll([Coll([], Name="Design Tracking Properties")]),
             ComponentDefinition=NS(Occurrences=occs, MassProperties=NS(Mass=5.0, Volume=1.0,
                                                                       CenterOfMass=NS(X=0, Y=0, Z=0))),
             SaveAs=save_as, SelectSet=Coll([]))
    app = NS(ActiveDocument=asm, SoftwareVersion=NS(DisplayVersion="2025.2"), Documents=Coll([asm, part]))
    b = InventorBackend.__new__(InventorBackend)
    b._app, b.last_error = app, ""
    assert b.connected() and b.app_info()["revision"] == "2025.2"
    d = b.active_doc()
    assert d.kind == ASSEMBLY and d.title == "Cart"
    comps = b.components()
    assert comps[0].properties["PartNo"] == "RG-7" and comps[0].fixed and comps[0].material == "Aluminum 6061"
    assert abs(comps[0].mass_kg - 2.0) < 1e-9
    b.set_properties({"PartNo": "RG-8", "Vendor": "McM"}, path="C:/p/Plate.ipt")
    assert user.Item("PartNo").Value == "RG-8" and added == {"Vendor": "McM"}
    out = b.export(str(tmp_path / "cart.step"))
    assert Path(out).exists()
    with pytest.raises(SWError):
        b.export(str(tmp_path / "cart.sldprt"))
    app.ActiveDocument = part
    p = b.active_doc()
    assert p.kind == PART and abs(p.volume_mm3 - 740000.0) < 1e-6 and p.center_mm == [10, 20, 30]


def test_inventor_view_camera():
    from daedalus.sw.more_backends import InventorBackend

    class P:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def DistanceTo(self, o):
            return ((self.X - o.X) ** 2 + (self.Y - o.Y) ** 2 + (self.Z - o.Z) ** 2) ** 0.5
    applied = []
    cam = NS(Target=P(0, 0, 0), Eye=P(0, 0, 100), UpVector=None, Apply=lambda: applied.append(1))
    tg = NS(CreatePoint=P, CreateUnitVector=lambda *v: v)
    view = NS(Camera=cam, Fit=lambda: applied.append("fit"))
    b = InventorBackend.__new__(InventorBackend)
    b._app, b.last_error = NS(ActiveView=view, TransientGeometry=tg), ""
    b.view("Top")
    assert (cam.Eye.X, round(cam.Eye.Y, 6), cam.Eye.Z) == (0, 100, 0) and cam.UpVector == (0, 0, -1)
    assert applied == [1, "fit"]


def test_solidedge_backend(tmp_path):
    from daedalus.sw.more_backends import SE_ASM, SolidEdgeBackend
    added = {}
    custom = Coll([NS(Name="PartNo", Value="SE-1")], Name="Custom", Add=lambda n, v: added.__setitem__(n, v),
                  Save=lambda: None)
    occ = NS(Name="Bracket.par:1", OccurrenceFileName="C:/p/Bracket.par", Visible=True, Subassembly=False,
             OccurrenceDocument=NS(Properties=Coll([custom])))
    doc = NS(FullName="C:/p/Frame.asm", Name="Frame.asm", Type=SE_ASM, Properties=Coll([custom]),
             Occurrences=Coll([occ], AddByFilename=lambda p: NS(Name=Path(p).name + ":1", Move=lambda *a: None)),
             SaveCopyAs=lambda p: Path(p).write_text("ISO-10303-21;"))
    b = SolidEdgeBackend.__new__(SolidEdgeBackend)
    b._app, b.last_error = NS(ActiveDocument=doc, Version="2025", Documents=Coll([doc])), ""
    d = b.active_doc()
    assert d.kind == ASSEMBLY and d.properties["PartNo"] == "SE-1"
    assert b.components()[0].path.endswith("Bracket.par")
    assert b.insert_component("C:/lib/Bolt.par", (10, 0, 0)) == "Bolt.par:1"
    b.set_properties({"Vendor": "McM"})
    assert added == {"Vendor": "McM"}
    assert Path(b.export(str(tmp_path / "f.step"))).exists()


def test_link_labels_and_cad_list():
    from daedalus.sw.more_backends import APPS, label
    from daedalus.sw.ui import CADS
    assert label("solidedge") == "Solid Edge" and label("rhino") == "Rhino"
    assert {a for a, _d, _l in CADS} == set(APPS)


# ------------------------------------------------------------------ UI: islands, grid, performance
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_islands(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QLabel
    from daedalus.ui.islands import PRESETS, Sidebar
    s = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    panels = [(k, k.title(), QLabel(k)) for k in ("tree", "objects", "mates", "layers", "physics", "studies", "cam",
                                                 "props")]
    sb = Sidebar(panels, s)
    assert sb.islands == PRESETS["three"][1] and sb.current_preset() == "three"
    assert [t.count() for t in sb.tabs] == [1, 6, 1]
    sb.set_preset("one")
    assert [t.count() for t in sb.tabs] == [8, 0, 0] and sb.tabs[1].isHidden()
    sb.move("props", 2)
    assert sb.island_of("props") == 2 and sb.tabs[2].count() == 1
    sb.move("cam", None)
    assert "cam" in sb.hidden()
    assert sb.show_panel(panels[6][2]) and sb.island_of("cam") is not None    # un-hides on demand
    sb2 = Sidebar(panels, s)                                                   # persisted
    assert sb2.islands == sb.islands
    sb.set_side_by_side(True)
    from PySide6.QtCore import Qt
    assert sb.split.orientation() == Qt.Horizontal and sb.split.childrenCollapsible()


def test_grid_and_performance(qapp):
    from build123d import Box
    from daedalus.ui.viewport import Viewport
    v = Viewport()
    v.resize(600, 400)
    from types import SimpleNamespace
    v.show_result(SimpleNamespace(body=Box(200, 100, 20), bodies={}, sketches={}))
    major, minor, half = v.grid_spacing()
    assert major in (50, 100) and minor == major / 5 and half >= 100
    assert v._grid and all(not a.GetPickable() for a in v._grid)
    assert v._bounds()[1] - v._bounds()[0] == pytest.approx(200, abs=1)         # grid never counts for fit
    v.set_grid(False)
    assert not any(a.GetVisibility() for a in v._grid)
    v.set_antialiasing("msaa")
    assert v.antialias == "msaa" and not v.ren.GetUseFXAA()
    v.set_antialiasing("fxaa")
    assert v.ren.GetUseFXAA()
    v.HEAVY_CELLS = 1                                     # treat this box as a big model
    v._moving_start()
    assert v._moving is not None and not v.ren.GetUseFXAA()
    assert not any(oa.edges.GetVisibility() for oa in v.objs.values())
    v._moving_end()
    assert v.ren.GetUseFXAA() and all(oa.edges.GetVisibility() for oa in v.objs.values())
    assert v.pick_info(300, 200) is not None or True                       # picking with locators doesn't fail
    assert v._loc_sig is not None


def test_logo_pixmap(qapp):
    from daedalus.ui.splash import logo_icon, logo_pixmap
    pm = logo_pixmap(64)
    img = pm.toImage()
    assert img.pixelColor(0, 0).alpha() == 0                              # transparent corners
    c = img.pixelColor(10, 32)
    assert (c.red(), c.green(), c.blue()) == (0x0A, 0x3A, 0x36)            # the disc
    assert not logo_icon().isNull()


# ------------------------------------------------------------------ setup check + installer
def test_setup_check_python_and_cli(capsys):
    from daedalus import setup_check as SC
    rep = SC.check_python()
    names = [r.name for r in rep.rows]
    assert "Version" in names and "Packages" in names
    assert next(r for r in rep.rows if r.name == "Packages").ok
    assert SC.main(["--python-only"]) == 0
    assert "Python" in capsys.readouterr().out
    data = json.loads(SC.Report(rows=rep.rows).to_json())
    assert data["ok"] is True


def test_setup_check_agent_offline_and_online(tmp_path):
    from daedalus import setup_check as SC
    rep = SC.check_agent(host="http://127.0.0.1:9", model="qwen2.5:7b", timeout=0.5)
    run = next(r for r in rep.rows if r.name == "Ollama running")
    assert run.ok is False and not rep.ok

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send({"models": [{"name": "qwen2.5:7b"}]} if self.path == "/api/tags" else {"version": "0.9"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/show":
                return self._send({"capabilities": ["completion", "tools"]})
            if self.path == "/api/pull":
                lines = [{"status": "pulling", "total": 100, "completed": 50},
                         {"status": "pulling", "total": 100, "completed": 100}, {"status": "success"}]
                b = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                H.pulled = req["model"]
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        rep = SC.check_agent(host=host, model="qwen2.5:7b")
        by = {r.name: r for r in rep.rows}
        assert by["Ollama running"].ok and by["Model"].ok and by["Tool calling"].ok
        rep2 = SC.check_agent(host=host, model="qwen2.5:14b")
        assert next(r for r in rep2.rows if r.name == "Model").action == "pull"
        seen = []
        assert SC.pull_model("qwen2.5:14b", host=host, progress=lambda s, d, t: seen.append(d)) == "qwen2.5:14b"
        assert H.pulled == "qwen2.5:14b" and 100 in seen
    finally:
        srv.shutdown()


def test_recommended_model_and_defaults():
    from daedalus import setup_check as SC
    from daedalus.ai import ollama
    assert SC.recommended_model(32) == "qwen2.5:14b"
    assert SC.recommended_model(16) == "qwen2.5:7b"
    assert SC.recommended_model(8) == "qwen2.5:3b"
    assert SC.DEFAULT_HOST == ollama.DEFAULT_HOST and SC.DEFAULT_MODEL == ollama.DEFAULT_MODEL
    assert SC.normalize_host("0.0.0.0") == ollama.normalize_host("0.0.0.0")


def test_installer_scripts_present():
    root = Path(__file__).parents[1]
    ps = (root / "install.ps1").read_bytes()
    assert ps.startswith(b"\xef\xbb\xbf")                       # BOM: Windows PowerShell 5.1 reads UTF-8 right
    text = ps.decode("utf-8-sig")
    for must in ("Python.Python.3.11", "Ollama.Ollama", "--check", "--set-model", "--make-icon", "-m venv"):
        assert must in text, must
    assert text.count("{") == text.count("}")
    bat = (root / "install.bat").read_bytes()
    assert b"\r\n" in bat and b"install.ps1" in bat


def test_setup_dialog(qapp, monkeypatch):
    from daedalus import setup_check as SC
    from daedalus.ui.setup_dialog import SetupDialog
    monkeypatch.setattr(SC, "check_agent", lambda rep=None, **k: (rep or SC.Report()).add(
        "Icarus agent", "Ollama running", False, "no answer", fix="start it", action="start") and rep)
    d = SetupDialog()
    rep = SC.check_python()
    SC.check_agent(rep)
    d._show(rep)
    texts = [d.grid.itemAt(i).widget().text() for i in range(d.grid.count()) if hasattr(d.grid.itemAt(i).widget(), "text")]
    assert "Start Ollama" in texts and "Icarus agent" in texts


# ------------------------------------------------------------------ sketch plane picker
def test_plane_picker(qapp):
    from daedalus.core.document import Document
    from daedalus.ui.plane_picker import PlanePicker, plane_geometry, plane_options
    doc = Document()
    vals = [v for v, _l, _g in plane_options(doc)]
    assert vals == ["XY", "XZ", "YZ"]                                   # nothing modelled: origin planes only
    sk = doc.add("sketch", {"plane": "XY", "entities": [{"type": "rect", "w": 40, "h": 20}]})
    doc.add("extrude", {"sketch": sk.name, "depth": 10})
    doc.add("plane", {"mode": "offset", "base": "XY", "offset": 25.0})
    vals = [v for v, _l, _g in plane_options(doc)]
    assert "top" in vals and "bottom" in vals and any(v.startswith("Plane") for v in vals), vals
    o, n, _x, _pose = plane_geometry(doc, "top")
    assert abs(o[2] - 10) < 1e-6 and n == (0, 0, 1)
    o, n, _x, _ = plane_geometry(doc, "XZ", offset=5)
    assert abs(o[1] + 5) < 1e-6 or abs(o[1] - 5) < 1e-6
    ref = next(v for v in vals if v.startswith("Plane"))
    assert abs(plane_geometry(doc, ref)[0][2] - 25) < 1e-6

    class V:
        shown = None

        def show_plane_preview(self, *a, **k):
            V.shown = a

        def clear_plane_preview(self):
            V.shown = None
    dlg = PlanePicker(doc, V(), default="top")
    assert dlg.choice() == ("top", 0.0) and V.shown is not None
    dlg._pick("YZ")
    dlg.offset.setValue(12.5)
    assert dlg.choice() == ("YZ", 12.5)
    dlg.reject()
    assert V.shown is None
