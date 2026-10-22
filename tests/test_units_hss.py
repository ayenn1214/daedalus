"""v2.9: unit systems (SI, US customary, construction), unit-aware parsing, measure, HSS builder."""
import json
import math

import pytest

from daedalus.core import hss as H
from daedalus.core import measure as M
from daedalus.core import units as U
from daedalus.core.document import Document
from daedalus.core.features import FeatureError


@pytest.fixture(autouse=True)
def _si():
    U.set_active("SI_mm")
    yield
    U.set_active("SI_mm")


@pytest.mark.parametrize("text,q,expect", [
    ("5'-6 1/4\"", "length", 1682.75), ("3 ft 4-1/2 in", "length", 1028.7), ("1/2\"", "length", 12.7),
    ("25.4mm + 2", "length", 27.4), ("2 m", "length", 2000.0), ("1e-3 m", "length", 1.0), ("12", "length", 12.0),
    ("1,250 mm", "length", 1250.0), ("min(3, 4) in", "length", 76.2), ("45°", "angle", 45.0),
    ("1 rad", "angle", 180 / math.pi), ("4/12", "slope", math.degrees(math.atan(4 / 12))),
    ("100%", "slope", 45.0), ("1:1", "slope", 45.0), ("212°F", "temperature", 100.0), ("273.15 K", "temperature", 0.0),
    ("1 ksi", "pressure", 6.894757293), ("1 kip", "force", 4448.2216), ("1 yd3", "volume", 764554857.98),
    ("1 bf", "volume", 2359737.216), ("1 lb", "mass", 0.45359237), ("1 klf", "linear_load", 14.5939),
    ("1 psf", "area_load", 0.04788), ("1 hp", "power", 745.69987), ("1 lbf·ft", "torque", 1.35582),
])
def test_parse(text, q, expect):
    assert U.parse(text, q) == pytest.approx(expect, rel=1e-4)


def test_parse_errors_and_cross_quantity():
    with pytest.raises(U.UnitError):
        U.parse("3 kg", "length")
    with pytest.raises(U.UnitError):
        U.parse("abc", "length")
    assert U.try_parse("??", "length") is None


def test_bare_numbers_use_active_units():
    U.set_active("IPS")
    assert U.parse("2", "length") == pytest.approx(50.8)
    U.set_active("CONST_US")
    assert U.parse("10", "length") == pytest.approx(3048.0)        # feet
    assert U.parse("6", "slope") == pytest.approx(math.degrees(math.atan(0.5)))


def test_format_systems():
    assert U.fmt(12.5) == "12.5 mm"
    U.set_active("CONST_US")
    assert U.fmt(1682.75) == "5'-6 1/4\""
    assert U.fmt(9.525) == "0'-3/8\""
    assert U.fmt(math.degrees(math.atan(4 / 12)), "slope") == "4/12"
    U.set_active("IPS_frac")
    assert U.fmt(25.4 * 1.515625) == "1 33/64\""
    U.set_active("MKS")
    assert U.fmt(1234.5) == "1.2345 m"
    assert U.fmt(-1e-15) == "0 m"
    s = U.system_from({"name": "SI_mm", "units": {"force": "kN"}, "decimals": {"default": 1}})
    assert U.fmt(2500, "force", s) == "2.5 kN"
    assert "custom" in s.label


def test_every_unit_round_trips():
    for q in U.Q:
        for u in U.units_of(q):
            if q == "slope" and u == "1:n":
                continue
            v = 37.5 if q != "temperature" else 21.0
            assert U.from_base(U.to_base(v, u, q), u, q) == pytest.approx(v), (q, u)


def test_document_units_persist(tmp_path):
    d = Document()
    d.set_units("CONST_US", units={"force": "lbf"}, denominator=32)
    assert U.active().name == "CONST_US" and U.active().unit("force") == "lbf"
    p = tmp_path / "u.daedalus"
    d.save(p)
    U.set_active("SI_mm")
    d2 = Document.open(p)
    assert d2.units["name"] == "CONST_US" and d2.unit_system().denominator == 32
    with pytest.raises(FeatureError):
        d.set_units("nope")
    with pytest.raises(FeatureError):
        d.set_units(units={"length": "kg"})


def test_param_quantity_inference():
    from daedalus.core.features import FEATURE_TYPES
    ps = {p.name: p for p in FEATURE_TYPES["hss"].PARAMS}
    assert U.param_quantity(ps["length"]) == "length"
    assert U.param_quantity(ps["start_miter"]) == "angle"
    assert U.param_quantity(ps["rotation"]) == "angle"


# ----------------------------------------------------------------- HSS ---
IN = 25.4


def test_aisc_properties_match_manual():
    p = H.properties(H.lookup("AISC", "HSS4x4x1/4"))          # AISC: A 3.37, I 7.80, Z 4.69, J 12.8, 12.21 lb/ft
    assert p["A"] / IN ** 2 == pytest.approx(3.37, rel=0.01)
    assert p["Ix"] / IN ** 4 == pytest.approx(7.80, rel=0.01)
    assert p["Zx"] / IN ** 3 == pytest.approx(4.69, rel=0.01)
    assert p["J"] / IN ** 4 == pytest.approx(12.8, rel=0.02)
    assert p["mass_per_m"] / 1.48816 == pytest.approx(12.21, rel=0.01)
    r = H.properties(H.lookup("AISC", "hss 6.625 x 0.280"))   # A 5.22 in², 18.97 lb/ft
    assert r["A"] / IN ** 2 == pytest.approx(5.22, rel=0.01)
    assert r["mass_per_m"] / 1.48816 == pytest.approx(18.97, rel=0.01)


def test_metric_standards():
    p = H.properties(H.lookup("EN10219", "100x100x5"))         # 18.36 cm², 14.4 kg/m, 271 cm⁴
    assert p["A"] / 100 == pytest.approx(18.36, rel=0.01)
    assert p["Ix"] / 1e4 == pytest.approx(271, rel=0.01)
    h = H.lookup("EN10210", "RHS 200x100x8")
    assert (h.ro, h.ri) == (12.0, 8.0)
    assert H.lookup("EN10219", "SHS 200x200x12.5").ro == pytest.approx(37.5)
    assert H.lookup("CSA", "HSS 102x102x6.4").t == 6.4
    assert len(H.TABLES["AISC"]) > 300 and len(H.TABLES["CSA"]) > 200
    with pytest.raises(KeyError):
        H.lookup("CSA", "HSS 999x1")


def test_custom_sections():
    h = H.custom("rect", 50, 75, 3, "custom", 4)
    assert (h.B, h.H, h.ro, h.ri) == (50, 75, 4, 1)
    assert H.custom("square", 40, None, 2, "sharp").ro == 0
    with pytest.raises(ValueError):
        H.custom("rect", 20, 40, 12)
    with pytest.raises(ValueError):
        H.custom("square", 40, None, 2, "custom", 30)
    sq = H.properties(H.custom("square", 100, None, 5, "sharp"))
    assert sq["A"] == pytest.approx(100 ** 2 - 90 ** 2)
    assert sq["Ix"] == pytest.approx((100 ** 4 - 90 ** 4) / 12)
    assert sq["Zx"] == pytest.approx((100 ** 3 - 90 ** 3) / 4)


def test_hss_member_feature():
    d = Document()
    f = d.add("hss", {"standard": "AISC", "designation": "HSS4x4x1/4", "length": 1000})
    assert not d.result.errors
    assert d.result.body.volume == pytest.approx(H.properties(H.lookup("AISC", "HSS4x4x1/4"))["A"] * 1000, rel=1e-3)
    d.edit(f.name, {"start_miter": 45, "end_miter": -30, "end_caps": True, "axis": "X", "x": 100})
    assert not d.result.errors
    bb = d.result.body.bounding_box()
    assert bb.max.X == pytest.approx(100 + 1000 + 50.8 * math.tan(math.radians(30)) + 6 / math.cos(math.radians(30)),
                                     abs=0.5)
    cl = {r["section"]: r for r in d.cut_list()}
    assert cl["HSS4x4x1/4"]["long_point"] == pytest.approx(1000 + 50.8 * (1 + math.tan(math.radians(30))), abs=0.05)
    assert cl["End cap t6"]["qty"] == 2
    assert cl["HSS4x4x1/4"]["total_kg"] > 18
    d.edit(f.name, {"standard": "custom", "shape": "round", "width": 60.3, "thickness": 4, "start_miter": 0,
                    "end_miter": 0, "end_caps": False, "axis": "-Y"})
    assert not d.result.errors and d.result.body.bounding_box().max.Y == pytest.approx(0, abs=1e-6)
    d.edit(f.name, {"standard": "CSA", "designation": "HSS 999"})
    assert "No CSA section" in d.result.errors[f.name]


