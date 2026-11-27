"""Icarus on your Google account, through Gemini CLI (`gemini`, headless, stream-json) — signed in the way you set
it up (Google login, or your Gemini key). The Daedalus MCP server and the instructions go in this engine's own work
folder (.gemini/settings.json, GEMINI.md), so your own Gemini settings don't change. See ai/agent_cli.py."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .. import claude_link as L
from .agent_cli import SERVER, AgentCLICopilot, find_command, npm_paths
from .agent_cli import version as _version

MODELS = [("", "Default"), ("gemini-3-pro", "Gemini 3 Pro"), ("gemini-3-flash", "Gemini 3 Flash"),
          ("gemini-2.5-pro", "Gemini 2.5 Pro"), ("gemini-2.5-flash", "Gemini 2.5 Flash")]


def find_cli(chosen: str | None = None) -> str | None:
    return find_command(["gemini"], npm_paths("gemini"), chosen)


def version(cli: str, timeout: float = 20.0) -> str:
    return _version(cli, timeout)


def write_settings(d: Path, system: str):
    e = L.server_entry()
    env = {k: v for k, v in (e.get("env") or {}).items() if k != "PYTHONPATH"}   # Gemini CLI refuses PYTHONPATH:
    cfg = {"mcpServers": {SERVER: {"command": e["command"], "args": e["args"], "cwd": str(L.root()),  # run from here
                                   "env": {**env, "DAEDALUS_HOME": str(L.home())},
                                   "trust": True, "timeout": 3_600_000}},
           "tools": {"exclude": ["run_shell_command", "write_file", "replace", "web_fetch", "google_web_search"]}}
    (d / ".gemini").mkdir(exist_ok=True)
    (d / ".gemini" / "settings.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (d / "GEMINI.md").write_text(system, encoding="utf-8")


class GeminiCopilot(AgentCLICopilot):
    NAME, LABEL, DIR = "Gemini CLI", "Gemini (your Google account)", "gemini-cli"
    LOGIN = "Open a terminal, run  gemini  once and choose Sign in with Google (or set a Gemini API key), then try again."
    LOGIN_WORDS = ("auth", "login", "credentials", "api key", "unauthenticated", "permission_denied", "401", "403")
    PREFIXES = (f"mcp_{SERVER}_", f"{SERVER}__", f"{SERVER}_")

    def env(self) -> dict:
        env = super().env()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"     # this engine's own folder: load its settings (the MCP server)
        return env

    def command(self, d: Path, files, images) -> list[str]:
        write_settings(d, self.system)
        cmd = [self.cli, "--output-format", "stream-json", "--approval-mode", "yolo",
               "--allowed-mcp-server-names", SERVER, "--skip-trust"]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        else:
            self._new_id = str(uuid.uuid4())
            cmd += ["--session-id", self._new_id]
        self._calls, self._chunks = {}, []
        return cmd                                     # the prompt comes on stdin (headless when not a terminal)

    def _flush(self):
        if self._chunks:
            self._say("".join(self._chunks))
            self._chunks = []

    def handle(self, ev: dict):
        typ = ev.get("type")
        if typ == "init":
            self.session_id = ev.get("session_id") or getattr(self, "_new_id", None)
            self._status("Gemini is thinking…")
        elif typ == "message" and ev.get("role") == "assistant":
            c = ev.get("content") or ""
            if ev.get("delta"):
                self._chunks.append(c)
                self._status("Gemini is writing…")
            else:
                self._chunks = [c]
        elif typ == "tool_use":
            self._flush()
            self._calls[ev.get("tool_id")] = (ev.get("tool_name", ""), ev.get("parameters") or {})
            self._status(f"Running {self.short(ev.get('tool_name', ''))}…")
        elif typ == "tool_result":
            name, args = self._calls.pop(ev.get("tool_id"), ("tool", {}))
            err = ev.get("status") == "error"
            out = (ev.get("error") or {}).get("message") if err else ev.get("output")
            self._tool_done(name, args, str(out or ""), err)
            if not self._stop:
                self._status("Gemini is thinking…")
        elif typ == "error":
            if ev.get("severity") == "error":
                self._status("!" + str(ev.get("message", "")))
        elif typ == "result":
            self._flush()
            if ev.get("status") == "error":
                self._error = str((ev.get("error") or {}).get("message") or "Gemini CLI failed")
            else:
                self._done = True
