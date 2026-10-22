"""Fusion: the real add-in module running against a mock `adsk` API, driven by FusionBackend over HTTP;
plus the simulated Fusion session and SolidWorks ⇄ Fusion transfers."""
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from daedalus.fusion import addin_source, install
from daedalus.sw import SWError, SWLink
from daedalus.sw.backend import FakeBackend
from daedalus.sw.fusion_backend import FakeFusionBackend, FusionBackend


class Coll(list):
    @property
    def count(self):
        return len(self)

    def item(self, i):
        return self[i]


class Attrs:
    def __init__(self):
        self.d = {}

    def add(self, group, name, value):
        self.d[(group, name)] = value

    def itemsByGroup(self, group):
        return [types.SimpleNamespace(name=n, value=v) for (g, n), v in self.d.items() if g == group]


class Comp:
    def __init__(self, name, pn="", desc="", mass=1.0):
        self.name, self.id, self.partNumber, self.description = name, name.lower(), pn, desc
        self.attributes = Attrs()
        self.occurrences = Coll()
        self.allOccurrences = Coll()
        self.material = types.SimpleNamespace(name="Steel")
        self.bRepBodies = Coll()
        self._mass = mass

    def getPhysicalProperties(self, _acc):
        return types.SimpleNamespace(mass=self._mass, volume=self._mass / 7.85e-3,
                                     centerOfMass=types.SimpleNamespace(x=1, y=2, z=3))


def make_adsk():
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    adsk = types.ModuleType("adsk")
    adsk.core, adsk.fusion = core, fusion

    class CustomEventHandler:
        pass
    core.CustomEventHandler = CustomEventHandler

    class Event:
        def __init__(self):
            self.handlers = []

        def add(self, h):
            self.handlers.append(h)

    root = Comp("Fixture", desc="Cam lock fixture", mass=3.0)
    plate, lever = Comp("Base Plate", "RG-5101", "Base plate", 1.5), Comp("Cam Lever", "", "Cam lever", 0.2)
    occs = [types.SimpleNamespace(fullPathName="Base Plate:1", name="Base Plate:1", component=plate,
                                  isLightBulbOn=True, isGrounded=True, assemblyContext=None)]
    occs += [types.SimpleNamespace(fullPathName=f"Cam Lever:{i}", name=f"Cam Lever:{i}", component=lever,
                                   isLightBulbOn=True, isGrounded=False, assemblyContext=None) for i in (1, 2)]
    root.occurrences = Coll(occs)
    root.allOccurrences = Coll(occs)
    design = types.SimpleNamespace(rootComponent=root, allComponents=[root, plate, lever], computeAll=lambda: None)

    class Design:
        @staticmethod
        def cast(x):
            return x if x is design else None
    fusion.Design = Design
    fusion.CalculationAccuracy = types.SimpleNamespace(LowCalculationAccuracy=0)
    fusion.BRepFace = types.SimpleNamespace(cast=lambda x: None)

    class App:
        version = "2.0.99999"
        currentUser = types.SimpleNamespace(displayName="AN")
        activeProduct = design
        activeDocument = types.SimpleNamespace(name="Cam Lock Fixture v3", dataFile=types.SimpleNamespace(id="urn:1"))
        userInterface = types.SimpleNamespace(activeSelections=Coll(), messageBox=print)
        events = {}

        def registerCustomEvent(self, eid):
            self.events[eid] = Event()
            return self.events[eid]

        def unregisterCustomEvent(self, eid):
            self.events.pop(eid, None)

        def fireCustomEvent(self, eid, info):
            ev = self.events[eid]
            # Fusion runs handlers later on its main thread; emulate with another thread
            threading.Thread(target=lambda: [h.notify(types.SimpleNamespace(additionalInfo=info))
                                             for h in ev.handlers]).start()
            return True
    app = App()
    core.Application = types.SimpleNamespace(get=lambda: app)
    return adsk, core, fusion, design


