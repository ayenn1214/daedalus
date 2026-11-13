"""SolidWorks BOM → DIMS item master, with a review step.

review()  reads the open assembly's structure (every level, quantities per parent), matches each part / sub-
          assembly to DIMS (DIMSId, then part number, then name), and says for each line: linked / matched /
          new / changed (revision, description) — with the DIMS type it would get and, for parts without a number,
          the next free number under the shop structure (C10 machined, C20 sub-assembly, C30 top level, P00
          fasteners, P0x purchased by category).
apply()   creates the new profiles (number, description, revision, type, vendor, cost, material, finish, model
          file), builds each assembly's BOM in DIMS, optionally allocates the parts to a project, and writes
          PartNo / DIMSId back to the SolidWorks files. preview=True returns what would happen and changes nothing.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from .backend import ASSEMBLY, DESC_KEYS, INVENTORY_KEY, LEGACY_INVENTORY_KEYS, PN_KEYS, first_prop

REV_KEYS = ("Revision", "Rev", "REV", "Revision Level")
VENDOR_KEYS = ("Vendor", "Supplier", "Manufacturer", "Mfr")
VENDOR_PN_KEYS = ("VendorNo", "Vendor Part Number", "Vendor Part No", "Supplier Part Number", "Mfg Part Number",
                  "MfrPartNo", "Manufacturer Part Number")
COST_KEYS = ("Cost", "UnitCost", "Unit Cost", "Price")
FINISH_KEYS = ("Finish", "Surface Finish", "Coating", "Surface Treatment")
TYPE_KEYS = ("Type", "Part Type", "Make/Buy", "MakeBuy", "Source")
FASTENER_WORDS = re.compile(r"\b(screw|bolt|nut|washer|shcs|hhcs|bhcs|fhcs|set ?screw|rivet|pin|stud|"
                            r"helicoil|insert|retaining ring|circlip)\b", re.I)


def _base(path) -> str:
    return Path(str(path)).stem


def structure(link) -> dict:
    """{"top": file row, "rows": {file key: row}} — every distinct part / sub-assembly once, with its children
    and quantity per parent (counted under one instance of each parent)."""
    b = link.b
    d = link._active()
    comps = [c for c in b.components() if not c.suppressed]
    key = lambda path, cfg: f"{str(path).lower()}|{cfg or ''}"  # noqa: E731
    inst = {c.name: c for c in comps}
    top_key = key(d.path, d.configuration if d.kind == ASSEMBLY else d.configuration)
    rows = {top_key: _row(d.path, d.configuration, d.properties, getattr(d, "material", ""), True, top=True)}
    first_of: dict = {}                                   # parent file key → the one instance we count under
    for c in comps:
        k = key(c.path, c.configuration)
        if k not in rows:
            rows[k] = _row(c.path, c.configuration, c.properties or {}, c.material or "", c.is_assembly)
        first_of.setdefault(k, c.name)
    for c in comps:
        parent = inst.get(c.parent) if c.parent else None
        pk = key(parent.path, parent.configuration) if parent else top_key
        if parent is not None and first_of.get(pk) != parent.name:
            continue                                      # second copy of a sub-assembly: already counted
        ck = key(c.path, c.configuration)
        ch = rows[pk]["children"]
        ch[ck] = ch.get(ck, 0) + 1
        rows[ck]["parents"].add(pk)
    return {"top": top_key, "rows": rows}


def _row(path, cfg, props, material, is_asm, top=False):
    props = props or {}
    return {"file": str(path), "configuration": cfg or "", "part_no": first_prop(props, PN_KEYS),
            "description": first_prop(props, DESC_KEYS) or _base(path), "revision": first_prop(props, REV_KEYS),
            "vendor": first_prop(props, VENDOR_KEYS), "vendor_pn": first_prop(props, VENDOR_PN_KEYS),
            "cost": first_prop(props, COST_KEYS), "finish": first_prop(props, FINISH_KEYS),
            "type_hint": first_prop(props, TYPE_KEYS), "material": material or "",
            "inventory_id": first_prop(props, (INVENTORY_KEY, *LEGACY_INVENTORY_KEYS)),
            "is_assembly": bool(is_asm), "top": top, "children": {}, "parents": set()}


def _depth(rows, k, seen=()):
    ch = rows[k]["children"]
    if not ch or k in seen:
        return 0
    return 1 + max(_depth(rows, c, seen + (k,)) for c in ch)


def suggest_type(r, inv=None) -> str:
    from ..inventory import mrp as R
    if r["part_no"]:
        pre, _seq = R.split_number(r["part_no"])
        t = R.describe_prefix(inv, pre).get("type") if pre and inv is not None else ""
        if t:
            return t
    if r["is_assembly"] or r["children"]:
        return "manufactured"
    hint = (r["type_hint"] or "").lower()
    if hint in ("buy", "purchased", "purchase", "bought", "vendor"):
        return "purchased"
    if hint in ("make", "made", "manufactured", "fabricated", "machined"):
        return "manufactured"
    text = f"{r['description']} {_base(r['file'])}"
    if FASTENER_WORDS.search(text) or "toolbox" in r["file"].lower():
        return "fastener"
    if r["vendor"] or r["vendor_pn"]:
        return "purchased"
    return "manufactured"


def _match(inv, r):
    if r["inventory_id"]:
        p = inv.part(r["inventory_id"])
        if p:
            return p, "DIMSId"
    pn = (r["part_no"] or "").strip().lower()
    if pn:
        p = next((x for x in inv.parts if (x.get("partNumber") or "").strip().lower() == pn), None)
        if p:
            return p, "part number"
    for nm in {r["description"].strip().lower(), _base(r["file"]).strip().lower()}:
        p = next((x for x in inv.parts if (x.get("name") or "").strip().lower() == nm), None)
        if p:
            return p, "name"
    return None, ""


def _prefix_for(r, typ, line, depth):
    """Shop-structure prefix for a part with no number: C10 / C20 / C30 for made parts (by depth in the tree),
    P00 for fasteners; other purchased parts need their category (P01…) so none is guessed."""
    if typ == "fastener":
        return "P00"
    if typ == "purchased":
        return None
    if not line:
        return None
    if r["top"]:
        return f"{line}30"
    return f"{line}20" if (r["is_assembly"] or r["children"]) else f"{line}10"


def review(link, inv=None) -> dict:
    """Every line of the open assembly against DIMS (nothing is changed)."""
    from ..inventory import mrp as R
    inv = inv or link.store.inv
    s = structure(link)
    rows = s["rows"]
    top = rows[s["top"]]
    m = re.match(r"^([A-OQ-Z])\d0-", (top["part_no"] or "").upper())
    lines = list(R.product_lines(inv))
    line = m.group(1) if m else (lines[0] if len(lines) == 1 else None)
    taken: dict = {}
    out = []
    order = sorted(rows, key=lambda k: (not rows[k]["top"], -_depth(rows, k), rows[k]["part_no"] or "~",
                                        rows[k]["description"].lower()))
    for i, k in enumerate(order, start=1):
        r = rows[k]
        p, how = _match(inv, r)
        typ = (p or {}).get("type") or suggest_type(r, inv)
        status, changes = ("linked" if how == "DIMSId" else "matched") if p else "new", []
        if p:
            if r["revision"] and (p.get("revision") or "").upper() != r["revision"].upper():
                changes.append(f"revision {p.get('revision') or '—'} → {r['revision']}")
            if r["part_no"] and not p.get("partNumber"):
                changes.append(f"part number — → {r['part_no']}")
            if r["description"] and p.get("description") and p["description"] != r["description"]:
                changes.append("description differs")
            if changes:
                status = "changed"
        suggest = ""
        if not r["part_no"] and not (p and p.get("partNumber")):
            pre = _prefix_for(r, typ, line, _depth(rows, k))
            if pre:
                n = taken.get(pre) or R.next_numbers(inv, pre, 1)[0]
                _pre, seq = R.split_number(n)
                suggest = n
                taken[pre] = R.fmt_number(pre, seq + 1)
        qty_total = _qty_in_top(rows, s["top"], k)
        out.append({"item": i, "key": k, "part_no": r["part_no"] or (p or {}).get("partNumber") or "",
                    "suggested_no": suggest, "description": r["description"], "revision": r["revision"],
                    "type": typ, "kind": ("assembly" if (r["is_assembly"] or r["top"]) else "part"),
                    "status": status, "matched_by": how, "dims_id": (p or {}).get("id"),
                    "dims_name": (p or {}).get("name"), "changes": changes, "vendor": r["vendor"],
                    "vendor_pn": r["vendor_pn"], "cost": r["cost"], "material": r["material"], "finish": r["finish"],
                    "qty_per_top": qty_total, "children": len(r["children"]), "file": r["file"],
                    "stock": (p or {}).get("currentStock")})
    return {"assembly": top["description"], "assembly_no": top["part_no"], "product_line": line, "lines": out,
            "counts": {s_: sum(1 for x in out if x["status"] == s_) for s_ in ("new", "matched", "linked",
                                                                              "changed")},
            "_structure": s}


def _qty_in_top(rows, start, k, mult=1.0, seen=()):
    """How many of k go into one of `start` (through every sub-assembly level)."""
    if k == start:
        return 1.0
    total = 0.0
    for ck, q in rows[start]["children"].items():
        if ck == k:
            total += q * mult
        elif rows[ck]["children"] and ck not in seen:
            total += _qty_in_top(rows, ck, k, q * mult, seen + (start,))
    return total


def apply(link, items=None, project: str | None = None, units: float | None = None, numbers: bool = True,
          update_existing: bool = False, write_back: bool = True, preview: bool = False,
          overrides: dict | None = None) -> dict:
    """Add / link the reviewed lines (items = item numbers; default every new or changed line plus the
    assemblies needed for the BOM). overrides = {item: {"part_no", "type", "description"}}. Returns a report."""
    from ..inventory import model as M
    from ..inventory import mrp as R
    store = link.store
    real = store.inv
    inv = copy.deepcopy(real) if preview else real
    rv = review(link, inv)
    rows = rv["_structure"]["rows"]
    pick = set(items) if items else {x["item"] for x in rv["lines"] if x["status"] in ("new", "changed")
                                     or x["kind"] == "assembly"}
    overrides = {int(k): v for k, v in (overrides or {}).items()}
    by_key, created, linked, updated, numbered = {}, [], [], [], []
    for x in rv["lines"]:
        r = rows[x["key"]]
        o = overrides.get(x["item"], {})
        p = inv.part(x["dims_id"]) if x["dims_id"] else None
        if p is None and x["item"] not in pick:
            continue
        pn = (o.get("part_no") or x["part_no"] or "").strip().upper()
        if not pn and numbers and o.get("part_no") is None and x["suggested_no"]:
            pre = R.split_number(x["suggested_no"])[0]
            note = f"{x['description']} (from SolidWorks)"
            if preview:
                pn = x["suggested_no"]                      # what it would get (the review numbers in sequence)
            elif hasattr(store, "reserve_part_numbers"):    # team server: the database hands it out
                pn = store.reserve_part_numbers(pre, 1, None, note)[0]["id"]
            else:
                pn = R.reserve(inv, pre, 1, note=note, by=inv.userName or "SolidWorks")[0]["id"]
            numbered.append(pn)
        typ = o.get("type") or x["type"]
        if p is None:
            fields = {"name": o.get("description") or x["description"], "description": x["description"],
                      "partNumber": pn, "type": typ, "revision": (x["revision"] or "").upper(), "cadFile": x["file"],
                      "createdBy": inv.userName or "SolidWorks", "notes": "Added from SolidWorks"}
            if x["vendor"] or x["vendor_pn"] or x["cost"]:
                fields["suppliers"] = [{"name": x["vendor"], "partNumber": x["vendor_pn"],
                                        "price": M.num(str(x["cost"]).replace("$", "")) if x["cost"] else "",
                                        "currency": "CAD"}, {}, {}]
                if x["vendor"]:
                    inv.ensure_vendor(x["vendor"])
            if x["cost"]:
                fields["unitCost"] = M.num(str(x["cost"]).replace("$", ""))
            if typ == "manufactured" and x["material"]:
                fields["material"] = {"grade": x["material"]}
            if x["finish"]:
                fields["finish"] = {"process": x["finish"]}
            p = M.new_part(**fields)
            inv.parts.append(p)
            created.append(p)
        else:
            linked.append(p)
            if update_existing or x["item"] in pick:
                ch = False
                if x["revision"] and (p.get("revision") or "").upper() != x["revision"].upper():
                    p["revision"] = x["revision"].upper()
                    ch = True
                if pn and not p.get("partNumber"):
                    p["partNumber"] = pn
                    ch = True
                if not p.get("cadFile"):
                    p["cadFile"] = x["file"]
                    ch = True
                if update_existing and x["description"] and p.get("description") != x["description"]:
                    p["description"] = x["description"]
                    ch = True
                if ch:
                    p["updatedAt"] = M.now_ms()
                    updated.append(p)
        if pn and p.get("partNumber", "").upper() == pn:
            try:
                R.assign(inv, pn, p)
            except ValueError:
                pass
        by_key[x["key"]] = p
    # BOMs: every assembly line that's in DIMS gets its children (those in DIMS) as components
    boms = 0
    for k, p in by_key.items():
        r = rows[k]
        if not r["children"]:
            continue
        lines = [(by_key[ck]["id"], q) for ck, q in r["children"].items() if ck in by_key]
        if lines:
            kind = "weldment" if p.get("isWeldment") else "assembly"
            R.set_bom(inv, p["id"], lines, kind)
            boms += 1
    alloc = 0
    if project:
        pid = inv.find_or_create_project(project, **({"totalQtyNeeded": units} if units else {}))
        for x in rv["lines"]:
            if x["kind"] == "assembly":
                continue
            p = by_key.get(x["key"])
            if p is not None and x["qty_per_top"]:
                inv.set_allocation(p, pid, x["qty_per_top"])
                alloc += 1
    report = {"preview": preview, "created": [_brief(p) for p in created], "linked": len(linked),
              "updated": [_brief(p) for p in updated], "numbers": numbered, "boms": boms,
              "allocated": alloc, "project": project or ""}
    if preview:
        return report
    store.save()
    if write_back:
        ok, failed = 0, []
        for k, p in by_key.items():
            props = {INVENTORY_KEY: p["id"]}
            r = rows[k]
            if p.get("partNumber") and not r["part_no"]:
                props["PartNo"] = p["partNumber"]
            try:
                link.b.set_properties(props, path=r["file"])
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{Path(r['file']).name}: {e}")
        report.update(written_back=ok, write_failed=failed)
    return report


def _brief(p):
    return {"id": p["id"], "partNumber": p.get("partNumber") or "", "name": p["name"], "type": p.get("type"),
            "revision": p.get("revision") or ""}
