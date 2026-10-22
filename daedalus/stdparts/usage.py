"""Where standard parts are used, and their link to the Holocron inventory.

Daedalus files   <library>/.usage.json maps each saved project file to the standard parts in it
                 (updated whenever a file is saved), so a part lists the files that use it.
Inventory        an inventory part is linked to a standard part by "stdPartId" (set by Add to inventory,
                 Link, or Send BOM), else matched by part number, else by vendor + vendor part number.
                 Its project allocations — directly or inside weldments — give the projects, and active
                 production orders on those projects mark it "in production".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ACTIVE_DONE = {"complete", "completed", "cancelled", "canceled", "shipped", "closed"}
NOT_STARTED = {"not started", "scheduled", "planned", "requested", ""}


# ------------------------------------------------------------------ Daedalus files

def _index_path(lib) -> Path:
    return Path(lib.root) / ".usage.json"


def _load_index(lib) -> dict:
    try:
        return json.loads(_index_path(lib).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_doc_usage(lib, doc, path) -> int:
    """Update the index for a saved document. Returns the number of standard parts in it."""
    counts: dict[str, int] = {}
    linked: dict[str, bool] = {}
    for o in doc.objects:
        sid = (o.std or {}).get("id")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
            linked[sid] = linked.get(sid, False) or bool(o.std.get("linked"))
    key = str(Path(path).resolve())
    idx = _load_index(lib)
    if not counts and key not in idx:
        return 0
    if counts:
        idx[key] = {"name": Path(path).stem, "parts": counts, "linked": linked, "saved": int(time.time()),
                    "by": os.environ.get("USERNAME") or os.environ.get("USER") or ""}
    else:
        idx.pop(key, None)
    p = _index_path(lib)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return len(counts)


def files_using(lib, pid: str) -> list[dict]:
    out = []
    for path, e in _load_index(lib).items():
        n = (e.get("parts") or {}).get(pid)
        if n:
            out.append({"path": path, "name": e.get("name") or Path(path).stem, "count": n,
                        "linked": (e.get("linked") or {}).get(pid, False), "saved": e.get("saved"),
                        "by": e.get("by", ""), "exists": Path(path).exists()})
    return sorted(out, key=lambda x: -(x["saved"] or 0))


# ------------------------------------------------------------------ inventory link

def _low(s):
    return str(s or "").strip().lower()


def inventory_parts_for(inv, info: dict) -> list[dict]:
    """Inventory parts that are this standard part (explicit link first)."""
    sid = info.get("id")
    linked = [p for p in inv.parts if sid and p.get("stdPartId") == sid]
    if linked:
        return linked
    pn = _low(info.get("part_number"))
    if pn:
        hit = [p for p in inv.parts if _low(p.get("partNumber")) == pn]
        if hit:
            return hit
    vpn, vendor = _low(info.get("vendor_part_number")), _low(info.get("vendor"))
    if vpn:
        return [p for p in inv.parts if any(_low(s.get("partNumber")) == vpn and
                                           (not vendor or _low(s.get("name")) == vendor)
                                           for s in inv.suppliers(p))]
    return []


def _production(inv, project_id) -> list[dict]:
    out = []
    for o in inv.productionOrders:
        if o.get("projectId") != project_id:
            continue
        st = _low(o.get("status"))
        if st in ACTIVE_DONE:
            continue
        out.append({"id": o.get("id"), "status": o.get("status") or "Not Started", "qty": o.get("qty"),
                    "due": o.get("dueDate") or "", "fab": [o.get("fabStart"), o.get("fabEnd")],
                    "finish": [o.get("finishStart"), o.get("finishEnd")],
                    "active": st not in NOT_STARTED})
    return sorted(out, key=lambda x: x["due"] or "9999")


def _parents(inv, part_id):
    """(parent weldment, qty per parent) for every weldment that contains this part."""
    for p in inv.parts:
        for c in p.get("components") or []:
            if c.get("partId") == part_id:
                yield p, float(c.get("qty") or 0)


def projects_for(inv, part, _mult=1.0, _via=None, _seen=None) -> list[dict]:
    """Projects using an inventory part, directly or inside weldments (qty multiplied through)."""
    from ..inventory.model import num
    _seen = _seen or set()
    if part["id"] in _seen:
        return []
    _seen = _seen | {part["id"]}
    out = []
    for a in inv.allocations(part):
        pr = inv.project(a.get("projectId"))
        if not pr:
            continue
        qpu = num(a.get("qty")) * _mult
        units = inv.project_qty(pr["id"])
        out.append({"project_id": pr["id"], "project": pr["name"], "archived": bool(pr.get("archived")),
                    "qty_per_unit": qpu, "units": units, "needed": qpu * units, "via": _via,
                    "production": _production(inv, pr["id"])})
    for parent, q in _parents(inv, part["id"]):
        out += projects_for(inv, parent, _mult * q, parent["name"], _seen)
    return out


def std_status(inv, info: dict) -> dict:
    """Inventory side of a standard part: linked parts with stock/bin/orders, projects, production."""
    from ..inventory.model import num
    parts = inventory_parts_for(inv, info)
    rows, projects = [], []
    for p in parts:
        stock = num(p.get("currentStock"))
        on_order = sum(num(o.get("qty")) for o in inv.open_part_orders() if o.get("partId") == p["id"])
        rows.append({"id": p["id"], "name": p["name"], "partNumber": p.get("partNumber") or "",
                     "stock": stock, "unit": p.get("unit") or "ea", "reorderPoint": num(p.get("reorderPoint")),
                     "low": stock <= num(p.get("reorderPoint")) and num(p.get("reorderPoint")) > 0,
                     "location": inv.location_label_for(p.get("locationId")), "on_order": on_order,
                     "linked": p.get("stdPartId") == info.get("id"), "needed": inv.total_needed(p)})
        projects += projects_for(inv, p)
    active = [pr for pr in projects if any(o["active"] for o in pr["production"])]
    scheduled = [pr for pr in projects if pr["production"] and pr not in active]
    stock = sum(r["stock"] for r in rows)
    needed = sum(pr["needed"] for pr in projects if not pr["archived"])
    return {"parts": rows, "projects": projects, "in_production": active, "scheduled": scheduled,
            "stock": stock, "needed": needed, "short": max(0.0, needed - stock),
            "on_order": sum(r["on_order"] for r in rows)}


def _supplier_rows(info):
    rows = [{"name": "", "price": "", "currency": "CAD", "partNumber": "", "website": ""} for _ in range(3)]
    if info.get("vendor"):
        rows[0] = {"name": info["vendor"], "price": info.get("unit_cost") or "", "currency": "CAD",
                   "partNumber": info.get("vendor_part_number") or "", "website": info.get("url") or ""}
    return rows


def add_to_inventory(inv, info: dict) -> dict:
    """Create the inventory part for a standard part (0 in stock), linked."""
    from ..inventory.model import new_part, now_ms
    ex = inventory_parts_for(inv, info)
    if ex:
        return link(inv, ex[0]["id"], info)
    kind = info.get("kind")
    p = new_part(type="manufactured" if kind == "manufactured" else "purchased", name=info["name"],
                 partNumber=info.get("part_number") or "", category=info.get("category") or "",
                 notes=(info.get("description") or "") + (" · " if info.get("description") else "") +
                 "Daedalus standard part", unitCost=info.get("unit_cost") or 0, unitCostCurrency="CAD",
                 drawingUrl=info.get("url") or info.get("path") or "", suppliers=_supplier_rows(info),
                 stdPartId=info.get("id"), updatedAt=now_ms())
    if kind == "manufactured":
        p["suppliers"] = [{} for _ in range(3)]
    inv.parts.append(p)
    return p


def link(inv, part_id: str, info: dict) -> dict:
    from ..inventory.model import now_ms
    p = inv.part(part_id)
    if p is None:
        raise ValueError("No such inventory part")
    for q in inv.parts:                              # one inventory part per standard part link
        if q is not p and q.get("stdPartId") == info.get("id"):
            q.pop("stdPartId", None)
    p["stdPartId"] = info.get("id")
    if not p.get("partNumber") and info.get("part_number"):
        p["partNumber"] = info["part_number"]
    p["updatedAt"] = now_ms()
    return p


def unlink(inv, part_id: str):
    p = inv.part(part_id)
    if p is not None:
        p.pop("stdPartId", None)


def sync_to_inventory(inv, info: dict) -> list[str]:
    """Copy name, part number, category, cost and vendor from the standard part to its linked inventory
    parts. Stock, bins and projects are untouched. Returns the names updated."""
    from ..inventory.model import now_ms
    done = []
    for p in inventory_parts_for(inv, info):
        p["name"] = info["name"]
        if info.get("part_number"):
            p["partNumber"] = info["part_number"]
        if info.get("category"):
            p["category"] = info["category"]
        if info.get("unit_cost"):
            p["unitCost"] = info["unit_cost"]
            p["unitCostCurrency"] = "CAD"
        if info.get("vendor") and p.get("type") != "manufactured":
            sups = list(p.get("suppliers") or [{}, {}, {}])
            i = next((k for k, s in enumerate(sups) if _low((s or {}).get("name")) == _low(info["vendor"])), 0)
            sups[i] = {**(sups[i] or {}), **_supplier_rows(info)[0]}
            p["suppliers"] = sups
        p["stdPartId"] = info.get("id")
        p["updatedAt"] = now_ms()
        done.append(p["name"])
    return done


def summary_line(st: dict) -> str:
    """'30 in stock · 2 projects · 1 in production' for list items."""
    bits = []
    if st["parts"]:
        bits.append(f"{st['stock']:g} in stock")
    if st["projects"]:
        n = len({p["project_id"] for p in st["projects"]})
        bits.append(f"{n} project{'s' if n > 1 else ''}")
    if st["in_production"]:
        bits.append(f"{len({p['project_id'] for p in st['in_production']})} in production")
    return " · ".join(bits)
