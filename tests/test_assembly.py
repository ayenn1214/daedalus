import json
import math

import numpy as np
import pytest

from daedalus.ai.tools import TOOLS, ToolExecutor
from daedalus.core import Document
from daedalus.core.assembly import rotmat
from daedalus.core.features import FeatureError

PLATE_TOP = {"face_near": [50, 0, 10], "normal": [0, 0, 1]}


def pin_and_arm():
    d = Document()
    d.set_object("Object1", new_name="Base")
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 100}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    d.add("sketch", {"plane": "top", "entities": [{"type": "circle", "x": -80, "y": 0, "r": 5}]})
    d.add("extrude", {"sketch": "Sketch2", "depth": 30})
    d.add_object("Arm", "#d9822b", pose=[0, 150, 40, 0, 0, 0.4])
    d.add("sketch", {"entities": [{"type": "slot", "x": 0, "y": 0, "x2": 150, "y2": 0, "w": 30},
                                  {"type": "circle", "x": 0, "y": 0, "r": 5.1, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch3", "depth": 8})
    return d


def world(d, obj, p):
    o = d.obj(obj)
    return rotmat(o.pose[3:]) @ np.asarray(p, float) + np.asarray(o.pose[:3])


def hinge(d):
    d.add_mate("concentric", {"obj": "Base", "ref": {"axis": "Extrude2"}},
               {"obj": "Arm", "ref": {"axis": "Extrude3", "near": [0, 0, 4]}})
    d.add_mate("coincident", {"obj": "Base", "ref": PLATE_TOP}, {"obj": "Arm", "ref": {"face": "bottom"}})


def test_objects_have_separate_bodies_and_histories():
    d = pin_and_arm()
    assert set(d.result.bodies) == {"Base", "Arm"} and not d.result.errors
    assert d.get("Sketch3").obj == "Arm" and d.get("Extrude2").obj == "Base"
    mp = d.mass_properties()
    assert set(mp["objects"]) == {"Base", "Arm"}
    assert mp["volume_mm3"] == pytest.approx(sum(v["volume_mm3"] for v in mp["objects"].values()), rel=1e-6)


def test_hinge_places_arm_and_leaves_one_dof():
    d = pin_and_arm()
    hinge(d)
    res = d.result.assembly
    assert res.ok and res.dof == 1
    assert world(d, "Arm", [0, 0, 0]) == pytest.approx([-80, 0, 10], abs=1e-4)


def test_drag_swings_about_the_pin():
    d = pin_and_arm()
    hinge(d)
    d.solve_assembly(drag=("Arm", [150, 0, 4], [100, 0, 14]))
    tip = world(d, "Arm", [150, 0, 4])
    assert math.hypot(tip[0] + 80, tip[1]) == pytest.approx(150, abs=1e-3)
    assert tip[2] == pytest.approx(14, abs=1e-4)
    assert tip[0] > 60                      # actually swung toward the target


def test_mates_follow_design_changes():
    d = pin_and_arm()
    hinge(d)
    d.edit("Sketch2", {"entities": [{"type": "circle", "x": -60, "y": 10, "r": 5}]})
    assert world(d, "Arm", [0, 0, 0]) == pytest.approx([-60, 10, 10], abs=1e-4)


def test_angle_drive_and_conflict_rejection():
    d = pin_and_arm()
    hinge(d)
    d.add_mate("angle", {"obj": "Base", "ref": {"face": "front"}}, {"obj": "Arm", "ref": {"face": "front"}}, value=30)
    frames = d.drive("Angle1", 10, 170, 8)
    assert len(frames) == 9 and all(f["_ok"] for f in frames)
    ang = []
    for f in frames:
        R = rotmat(f["Arm"][3:])
        x = R @ [1, 0, 0]
        ang.append(math.degrees(math.atan2(x[1], x[0])))
    assert max(ang) - min(ang) > 100           # it really rotated through the sweep
    with pytest.raises(FeatureError):
        d.add_mate("distance", {"obj": "Base", "ref": PLATE_TOP}, {"obj": "Arm", "ref": {"face": "bottom"}}, value=5)


def test_interference_detects_overlap():
    d = pin_and_arm()
    d.set_object("Arm", pose=[-80, 0, 15, 0, 0, 0])     # drop the arm onto the pin with no hole alignment
    d.place_object("Arm", translate=(-40, 0, 0))       # pin now passes through solid arm material
    hits = d.interference()
    assert hits and hits[0][:2] == ("Base", "Arm")


def test_roundtrip_and_undo(tmp_path):
    d = pin_and_arm()
    hinge(d)
    d.save(tmp_path / "a.daedalus")
    d2 = Document.open(tmp_path / "a.daedalus")
    assert [m.name for m in d2.mates] == [m.name for m in d.mates] and d2.result.assembly.ok
    d.delete_mate("Coincident1")
    assert len(d.mates) == 1
    d.undo()
    assert len(d.mates) == 2


def test_cross_object_reference_is_an_error():
    d = pin_and_arm()
    d.add("extrude", {"sketch": "Sketch1", "depth": 5}, obj="Arm")
    assert "another object" in d.result.errors["Extrude4"]


def test_claude_assembly_tools():
    names = {t["name"] for t in TOOLS}
    assert {"add_object", "add_mate", "drive_mate", "place_object", "check_interference"} <= names
    d = Document()
    ex = ToolExecutor(d)
    ex.run("add_sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100}]})
    ex.run("add_extrude", {"sketch": "Sketch1", "depth": 10})
    assert not ex.run("add_object", {"name": "Lid", "translate": [0, 0, 50]})[1]
    ex.run("add_sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100}]})
    ex.run("add_extrude", {"sketch": "Sketch2", "depth": 4})
    assert d.get("Extrude2").obj == "Lid"
    text, err = ex.run("add_mate", {"type": "coincident", "a": {"object": "Object1", "ref": {"face": "top"}},
                                    "b": {"object": "Lid", "ref": {"face": "bottom"}}})
    assert not err, text
    assert world(d, "Lid", [0, 0, 0])[2] == pytest.approx(10, abs=1e-4)
    st = ex.model_state()
    assert st["active_object"] == "Lid" and st["mates"][0]["ok"] and st["assembly_dof"] == 3
