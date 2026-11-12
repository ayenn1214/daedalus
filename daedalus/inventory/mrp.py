"""DIMS MRP-lite (Qt-free): part-number registry, item profiles (vendors, lead times, where used), multi-level
BOMs with raw material and finishing, job orders split into one purchase draft per vendor, kits / pick lists,
material needs for bulk buying, and the time-phased view (incoming orders, projected stock, order log).

Records are plain dicts inside the Inventory, like everything else in DIMS:
- inv.partNumbers: {"id": "M10-0001", "prefix": "M10", "seq": 1, "status": "reserved"|"assigned"|"released",
  "projectId", "partId", "note", "createdBy", "createdAt"}
- part["components"]: [{"partId", "qty"}] with part["isWeldment"] (welded in, not stocked separately) or
  part["isAssembly"] (kit of separately stocked parts)
- part["material"]: {"shape", "grade", "size", "qty", "unit", "vendor", "stockLength"} — raw stock per part
- part["finish"]: {"process", "spec", "vendor"}
- order["events"]: [{"what": ordered|received|binned|cancelled, "at": ms, "by"}]
"""
from __future__ import annotations

import datetime as dt
import re

from .model import TYPE_LABEL, Inventory, add_days, days_between, now_ms, num, parse_date, today, uid

PN_RE = re.compile(r"^([A-Z0-9]{1,8})-(\d{3,})$")
DIGITS = 4
MATERIAL_SHAPES = ["Flat bar", "Round bar", "Square bar", "Hex bar", "Plate", "Sheet", "Square tube (HSS)",
                   "Rect tube (HSS)", "Round tube / pipe", "Angle", "Channel", "Beam", "Other"]
MATERIAL_GRADES = ["HRS (A36)", "CRS (1018)", "A500 Gr B/C", "304 SS", "316 SS", "6061-T6 Al", "5052-H32 Al",
                   "4140", "Galvanized", "Other"]
MATERIAL_UNITS = ["in", "ft", "mm", "m", "sq in", "sq ft", "lb", "ea"]
FINISHES = ["Powder coat", "Paint", "Zinc plate", "Hot-dip galvanize", "Anodize", "Black oxide", "Passivate",
            "Other"]


# ================================================================== part numbers
def norm_prefix(p: str) -> str:
    p = re.sub(r"[^A-Za-z0-9]", "", str(p or "")).upper()
    if not p:
        raise ValueError("Enter a prefix, e.g. M10")
    if len(p) > 8:
        raise ValueError("Prefix is at most 8 letters / digits")
    return p


# Shop numbering (Part Number Structure): product-line letter + level for made parts (C30 top assembly, C20
# sub-assembly / weldment, C10 machined part), P + category for purchased parts, 4-digit sequence; purchased raw
# material is numeric (1000-001, 3-digit sequence).
LEVELS = {"3": ("Top-level assembly", "assembly"), "2": ("Sub-assembly / weldment", "assembly"),
          "1": ("Machined / fabricated part", "")}
DEFAULT_CATEGORIES = {"P00": "Fasteners (bolts, nuts, washers)", "P01": "Transmission (bearings, bushings, motors, "
                      "reducers)", "P02": "Hydraulics", "P03": "Pneumatics", "P04": "Electrical"}
DEFAULT_LINES = {"C": "Clamping"}
_LINE_RE = re.compile(r"^([A-Z])([1-9])0$")


def digits_for(prefix: str) -> int:
    return 3 if prefix.isdigit() else DIGITS


def fmt_number(prefix: str, seq: int) -> str:
    return f"{prefix}-{seq:0{digits_for(prefix)}d}"


def product_lines(inv) -> dict:
    return dict((inv.syncMeta or {}).get("pnProductLines") or DEFAULT_LINES)


def describe_prefix(inv, prefix: str) -> dict:
    """{"label", "type", "bom", "level"} for a prefix: the team's table first, then the shop structure."""
    prefix = (prefix or "").upper()
    own = prefixes(inv).get(prefix) or {}
    out = {"label": "", "type": "", "bom": "", "level": None}
    m = _LINE_RE.match(prefix)
    if m:
        line = product_lines(inv).get(m.group(1), f"Product line {m.group(1)}")
        lab, bom = LEVELS.get(m.group(2), (f"Level {m.group(2)}0", "assembly"))
        out.update(label=f"{line} · {lab}", type="manufactured", bom=bom, level=int(m.group(2)))
    elif re.match(r"^P\d\d$", prefix):
        cats = {**DEFAULT_CATEGORIES, **{k: v.get("label", "") for k, v in prefixes(inv).items() if v.get("label")}}
        out.update(label=cats.get(prefix, f"Purchased category {prefix[1:]}"),
                   type="fastener" if prefix == "P00" else "purchased")
    elif prefix.isdigit():
        out.update(label="Purchased material", type="purchased")
    out.update({k: v for k, v in own.items() if v})
    return out


def is_material(p) -> bool:
    """Numbered raw material (####-###) or a part flagged as material."""
    pre, _s = split_number(p.get("partNumber") or "")
    return bool(p.get("isMaterial")) or bool(pre and pre.isdigit())


def level_of(pn: str):
    pre, _seq = split_number(pn)
    m = _LINE_RE.match(pre or "")
    return int(m.group(2)) if m else (0 if pre else None)


def level_warning(parent_pn: str, child_pn: str) -> str:
    """A child should sit at a lower level than its parent (C30 > C20 > C10; purchased = 0)."""
    a, b = level_of(parent_pn), level_of(child_pn)
    if a and b is not None and b >= a:
        return f"{child_pn} is level {b}0 — not below its parent {parent_pn} (level {a}0)"
    return ""


