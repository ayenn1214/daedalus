"""Icarus on your Claude plan, through Claude Code (`claude -p`, signed in with your Claude account — no API key).
See ai/agent_cli.py for how the prompt and the tools travel."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from .. import claude_link as L
from .agent_cli import SERVER, AgentCLICopilot, AgentError, _text_of, find_command, npm_paths
from .agent_cli import version as _version

MODELS = [("", "Default"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku"), ("fable", "Fable")]
ClaudeCodeError = AgentError


def candidates() -> list[str]:
    out = npm_paths("claude")
    if sys.platform != "win32":
        out.append(str(Path.home() / ".claude" / "local" / "claude"))
    return out


def find_cli(chosen: str | None = None) -> str | None:
    if chosen and Path(chosen).is_file():
        return chosen
    w = shutil.which("claude")
    return w or next((c for c in candidates() if Path(c).is_file()), None)


def version(cli: str, timeout: float = 20.0) -> str:
    return _version(cli, timeout)


def short(name: str) -> str:
    pre = f"mcp__{SERVER}__"
    return name[len(pre):] if name.startswith(pre) else name


def write_config(d: Path) -> Path:
    p = d / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {SERVER: L.server_entry()}}, indent=2), encoding="utf-8")
    return p


class ClaudeCodeCopilot(AgentCLICopilot):
    NAME, LABEL, DIR = "Claude Code", "Claude (your plan)", "claude-code"
    DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    LOGIN = "Open a terminal, run  claude  once and sign in with your Claude account, then try again."
    LOGIN_WORDS = ("not logged in", "/login", "invalid api key", "authentication", "unauthorized", "oauth")

    def command(self, d: Path, files, images) -> list[str]:
        cfg = write_config(d)
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=d, delete=False, encoding="utf-8") as fh:
            fh.write(self.system)
            self._sysfile = Path(fh.name)
        reading = bool(files)
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--mcp-config", str(cfg), "--strict-mcp-config",
               "--tools", "Read" if reading else "",
               "--allowedTools", f"mcp__{SERVER}" + (",Read" if reading else ""),
               "--permission-mode", "dontAsk",
               "--system-prompt-file", str(self._sysfile),
               "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        else:
            self._new_id = str(uuid.uuid4())
            cmd += ["--session-id", self._new_id]
        self._pending = {}
        return cmd

    def cleanup(self, d):
        f = getattr(self, "_sysfile", None)
        if f is not None:
            f.unlink(missing_ok=True)

    def handle(self, ev: dict):
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            if any(s.get("name") == SERVER and s.get("status") not in ("connected", "ok")
                   for s in ev.get("mcp_servers") or []):
                self._status("!The Daedalus tools didn't connect to Claude Code — is the Claude link on "
                             "(Settings ▸ Claude Desktop)?")
            self._status("Claude is thinking…")
        elif typ == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "tool_use":
                self._status(f"Running {short(e['content_block'].get('name', ''))}…")
            elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                self._status("Claude is writing…")
        elif typ == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text":
                    self._say(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    self._pending[b.get("id")] = (b.get("name", ""), b.get("input") or {})
        elif typ == "user":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    name, args = self._pending.pop(b.get("tool_use_id"), ("tool", {}))
                    self._tool_done(name, args, _text_of(b.get("content")), b.get("is_error"))
            if not self._stop:
                self._status("Claude is thinking…")
        elif typ == "result":
            self.cost += float(ev.get("total_cost_usd") or 0)
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if ev.get("is_error"):
                msg = str(ev.get("result") or ev.get("subtype") or "error")
                if ev.get("subtype") == "error_max_turns":
                    msg = f"Stopped after {self.max_turns} steps — say “continue” to carry on."
                self._error = msg
            else:
                self._done = True
                if not self._final and ev.get("result"):
                    self._say(str(ev["result"]))

    def ask(self, *a, **k):
        try:
            return super().ask(*a, **k)
        finally:
            if self.session_id is None and getattr(self, "_new_id", None) and self._final:
                self.session_id = self._new_id