@pytest.fixture
def addin(tmp_path, monkeypatch):
    adsk, core, fusion, design = make_adsk()
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)
    src = addin_source() / "DaedalusLink.py"
    spec = importlib.util.spec_from_file_location("DaedalusLink_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LINK_FILE = str(tmp_path / "fusion-link.json")
    mod.run(None)
    yield mod, design
    mod.stop(None)


def test_addin_round_trip(addin, tmp_path):
    mod, design = addin
    link_file = Path(mod.LINK_FILE)
    cfg = json.loads(link_file.read_text())
    assert cfg["port"] and len(cfg["token"]) > 20
    b = FusionBackend(link_file=link_file, timeout=10)
    assert b.connected() and b.app_info()["revision"] == "2.0.99999"
    d = b.active_doc()
    assert d.title == "Cam Lock Fixture v3" and d.kind == 2 and abs(d.mass_kg - 3.0) < 1e-9
    comps = b.components()
    assert [c.name for c in comps] == ["Base Plate:1", "Cam Lever:1", "Cam Lever:2"]
    assert comps[0].fixed and comps[0].properties["PartNo"] == "RG-5101"
    b.set_properties({"PartNo": "RG-5102", "HolocronId": "inv-7"}, path=comps[1].path)
    lever = design.allComponents[2]
    assert lever.partNumber == "RG-5102" and b.get_properties(comps[1].path)["HolocronId"] == "inv-7"
    with pytest.raises(SWError, match="Unknown operation"):
        b._call("launch")
    with pytest.raises(SWError, match="Fusion scripts"):
        b.run_macro("x.swp")
    # BOM through the Link layer, names from components (no files in Fusion)
    rows = SWLink(b, cache_dir=tmp_path / "c").bom()
    assert {r["part"]: r["qty"] for r in rows} == {"RG-5101": 1, "RG-5102": 2}
    # wrong token is refused
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**cfg, "token": "nope"}))
    with pytest.raises(SWError, match="bad token"):
        FusionBackend(link_file=bad, timeout=5)


def test_addin_not_running(tmp_path):
    with pytest.raises(SWError, match="Install Fusion add-in"):
        FusionBackend(link_file=tmp_path / "missing.json")


def test_install_copies_addin(tmp_path):
    dest = install(tmp_path / "AddIns")
    assert (dest / "DaedalusLink.py").exists() and (dest / "DaedalusLink.manifest").exists()
    assert json.loads((dest / "DaedalusLink.manifest").read_text())["type"] == "addin"


def test_fake_fusion_and_cross_cad(tmp_path):
    from daedalus.stdparts import StandardLibrary
    lib = StandardLibrary(tmp_path / "lib")
    fu = SWLink(FakeFusionBackend(tmp_path / "fu"), cache_dir=tmp_path / "cf", library=lib)
    sw = SWLink(FakeBackend(tmp_path / "sw"), cache_dir=tmp_path / "cs", library=lib)
    rows = {r["part"]: r for r in fu.bom()}
    assert rows["Pivot Pin"]["qty"] == 2 and rows["RG-5102"]["qty"] == 2
    fu.b.select_hole()
    r = fu.insert_fastener("socket head cap screw", "M5", 16)
    assert r["file"].endswith(".step") and r["mates"][0].startswith("Revolute")       # STEP direct, joint
    comp = next(c for c in fu.b.components() if c.name == r["component"])
    assert comp.properties["PartNo"].startswith("SHCS M5 x 16")
    p = fu.command("export", "step")
    assert Path(p).exists() and "exports" in p
    assert fu.to_daedalus(tmp_path / "rt").endswith(".step")
    # SolidWorks part → Fusion assembly (inserted), with properties
    sw.b.open_doc(next(k for k in sw.b.docs if k.endswith("Lift Arm.SLDPRT")))
    t = sw.transfer_to(fu)
    assert t["mode"] == "inserted" and t["into"] == "Cam Lock Fixture v3"
    got = next(c for c in fu.b.components() if c.name == t["component"])
    assert got.properties["PartNo"] == "RG-3002"
    # Fusion design → SolidWorks assembly (converted to a native part, inserted)
    sw.b.open_doc(next(k for k in sw.b.docs if k.endswith(".SLDASM")))
    t2 = fu.transfer_to(sw)
    assert t2["mode"] == "inserted" and t2["component"].startswith("Cam Lock Fixture v3")
    assert any(".SLDPRT" in line for line in sw.b.log if line.startswith("import"))
    # a part saved to the library from Fusion inserts into SolidWorks
    fu.b.active = next(k for k in fu.b.docs if k.endswith("CamLever"))
    s = fu.save_active_to_std({"kind": "manufactured"})
    assert s["name"] == "Cam Lever" and "fusion_path" not in s
    assert sw.insert_std(s["id"], mate=False)["file"].endswith(".SLDPRT")
