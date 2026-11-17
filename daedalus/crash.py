"""Crash reports.

• Any unhandled Python error (main thread, worker threads, Qt slots) → a report file in ~/.daedalus/crashes and a
  window: what happened, the details, Copy report, Open folder, Report on GitHub (pre-filled issue), Continue.
• A hard crash (Qt / OCC segfault, abort) can't run Python, so faulthandler writes the stack to a session file;
  the next start finds it and offers the report ("Daedalus closed unexpectedly last time").
The same error is only shown once per session; reports keep the last 50 files.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import os
import platform
import sys
import threading
import traceback
from pathlib import Path
from urllib.parse import quote

REPO = "ayenn1214/daedalus"
REPORT_TO = "narula.ayenn@gmail.com"           # crash reports go here by default
KEEP = 50
_state = {"installed": False, "seen": set(), "fatal": None, "showing": False, "ui": None, "context": None}


def crash_dir() -> Path:
    d = Path(os.environ.get("DAEDALUS_HOME") or Path.home() / ".daedalus") / "crashes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "?"


def system_info() -> dict:
    info = {"Daedalus": _version(), "OS": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "Python": platform.python_version()}
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        info["Qt"] = f"{qVersion()} (PySide6 {PySide6.__version__})"
    except Exception:  # noqa: BLE001
        pass
    ctx = _state.get("context")
    if callable(ctx):
        try:
            info.update({k: str(v) for k, v in (ctx() or {}).items()})
        except Exception:  # noqa: BLE001
            pass
    return info


def set_context(fn):
    """fn() → {label: value} added to every report (open module / tab / file …)."""
    _state["context"] = fn


def build_report(exc_type, exc, tb, where: str = "") -> str:
    when = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"Daedalus crash report — {when}"]
    lines += [f"{k}: {v}" for k, v in system_info().items()]
    if where:
        lines.append(f"Where: {where}")
    lines += ["", "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()]
    return "\n".join(lines) + "\n"


def summary(exc_type, exc) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{exc_type.__name__}: {msg}"[:200] if msg else exc_type.__name__


def _prune():
    files = sorted(crash_dir().glob("crash-*.txt"), key=lambda p: p.stat().st_mtime)
    for p in files[:-KEEP]:
        try:
            p.unlink()
        except OSError:
            pass


def save_report(text: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    p, n = crash_dir() / f"crash-{stamp}.txt", 1
    while p.exists():
        n += 1
        p = crash_dir() / f"crash-{stamp}-{n}.txt"
    p.write_text(text, encoding="utf-8")
    _prune()
    return p


def _key(exc_type, tb) -> tuple:
    frames = traceback.extract_tb(tb)[-3:] if tb else []
    return (exc_type.__name__,) + tuple((f.filename, f.lineno) for f in frames)


def issue_url(title: str, text: str) -> str:
    body = "**What were you doing?**\n\n\n**Report**\n```\n" + text[-5500:] + "\n```\n"
    return f"https://github.com/{REPO}/issues/new?title={quote('Crash: ' + title)}&body={quote(body)}"


def mail_url(title: str, text: str, path=None, to: str = REPORT_TO) -> str:
    """mailto: with the report filled in. Mail apps cap the link (~2000 chars on Windows), so the body keeps the
    header and the end of the traceback and names the saved file to attach."""
    head, _, rest = text.partition("\n\n")
    room = 1500 - len(head)
    tail = rest if len(rest) <= room else "…\n" + rest[-room:]
    body = ("What was I doing:\n\n\n---\n" + head + "\n\n" + tail
            + (f"\n\nFull report: {path} (attach it)" if path else ""))
    return f"mailto:{quote(to, safe='@')}?subject={quote('Daedalus crash: ' + title[:90])}&body={quote(body)}"


def handle(exc_type, exc, tb, where: str = ""):
    """Write the report and (once per distinct error) show it. Never raises."""
    try:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return sys.__excepthook__(exc_type, exc, tb)
        text = build_report(exc_type, exc, tb, where)
        path = save_report(text)
        sys.stderr.write(text)
        k = _key(exc_type, tb)
        if k in _state["seen"]:
            return path
        _state["seen"].add(k)
        ui = _state.get("ui")
        if ui is not None:
            ui(summary(exc_type, exc), text, path)
        return path
    except Exception:  # noqa: BLE001
        traceback.print_exception(exc_type, exc, tb)


def _thread_hook(args):
    handle(args.exc_type, args.exc_value, args.exc_traceback, f"thread {getattr(args.thread, 'name', '?')}")


# ------------------------------------------------------------------ hard crashes
def _fatal_path() -> Path:
    return crash_dir() / f"session-{os.getpid()}.log"


def _alive(p: Path) -> bool:
    try:
        pid = int(p.stem.split("-", 1)[1])
    except (IndexError, ValueError):
        return False
    if pid == os.getpid():
        return False
    if sys.platform == "win32":
        return False                                   # an open file can't be deleted there — unlink decides
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def last_fatal() -> tuple[Path, str] | None:
    """A hard crash from an earlier session (its faulthandler file has a stack) → (report path, text)."""
    out = None
    for p in crash_dir().glob("session-*.log"):
        if p == _state.get("fatal_path"):
            continue
        if _alive(p):
            continue                                   # another Daedalus is running
        try:
            stack = p.read_text(encoding="utf-8", errors="replace").strip()
            when = dt.datetime.fromtimestamp(p.stat().st_mtime)
            p.unlink()
        except OSError:
            continue                                   # still open (Windows): that session is running
        if stack and out is None:
            text = (f"Daedalus crash report — hard crash (app closed)\n"
                    + "\n".join(f"{k}: {v}" for k, v in system_info().items())
                    + f"\nFound: {when:%Y-%m-%d %H:%M}\n\n{stack}\n")
            out = (save_report(text), text)
    return out


def install(ui=None):
    """Hook everything. ui(summary, text, path) shows the window (set later with set_ui once Qt is up)."""
    if ui is not None:
        _state["ui"] = ui
    if _state["installed"]:
        return
    _state["installed"] = True
    sys.excepthook = lambda t, e, tb: handle(t, e, tb)
    threading.excepthook = _thread_hook
    try:
        fp = _fatal_path()
        _state["fatal_path"] = fp
        _state["fatal"] = open(fp, "w", encoding="utf-8")       # noqa: SIM115 — kept open for the session
        faulthandler.enable(file=_state["fatal"], all_threads=True)
    except Exception:  # noqa: BLE001
        pass


def set_ui(ui):
    _state["ui"] = ui


def close():
    """Clean exit: no hard crash to report next time."""
    f = _state.get("fatal")
    if f is None:
        return
    try:
        faulthandler.disable()
        f.close()
        _state["fatal_path"].unlink()
    except Exception:  # noqa: BLE001
        pass
    _state["fatal"] = None
