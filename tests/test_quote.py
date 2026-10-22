import pytest
from build123d import Box, Cylinder, Pos

from daedalus.core.document import Document
from daedalus.manufacturing import gcode_sim as G
from daedalus.manufacturing import quote as Q


def _mat():
    return Document().material_of(None)


def test_sheet_geometry_and_quote():
    plate = Box(200, 100, 3) - Pos(80, 30, 0) * Cylinder(5, 10) - Pos(-80, 30, 0) * Cylinder(5, 10)
    g = Q.part_geometry(plate)
    assert g["sheet"] and g["thickness"] == pytest.approx(3) and g["pierces"] == 3
    assert g["cut_length"] == pytest.approx(600 + 2 * 3.14159265 * 10, rel=1e-6)
    a, b = Q.calc_sheet(g, _mat(), qty=1), Q.calc_sheet(g, _mat(), qty=100)
    assert b["unit"] < a["unit"] and b["setup_min"] == a["setup_min"]   # setup spread over the batch


def test_cnc_quote_and_cam_time():
    blk = Box(60, 40, 20) - Cylinder(10, 30)
    g = Q.part_geometry(blk)
    assert not g["sheet"] and len(g["holes"]) == 1
    q = Q.calc_cnc(g, _mat())
    assert q["time_source"] == "rate model" and q["total"] > q["subtotal"] > 0
    q2 = Q.calc_cnc(g, _mat(), cycle_min=3.0)
    assert q2["time_source"] == "CAM cycle time" and q2["rough_min"] == 3.0
    units = [u for _n, u in Q.qty_breaks(g, _mat())]
    assert units == sorted(units, reverse=True)


def test_shop_schedule(tmp_path):
    s = Q.Shop(tmp_path / "shop.json")
    m = s.add_machine("Haas", "cnc", 85, 8)
    b1 = s.book(m["id"], "A", 10, "2026-01-05")
    b2 = s.book(m["id"], "B", 4, "2026-01-05")
    used = s.allocation(m)
    assert used["2026-01-05"] == 8 and used["2026-01-06"] == 6
    assert s.finish_date(b1) == "2026-01-06" and s.finish_date(b2) == "2026-01-06"
    assert s.next_free(m, 1, "2026-01-05") == "2026-01-06"
    s2 = Q.Shop(tmp_path / "shop.json")
    assert len(s2.bookings) == 2
    s2.remove_machine(m["id"])
    assert not s2.bookings


def test_gcode_sim_arcs_and_cycles():
    p = G.parse("G21 G90\nG0 Z5\nG1 Z-1 F100\nG2 X10 Y0 I5 J0\nG98 G81 X20 Y0 Z-5 R1 F50\nX30\nG80\n"
                "G20\nG0 X1\n")
    st = G.stats(p)
    assert st["feed_mm"] > 6 + 3.14 * 5 - 0.1
    xs = [b[0] for _k, _a, b, _f, _n in p["segments"]]
    assert 30 in xs and pytest.approx(25.4) in xs                      # inch move converted to mm


def test_siemens_post(tmp_path):
    from daedalus.manufacturing import cam
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 30}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    d.add("hole", {"face": "top", "positions": [[0, 0]], "diameter": 6, "depth": 5})
    d.cam_setup(post="siemens")
    for t in ("facing", "drill"):
        d.cam_add_op(t)
    d.export_gcode(tmp_path / "p.mpf")
    g = (tmp_path / "p.mpf").read_text()
    assert g.startswith(";") and "G71" in g and "G83" not in g and "M30" in g
    assert not any(line.startswith("(") for line in g.splitlines())
    assert "siemens" in cam.POSTS
    q = Q.quote_object(d, qty=2)
    assert q["process"] == "cnc" and q["qty"] == 2


def _drilled(post, **edit):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 60, "h": 40}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 30})
    d.add("hole", {"face": "top", "positions": [[-10, 0], [10, 0]], "diameter": 6, "depth": 25})
    d.cam_setup(post=post)
    d.cam_add_op("drill")
    if edit:
        d.cam_edit_op(0, **edit)
    from daedalus.manufacturing import cam
    d.cam_plan()
    return cam.gcode(d.last_cam, post), d


