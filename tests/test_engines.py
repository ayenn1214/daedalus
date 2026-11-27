"""Icarus engines beyond Ollama / Claude API: OpenAI-compatible servers (GPT, LM Studio, Jan…), prompted tools for
models that can't call tools, Ollama thinking, and the Codex (ChatGPT plan) / Gemini CLI agents through the link."""
import json
import os
import stat
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from daedalus import claude_link as L
from daedalus.ai import toolsets as TS
from daedalus.ai.tools import ToolExecutor
from daedalus.core.document import Document

ROOT = Path(__file__).resolve().parents[1]
RECT = {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 30, "h": 20}]}


# ------------------------------------------------------------------ tool sets / prompted tools
def test_tool_sets_and_api_limit():
    core, model, allt = TS.select("core"), TS.select("model"), TS.select("all")
    assert len(core) == 32 and len(model) <= TS.API_TOOL_LIMIT < len(allt)
    assert not any(t["name"].startswith("dims_") for t in model)
    assert all(t["name"].startswith(TS.DIMS_PREFIX) or t["name"] in ("get_model", "get_bom") for t in TS.select("dims"))
    lim = TS.select("all", limit=TS.API_TOOL_LIMIT)
    assert len(lim) == 128 and "add_extrude" in {t["name"] for t in lim}


def test_parse_calls_reads_the_usual_shapes():
    known = {"add_sketch", "add_extrude"}
    txt = ('Sure.\n```json\n{"tool": "add_sketch", "arguments": {"plane": "XY"}}\n```\n'
           '[{"name": "add_extrude", "parameters": "{\\"depth\\": 5}"}, {"tool": "nope", "arguments": {}}]'
           '{"function": {"name": "add_extrude", "arguments": {"depth": 2}}}')
    assert TS.parse_calls(txt, known) == [("add_sketch", {"plane": "XY"}), ("add_extrude", {"depth": 5}),
                                          ("add_extrude", {"depth": 2})]
    assert "add_sketch" in TS.catalogue(TS.select("core")) and '"tool"' in TS.PROMPTED


