"""Icarus on your Claude plan: the prompt you type in Icarus goes to Claude through Claude Code (the `claude`
command, signed in with your Claude account — no API key), and Claude works with the same Daedalus tools as
Claude Desktop: Claude Code starts `python -m daedalus.mcp`, which hands each tool call to the tool server of the
open window (ui/claude_bridge.py). Claude Desktop's own connection is untouched.

The conversation continues across prompts (one Claude Code session per Icarus chat; New chat starts another).
Qt-free; run ask() off the GUI thread — the tools run on the GUI thread through the tool server."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable

from .. import claude_link as L

SERVER = "daedalus"
MODELS = [("", "Default"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku"),
          ("fable", "Fable")]
NOTE = """
You are running inside Daedalus's Icarus panel. The person typed this prompt in Daedalus; your Daedalus tools come
from its MCP server (named mcp__daedalus__<tool>) and act on the window that is open. Answer briefly: what you did and
the key numbers. Attached files are listed by path; read them with the Read tool."""


class ClaudeCodeError(RuntimeError):
    pass


def candidates() -> list[str]:
    out = []
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        out += [str(home / ".local" / "bin" / "claude.exe"),
                str(Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "claude.cmd"),
                str(Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "claude" /
                    "claude.exe")]
    else:
        home = Path.home()
        out += [str(home / ".local" / "bin" / "claude"), str(home / ".claude" / "local" / "claude"),
                "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
    return out


def find_cli(chosen: str | None = None) -> str | None:
    """The `claude` command: the one chosen in Settings, on PATH, or where the installers put it."""
    if chosen and Path(chosen).is_file():
        return chosen
    w = shutil.which("claude")
    if w:
        return w
    return next((c for c in candidates() if Path(c).is_file()), None)


def _flags():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}      # CREATE_NO_WINDOW


def _env() -> dict:
    """Claude Code signs in with your Claude account; an API key in the environment would bill the API instead."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def version(cli: str, timeout: float = 20.0) -> str:
    r = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=_env(), **_flags())
    return (r.stdout or r.stderr).strip()


def workdir() -> Path:
    d = L.home() / "claude-code"
    (d / "attachments").mkdir(parents=True, exist_ok=True)
    return d


def write_config(d: Path) -> Path:
    p = d / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {SERVER: L.server_entry()}}, indent=2), encoding="utf-8")
    return p


def short(name: str) -> str:
    pre = f"mcp__{SERVER}__"
    return name[len(pre):] if name.startswith(pre) else name


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def hint(err: str) -> str:
    low = err.lower()
    if any(k in low for k in ("not logged in", "/login", "invalid api key", "authentication", "unauthorized",
                              "oauth")):
        return ("Claude Code isn't signed in. Open a terminal, run  claude  once and sign in with your Claude "
                "account, then try again.")
    if "credit balance" in low or "usage limit" in low or "rate limit" in low:
        return "Your Claude plan's usage limit is reached for now — try again later, or switch Icarus to Ollama."
    return ""