@pytest.mark.parametrize("cyc,expect", [("G81", "G81"), ("G82", "G82"), ("G83", "G83"), ("G73", "G73"),
                                         ("G85", "G85"), ("G86", "G86"), ("G89", "G89"), ("G76", "G76")])
def test_canned_cycles_fanuc(cyc, expect):
    g, d = _drilled("fanuc", cycle=cyc, dwell=0.5, retract="r")
    line = next(l for l in g.splitlines() if f" {expect} " in l)
    assert "G99" in line and " R" in line and " Z" in line
    if cyc in ("G82", "G89"):
        assert " P500" in line                                  # Fanuc dwell in ms
    if cyc in ("G83", "G73"):
        assert " Q" in line
    assert "G80" in g
    t = d.last_cam["time_s"]
    assert t > 0


def test_auto_cycle_and_other_posts():
    g, d = _drilled("fanuc")
    assert " G83 " in g                                         # 25 deep with Ø6 → peck
    g, _ = _drilled("haas", cycle="G82", dwell=0.5)
    assert " P0.5" in g
    g, _ = _drilled("linuxcnc", cycle="G76")
    code = "\n".join(l for l in g.splitlines() if not l.startswith("("))
    assert " G86 " in code and " G76 " not in code
    g, _ = _drilled("grbl", cycle="G83")
    assert "G83" not in g and g.count("G1 Z") > 4
    g, _ = _drilled("siemens", cycle="G83")
    assert "MCALL CYCLE83(" in g and g.count("MCALL") == 2
    sim = G.parse(g)
    assert sim["segments"] and not sim["warnings"]
    with pytest.raises(Exception):
        _drilled("fanuc", cycle="G99")


def test_simulator_reads_cycles():
    for post in ("fanuc", "haas", "linuxcnc"):
        for cyc in ("G81", "G83", "G73", "G85", "G89"):
            g, d = _drilled(post, cycle=cyc, dwell=0.2)
            p = G.parse(g)
            zs = [b[2] for _k, _a, b, _f, _n in p["segments"]]
            assert min(zs) < -20, (post, cyc)                  # reaches the hole bottom


def test_auto_cam_two_setups(tmp_path):
    from daedalus.manufacturing import autocam as A
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 20})
    d.add("sketch", {"plane": "top", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 30, "h": 20}]})
    d.add("extrude", {"sketch": "Sketch2", "depth": 6, "operation": "cut"})
    d.add("hole", {"face": "top", "positions": [[-30, 15], [30, 15]], "diameter": 6.6, "depth": 25})
    d.add("hole", {"face": "bottom", "positions": [[25, 0]], "diameter": 10, "depth": 8})
    n = d.active_object
    rec = A.recommend(d.placed_bodies()[n])
    assert rec["best"]["orient"] == "+Z" and rec["second"]["orient"] == "-Z"
    a = rec["best"]
    assert len(a["holes"]) == 2 and len(a["hidden_holes"]) == 1 and a["floors"]
    r = A.build(d, n, {"orient": "+Z", "drills": "6.6, 10", "endmills": "12, 6"})
    types = [o["type"] for o in r["ops"]]
    assert types[0] == "facing" and "pocket" in types and "profile" in types
    assert any("other side" in w for w in r["warnings"])
    drill = [o for o in r["summary"]["operations"] if "6.6" in o["tool"]]
    assert drill and drill[0]["holes"] == 2
    p = tmp_path / "s1.nc"
    d.export_gcode(p)
    sim = G.parse(p.read_text())
    assert sim["segments"]
    r2 = A.build(d, n, {"orient": "-Z", "second": True, "drills": "6.6, 10", "endmills": "12, 6",
                        "profile": False})
    holes2 = sum(o["holes"] for o in r2["summary"]["operations"] if "drill" in o["tool"] and "spot" not in o["tool"])
    assert holes2 == 1                                           # only the hole from the other side
