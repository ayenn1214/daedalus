"""Icarus on any OpenAI-compatible chat API: OpenAI (GPT), local desktop servers (LM Studio, Jan, llama.cpp,
vLLM, LocalAI, GPT4All, KoboldCpp) and hosted ones (OpenRouter, Groq, Mistral, Google Gemini, xAI, DeepSeek,
Together…). Chat Completions with function calling; models that can't call tools get prompted tools
(ai/toolsets.py). Qt-free; ask() runs off the GUI thread, tools run through call_tool."""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from . import toolsets as TS

# key, label, base URL, needs a key, default model, local
PRESETS = [
    ("openai", "OpenAI (GPT)", "https://api.openai.com/v1", True, "gpt-5.5", False),
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1", False, "", True),
    ("jan", "Jan", "http://127.0.0.1:1337/v1", False, "", True),
    ("llamacpp", "llama.cpp server", "http://127.0.0.1:8080/v1", False, "", True),
    ("vllm", "vLLM", "http://127.0.0.1:8000/v1", False, "", True),
    ("localai", "LocalAI", "http://127.0.0.1:8080/v1", False, "", True),
    ("gpt4all", "GPT4All", "http://127.0.0.1:4891/v1", False, "", True),
    ("kobold", "KoboldCpp", "http://127.0.0.1:5001/v1", False, "", True),
    ("ollama", "Ollama (OpenAI API)", "http://127.0.0.1:11434/v1", False, "", True),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", True, "openai/gpt-5.5", False),
    ("groq", "Groq", "https://api.groq.com/openai/v1", True, "", False),
    ("mistral", "Mistral", "https://api.mistral.ai/v1", True, "mistral-large-latest", False),
    ("gemini", "Google Gemini API", "https://generativelanguage.googleapis.com/v1beta/openai", True, "gemini-2.5-flash",
     False),
    ("xai", "xAI (Grok)", "https://api.x.ai/v1", True, "", False),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1", True, "deepseek-chat", False),
    ("together", "Together AI", "https://api.together.xyz/v1", True, "", False),
    ("custom", "Custom (any OpenAI-compatible URL)", "", False, "", False),
]
PRESET = {p[0]: p for p in PRESETS}
NOTE = """
Tool rules
- Make each change with a tool call, one feature per call, with arguments that match the tool's schema exactly.
- Read each tool result; on an error, fix the arguments and call again. Answer briefly when done."""
_THINK = re.compile(r"<think>.*?</think>", re.S)
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # local servers: never via a proxy


class OpenAIError(RuntimeError):
    pass


