"""Project BOM / parts list — shown in DIMS (Project BOM window) and exported to Excel.

  MFG parts        manufactured parts as a hierarchy: item 1, 1.1, 1.1.1 … through weldments and assemblies
  Fasteners        every fastener in the build, totalled (through every BOM level)
  Purchased parts  every purchased part in the build, totalled
  Other            anything without a type

Quantities: Qty is per parent (per unit for top-level lines), Per Unit is for one project unit, Total is × units.

Workbook sheets: BOM (all sections, hierarchy) · MFG · Fasteners · Purchased · Buy List (by vendor, to order).
Several projects → one workbook with a Summary and sheets per project.
"""
from __future__ import annotations

import re

from . import model as M

GROUPS = ("MFG parts", "Purchased parts", "Fasteners", "Other")
LIST_GROUPS = ("Fasteners", "Purchased parts", "Other")


def group(p) -> str:
    return {"manufactured": "MFG parts", "purchased": "Purchased parts", "fastener": "Fasteners"}.get(p.get("type"),
                                                                                                    "Other")


def finish(p) -> str:
    f = p.get("finish")
    return ((f or {}).get("process") if isinstance(f, dict) else f) or ""


def material(p) -> str:
    """Raw material as text — the part profile keeps it as {grade, shape, size, …}; imports may have plain text."""
    m = p.get("material")
    if isinstance(m, dict):
        return " ".join(str(x) for x in (m.get("grade"), m.get("shape"), m.get("size")) if x)
    return str(m or "")


def finisher(p) -> str:
    f = p.get("finish")
    return ((f or {}).get("vendor") if isinstance(f, dict) else "") or ""


def cost(inv, p) -> float:
    return inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))


def vendor(inv, p) -> dict:
    b = inv.best_supplier(p)
    if b:
        return b
    s = inv.suppliers(p)
    return s[0] if s else {}


def spec(p) -> str:
    if p.get("type") == "fastener":
        bits = [p.get("size"), p.get("length") and f"L {p['length']}", p.get("grade") and f"Gr {p['grade']}",
                p.get("fastenerMaterial"), p.get("fastenerFinish")]
        return " · ".join(str(b) for b in bits if b)
    return " · ".join(str(b) for b in (material(p), finish(p)) if b)


def kind(p) -> str:
    if p.get("isWeldment"):
        return "Weldment"
    if p.get("isAssembly"):
        return "Assembly"
    return "Part" if p.get("type") == "manufactured" else (M.TYPE_LABEL.get(p.get("type"), "") or "Part")


def _sort_key(p):
    return (p.get("partNumber") or "~").upper(), p["name"].lower()


def top_level(inv, pid, config=None):
    """Project parts that aren't inside another project part's BOM, with their qty per unit — plus what the
    chosen options add (config; defaults when None)."""
    from .configs import lines as option_lines
    on = inv.parts_for_project(pid)
    inside = set()
    for p in on:
        for c in p.get("components") or []:
            inside.add(c.get("partId"))
    out = []
    for p in on:
        if p["id"] in inside:
            continue
        a = inv.allocation(p, pid)
        q = M.num(a.get("qty")) if a else 0
        if q > 0:
            out.append((p, q))
    have = {p["id"]: i for i, (p, _q) in enumerate(out)}
    for part_id, q in option_lines(inv.project(pid), config).items():
        p = inv.part(part_id)
        if p is None:
            continue
        if part_id in have:
            out[have[part_id]] = (p, out[have[part_id]][1] + q)
        else:
            out.append((p, q))
    return sorted(out, key=lambda x: _sort_key(x[0]))


def mfg_tree(inv, pid, units: float, config=None) -> list[dict]:
    """Manufactured parts as a hierarchy. Each line: item, level, part, qty (per parent), perUnit, total."""
    lines: list[dict] = []

    def walk(p, qty, per_unit, level, item, seen):
        lines.append({"item": item, "level": level, "part": p, "qty": qty, "perUnit": per_unit,
                      "total": per_unit * units, "hasChildren": False})
        me = lines[-1]
        if p["id"] in seen or level > 20:
            return
        kids = []
        for c in p.get("components") or []:
            ch = inv.part(c.get("partId"))
            if ch is not None and ch.get("type") == "manufactured":
                kids.append((ch, M.num(c.get("qty"), 1)))
        kids.sort(key=lambda x: _sort_key(x[0]))
        me["hasChildren"] = bool(kids)
        for i, (ch, q) in enumerate(kids, 1):
            walk(ch, q, per_unit * q, level + 1, f"{item}.{i}", seen | {p["id"]})

    n = 0
    for p, q in top_level(inv, pid, config):
        if p.get("type") == "manufactured":
            n += 1
            walk(p, q, q, 0, str(n), set())
    return lines