class ClaudeCodeCopilot:
    """Same shape as ai.copilot.Copilot (ask / stop / kill / reset / repair / messages)."""
    supports_status = True

    def __init__(self, cli: str, model: str = "", system: str | None = None, max_turns: int = 60,
                 call_tool: Callable | None = None, runner=None):
        from .copilot import SYSTEM
        self.cli, self.model, self.max_turns = cli, model or "", max_turns
        self.system = (system or SYSTEM).rstrip() + "\n" + NOTE
        self.call_tool = call_tool                     # unused: tools run through the MCP server
        self.messages: list = []                       # the conversation lives in the Claude Code session
        self.session_id: str | None = None
        self._stop = self._killed = False
        self.proc = None
        self.runner = runner or subprocess.Popen       # tests swap this
        self.cost = 0.0

    # ------------------------------------------------------------ control
    def stop(self):
        self._stop = True

    def kill(self):
        self._stop = self._killed = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def reset(self):
        self.session_id = None
        self.messages.clear()

    def repair(self):
        pass

    # ------------------------------------------------------------ one prompt
    def command(self, d: Path, cfg: Path, sysfile: Path, reading: bool) -> list[str]:
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--mcp-config", str(cfg), "--strict-mcp-config",
               "--tools", "Read" if reading else "",
               "--allowedTools", f"mcp__{SERVER}" + (",Read" if reading else ""),
               "--permission-mode", "dontAsk",
               "--system-prompt-file", str(sysfile),
               "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        else:
            self._new_id = str(uuid.uuid4())
            cmd += ["--session-id", self._new_id]
        return cmd

    def _attachments(self, d: Path, images, documents) -> list[str]:
        paths = []
        for i, data in enumerate(images or []):
            ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
            p = d / "attachments" / f"image-{uuid.uuid4().hex[:8]}-{i + 1}{ext}"
            p.write_bytes(data)
            paths.append(str(p))
        for i, data in enumerate(documents or []):
            p = d / "attachments" / f"document-{uuid.uuid4().hex[:8]}-{i + 1}.pdf"
            p.write_bytes(data)
            paths.append(str(p))
        return paths

    def ask(self, prompt: str, model_state: dict | None = None,
            on_text: Callable[[str], None] = print,
            on_tool: Callable[[str, dict, str, bool], None] | None = None,
            images: list[bytes] | None = None, documents: list[bytes] | None = None,
            on_status: Callable[[str], None] | None = None) -> str:
        self._stop = self._killed = False
        status = on_status or (lambda _s: None)
        if not L.read_links():
            raise ClaudeCodeError("The Claude link is off, so Claude can't reach this window's tools — turn it on "
                                  "in Settings ▸ Claude Desktop (then restart Daedalus).")
        d = workdir()
        cfg = write_config(d)
        files = self._attachments(d, images, documents)
        content = prompt
        if model_state is not None:
            content = f"<current_model>\n{json.dumps(model_state)}\n</current_model>\n\n{prompt}"
        if files:
            content += "\n\nAttached files (read them first):\n" + "\n".join(f"- {p}" for p in files)
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=d, delete=False, encoding="utf-8") as fh:
            fh.write(self.system)
            sysfile = Path(fh.name)
        cmd = self.command(d, cfg, sysfile, bool(files))
        status("Starting Claude…")
        try:
            self.proc = self.runner(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=str(d), env=_env(), text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, **_flags())
        except OSError as e:
            sysfile.unlink(missing_ok=True)
            raise ClaudeCodeError(f"Couldn't start Claude Code ({e}). Install it (claude.com/claude-code) or set "
                                  "its path in Settings ▸ Icarus.") from None
        err_lines: list[str] = []
        t = threading.Thread(target=lambda: err_lines.extend(self.proc.stderr), daemon=True)
        t.start()
        try:
            self.proc.stdin.write(content)
            self.proc.stdin.close()
        except OSError:
            pass
        final, pending, result = "", {}, None
        try:
            for line in self.proc.stdout:
                if self._killed:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                typ = ev.get("type")
                if typ == "system" and ev.get("subtype") == "init":
                    bad = [s.get("name") for s in ev.get("mcp_servers") or [] if s.get("name") == SERVER
                           and s.get("status") not in ("connected", "ok")]
                    if bad:
                        status("!The Daedalus tools didn't connect to Claude Code — is the Claude link on "
                               "(Settings ▸ Claude Desktop)?")
                    status("Claude is thinking…")
                elif typ == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == \
                            "tool_use":
                        status(f"Running {short(e['content_block'].get('name', ''))}…")
                    elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == \
                            "text_delta":
                        status("Claude is writing…")
                elif typ == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            final = b["text"]
                            on_text(b["text"])
                        elif b.get("type") == "tool_use":
                            pending[b.get("id")] = (short(b.get("name", "")), b.get("input") or {})
                elif typ == "user":
                    content_ = (ev.get("message") or {}).get("content")
                    for b in content_ if isinstance(content_, list) else []:
                        if b.get("type") != "tool_result":
                            continue
                        name, args = pending.pop(b.get("tool_use_id"), ("tool", {}))
                        if on_tool:
                            on_tool(name, args, _text_of(b.get("content")), bool(b.get("is_error")))
                    if self._stop:                     # Stop: finish the tool that ran, then end the turn
                        self.kill()
                        break
                    status("Claude is thinking…")
                elif typ == "result":
                    result = ev
                    self.cost += float(ev.get("total_cost_usd") or 0)
        finally:
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.kill()
            t.join(timeout=2)
            sysfile.unlink(missing_ok=True)
            self.proc = None
        if result is not None and result.get("session_id"):
            self.session_id = result["session_id"]
        elif result is None and getattr(self, "_new_id", None) and not self._killed and final:
            self.session_id = self._new_id
        if self._killed or self._stop:
            if result is not None and result.get("session_id"):
                self.session_id = result["session_id"]
            return final
        if result is None:
            err = "".join(err_lines).strip()[-800:] or "Claude Code ended without an answer."
            raise ClaudeCodeError((hint(err) + "\n\n" if hint(err) else "") + err)
        if result.get("is_error"):
            msg = str(result.get("result") or result.get("subtype") or "error")
            if result.get("subtype") == "error_max_turns":
                msg = f"Stopped after {self.max_turns} steps — say “continue” to carry on."
            raise ClaudeCodeError((hint(msg) + "\n\n" if hint(msg) else "") + msg)
        if not final and result.get("result"):
            final = str(result["result"])
            on_text(final)
        return final