# ------------------------------------------------------------------ OpenAI-compatible server
def fake_openai(script, no_tools_models=()):
    """script: list of replies; each {'text': str} or {'calls': [(name, args)]}. Streams SSE like OpenAI."""
    st = {"reqs": [], "script": list(script), "auth": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            b = json.dumps({"data": [{"id": "gpt-test"}, {"id": "local-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            st["reqs"].append(body)
            st["auth"].append(self.headers.get("Authorization"))
            if body.get("tools") and body["model"] in no_tools_models:
                b = json.dumps({"error": {"message": f"{body['model']} does not support tools"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b)
                return
            r = st["script"].pop(0) if st["script"] else {"text": "done"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def ev(d):
                self.wfile.write(f"data: {json.dumps({'choices': [{'index': 0, **d}]})}\n\n".encode())
                self.wfile.flush()
            if "calls" in r:
                for i, (name, args) in enumerate(r["calls"]):
                    a = json.dumps(args)
                    ev({"delta": {"tool_calls": [{"index": i, "id": f"c{i}", "type": "function",
                                                  "function": {"name": name, "arguments": a[:7]}}]}})
                    ev({"delta": {"tool_calls": [{"index": i, "function": {"arguments": a[7:]}}]}})
                ev({"delta": {}, "finish_reason": "tool_calls"})
            else:
                for w in r["text"].split(" "):
                    ev({"delta": {"content": w + " "}})
                ev({"delta": {}, "finish_reason": "stop"})
            self.wfile.write(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1", st


def test_openai_compatible_native_tool_calls():
    from daedalus.ai import openai_compat as OA
    srv, base, st = fake_openai([{"calls": [("add_sketch", RECT)]},
                                 {"calls": [("add_extrude", {"sketch": "Sketch1", "depth": 6})]},
                                 {"text": "Made a 30 x 20 x 6 block."}])
    try:
        assert OA.list_models(base, "k") == ["gpt-test", "local-model"]
        doc = Document()
        cp = OA.OpenAICopilot(ToolExecutor(doc).run, base, "sk-1", "gpt-test", tool_set="model")
        said, tools = [], []
        out = cp.ask("block", {"features": []}, on_text=said.append, on_tool=lambda *a: tools.append(a),
                     images=[b"\x89PNGxx"])
        assert out.strip() == "Made a 30 x 20 x 6 block." and [t[0] for t in tools] == ["add_sketch", "add_extrude"]
        assert [f.name for f in doc.features][:2] == ["Sketch1", "Extrude1"]
        r0 = st["reqs"][0]
        assert st["auth"][0] == "Bearer sk-1" and len(r0["tools"]) <= 128 and r0["stream"] is True
        assert r0["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "temperature" not in r0
        tool_msgs = [m for m in st["reqs"][1]["messages"] if m["role"] == "tool"]
        assert tool_msgs[0]["tool_call_id"] == "c0" and '"ok": true' in tool_msgs[0]["content"]
        cp.reset()
        assert cp.messages == []
    finally:
        srv.shutdown()


def test_openai_compatible_model_without_tools_gets_prompted_tools():
    from daedalus.ai import openai_compat as OA
    srv, base, st = fake_openai([{"text": json.dumps({"tool": "add_sketch", "arguments": RECT})},
                                 {"text": "Sketched it."}], no_tools_models=("local-model",))
    try:
        doc = Document()
        status = []
        cp = OA.OpenAICopilot(ToolExecutor(doc).run, base, None, "local-model", temperature=0.2)
        out = cp.ask("sketch", on_text=lambda _t: None, on_status=status.append)
        assert out.strip() == "Sketched it." and cp.prompted
        assert any(s.startswith("!") and "can't call tools" in s for s in status)
        assert "tools" not in st["reqs"][1] and "How to use tools" in st["reqs"][1]["messages"][0]["content"]
        assert st["reqs"][2]["messages"][-1]["content"].startswith("TOOL RESULT add_sketch")
        assert st["auth"][1] is None and st["reqs"][1]["temperature"] == 0.2
        assert [f.name for f in doc.features] == ["Sketch1"]
    finally:
        srv.shutdown()


def test_openai_repair_fills_skipped_results():
    from daedalus.ai import openai_compat as OA
    cp = OA.OpenAICopilot(base_url="x", model="m")
    cp.messages = [{"role": "user", "content": "x"},
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                       {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "a", "content": "ok"}]
    cp.repair()
    assert cp.messages[-1] == {"role": "tool", "tool_call_id": "b", "content": "Not run — stopped by the user."}


# ------------------------------------------------------------------ Ollama: any model
def test_ollama_prompted_tools_and_thinking():
    from daedalus.ai import ollama
    from tests.test_ollama import fake_ollama
    srv, host, st = fake_ollama(models=("gemma3:12b",), caps=("completion", "vision", "thinking"),
                                replies=[{"content": 'I will sketch. {"tool": "add_sketch", "arguments": '
                                                     + json.dumps(RECT) + "}"}, {"content": "Done."}])
    try:
        doc = Document()
        status = []
        cp = ollama.OllamaCopilot(ToolExecutor(doc).run, host=host, model="gemma3:12b", think="off")
        out = cp.ask("sketch", on_text=lambda _t: None, on_status=status.append)
        assert out.strip() == "Done." and cp.prompted and [f.name for f in doc.features] == ["Sketch1"]
        c0, c1 = st["chats"]
        assert "tools" not in c0 and "How to use tools" in c0["messages"][0]["content"] and c0["think"] is False
        assert c1["messages"][-1]["role"] == "user" and c1["messages"][-1]["content"].startswith("TOOL RESULT")
        assert any("can't call tools natively" in s for s in status)
    finally:
        srv.shutdown()


def test_ollama_model_list_details():
    from daedalus.ai import ollama
    from tests.test_ollama import fake_ollama
    srv, host, _st = fake_ollama(models=("qwen3:8b", "gemma3:12b"))
    try:
        assert [m["name"] for m in ollama.installed(host)] == ["gemma3:12b", "qwen3:8b"]
        mi = ollama.model_info(host, "qwen3:8b")
        assert "tools" in mi["caps"] and mi["context"] == 32768 and mi["params"] == "14B"
        assert any(r[0].endswith("-cloud") for r in ollama.RECOMMENDED)
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ Codex / Gemini CLI (fake commands, real link)
FAKE = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fake 1.0"); sys.exit(0)
flavor = os.environ["FAKE_FLAVOR"]
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(),
                         "keys": [k for k in ("OPENAI_API_KEY", "CODEX_API_KEY") if os.environ.get(k)],
                         "trust": os.environ.get("GEMINI_CLI_TRUST_WORKSPACE")}) + "\n")
if flavor == "codex":
    o = {}
    for i, a in enumerate(args):
        if a == "-c" and args[i + 1].startswith("mcp_servers.daedalus."):
            k, v = args[i + 1][len("mcp_servers.daedalus."):].split("=", 1)
            o[k] = v
    import tomllib
    cfg = tomllib.loads("\n".join(f"{k} = {v}" for k, v in o.items()))
else:
    cfg = json.load(open(".gemini/settings.json"))["mcpServers"]["daedalus"]
env = {**os.environ, **cfg.get("env", {})}
p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
                     cwd=cfg.get("cwd"))
n = [0]
def rpc(m, prm=None):
    n[0] += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": m, "params": prm or {}}) + "\n"); p.stdin.flush()
    return json.loads(p.stdout.readline())
def out(ev): print(json.dumps(ev), flush=True)
rpc("initialize", {"protocolVersion": "2025-06-18"})
calls = [("add_sketch", RECT), ("add_extrude", {"sketch": "Sketch1", "depth": 4})]
if flavor == "codex":
    out({"type": "thread.started", "thread_id": "th-1"}); out({"type": "turn.started"})
    for i, (name, a) in enumerate(calls):
        it = {"id": f"i{i}", "type": "mcp_tool_call", "server": "daedalus", "tool": name, "arguments": a, "status": "in_progress"}
        out({"type": "item.started", "item": it})
        r = rpc("tools/call", {"name": name, "arguments": a})["result"]
        out({"type": "item.completed", "item": {**it, "status": "completed", "result": {"content": r["content"]}}})
    out({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "Block made."}})
    out({"type": "turn.completed", "usage": {}})
else:
    out({"type": "init", "session_id": "gs-1", "model": "gemini"})
    for i, (name, a) in enumerate(calls):
        out({"type": "tool_use", "tool_name": "mcp_daedalus_" + name, "tool_id": f"t{i}", "parameters": a})
        r = rpc("tools/call", {"name": name, "arguments": a})["result"]
        out({"type": "tool_result", "tool_id": f"t{i}", "status": "success", "output": r["content"][0]["text"]})
    for w in ("Block ", "made."):
        out({"type": "message", "role": "assistant", "content": w, "delta": True})
    out({"type": "result", "status": "success", "stats": {}})
p.stdin.close(); p.wait(10)
'''.replace("RECT", repr(RECT))


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dhome"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-no")
    monkeypatch.setattr(L, "root", lambda: ROOT)
    if sys.platform == "win32":
        script = tmp_path / "fake_agent.py"
        script.write_text(FAKE)
        cli = tmp_path / "agent.cmd"
        cli.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        cli = tmp_path / "agent"
        cli.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE))
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return str(cli), tmp_path / "log.jsonl"


def _pump(app, fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _safe(fn)))
    t.start()
    end = time.time() + 90
    while t.is_alive() and time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    t.join(1)
    if isinstance(out.get("r"), BaseException):
        raise out["r"]
    return out.get("r")


def _safe(fn):
    try:
        return fn()
    except BaseException as e:  # noqa: BLE001
        return e


@pytest.mark.parametrize("flavor", ["codex", "gemini"])
@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_agent_cli_runs_tools_through_the_link(agent_env, monkeypatch, flavor):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.copilot import SYSTEM
    from daedalus.ai.tools import TOOLS
    from daedalus.ui.claude_bridge import ClaudeBridge
    cli, log = agent_env
    monkeypatch.setenv("FAKE_FLAVOR", flavor)
    if flavor == "codex":
        from daedalus.ai.codex_cli import CodexCopilot as C
    else:
        from daedalus.ai.gemini_cli import GeminiCopilot as C
    doc = Document()
    b = ClaudeBridge(ToolExecutor(doc), TOOLS, SYSTEM, "3d", doc=doc)
    b.start()
    try:
        cp = C(cli, model="m1")
        texts, tools = [], []
        ans = _pump(app, lambda: cp.ask("block", {"features": []}, on_text=texts.append,
                                         on_tool=lambda *a: tools.append(a), images=[b"\x89PNG.."]))
        assert ans == "Block made." and [t[0] for t in tools] == ["add_sketch", "add_extrude"]
        assert not any(t[3] for t in tools) and [f.name for f in doc.features][:2] == ["Sketch1", "Extrude1"]
        rec = json.loads(log.read_text().splitlines()[0])
        a = rec["args"]
        assert "<current_model>" in rec["prompt"] and a[a.index("-m" if flavor == "codex" else "--model") + 1] == "m1"
        if flavor == "codex":
            assert cp.session_id == "th-1" and rec["keys"] == [] and a[-1] == "-" and "-i" in a
            assert 'approval_policy="never"' in a and (Path(rec["cwd"]) / "AGENTS.md").exists()
        else:
            assert cp.session_id == "gs-1" and rec["trust"] == "true" and "--session-id" in a
            assert (Path(rec["cwd"]) / "GEMINI.md").read_text().startswith(SYSTEM[:40])
            assert "attachments" in rec["prompt"]                       # pictures are files Gemini reads
        _pump(app, lambda: cp.ask("again", None, on_text=texts.append))
        a2 = json.loads(log.read_text().splitlines()[1])["args"]
        assert (a2[:3] == ["exec", "resume", "th-1"]) if flavor == "codex" else \
            (a2[a2.index("--resume") + 1] == "gs-1")
    finally:
        b.stop()


# ------------------------------------------------------------------ the panel and the AI-apps settings
def test_panel_engines_and_openai_turn(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import copilot_panel as CP
    st = QSettings("Daedalus", "Daedalus")
    keep = {k: st.value(k) for k in ("ai/provider", "openai/preset", "openai/custom/url", "openai/custom/model")}
    srv, base, reqs = fake_openai([{"calls": [("add_sketch", RECT)]}, {"text": "Sketched."}])
    try:
        doc = Document()
        p = CP.CopilotPanel(doc)
        keys = [p.provider.itemData(i) for i in range(p.provider.count())]
        assert keys == ["ollama", "openai", "claude", "claude_code", "codex", "gemini_cli"]
        for k in ("codex", "gemini_cli"):
            p.provider.setCurrentIndex(keys.index(k))
            assert p.who() == dict(CP.PROVIDERS)[k] and p.model.count() >= 2
        st.setValue("openai/preset", "custom")
        st.setValue("openai/custom/url", base)
        st.setValue("openai/custom/model", "gpt-test")
        p.provider.setCurrentIndex(keys.index("openai"))
        end = time.time() + 5
        while time.time() < end and p.model.count() < 2:              # the server's model list arrives
            app.processEvents()
            time.sleep(0.01)
        assert p.model.currentText() == "gpt-test" and "Custom" in p.tag.text()
        p.input.setPlainText("sketch a rectangle")
        p.send()
        end = time.time() + 20
        while time.time() < end and (p.worker is not None and p.worker.isRunning() or not doc.features):
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert [f.name for f in doc.features] == ["Sketch1"] and "Sketched." in p.log.toPlainText()
        assert reqs["reqs"][0]["model"] == "gpt-test" and reqs["auth"][0] is None
    finally:
        srv.shutdown()
        for k, v in keep.items():
            st.setValue(k, v if v is not None else "")
        st.setValue("ai/provider", keep["ai/provider"] or "ollama")


def test_connect_ai_apps(tmp_path, monkeypatch):
    tomllib = pytest.importorskip("tomllib")
    from daedalus import ai_apps as A
    monkeypatch.setattr(A, "_home", lambda: tmp_path)
    monkeypatch.setattr(A, "_appdata", lambda: tmp_path / "appdata")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dh"))
    monkeypatch.setattr(A, "_claude_cli", lambda: None)
    cx = tmp_path / ".codex" / "config.toml"
    cx.parent.mkdir()
    cx.write_text('model = "gpt-5.5"\n[mcp_servers.other]\ncommand = "x"\n[mcp_servers.daedalus]\ncommand = "old"\n'
                  '[mcp_servers.daedalus.env]\nA = "1"\n[profiles.fast]\nmodel = "o4"\n')
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text('{"mcpServers": {"x": {"command": "y"}}, "other": 1}')
    for k, *_ in A.APPS:
        A.connect(k)
        assert A.status(k), k
    d = tomllib.loads(cx.read_text())
    assert sorted(d["mcp_servers"]) == ["daedalus", "other"] and d["profiles"]["fast"]["model"] == "o4"
    assert d["mcp_servers"]["daedalus"]["args"] == ["-m", "daedalus.mcp"]
    assert d["mcp_servers"]["daedalus"]["env"]["DAEDALUS_HOME"] == str(tmp_path / "dh")
    assert (cx.parent / "config.toml.before-daedalus").read_text().count("old") == 1
    g = json.loads((tmp_path / ".gemini" / "settings.json").read_text())["mcpServers"]["daedalus"]
    assert "PYTHONPATH" not in g["env"] and g["cwd"] and g["trust"] is True
    cur = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert cur["other"] == 1 and set(cur["mcpServers"]) == {"x", "daedalus"}
    vs = json.loads((tmp_path / "appdata" / "Code" / "User" / "mcp.json").read_text())
    assert vs["servers"]["daedalus"]["type"] == "stdio"
    for k, *_ in A.APPS:
        A.disconnect(k)
        assert not A.status(k), k
    assert "daedalus" not in cx.read_text() and "[mcp_servers.other]" in cx.read_text()
    rows = A.all_status()
    assert len(rows) == len(A.APPS) and all("path" in r for r in rows)