def flat_lists(inv, pid, units: float, config=None) -> dict:
    """{group: [line]} for fasteners / purchased / other — totalled through every BOM level."""
    from .mrp import explode
    per_unit: dict = {}
    for p, q in top_level(inv, pid, config):
        for part_id, e in explode(inv, p["id"], q, False).items():
            per_unit[part_id] = per_unit.get(part_id, 0.0) + e["gross"]
    out = {g: [] for g in LIST_GROUPS}
    for part_id, q in per_unit.items():
        p = inv.part(part_id)
        if p is None or p.get("type") == "manufactured":
            continue
        tot = q * units
        stock = M.num(p.get("currentStock"))
        out[group(p)].append({"part": p, "perUnit": q, "total": tot, "onHand": stock,
                              "short": max(0.0, tot - stock)})
    for g in out:
        out[g].sort(key=lambda r: _sort_key(r["part"]))
    return out


def project_data(inv: M.Inventory, pid: str, units: float | None = None, config=None) -> dict:
    pr = inv.project(pid)
    if pr is None:
        raise ValueError("No such project")
    on_order = inv.project_qty(pid)
    units = float(units if units is not None else (on_order or 1))
    tree = mfg_tree(inv, pid, units, config)
    lists = flat_lists(inv, pid, units, config)
    mfg_totals: dict = {}
    for ln in tree:
        mfg_totals[ln["part"]["id"]] = mfg_totals.get(ln["part"]["id"], 0.0) + ln["total"]
    mfg_flat = []
    for part_id, tot in mfg_totals.items():
        p = inv.part(part_id)
        stock = M.num(p.get("currentStock"))
        mfg_flat.append({"part": p, "perUnit": tot / units if units else 0, "total": tot, "onHand": stock,
                         "short": max(0.0, tot - stock)})
    mfg_flat.sort(key=lambda r: _sort_key(r["part"]))
    per_unit_cost = {g: sum(r["perUnit"] * cost(inv, r["part"]) for r in lists.get(g, [])) for g in LIST_GROUPS}
    per_unit_cost["MFG parts"] = sum(r["perUnit"] * cost(inv, r["part"]) for r in mfg_flat
                                     if not (r["part"].get("isWeldment") or r["part"].get("isAssembly")))
    counts = {"MFG parts": len(mfg_flat), **{g: len(lists[g]) for g in LIST_GROUPS}}
    short = sum(1 for r in mfg_flat if r["short"] > 0) + sum(1 for g in LIST_GROUPS for r in lists[g]
                                                             if r["short"] > 0)
    from . import configs as C
    return {"project": pr, "units": units, "onOrder": on_order, "tree": tree, "mfg": mfg_flat, "lists": lists,
            "config": C.normalize(pr, config), "configLabel": C.label(pr, config, short=False),
            "counts": counts, "perUnitCost": per_unit_cost, "short": short}


# ------------------------------------------------------------------ table rows (window, copy, Excel)
def tree_rows(inv, d):
    cols = ["Item", "Part No.", "Part", "Description", "Kind", "Material", "Finish", "Qty", "Per Unit",
            f"Total ×{d['units']:g}", "On Hand", "Bin"]
    rows = []
    for ln in d["tree"]:
        p = ln["part"]
        rows.append([ln["item"], p.get("partNumber") or "", ("    " * ln["level"]) + p["name"],
                     p.get("description") or "", kind(p), material(p), finish(p), _n(ln["qty"]),
                     _n(ln["perUnit"]), _n(ln["total"]), _n(p.get("currentStock")),
                     inv.location_label_for(p.get("locationId"))])
    return cols, rows


