"""CAD ⇄ inventory: turn a Daedalus BOM into inventory parts and a project, and check stock for a design."""
from __future__ import annotations

import re
from pathlib import Path

from .model import Inventory, fastener_name, new_part, now_ms, num

# Daedalus library designation → Holocron fastener spec
_KIND = {"SHCS": ("Screw", "Socket Head"), "Hex bolt": ("Bolt", "Hex"), "FHCS": ("Screw", "Flat Head"),
         "Hex nut": ("Nut", "Reg"), "Washer": ("Washer", "Flat")}
_DESIG = re.compile(r'^(SHCS|Hex bolt|FHCS|Hex nut|Washer) (\S+)(?: x ([\d.]+)("?))? \((ISO|ASME)\)$')


def _inch_size(s):
    """'1/4-20' → '1/4"', '#10-24' → '#10'."""
    head = s.split("-")[0]
    return head if head.startswith("#") else f'{head}"'


def _frac(v):
    """1.25 → '1-1/4', 0.5 → '1/2' (eighths/sixteenths)."""
    whole = int(v)
    rem = v - whole
    for den in (2, 4, 8, 16):
        n = round(rem * den)
        if abs(rem - n / den) < 1e-6:
            if n == 0:
                return f"{whole}"
            if n == den:
                return f"{whole + 1}"
            from math import gcd
            g = gcd(n, den)
            f = f"{n // g}/{den // g}"
            return f"{whole}-{f}" if whole else f
    return f"{v:g}"


def spec_from_designation(desig: str) -> dict | None:
    """Fastener spec fields for a Daedalus library part designation, or None."""
    m = _DESIG.match(desig or "")
    if not m:
        return None
    kind, size, length, inch_mark, std = m.groups()
    cat, head = _KIND[kind]
    metric = std == "ISO"
    size_h = size if metric else _inch_size(size)
    standard = "Metric Coarse" if metric else "Imperial UNC"
    L = ""
    if length:
        L = f"{float(length):g}" if metric else _frac(float(length))
    name = fastener_name(size_h, standard, head, "", cat, L)
    return {"type": "fastener", "category": cat, "headStyle": head, "threadStandard": standard, "size": size_h,
            "length": L, "name": name, "unit": "ea"}


def _templates(doc):
    """[(template part dict, qty per assembly, bom row)] for every BOM row."""
    out = []
    path = str(doc.path) if getattr(doc, "path", None) else ""
    for row in doc.bom():
        desig = row["part"]
        spec = spec_from_designation(desig)
        std = row.get("standard")
        if std and not spec:
            kind = std.get("kind")
            t = {"type": "manufactured" if kind == "manufactured" else "purchased",
                 "name": std.get("name") or desig, "partNumber": desig, "category": std.get("category") or "",
                 "notes": f"Daedalus standard part · {row['description']}", "unit": "ea",
                 "stdPartId": std.get("id")}
            if std.get("vendor"):
                t["suppliers"] = [{"name": std["vendor"], "price": row.get("unit_cost") or 0, "currency": "CAD",
                                   "partNumber": "", "website": ""}, *[dict(name="", price=0, currency="CAD",
                                                                          partNumber="", website="")] * 2]
        elif spec:
            t = {**spec, "partNumber": "", "notes": f"From Daedalus: {desig}"}
        elif desig.lower().startswith("ball bearing"):
            t = {"type": "purchased", "name": desig, "category": "Bearing", "partNumber": desig.split()[-1],
                 "notes": "From Daedalus parts library", "unit": "ea"}
        else:
            t = {"type": "manufactured", "name": desig, "partNumber": desig, "category": "",
                 "notes": f"{row['description']} · {row['material']} · {row['unit_mass_kg']:g} kg (Daedalus)",
                 "drawingUrl": row.get("file") or path, "unit": "ea"}
        if row.get("inventory_id"):
            t["inventoryId"] = row["inventory_id"]
        t["unitCost"] = row.get("unit_cost") or 0
        t["unitCostCurrency"] = "CAD"
        out.append((t, row["qty"], row))
    return out


def _cut_members(doc, obj):
    """Cut-list members of one object's Frame features: [(name, qty)]."""
    feats = [f.name for f in doc.features if f.obj == obj and f.TYPE == "frame"]
    rows = {}
    for fn in feats:
        for m in doc.result.cutlists.get(fn, []):
            key = (m["section"], round(m["length"], 1))
            rows[key] = rows.get(key, 0) + 1
    return [(f"{sec} × {L:g} mm", q) for (sec, L), q in sorted(rows.items())]


