"""v1.0: drawings, 2D exchange, export options, CAM, manufacturability, assembly/drop/topology studies,
design studies and the new copilot tools."""
import json
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from daedalus.ai.tools import TOOLS, ToolExecutor  # noqa: E402
from daedalus.core.document import Document  # noqa: E402
from daedalus.core.features import FeatureError  # noqa: E402


def plate():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 20})
    d.add("sketch", {"plane": "top", "entities": [{"type": "rect", "x": -10, "y": 0, "w": 40, "h": 30}]})
    d.add("extrude", {"sketch": "Sketch2", "depth": 8, "operation": "cut"})
    d.add("hole", {"face": "top", "positions": [[30, 15], [30, -15]], "fastener": "M6", "fit": "tap", "depth": 15})
    return d


def beam(depth=10, length=100):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": length / 2, "y": 0, "w": length, "h": 10}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": depth})
    return d


def end_faces(d, obj="Object1"):
    fs = list(d.result.bodies[obj].faces())
    return (min(range(len(fs)), key=lambda i: fs[i].center().X), max(range(len(fs)), key=lambda i: fs[i].center().X))


# ------------------------------------------------------------------ drawings

def test_drawing_views_and_callouts(tmp_path):
    d = plate()
    dr = d.new_drawing(title={"title": "Plate"})
    sh = dr.sheet()
    dirs = [v["dir"] for v in sh.views]
    assert dirs == ["front", "top", "right", "iso"]
    top = sh.views[1]
    assert top["_bbox"] == pytest.approx((-40, -25, 40, 25), abs=1e-6)
    texts = [a.get("text", "") for a in top["_auto"] if a["t"] == "dim"]
    assert any("M6x1" in t for t in texts)                       # tapped hole callout
    hs = [a for a in top["_auto"] if a.get("kind") == "h"]
    assert hs and abs(hs[0]["p2"][0] - hs[0]["p1"][0]) == pytest.approx(80)
    sec = d.add_drawing_view(kind="section", parent=top["id"], line=[[-45, 0], [45, 0]])
    assert any(p["t"] == "hatch" for p in sec["_prims"])
    det = d.add_drawing_view(kind="detail", parent=top["id"], center=[30, 15], radius=8)
    assert det["scale"] == pytest.approx(2.0)
    for ext in ("pdf", "svg", "png", "dxf"):
        p = d.export_drawing(tmp_path / f"p.{ext}")
        assert os.path.getsize(p) > 1000
    with pytest.raises(FeatureError, match="DWF"):
        d.export_drawing(tmp_path / "p.dwf")
    # saved without caches, reloaded and regenerated
    d.save(tmp_path / "p.daedalus")
    raw = json.loads((tmp_path / "p.daedalus").read_text())
    assert "_prims" not in json.dumps(raw["drawings"])
    e = Document.open(tmp_path / "p.daedalus")
    e.regenerate_drawings()
    assert len(e.drawings[0].sheet().views) == 6
    assert e.drawings[0].sheet().views[1]["_bbox"] == pytest.approx((-40, -25, 40, 25), abs=1e-6)


def test_dxf_roundtrip_keeps_layers_and_dims(tmp_path):
    import ezdxf
    d = plate()
    d.new_drawing()
    d.add_drawing_item({"t": "dim", "kind": "aligned", "p1": [30, 30], "p2": [80, 60], "at": [40, 50]})
    d.export_drawing(tmp_path / "a.dxf")
    dx = ezdxf.readfile(tmp_path / "a.dxf")
    assert len(dx.modelspace().query("DIMENSION")) >= 8
    assert dx.layers.get("HIDDEN").dxf.linetype == "HIDDEN"
    dr = d.open_2d(tmp_path / "a.dxf")
    sh = dr.sheet()
    assert not dr.linked and sh.size == "Model space"
    assert {"VISIBLE", "HIDDEN", "DIM", "CENTER"} <= set(sh.layers)
    dims = [i for i in sh.items if i["t"] == "dim"]
    assert len(dims) >= 8
    assert any(i["t"] == "line" and i.get("style") == "hidden" for i in sh.items)
    d.export_drawing(tmp_path / "b.svg", dr.name)
    e = d.open_2d(tmp_path / "b.svg")
    assert len(e.sheet().items) > 20