def split_number(pn: str):
    m = PN_RE.match(str(pn or "").strip().upper())
    return (m.group(1), int(m.group(2))) if m else (None, None)


def used_seqs(inv: Inventory, prefix: str) -> set:
    """Every sequence number taken for a prefix: the registry (incl. released — never reused) and part numbers
    already on parts."""
    out = set()
    for r in inv.partNumbers or []:
        if r.get("prefix") == prefix:
            out.add(int(num(r.get("seq"))))
    for p in inv.parts:
        pre, seq = split_number(p.get("partNumber"))
        if pre == prefix:
            out.add(seq)
    return out


def next_numbers(inv: Inventory, prefix: str, n: int = 1) -> list[str]:
    """The next n numbers after the highest one in use (gaps are not refilled)."""
    prefix = norm_prefix(prefix)
    top = max(used_seqs(inv, prefix) | {0})
    return [fmt_number(prefix, top + i) for i in range(1, max(1, int(n)) + 1)]


def reserve(inv: Inventory, prefix: str, n: int = 1, project_id=None, note: str = "", by: str = "") -> list[dict]:
    """Reserve n numbers locally (file store; the team server does this in the database so two people can't get
    the same number)."""
    out = []
    for pn in next_numbers(inv, prefix, n):
        pre, seq = split_number(pn)
        r = {"id": pn, "prefix": pre, "seq": seq, "status": "reserved", "projectId": project_id, "partId": None,
             "note": note, "createdBy": by or inv.userName or "unknown", "createdAt": now_ms()}
        inv.partNumbers.append(r)
        out.append(r)
    return out


def number_record(inv: Inventory, pn: str):
    return next((r for r in inv.partNumbers or [] if r.get("id") == str(pn).strip().upper()), None)


def assign(inv: Inventory, pn: str, part) -> dict:
    """Give a part a registered number (registering it first if it isn't)."""
    pn = str(pn).strip().upper()
    other = next((p for p in inv.parts if p is not part and (p.get("partNumber") or "").upper() == pn), None)
    if other:
        raise ValueError(f"{pn} is already used by {other['name']}")
    r = number_record(inv, pn)
    if r is None:
        pre, seq = split_number(pn)
        if pre is None:
            raise ValueError(f"{pn} isn't a PREFIX-#### number")
        r = {"id": pn, "prefix": pre, "seq": seq, "projectId": None, "note": "", "createdBy": inv.userName or "unknown",
             "createdAt": now_ms()}
        inv.partNumbers.append(r)
    r.update(status="assigned", partId=part["id"], assignedAt=now_ms(), assignedBy=inv.userName or "unknown")
    part["partNumber"] = pn
    part["updatedAt"] = now_ms()
    return r


def release(inv: Inventory, pn: str):
    r = number_record(inv, pn)
    if r is None:
        raise ValueError(f"{pn} isn't in the registry")
    if r.get("status") == "assigned" and inv.part(r.get("partId")):
        raise ValueError(f"{pn} is on {inv.part(r['partId'])['name']} — change that part's number first")
    r.update(status="released", releasedAt=now_ms(), releasedBy=inv.userName or "unknown")
    return r


def sync_assignments(inv: Inventory):
    """Registry status follows the parts: a reserved number now on a part becomes assigned."""
    by_pn = {(p.get("partNumber") or "").upper(): p for p in inv.parts if p.get("partNumber")}
    changed = []
    for r in inv.partNumbers or []:
        p = by_pn.get(r["id"])
        if p and (r.get("status") != "assigned" or r.get("partId") != p["id"]):
            r.update(status="assigned", partId=p["id"])
            changed.append(r)
    return changed


def registry_rows(inv: Inventory, prefix=None, status=None, query="", by=None):
    """Every number in use: the registry plus numbers already on parts that were never registered."""
    rows = []
    seen = set()
    for r in inv.partNumbers or []:
        seen.add(r["id"])
        p = inv.part(r.get("partId")) if r.get("partId") else None
        rows.append({"Number": r["id"], "Prefix": r.get("prefix"), "Status": r.get("status"),
                     "Part": p["name"] if p else "", "Description": (p or {}).get("description") or r.get("note") or "",
                     "Project": inv.project_name(r.get("projectId")) or "", "Created By": r.get("createdBy") or "",
                     "Created": _d(r.get("createdAt")), "partId": r.get("partId")})
    for p in inv.parts:
        pre, _seq = split_number(p.get("partNumber"))
        if pre and p["partNumber"].upper() not in seen:
            rows.append({"Number": p["partNumber"].upper(), "Prefix": pre, "Status": "in use (unregistered)",
                         "Part": p["name"], "Description": p.get("description") or "", "Project": ", ".join(
                             inv.project_chips(p)), "Created By": p.get("createdBy") or "",
                         "Created": _d(p.get("createdAt")), "partId": p["id"]})
    if prefix:
        rows = [r for r in rows if r["Prefix"] == norm_prefix(prefix)]
    if status:
        rows = [r for r in rows if r["Status"].startswith(status)]
    if by:
        rows = [r for r in rows if by.lower() in (r["Created By"] or "").lower()]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in " ".join(str(v) for v in r.values()).lower()]
    return sorted(rows, key=lambda r: (r["Prefix"] or "", r["Number"]))


def prefixes(inv: Inventory) -> dict:
    """{prefix: {"label", "type"}} — shared with the team (stored with the data)."""
    return dict((inv.syncMeta or {}).get("pnPrefixes") or {})


