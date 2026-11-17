"""Finishing costs: zinc plating (always by weight, $ / lb) and powder coating (quoted per part; a quote tends to
hold for later orders, so the last quote is reused).

Parts carry:
  weightLb            weight of one part, lb (zinc plating cost = weight × qty × rate)
  finish.process      "Zinc plating" / "Powder coat" … (which list a part belongs on)
  pcQuotes            [{price, currency, vendor, date, by, ref}] — newest last; the newest is the price used
The zinc rate lives in the shared settings (syncMeta["finishing"]["zincRate"]), so everyone prices the same.
"""
from __future__ import annotations

from . import model as M
from . import mrp as R

ZINC_RATE = 1.21                       # $ / lb, CAD — the plater's current rate (change it in the Finishing tab)
ZINC, POWDER = "zinc", "powder"
PROCESS_NAME = {ZINC: "Zinc plating", POWDER: "Powder coat"}


def settings(inv) -> dict:
    return (inv.syncMeta or {}).setdefault("finishing", {}) if isinstance(inv.syncMeta, dict) else {}


def zinc_rate(inv) -> float:
    return M.num(settings(inv).get("zincRate")) or ZINC_RATE


def set_zinc_rate(inv, rate: float):
    if inv.syncMeta is None or not isinstance(inv.syncMeta, dict):
        inv.syncMeta = {}
    inv.syncMeta.setdefault("finishing", {})["zincRate"] = round(float(rate), 4)


def kind_of(p) -> str | None:
    proc = (R.fin_of(p).get("process") or "").lower()
    if "zinc" in proc or proc in ("zp", "zinc plate", "zinc plated"):
        return ZINC
    if "powder" in proc or proc in ("pc",):
        return POWDER
    return None


def set_kind(p, kind: str | None):
    """Put a part on (or take it off) a finishing list — keeps the finisher and spec it already had."""
    f = dict(R.fin_of(p))
    if kind is None:
        f.pop("process", None)
    else:
        f["process"] = PROCESS_NAME[kind]
    p["finish"] = f if f else None


def weight(p) -> float:
    return M.num(p.get("weightLb"))


def last_quote(p) -> dict | None:
    q = [x for x in (p.get("pcQuotes") or []) if x.get("price") not in (None, "")]
    return q[-1] if q else None


def add_quote(inv, p, price: float, vendor: str = "", currency: str = "CAD", ref: str = "", date: str = ""):
    q = {"price": round(float(price), 4), "currency": currency or "CAD", "vendor": vendor or R.fin_of(p).get("vendor")
         or "", "date": date or M.today(), "by": inv.userName or "", "ref": ref}
    p.setdefault("pcQuotes", []).append(q)
    p["updatedAt"] = M.now_ms()
    return q


# ------------------------------------------------------------------ what's being finished
def build_qty(inv, source: dict) -> dict:
    """{partId: qty} for a source: {"kind": "project", "id", "units"} or {"kind": "production", "id"}.
    Every BOM level: a plated bracket inside a weldment counts once per weldment."""
    if source.get("kind") == "production":
        o = next((x for x in inv.productionOrders if x.get("id") == source.get("id")), None)
        if o is None:
            return {}
        pid, units, cfg = o.get("projectId"), M.num(o.get("qty")) or 1, o.get("config")
    else:
        pid, units, cfg = source.get("id"), M.num(source.get("units")) or 1, source.get("config")
    if not pid:
        return {}
    req = R.explode_many(inv, R.project_demands(inv, pid, units, cfg), False)
    return {k: e["gross"] for k, e in req.items() if e["gross"] > 0}


def _sort(p):
    return (p.get("partNumber") or "~").upper(), p["name"].lower()


def candidates(inv, source: dict) -> list:
    """MFG parts in the build — what can be put on a finishing list."""
    qty = build_qty(inv, source)
    out = [(inv.part(k), q) for k, q in qty.items() if inv.part(k) and inv.part(k).get("type") == "manufactured"]
    return sorted(out, key=lambda x: _sort(x[0]))


