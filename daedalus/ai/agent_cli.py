"""Icarus on a desktop AI subscription: the prompt typed in Icarus goes to an AI company's own command-line agent —
Claude Code (Claude plan), Codex (ChatGPT plan) or Gemini CLI (Google account) — signed in with the person's
account, no API key. Each agent starts `python -m daedalus.mcp`, which hands every tool call to the tool server of the
open window (ui/claude_bridge.py, 'the link'), so the agent works with the same tools as Claude Desktop / ChatGPT.

AgentCLICopilot has the Copilot shape (ask / stop / kill / reset / repair / messages). Subclasses say how to start the
agent and how to read its JSON event stream. Qt-free; run ask() off the GUI thread."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable

from .. import claude_link as L

SERVER = "daedalus"
NOTE = """
You are running inside Daedalus's Icarus panel. The person typed this prompt in Daedalus. Your Daedalus tools come
from its MCP server ("daedalus") and act on the window that is open — use them for every change; don't write files or
run shell commands. Answer briefly: what you did and the key numbers. Attached files are listed by path; read them."""


class AgentError(RuntimeError):
    pass


def _flags():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}      # CREATE_NO_WINDOW


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(content, dict):
        return _text_of(content.get("content")) or json.dumps(content)
    return str(content or "")


def find_command(names: list[str], extra: list[str], chosen: str | None = None) -> str | None:
    """The agent's command: the one chosen in Settings, on PATH, or where its installers put it."""
    if chosen and Path(chosen).is_file():
        return chosen
    for n in names:
        w = shutil.which(n)
        if w:
            return w
    return next((c for c in extra if Path(c).is_file()), None)


def npm_paths(name: str) -> list[str]:
    """Where `npm i -g` and the usual installers leave a command."""
    home = Path.home()
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE") or home)
        roam = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [str(home / ".local" / "bin" / f"{name}.exe"), str(roam / "npm" / f"{name}.cmd"),
                str(local / "Programs" / name / f"{name}.exe"), str(local / "pnpm" / f"{name}.cmd")]
    return [str(home / ".local" / "bin" / name), str(home / ".npm-global" / "bin" / name), f"/usr/local/bin/{name}",
            f"/opt/homebrew/bin/{name}", str(home / ".bun" / "bin" / name)]


def version(cli: str, timeout: float = 20.0) -> str:
    r = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, **_flags())
    return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""


