"""Put the Rhino link script somewhere stable and tell the user the one command that runs it."""
from __future__ import annotations

import shutil
from pathlib import Path


def script_source() -> Path:
    return Path(__file__).parent / "DaedalusLink.py"


def install(target: str | Path | None = None) -> Path:
    """Copy DaedalusLink.py to ~/.daedalus/rhino/ (or `target`). Returns the installed file."""
    dest_dir = Path(target or Path.home() / ".daedalus" / "rhino")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "DaedalusLink.py"
    shutil.copyfile(script_source(), dest)
    return dest


def startup_command(path: str | Path) -> str:
    """What to type in Rhino (or paste into Options ▸ General ▸ Run these commands every time Rhino starts)."""
    return f'_-RunPythonScript "{Path(path)}"'