def set_prefixes(inv: Inventory, table: dict):
    inv.syncMeta = {**(inv.syncMeta or {}), "pnPrefixes": table}


def all_prefixes(inv: Inventory) -> list[str]:
    s = set(prefixes(inv)) | set(DEFAULT_CATEGORIES) | {r.get("prefix") for r in inv.partNumbers or [] if r.get("prefix")}
    s |= {f"{line}{lvl}0" for line in product_lines(inv) for lvl in "123"}
    s |= {split_number(p.get("partNumber"))[0] for p in inv.parts} - {None}
    return sorted(s - {None})


def _d(ms):
    try:
        return dt.datetime.fromtimestamp(num(ms) / 1000).strftime("%Y-%m-%d") if ms else ""
    except (OverflowError, OSError, ValueError):
        return ""


# ================================================================== BOM
def has_bom(p) -> bool:
    return bool(p.get("components")) and bool(p.get("isWeldment") or p.get("isAssembly"))


def bom_kind(p) -> str:
    return "weldment" if p.get("isWeldment") else ("assembly" if p.get("isAssembly") else "")


def explode(inv: Inventory, pid, qty: float, net: bool = False, _seen=None, _avail=None):
    """Requirements for building qty of a part, all levels. Returns {partId: {"gross", "net", "level", "parent"}}.
    net=True takes stock on hand at each level first (stock is used once across the whole explosion); a
    weldment's components are inside it, so its stock covers them."""
    _seen = _seen or set()
    _avail = _avail if _avail is not None else {p["id"]: max(0.0, num(p.get("currentStock"))) for p in inv.parts}
    out: dict = {}

    def add(part_id, q, level, parent):
        p = inv.part(part_id)
        if p is None or q <= 0:
            return
        if part_id in _seen and level > 0 and inv.contains_recursive(part_id, part_id):
            return
        need = q
        if net:
            take = min(_avail.get(part_id, 0.0), q)
            _avail[part_id] = _avail.get(part_id, 0.0) - take
            need = q - take
        e = out.setdefault(part_id, {"gross": 0.0, "net": 0.0, "level": level, "parents": set()})
        e["gross"] += q
        e["net"] += need
        e["level"] = min(e["level"], level)
        if parent:
            e["parents"].add(parent)
        if has_bom(p) and need > 0 and level < 25:
            for c in p.get("components") or []:
                add(c.get("partId"), need * num(c.get("qty"), 1), level + 1, part_id)
    add(pid, float(qty), 0, None)
    return out


def bom_cost(inv: Inventory, pid, _depth=0) -> float:
    """Rolled-up unit cost: a part with a BOM costs its components (plus its own unit cost as the labour /
    fabrication add-on); a part without one, its unit cost."""
    p = inv.part(pid)
    if p is None or _depth > 25:
        return 0.0
    own = inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
    if not has_bom(p):
        return own
    return own + sum(num(c.get("qty"), 1) * bom_cost(inv, c.get("partId"), _depth + 1) for c in p["components"])


def bom_rows(inv: Inventory, pid, levels=1, _lvl=1, _mult=1.0):
    """Indented BOM lines below a part (levels deep; 0 = all)."""
    p = inv.part(pid)
    rows = []
    for c in (p or {}).get("components") or []:
        cp = inv.part(c.get("partId"))
        if cp is None:
            continue
        q = num(c.get("qty"), 1)
        rows.append({"Level": _lvl, "Part Number": cp.get("partNumber") or "", "Part": cp["name"],
                     "Type": TYPE_LABEL.get(cp.get("type"), ""), "Qty / Assy": q, "Qty Total": q * _mult,
                     "On Hand": num(cp.get("currentStock")), "Unit Cost": bom_cost(inv, cp["id"]),
                     "Vendor": primary_vendor(inv, cp), "Bin": inv.location_label_for(cp.get("locationId")),
                     "Kind": bom_kind(cp), "id": cp["id"]})
        if has_bom(cp) and (levels == 0 or _lvl < levels):
            rows += bom_rows(inv, cp["id"], levels, _lvl + 1, _mult * q)
    return rows


def set_bom(inv: Inventory, pid, lines: list, kind: str = "assembly"):
    """Replace a part's BOM: lines = [(partId, qty)]. Refuses loops (a part inside itself)."""
    p = inv.part(pid)
    if p is None:
        raise ValueError("No such part")
    comps = []
    for cid, q in lines:
        if cid == pid or inv.contains_recursive(cid, pid):
            raise ValueError(f"{(inv.part(cid) or {}).get('name', cid)} contains {p['name']} — that would be a loop")
        if inv.part(cid) is None:
            raise ValueError(f"No part {cid}")
        comps.append({"partId": cid, "qty": num(q, 1)})
    p["components"] = comps
    p["isWeldment"] = kind == "weldment" and bool(comps)
    p["isAssembly"] = kind != "weldment" and bool(comps)
    p["updatedAt"] = now_ms()
    return p


# ================================================================== where used
def parents(inv: Inventory, pid):
    return [(p, num(c.get("qty"), 1)) for p in inv.parts for c in p.get("components") or [] if c.get("partId") == pid]