def is_local(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.startswith(("192.168.", "10.")) or h.endswith(".local")


def _opener(url):
    return _DIRECT if is_local(url) else urllib.request.build_opener()


def _headers(key: str | None, base: str) -> dict:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if "openrouter.ai" in base:
        h["HTTP-Referer"], h["X-Title"] = "https://github.com/ayenn1214/daedalus", "Daedalus"
    return h


def _err(e: urllib.error.HTTPError, base: str) -> OpenAIError:
    body = e.read().decode(errors="replace")
    try:
        j = json.loads(body)
        body = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else (j.get("error") or body)
    except ValueError:
        pass
    hint = ""
    if e.code in (401, 403):
        hint = " — check the API key (gear button)."
    elif e.code == 404:
        hint = " — check the server address and the model name."
    elif e.code == 429:
        hint = " — rate limit or quota reached; wait or check your plan."
    return OpenAIError(f"{base} {e.code}: {body}{hint}")


def http(method: str, url: str, key: str | None = None, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(key, url))
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise _err(e, url.rsplit("/", 1)[0]) from None
    except urllib.error.URLError as e:
        raise OpenAIError(f"Can't reach {url.rsplit('/', 1)[0]} — is the server running? ({e.reason})") from None


def list_models(base: str, key: str | None = None, timeout: float = 6.0) -> list[str]:
    try:
        got = http("GET", base.rstrip("/") + "/models", key, timeout=timeout)
    except OpenAIError:
        return []
    return sorted(str(m.get("id")) for m in got.get("data") or [] if m.get("id"))


def chat_stream(base: str, key: str | None, payload: dict, on_progress=None, stopped=None, keep=None,
                timeout: float = 900.0) -> dict:
    """POST /chat/completions streamed (SSE). Returns {'content', 'tool_calls': [{id, name, arguments(str)}],
    'finish_reason'}."""
    url = base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps({**payload, "stream": True}).encode(), method="POST",
                                 headers=_headers(key, base))
    content, calls, finish, first = [], {}, None, True
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            if keep:
                keep(r)
            ctype = r.headers.get("Content-Type", "")
            if "event-stream" not in ctype:                       # a server that ignores stream=True
                j = json.loads(r.read().decode() or "{}")
                ch = (j.get("choices") or [{}])[0]
                m = ch.get("message") or {}
                return {"content": m.get("content") or "", "finish_reason": ch.get("finish_reason"),
                        "tool_calls": [{"id": c.get("id"), "name": (c.get("function") or {}).get("name", ""),
                                        "arguments": (c.get("function") or {}).get("arguments") or "{}"}
                                       for c in m.get("tool_calls") or []]}
            for raw in r:
                if stopped and stopped():
                    break
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except ValueError:
                    continue
                if j.get("error"):
                    e = j["error"]
                    raise OpenAIError(str(e.get("message") if isinstance(e, dict) else e))
                for ch in j.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        content.append(d["content"])
                        if first and on_progress:
                            on_progress("Writing…")
                            first = False
                    if d.get("reasoning_content") or d.get("reasoning"):
                        if first and on_progress:
                            on_progress("Thinking…")
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                            if on_progress:
                                on_progress(f"Calling {slot['name']}…")
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"] if isinstance(fn["arguments"], str) else \
                                json.dumps(fn["arguments"])
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
    except urllib.error.HTTPError as e:
        raise _err(e, base) from None
    except urllib.error.URLError as e:
        raise OpenAIError(f"Can't reach {base} — is the server running? ({e.reason})") from None
    except (TimeoutError, OSError) as e:
        if stopped and stopped():
            return {"content": "", "tool_calls": [], "finish_reason": "stop"}
        raise OpenAIError(f"No answer from {base} ({e}).") from None
    return {"content": "".join(content), "finish_reason": finish,
            "tool_calls": [calls[k] for k in sorted(calls)]}


def _no_tools_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in ("does not support tools", "tool use is not supported", "tools are not supported",
                                  "tool_choice", "function calling is not", "not support function", "tools param"))


