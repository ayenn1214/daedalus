"""Ollama (local models) agent loop — same interface as copilot.Copilot.

Talks to the Ollama server's /api/chat with native tool calling. Qt-free and dependency-free (urllib).
Small local models struggle with ~90 tools, so a "core" tool set (modelling, assembly, materials, import,
BOM) is sent by default; the panel can switch to all tools.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Callable

from .copilot import SYSTEM
from .tools import TOOLS

DEFAULT_HOST = "http://127.0.0.1:11434"   # not "localhost": on Windows that can try IPv6 ::1 first
DEFAULT_MODEL = "qwen2.5:14b"


def normalize_host(h: str | None) -> str:
    """'0.0.0.0', 'localhost', '192.168.1.5:11434', 'http://pc:11434/' → a usable base URL."""
    h = (h or "").strip().rstrip("/") or DEFAULT_HOST
    if "://" not in h:
        h = "http://" + h
    scheme, rest = h.split("://", 1)
    hostport = rest.split("/", 1)[0]
    host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "")
    if not host:
        host, port = hostport, ""
    if host in ("0.0.0.0", "*", "[::]", "::"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port or '11434'}"
DEFAULT_CTX = 32768

CORE_TOOLS = [
    "get_model", "add_sketch", "add_extrude", "add_revolve", "add_sweep", "add_fillet", "add_chamfer", "add_shell",
    "add_hole", "add_linear_pattern", "add_circular_pattern", "add_mirror", "add_plane", "add_loft", "edit_feature",
    "set_dimension", "delete_feature", "suppress_feature", "add_object", "set_object", "place_object",
    "orient_object", "add_mate", "set_mate", "delete_mate", "set_material", "list_materials", "insert_part",
    "import_search", "import_part", "get_bom", "export_file",
]

LOCAL_NOTE = """
Local-model rules
- Call tools through the tool-calling interface only, one feature per call, with valid JSON arguments that
  match the tool's schema exactly. Don't describe a tool call in text — make it.
- Read each tool result; on an error, fix the arguments and call again."""


class OllamaError(RuntimeError):
    pass


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 600.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise OllamaError(f"Ollama {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise OllamaError(f"Can't reach Ollama at {url.split('/api')[0]} — is it running? ({e.reason})") from None


def list_models(host: str = DEFAULT_HOST, timeout: float = 4.0) -> list[str]:
    """Installed model names (empty list if the server isn't reachable)."""
    try:
        got = _http("GET", host.rstrip("/") + "/api/tags", timeout=timeout)
    except OllamaError:
        return []
    return sorted(m.get("name", "") for m in got.get("models", []) if m.get("name"))


def pull(host: str, model: str, progress: Callable[[str, int, int], None] | None = None,
         stop: Callable[[], bool] | None = None, timeout: float = 30.0) -> None:
    """Download a model through the server (POST /api/pull, streamed). progress(status, done, total) bytes;
    stop() → True cancels. Raises OllamaError on failure."""
    req = urllib.request.Request(host.rstrip("/") + "/api/pull", method="POST",
                                 data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                if stop and stop():
                    raise OllamaError("Download cancelled")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise OllamaError(f"Ollama: {msg['error']}")
                if progress:
                    progress(str(msg.get("status", "")), int(msg.get("completed") or 0), int(msg.get("total") or 0))
                if msg.get("status") == "success":
                    return
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama {e.code}: {e.read().decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise OllamaError(f"Can't reach Ollama at {host} — is it running? ({e.reason})") from None


def capabilities(host: str, model: str, timeout: float = 6.0) -> set[str]:
    """{'completion', 'tools', 'vision', 'thinking', …} as reported by /api/show (empty if unknown)."""
    try:
        got = _http("POST", host.rstrip("/") + "/api/show", {"model": model}, timeout=timeout)
    except OllamaError:
        return set()
    return set(got.get("capabilities") or [])


def tool_schemas(names: list[str] | None = None, tools: list[dict] | None = None) -> list[dict]:
    keep = set(names) if names else None
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in (tools if tools is not None else TOOLS) if keep is None or t["name"] in keep]


_THINK = re.compile(r"<think>.*?</think>", re.S)


def _text_tool_calls(text: str, known: set[str]) -> list[dict]:
    """Models without native tool calling sometimes print the call as JSON: recover it."""
    dec = json.JSONDecoder()
    calls, i = [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        i = end
        items = obj if isinstance(obj, list) else [obj]
        for o in items:
            if not isinstance(o, dict):
                continue
            o = o.get("function", o)
            name = o.get("name")
            if name not in known:
                continue
            args = o.get("arguments", o.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            calls.append({"function": {"name": name, "arguments": args}})
    return calls


class OllamaCopilot:
    def __init__(self, call_tool: Callable[[str, dict], tuple[str, bool]], host: str = DEFAULT_HOST,
                 model: str | None = None, num_ctx: int = DEFAULT_CTX, tool_set: str = "core",
                 max_steps: int = 40, temperature: float = 0.2, post=None, tools: list[dict] | None = None,
                 system: str | None = None):
        self.host = host.rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.num_ctx = int(num_ctx)
        self.tool_set = tool_set
        self.call_tool = call_tool
        self.max_steps = max_steps
        self.temperature = temperature
        self.messages: list[dict] = []
        self._stop = False
        self.custom_tools = tools
        self.system = system
        self._post = post or (lambda payload: _http("POST", self.host + "/api/chat", payload))

    def stop(self):
        self._stop = True

    def reset(self):
        self.messages.clear()

    def _tools(self):
        if self.custom_tools is not None:
            return tool_schemas(tools=self.custom_tools)
        return tool_schemas(None if self.tool_set == "all" else CORE_TOOLS)

    def ask(self, prompt: str, model_state: dict | None = None,
            on_text: Callable[[str], None] = print,
            on_tool: Callable[[str, dict, str, bool], None] | None = None,
            images: list[bytes] | None = None) -> str:
        self._stop = False
        content = prompt
        if model_state is not None:
            content = f"<current_model>\n{json.dumps(model_state)}\n</current_model>\n\n{prompt}"
        msg = {"role": "user", "content": content}
        if images:
            msg["images"] = [base64.b64encode(b).decode() for b in images]
        self.messages.append(msg)
        tools = self._tools()
        known = {t["function"]["name"] for t in tools}
        final = ""
        for _ in range(self.max_steps):
            if self._stop:
                break
            resp = self._post({
                "model": self.model, "stream": False, "tools": tools,
                "messages": [{"role": "system", "content": (self.system or SYSTEM) + LOCAL_NOTE}] + self.messages,
                "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
            })
            m = resp.get("message") or {}
            text = _THINK.sub("", m.get("content") or "").strip()
            calls = m.get("tool_calls") or []
            if not calls and text:
                calls = _text_tool_calls(text, known)
                if calls:
                    text = ""
            self.messages.append({"role": "assistant", "content": m.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
            if text:
                final = text
                on_text(text)
            if not calls:
                break
            for c in calls:
                fn = c.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name not in known:
                    out, err = f"Unknown tool '{name}'. Available: {', '.join(sorted(known))}", True
                else:
                    out, err = self.call_tool(name, args)
                if on_tool:
                    on_tool(name, args, out, err)
                self.messages.append({"role": "tool", "tool_name": name,
                                      "content": ("ERROR: " if err else "") + out})
                if self._stop:
                    break
        return final