def where_used(inv: Inventory, pid) -> dict:
    """Direct parents, the top-level assemblies it ends up in, and every project that needs it (directly or
    through an assembly). shared = used by more than one parent / project."""
    direct = parents(inv, pid)
    tops, projects, seen = {}, {}, set()

    def up(part_id, mult, depth):
        if part_id in seen or depth > 25:
            return
        seen.add(part_id)
        ps = parents(inv, part_id)
        p = inv.part(part_id)
        for a in inv.allocations(p or {}):
            name = inv.project_name(a.get("projectId"))
            if name:
                projects[name] = projects.get(name, 0.0) + num(a.get("qty")) * mult
        if not ps and part_id != pid:
            tops[p["name"]] = tops.get(p["name"], 0.0) + mult
        for par, q in ps:
            up(par["id"], mult * q, depth + 1)
        seen.discard(part_id)
    up(pid, 1.0, 0)
    users = {par["id"] for par, _q in direct} | set(projects)
    return {"parents": [{"Part": par["name"], "Part Number": par.get("partNumber") or "", "Qty": q,
                         "Kind": bom_kind(par)} for par, q in direct],
            "top_assemblies": [{"Assembly": k, "Qty per": v} for k, v in sorted(tops.items())],
            "projects": [{"Project": k, "Qty per unit": v} for k, v in sorted(projects.items())],
            "shared": len(users) > 1}


def shared_parts(inv: Inventory):
    out = []
    for p in inv.parts:
        w = where_used(inv, p["id"])
        if w["shared"]:
            out.append({"Part": p["name"], "Part Number": p.get("partNumber") or "",
                        "Used In": ", ".join([x["Part"] for x in w["parents"]] + [x["Project"] for x in w["projects"]]),
                        "id": p["id"]})
    return sorted(out, key=lambda r: r["Part"].lower())


# ================================================================== vendors, lead times, prices
def primary_vendor(inv: Inventory, p) -> str:
    sups = inv.suppliers(p)
    return sups[0]["name"] if sups else ""


def vendor_record(inv: Inventory, name):
    n = (name or "").strip().lower()
    return next((v for v in inv.vendors if v["name"].strip().lower() == n), None) if n else None


def lead_stats(inv: Inventory, pid, vendor: str | None = None) -> dict:
    """Last and average order→received days from received part orders (optionally one vendor's)."""
    rec = [o for o in inv.partOrders if o.get("partId") == pid and o.get("status") in ("Received", "Binned")
           and o.get("orderDate") and o.get("receivedDate")
           and (not vendor or (o.get("vendorName") or "").lower() == vendor.lower())]
    rec.sort(key=lambda o: o["receivedDate"])
    days = [d for d in (days_between(o["orderDate"], o["receivedDate"]) for o in rec) if d is not None]
    return {"last": days[-1] if days else None, "avg": round(sum(days) / len(days), 1) if days else None,
            "orders": len(days)}


def vendor_options(inv: Inventory, p) -> list[dict]:
    """Primary / secondary / alternate supplier with price (CAD), vendor part #, quoted and actual lead times."""
    out = []
    for i, s in enumerate(inv.suppliers(p)):
        ls = lead_stats(inv, p["id"], s["name"])
        rng = inv.lead_time_range(s["name"])
        out.append({"Role": ["Primary", "Secondary", "Alternate"][min(i, 2)], "Vendor": s["name"],
                    "Vendor Part #": s.get("partNumber") or "", "Price": s.get("price") if s.get("price") not in
                    ("", None) else None, "Currency": s.get("currency") or "CAD",
                    "Price CAD": inv.to_cad(s["price"], s.get("currency")) if s.get("price") not in ("", None) else None,
                    "Quoted Lead": s.get("leadDays") or (Inventory.lead_label(rng) if rng else ""),
                    "Last Lead (days)": ls["last"], "Avg Lead (days)": ls["avg"], "Price Date": s.get("priceDate") or "",
                    "Website": s.get("website") or ""})
    return out


def order_history(inv: Inventory, pid):
    rows = []
    for o in sorted([o for o in inv.partOrders if o.get("partId") == pid], key=lambda o: o.get("orderDate") or "",
                    reverse=True):
        rows.append({"Ordered": o.get("orderDate") or "", "Qty": num(o.get("qty")), "Vendor": o.get("vendorName") or "",
                     "PO": o.get("poNumber") or "", "Expected": o.get("expectedDate") or "",
                     "Received": o.get("receivedDate") or "", "Status": o.get("status") or "",
                     "For": inv.project_name(o.get("projectId")) or "",
                     "Lead (days)": days_between(o.get("orderDate"), o.get("receivedDate"))})
    return rows


VENDOR_FIELDS = {"name": ("vendor", "company", "name", "supplier", "vendor name", "company name"),
                 "category": ("category", "type", "category description"),
                 "status": ("status", "vendor status"),
                 "contactName": ("contact", "contact name", "attention", "contact [1]", "contact 1", "primary contact"),
                 "contactName2": ("contact [2]", "contact 2", "secondary contact"),
                 "phone": ("phone", "telephone", "tel", "phone number", "phone [1]", "phone 1"),
                 "phone2": ("phone [2]", "phone 2", "cell", "mobile"),
                 "email": ("email", "e-mail", "email address", "email [1]"),
                 "website": ("website", "web", "url", "site"),
                 "address": ("address", "street", "address 1", "address line 1", "ship from address"),
                 "address2": ("address 2", "address line 2", "suite"), "city": ("city", "town", "ship from city"),
                 "area": ("location", "area", "region"),
                 "province": ("province", "state", "prov", "province/state", "state/province"),
                 "postalCode": ("postal code", "postal", "zip", "zip code", "postcode"), "country": ("country",),
                 "accountNumber": ("account", "account #", "account number", "acct", "customer #"),
                 "terms": ("terms", "payment terms"), "notes": ("notes", "comments"),
                 "leadTimeMinDays": ("lead time min", "min lead", "lead min"),
                 "leadTimeMaxDays": ("lead time max", "max lead", "lead max", "lead time", "lead time (days)")}