def test_frame_with_standard_designation():
    d = Document()
    d.add("sketch3d", {"geometry": {"points": {"p1": [0, 0, 0], "p2": [1000, 0, 0], "p3": [1000, 0, 800]},
                                    "curves": [{"id": "l1", "type": "line", "a": "p1", "b": "p2"},
                                               {"id": "l2", "type": "line", "a": "p2", "b": "p3"}],
                                    "constraints": []}})
    f = d.add("frame", {"path": "3DSketch1" if d.features[0].name == "3DSketch1" else d.features[0].name,
                        "standard": "CSA", "designation": "HSS 102x102x6.4"})
    assert not d.result.errors, d.result.errors
    assert f.summary() == "HSS 102x102x6.4"
    rows = d.cut_list()
    assert all(r["section"] == "HSS 102x102x6.4" and r["kg_per_m"] > 18 for r in rows)
    v_std = d.result.body.volume
    d.edit(f.name, {"standard": "custom", "profile": "square_tube", "width": 101.6, "thickness": 6.4,
                    "corner_radius": 0.0})
    assert d.result.body.volume > v_std                       # sharp corners hold more steel
    d.edit(f.name, {"corner_radius": 12.8})
    assert d.result.body.volume == pytest.approx(v_std, rel=0.02)


def test_measure_entities():
    d = Document()
    d.add("hss", {"standard": "custom", "shape": "square", "width": 100, "thickness": 5, "corner_rule": "sharp",
                  "length": 500})
    body = d.result.body
    r = dict((k, v) for k, v, _q in M.measure(d, [{"kind": "body"}])["rows"])
    assert r["Volume"] == pytest.approx((100 ** 2 - 90 ** 2) * 500)
    assert r["Mass"] == pytest.approx(r["Volume"] * d.material_of(None).density / 1e6)
    planes = [i for i, f in enumerate(body.faces()) if f.geom_type.name == "PLANE"]
    outer = [i for i in planes if abs(body.faces()[i].center().X - 50) < 1e-6][0]
    opp = [i for i in planes if abs(body.faces()[i].center().X + 50) < 1e-6][0]
    two = dict((k, v) for k, v, _q in M.measure(d, [{"kind": "face", "index": outer},
                                                     {"kind": "face", "index": opp}])["rows"])
    assert two["Normal distance (parallel planes)"] == pytest.approx(100)
    assert two["Angle between normals"] == pytest.approx(180)
    pts = dict((k, v) for k, v, _q in M.measure(d, [{"kind": "point", "p": [0, 0, 0]},
                                                     {"kind": "point", "p": [3000, 0, 1000]}])["rows"])
    assert pts["Distance"] == pytest.approx(math.hypot(3000, 1000))
    assert pts["Slope / pitch"] == pytest.approx(math.degrees(math.atan(1 / 3)))
    U.set_active("CONST_US")
    txt = dict(M.format_rows(M.measure(d, [{"kind": "point", "p": [0, 0, 0]},
                                           {"kind": "point", "p": [3000, 0, 1000]}])))
    assert txt["Slope / pitch"] == "4/12"
    with pytest.raises(FeatureError):
        M.measure(d, [{"kind": "face", "index": 999}])


def test_ai_tools():
    from daedalus.ai.tools28 import run28
    d = Document()
    out = json.loads(run28(d, "set_units", {"name": "CONST_SI"}))
    assert out["system"] == "CONST_SI" and out["units"]["linear_load"] == "kN/m"
    c = json.loads(run28(d, "convert_units", {"value": "50 psf", "quantity": "area_load", "to": "kPa"}))
    assert c["result"].startswith("2.394")
    s = json.loads(run28(d, "hss_sizes", {"standard": "AISC", "query": "6x6", "limit": 3}))
    assert s["count"] >= 5 and s["sizes"][0]["designation"].startswith("HSS6x6")
    p = json.loads(run28(d, "hss_sizes", {"standard": "AISC", "designation": "HSS6x6x3/8"}))
    assert p["properties_base"]["A"] > 4000
    d.add("hss", {"standard": "AISC", "designation": "HSS6x6x3/8", "length": 1000})
    m = json.loads(run28(d, "measure", {"refs": [{"kind": "body"}]}))
    assert m["base"]["Volume"] > 4e6
