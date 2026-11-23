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
DEFAULT_CTX = 16384        # the system prompt + tools are ~6–10k tokens; 32k made a 14b model spill onto the CPU
KEEP_ALIVE = "30m"         # keep the model loaded between prompts (loading it is most of a first reply's wait)
FIRST_REPLY_S = 900.0      # longest wait for the first word (model load + reading the prompt on a slow PC)


def _all_tools():
    from .tools import TOOLS
    return TOOLS


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


# Ollama is on this PC or the local network: never send it through the system proxy (a Windows / company proxy
# set in Internet Options would otherwise catch 127.0.0.1 and the request just hangs).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 600.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _open(req, timeout) as r:
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
        with _open(req, timeout) as r:
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


def preflight(host: str, model: str, timeout: float = 4.0) -> None:
    """Before a prompt: is Ollama running, and is the model downloaded? Raises OllamaError with what to do —
    instead of a request that waits forever."""
    try:
        _http("GET", host.rstrip("/") + "/api/version", timeout=timeout)
    except OllamaError:
        raise OllamaError(f"Ollama isn't answering at {host}. Start Ollama (Start menu ▸ Ollama — its llama icon "
                          f"appears by the clock), then send again.") from None
    names = list_models(host, timeout=timeout)
    if names and model not in names and f"{model}:latest" not in names:
        raise OllamaError(f"The model '{model}' isn't downloaded. Pick one you have ({', '.join(names[:6])}) or "
                          f"download it: ollama pull {model}")
    if not names:
        raise OllamaError(f"Ollama has no models yet. Download one: ollama pull {model}")


def running(host: str, model: str, timeout: float = 4.0) -> dict | None:
    """How a loaded model sits in memory: {'size', 'size_vram', 'gpu_share'} (None if unknown)."""
    try:
        got = _http("GET", host.rstrip("/") + "/api/ps", timeout=timeout)
    except OllamaError:
        return None
    for m in got.get("models") or []:
        if m.get("name") in (model, f"{model}:latest") or m.get("model") in (model, f"{model}:latest"):
            size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            return {"size": size, "size_vram": vram, "gpu_share": (vram / size) if size else 0.0}
    return None


def chat_stream(host: str, payload: dict, on_progress: Callable[[str], None] | None = None,
                stopped: Callable[[], bool] | None = None, first_timeout: float = FIRST_REPLY_S,
                keep: Callable[[object], None] | None = None) -> dict:
    """POST /api/chat streamed: words arrive as they're written, so a long wait shows progress and a stuck
    server is caught. Returns the whole reply like the non-streamed API ({'message': {...}, stats})."""
    req = urllib.request.Request(host.rstrip("/") + "/api/chat", method="POST",
                                 data=json.dumps({**payload, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    content, calls, last = [], [], {}
    try:
        with _open(req, first_timeout) as r:
            if keep:
                keep(r)
            first = True
            for line in r:
                if stopped and stopped():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise OllamaError(f"Ollama: {msg['error']}")
                m = msg.get("message") or {}
                if m.get("content"):
                    content.append(m["content"])
                if m.get("tool_calls"):
                    calls.extend(m["tool_calls"])
                if first and on_progress:
                    on_progress("Writing…")
                    first = False
                if msg.get("done"):
                    last = msg
                    break
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise OllamaError(f"Ollama {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise OllamaError(f"Can't reach Ollama at {host} — is it running? ({e.reason})") from None
    except (TimeoutError, OSError) as e:
        if stopped and stopped():
            return {"message": {"content": ""}}
        raise OllamaError(f"Ollama didn't answer within {int(first_timeout // 60)} minutes ({e}). The model is "
                          f"probably too big for this PC — pick a smaller one (e.g. qwen2.5:7b).") from None
    out = {k: v for k, v in last.items() if k != "message"}
    out["message"] = {"role": "assistant", "content": "".join(content), **({"tool_calls": calls} if calls else {})}
    return out


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
            for t in (tools if tools is not None else _all_tools()) if keep is None or t["name"] in keep]


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
        self._killed = False
        self.custom_tools = tools
        self.system = system
        self._custom_post = post is not None              # tests / special hosts: one plain request per step
        self._post = post or (lambda payload: _http("POST", self.host + "/api/chat", payload))
        self._resp = None
        self._warned = False
        self.supports_status = True

    def stop(self):
        self._stop = True

    def kill(self):
        """Hard stop: the request in flight is abandoned (its answer is ignored) and no more tools run."""
        self._stop = self._killed = True
        r = self._resp
        if r is not None:                                  # unblock a reply that's still being waited for
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    def _step(self, payload, on_status):
        if self._custom_post:
            return self._post(payload)
        return chat_stream(self.host, payload, on_progress=on_status, stopped=lambda: self._killed,
                           keep=lambda r: setattr(self, "_resp", r))

    def reset(self):
        self.messages.clear()

    def _tools(self):
        if self.custom_tools is not None:
            return tool_schemas(tools=self.custom_tools)
        return tool_schemas(None if self.tool_set == "all" else CORE_TOOLS)

    def ask(self, prompt: str, model_state: dict | None = None,
            on_text: Callable[[str], None] = print,
            on_tool: Callable[[str, dict, str, bool], None] | None = None,
            images: list[bytes] | None = None, documents: list[bytes] | None = None,
            on_status: Callable[[str], None] | None = None) -> str:
        """documents (PDF bytes) aren't sent — local models get the PDF's extracted text in the prompt.
        on_status(text) reports progress ('Loading qwen2.5:14b…', 'Writing…'); a line starting with '!' is a
        tip for the chat (e.g. the model is running on the CPU)."""
        self._stop = self._killed = False
        status = on_status or (lambda _t: None)
        if not self._custom_post:
            status(f"Connecting to Ollama…")
            preflight(self.host, self.model)
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
            status(f"Waiting for {self.model} — loading it and reading the request (the first reply takes longest)…")
            resp = self._step({
                "model": self.model, "stream": False, "tools": tools, "keep_alive": KEEP_ALIVE,
                "messages": [{"role": "system", "content": (self.system or SYSTEM) + LOCAL_NOTE}] + self.messages,
                "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
            }, status)
            self._resp = None
            if getattr(self, "_killed", False):
                break
            if not self._custom_post and not self._warned:
                self._warned = True
                ps = running(self.host, self.model)
                if ps and ps["size"] and ps["gpu_share"] < 0.9:
                    status(f"!{self.model} is running {round(100 * (1 - ps['gpu_share']))}% on the CPU "
                           f"(it doesn't fit in the graphics card's memory) — that's what makes it slow. Pick a smaller "
                           f"model (e.g. qwen2.5:7b) or lower the context size in the Ollama settings.")
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
                if self._killed:
                    break
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
