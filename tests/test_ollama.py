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


# ------------------------------------------------------------------ a fake Ollama server (streamed replies)
def fake_ollama(models=("qwen2.5:14b",), replies=(), vram_share=1.0, delay=0.0):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state = {"chats": [], "replies": list(replies)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            if self.path == "/api/version":
                return self._json({"version": "0.9"})
            if self.path == "/api/tags":
                return self._json({"models": [{"name": m} for m in models]})
            if self.path == "/api/ps":
                return self._json({"models": [{"name": models[0], "size": 1000, "size_vram": int(1000 * vram_share)}]})
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["chats"].append(body)
            time.sleep(delay)
            msg = state["replies"].pop(0) if state["replies"] else {"content": "ok"}
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                for part in (msg.get("content") or "").split(" "):           # word by word, like Ollama
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": part + " "},
                                                 "done": False}).encode() + b"\n")
                    self.wfile.flush()
                if msg.get("tool_calls"):
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": "",
                                                             "tool_calls": msg["tool_calls"]}, "done": False}).encode() + b"\n")
                self.wfile.write(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                             "eval_count": 5}).encode() + b"\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", state


def test_streamed_reply_with_tools_status_and_gpu_tip(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")                 # a system proxy must not catch Ollama
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    srv, host, st = fake_ollama(replies=[
        {"content": "", "tool_calls": [{"function": {"name": "add_sketch", "arguments": {
            "plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 20, "h": 10}]}}}]},
        {"content": "Made a sketch."}], vram_share=0.55)
    try:
        doc = Document()
        ex = ToolExecutor(doc)
        cp = ollama.OllamaCopilot(ex.run, host=host, model="qwen2.5:14b")
        said, status = [], []
        out = cp.ask("sketch a 20x10 rectangle", on_text=said.append, on_status=status.append)
        assert out.strip() == "Made a sketch." and said[-1].strip() == "Made a sketch."
        assert any(f["name"] == "Sketch1" for f in doc.to_dict()["features"])
        assert st["chats"][0]["stream"] is True and st["chats"][0]["keep_alive"] == ollama.KEEP_ALIVE
        assert st["chats"][0]["options"]["num_ctx"] == ollama.DEFAULT_CTX == 16384
        assert status[0] == "Connecting to Ollama…" and "Writing…" in status
        tips = [s for s in status if s.startswith("!")]
        assert len(tips) == 1 and "45% on the CPU" in tips[0]                 # told once, why it's slow
    finally:
        srv.shutdown()


def test_preflight_says_what_is_wrong():
    import pytest
    with pytest.raises(ollama.OllamaError, match="isn't answering"):
        ollama.preflight("http://127.0.0.1:9", "qwen2.5:14b", timeout=0.5)
    srv, host, _st = fake_ollama(models=("qwen2.5:7b",))
    try:
        with pytest.raises(ollama.OllamaError, match="isn't downloaded.*qwen2.5:7b"):
            ollama.preflight(host, "qwen2.5:14b")
        ollama.preflight(host, "qwen2.5:7b")
        cp = ollama.OllamaCopilot(lambda *a: ("", False), host=host, model="qwen2.5:14b")
        with pytest.raises(ollama.OllamaError, match="isn't downloaded"):
            cp.ask("hi", on_text=lambda t: None)                              # fails at once, not after minutes
    finally:
        srv.shutdown()


def test_kill_while_waiting_for_a_slow_model():
    import threading
    import time
    srv, host, _st = fake_ollama(replies=[{"content": "late answer"}], delay=3.0)
    try:
        cp = ollama.OllamaCopilot(lambda *a: ("", False), host=host, model="qwen2.5:14b")
        done = {}
        t = threading.Thread(target=lambda: done.setdefault("out", cp.ask("hi", on_text=lambda t: None)))
        t0 = time.time()
        t.start()
        time.sleep(0.5)
        cp.kill()
        t.join(5)
        assert not t.is_alive() and done.get("out", "") == "" and time.time() - t0 < 4.5
    finally:
        srv.shutdown()


def test_panel_shows_progress_elapsed_time_and_tips():
    import os
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.dims_tools import DIMS_TOOLS, DimsToolExecutor
    from daedalus.ui import copilot_panel as CP
    srv, host, _st = fake_ollama(replies=[{"content": "All good."}], vram_share=0.4, delay=0.3)
    try:
        panel = CP.CopilotPanel(None, executor=DimsToolExecutor(lambda: None), tools=DIMS_TOOLS, system="x")
        panel.copilot = ollama.OllamaCopilot(lambda *a: ("", False), host=host, model="qwen2.5:14b",
                                             tools=DIMS_TOOLS, system="x")
        seen = set()
        panel._start("hello", [])
        for _ in range(300):
            app.processEvents()
            seen.add(panel.status.text())
            if not panel.busy():
                break
            time.sleep(0.01)
        assert any(s.startswith("Waiting for qwen2.5:14b") for s in seen)     # not a silent spinner
        log = panel.log.toPlainText()
        assert "All good." in log and "60% on the CPU" in log and "smaller" in log
        assert panel.status.text() == "Done"
    finally:
        srv.shutdown()