def zinc_rows(inv, source: dict) -> dict:
    rate = zinc_rate(inv)
    rows = []
    for p, q in candidates(inv, source):
        if kind_of(p) != ZINC:
            continue
        w = weight(p)
        rows.append({"part": p, "qty": q, "weight": w, "total": round(w * q, 4), "rate": rate,
                     "cost": round(w * q * rate, 2), "missing": w <= 0})
    for i, r in enumerate(rows, 1):
        r["item"] = i
    return {"rows": rows, "rate": rate, "totalLb": round(sum(r["total"] for r in rows), 3),
            "total": round(sum(r["cost"] for r in rows), 2), "missing": sum(1 for r in rows if r["missing"])}


def powder_rows(inv, source: dict) -> dict:
    rows = []
    for p, q in candidates(inv, source):
        if kind_of(p) != POWDER:
            continue
        lq = last_quote(p)
        price = inv.to_cad(lq["price"], lq.get("currency")) if lq else None
        rows.append({"part": p, "qty": q, "spec": R.fin_of(p).get("spec") or "", "quote": lq,
                     "price": price, "cost": round(price * q, 2) if price is not None else None,
                     "vendor": (lq or {}).get("vendor") or R.fin_of(p).get("vendor") or ""})
    for i, r in enumerate(rows, 1):
        r["item"] = i
    quoted = [r for r in rows if r["price"] is not None]
    return {"rows": rows, "total": round(sum(r["cost"] for r in quoted), 2), "needQuote": len(rows) - len(quoted)}


# ------------------------------------------------------------------ copy / export (same columns as the plater's sheet)
ZINC_COLS = ["Item No", "Part Number", "Description", "Qty", "Weight (lb)", "Total (lb)", "Rate ($/lb)", "Cost"]
POWDER_COLS = ["Item No", "Part Number", "Description", "Qty", "Colour / Spec", "Price / Part", "Cost", "Quote Date",
               "Vendor"]


def zinc_table(z) -> list:
    return [[r["item"], r["part"].get("partNumber") or "", r["part"]["name"], r["qty"], r["weight"] or None,
             r["total"] or None, r["rate"], r["cost"] if not r["missing"] else None] for r in z["rows"]]


def powder_table(pc) -> list:
    return [[r["item"], r["part"].get("partNumber") or "", r["part"]["name"], r["qty"], r["spec"],
             r["price"], r["cost"], (r["quote"] or {}).get("date") or "", r["vendor"]] for r in pc["rows"]]


def write_xlsx(path, title: str, z: dict | None, pc: dict | None):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    head, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2B3A4A")
    for name, cols, rows, total, label in (
            ("Zinc Plating", ZINC_COLS, zinc_table(z) if z else None, (z or {}).get("total"), "Zinc Plating Total"),
            ("Powder Coating", POWDER_COLS, powder_table(pc) if pc else None, (pc or {}).get("total"),
             "Powder Coating Total")):
        if rows is None:
            continue
        ws = wb.create_sheet(name)
        ws["A1"] = f"{title} — {name}"
        ws["A1"].font = Font(bold=True, size=13)
        for j, c in enumerate(cols, 1):
            cell = ws.cell(row=3, column=j, value=c)
            cell.font, cell.fill = head, fill
        for i, r in enumerate(rows, 4):
            for j, v in enumerate(r, 1):
                cell = ws.cell(row=i, column=j, value=v)
                if cols[j - 1] in ("Rate ($/lb)", "Cost", "Price / Part") and isinstance(v, (int, float)):
                    cell.number_format = '"$"#,##0.00'
        end = 4 + len(rows)
        ws.cell(row=end + 1, column=len(cols) - (2 if name == "Powder Coating" else 1), value=label).font = \
            Font(bold=True)
        c = ws.cell(row=end + 1, column=len(cols) - (1 if name == "Powder Coating" else 0), value=total)
        c.font, c.number_format = Font(bold=True), '"$"#,##0.00'
        for j, w in enumerate((8, 14, 34, 7, 12, 12, 12, 12, 18), 1):
            if j <= len(cols):
                ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = w
        ws.freeze_panes = "A4"
    wb.save(path)
    return path
