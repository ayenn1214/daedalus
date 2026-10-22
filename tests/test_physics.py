import json
import math

import numpy as np
import pytest

from daedalus.ai.tools import ToolExecutor
from daedalus.core import assembly as asm
from daedalus.core.document import Document
from daedalus.core.features import FeatureError
from daedalus.core.physics import collision_pieces, mass_props, _hull_volume


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_CACHE", str(tmp_path / "cache"))


def box(d, obj, w, h, t, x=0, y=0):
    d.add("sketch", {"entities": [{"type": "rect", "x": x, "y": y, "w": w, "h": h}]}, obj=obj)
    d.add("extrude", {"sketch": d.last_of("sketch"), "depth": t}, obj=obj)


def test_box_drops_and_rests_on_plate():
    d = Document()
    box(d, "Object1", 200, 200, 10)
    d.add_object("Box", pose=[0, 0, 60, 0.3, 0.2, 0])
    box(d, "Box", 20, 20, 20)
    d.set_physics(duration=1.5)
    r = d.simulate()
    assert r.ok and "Box: free body" in r.joints
    z = r.frames[-1]["Box"][2]
    assert z == pytest.approx(10, abs=0.1)                     # resting on the 10 mm plate
    assert r.summary["settled"] and r.summary["contacts_between"] == ["Box ↔ Object1"]
    assert r.traces["kinetic energy (J)"][-1] < 1e-4


def _pendulum():
    d = Document()
    d.add("sketch", {"plane": "XZ", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 5}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 20, "end": "mid_plane"})
    d.add_object("Arm")
    d.add("sketch", {"plane": "XZ", "entities": [{"type": "slot", "x": 0, "y": 0, "x2": 0, "y2": -200, "w": 20},
                                                 {"type": "circle", "x": 0, "y": 0, "r": 5, "mode": "subtract"}]},
          obj="Arm")
    d.add("extrude", {"sketch": "Sketch2", "depth": 10, "end": "mid_plane"}, obj="Arm")
    d.set_object("Arm", pose=[0, 0, 0, 0, math.radians(5), 0])
    d.add_mate("concentric", {"obj": "Object1", "ref": {"axis": "any"}}, {"obj": "Arm", "ref": {"axis": "any", "radius": 5}})
    return d


def test_pendulum_period_matches_theory():
    d = _pendulum()
    d.set_physics(duration=3, collisions="hull")
    r = d.simulate()
    ys = [f["Arm"][4] for f in r.frames]
    cross = [r.times[i] for i in range(1, len(ys)) if ys[i - 1] > 0 >= ys[i]]
    m, com, inertia = mass_props(d.result.bodies["Arm"], 7.87)
    dist = abs(com[2]) / 1000
    T = 2 * math.pi * math.sqrt((inertia[1, 1] + m * dist * dist) / (m * 9.81 * dist))
    assert np.diff(cross).mean() == pytest.approx(T, rel=0.01)


def _slider():
    d = Document()
    box(d, "Object1", 300, 40, 10)
    d.add_object("Car", pose=[0, 0, 10, 0, 0, 0])
    box(d, "Car", 40, 40, 20)
    d.add_mate("coincident", {"obj": "Object1", "ref": {"face": "top"}}, {"obj": "Car", "ref": {"face": "bottom"}})
    d.add_mate("coincident", {"obj": "Object1", "ref": {"face": "front"}}, {"obj": "Car", "ref": {"face": "front"}})
    d.add_mate("distance", {"obj": "Object1", "ref": {"face": "left"}}, {"obj": "Car", "ref": {"face": "left"}}, value=130)
    return d


def test_slider_speed_position_and_keep():
    d = _slider()
    d.set_drive("Distance1", "speed", 50)
    d.set_physics(duration=1.0)
    r = d.simulate()
    assert r.joints == ["Car: slide (1 DOF) on Object1"]
    pos = r.traces["Distance1 position (mm)"]
    assert pos[-1] == pytest.approx(50 * (1.0 - 0.075), abs=0.5)    # 50 mm/s after a 0.15 s ease-in
    d.apply_frame(r.frames[-1])
    assert d.mate("Distance1").value == pytest.approx(130 + pos[-1], abs=0.05)   # solver keeps it there
    d.set_drive("Distance1", "position", -60)
    r = d.simulate()
    assert r.traces["Distance1 position (mm)"][-1] == pytest.approx(-60, abs=0.05)


def test_spring_static_deflection():
    d = _slider()
    d.set_drive("Distance1", "spring", 2.0, damping=0.05)            # 2 N/mm
    d.set_physics(gravity=[-9.81, 0, 0], duration=3)
    r = d.simulate()
    m = d.result.bodies["Car"].volume * 7.87e-6
    assert abs(r.traces["Distance1 position (mm)"][-1]) == pytest.approx(m * 9.81 / 2.0, rel=0.02)


def _fourbar():
    def inter(c1, r1, c2, r2):
        dd = np.linalg.norm(c2 - c1)
        a = (r1 * r1 - r2 * r2 + dd * dd) / (2 * dd)
        h = math.sqrt(r1 * r1 - a * a)
        p = c1 + a * (c2 - c1) / dd
        return p + h * np.array([-(c2 - c1)[1], (c2 - c1)[0]]) / dd

    O1, O2, A = np.array([0, 0.]), np.array([100, 0.]), np.array([0, 40.])
    B = inter(A, 100, O2, 80)
    d = Document()
    box(d, "Object1", 160, 20, 10, x=50, y=-30)
    for p in (O1, O2):
        d.add("sketch", {"plane": "XY", "offset": 10, "entities": [{"type": "circle", "x": p[0], "y": p[1], "r": 4}]})
        d.add("extrude", {"sketch": d.last_of("sketch"), "depth": 4.5})

    def link(name, p, q, z):
        d.add_object(name)
        d.add("sketch", {"plane": "XY", "offset": z, "entities": [
            {"type": "slot", "x": p[0], "y": p[1], "x2": q[0], "y2": q[1], "w": 16},
            {"type": "circle", "x": p[0], "y": p[1], "r": 4, "mode": "subtract"},
            {"type": "circle", "x": q[0], "y": q[1], "r": 4, "mode": "subtract"}]}, obj=name)
        d.add("extrude", {"sketch": d.last_of("sketch"), "depth": 5}, obj=name)

    link("Crank", O1, A, 10)
    link("Rocker", O2, B, 10)
    link("Coupler", A, B, 15)

    def hinge(a, pa, b, pb, z):
        d.add_mate("concentric", {"obj": a, "ref": {"axis_near": [pa[0], pa[1], z]}},
                   {"obj": b, "ref": {"axis_near": [pb[0], pb[1], z]}})
        d.add_mate("coincident", {"obj": a, "ref": {"face_near": [pa[0] + (20 if a == "Object1" else 8.5), pa[1], z],
                                                   "normal": [0, 0, 1]}},
                   {"obj": b, "ref": {"face_near": [pb[0] + 6, pb[1], z], "normal": [0, 0, -1]}})

    hinge("Object1", O1, "Crank", O1, 10)
    hinge("Object1", O2, "Rocker", O2, 10)
    hinge("Crank", A, "Coupler", A, 15)
    d.add_mate("concentric", {"obj": "Coupler", "ref": {"axis_near": [B[0], B[1], 15]}},
               {"obj": "Rocker", "ref": {"axis_near": [B[0], B[1], 15]}})
    return d, B


def test_four_bar_loop_closure_with_motor():
    d, B = _fourbar()
    assert d.result.assembly.dof == 1
    d.set_drive("Concentric1", "speed", 90)
    d.set_physics(duration=2)
    r = d.simulate()
    assert r.ok
    assert any("loop closure" in j for j in r.joints)
    travel = r.traces["Concentric1 position (deg)"][-1]
    assert travel == pytest.approx(90 * (2 - 0.075), abs=1.0)
    err = 0.0
    for f in r.frames:
        Mc, Mr = asm.pose_matrix(f["Coupler"]), asm.pose_matrix(f["Rocker"])
        pc = Mc[:3, :3] @ [B[0], B[1], 15] + Mc[:3, 3]
        pr = Mr[:3, :3] @ [B[0], B[1], 15] + Mr[:3, 3]
        err = max(err, float(np.linalg.norm(pc - pr)))
    assert err < 0.5                                         # the loop stays closed (mm)
    assert max(r.traces["Rocker speed (mm/s)"]) < 150        # no jolt at start-up


def test_pin_falls_through_hole_only_with_exact_shapes():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 40},
                                  {"type": "circle", "x": 0, "y": 0, "r": 6, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 20})
    d.add_object("Pin", pose=[0, 0, 30, 0, 0, 0])
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 4.5}]}, obj="Pin")
    d.add("extrude", {"sketch": "Sketch2", "depth": 40}, obj="Pin")
    d.set_physics(duration=0.5)
    r = d.simulate()
    assert r.frames[-1]["Pin"][2] < -1000                   # dropped straight through
    d.set_physics(collisions="hull")
    r = d.simulate()
    assert r.frames[-1]["Pin"][2] == pytest.approx(20, abs=0.2)   # hull plugs the hole


def test_decomposition_is_tight():
    from build123d import Box, Cylinder, Pos
    blk = Box(40, 40, 20) - Cylinder(6, 20)
    P = collision_pieces(blk, "decompose")
    assert sum(_hull_volume(p) for p in P) == pytest.approx(blk.volume, rel=0.01)
    L = Box(60, 40, 10) + Pos(-25, 0, 25) * Box(10, 40, 40)
    P = collision_pieces(L, "decompose")
    assert len(P) == 2


def test_settings_persist_and_drives_follow_mates(tmp_path):
    d = _slider()
    d.set_physics(gravity=[0, -9.81, 0], duration=0.7, friction=0.3, collisions="hull")
    d.set_drive("Distance1", "position", 12)
    d.save(tmp_path / "s.daedalus")
    e = Document.open(tmp_path / "s.daedalus")
    assert e.physics.gravity == [0, -9.81, 0] and e.physics.duration == 0.7
    assert e.physics.drives[0].mate == "Distance1" and e.physics.drives[0].kind == "position"
    e.delete_mate("Distance1")
    assert e.physics.drives == []
    with pytest.raises(FeatureError):
        e.set_drive("Nope", "speed", 1)
    with pytest.raises(FeatureError):
        e.set_physics(collisions="magic")
    e.undo()
    assert e.physics.drives and e.physics.drives[0].value == 12


def test_claude_physics_tools():
    d = _slider()
    ex = ToolExecutor(d)
    out, err = ex.run("set_physics", {"duration": 0.5})
    assert not err
    out, err = ex.run("set_drive", {"mate": "Distance1", "kind": "speed", "value": 40})
    assert not err
    out, err = ex.run("run_simulation", {"keep_end": True})
    assert not err, out
    res = json.loads(out)
    assert res["Distance1"]["kind"] == "speed" and res["moved_mm"]["Car"] > 10
    assert d.last_sim is not None
    assert d.mate("Distance1").value > 140                   # kept where the motor took it
    assert "physics" in ex.model_state()
    out, err = ex.run("set_drive", {"mate": "Distance1", "kind": "none"})
    assert not err and d.physics.drives == []
