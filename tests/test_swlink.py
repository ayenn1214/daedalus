"""SolidWorks Link against the simulated SolidWorks session."""
from pathlib import Path

import pytest

from daedalus.core.document import Document
from daedalus.inventory.model import Inventory, new_part
from daedalus.stdparts import StandardLibrary
from daedalus.sw import FakeBackend, SWError, SWLink


class Store:
    def __init__(self):
        self.inv = Inventory()
        self.saved = 0

    def save(self):
        self.saved += 1


@pytest.fixture
def link(tmp_path):
    b = FakeBackend(tmp_path / "sw")
    return SWLink(b, cache_dir=tmp_path / "cache", library=StandardLibrary(tmp_path / "lib"), store=Store())


def test_status_and_bom(link):
    st = link.status()
    assert st["connected"] and st["document"]["kind"] == "assembly"
    rows = link.bom()
    by = {r["description"]: r for r in rows}
    assert by["Lift arm, 1/2 plate"]["qty"] == 2 and by["Lift arm, 1/2 plate"]["part"] == "RG-3002"
    assert by["Caster mount plate"]["qty"] == 4 and by["Caster mount plate"]["part"] == "Caster Plate"
    assert by["Socket head cap screw"]["qty"] == 16
    assert [r["item"] for r in rows] == list(range(1, len(rows) + 1))


def test_send_bom_writes_back_and_matches_again(link):
    inv = link.store.inv
    inv.parts.append(new_part(name="M8 x 25 SHCS", type="fastener", category="Screw", headStyle="Socket Head",
                              threadStandard="Metric Coarse", size="M8", length="25", currentStock=10))
    rep = link.send_bom("P25605 Lift cart", units=2)
    assert rep["written_back"] == 4 and not rep["write_failed"]
    assert "M8 x 25 SHCS" in rep["linked"]                         # stocked fastener recognised from the PartNo
    plate = next(c for c in link.b.components() if c.name == "Caster Plate-1")
    hid = plate.properties["HolocronId"]
    assert inv.part(hid)["name"]
    # second send: same parts (matched by HolocronId), nothing new
    rep2 = link.send_bom("P25605 Lift cart", units=2)
    assert rep2["added"] == []
    stock = {r["part"]: r for r in link.stock_report(units=2)}
    assert stock["SHCS M8 x 25 (ISO)"]["needed"] == 32 and stock["SHCS M8 x 25 (ISO)"]["short"] == 22


def test_insert_fastener_and_std_part_mated(link, tmp_path):
    link.b.select_hole()
    r = link.insert_fastener("socket head cap screw", "M8", 25)
    assert r["component"].startswith("SHCS M8 x 25") and r["mates"]
    assert Path(r["file"]).suffix == ".SLDPRT" and Path(r["file"]).exists()
    again = link.insert_fastener("socket head cap screw", "M8", 25, mate=False)
    assert again["file"] == r["file"]                              # cached native part reused
    assert "select a hole" not in again["note"]
    d = Document()
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 6}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 30})
    info = link.library.add_object(d, "Object1", {"name": "Pivot pin", "part_number": "RG-0400", "kind": "purchased"})
    r = link.insert_std(info["id"], mate=True)
    assert r["note"].startswith("select a hole")
    comp = next(c for c in link.b.components() if c.name == r["component"])
    assert comp.properties["DaedalusStdId"] == info["id"] and comp.properties["PartNo"] == "RG-0400"


def test_save_part_to_library_and_round_trips(link, tmp_path):
    with pytest.raises(SWError):
        link.save_active_to_std({"name": "x"})                     # assembly is active
    part = next(p for p in link.b.docs if p.endswith("Lift Arm.SLDPRT"))
    link.b.open_doc(part)
    saved = link.save_active_to_std({"kind": "manufactured"})
    assert saved["name"] == "Lift Arm" and saved["part_number"] == "RG-3002" and saved["sw_path"] == part
    assert link.b.docs[part].properties["DaedalusStdId"] == saved["id"]
    # inserting it back into SolidWorks uses the original native file
    link.b.open_doc(next(p for p in link.b.docs if p.endswith(".SLDASM")))
    r = link.insert_std(saved["id"], mate=False)
    assert r["file"] == part
    # SolidWorks → Daedalus
    marker = link.to_daedalus(tmp_path / "rt")
    d = Document()
    d.import_file(marker)
    assert len(d.objects) >= 5
    # Daedalus → SolidWorks
    out = link.from_daedalus(d)
    assert Path(out).exists() and link.b.active.endswith(".step")


