"""Closing and restarting Daedalus reliably (updates, roll-back, theme changes).

Restart order matters: every window is asked to close first (a "save changes?" answer of Cancel stops the restart
and nothing else happens), then the new copy is started, then this process quits — and is ended outright a few
seconds later if a background thread or a hidden window would otherwise keep it alive, so an update helper waiting
for it to exit isn't left waiting."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow


def close_all(window=None) -> bool:
    """Close every main window (others first, then `window`). False if one refused (user cancelled)."""
    wins = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMainWindow) and w.isVisible()]
    for w in [w for w in wins if w is not window] + ([window] if window in wins else []):
        if not w.close():
            return False
    return True


def hard_quit(grace_ms: int = 4000):
    """Quit the event loop; if the process is still alive after grace_ms, end it."""
    QApplication.quit()
    QTimer.singleShot(grace_ms, lambda: os._exit(0))
    t = __import__("threading").Timer(grace_ms / 1000 + 2, lambda: os._exit(0))
    t.daemon = True
    t.start()


def gui_python() -> str:
    """The interpreter to start Daedalus with: pythonw.exe on Windows (no console window)."""
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("python.exe"):
        w = py[:-10] + "pythonw.exe"
        if Path(w).exists():
            return w
    return py


def launch_args(extra: list[str] | None = None, drop_files: bool = False) -> list[str]:
    """Arguments this copy was started with (startup modules, a file to open), for the next start."""
    args = list(sys.argv[1:])
    if drop_files:
        args = [a for a in args if not str(a).lower().endswith((".daedalus", ".strata"))]
    return args + list(extra or [])


def start_new(args: list[str] | None = None) -> bool:
    """Start a fresh Daedalus (detached, from the install folder)."""
    from .. import updater as U
    return bool(QProcess.startDetached(gui_python(), ["-m", "daedalus", *(launch_args() if args is None else args)],
                                       str(U.root())))


def restart(window=None, args: list[str] | None = None) -> bool:
    """Close everything, start a new copy, quit. False (and nothing happens) if a window refused to close."""
    if not close_all(window):
        return False
    start_new(args)
    hard_quit()
    return True