_CO_WORDS = {"ltd", "ltd.", "inc", "inc.", "lp", "corp", "corporation", "co", "co.", "llc", "the", "&", "and",
             "limited", "company"}


def _same_company(inv: Inventory, name: str):
    """An existing vendor that's the same company under a shorter name ('Sabre' ↔ 'SABRE INDUSTRIAL SUPPLIES',
    'IOS CNC' ↔ 'IOS ENTERPRISES LTD'): same first word, and that word isn't generic."""
    def words(t):
        return [w for w in re.findall(r"[a-z0-9&]+", t.lower()) if w not in _CO_WORDS]
    w = words(name)
    if not w:
        return None
    hits = []
    for v in inv.vendors:
        vw = words(v["name"])
        if vw and (w[:len(vw)] == vw or vw[:len(w)] == w or (len(w[0]) >= 3 and vw[0] == w[0])):
            hits.append(v)                       # '3 Amigos' ⊂ '3 AMIGOS MANUFACTURING'; 'IOS CNC' ~ 'IOS …'
    return hits[0] if len(hits) == 1 else None


def import_vendors(inv: Inventory, rows: list[dict], overwrite: bool = False, report: list | None = None):
    """Vendor directory from a spreadsheet (column names matched loosely). New vendors are added; an existing one
    (same name, or the same company under a shorter name) gets the sheet's empty-here fields filled in. Contact
    details that differ are kept as they are unless overwrite=True — each such difference goes to `report` as
    (vendor, field, kept, sheet). Category and status always follow the sheet. Returns (added, updated)."""
    def pick(r, names):
        low = {str(k).strip().lower(): v for k, v in r.items()}
        for n in names:
            if low.get(n) not in (None, ""):
                return low[n]
        return None
    added = updated = 0
    for r in rows:
        name = pick(r, VENDOR_FIELDS["name"])
        if not name or not str(name).strip():
            continue
        name = str(name).strip()
        v = vendor_record(inv, name) or _same_company(inv, name)
        was_new = v is None
        if v is None:
            v = inv.ensure_vendor(name)
        elif v["name"].strip().lower() != name.lower():
            v["legalName"] = name                # keep the short name parts already use; record the full one
        for key, names in VENDOR_FIELDS.items():
            val = pick(r, names)
            if val is None or key == "name":
                continue
            if key in ("leadTimeMinDays", "leadTimeMaxDays"):
                val = int(num(val)) if num(val) else None
            val = str(val).strip() if isinstance(val, str) else val
            cur = v.get(key)
            if was_new or overwrite or key in ("category", "status") or cur in (None, "", 0) or \
                    str(cur).strip().lower() == str(val).strip().lower():
                v[key] = val
            elif report is not None:
                report.append((v["name"], key, cur, val))
        added += was_new
        updated += not was_new
    return added, updated


PRICE_FIELDS = {"pn": ("part number", "part no", "part no.", "part #", "pn", "item", "item number", "our part #"),
                "vendor": ("vendor", "supplier", "vendor name"),
                "vpn": ("vendor part #", "vendor part number", "supplier part #", "vendor pn", "mfr part #"),
                "price": ("price", "cost", "unit price", "unit cost"), "currency": ("currency", "cur"),
                "lead": ("lead", "lead time", "lead days", "lead time (days)"), "date": ("date", "price date",
                                                                                          "quoted", "as of")}


def import_prices(inv: Inventory, rows: list[dict]) -> dict:
    """Vendor prices (e.g. the pricing Google Sheet saved as .xlsx / .csv): matched to parts by part number, written
    to that vendor's supplier slot (added if the part has a free slot)."""
    def pick(r, names):
        low = {str(k).strip().lower(): v for k, v in r.items()}
        for n in names:
            if low.get(n) not in (None, ""):
                return low[n]
        return None
    by_pn = {(p.get("partNumber") or "").strip().upper(): p for p in inv.parts if p.get("partNumber")}
    updated, unmatched, full = 0, [], []
    for r in rows:
        pn = str(pick(r, PRICE_FIELDS["pn"]) or "").strip().upper()
        vend = str(pick(r, PRICE_FIELDS["vendor"]) or "").strip()
        if not pn or not vend:
            continue
        p = by_pn.get(pn)
        if p is None:
            unmatched.append(pn)
            continue
        sups = [dict(s or {}) for s in (p.get("suppliers") or [])] + [{}, {}, {}]
        sups = sups[:max(3, len([s for s in sups if s.get("name")]))]
        slot = next((s for s in sups if (s.get("name") or "").lower() == vend.lower()), None) or \
            next((s for s in sups if not s.get("name")), None)
        if slot is None:
            full.append(pn)
            continue
        slot["name"] = vend
        price = pick(r, PRICE_FIELDS["price"])
        if price not in (None, ""):
            slot["price"] = num(str(price).replace("$", "").replace(",", ""))
        for key, f in (("partNumber", "vpn"), ("currency", "currency"), ("leadDays", "lead"), ("priceDate", "date")):
            val = pick(r, PRICE_FIELDS[f])
            if val not in (None, ""):
                slot[key] = str(val).strip().upper() if key == "currency" else (
                    int(num(val)) if key == "leadDays" and num(val) else str(val)[:10] if key == "priceDate"
                    else str(val).strip())
        slot.setdefault("currency", "CAD")
        p["suppliers"] = sups
        p["updatedAt"] = now_ms()
        inv.ensure_vendor(vend)
        updated += 1
    return {"updated": updated, "unmatched": sorted(set(unmatched)), "no_free_slot": sorted(set(full))}