def test_dwg_needs_converter(tmp_path):
    from daedalus.drawing import dxfio
    d = plate()
    d.new_drawing()
    if dxfio.dwg_available():
        pytest.skip("ODA converter installed")
    with pytest.raises(FeatureError, match="ODA"):
        d.export_drawing(tmp_path / "a.dwg")


# ------------------------------------------------------------ export options

@pytest.mark.parametrize("schema,tag", [("AP203", "CONFIG_CONTROL_DESIGN"), ("AP214", "AUTOMOTIVE_DESIGN"),
                                        ("AP242", "AP242_MANAGED_MODEL")])
def test_step_schemas(tmp_path, schema, tag):
    d = beam()
    d.export(tmp_path / "a.step", {"step_schema": schema})
    assert tag in (tmp_path / "a.step").read_text()[:2000]
    e = Document()
    e.import_file(tmp_path / "a.step")
    assert e.result.bodies["Object1"].volume == pytest.approx(10000, rel=1e-6)


def test_inch_units_and_mesh_formats(tmp_path):
    d = beam()
    d.export(tmp_path / "in.step", {"units": "in"})
    e = Document()
    e.import_file(tmp_path / "in.step")
    assert e.result.bodies["Object1"].volume == pytest.approx(10000, rel=1e-6)
    for ext in ("ply", "wrl"):
        d.export(tmp_path / f"a.{ext}")
        assert os.path.getsize(tmp_path / f"a.{ext}") > 200
    d.export(tmp_path / "a.stl", {"stl_ascii": True})
    assert (tmp_path / "a.stl").read_text()[:5] == "solid"


def test_auto_exchange_on_save(tmp_path):
    d = beam()
    d.set_export_options(auto_exchange=["step", "iges"])
    d.save(tmp_path / "m.daedalus")
    assert (tmp_path / "m.step").exists() and (tmp_path / "m.iges").exists()
    with pytest.raises(FeatureError):
        d.set_export_options(step_schema="AP999")


# --------------------------------------------------------------- manufacturing

def test_cam_gcode(tmp_path):
    d = plate()
    d.cam_setup(post="fanuc")
    for t in ("facing", "pocket", "profile", "drill"):
        d.cam_add_op(t)
    kinds = [t["kind"] for t in d.cam["tools"]]
    assert "drill" in kinds and "tap" in kinds                       # tapped holes get a tap
    s = d.export_gcode(tmp_path / "p.nc")
    g = (tmp_path / "p.nc").read_text()
    assert g.startswith("%\nO1000") and g.rstrip().endswith("%")
    assert ("G83" in g or "G81" in g) and "G84" in g and "M30" in g   # auto: G81 shallow, G83 deep
    assert all(ord(c) < 128 for c in g)
    # drilled to the hole bottom: blind 15 mm from the 20 mm top, stock top +1 → Z-16
    assert "Z-16" in [w for line in g.splitlines() if " G83 " in line or " G81 " in line for w in line.split()]
    assert s["cycle_time_min"] > 0
    pocket = next(o for o in d.last_cam["ops"] if o.type == "pocket")
    xs = [m[1] for m in pocket.moves if m[0] == "feed"]
    assert min(xs) == pytest.approx(-25, abs=0.05) and max(xs) == pytest.approx(5, abs=0.05)   # Ø10 tool in 40 wide
    grbl = __import__("daedalus.manufacturing.cam", fromlist=["x"]).gcode(d.last_cam, "grbl")
    assert "G83" not in grbl and "M0" in grbl


def test_manufacturability():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 30}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 20})
    d.add("hole", {"face": "front", "positions": [[0, 10]], "diameter": 6, "depth": 10})
    s = d.check_manufacturability("draft")
    assert s["undercut_area_pct"] > 0                              # side hole can't be pulled up
    s = d.check_manufacturability("thickness", min_wall=1)
    assert s["thinnest_mm"] == pytest.approx(7.0, abs=0.1)
    s = d.check_manufacturability("overhang")
    assert s["support_area_mm2"] > 0 and s["best_build_dir"]["support_area_mm2"] == 0


