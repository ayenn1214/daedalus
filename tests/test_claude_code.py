"""Icarus 'Claude (your plan)': the prompt goes to Claude Code, whose tool calls come back through the Claude link.
A fake `claude` stands in for Claude Code: it reads the --mcp-config it was given, starts that MCP server, makes
real tool calls through it and prints stream-json like `claude -p --output-format stream-json`."""
import json
import os
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from daedalus import claude_link as L
from daedalus.ai import claude_code as CC

ROOT = Path(__file__).resolve().parents[1]

FAKE = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
log = os.environ["FAKE_CLAUDE_LOG"]
prompt = sys.stdin.read()
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "key": os.environ.get("ANTHROPIC_API_KEY")}) + "\n")
if os.environ.get("FAKE_CLAUDE_MODE") == "logged_out":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Invalid API key · Please run /login", "session_id": "s-x"})); sys.exit(1)
cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["daedalus"]
env = {**os.environ, **cfg.get("env", {})}
p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
n = [0]
def rpc(method, params=None):
    n[0] += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())
def out(ev): print(json.dumps(ev), flush=True)
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
rpc("initialize", {"protocolVersion": "2025-06-18"})
out({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": [{"name": "daedalus", "status": "connected"}]})
calls = [("add_sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 50, "h": 30}]}),
         ("add_extrude", {"sketch": "Sketch1", "depth": 8})] if "--resume" not in args else [("get_model", {})]
for i, (name, a) in enumerate(calls):
    out({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "mcp__daedalus__" + name}}})
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "mcp__daedalus__" + name, "input": a}]}})
    r = rpc("tools/call", {"name": name, "arguments": a})["result"]
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": r["content"], "is_error": r.get("isError", False)}]}})
text = "Made a 50 x 30 x 8 plate." if "--resume" not in args else "It has Sketch1 and Extrude1."
out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "total_cost_usd": 0})
p.stdin.close(); p.wait(10)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dhome"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    monkeypatch.setattr(L, "root", lambda: ROOT)
    if sys.platform == "win32":
        script = tmp_path / "fake_claude.py"
        script.write_text(FAKE)
        cli = tmp_path / "claude.cmd"
        cli.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        cli = tmp_path / "claude"
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
    r = out.get("r")
    if isinstance(r, BaseException):
        raise r
    return r


def _safe(fn):
    try:
        return fn()
    except BaseException as e:  # noqa: BLE001
        return e


def test_find_cli_prefers_chosen_path(tmp_path, monkeypatch):
    p = tmp_path / "claude"
    p.write_text("x")
    assert CC.find_cli(str(p)) == str(p)
    monkeypatch.setattr(CC.shutil, "which", lambda _n: None)
    monkeypatch.setattr(CC, "candidates", lambda: [str(tmp_path / "nope")])
    assert CC.find_cli(str(tmp_path / "missing")) is None


