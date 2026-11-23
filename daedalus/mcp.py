"""`python -m daedalus.mcp` — the MCP server Claude Desktop starts (stdin / stdout, JSON-RPC 2.0, one message
per line). It forwards Icarus's tools to the open Daedalus over 127.0.0.1 (see claude_link.py). Stdlib only and
quick to start: it never loads the CAD kernel or Qt itself.

Extra tools of its own: daedalus_status (is Daedalus open, which workspace), daedalus_open (start it and wait),
daedalus_guide (how to model with these tools — Icarus's instructions).

CAD Link (SolidWorks / Fusion / Inventor / Solid Edge / Rhino) runs a second tool server when it's open; its tools
(sw_*, cad_*) are offered next to Daedalus's, each call goes to the server that has the tool — so Claude can bridge
the CAD program, Daedalus and DIMS."""
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
  production changes they didn't ask for.
- sw_* and cad_* tools work in the user's CAD program through Daedalus's CAD Link (SolidWorks, Fusion, Inventor,
  Solid Edge, Rhino). daedalus_status says whether CAD Link is open; daedalus_open(cad="solidworks") opens it.
  Read daedalus_guide(part="cad") once before moving work between the CAD program, Daedalus and DIMS."""

CADS = {"solidworks": "--sw", "fusion": "--fusion", "inventor": "--inventor", "solidedge": "--solidedge",
        "rhino": "--rhino"}


OWN = [
    {"name": "daedalus_status", "description": "Is Daedalus open on this computer, and which workspace (3D modelling "
     "+ DIMS inventory, or DIMS only)? Call this first.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daedalus_open", "description": "Start Daedalus on this computer (if it isn't open) and wait until its "
     "tools are ready (up to about a minute). With cad (solidworks, fusion, inventor, solidedge, rhino), open CAD "
     "Link for that program instead.",
     "inputSchema": {"type": "object", "properties": {"cad": {"type": "string", "enum": list(CADS)}}}},
    {"name": "daedalus_guide", "description": "How to model with Daedalus's tools: units, sketch planes and entities, "
     "feature references, assemblies, inventory. Read once before building. part=cad: how to work in the CAD "
     "program through CAD Link and move parts / BOMs between it, Daedalus and DIMS.",
     "inputSchema": {"type": "object", "properties": {"part": {"type": "string", "enum": ["daedalus", "cad"]}}}},
]


def _call(link: dict, method: str, path: str, body=None, timeout=600.0):
    req = urllib.request.Request(f"http://127.0.0.1:{link['port']}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Daedalus-Token": link["token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def _cached(role: str = "app") -> dict:
    try:
        return json.loads(L.tools_file(role).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _schema(t: dict) -> dict:
    return {"name": t["name"], "description": t.get("description", ""),
            "inputSchema": t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}}


def _tools_of(role: str, link: dict | None) -> list[dict]:
    if link:
        try:
            return _call(link, "GET", "/tools", timeout=10) or []
        except (OSError, ValueError, urllib.error.URLError):
            pass
    return _cached(role).get("tools") or []


def routes() -> tuple[list[dict], dict]:
    """Every tool Claude can call, and which server answers it: Daedalus's own first (dims_* go there when both
    are open), then CAD Link's. Closed servers still list their last tools, so Claude knows what opening them gives."""
    links = L.read_links()
    seen = {o["name"] for o in OWN}
    tools, route = [], {}
    for role in L.ROLES:
        for t in _tools_of(role, links.get(role)):
            n = t.get("name")
            if not n or n in seen:
                if n in route and route[n] not in links and role in links:     # the open one answers
                    route[n] = role
                continue
            seen.add(n)
            route[n] = role
            tools.append(_schema(t))
    return tools, route


def tool_list() -> list[dict]:
    return OWN + routes()[0]


