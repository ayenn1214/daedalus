"""One-time carry-over from Strata CAD (the app's former name): saved settings and the inventory file."""
from __future__ import annotations

import shutil


def migrate_settings() -> int:
    """Copy every key from the old QSettings store when the new one is empty. Returns keys copied."""
    from PySide6.QtCore import QSettings
    new = QSettings("Daedalus", "Daedalus")
    if new.allKeys() or new.value("migrated/from_strata"):
        return 0
    old = QSettings("Strata", "StrataCAD")
    keys = old.allKeys()
    for k in keys:
        new.setValue(k, old.value(k))
    new.setValue("migrated/from_strata", True)
    new.sync()
    return len(keys)


def migrate_inventory(new_path) -> bool:
    """Copy %APPDATA%/StrataCAD/holocron-inventory.json to the new default location if that is still empty."""
    from pathlib import Path
    new_path = Path(new_path)
    if new_path.exists():
        return False
    old = new_path.parent.parent / "StrataCAD" / new_path.name
    if not old.exists():
        return False
    new_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, new_path)
    return True
