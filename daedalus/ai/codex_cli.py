"""Icarus on your ChatGPT plan, through Codex (`codex exec --json`, signed in with your ChatGPT account — no API key).
Codex is OpenAI's agent; the ChatGPT desktop app shares its MCP settings (~/.codex/config.toml). The Daedalus MCP
server is passed on the command line, so nothing in your Codex settings changes. See ai/agent_cli.py."""
from __future__ import annotations

import json
from pathlib import Path

from .. import claude_link as L
from .agent_cli import SERVER, AgentCLICopilot, _text_of, find_command, npm_paths
from .agent_cli import version as _version

MODELS = [("", "Default"), ("gpt-5.5", "GPT-5.5"), ("gpt-5.5-codex", "GPT-5.5 Codex"), ("gpt-5.4-mini", "GPT-5.4 mini")]


def find_cli(chosen: str | None = None) -> str | None:
    return find_command(["codex"], npm_paths("codex"), chosen)


def version(cli: str, timeout: float = 20.0) -> str:
    return _version(cli, timeout)


def _toml(v) -> str:
    """A TOML value for `codex -c key=value` (JSON strings and arrays are valid TOML)."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + "}"
    return json.dumps(v)


def server_overrides() -> list[str]:
    e = L.server_entry()
    pre = f"mcp_servers.{SERVER}"
    return ["-c", f"{pre}.command={_toml(e['command'])}", "-c", f"{pre}.args={_toml(e['args'])}",
            "-c", f"{pre}.env={_toml({**(e.get('env') or {}), 'DAEDALUS_HOME': str(L.home())})}", "-c", f"{pre}.startup_timeout_sec=60",
            "-c", f"{pre}.tool_timeout_sec=3600", "-c", f'{pre}.default_tools_approval_mode="approve"']


class CodexCopilot(AgentCLICopilot):
    NAME, LABEL, DIR = "Codex", "ChatGPT (your plan)", "codex"
    DROP_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")
    LOGIN = "Open a terminal, run  codex login  (or just  codex ) and sign in with your ChatGPT account, then try again."
    LOGIN_WORDS = ("not logged in", "codex login", "401", "unauthorized", "authentication", "please log in",
                   "sign in")
    PREFIXES = ()

    def command(self, d: Path, files, images) -> list[str]:
        (d / "AGENTS.md").write_text(self.system, encoding="utf-8")     # Codex reads it as its instructions
        cmd = [self.cli, "exec"]
        if self.session_id:
            cmd += ["resume", self.session_id]
        cmd += ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                *server_overrides()]
        if self.model:
            cmd += ["-m", self.model]
        for p in images:
            cmd += ["-i", p]
        cmd.append("-")                                # the prompt comes on stdin
        self._calls = {}
        return cmd

    def prompt_text(self, prompt, model_state, files):
        docs = [f for f in files if not f.lower().endswith((".png", ".jpg"))]   # images go with -i
        return super().prompt_text(prompt, model_state, docs)

    def handle(self, ev: dict):
        typ = ev.get("type", "")
        if typ == "thread.started":
            self.session_id = ev.get("thread_id") or self.session_id
            self._status("ChatGPT is thinking…")
        elif typ in ("item.started", "item.updated", "item.completed"):
            it = ev.get("item") or {}
            kind = it.get("type")
            if kind in ("mcp_tool_call", "mcpToolCall"):
                if typ == "item.started":
                    self._status(f"Running {it.get('tool', '')}…")
                elif typ == "item.completed":
                    res, err = it.get("result") or {}, it.get("error")
                    out = (err or {}).get("message") if err else _text_of(res.get("content") or res)
                    failed = bool(err) or it.get("status") == "failed" or bool(res.get("is_error") or
                                                                               res.get("isError"))
                    args = it.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    self._tool_done(it.get("tool", "tool"), args or {}, out or "", failed)
                    if not self._stop:
                        self._status("ChatGPT is thinking…")
            elif kind in ("agent_message", "agentMessage") and typ == "item.completed":
                self._say(it.get("text", ""))
            elif kind == "reasoning" and typ == "item.started":
                self._status("ChatGPT is thinking…")
            elif kind in ("command_execution", "commandExecution") and typ == "item.completed":
                self._tool_done("shell", {"command": it.get("command", "")},
                                str(it.get("aggregatedOutput") or it.get("aggregated_output") or ""),
                                it.get("status") in ("failed", "declined"))
            elif kind == "error" and typ == "item.completed":
                self._status("!" + str(it.get("message", "")))
        elif typ == "turn.completed":
            self._done = True
        elif typ == "turn.failed":
            self._error = str((ev.get("error") or {}).get("message") or "Codex turn failed")
        elif typ == "error":
            msg = str(ev.get("message") or "")
            if msg and not msg.lower().startswith(("reconnecting", "retrying")):
                self._error = msg