def _text(s: str, err=False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": bool(err)}


def open_daedalus(wait: float = 90.0, cad: str | None = None) -> dict | None:
    role = "cad" if cad else "app"
    link = L.read_link(role)
    if link:
        return link
    c = _cached(role) if not cad else _cached("app")
    cmd = list(c.get("launch") or [sys.executable, "-m", "daedalus"])
    if cad:
        cmd = [x for x in cmd if not x.startswith("--")] + [CADS.get(cad, "--sw")]
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
        link = L.read_link(role)
        if link:
            return link
    return None


def _status() -> dict:
    links = L.read_links()
    out = {}
    app = links.get("app")
    if not app:
        out.update({"open": False, "hint": "daedalus_open starts it"})
    else:
        try:
            info = _call(app, "GET", "/info", timeout=10)
        except (OSError, ValueError, urllib.error.URLError) as e:
            info = {"busy": str(e)}
        out.update({"open": True, "workspace": "3D modelling + DIMS inventory" if app.get("kind") == "3d"
                    else "DIMS inventory only", "version": app.get("version"), **(info or {})})
    cad = links.get("cad")
    if not cad:
        out["cad_link"] = {"open": False, "hint": "daedalus_open(cad=\"solidworks\") opens CAD Link"}
    else:
        try:
            info = _call(cad, "GET", "/info", timeout=20)
        except (OSError, ValueError, urllib.error.URLError) as e:
            info = {"busy": str(e)}
        out["cad_link"] = {"open": True, **(info or {})}
    return out


def call_tool(name: str, args: dict) -> dict:
    if name == "daedalus_status":
        st = _status()
        if not st["open"] and not st["cad_link"]["open"]:
            return _text("Daedalus isn't open on this computer. Call daedalus_open to start it "
                         "(daedalus_open(cad=\"solidworks\") for CAD Link).")
        return _text(json.dumps(st, default=str))
    if name == "daedalus_open":
        cad = (args or {}).get("cad")
        link = open_daedalus(cad=cad)
        what = "CAD Link" if cad else "Daedalus"
        if not link:
            return _text(f"Started {what}, but it wasn't ready within 90 seconds — check the computer, then "
                         "call daedalus_status.", True)
        if cad:
            return _text(f"CAD Link is open ({cad}). Its sw_* / cad_* tools are ready — if Claude doesn't list them "
                         "yet, they appear on the next tool refresh.")
        return _text(f"Daedalus is open ({'3D + DIMS' if link.get('kind') == '3d' else 'DIMS'}).")
    if name == "daedalus_guide":
        role = "cad" if (args or {}).get("part") == "cad" else "app"
        link = L.read_link(role)
        if link:
            try:
                return _text(_call(link, "GET", "/guide", timeout=10) or "")
            except (OSError, ValueError, urllib.error.URLError):
                pass
        return _text(_cached(role).get("guide") or ("Open CAD Link first (daedalus_open(cad=...))" if role == "cad"
                                                    else "Open Daedalus first (daedalus_open) to get the guide."))
    _tools, route = routes()
    role = route.get(name, "app")
    link = L.read_link(role)
    if not link:
        if role == "cad":
            return _text("CAD Link isn't open — call daedalus_open(cad=\"solidworks\") (or fusion, inventor, "
                         "solidedge, rhino), then try again.", True)
        return _text("Daedalus isn't open on this computer — call daedalus_open, then try again.", True)
    try:
        r = _call(link, "POST", "/call", {"name": name, "arguments": args or {}})
    except urllib.error.HTTPError as e:
        return _text(f"Daedalus refused the call ({e.code}: {e.read().decode(errors='replace')[:300]})", True)
    except (OSError, ValueError, urllib.error.URLError) as e:
        return _text(f"Couldn't reach {'CAD Link' if role == 'cad' else 'Daedalus'} ({e}). Is it still open? "
                     "Call daedalus_status.", True)
    out = _text(r.get("text", ""), r.get("isError"))
    if r.get("image"):                                                     # e.g. a render of the model
        out["content"].append({"type": "image", "data": r["image"], "mimeType": "image/png"})
    return out


_listed: set | None = None                     # tool names Claude was last given


def changed_notice() -> dict | None:
    """After a call: if opening / closing CAD Link or Daedalus changed the tools, tell Claude to list them again."""
    global _listed
    if _listed is None:
        return None
    try:
        now = {t["name"] for t in tool_list()}
    except Exception:  # noqa: BLE001
        return None
    if now == _listed:
        return None
    _listed = now
    return {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                                        # a notification: nothing to answer
        return None
    try:
        if method == "initialize":
            from . import __version__
            res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": True}},
                   "serverInfo": {"name": "daedalus", "title": "Daedalus", "version": __version__},
                   "instructions": INSTRUCTIONS}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            global _listed
            res = {"tools": tool_list()}
            _listed = {t["name"] for t in res["tools"]}
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
        if any(isinstance(m, dict) and m.get("method") == "tools/call" for m in batch):
            note = changed_notice()
            if note:
                stdout.write(json.dumps(note) + "\n")
        stdout.flush()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
