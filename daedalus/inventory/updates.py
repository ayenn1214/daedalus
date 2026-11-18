"""Part updates: what changed on which part profile, by whom and when.

* Team server: read from the audit trail (dims_audit — admins and editors can read it).
* Inventory file: each save writes the part changes made on this PC to <inventory>.updates.jsonl next to the file
  (the store's last-read copy is the 'before', so a teammate's edits merged in aren't put down to you).

An entry: {"at": ms, "by": name, "partId", "part": name, "action": "added" | "updated" | "deleted",
           "changes": [{"field", "label", "old", "new"}]}
Stock moves are left out (they're in the Activity log), as are housekeeping fields (timestamps, file signatures).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import model as M

LABELS = {
    "name": "Name", "partNumber": "Part number", "description": "Description", "type": "Type", "category": "Category",
    "revision": "Revision", "revisionHistory": "Revision history", "weightLb": "Weight (lb)", "unit": "Unit",
    "unitCost": "Unit cost", "unitCostCurrency": "Cost currency", "suppliers": "Suppliers", "drawingUrl": "Drawing",
    "cadFile": "3D model", "imageFile": "Picture", "material": "Material", "finish": "Finish",
    "locationId": "Bin", "reorderPoint": "Reorder point", "reorderQty": "Reorder qty", "components": "BOM parts",
    "projectAllocations": "Projects", "discontinued": "Discontinued", "replacementId": "Replacement",
    "notes": "Notes", "qbPartNumber": "QuickBooks #", "isWeldment": "Weldment", "isAssembly": "Assembly",
    "pcQuotes": "Powder coat quotes", "size": "Size", "length": "Length", "headStyle": "Head style",
    "threadType": "Thread type", "threadStandard": "Thread standard", "grade": "Grade",
}
IGNORE = {"updatedAt", "currentStock", "revisionFiles", "revisionCheckedAt", "createdAt", "createdBy", "id",
          "thumb", "thumbSig", "lastCounted", "discontinuedDate", "modelOrient"}


def _short(v, inv=None, key=None) -> str:
    if v in (None, "", [], {}):
        return "—"
    if key == "locationId" and inv is not None:
        return inv.location_label_for(v) or str(v)
    if key == "projectAllocations" and inv is not None:
        return ", ".join(f"{inv.project_name(a.get('projectId')) or '?'} ×{M.num(a.get('qty')):g}" for a in v) or "—"
    if key == "components" and inv is not None:
        out = []
        for c in v:
            p = inv.part(c.get("partId"))
            out.append(f"{(p or {}).get('partNumber') or (p or {}).get('name') or '?'} ×{M.num(c.get('qty')):g}")
        return ", ".join(out)
    if key == "suppliers":
        return ", ".join(f"{s.get('name')}" + (f" {s.get('partNumber')}" if s.get("partNumber") else "")
                         + (f" ${M.num(s.get('price')):g}" if s.get("price") not in (None, "") else "")
                         for s in v if s and s.get("name")) or "—"
    if key == "revisionHistory":
        return ", ".join(str(h.get("rev")) for h in v) or "—"
    if isinstance(v, dict):
        return ", ".join(f"{k} {x}" for k, x in v.items() if x not in (None, "", 0)) or "—"
    if isinstance(v, list):
        return f"{len(v)} item{'s' if len(v) != 1 else ''}"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    s = str(v)
    if key in ("drawingUrl", "cadFile", "imageFile"):
        s = s.replace("\\", "/").rsplit("/", 1)[-1]
    return s if len(s) <= 80 else s[:77] + "…"


def _same(a, b) -> bool:
    if a in (None, "", [], {}) and b in (None, "", [], {}):
        return True
    if isinstance(a, list) and isinstance(b, list):          # supplier rows: blank rows don't count
        def clean(x):
            return [y for y in x if not (isinstance(y, dict) and not any(v not in (None, "", 0) for v in y.values()))]
        return json.dumps(clean(a), sort_keys=True) == json.dumps(clean(b), sort_keys=True)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def diff(old: dict | None, new: dict | None, inv=None) -> list[dict]:
    old, new = old or {}, new or {}
    out = []
    for k in sorted(set(old) | set(new), key=lambda k: list(LABELS).index(k) if k in LABELS else 999):
        if k in IGNORE or k.startswith("_"):
            continue
        if _same(old.get(k), new.get(k)):
            continue
        out.append({"field": k, "label": LABELS.get(k, k), "old": _short(old.get(k), inv, k),
                    "new": _short(new.get(k), inv, k)})
    return out


def entries_between(before_parts: list, after_parts: list, by: str, inv=None, at: int | None = None) -> list[dict]:
    """Part changes from one list of parts to another (a save on this PC)."""
    b = {p["id"]: p for p in before_parts or [] if isinstance(p, dict) and p.get("id")}
    a = {p["id"]: p for p in after_parts or [] if isinstance(p, dict) and p.get("id")}
    at = at or M.now_ms()
    out = []
    for pid, p in a.items():
        if pid not in b:
            out.append({"at": at, "by": by, "partId": pid, "part": p.get("name") or "", "pn": p.get("partNumber") or "",
                        "action": "added", "changes": []})
            continue
        ch = diff(b[pid], p, inv)
        if ch:
            out.append({"at": at, "by": by, "partId": pid, "part": p.get("name") or "",
                        "pn": p.get("partNumber") or "", "action": "updated", "changes": ch})
    for pid, p in b.items():
        if pid not in a:
            out.append({"at": at, "by": by, "partId": pid, "part": p.get("name") or "", "pn": p.get("partNumber") or "",
                        "action": "deleted", "changes": []})
    return out


# ------------------------------------------------------------------ the file log
def log_path(store) -> Path | None:
    p = getattr(store, "path", None)
    return Path(p).with_name(Path(p).stem + ".updates.jsonl") if p else None


def append(store, entries: list[dict]):
    path = log_path(store)
    if not path or not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_file(store, days: int = 90, limit: int = 2000) -> list[dict]:
    path = log_path(store)
    if not path or not path.exists():
        return []
    cut = M.now_ms() - days * 86400_000
    out = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("at", 0) >= cut:
                out.append((i, e))
    return [e for _i, e in sorted(out, key=lambda x: (-x[1]["at"], -x[0]))][:limit]


# ------------------------------------------------------------------ the server's audit trail
def read_server(store, inv, days: int = 90, limit: int = 1000) -> list[dict]:
    from urllib.parse import quote
    from .chat import iso_utc, ts_ms
    since = iso_utc(M.now_ms() - days * 86400_000)
    code, rows = store._rest("GET", "/rest/v1/dims_audit?select=at,user_id,record_id,action,old,new"
                                    f"&collection=eq.parts&at=gt.{quote(since)}&order=at.desc&limit={limit}")
    if code != 200 or not isinstance(rows, list):
        raise PermissionError("The update history needs an admin or editor account on the team server")
    names = {}
    try:
        code2, people = store._rest("GET", "/rest/v1/dims_profiles?select=user_id,display_name,email")
        if code2 == 200:
            names = {p["user_id"]: p.get("display_name") or (p.get("email") or "?").split("@")[0] for p in people}
    except Exception:  # noqa: BLE001
        pass
    out = []
    for r in rows:
        new, old = r.get("new") or {}, r.get("old") or {}
        act = {"insert": "added", "delete": "deleted"}.get(r.get("action"), "updated")
        ch = diff(old, new, inv) if act == "updated" else []
        if act == "updated" and not ch:
            continue                                          # stock moves / housekeeping only
        p = new or old
        out.append({"at": ts_ms(str(r["at"])), "by": names.get(r.get("user_id"), "?"), "partId": r.get("record_id"),
                    "part": p.get("name") or "", "pn": p.get("partNumber") or "", "action": act, "changes": ch})
    return out


def read(store, inv, days: int = 90) -> list[dict]:
    if getattr(store, "kind", "file") == "supabase":
        return read_server(store, inv, days)
    return read_file(store, days)


def last_by_part(entries) -> dict:
    """{partId: newest entry}."""
    out = {}
    for e in entries:
        if e["partId"] not in out or e["at"] > out[e["partId"]]["at"]:
            out[e["partId"]] = e
    return out