def list_rows(inv, d, g):
    cols = ["Part No.", "Part", "Description", "Spec", "Per Unit", f"Total ×{d['units']:g}", "On Hand", "Short",
            "Vendor", "Vendor PN", "Price", "Currency", "Bin"]
    rows = []
    for r in d["lists"][g]:
        p = r["part"]
        v = vendor(inv, p)
        rows.append([p.get("partNumber") or "", p["name"], p.get("description") or "", spec(p), _n(r["perUnit"]),
                     _n(r["total"]), _n(r["onHand"]), _n(r["short"]), v.get("name") or "",
                     v.get("partNumber") or "", _n(v.get("price")) if v.get("price") not in (None, "") else None,
                     (v.get("currency") or "CAD") if v else "", inv.location_label_for(p.get("locationId"))])
    return cols, rows


def tsv(cols, rows) -> str:
    """Tab-separated text: pastes straight into Excel, Sheets, Word or email tables."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).replace("\t", " ").replace("\n", " ")
    return "\n".join("\t".join(cell(v) for v in r) for r in [cols, *rows]) + "\n"


def _n(x):
    if x in (None, ""):
        return None
    try:
        f = round(float(x), 4)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


# ------------------------------------------------------------------ workbook
def _styles():
    from openpyxl.styles import Font, PatternFill
    return {
        "title": Font(bold=True, size=14),
        "bold": Font(bold=True),
        "head": Font(bold=True, color="FFFFFF"),
        "headfill": PatternFill("solid", fgColor="2B3A4A"),
        "section": Font(bold=True, size=12, color="0B6E5E"),
        "sectionfill": PatternFill("solid", fgColor="E3F1EE"),
        "top": Font(bold=True),
        "short": Font(bold=True, color="C0392B"),
        "muted": Font(color="7A828C", italic=True),
    }


def _sheet_name(wb, name):
    base = re.sub(r"[\\/*?:\[\]]", "-", name)[:31] or "Sheet"
    nm, i = base, 2
    while nm in wb.sheetnames:
        nm = f"{base[:28]} {i}"
        i += 1
    return nm


def _print_setup(ws, head_row=1):
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    if head_row:
        ws.print_title_rows = f"{head_row}:{head_row}"


def _widths(ws, cols, rows, fixed=None):
    from openpyxl.utils import get_column_letter
    for j, c in enumerate(cols, 1):
        w = (fixed or {}).get(c)
        if w is None:
            vals = [len(str(r[j - 1])) for r in rows[:400] if not isinstance(r, str) and j - 1 < len(r)
                    and r[j - 1] is not None]
            w = min(44, max(7, max([len(str(c))] + vals) + 2))
        ws.column_dimensions[get_column_letter(j)].width = w


def _write_rows(ws, r, cols, rows, st, bold_top=False, money=("Price", "Unit Cost", "Ext. (CAD)", "Est. Cost")):
    """Header at row r, rows after it. Returns the last row written."""
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=r, column=j, value=c)
        cell.font, cell.fill = st["head"], st["headfill"]
    for row in rows:
        r += 1
        top = bold_top and isinstance(row[0], str) and "." not in row[0]
        for j, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=j, value=v)
            if cols[j - 1] in money and isinstance(v, (int, float)):
                cell.number_format = '"$"#,##0.00'
            if cols[j - 1] in ("Short", "To Order") and isinstance(v, (int, float)) and v > 0:
                cell.font = st["short"]
            elif top:
                cell.font = st["top"]
    return r


def _sheet(wb, name, cols, rows, st, **kw):
    ws = wb.create_sheet(_sheet_name(wb, name))
    last = _write_rows(ws, 1, cols, rows, st, **kw)
    _widths(ws, cols, rows, {"Part": 36, "Description": 30})
    ws.freeze_panes = "A2"
    from openpyxl.utils import get_column_letter
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(last, 1)}"
    _print_setup(ws)
    return ws


def buy_rows(inv, d):
    """To-order lines for the project build, by vendor (fasteners + purchased + other)."""
    cols = ["Vendor", "Part No.", "Vendor PN", "Part", "Spec", "Need", "On Hand", "To Order", "Unit", "Price",
            "Currency", "Ext. (CAD)", "Website"]
    by: dict = {}
    for g in LIST_GROUPS:
        for r in d["lists"][g]:
            if r["short"] <= 0:
                continue
            v = vendor(inv, r["part"])
            by.setdefault(v.get("name") or "No vendor set", []).append((r, v))
    rows = []
    for vend in sorted(by, key=lambda x: (x == "No vendor set", x.lower())):
        for r, v in by[vend]:
            p = r["part"]
            price = M.num(v.get("price")) if v.get("price") not in (None, "") else None
            rows.append([vend, p.get("partNumber") or "", v.get("partNumber") or "", p["name"], spec(p),
                         _n(r["total"]), _n(r["onHand"]), _n(r["short"]), p.get("unit") or "ea", price,
                         v.get("currency") or "CAD",
                         round(inv.to_cad(price, v.get("currency")) * r["short"], 2) if price else None,
                         v.get("website") or ""])
    return cols, rows


def write(path, inv: M.Inventory, pids, units: dict | None = None, by: str = "", configs: dict | None = None):
    """Project BOM workbook. units: {projectId: units} (default: units on open production, else 1)."""
    import openpyxl
    from openpyxl.utils import get_column_letter
    st = _styles()
    data = [project_data(inv, pid, (units or {}).get(pid), (configs or {}).get(pid)) for pid in pids]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    multi = len(data) > 1
    if multi:
        ws = wb.create_sheet("Summary")
        ws["A1"] = "Projects"
        ws["A1"].font = st["title"]
        ws["A2"] = f"{M.today()}" + (f" · {by}" if by else "") + " · costs in CAD"
        ws["A2"].font = st["muted"]
        cols = ["Project", "Units", "MFG parts", "Fasteners", "Purchased parts", "Other", "Lines short",
                "Cost / unit", "Cost × units"]
        rows = [[d["project"]["name"], _n(d["units"]), d["counts"]["MFG parts"], d["counts"]["Fasteners"],
                 d["counts"]["Purchased parts"], d["counts"]["Other"], d["short"],
                 round(sum(d["perUnitCost"].values()), 2), round(sum(d["perUnitCost"].values()) * d["units"], 2)]
                for d in data]
        _write_rows(ws, 4, cols, rows, st, money=("Cost / unit", "Cost × units"))
        _widths(ws, cols, rows, {"Project": 24})
        _print_setup(ws, 0)
    for d in data:
        name = d["project"]["name"]
        pre = f"{name} · " if multi else ""
        u = d["units"]
        # BOM: one sheet, all sections
        ws = wb.create_sheet(_sheet_name(wb, pre + "BOM"))
        ws["A1"] = f"{name} — Bill of Materials"
        ws["A1"].font = st["title"]
        ws["A2"] = (f"{u:g} unit{'s' if u != 1 else ''}" + (f" · {d['configLabel']}" if d.get("configLabel") else "")
                    + f" · {M.today()}" + (f" · {by}" if by else "")
                    + f" · {d['counts']['MFG parts']} MFG · {d['counts']['Fasteners']} fasteners · "
                      f"{d['counts']['Purchased parts']} purchased")
        ws["A2"].font = st["muted"]
        r = 4
        sections = [("MFG parts", *tree_rows(inv, d), True)]
        for g in LIST_GROUPS:
            if d["lists"][g]:
                sections.append((g, *list_rows(inv, d, g), False))
        width = 1
        for title, cols, rows, tree in sections:
            if not rows:
                continue
            width = max(width, len(cols))
            ws.cell(row=r, column=1, value=f"{title} · {len(rows)}").font = st["section"]
            for j in range(1, len(cols) + 1):
                ws.cell(row=r, column=j).fill = st["sectionfill"]
            r = _write_rows(ws, r + 1, cols, rows, st, bold_top=tree) + 2
        for j in range(1, width + 1):
            ws.column_dimensions[get_column_letter(j)].width = 13
        for col, w in (("A", 11), ("B", 14), ("C", 38), ("D", 28)):
            ws.column_dimensions[col].width = w
        _print_setup(ws, 0)
        # one sheet per section (filterable)
        cols, rows = tree_rows(inv, d)
        _sheet(wb, pre + "MFG", cols, rows, st, bold_top=True)
        for g, short in (("Fasteners", "Fasteners"), ("Purchased parts", "Purchased"), ("Other", "Other")):
            if d["lists"][g] or g != "Other":
                cols, rows = list_rows(inv, d, g)
                _sheet(wb, pre + short, cols, rows, st)
        cols, rows = buy_rows(inv, d)
        ws = _sheet(wb, pre + "Buy List", cols, rows, st)
        for k in range(2, len(rows) + 2):
            c = ws.cell(row=k, column=len(cols))
            if c.value and str(c.value).startswith("http"):
                c.hyperlink = str(c.value)
                c.style = "Hyperlink"
    wb.save(path)
    return path
