"""Weekly Excel backup of the team inventory.

From Friday on, everyone using the inventory is reminded that this week's backup is due. As soon as one person
downloads it, the reminder clears for everyone (the "who / when" is stored in the shared data, so it syncs through
the team server or the shared file). Missed on Friday? The reminder stays up until somebody does it.
"""
from __future__ import annotations

import datetime as dt
import json

from .model import KEYS, now_ms, write_xlsx

FRIDAY = 4                                          # Monday = 0


def week_start(now: dt.datetime | None = None, day: int = FRIDAY) -> dt.datetime:
    """Start (00:00 local) of the current backup week: the most recent backup day, today included."""
    now = now or dt.datetime.now()
    back = (now.weekday() - day) % 7
    return (now - dt.timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)


def last_backup(meta: dict) -> dict | None:
    if not meta or not meta.get("weeklyBackupAt"):
        return None
    return {"at": meta["weeklyBackupAt"], "by": meta.get("weeklyBackupBy", ""), "file": meta.get("weeklyBackupFile", "")}


def is_due(meta: dict, now: dt.datetime | None = None) -> bool:
    day = int((meta or {}).get("weeklyBackupDay", FRIDAY))
    start = week_start(now, day)
    last = last_backup(meta)
    return last is None or dt.datetime.fromtimestamp(last["at"] / 1000) < start


def default_name(now: dt.datetime | None = None) -> str:
    return f"Holocron-weekly-backup-{(now or dt.datetime.now()):%Y-%m-%d}.xlsx"


SHEETS = [("Projects", "projects"), ("Production", "productionOrders"), ("Locations", "locations"),
          ("Vendors", "vendors"), ("Clients", "clients"), ("Client Orders", "clientOrders"),
          ("Part Orders", "partOrders"), ("Transactions", "transactions")]


def _flat(rows: list) -> list[dict]:
    """Records → sheet rows with every field as a column (nested values as JSON text)."""
    cols: list = []
    for r in rows:
        if isinstance(r, dict):
            cols += [k for k in r if k not in cols]
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append({k: (json.dumps(r[k], separators=(",", ":")) if isinstance(r.get(k), (list, dict))
                            else r.get(k)) for k in cols})
    return out


def write_backup(inv, path) -> str:
    """Full workbook: the master Inventory and Activity Log sheets plus every other collection, so nothing is
    left out of the backup."""
    path = str(path)
    sheets = [("Inventory", inv.export_rows()), ("Activity Log", inv.log_rows())]
    for title, attr in SHEETS:
        sheets.append((title, _flat(getattr(inv, attr, []) or [])))
    sheets.append(("Parts (all fields)", _flat(inv.parts)))
    write_xlsx(path, [(t, rows or [{"(empty)": ""}]) for t, rows in sheets])
    return path


def mark_done(inv, path) -> dict:
    """Record the backup in the shared data — clears the reminder for everyone."""
    from pathlib import Path
    inv.syncMeta = {**(inv.syncMeta or {}), "weeklyBackupAt": now_ms(), "weeklyBackupBy": inv.userName or "unknown",
                    "weeklyBackupFile": Path(path).name}
    return last_backup(inv.syncMeta)


assert set(dict(SHEETS).values()) <= set(KEYS.values())
