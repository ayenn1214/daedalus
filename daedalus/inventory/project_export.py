"""Project export → Excel: everything needed to quote, buy for and build a project.

Sheets
  Summary      project, units, counts and cost by type, shortages
  BOM          the project as shown in DIMS — MFG / Purchased / Fasteners / Other, BOM lines under their part
  Build List   every part through every BOM level, totalled for the units (what the shop actually makes / uses)
  Purchasing   purchased parts and fasteners to buy for the units, grouped by vendor, with to-order qty and cost
  MFG          manufactured parts for the units, with material and finish (cut / fab list)

Several projects export into one workbook: one Summary sheet for all, then the other sheets per project.
"""
from __future__ import annotations

import re

from . import model as M

GROUPS = ("MFG parts", "Purchased parts", "Fasteners", "Other")


def group(p) -> str:
    return {"manufactured": "MFG parts", "purchased": "Purchased parts", "fastener": "Fasteners"}.get(p.get("type"),
                                                                                                    "Other")


def _finish(p) -> str:
    f = p.get("finish")
    return ((f or {}).get("process") if isinstance(f, dict) else f) or ""


def _cost(inv, p) -> float:
    return inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))


def _vendor(inv, p) -> dict:
    b = inv.best_supplier(p)
    if b:
        return b
    s = inv.suppliers(p)
    return s[0] if s else {}


def _spec(p) -> str:
    if p.get("type") == "fastener":
        bits = [p.get("size"), p.get("length") and f"L {p['length']}", p.get("grade"), p.get("fastenerMaterial"),
                p.get("fastenerFinish")]
        return " · ".join(str(b) for b in bits if b)
    return " · ".join(str(b) for b in (p.get("material"), _finish(p)) if b)


def build_totals(inv, pid, units) -> dict:
    """{partId: qty} for `units` of the project through every BOM level."""
    from .orders import requirements
    return requirements(inv, {"projectId": pid, "qty": units})


def project_data(inv: M.Inventory, pid: str, units: float | None = None) -> dict:
    """Everything the workbook shows, as plain data (also used by tests and Icarus)."""
    pr = inv.project(pid)
    if pr is None:
        raise ValueError("No such project")
    on_order = inv.project_qty(pid)
    units = float(units if units is not None else (on_order or 1))
    rows = inv.project_rows(pid, per_unit=True)
    # BOM view: top-level part + its BOM lines, grouped by type, sorted by part number
    blocks: dict = {g: [] for g in GROUPS}
    cur = None
    for p, depth, ql, need1, on_hand, emb in rows:
        if depth == 0:
            cur = []
            blocks[group(p)].append(cur)
        cur.append({"part": p, "level": depth, "qtyLabel": ql, "perUnit": need1, "onHand": on_hand,
                    "embedded": emb})
    bom = []
    for g in GROUPS:
        bl = sorted(blocks[g], key=lambda b: ((b[0]["part"].get("partNumber") or "~").upper(),
                                              b[0]["part"]["name"].lower()))
        for b in bl:
            for r in b:
                r["group"] = g
                bom.append(r)
    totals = build_totals(inv, pid, units)
    build = []
    for part_id, qty in totals.items():
        p = inv.part(part_id)
        if p is None or qty <= 0:
            continue
        stock = M.num(p.get("currentStock"))
        build.append({"part": p, "group": group(p), "qty": qty, "onHand": stock, "short": max(0.0, qty - stock),
                      "cost": _cost(inv, p)})
    build.sort(key=lambda r: (GROUPS.index(r["group"]), (r["part"].get("partNumber") or "~").upper(),
                              r["part"]["name"].lower()))
    per_unit_cost = {g: 0.0 for g in GROUPS}
    one = build_totals(inv, pid, 1)
    for part_id, qty in one.items():
        p = inv.part(part_id)
        if p is not None and not (p.get("isWeldment") or p.get("isAssembly")):   # count parts, not their kits
            per_unit_cost[group(p)] += qty * _cost(inv, p)
    counts = {g: sum(1 for r in bom if r["level"] == 0 and r["group"] == g) for g in GROUPS}
    return {"project": pr, "units": units, "onOrder": on_order, "bom": bom, "build": build,
            "counts": counts, "perUnitCost": per_unit_cost,
            "short": sum(1 for r in build if r["short"] > 0)}


# ------------------------------------------------------------------ workbook
def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    thin = Side(style="thin", color="D0D4DA")
    return {
        "title": Font(bold=True, size=14),
        "bold": Font(bold=True),
        "head": Font(bold=True, color="FFFFFF"),
        "headfill": PatternFill("solid", fgColor="2B3A4A"),
        "groupfont": Font(bold=True, color="0B6E5E"),
        "groupfill": PatternFill("solid", fgColor="E3F1EE"),
        "short": Font(bold=True, color="C0392B"),
        "muted": Font(color="7A828C", italic=True),
        "border": Border(bottom=thin),
        "wrap": Alignment(vertical="top"),
    }


def _sheet_name(wb, name):
    base = re.sub(r"[\\/*?:\[\]]", "-", name)[:31] or "Sheet"
    nm, i = base, 2
    while nm in wb.sheetnames:
        nm = f"{base[:28]} {i}"
        i += 1
    return nm


def _table(ws, cols, rows, st, start_row=1, widths=None, money=(), qty=()):
    """Header + rows. A row that is a str is a group header spanning the table."""
    from openpyxl.utils import get_column_letter
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=start_row, column=j, value=c)
        cell.font, cell.fill = st["head"], st["headfill"]
    r = start_row
    for row in rows:
        r += 1
        if isinstance(row, str):
            ws.cell(row=r, column=1, value=row).font = st["groupfont"]
            for j in range(1, len(cols) + 1):
                ws.cell(row=r, column=j).fill = st["groupfill"]
            continue
        for j, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=j, value=v)
            if cols[j - 1] in money and isinstance(v, (int, float)):
                cell.number_format = '"$"#,##0.00'
            elif cols[j - 1] in qty and isinstance(v, (int, float)):
                cell.number_format = "#,##0.##"
            if cols[j - 1].startswith("Short") and isinstance(v, (int, float)) and v > 0:
                cell.font = st["short"]
    for j, c in enumerate(cols, 1):
        w = (widths or {}).get(c)
        if w is None:
            w = max([len(str(c))] + [len(str(x[j - 1] if j - 1 < len(x) else "")) for x in rows
                                     if not isinstance(x, str)][:300] or [8])
            w = min(46, max(8, w + 2))
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    ws.page_setup.orientation = "landscape"                  # prints one page wide
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = f"{start_row}:{start_row}"
    ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(cols))}{max(r, start_row)}"
    return r


def _n(x):
    return round(float(x), 4) if x not in (None, "") else None


def write(path, inv: M.Inventory, pids, units: dict | None = None, by: str = ""):
    """Write the project workbook. units: {projectId: units} (default: units on open production, else 1)."""
    import openpyxl
    st = _styles()
    data = [project_data(inv, pid, (units or {}).get(pid)) for pid in pids]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Project export" if len(data) > 1 else data[0]["project"]["name"]
    ws["A1"].font = st["title"]
    ws["A2"] = f"{M.today()}" + (f" · {by}" if by else "") + " · costs in CAD"
    ws["A2"].font = st["muted"]
    cols = ["Project", "Units", "On open production", *[f"{g}" for g in GROUPS], "Lines short",
            "Cost / unit", "Cost × units"]
    rows = []
    for d in data:
        cu = sum(d["perUnitCost"].values())
        rows.append([d["project"]["name"], _n(d["units"]), _n(d["onOrder"]) or "—",
                     *[d["counts"][g] for g in GROUPS], d["short"], cu, cu * d["units"]])
    _table(ws, cols, rows, st, start_row=4, money=("Cost / unit", "Cost × units"), qty=("Units",))
    r = 6 + len(rows)
    ws.cell(row=r, column=1, value="Cost / unit by type").font = st["bold"]
    _table(ws, ["Project", *GROUPS], [[d["project"]["name"], *[d["perUnitCost"][g] for g in GROUPS]] for d in data],
           st, start_row=r + 1, money=GROUPS)
    ws.freeze_panes = None
    ws.print_title_rows = None
    from openpyxl.utils import get_column_letter
    ws.column_dimensions["A"].width = max(16, max(len(d["project"]["name"]) for d in data) + 2)
    for j in range(2, 12):
        ws.column_dimensions[get_column_letter(j)].width = 17
    multi = len(data) > 1
    for d in data:
        nm = d["project"]["name"]
        u = d["units"]
        pre = f"{nm} · " if multi else ""
        # BOM
        ws = wb.create_sheet(_sheet_name(wb, pre + "BOM"))
        cols = ["Part No.", "Part", "Description", "Spec", "Qty / Unit", f"For {u:g} unit{'s' if u != 1 else ''}",
                "On Hand", "Short", "Unit Cost", "Vendor", "Vendor PN", "Bin"]
        out, last = [], None
        for row in d["bom"]:
            if row["group"] != last:
                last = row["group"]
                out.append(f"{last} · {d['counts'][last]}")
            p = row["part"]
            v = _vendor(inv, p)
            need = row["perUnit"] * u
            out.append([p.get("partNumber") or "", ("    " * row["level"]) + p["name"], p.get("description") or "",
                        _spec(p), _n(row["perUnit"]), _n(need), _n(row["onHand"]),
                        _n(max(0.0, need - row["onHand"])), _cost(inv, p) or None, v.get("name") or "",
                        v.get("partNumber") or "", inv.location_label_for(p.get("locationId"))])
        _table(ws, cols, out, st, money=("Unit Cost",), qty=cols[4:8], widths={"Part": 34, "Description": 34})
        # Build list (every level, totalled)
        ws = wb.create_sheet(_sheet_name(wb, pre + "Build List"))
        cols = ["Type", "Part No.", "Part", "Spec", "Qty", "On Hand", "Short", "Unit Cost", "Ext. Cost"]
        out, last = [], None
        for row in d["build"]:
            if row["group"] != last:
                last = row["group"]
                out.append(last)
            p = row["part"]
            out.append([M.TYPE_LABEL.get(p.get("type"), ""), p.get("partNumber") or "", p["name"],
                        _spec(p), _n(row["qty"]), _n(row["onHand"]), _n(row["short"]), row["cost"] or None,
                        (row["cost"] * row["qty"]) or None])
        _table(ws, cols, out, st, money=("Unit Cost", "Ext. Cost"), qty=("Qty", "On Hand", "Short"),
               widths={"Part": 34})
        # Purchasing by vendor
        ws = wb.create_sheet(_sheet_name(wb, pre + "Purchasing"))
        cols = ["Vendor", "Type", "Part No.", "Part", "Vendor PN", "Need", "On Hand", "To Order", "Price",
                "Currency", "Ext. (CAD)", "Website"]
        buys: dict = {}
        for row in d["build"]:
            p = row["part"]
            if p.get("type") not in ("purchased", "fastener"):
                continue
            v = _vendor(inv, p)
            buys.setdefault(v.get("name") or "No vendor set", []).append((row, v))
        out = []
        for vend in sorted(buys, key=lambda x: (x == "No vendor set", x.lower())):
            lines = buys[vend]
            tot = sum(inv.to_cad(v.get("price"), v.get("currency")) * r["short"] for r, v in lines)
            out.append(f"{vend} · {len(lines)} line{'s' if len(lines) != 1 else ''} · to order ≈ ${tot:,.2f}")
            for r, v in lines:
                p = r["part"]
                price = M.num(v.get("price")) if v.get("price") not in (None, "") else None
                out.append([vend, M.TYPE_LABEL.get(p.get("type"), ""), p.get("partNumber") or "", p["name"],
                            v.get("partNumber") or "", _n(r["qty"]), _n(r["onHand"]), _n(r["short"]), price,
                            v.get("currency") or "CAD",
                            (inv.to_cad(price, v.get("currency")) * r["short"]) if price else None,
                            v.get("website") or ""])
        last_row = _table(ws, cols, out, st, money=("Price", "Ext. (CAD)"), qty=("Need", "On Hand", "To Order"),
                          widths={"Part": 34, "Website": 30})
        for k in range(2, last_row + 1):
            c = ws.cell(row=k, column=len(cols))
            if c.value and str(c.value).startswith("http"):
                c.hyperlink = str(c.value)
                c.style = "Hyperlink"
        # MFG list
        ws = wb.create_sheet(_sheet_name(wb, pre + "MFG"))
        cols = ["Part No.", "Part", "Kind", "Material", "Finish", "Finisher", "Qty", "On Hand", "To Make"]
        out = []
        for row in d["build"]:
            p = row["part"]
            if p.get("type") != "manufactured":
                continue
            f = p.get("finish")
            kind = "Weldment" if p.get("isWeldment") else "Assembly" if p.get("isAssembly") else "Part"
            out.append([p.get("partNumber") or "", p["name"], kind, p.get("material") or "", _finish(p),
                        (f or {}).get("vendor", "") if isinstance(f, dict) else "", _n(row["qty"]),
                        _n(row["onHand"]), _n(row["short"])])
        _table(ws, cols, out, st, qty=("Qty", "On Hand", "To Make"), widths={"Part": 34})
    wb.save(path)
    return path