class AgentCLICopilot:
    supports_status = True
    NAME = "agent"                   # 'Claude Code'
    LABEL = "agent"                  # 'Claude (your plan)'
    DIR = "agent"                    # work folder under claude_link.home()
    DROP_ENV: tuple = ()             # API keys that would bill an API account instead of the person's plan
    LOGIN = "Sign in to it once from a terminal, then try again."
    LOGIN_WORDS = ("not logged in", "/login", "login required", "please log in", "authentication", "unauthorized",
                   "oauth", "sign in", "auth")

    def __init__(self, cli: str, model: str = "", system: str | None = None, max_turns: int = 60,
                 call_tool: Callable | None = None, runner=None):
        from .copilot import SYSTEM
        self.cli, self.model, self.max_turns = cli, model or "", max_turns
        self.system = (system or SYSTEM).rstrip() + "\n" + NOTE
        self.call_tool = call_tool                     # unused: tools run through the MCP server
        self.messages: list = []                       # the conversation lives in the agent's own session
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

    # ------------------------------------------------------------ helpers for subclasses
    def workdir(self) -> Path:
        d = L.home() / self.DIR
        (d / "attachments").mkdir(parents=True, exist_ok=True)
        return d

    def env(self) -> dict:
        env = dict(os.environ)
        for k in self.DROP_ENV:
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    PREFIXES: tuple = (f"mcp__{SERVER}__",)        # how the agent names the Daedalus tools

    def short(self, name: str) -> str:
        for pre in self.PREFIXES:
            if name.startswith(pre):
                return name[len(pre):]
        return name

    def hint(self, err: str) -> str:
        low = err.lower()
        if "usage limit" in low or "rate limit" in low or "quota" in low or "credit balance" in low:
            return f"Your {self.LABEL} usage limit is reached for now — try again later, or switch Icarus's engine."
        if any(k in low for k in self.LOGIN_WORDS):
            return f"{self.NAME} isn't signed in. {self.LOGIN}"
        return ""

    def fail(self, msg: str):
        h = self.hint(msg)
        raise AgentError((h + "\n\n" if h else "") + msg)

    # events → Icarus (subclasses call these from handle())
    def _say(self, text: str):
        if text and text.strip():
            self._final = text
            self._on_text(text)

    def _tool_done(self, name: str, args, out: str, err: bool):
        if self._on_tool:
            self._on_tool(self.short(name), args if isinstance(args, dict) else {}, out, bool(err))
        if self._stop:                                 # Stop: finish the tool that ran, then end the turn
            self.kill()

    # ------------------------------------------------------------ subclass hooks
    def command(self, d: Path, files: list[str], images: list[str]) -> list[str]:
        raise NotImplementedError

    def stdin_text(self, content: str) -> str | None:
        return content

    def handle(self, ev: dict):
        raise NotImplementedError

    def prompt_text(self, prompt: str, model_state, files: list[str]) -> str:
        content = prompt
        if model_state is not None:
            content = f"<current_model>\n{json.dumps(model_state)}\n</current_model>\n\n{prompt}"
        if files:
            content += "\n\nAttached files (read them first):\n" + "\n".join(f"- {p}" for p in files)
        return content

    # ------------------------------------------------------------ one prompt
    def _attachments(self, d: Path, images, documents) -> tuple[list[str], list[str]]:
        imgs, docs = [], []
        for i, data in enumerate(images or []):
            ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
            p = d / "attachments" / f"image-{uuid.uuid4().hex[:8]}-{i + 1}{ext}"
            p.write_bytes(data)
            imgs.append(str(p))
        for i, data in enumerate(documents or []):
            p = d / "attachments" / f"document-{uuid.uuid4().hex[:8]}-{i + 1}.pdf"
            p.write_bytes(data)
            docs.append(str(p))
        return imgs, docs

    def ask(self, prompt: str, model_state: dict | None = None,
            on_text: Callable[[str], None] = print,
            on_tool: Callable[[str, dict, str, bool], None] | None = None,
            images: list[bytes] | None = None, documents: list[bytes] | None = None,
            on_status: Callable[[str], None] | None = None) -> str:
        self._stop = self._killed = False
        self._on_text, self._on_tool = on_text, on_tool
        self._status = on_status or (lambda _s: None)
        self._final, self._done, self._error = "", False, None
        if not L.read_links():
            raise AgentError("The Claude link is off, so the agent can't reach this window's tools — turn it on in "
                             "Settings ▸ Claude Desktop (then restart Daedalus).")
        d = self.workdir()
        imgs, docs = self._attachments(d, images, documents)
        files = imgs + docs
        content = self.prompt_text(prompt, model_state, files)
        cmd = self.command(d, files, imgs)
        feed = self.stdin_text(content)
        self._status(f"Starting {self.NAME}…")
        try:
            self.proc = self.runner(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=str(d), env=self.env(), text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, **_flags())
        except OSError as e:
            raise AgentError(f"Couldn't start {self.NAME} ({e}). Install it or set where it is with Icarus's gear "
                             "button.") from None
        err_lines: list[str] = []
        t = threading.Thread(target=lambda: err_lines.extend(self.proc.stderr), daemon=True)
        t.start()
        try:
            if feed is not None:
                self.proc.stdin.write(feed)
            self.proc.stdin.close()
        except OSError:
            pass
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
                if isinstance(ev, dict):
                    self.handle(ev)
                if self._killed:
                    break
        finally:
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.kill()
            t.join(timeout=2)
            self.cleanup(d)
            self.proc = None
        if self._killed or self._stop:
            return self._final
        if self._error:
            self.fail(self._error)
        if not self._done:
            err = "".join(err_lines).strip()[-800:] or f"{self.NAME} ended without an answer."
            self.fail(err)
        return self._final

    def cleanup(self, d: Path):
        pass