def test_commands(link, tmp_path):
    link.command("view", "Isometric")
    link.command("rebuild")
    link.command("save")
    p = link.command("export", "step")
    assert Path(p).exists()
    with pytest.raises(SWError):
        link.command("view", "Sideways")
    mac = tmp_path / "x.swp"
    mac.write_text("")
    link.command("macro", str(mac))
    assert link.b.log[-1] == "macro x.swp"


def test_copilot_tools(link):
    import json

    from daedalus.sw.tools import SW_TOOLS, SWToolExecutor
    opened = []
    ex = SWToolExecutor(link, opened.append)
    names = {t["name"] for t in SW_TOOLS}
    assert {"sw_bom", "sw_insert_fastener", "send_bom_to_inventory", "open_in_daedalus"} <= names
    out, err = ex.run("sw_bom", {})
    assert not err and len(json.loads(out)) == 4
    out, err = ex.run("sw_insert_fastener", {"kind": "hex bolt", "size": "M10"})
    assert err and "length" in out
    out, err = ex.run("sw_set_properties", {"properties": {"Revision": "B"}})
    assert not err and link.b.active_doc().properties["Revision"] == "B"
    out, err = ex.run("open_in_daedalus", {})
    assert not err and opened and opened[0].lower().endswith(".sldasm")
    out, err = ex.run("nope", {})
    assert err


def test_com_backend_late_bound():
    """pywin32 late binding runs zero-argument methods on lookup (doc.GetType is already an int)."""
    from types import SimpleNamespace as NS
    from daedalus.sw.backend import ComBackend, PART
    mgr = NS(GetNames=("PartNo",), Get6=lambda n, cached: ("", "RG-100", "RG-100", False))
    ext = NS(CreateMassProperty=NS(Mass=1.5, Volume=1e-4, CenterOfMass=(0.0, 0.0, 0.01)),
             CustomPropertyManager=lambda scope: mgr)
    doc = NS(GetType=PART, GetPathName="C:/p/Bracket.SLDPRT", GetTitle="Bracket",
             ConfigurationManager=NS(ActiveConfiguration=NS(Name="Default")), Extension=ext,
             GetMaterialPropertyName2=lambda cfg, db: ("6061 Alloy", "SOLIDWORKS Materials"))
    b = ComBackend.__new__(ComBackend)
    b._sw, b.attach_only, b.last_error = NS(RevisionNumber="32.1.0", ActiveDoc=doc), True, ""
    assert b.connected()
    assert b.app_info()["year"] == 2024
    d = b.active_doc()
    assert d.kind == PART and d.title == "Bracket" and d.material == "6061 Alloy"
    assert abs(d.mass_kg - 1.5) < 1e-9


def test_com_export_falls_back(tmp_path, monkeypatch):
    """'Member not found' on one SaveAs variant → the next one is tried; the error lists every attempt."""
    from types import SimpleNamespace as NS
    from daedalus.sw import backend as B

    class MemberNotFound(Exception):
        pass
    monkeypatch.setattr(B.ComBackend, "_byref_int", staticmethod(lambda: NS(value=0)))
    monkeypatch.setattr(B.ComBackend, "_null", staticmethod(lambda: None))

    def no(*a):
        raise MemberNotFound(-2147352573, "Member not found.", None, None)

    def save_as(p, *a):
        open(p, "w").write("ISO-10303-21;")
        return True
    doc = NS(GetType=B.PART, GetPathName=str(tmp_path / "P.SLDPRT"), GetTitle="P",
             Extension=NS(SaveAs3=no, SaveAs=save_as), SaveAs3=no, SaveAs=no)
    b = B.ComBackend.__new__(B.ComBackend)
    b._sw, b.attach_only, b.last_error = NS(RevisionNumber="32.0", ActiveDoc=doc), True, ""
    out = b.export(str(tmp_path / "x.step"))
    assert open(out).read().startswith("ISO")
    doc.Extension.SaveAs = no
    try:
        b.export(str(tmp_path / "y.step"))
        raise AssertionError("expected SWError")
    except B.SWError as e:
        assert "Extension.SaveAs3" in str(e) and "Member not found" in str(e)
    msg = B.explain(MemberNotFound(-2147352573, "Member not found.", None, None))
    assert "Member not found" in msg