# ----------------------------------------------------------------- studies

def test_assembly_bonded_cantilever():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 25, "y": 0, "w": 50, "h": 10}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    d.add_object("B", pose=[50, 0, 0, 0, 0, 0])
    d.add("sketch", {"entities": [{"type": "rect", "x": 25, "y": 0, "w": 50, "h": 10}]}, obj="B")
    d.add("extrude", {"sketch": "Sketch2", "depth": 10}, obj="B")
    st = d.add_study("assembly")
    d.add_study_item(st["name"], "fixtures", "fixed", faces=[end_faces(d)[0]], object="Object1")
    d.add_study_item(st["name"], "loads", "force", faces=[end_faces(d, "B")[1]], object="B", value=100,
                     direction=[0, 0, -1])
    r = d.run_study(st["name"])
    E, I = 205000.0, 10 ** 4 / 12
    assert r.summary["max_displacement_mm"] == pytest.approx(100 * 100 ** 3 / (3 * E * I), rel=0.03)
    assert set(r.summary["per_part"]) == {"Object1", "B"}


def test_design_study_restores_model():
    d = beam()
    a, b = end_faces(d)
    st = d.add_study("static")
    d.add_study_item(st["name"], "fixtures", "fixed", faces=[a])
    d.add_study_item(st["name"], "loads", "force", faces=[b], value=100, direction=[0, 0, -1])
    r = d.design_study("Extrude1", "depth", [8, 14], st["name"])
    s8, s14 = (row["max_von_mises_MPa"] for row in r["rows"])
    assert s8 / s14 == pytest.approx((14 / 8) ** 2, rel=0.12)      # bending stress ∝ 1/h²
    assert d.get("Extrude1").params["depth"] == 10
    assert d.result.bodies["Object1"].volume == pytest.approx(10000)


def test_topology_and_body():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 40, "y": 0, "w": 80, "h": 8}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 30})
    a, b = end_faces(d)
    st = d.add_study("topology", iterations=12, volume_fraction=0.4, mesh_size=4)
    d.add_study_item(st["name"], "fixtures", "fixed", faces=[a])
    d.add_study_item(st["name"], "loads", "force", faces=[b], value=100, direction=[0, 0, -1])
    r = d.run_study(st["name"])
    assert 0.15 < r.summary["volume_fraction_kept"] < 0.6
    assert r.summary["compliance_end"] < r.summary["compliance_start"]
    n = d.topology_to_body(st["name"])
    assert 0.1 * 19200 < d.result.bodies[n].volume < 0.65 * 19200


def test_drop_test():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 30, "h": 30}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 30})
    st = d.add_study("drop", height=500, steps=120)
    r = d.run_study(st["name"])
    s = r.summary
    assert s["impact_velocity_m_s"] == pytest.approx(math.sqrt(2 * 9.81 * 0.5), rel=1e-3)
    # elastic bar impact: contact ≈ 2L/c, stress order ρcv
    c = math.sqrt(205e9 / 7870)
    assert 0.5 * 2 * 0.03 / c * 1000 < s["contact_time_ms"] < 3 * 2 * 0.03 / c * 1000
    assert 0.5 * 7870 * c * 3.13 / 1e6 < s["max_von_mises_MPa"] < 5 * 7870 * c * 3.13 / 1e6
    assert s["peak_deceleration_g"] > 1000


# ------------------------------------------------------------------- tools

def test_new_tools_registered_and_grouped():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for n in ("insert_part", "get_bom", "new_drawing", "add_drawing_view", "export_drawing", "open_2d",
              "set_export_options", "cam_add_operation", "export_gcode", "check_manufacturability", "add_bolt",
              "design_study", "topology_to_body"):
        assert n in names
    d = plate()
    ex = ToolExecutor(d)
    n0 = len(d._undo)
    out, err = ex.run("new_drawing", {"size": "A4"})
    assert not err, out
    out, err = ex.run("cam_add_operation", {"type": "profile"})
    assert not err, out
    assert len(d._undo) == n0                       # the copilot turn makes the single undo step
    st = ex.model_state()
    assert st["drawings"][0]["sheets"][0]["size"] == "A4" and st["cam"]["operations"]
