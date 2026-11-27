"""Connect desktop AI apps to Daedalus: each gets the same MCP server Claude Desktop uses (`python -m daedalus.mcp`),
so it works with every Icarus tool in the open Daedalus window.

ChatGPT desktop, Codex CLI and the Codex IDE extension share ~/.codex/config.toml; Gemini CLI, LM Studio, Cursor,
Windsurf and VS Code each have their own settings file; Claude Code is added with its own command. Claude Desktop is
in claude_link.py. Every edit keeps the rest of the file and saves the old one as <name>.before-daedalus."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import claude_link as L

NAME = L.SERVER_NAME


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) if sys.platform == "win32" else Path.home()


def _appdata() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")


def entry(cwd: bool = False) -> dict:
    """The server command; DAEDALUS_HOME pinned so every app finds this window's link. cwd=True: run from the code
    folder instead of setting PYTHONPATH (Gemini CLI refuses PYTHONPATH)."""
    e = L.server_entry()
    env = {**(e.get("env") or {}), "DAEDALUS_HOME": str(L.home())}
    out = {"command": e["command"], "args": list(e["args"])}
    if cwd:
        env.pop("PYTHONPATH", None)
        out["cwd"] = str(L.root())
    out["env"] = env
    return out


# key, name, what it covers, format
APPS = [
    ("chatgpt", "ChatGPT desktop · Codex", "ChatGPT's desktop app, the Codex CLI and the Codex IDE extension (one "
                                           "shared settings file)", "codex"),
    ("gemini", "Gemini CLI", "Google's command-line agent", "json"),
    ("claude_code", "Claude Code", "Anthropic's command-line agent (user scope)", "claude"),
    ("lmstudio", "LM Studio", "Local models with tools (LM Studio 0.3.17+)", "json"),
    ("cursor", "Cursor", "Cursor's agent", "json"),
    ("windsurf", "Windsurf", "Windsurf's Cascade agent", "json"),
    ("vscode", "VS Code (Copilot agent)", "GitHub Copilot agent mode in VS Code", "vscode"),
]
APP = {a[0]: a for a in APPS}


def config_path(key: str) -> Path:
    h = _home()
    if key == "chatgpt":
        return Path(os.environ.get("CODEX_HOME") or h / ".codex") / "config.toml"
    if key == "gemini":
        return h / ".gemini" / "settings.json"
    if key == "claude_code":
        return h / ".claude.json"
    if key == "lmstudio":
        return h / ".lmstudio" / "mcp.json"
    if key == "cursor":
        return h / ".cursor" / "mcp.json"
    if key == "windsurf":
        return h / ".codeium" / "windsurf" / "mcp_config.json"
    if key == "vscode":
        return _appdata() / "Code" / "User" / "mcp.json"
    raise KeyError(key)


def installed(key: str) -> bool:
    """Is the app (or its settings folder) on this computer?"""
    p = config_path(key)
    if key == "chatgpt":
        return p.parent.exists() or bool(shutil.which("codex"))
    if key == "claude_code":
        return bool(_claude_cli()) or p.exists()
    if key == "gemini":
        return p.parent.exists() or bool(shutil.which("gemini"))
    return p.parent.exists()


# ----------------------------------------------------------------------------- files
def _backup(p: Path):
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".before-daedalus"))


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _read_json(p: Path) -> dict:
    return L._read(p)


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_without(text: str, table: str) -> str:
    """The TOML text minus [table] and its sub-tables ([table.env] …)."""
    out, skip = [], False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            name = m.group(1).replace('"', "").replace("'", "")
            skip = name == table or name.startswith(table + ".")
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + ("\n" if out else "")


def _toml_str(s: str) -> str:
    return json.dumps(s)


def _codex_block() -> str:
    e = entry()
    lines = [f"[mcp_servers.{NAME}]", f"command = {_toml_str(e['command'])}",
             "args = [" + ", ".join(_toml_str(a) for a in e["args"]) + "]",
             "startup_timeout_sec = 60", "tool_timeout_sec = 3600", f"[mcp_servers.{NAME}.env]"]
    lines += [f"{k} = {_toml_str(v)}" for k, v in e["env"].items()]
    return "\n".join(lines) + "\n"


def _codex_has(text: str) -> bool:
    return any((m := _HEADER.match(ln)) and m.group(1).replace('"', "") == f"mcp_servers.{NAME}"
               for ln in text.splitlines())


def _claude_cli() -> str | None:
    from .ai.claude_code import find_cli
    return find_cli()


# ----------------------------------------------------------------------------- public
def status(key: str) -> bool:
    """Is Daedalus in that app's settings?"""
    p = config_path(key)
    if not p.exists():
        return False
    if key == "chatgpt":
        return _codex_has(p.read_text(encoding="utf-8"))
    try:
        cfg = _read_json(p)
    except ValueError:
        return False
    return NAME in (cfg.get("servers" if key == "vscode" else "mcpServers") or {})


def connect(key: str) -> str:
    """Add Daedalus to the app's settings (keeps everything else). Returns what changed."""
    p = config_path(key)
    if key == "chatgpt":
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        _backup(p)
        body = _toml_without(text, f"mcp_servers.{NAME}")
        _write(p, (body + "\n" if body.strip() else "") + _codex_block())
        return str(p)
    if key == "claude_code":
        cli = _claude_cli()
        if cli:
            subprocess.run([cli, "mcp", "remove", "-s", "user", NAME], capture_output=True, timeout=60)
            r = subprocess.run([cli, "mcp", "add-json", "-s", "user", NAME, json.dumps({"type": "stdio", **entry()})],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip() or "claude mcp add-json failed")
            return f"{cli} mcp add-json -s user {NAME}"
    cfg = _read_json(p)
    _backup(p)
    if key == "vscode":
        cfg.setdefault("servers", {})[NAME] = {"type": "stdio", **entry()}
    else:
        cfg.setdefault("mcpServers", {})[NAME] = {**entry(cwd=key == "gemini"),
                                                  **({"trust": True, "timeout": 3_600_000} if key == "gemini" else {})}
    _write(p, json.dumps(cfg, indent=2))
    return str(p)


def disconnect(key: str) -> str:
    p = config_path(key)
    if key == "claude_code" and _claude_cli():
        subprocess.run([_claude_cli(), "mcp", "remove", "-s", "user", NAME], capture_output=True, timeout=60)
        return "claude mcp remove"
    if not p.exists():
        return ""
    if key == "chatgpt":
        text = p.read_text(encoding="utf-8")
        if _codex_has(text):
            _backup(p)
            _write(p, _toml_without(text, f"mcp_servers.{NAME}"))
        return str(p)
    cfg = _read_json(p)
    sect = cfg.get("servers" if key == "vscode" else "mcpServers") or {}
    if NAME in sect:
        _backup(p)
        del sect[NAME]
        _write(p, json.dumps(cfg, indent=2))
    return str(p)


def all_status() -> list[dict]:
    out = []
    for key, name, what, _fmt in APPS:
        try:
            on = status(key)
        except Exception:  # noqa: BLE001
            on = False
        out.append({"key": key, "name": name, "what": what, "installed": installed(key), "connected": on,
                    "path": str(config_path(key))})
    return out
