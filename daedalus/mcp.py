"""`python -m daedalus.mcp` — the MCP server Claude Desktop starts (stdin / stdout, JSON-RPC 2.0, one message
per line). It forwards Icarus's tools to the open Daedalus over 127.0.0.1 (see claude_link.py). Stdlib only and
quick to start: it never loads the CAD kernel or Qt itself.

Extra tools of its own: daedalus_status (is Daedalus open, which workspace), daedalus_open (start it and wait),
daedalus_guide (how to model with these tools — Icarus's instructions)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import claude_link as L

PROTOCOL = "2025-06-18"
INSTRUCTIONS = """Daedalus is a desktop CAD + inventory app (the user's own). These tools drive the Daedalus window that
is open on the user's computer — parts build live there, as editable feature history; inventory (DIMS) changes are
saved to the team's inventory under the user's account and are visible to the whole team.
- Call daedalus_status first; if Daedalus isn't open, call daedalus_open.
- Before modelling, read daedalus_guide once (units, sketch conventions, how features reference each other).
- Call get_model before editing an existing part. One request ≈ one undo step in Daedalus (Ctrl+Z).
- dims_* tools read and change the real inventory: confirm with the user before recording stock, orders or
  production changes they didn't ask for."""

OWN = [
    {"name": "daedalus_status", "description": "Is Daedalus open on this computer, and which workspace (3D modelling "
     "+ DIMS inventory, or DIMS only)? Call this first.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daedalus_open", "description": "Start Daedalus on this computer (if it isn't open) and wait until its "
     "tools are ready (up to about a minute).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daedalus_guide", "description": "How to model with Daedalus's tools: units, sketch planes and entities, "
     "feature references, assemblies, inventory. Read once before building.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _call(link: dict, method: str, path: str, body=None, timeout=600.0):
    req = urllib.request.Request(f"http://127.0.0.1:{link['port']}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Daedalus-Token": link["token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def _cached() -> dict:
    try:
        return json.loads(L.tools_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _schema(t: dict) -> dict:
    return {"name": t["name"], "description": t.get("description", ""),
            "inputSchema": t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}}


def tool_list() -> list[dict]:
    link = L.read_link()
    tools = None
    if link:
        try:
            tools = _call(link, "GET", "/tools", timeout=10)
        except (OSError, ValueError, urllib.error.URLError):
            tools = None
    if tools is None:
        tools = _cached().get("tools") or []
    return OWN + [_schema(t) for t in tools if t.get("name") not in {o["name"] for o in OWN}]


def _text(s: str, err=False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": bool(err)}


def open_daedalus(wait: float = 90.0) -> dict | None:
    link = L.read_link()
    if link:
        return link
    c = _cached()
    cmd = c.get("launch") or [sys.executable, "-m", "daedalus"]
    kw = {"cwd": c.get("cwd") or str(L.root()), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200                     # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)                                            # noqa: S603 (our own app)
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1.0)
        link = L.read_link()
        if link:
            return link
    return None


def call_tool(name: str, args: dict) -> dict:
    if name == "daedalus_status":
        link = L.read_link()
        if not link:
            return _text("Daedalus isn't open on this computer. Call daedalus_open to start it.")
        kind = "3D modelling + DIMS inventory" if link.get("kind") == "3d" else "DIMS inventory only"
        try:
            info = _call(link, "GET", "/info", timeout=10)
        except (OSError, ValueError, urllib.error.URLError) as e:
            return _text(f"Daedalus is starting or busy ({e}) — try again in a moment.", True)
        return _text(json.dumps({"open": True, "workspace": kind, "version": link.get("version"), **(info or {})}))
    if name == "daedalus_open":
        link = open_daedalus()
        if not link:
            return _text("Started Daedalus, but it wasn't ready within 90 seconds — check the computer, then "
                         "call daedalus_status.", True)
        return _text(f"Daedalus is open ({'3D + DIMS' if link.get('kind') == '3d' else 'DIMS'}).")
    if name == "daedalus_guide":
        link = L.read_link()
        if link:
            try:
                return _text(_call(link, "GET", "/guide", timeout=10) or "")
            except (OSError, ValueError, urllib.error.URLError):
                pass
        return _text(_cached().get("guide") or "Open Daedalus first (daedalus_open) to get the guide.")
    link = L.read_link()
    if not link:
        return _text("Daedalus isn't open on this computer — call daedalus_open, then try again.", True)
    try:
        r = _call(link, "POST", "/call", {"name": name, "arguments": args or {}})
    except urllib.error.HTTPError as e:
        return _text(f"Daedalus refused the call ({e.code}: {e.read().decode(errors='replace')[:300]})", True)
    except (OSError, ValueError, urllib.error.URLError) as e:
        return _text(f"Couldn't reach Daedalus ({e}). Is it still open? Call daedalus_status.", True)
    out = _text(r.get("text", ""), r.get("isError"))
    if r.get("image"):                                                     # e.g. a render of the model
        out["content"].append({"type": "image", "data": r["image"], "mimeType": "image/png"})
    return out


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                                        # a notification: nothing to answer
        return None
    try:
        if method == "initialize":
            from . import __version__
            res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "daedalus", "title": "Daedalus", "version": __version__},
                   "instructions": INSTRUCTIONS}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": tool_list()}
        elif method == "tools/call":
            res = call_tool(params.get("name", ""), params.get("arguments") or {})
        elif method in ("resources/list", "prompts/list"):
            res = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{e.__class__.__name__}: {e}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            stdout.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        outs = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        for r in outs:
            stdout.write(json.dumps(r) + "\n")
        stdout.flush()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