def match_part(inv: Inventory, t: dict):
    """Existing inventory part for a template: part number, then fastener spec, then name."""
    typ = t.get("type")
    if t.get("inventoryId"):                       # imported from inventory: the very same part
        hit = next((p for p in inv.parts if p.get("id") == t["inventoryId"]), None)
        if hit:
            return hit
    if t.get("stdPartId"):
        hit = next((p for p in inv.parts if p.get("stdPartId") == t["stdPartId"]), None)
        if hit:
            return hit
    pn = (t.get("partNumber") or "").strip().lower()
    if pn:
        hit = next((p for p in inv.parts if (p.get("partNumber") or "").strip().lower() == pn and p.get("type") == typ),
                   None)
        if hit:
            return hit
    if typ == "fastener":
        def norm_len(s):
            s = str(s or "").replace('"', "").replace("mm", "").strip()
            m = re.match(r"^(\d+)(?:[- ](\d+)/(\d+))?$|^(\d+)/(\d+)$|^([\d.]+)$", s)
            if not m:
                return s
            if m.group(1):
                return num(m.group(1)) + (num(m.group(2)) / num(m.group(3)) if m.group(2) else 0)
            if m.group(4):
                return num(m.group(4)) / num(m.group(5))
            return num(m.group(6))
        for p in inv.parts:
            if p.get("type") != "fastener":
                continue
            if (p.get("size") or "").strip().lower() != (t.get("size") or "").lower():
                continue
            if (p.get("category") or "") != t.get("category"):
                continue
            if t.get("headStyle") and p.get("headStyle") and p["headStyle"] != t["headStyle"]:
                continue
            if norm_len(p.get("length")) != norm_len(t.get("length")):
                continue
            return p
    name = (t.get("name") or "").strip().lower()
    return next((p for p in inv.parts if (p.get("name") or "").strip().lower() == name and p.get("type") == typ), None)


def send_bom(inv: Inventory, doc, project: str, units: float | None = None, weldments: bool = True) -> dict:
    """Add the design's parts to the inventory (new ones with 0 stock), link existing ones, and allocate the
    per-assembly quantities to a project (created if needed). Returns a report."""
    if not project.strip():
        raise ValueError("Give the project a name")
    extra = {"totalQtyNeeded": units} if units is not None else {}
    if getattr(doc, "path", None):
        extra["folderPath"] = str(Path(doc.path).parent)
    pid = inv.find_or_create_project(project, **extra)
    added, linked, rows = [], [], []
    for t, qty, row in _templates(doc):
        p = match_part(inv, t)
        if p is None:
            p = new_part(**{k: v for k, v in t.items() if k != "inventoryId"})
            inv.parts.append(p)
            added.append(p["name"])
        else:
            linked.append(p["name"])
            if t.get("stdPartId") and not p.get("stdPartId"):
                p["stdPartId"] = t["stdPartId"]
            if not num(p.get("unitCost")) and t.get("unitCost"):
                p["unitCost"] = t["unitCost"]
        inv.set_allocation(p, pid, qty)
        p["updatedAt"] = now_ms()
        # frames → weldment with its cut members as components
        if weldments and p.get("type") == "manufactured" and len(row["objects"]) >= 1:
            members = _cut_members(doc, row["objects"][0])
            if members:
                comps = []
                for mname, mq in members:
                    mt = {"type": "manufactured", "name": mname, "partNumber": "", "category": "Frame member",
                          "notes": "Cut-list member (Daedalus)", "unit": "ea"}
                    mp = match_part(inv, mt)
                    if mp is None:
                        mp = new_part(**mt)
                        inv.parts.append(mp)
                        added.append(mname)
                    comps.append({"partId": mp["id"], "qty": mq})
                p["isWeldment"] = True
                p["components"] = comps
        rows.append({"part": p["name"], "id": p["id"], "qty_per_unit": qty})
    return {"project": project, "projectId": pid, "added": added, "linked": linked, "rows": rows,
            "units": inv.project_qty(pid)}


def stock_report(inv: Inventory, doc, units: float = 1.0) -> list[dict]:
    """For each BOM row: matching inventory part, stock, location, needed for `units` builds, shortfall,
    open orders."""
    out = []
    for t, qty, row in _templates(doc):
        p = match_part(inv, t)
        need = qty * units
        on_order = sum(num(o.get("qty")) for o in inv.open_part_orders() if p and o.get("partId") == p["id"])
        stock = num(p.get("currentStock")) if p else 0
        out.append({"item": row["item"], "part": row["part"], "qty_per_unit": qty, "needed": need,
                    "inventory_part": p["name"] if p else None, "in_stock": stock if p else None,
                    "location": inv.location_label_for(p.get("locationId")) if p else "",
                    "short": max(0, need - stock), "on_order": on_order,
                    "status": "not in inventory" if p is None else ("short" if need > stock else "ok")})
    return out


def stock_for_designation(inv: Inventory, desig: str):
    """(part, stock, location label) for a library designation, or None."""
    spec = spec_from_designation(desig)
    t = spec or {"type": "purchased", "name": desig, "partNumber": desig.split()[-1] if desig else ""}
    p = match_part(inv, t)
    if p is None:
        return None
    return p, num(p.get("currentStock")), inv.location_label_for(p.get("locationId"))
