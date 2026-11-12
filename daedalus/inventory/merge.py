"""Three-way merge of inventory data, so several people can edit at the same time without overwriting each other.

base   = what this PC last loaded / saved
mine   = this PC's data now
theirs = what's on the server (or shared file) now

Collections are lists of records with an "id". Records added on either side are kept; a record deleted on one side
is deleted unless the other side changed it meanwhile (then the change wins — nothing is lost silently). When both
sides changed the same record, fields merge one by one: a field only one side touched takes that side's value;
stock counts merge as deltas (two people each taking 5 off 100 → 90, not 95); for any other field both changed,
this PC's edit wins and the clash is reported.
"""
from __future__ import annotations

import json

COUNTERS = {"currentStock"}                       # numeric fields merged as deltas
NEWEST = {"updatedAt"}                            # timestamp fields: keep the larger


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _tidy(x):
    return int(x) if float(x).is_integer() else x


def merge_record(base: dict | None, mine: dict, theirs: dict, conflicts: list | None = None) -> dict:
    base = base or {}
    out = dict(theirs)
    for k in set(mine) | set(theirs) | set(base):
        b, m, t = base.get(k), mine.get(k), theirs.get(k)
        if m == b:                                 # untouched here: theirs (already in out)
            if k not in theirs and k in mine:
                out[k] = m
            continue
        if t == b or t == m:                       # only we changed it (or both to the same value)
            if k in mine:
                out[k] = m
            else:
                out.pop(k, None)
            continue
        if k in COUNTERS:
            out[k] = _tidy(_num(t) + _num(m) - _num(b))
        elif k in NEWEST:
            out[k] = max(_num(m), _num(t))
        else:
            out[k] = m                             # both changed: this PC's edit wins
            if conflicts is not None:
                conflicts.append((mine.get("name") or mine.get("id"), k))
    return out


def _is_records(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, dict) and "id" in x for x in v)


def merge_list(base: list, mine: list, theirs: list, conflicts: list | None = None) -> list:
    if not (_is_records(base) and _is_records(mine) and _is_records(theirs)):
        return mine if mine != base else theirs
    B = {x["id"]: x for x in base}
    Mi = {x["id"]: x for x in mine}
    T = {x["id"]: x for x in theirs}
    out = []
    for x in theirs:
        i = x["id"]
        if i in Mi:
            m = Mi[i]
            if i in B and m != B[i]:
                out.append(merge_record(B[i], m, x, conflicts) if x != B[i] else m)
            else:
                out.append(x)
        elif i in B:                               # deleted here
            if x != B[i]:
                out.append(x)                      # …but changed there: keep their change
                if conflicts is not None:
                    conflicts.append((x.get("name") or i, "deleted here, edited elsewhere — kept"))
        else:
            out.append(x)                          # added there
    for x in mine:
        i = x["id"]
        if i not in T and i not in B:              # added here
            out.append(x)
        elif i not in T and i in B and x != B[i]:  # deleted there, changed here: keep ours
            out.append(x)
            if conflicts is not None:
                conflicts.append((x.get("name") or i, "deleted elsewhere, edited here — kept"))
    return out


def merge_value(key: str, base: str | None, mine: str | None, theirs: str | None,
                conflicts: list | None = None) -> str | None:
    """One storage key (JSON text, as the web app stores it)."""
    if mine == base:
        return theirs
    if theirs == base or theirs == mine:
        return mine
    try:
        b = json.loads(base) if base else None
        m = json.loads(mine) if mine else None
        t = json.loads(theirs) if theirs else None
    except ValueError:
        return mine
    if isinstance(m, list) or isinstance(t, list):
        return json.dumps(merge_list(b or [], m or [], t or [], conflicts), separators=(",", ":"))
    if isinstance(m, dict) and isinstance(t, dict):
        return json.dumps(merge_record(b if isinstance(b, dict) else {}, m, t, conflicts), separators=(",", ":"))
    return mine


def merge_values(base: dict, mine: dict, theirs: dict, conflicts: list | None = None) -> dict:
    keys = set(base) | set(mine) | set(theirs)
    return {k: merge_value(k, base.get(k), mine.get(k), theirs.get(k), conflicts) for k in keys}
