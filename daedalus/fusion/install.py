"""Copy the Daedalus Link add-in into Fusion's AddIns folder."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def addin_source() -> Path:
    return Path(__file__).parent / "addin" / "DaedalusLink"


def addins_folder() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"                    # not a Fusion platform; useful for tests
    return base / "Autodesk" / "Autodesk Fusion 360" / "API" / "AddIns"


def install(target: str | Path | None = None) -> Path:
    """Install (or update) the add-in. Returns the installed folder. In Fusion: Utilities ▸ Add-Ins ▸
    Scripts and Add-Ins ▸ Add-Ins ▸ DaedalusLink ▸ Run (it then starts with Fusion)."""
    dest = Path(target or addins_folder()) / "DaedalusLink"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(addin_source(), dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest
