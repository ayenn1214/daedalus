import json

import pytest

from daedalus.ai.tools import TOOLS, ToolExecutor
from daedalus.core import Document
from daedalus.core.features import FeatureError


def plate_with_layers():
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 100}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 12})
    d.add_layer("Holes", "#4f9fd9", activate=True)
    d.add("hole", {"face": "top", "diameter": 9, "positions": [[-85, -35], [85, -35], [85, 35], [-85, 35]]})
    return d


def test_new_features_go_on_active_layer_and_faces_are_attributed():
    d = plate_with_layers()
    assert d.get("Hole1").layer == "Holes" and d.get("Extrude1").layer == "Default"
    fl = d.result.face_layers
    assert len(fl) == len(d.result.body.faces())
    assert fl.count("Holes") == 4            # one cylindrical wall per hole


def test_lock_blocks_edits_everywhere():
    d = plate_with_layers()
    d.set_layer("Holes", locked=True)
    for fn in (lambda: d.edit("Hole1", {"diameter": 10}), lambda: d.delete("Hole1"),
               lambda: d.rename("Hole1", "H"), lambda: d.set_suppressed("Hole1", True),
               lambda: d.move_to_layer(["Hole1"], "Default"), lambda: d.add("fillet", {}, layer="Holes")):
        with pytest.raises(FeatureError):
            fn()
    ex = ToolExecutor(d)
    text, err = ex.run("edit_feature", {"name": "Hole1", "params": {"diameter": 11}})
    assert err and "locked" in text


def test_in_model_off_rebuilds_without_layer_and_undo_restores():
    d = plate_with_layers()
    v = d.result.body.volume
    d.set_layer("Holes", in_model=False)
    assert d.result.body.volume == pytest.approx(200 * 100 * 12)
    d.undo()
    assert d.result.body.volume == pytest.approx(v)


def test_visibility_is_display_only():
    d = plate_with_layers()
    v = d.result.body.volume
    d.set_layer("Holes", visible=False)
    assert d.result.body.volume == pytest.approx(v)


def test_delete_rename_and_roundtrip(tmp_path):
    d = plate_with_layers()
    d.set_layer("Holes", new_name="Bolt holes")
    assert d.get("Hole1").layer == "Bolt holes" and d.active_layer == "Bolt holes"
    d.save(tmp_path / "p.daedalus")
    d2 = Document.open(tmp_path / "p.daedalus")
    assert [l.name for l in d2.layers] == ["Default", "Bolt holes"] and d2.get("Hole1").layer == "Bolt holes"
    d2.delete_layer("Bolt holes")
    assert d2.get("Hole1").layer == "Default" and d2.active_layer == "Default"
    with pytest.raises(FeatureError):
        d2.delete_layer("Default")


def test_old_files_without_layers_load():
    d = Document()
    d._load_dict({"features": [{"type": "sketch", "name": "Sketch1", "params": {
        "entities": [{"type": "circle", "x": 0, "y": 0, "r": 5}]}}]})
    d.rebuild()
    assert d.get("Sketch1").layer == "Default" and [l.name for l in d.layers] == ["Default"]


def test_claude_layer_tools():
    names = {t["name"] for t in TOOLS}
    assert {"add_layer", "set_layer", "move_to_layer"} <= names
    assert "layer" in next(t for t in TOOLS if t["name"] == "add_extrude")["input_schema"]["properties"]
    d = Document()
    ex = ToolExecutor(d)
    assert not ex.run("add_layer", {"name": "Frame", "color": "#d9822b", "active": True})[1]
    ex.run("add_sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 50, "h": 50}]})
    ex.run("add_extrude", {"sketch": "Sketch1", "depth": 5, "layer": "Default"})
    assert d.get("Sketch1").layer == "Frame" and d.get("Extrude1").layer == "Default"
    assert not ex.run("move_to_layer", {"features": ["Extrude1"], "layer": "Frame"})[1]
    st = ex.model_state()
    assert st["active_layer"] == "Frame" and {l["name"] for l in st["layers"]} == {"Default", "Frame"}
    assert all(f["layer"] == "Frame" for f in st["features"])
