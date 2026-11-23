"""Claude Desktop ↔ Daedalus: the files they share and the Claude Desktop settings. Qt-free, stdlib only.

* Daedalus (while open) runs a small tool server on 127.0.0.1 and writes where it is to ~/.daedalus/claude-link.json
  (port, a random token, its pid, which workspace). The tools are Icarus's: 3D modelling + DIMS, or DIMS only.
* Claude Desktop starts `python -m daedalus.mcp` (an MCP server on stdin / stdout), which forwards each tool call to
  the open Daedalus — the part builds live in your window, DIMS changes go through your account.
* connect() adds that server to Claude Desktop's settings (claude_desktop_config.json); restart Claude Desktop once.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_NAME = "daedalus"


def home() -> Path:
    return Path(os.environ.get("DAEDALUS_HOME") or Path.home() / ".daedalus")


def link_file() -> Path:
    return home() / "claude-link.json"


def tools_file() -> Path:
    """The last tool list (so Claude Desktop knows the tools even before Daedalus is opened) + how to start it."""
    return home() / "claude-tools.json"


def root() -> Path:
    return Path(__file__).resolve().parents[1]


def console_python(exe: str | None = None) -> str:
    """Claude Desktop talks over stdin / stdout: pythonw.exe has none — use python.exe next to it."""
    exe = exe or sys.executable
    p = Path(exe)
    if p.name.lower() == "pythonw.exe":
        alt = p.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return exe


def launch_command() -> list[str]:
    exe = sys.executable
    if sys.platform == "win32":                    # no console window when Claude starts Daedalus
        w = Path(exe).with_name("pythonw.exe")
        if w.exists():
            exe = str(w)
    return [exe, "-m", "daedalus"]


def read_link() -> dict | None:
    """The running Daedalus's link, if its process is still alive."""
    try:
        d = json.loads(link_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _alive(int(d.get("pid") or 0)):
        return None
    return d


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259                              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_link(port: int, token: str, kind: str, tools: list[dict], guide: str, version: str):
    home().mkdir(parents=True, exist_ok=True)
    tmp = link_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid(), "kind": kind, "version": version,
                               "started": time.time()}), encoding="utf-8")
    os.replace(tmp, link_file())
    try:
        old = json.loads(tools_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if kind == "3d" or old.get("kind") != "3d":    # keep the fuller 3D list when a DIMS-only window opens
        tools_file().write_text(json.dumps({"kind": kind, "tools": tools, "guide": guide, "version": version,
                                            "launch": launch_command(), "cwd": str(root())}), encoding="utf-8")


def clear_link():
    try:
        d = json.loads(link_file().read_text(encoding="utf-8"))
        if int(d.get("pid") or 0) == os.getpid():
            link_file().unlink()
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ Claude Desktop's settings
CONFIG = "claude_desktop_config.json"


def _app_data_dirs(base: Path, depth: int = 4) -> list[Path]:
    """Claude Desktop's own data folders under base: where its settings file is, or where Electron keeps the
    app's data ('Local State' / config.json) — the settings file belongs next to those."""
    if not base.exists():
        return []
    found, marked = [], []
    stack = [(base, 0)]
    while stack:
        d, n = stack.pop()
        try:
            names = {p.name for p in d.iterdir()}
        except OSError:
            continue
        if CONFIG in names:
            found.append(d)
        elif "Local State" in names or "config.json" in names:
            marked.append(d)
        if n < depth:
            stack.extend((d / x, n + 1) for x in names if (d / x).is_dir() and x not in (
                "Cache", "Code Cache", "GPUCache", "blob_storage", "IndexedDB", "Local Storage", "Session Storage",
                "Service Worker", "logs", "Crashpad", "DawnCache", "DawnGraphiteCache", "Shared Dictionary",
                "node_modules", "vm_bundles", "claude-code"))
    return found or marked


def config_paths() -> list[Path]:
    """Where Claude Desktop keeps claude_desktop_config.json on this computer: the usual place, the Store
    version, and newer installs that keep their data under %LOCALAPPDATA%\\Claude-Data\\<id>. A file chosen by
    hand (Choose settings file…) comes first."""
    out: list[Path] = []
    try:
        from PySide6.QtCore import QSettings
        chosen = QSettings("Daedalus", "Daedalus").value("claude/config_path")
        if chosen:
            out.append(Path(chosen))
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        for d in _app_data_dirs(local / "Claude-Data"):
            out.append(d / CONFIG)
        pk = local / "Packages"
        if pk.exists():
            for d in pk.glob("Claude_*"):
                out.append(d / "LocalCache" / "Roaming" / "Claude" / CONFIG)
        out.append(Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / CONFIG)
        seen, uniq = set(), []
        for p in out:
            if str(p).lower() not in seen:
                seen.add(str(p).lower())
                uniq.append(p)
        return uniq
    if sys.platform == "darwin":
        return out + [Path.home() / "Library" / "Application Support" / "Claude" / CONFIG]
    return out + [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / CONFIG]


def snippet() -> str:
    """What to paste into Claude Desktop ▸ Settings ▸ Developer ▸ Edit Config, when connecting by hand."""
    return json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2)


def server_entry() -> dict:
    return {"command": console_python(), "args": ["-m", "daedalus.mcp"],
            "env": {"PYTHONPATH": str(root()), "PYTHONIOENCODING": "utf-8"}}


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ValueError(f"{p} isn't valid JSON — fix or delete it first ({e})") from None


def status() -> dict:
    """{'installed': Claude Desktop found, 'connected': our server is in its settings, 'paths': [...]}"""
    paths = config_paths()
    found = [p for p in paths if p.parent.exists()]
    connected = []
    for p in found:
        try:
            if SERVER_NAME in (_read(p).get("mcpServers") or {}):
                connected.append(p)
        except ValueError:
            pass
    return {"installed": bool(found), "connected": bool(connected), "paths": [str(p) for p in found or paths[:1]],
            "entry": server_entry()}


def connect() -> list[str]:
    """Add Daedalus to Claude Desktop (every settings file it has); keeps everything else; backs the file up."""
    targets = [p for p in config_paths() if p.parent.exists()] or config_paths()[:1]
    done = []
    for p in targets:
        cfg = _read(p)
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + ".before-daedalus"))
        cfg.setdefault("mcpServers", {})[SERVER_NAME] = server_entry()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        done.append(str(p))
    return done


def disconnect() -> list[str]:
    done = []
    for p in config_paths():
        if not p.exists():
            continue
        cfg = _read(p)
        if SERVER_NAME in (cfg.get("mcpServers") or {}):
            del cfg["mcpServers"][SERVER_NAME]
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            os.replace(tmp, p)
            done.append(str(p))
    return done