class OpenAICopilot:
    supports_status = True

    def __init__(self, call_tool: Callable[[str, dict], tuple[str, bool]] | None = None, base_url: str = "",
                 api_key: str | None = None, model: str = "", tool_set: str = "core", tool_mode: str = "auto",
                 tools: list[dict] | None = None, system: str | None = None, max_steps: int = 40,
                 temperature: float | None = None, label: str = "OpenAI", post=None):
        self.call_tool, self.base, self.key, self.model = call_tool, base_url.rstrip("/"), api_key, model
        self.tool_set, self.tool_mode, self.custom_tools, self.system = tool_set, tool_mode, tools, system
        self.max_steps, self.temperature, self.label = max_steps, temperature, label
        self.messages: list[dict] = []
        self._stop = self._killed = False
        self._resp = None
        self._post = post                               # tests: payload → reply dict like chat_stream's
        self.prompted = tool_mode == "prompted"

    def stop(self):
        self._stop = True

    def kill(self):
        self._stop = self._killed = True
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    def reset(self):
        self.messages.clear()

    def repair(self):
        """Every tool call needs a result before the next request: fill in the ones a stop skipped."""
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                have = {x.get("tool_call_id") for x in self.messages[i + 1:] if x.get("role") == "tool"}
                for c in m["tool_calls"]:
                    if c["id"] not in have:
                        self.messages.append({"role": "tool", "tool_call_id": c["id"],
                                              "content": "Not run — stopped by the user."})
                break
            if m.get("role") == "user" and not isinstance(m.get("content"), str):
                break

    def _defs(self):
        return TS.select(self.tool_set, self.custom_tools, limit=TS.API_TOOL_LIMIT)

    def _system(self, defs):
        from .copilot import SYSTEM
        s = (self.system or SYSTEM) + NOTE
        return s + "\n" + TS.catalogue(defs) if self.prompted else s

    def _step(self, payload, status):
        if self._post is not None:
            return self._post(payload)
        return chat_stream(self.base, self.key, payload, on_progress=status, stopped=lambda: self._killed,
                           keep=lambda r: setattr(self, "_resp", r))

    def ask(self, prompt: str, model_state: dict | None = None, on_text: Callable[[str], None] = print,
            on_tool: Callable[[str, dict, str, bool], None] | None = None, images: list[bytes] | None = None,
            documents: list[bytes] | None = None, on_status: Callable[[str], None] | None = None) -> str:
        self._stop = self._killed = False
        status = on_status or (lambda _t: None)
        if not self.model:
            raise OpenAIError(f"Pick a model for {self.label} (model box).")
        text = prompt if model_state is None else \
            f"<current_model>\n{json.dumps(model_state)}\n</current_model>\n\n{prompt}"
        if images:
            content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": "data:image/%s;base64,%s" % (
                    "jpeg" if b[:3] == b"\xff\xd8\xff" else "png", base64.b64encode(b).decode())}} for b in images]
        else:
            content = text
        self.messages.append({"role": "user", "content": content})
        defs = self._defs()
        known = {t["name"] for t in defs}
        final = ""
        for _ in range(self.max_steps):
            if self._stop:
                break
            payload = {"model": self.model, "messages": [{"role": "system", "content": self._system(defs)}] +
                       self.messages}
            if not self.prompted:
                payload["tools"] = TS.openai_schemas(defs)
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            status(f"Waiting for {self.model}…")
            try:
                r = self._step(payload, status)
            except OpenAIError as e:
                if not self.prompted and self.tool_mode == "auto" and _no_tools_error(str(e)):
                    self.prompted = True               # this model can't call tools: describe them instead
                    status(f"!{self.model} can't call tools natively — Icarus is describing them in the prompt "
                           "instead (slower, less reliable).")
                    continue
                raise
            finally:
                self._resp = None
            if self._killed:
                break
            said = _THINK.sub("", r.get("content") or "").strip()
            calls = [(c["name"], c["arguments"], c.get("id") or f"call_{i}") for i, c in enumerate(r["tool_calls"])]
            if not calls and said and (self.prompted or self.tool_mode == "auto"):
                found = TS.parse_calls(said, known)
                if found:
                    calls = [(n, json.dumps(a), f"call_{i}") for i, (n, a) in enumerate(found)]
                    said = ""
            if self.prompted or (calls and not r["tool_calls"]):     # text tool calls: plain messages
                self.messages.append({"role": "assistant", "content": r.get("content") or ""})
            else:
                self.messages.append({"role": "assistant", "content": r.get("content") or None,
                                      **({"tool_calls": [{"id": cid, "type": "function",
                                                          "function": {"name": n, "arguments": a or "{}"}}
                                                         for n, a, cid in calls]} if calls else {})})
            if said:
                final = said
                on_text(said)
            if not calls:
                break
            text_results = []
            for name, a, cid in calls:
                try:
                    args = json.loads(a or "{}")
                except ValueError:
                    args = None
                if self._killed:
                    break
                if args is None:
                    out, err = f"Arguments for {name} weren't valid JSON: {a[:200]}", True
                elif name not in known:
                    out, err = f"Unknown tool '{name}'. Available: {', '.join(sorted(known))}", True
                else:
                    out, err = self.call_tool(name, args)
                if on_tool:
                    on_tool(name, args or {}, out, err)
                body = ("ERROR: " if err else "") + out
                if self.prompted or not r["tool_calls"]:
                    text_results.append(f"TOOL RESULT {name}:\n{body}")
                else:
                    self.messages.append({"role": "tool", "tool_call_id": cid, "content": body})
                if self._stop:
                    break
            if text_results:
                self.messages.append({"role": "user", "content": "\n\n".join(text_results)})
            if self._stop:
                self.repair()
        return final
