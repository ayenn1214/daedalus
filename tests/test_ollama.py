"""Ollama copilot: tool loop, message format, text-JSON fallback, tool sets."""
import json

from daedalus.ai import ollama
from daedalus.ai.tools import ToolExecutor
from daedalus.core.document import Document


def scripted(responses, seen):
    it = iter(responses)

    def post(payload):
        seen.append(payload)
        return {"message": next(it)}
    return post


def test_tool_loop_builds_a_part():
    doc = Document()
    ex = ToolExecutor(doc)
    seen = []
    post = scripted([
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "add_sketch", "arguments": {
                "plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 50, "h": 30}]}}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "add_extrude", "arguments": json.dumps({"sketch": "Sketch1", "depth": 10})}}]},
        {"role": "assistant", "content": "<think>done?</think>Built a 50×30×10 block."},
    ], seen)
    cp = ollama.OllamaCopilot(ex.run, model="test", post=post)
    tools_seen, texts = [], []
    out = cp.ask("make a block", ex.model_state(), on_text=texts.append,
                 on_tool=lambda n, a, r, e: tools_seen.append((n, e)))
    assert out == "Built a 50×30×10 block." and texts == [out]
    assert tools_seen == [("add_sketch", False), ("add_extrude", False)]
    bb = doc.result.body.bounding_box()
    assert round(bb.size.Z, 3) == 10
    p = seen[-1]
    assert p["messages"][0]["role"] == "system" and p["options"]["num_ctx"] == ollama.DEFAULT_CTX
    roles = [m["role"] for m in p["messages"][1:]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    names = {t["function"]["name"] for t in p["tools"]}
    assert names == set(ollama.CORE_TOOLS)


def test_text_json_fallback_unknown_tool_and_images():
    doc = Document()
    ex = ToolExecutor(doc)
    seen = []
    post = scripted([
        {"role": "assistant", "content": 'Sure: {"name": "add_sketch", "arguments": {"plane": "XY", "entities": '
                                         '[{"type": "circle", "x": 0, "y": 0, "r": 10}]}}'},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "launch_rocket", "arguments": {}}}]},
        {"role": "assistant", "content": "ok"},
    ], seen)
    cp = ollama.OllamaCopilot(ex.run, model="test", tool_set="all", post=post)
    got = []
    cp.ask("circle", None, on_text=lambda t: None, on_tool=lambda n, a, r, e: got.append((n, e)),
           images=[b"\x89PNG fake"])
    assert got == [("add_sketch", False), ("launch_rocket", True)]
    assert seen[0]["messages"][1]["images"]                       # base64 image attached to the user turn
    assert len(seen[0]["tools"]) > len(ollama.CORE_TOOLS)
    assert [f.TYPE for f in doc.features] == ["sketch"]


def test_core_tools_exist_and_unreachable_server():
    from daedalus.ai.tools import TOOLS
    names = {t["name"] for t in TOOLS}
    assert set(ollama.CORE_TOOLS) <= names
    assert ollama.list_models("http://127.0.0.1:9", timeout=0.5) == []