def test_icarus_prompt_runs_through_claude_code_and_the_link(fake):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.copilot import SYSTEM
    from daedalus.ai.tools import TOOLS, ToolExecutor
    from daedalus.core.document import Document
    from daedalus.ui.claude_bridge import ClaudeBridge
    cli, log = fake
    assert CC.version(cli).startswith("9.9.9")
    doc = Document()
    b = ClaudeBridge(ToolExecutor(doc), TOOLS, SYSTEM, "3d", doc=doc)
    b.start()
    try:
        cp = CC.ClaudeCodeCopilot(cli, model="sonnet")
        texts, tools, status = [], [], []
        doc.checkpoint()
        b.via = "Icarus · Claude"
        ans = _pump(app, lambda: cp.ask("make a plate", {"features": []}, on_text=texts.append,
                                         on_tool=lambda *a: tools.append(a), on_status=status.append))
        assert ans == "Made a 50 x 30 x 8 plate." and texts == [ans]
        assert [t[0] for t in tools] == ["add_sketch", "add_extrude"] and not any(t[3] for t in tools)
        assert tools[1][1] == {"sketch": "Sketch1", "depth": 8}
        assert [f.name for f in doc.features][:2] == ["Sketch1", "Extrude1"] and b.calls == 2
        assert "Running add_extrude…" in status
        first = json.loads(log.read_text().splitlines()[0])
        a = first["args"]
        assert first["key"] is None                                     # your plan, not the API key
        assert a[a.index("--model") + 1] == "sonnet" and "--session-id" in a and "--strict-mcp-config" in a
        assert a[a.index("--allowedTools") + 1] == "mcp__daedalus" and a[a.index("--tools") + 1] == ""
        assert "<current_model>" in first["prompt"] and first["prompt"].endswith("make a plate")
        sid = cp.session_id
        assert sid == a[a.index("--session-id") + 1]
        # follow-up continues the same Claude Code session
        ans2 = _pump(app, lambda: cp.ask("what's in it?", None, on_text=texts.append))
        assert ans2 == "It has Sketch1 and Extrude1."
        a2 = json.loads(log.read_text().splitlines()[1])["args"]
        assert a2[a2.index("--resume") + 1] == sid
        doc.undo()                                                      # the whole Icarus turn = one undo step
        assert not doc.features
        cp.reset()
        assert cp.session_id is None
    finally:
        b.stop()


def test_attachments_are_files_claude_reads(fake):
    cli, log = fake
    cp = CC.ClaudeCodeCopilot(cli)
    L.write_link(1, "t", "3d", [], "", "0")                             # pretend the link is on
    cp.runner = _no_tools_runner(cli)
    with pytest.raises(CC.ClaudeCodeError):                             # the fake answers 'signed out'
        cp.ask("what is this?", None, on_text=lambda _t: None, images=[b"\x89PNG...."], documents=[b"%PDF-1.4"])
    first = json.loads(log.read_text().splitlines()[0])
    a = first["args"]
    assert a[a.index("--tools") + 1] == "Read" and a[a.index("--allowedTools") + 1] == "mcp__daedalus,Read"
    files = [ln[2:] for ln in first["prompt"].splitlines() if ln.startswith("- ")]
    assert len(files) == 2 and files[0].endswith(".png") and files[1].endswith(".pdf")
    assert Path(files[1]).read_bytes() == b"%PDF-1.4"


def _no_tools_runner(cli):
    import subprocess

    def run(cmd, **kw):
        kw["env"] = {**kw["env"], "FAKE_CLAUDE_MODE": "logged_out"}
        return subprocess.Popen(cmd, **kw)
    return run


def test_signed_out_says_how_to_sign_in(fake):
    cli, _ = fake
    L.write_link(1, "t", "3d", [], "", "0")
    cp = CC.ClaudeCodeCopilot(cli, runner=_no_tools_runner(cli))
    with pytest.raises(CC.ClaudeCodeError, match="sign in"):
        cp.ask("hi", None, on_text=lambda _t: None)


def test_link_off_is_explained(fake):
    cli, _ = fake
    with pytest.raises(CC.ClaudeCodeError, match="Claude link is off"):
        CC.ClaudeCodeCopilot(cli).ask("hi", None, on_text=lambda _t: None)


def test_panel_offers_your_plan(fake, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.core.document import Document
    from daedalus.ui.copilot_panel import CopilotPanel
    st = QSettings("Daedalus", "Daedalus")
    old = st.value("ai/provider")
    try:
        p = CopilotPanel(Document())
        i = p.provider.findData("claude_code")
        assert i >= 0
        p.provider.setCurrentIndex(i)
        assert p.who() == "Claude (your plan)" and p.model.findData("opus") >= 0
        monkeypatch.setattr(CC, "find_cli", lambda *_a: None)
        p.input.setPlainText("hi")
        p.send()
        assert "isn't installed" in p.log.toPlainText()
        monkeypatch.setattr(CC, "find_cli", lambda *_a: fake[0])
        p.input.setPlainText("hi")
        p.send()                                                        # no window link → explained, not run
        assert "Claude link is off" in p.log.toPlainText() and p.worker is None
    finally:
        st.setValue("ai/provider", old or "ollama")
