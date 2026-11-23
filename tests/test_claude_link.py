"""Claude Desktop link: Claude Desktop's settings, the MCP server (`python -m daedalus.mcp`) over stdin / stdout,
and tool calls reaching the open Daedalus — a part built in the window, DIMS stock recorded."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from daedalus import claude_link as L

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dhome"))
    return tmp_path


def test_claude_desktop_settings_connect_and_disconnect(home, monkeypatch):
    cfg = home / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    monkeypatch.setattr(L, "config_paths", lambda: [cfg])
    assert L.status() == {**L.status(), "installed": True, "connected": False}
    assert L.connect() == [str(cfg)]
    d = json.loads(cfg.read_text())
    assert d["theme"] == "dark" and d["mcpServers"]["other"] == {"command": "x"}           # kept
    e = d["mcpServers"]["daedalus"]
    assert e["args"] == ["-m", "daedalus.mcp"] and e["env"]["PYTHONPATH"] == str(ROOT)
    assert Path(e["command"]).exists() and "pythonw" not in Path(e["command"]).name.lower()
    assert cfg.with_name(cfg.name + ".before-daedalus").exists() and L.status()["connected"]
    L.disconnect()
    assert "daedalus" not in json.loads(cfg.read_text())["mcpServers"] and not L.status()["connected"]
    cfg.write_text("{broken")
    with pytest.raises(ValueError, match="isn't valid JSON"):
        L.connect()                                                     # never overwrite a file we can't read


def test_console_python_on_windows(tmp_path):
    w = tmp_path / "pythonw.exe"
    (tmp_path / "python.exe").write_text("")
    w.write_text("")
    assert L.console_python(str(w)) == str(tmp_path / "python.exe")


class MCP:
    """Claude Desktop's side: the MCP server as a child process, one JSON-RPC message per line."""

    def __init__(self, env_home):
        env = {**os.environ, "DAEDALUS_HOME": str(env_home), "PYTHONPATH": str(ROOT)}
        self.p = subprocess.Popen([sys.executable, "-m", "daedalus.mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=env, cwd=str(ROOT))
        self.n = 0

    def ask(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def note(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


def test_mcp_server_without_daedalus_open(home):
    m = MCP(home / "dhome")
    try:
        r = m.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        assert r["result"]["serverInfo"]["name"] == "daedalus" and "tools" in r["result"]["capabilities"]
        assert "daedalus_status" in r["result"]["instructions"]
        m.note("notifications/initialized")                            # no answer to a notification
        names = [t["name"] for t in m.ask("tools/list")["result"]["tools"]]
        assert names[:3] == ["daedalus_status", "daedalus_open", "daedalus_guide"]
        r = m.ask("tools/call", {"name": "daedalus_status", "arguments": {}})
        assert "isn't open" in r["result"]["content"][0]["text"]
        r = m.ask("tools/call", {"name": "add_sketch", "arguments": {}})
        assert r["result"]["isError"] and "daedalus_open" in r["result"]["content"][0]["text"]
        assert m.ask("ping")["result"] == {} and m.ask("nope/x")["error"]["code"] == -32601
    finally:
        m.close()


def _pump(app, fn):
    """Run fn (Claude Desktop talking) on a thread while this thread runs the GUI — tool calls run there."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    end = time.time() + 60
    while t.is_alive() and time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    t.join(1)
    return out.get("r")


def test_claude_desktop_builds_a_part_in_the_open_window(home):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.copilot import SYSTEM
    from daedalus.ai.tools import TOOLS, ToolExecutor
    from daedalus.core.document import Document
    from daedalus.ui.claude_bridge import ClaudeBridge
    doc = Document()
    b = ClaudeBridge(ToolExecutor(doc), TOOLS, SYSTEM, "3d", doc=doc)
    b.start()
    m = MCP(home / "dhome")
    try:
        def talk():
            m.ask("initialize", {"protocolVersion": "2025-06-18"})
            tools = m.ask("tools/list")["result"]["tools"]
            st = json.loads(m.ask("tools/call", {"name": "daedalus_status", "arguments": {}})
                            ["result"]["content"][0]["text"])
            guide = m.ask("tools/call", {"name": "daedalus_guide", "arguments": {}})["result"]["content"][0]["text"]
            s1 = m.ask("tools/call", {"name": "add_sketch", "arguments": {
                "plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 60, "h": 40}]}})["result"]
            s2 = m.ask("tools/call", {"name": "add_extrude", "arguments": {"sketch": "Sketch1", "depth": 12}})["result"]
            gm = m.ask("tools/call", {"name": "get_model", "arguments": {}})["result"]
            return tools, st, guide, s1, s2, gm
        tools, st, guide, s1, s2, gm = _pump(app, talk)
        names = {t["name"] for t in tools}
        assert {"add_sketch", "add_extrude", "dims_search_parts", "daedalus_open"} <= names
        assert len(tools) == len(TOOLS) + 3 and all("inputSchema" in t for t in tools)
        assert st["open"] and st["workspace"].startswith("3D") and guide == SYSTEM
        assert not s1["isError"] and not s2["isError"], (s1, s2)
        assert [f.name for f in doc.features][:2] == ["Sketch1", "Extrude1"] and doc.result.body is not None
        assert "Extrude1" in gm["content"][0]["text"] and b.calls == 3
        doc.undo()                                                     # Claude's request = one undo step
        assert not doc.features
        tf = json.loads(L.tools_file().read_text())
        assert tf["kind"] == "3d" and len(tf["tools"]) == len(TOOLS) and tf["launch"][1:] == ["-m", "daedalus"]
    finally:
        m.close()
        b.stop()
    assert L.read_link() is None                                       # closed: the link is gone
    m = MCP(home / "dhome")                                            # tools still listed from the last run
    try:
        assert len(m.ask("tools/list")["result"]["tools"]) == len(TOOLS) + 3
    finally:
        m.close()


def test_claude_desktop_uses_dims(home, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.dims_tools import DIMS_SYSTEM, DIMS_TOOLS, DimsToolExecutor
    from daedalus.inventory import model as M
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.claude_bridge import ClaudeBridge
    st = InventoryStore(tmp_path / "inv.json")
    st.inv.parts = [M.new_part(id="bolt", name="1/2-13 HHCS", partNumber="F-0001", type="fastener", currentStock=40)]
    st.save()
    b = ClaudeBridge(DimsToolExecutor(lambda: st, lambda msg, undo=None: st.save()), DIMS_TOOLS, DIMS_SYSTEM, "dims")
    b.start()
    m = MCP(home / "dhome")
    try:
        def talk():
            m.ask("initialize", {})
            found = m.ask("tools/call", {"name": "dims_search_parts", "arguments": {"query": "HHCS"}})["result"]
            used = m.ask("tools/call", {"name": "dims_record", "arguments": {"part": "F-0001", "mode": "usage",
                                                                               "qty": 5}})["result"]
            bad = m.ask("tools/call", {"name": "add_sketch", "arguments": {}})["result"]
            return found, used, bad
        found, used, bad = _pump(app, talk)
        assert "1/2-13 HHCS" in found["content"][0]["text"]
        assert not used["isError"], used
        assert st.inv.part("bolt")["currentStock"] == 35
        assert bad["isError"] and "Unknown tool" in bad["content"][0]["text"]  # DIMS window: no modelling tools
    finally:
        m.close()
        b.stop()


def test_bad_token_is_refused(home):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import urllib.error
    import urllib.request
    from daedalus.ui.claude_bridge import ClaudeBridge
    b = ClaudeBridge(None, [], "", "dims")
    port = b.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/tools", headers={"X-Daedalus-Token": "wrong"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(req, timeout=5)
        assert e.value.code == 403
    finally:
        b.stop()


def test_finds_claude_desktop_data_under_claude_data(tmp_path, monkeypatch):
    """Newer Claude Desktop installs keep their data in %LOCALAPPDATA%\\Claude-Data\\claude_<id>\\…"""
    monkeypatch.setattr(L.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(L, "_chosen", lambda: None, raising=False)
    data = tmp_path / "Local" / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "Roaming" / "Claude"
    data.mkdir(parents=True)
    (data / "Local State").write_text("{}")                             # Electron's own file: the app's data folder
    (data / "Cache").mkdir()
    paths = [p for p in L.config_paths() if "Claude-Data" in str(p)]
    assert paths == [data / L.CONFIG]
    (data / L.CONFIG).write_text(json.dumps({"mcpServers": {}}))
    deeper = data / "x"
    deeper.mkdir()
    (deeper / "config.json").write_text("{}")
    assert [p for p in L.config_paths() if "Claude-Data" in str(p)] == [data / L.CONFIG]   # the real file wins
    done = L.connect()
    assert str(data / L.CONFIG) in done
    assert "daedalus" in json.loads((data / L.CONFIG).read_text())["mcpServers"]
    snip = json.loads(L.snippet())
    assert snip["mcpServers"]["daedalus"]["args"] == ["-m", "daedalus.mcp"]


def test_dialog_edits_the_settings_file_by_hand(tmp_path, monkeypatch, home):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from daedalus.ui import claude_bridge as CB
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    QSettings("Daedalus", "Daedalus").remove("claude/config_path")
    cfg = tmp_path / "somewhere" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    monkeypatch.setattr(L, "config_paths", lambda: [tmp_path / "nowhere" / "claude_desktop_config.json"])
    d = CB.ClaudeDesktopDialog(None)
    d.path.setText(str(cfg))                                            # typed by hand
    d.load_file()
    assert "other" in d.editor.toPlainText() and "no Daedalus entry" in d.json_ok.text()
    d.editor.setPlainText("{ broken")
    assert not d.save_btn.isEnabled() and "not valid JSON" in d.json_ok.text()
    d.editor.setPlainText(cfg.read_text())
    d.insert_entry()
    assert "Daedalus entry present" in d.json_ok.text()
    txt = json.loads(d.editor.toPlainText())
    txt["mcpServers"]["daedalus"]["command"] = r"C:\my\python.exe"         # edited by hand
    d.editor.setPlainText(json.dumps(txt))
    d.save_file()
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["other"] == {"command": "x"}
    assert saved["mcpServers"]["daedalus"]["command"] == r"C:\my\python.exe"
    assert cfg.with_name(cfg.name + ".before-daedalus").exists()
    assert QSettings("Daedalus", "Daedalus").value("claude/config_path") == str(cfg)   # remembered for Connect
    QSettings("Daedalus", "Daedalus").remove("claude/config_path")
    d.close()