# ================================================================== job orders / kits / materials
def _material_key(m):
    return (m.get("shape") or "", m.get("grade") or "", m.get("size") or "", m.get("unit") or "")


def job_order(inv: Inventory, pid, qty: float, net: bool = True, project_id=None) -> dict:
    """What it takes to build qty of a part: every requirement routed to who supplies it.
    groups: {vendor: [lines]} — purchased parts to their primary vendor, fabricated parts to their fab / machine
    shop (primary supplier, else 'In-house'), raw material to its material vendor, finishing to the finisher;
    assemblies are built in-house from their kits. Also the pick list (what comes off the shelf)."""
    req = explode(inv, pid, qty, net)
    groups: dict = {}
    pick = []
    materials: dict = {}

    def line(vendor, kind, item, q, unit, price=0.0, vpn="", pn="", part_id=None, note=""):
        groups.setdefault(vendor, []).append({"Kind": kind, "Part Number": pn, "Item": item, "Vendor Part #": vpn,
                                              "Qty": q, "Unit": unit, "Unit Price": price, "Ext": q * price,
                                              "Note": note, "partId": part_id})
    for part_id, e in req.items():
        p = inv.part(part_id)
        need = e["net"]
        if need <= 0:
            continue
        sups = inv.suppliers(p)
        s0 = sups[0] if sups else None
        price = inv.to_cad(s0["price"], s0.get("currency")) if s0 and s0.get("price") not in ("", None) else \
            inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
        if has_bom(p) and not s0:                       # assembled / welded here from its kit
            line("In-house assembly", "Weldment" if p.get("isWeldment") else "Assembly", p["name"], need,
                 p.get("unit") or "ea", 0.0, "", p.get("partNumber") or "", part_id,
                 f"{len(p['components'])} BOM lines")
        elif p.get("type") == "manufactured":
            vend = s0["name"] if s0 else "In-house fabrication"
            line(vend, "Fabricate", p["name"], need, p.get("unit") or "ea", price, (s0 or {}).get("partNumber", ""),
                 p.get("partNumber") or "", part_id, "; ".join(x for x in (
                     f"Rev {p['revision']}" if p.get("revision") else "",
                     f"includes {len(p['components'])} BOM line(s) — see their own vendors" if has_bom(p) else "")
                     if x))
            m = p.get("material") or {}
            if m.get("shape") or m.get("size"):
                k = _material_key(m)
                mm = materials.setdefault(k, {"qty": 0.0, "vendor": m.get("vendor") or "", "parts": [],
                                              "stockLength": num(m.get("stockLength"))})
                mm["qty"] += num(m.get("qty")) * need
                mm["parts"].append(p.get("partNumber") or p["name"])
                if m.get("vendor"):
                    mm["vendor"] = m["vendor"]
            f = p.get("finish") or {}
            if f.get("process"):
                line(f.get("vendor") or f"Finishing ({f['process']})", "Finish",
                     f"{f['process']}{' — ' + f['spec'] if f.get('spec') else ''}: {p['name']}", need,
                     p.get("unit") or "ea", 0.0, "", p.get("partNumber") or "", part_id)
        else:
            line(s0["name"] if s0 else "No vendor set", "Purchase", p["name"], need, p.get("unit") or "ea", price,
                 (s0 or {}).get("partNumber", ""), p.get("partNumber") or "", part_id)
    for (shape, grade, size, unit), mm in materials.items():
        desc = " ".join(x for x in (grade, shape, size) if x) or "Raw material"
        q = mm["qty"]
        note = ""
        if mm["stockLength"] and unit in ("in", "ft", "mm", "m"):
            bars = -(-q // mm["stockLength"])
            note = f"≈ {bars:g} × {mm['stockLength']:g} {unit} lengths (no kerf / nesting allowance)"
        line(mm["vendor"] or "Material (no vendor set)", "Material", desc, round(q, 3), unit, 0.0, "", "", None,
             (note + "; " if note else "") + "for " + ", ".join(sorted(set(mm["parts"]))))
    for part_id, e in req.items():                   # the kit: what comes off the shelf, what's still missing
        p = inv.part(part_id)
        if e["level"] == 0 or p is None:
            continue
        take, short = e["gross"] - e["net"], (0.0 if has_bom(p) else e["net"])
        if take > 0 or short > 0:
            pick.append({"Bin": inv.location_label_for(p.get("locationId")) or "—",
                         "Part Number": p.get("partNumber") or "", "Part": p["name"], "Pick": take,
                         "Unit": p.get("unit") or "ea", "On Hand": num(p.get("currentStock")), "Short": short,
                         "Level": e["level"]})
    pick.sort(key=lambda r: (r["Bin"] == "—", r["Bin"], r["Part"].lower()))
    root = inv.part(pid)
    totals = {v: sum(x["Ext"] for x in ls) for v, ls in groups.items()}
    return {"part": root["name"] if root else "", "partNumber": (root or {}).get("partNumber") or "", "qty": qty,
            "net": net, "projectId": project_id, "groups": dict(sorted(groups.items())), "totals": totals,
            "pick": pick, "requirements": req}


def material_needs(inv: Inventory, demands: list[tuple]) -> list[dict]:
    """Raw material for a set of demands [(partId, qty)] — the bulk-buy view."""
    agg: dict = {}
    for pid, q in demands:
        for part_id, e in explode(inv, pid, q).items():
            p = inv.part(part_id)
            if p is not None and is_material(p) and not has_bom(p):       # numbered material as a BOM line
                a = agg.setdefault(("#", p["id"]), {
                    "Material": (f"{p['partNumber']} " if p.get("partNumber") else "") + p["name"],
                    "Unit": p.get("unit") or "", "Qty": 0.0, "Vendor": primary_vendor(inv, p),
                    "Stock Length": num((p.get("material") or {}).get("stockLength")) or None, "parts": set()})
                a["Qty"] += e["gross"]
                a["parts"].update((inv.part(x) or {}).get("partNumber") or (inv.part(x) or {}).get("name", "")
                                  for x in e["parents"])
                continue
            m = (p or {}).get("material") or {}
            if not (m.get("shape") or m.get("size")) or has_bom(p):
                continue
            k = _material_key(m)
            a = agg.setdefault(k, {"Material": " ".join(x for x in (m.get("grade"), m.get("shape"), m.get("size"))
                                                        if x), "Unit": m.get("unit") or "", "Qty": 0.0,
                                   "Vendor": m.get("vendor") or "", "Stock Length": num(m.get("stockLength")) or None,
                                   "parts": set()})
            a["Qty"] += num(m.get("qty")) * e["gross"]
            a["parts"].add(p.get("partNumber") or p["name"])
    out = []
    for a in agg.values():
        a["Used By"] = ", ".join(sorted(a.pop("parts")))
        a["Lengths"] = -(-a["Qty"] // a["Stock Length"]) if a["Stock Length"] else None
        a["Qty"] = round(a["Qty"], 3)
        out.append(a)
    return sorted(out, key=lambda r: r["Material"])


def open_demands(inv: Inventory) -> list[tuple]:
    """Demand from open production orders: each order's project parts × order qty (top-level parts only — their
    BOMs are exploded later)."""
    out = []
    for o in inv.productionOrders:
        if o.get("status") == "Complete":
            continue
        for p in inv.parts_for_project(o.get("projectId")):
            a = inv.allocation(p, o.get("projectId"))
            if a and num(a.get("qty")):
                out.append((p["id"], num(a.get("qty")) * num(o.get("qty"), 1)))
    return out


def log_job_orders(inv: Inventory, job: dict, vendors=None, expected=None) -> list[dict]:
    """Turn a job order's vendor groups into part orders (status Ordered) with the project they're for and one PO
    draft number per vendor. Material / finishing lines are logged as text items."""
    made = []
    stamp = dt.date.today().strftime("%y%m%d")
    for i, (vend, lines) in enumerate(job["groups"].items(), start=1):
        if vend.startswith("In-house") or (vendors and vend not in vendors):
            continue
        po = f"PO-{stamp}-{i:02d}"
        rng = inv.lead_time_range(vend)
        exp = expected or (add_days(today(), rng[1]) if rng else "")
        for ln in lines:
            o = {"id": uid(), "partId": ln.get("partId"), "item": None if ln.get("partId") else ln["Item"],
                 "qty": ln["Qty"], "unit": ln["Unit"], "orderDate": today(), "expectedDate": exp, "status": "Ordered",
                 "vendorName": vend, "poNumber": po, "projectId": job.get("projectId"), "kind": ln["Kind"],
                 "events": [{"what": "ordered", "at": now_ms(), "by": inv.userName or "unknown"}]}
            inv.partOrders.append(o)
            made.append(o)
    return made


def write_po_drafts(inv: Inventory, job: dict, folder) -> list[str]:
    """One workbook per vendor: vendor details, the job, and the lines to order."""
    import openpyxl
    from openpyxl.styles import Font
    from pathlib import Path
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for vend, lines in job["groups"].items():
        if vend.startswith("In-house"):
            continue
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "PO draft"
        v = vendor_record(inv, vend) or {}
        ws.append([f"Purchase order draft — {vend}"])
        ws["A1"].font = Font(bold=True, size=14)
        addr = ", ".join(x for x in (v.get("address"), v.get("address2"), v.get("city"), v.get("province"),
                                     v.get("postalCode"), v.get("country")) if x)
        for lab, val in (("Vendor", vend), ("Address", addr), ("Contact", v.get("contactName")),
                         ("Phone", v.get("phone")), ("Email", v.get("email")), ("Account #", v.get("accountNumber")),
                         ("Terms", v.get("terms")), ("For", f"{job['qty']:g} × {job['partNumber']} {job['part']}"
                                                     + (f" ({inv.project_name(job['projectId'])})"
                                                        if job.get("projectId") else "")),
                         ("Date", today()), ("Prepared by", inv.userName)):
            ws.append([lab, val or ""])
            ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.append([])
        cols = ["Kind", "Part Number", "Item", "Vendor Part #", "Qty", "Unit", "Unit Price", "Ext", "Note"]
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        for ln in lines:
            ws.append([ln[c] for c in cols])
        ws.append(["", "", "", "", "", "", "Total", sum(ln["Ext"] for ln in lines)])
        ws.cell(ws.max_row, 7).font = Font(bold=True)
        for col, w in zip("ABCDEFGHI", (12, 14, 46, 16, 8, 6, 11, 11, 50)):
            ws.column_dimensions[col].width = w
        safe = re.sub(r"[^A-Za-z0-9 _.-]", "", vend).strip() or "vendor"
        p = folder / f"PO draft - {safe} - {job['partNumber'] or job['part']}.xlsx"
        wb.save(p)
        paths.append(str(p))
    return paths


# ================================================================== time phased
def order_item(inv: Inventory, o) -> str:
    p = inv.part(o.get("partId"))
    return p["name"] if p else (o.get("item") or "(deleted part)")


def incoming(inv: Inventory) -> list[dict]:
    rows = []
    t = dt.date.today()
    for o in inv.open_part_orders():
        d = parse_date(o.get("expectedDate"))
        rows.append({"Expected": o.get("expectedDate") or "", "Week": (f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                                                                      if d else "No date"),
                     "Late": bool(d and d < t), "Item": order_item(inv, o), "Qty": num(o.get("qty")),
                     "Vendor": o.get("vendorName") or "", "PO": o.get("poNumber") or "",
                     "For": inv.project_name(o.get("projectId")) or "Stock", "Ordered": o.get("orderDate") or "",
                     "id": o["id"]})
    return rows


def projected(inv: Inventory, horizon_days: int = 120) -> list[dict]:
    """Projected on-hand per part over time: stock now, + open orders on their expected dates, − open production
    orders' needs on their due dates (fab start if set). First date it goes negative = shortage date."""
    t0 = dt.date.today()
    end = t0 + dt.timedelta(days=horizon_days)
    events: dict = {}
    for o in inv.open_part_orders():
        if o.get("partId"):
            d = parse_date(o.get("expectedDate")) or t0
            events.setdefault(o["partId"], []).append((max(d, t0), num(o.get("qty")), "in", o))
    for o in inv.productionOrders:
        if o.get("status") == "Complete":
            continue
        d = parse_date(o.get("fabStart")) or parse_date(o.get("dueDate")) or t0
        for p in inv.parts_for_project(o.get("projectId")):
            a = inv.allocation(p, o.get("projectId"))
            q = num(a.get("qty")) * num(o.get("qty"), 1) if a else 0
            for part_id, e in explode(inv, p["id"], q).items():
                events.setdefault(part_id, []).append((max(d, t0), -e["gross"], "out", o))
    rows = []
    for part_id, evs in events.items():
        p = inv.part(part_id)
        if p is None:
            continue
        evs.sort(key=lambda e: (e[0], e[1] < 0))
        bal = num(p.get("currentStock"))
        low = short = None
        incoming_q = sum(q for d, q, k, _o in evs if k == "in" and d <= end)
        demand_q = -sum(q for d, q, k, _o in evs if k == "out" and d <= end)
        for d, q, _k, _o in evs:
            if d > end:
                break
            bal += q
            if bal < 0 and short is None:
                short = d.isoformat()
            if bal <= num(p.get("reorderPoint")) and low is None:
                low = d.isoformat()
        rows.append({"Part": p["name"], "Part Number": p.get("partNumber") or "", "On Hand": num(p.get("currentStock")),
                     "Incoming": incoming_q, "Demand": demand_q, "Projected": bal, "Short On": short or "",
                     "Below Reorder On": low or "", "id": part_id,
                     "timeline": [(d.isoformat(), q) for d, q, _k, _o in evs if d <= end]})
    return sorted(rows, key=lambda r: (r["Short On"] == "", r["Short On"], r["Below Reorder On"] == "", r["Part"].lower()))


def add_event(o, what, by=""):
    o.setdefault("events", []).append({"what": what, "at": now_ms(), "by": by or "unknown"})


def bin_order(inv: Inventory, oid, location_id=None):
    """Received goods put away: status Binned, optional bin, logged."""
    o = next((x for x in inv.partOrders if x["id"] == oid), None)
    if o is None:
        raise ValueError("No such order")
    if o.get("status") not in ("Received", "Binned"):
        raise ValueError("Receive the order first")
    o["status"] = "Binned"
    o["binnedDate"] = today()
    p = inv.part(o.get("partId"))
    if p is not None and location_id:
        p["locationId"] = location_id
        p["updatedAt"] = now_ms()
    add_event(o, "binned", inv.userName)
    return o


def order_log(inv: Inventory, limit: int = 500) -> list[dict]:
    """Ordered / received / binned / cancelled for part orders and shipped for client orders, newest first."""
    rows = []
    for o in inv.partOrders:
        evs = list(o.get("events") or [])
        have = {e["what"] for e in evs}
        for what, k in (("ordered", "orderDate"), ("received", "receivedDate"), ("binned", "binnedDate")):
            if what not in have and o.get(k):
                evs.append({"what": what, "date": o[k], "by": ""})
        for e in evs:
            when = _d(e["at"]) + dt.datetime.fromtimestamp(e["at"] / 1000).strftime(" %H:%M") if e.get("at") \
                else e.get("date", "")
            rows.append({"When": when, "Event": e["what"].title(), "Item": order_item(inv, o),
                         "Qty": num(o.get("qty")), "Vendor": o.get("vendorName") or "", "PO": o.get("poNumber") or "",
                         "For": inv.project_name(o.get("projectId")) or "Stock", "By": e.get("by") or ""})
    for co in inv.clientOrders:
        if co.get("status") in ("Shipped", "Complete") and (co.get("shippedDate") or co.get("dueDate")):
            rows.append({"When": co.get("shippedDate") or co.get("dueDate"), "Event": "Shipped",
                         "Item": co.get("notes") or "Client order", "Qty": "",
                         "Vendor": (inv.client(co.get("clientId")) or {}).get("name", ""), "PO": co.get("poNumber", ""),
                         "For": (inv.client(co.get("clientId")) or {}).get("name", ""), "By": ""})
    rows.sort(key=lambda r: r["When"], reverse=True)
    return rows[:limit]
