"""Local storage for the inventory: one JSON file (put it on a shared drive for the team).

The file holds the same keys as the old web app ("parts", "projects", …). Writes are atomic (temp file +
replace); other Daedalus instances notice changes by modification time and reload.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .model import KEYS, Inventory


def default_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Daedalus" / "holocron-inventory.json"


class InventoryStore:
    """Inventory + its file. Call save() after edits; check_external() to pick up other users' changes."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_path()
        if not path:
            from ..migrate import migrate_inventory
            migrate_inventory(self.path)
        self.inv = Inventory()
        self._mtime = None
        self.listeners = []
        self.load()

    # ------------------------------------------------------------------ io
    def load(self):
        values = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                values = raw.get("values", raw) if isinstance(raw, dict) else {}
                self._mtime = self.path.stat().st_mtime
            except (OSError, ValueError):
                values = {}
        user = self.inv.userName
        self.inv.load_values(values)
        self.inv.userName = user

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": "holocron-inventory", "version": 1, "savedAt": int(time.time() * 1000),
                "savedBy": self.inv.userName or "", "values": self.inv.dump_values()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def check_external(self) -> bool:
        """Reload if another program/PC changed the file. Returns True when reloaded."""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return False
        if self._mtime is not None and m <= self._mtime + 1e-6:
            return False
        self.load()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        return True

    def move_to(self, path):
        """Use another file (e.g. on a network drive). An existing file there is loaded; otherwise the current
        data is written there."""
        self.path = Path(path)
        if self.path.exists():
            self._mtime = None
            self.load()
        else:
            self.save()


_STORE: InventoryStore | None = None


def get_store(path=None) -> InventoryStore:
    """The app-wide store (lazily created)."""
    global _STORE
    if _STORE is None or (path and Path(path) != _STORE.path):
        _STORE = InventoryStore(path)
    return _STORE


def set_store(store: InventoryStore):
    global _STORE
    _STORE = store


__all__ = ["InventoryStore", "get_store", "set_store", "default_path", "KEYS"]
