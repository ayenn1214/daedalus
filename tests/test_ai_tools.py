import json

from daedalus.ai.tools import TOOLS, ToolExecutor
from daedalus.core import Document


def test_tool_schemas_are_wellformed():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        assert t["input_schema"]["type"] == "object"
        json.dumps(t)


def test_executor_builds_part_like_claude_would():
    doc = Document()
    ex = ToolExecutor(doc)
    calls = [
        ("add_sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 120, "h": 80}]}),
        ("add_extrude", {"sketch": "Sketch1", "depth": 10}),
        ("add_hole", {"face": "top", "style": "counterbore", "diameter": 6.6,
                      "head_diameter": 11, "head_depth": 6.5, "positions": [[-45, -25], [45, 25]]}),
        ("add_fillet", {"radius": 4, "edges": "vertical&linear"}),
        ("set_material", {"material": "Aluminum 6061"}),
    ]
    for name, args in calls:
        text, err = ex.run(name, args)
        assert not err, (name, text)
    text, err = ex.run("edit_feature", {"name": "Extrude1", "params": {"depth": 15}})
    assert not err and json.loads(text)["bbox_mm"][2] == 15
    text, err = ex.run("add_fillet", {"radius": 999, "edges": "top"})
    assert err   # Claude gets the error back to self-correct
    state = ex.model_state()
    assert state["features"][-1]["error"]
    assert state["mass_properties"]["material"] == "Aluminum 6061-T6"
