"""DIMS reports (Qt-free): usage trends, cost analysis, vendor price cross-reference, production history, the
binning list, discontinued parts and the manufactured-part revision check.

Costs use each part's current unit cost (CAD) — the inventory keeps one cost per part, not a price history."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
from pathlib import Path

from .model import TYPE_LABEL, Inventory, generate_bin_serial, num, parse_date, today


# ------------------------------------------------------------------ periods
def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def last_months(n: int, end: dt.date | None = None) -> list[str]:
    """['2026-01', …, '2026-11'] — the n months ending with end's month (oldest first)."""
    end = end or dt.date.today()
    y, m = end.year, end.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def tx_date(t) -> dt.date | None:
    try:
        return dt.datetime.fromtimestamp(num(t.get("ts")) / 1000).date()
    except (OverflowError, OSError, ValueError):
        return None


def unit_cad(inv: Inventory, p) -> float:
    return inv.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))


# ------------------------------------------------------------------ discontinued
def is_discontinued(p) -> bool:
    return bool(p.get("discontinued"))


def replacement(inv: Inventory, p):
    return inv.part(p.get("replacementId")) if p.get("replacementId") else None


def discontinued_rows(inv: Inventory):
    """Discontinued parts still in stock or still needed by a project — what to use up or replace."""
    out = []
    for p in inv.parts:
        if not is_discontinued(p):
            continue
        need = inv.total_needed(p)
        rep = replacement(inv, p)
        out.append({"Part": p["name"], "Part Number": p.get("partNumber") or "",
                    "Discontinued": p.get("discontinuedDate") or "", "On Hand": num(p.get("currentStock")),
                    "Needed by Projects": need, "Projects": ", ".join(inv.project_chips(p)),
                    "Replacement": rep["name"] if rep else "", "Note": p.get("discontinuedNote") or ""})
    return sorted(out, key=lambda r: (-r["Needed by Projects"], r["Part"].lower()))


# ------------------------------------------------------------------ trends
def usage_by_month(inv: Inventory, months: list[str], pid=None) -> dict:
    """{'used': [...], 'received': [...], 'used_cost': [...], 'received_cost': [...]} per month."""
    idx = {m: i for i, m in enumerate(months)}
    out = {k: [0.0] * len(months) for k in ("used", "received", "used_cost", "received_cost")}
    for t in inv.transactions:
        if pid and t.get("partId") != pid:
            continue
        d = tx_date(t)
        i = idx.get(month_key(d)) if d else None
        if i is None:
            continue
        p = inv.part(t.get("partId"))
        q = num(t.get("qty"))
        c = q * (unit_cad(inv, p) if p else 0)
        if t.get("mode") == "usage":
            out["used"][i] += q
            out["used_cost"][i] += c
        elif t.get("mode") == "receiving":
            out["received"][i] += q
            out["received_cost"][i] += c
    return out


def _slope(ys):
    n = len(ys)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(ys) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (y - my) for i, y in enumerate(ys)) / den if den else 0.0


def trend_rows(inv: Inventory, months: int = 12, window: int = 6, end: dt.date | None = None):
    """Per part with any usage in the period: monthly usage, average over the last `window` months, trend
    (% change per month, least squares), months of stock left, run-out date and a suggested reorder point
    (average daily use × vendor lead time × 1.5)."""
    ms = last_months(months, end)
    per = {}
    idx = {m: i for i, m in enumerate(ms)}
    for t in inv.transactions:
        if t.get("mode") != "usage":
            continue
        d = tx_date(t)
        i = idx.get(month_key(d)) if d else None
        if i is not None:
            per.setdefault(t.get("partId"), [0.0] * len(ms))[i] += num(t.get("qty"))
    rows = []
    end = end or dt.date.today()
    for pid, series in per.items():
        p = inv.part(pid)
        if p is None:
            continue
        recent = series[-window:]
        avg = sum(recent) / len(recent)
        slope = _slope(recent)
        pct = (slope / avg * 100) if avg else 0.0
        stock = num(p.get("currentStock"))
        cover = stock / avg if avg > 0 else None
        runout = (end + dt.timedelta(days=int(cover * 30.4))).isoformat() if cover is not None else ""
        best = inv.best_supplier(p)
        lead = inv.lead_time(best["name"]) if best else None
        rp = round(avg / 30.4 * lead * 1.5) if lead and avg else None
        rows.append({"id": pid, "Part": p["name"], "Type": TYPE_LABEL.get(p.get("type"), ""),
                     "Used (period)": sum(series), "Avg / Month": round(avg, 2), "Trend %/mo": round(pct, 1),
                     "On Hand": stock, "Months Left": round(cover, 1) if cover is not None else None,
                     "Runs Out": runout, "Reorder Point": num(p.get("reorderPoint")),
                     "Suggested Reorder Pt": rp, "series": series})
    rows.sort(key=lambda r: (r["Months Left"] if r["Months Left"] is not None else 1e9, -r["Used (period)"]))
    return ms, rows


# ------------------------------------------------------------------ cost analysis
def cost_summary(inv: Inventory, months: int = 12, end: dt.date | None = None) -> dict:
    ms = last_months(months, end)
    flow = usage_by_month(inv, ms)

    def group(keyf):
        g = {}
        for p in inv.parts:
            v = num(p.get("currentStock")) * unit_cad(inv, p)
            if v:
                k = keyf(p) or "—"
                g[k] = g.get(k, 0.0) + v
        return sorted(g.items(), key=lambda kv: -kv[1])

    def vendor_of(p):
        b = inv.best_supplier(p) or (inv.suppliers(p)[0] if inv.suppliers(p) else None)
        return b["name"] if b else ""

    top = sorted(inv.parts, key=lambda p: -num(p.get("currentStock")) * unit_cad(inv, p))[:15]
    projects = []
    for pr in inv.active_projects():
        rows = [(p, num(a.get("qty"))) for p in inv.parts for a in inv.allocations(p)
                if a.get("projectId") == pr["id"]]
        per_unit = sum(unit_cad(inv, p) * q for p, q in rows)
        units = inv.project_qty(pr["id"])
        short_cost = sum(max(0.0, inv.needed_for_project(p, pr["id"]) - num(p.get("currentStock"))) * unit_cad(inv, p)
                         for p, _q in rows)
        projects.append({"Project": pr["name"], "Parts": len(rows), "Cost / Unit": per_unit, "Units": units,
                         "Total": per_unit * units, "Cost to Cover Shortages": short_cost})
    no_cost = sum(1 for p in inv.parts if not num(p.get("unitCost")) and not is_discontinued(p))
    return {"months": ms, "flow": flow, "stock_value": inv.stock_value(),
            "by_type": group(lambda p: TYPE_LABEL.get(p.get("type"), "")),
            "by_category": group(lambda p: p.get("category")), "by_vendor": group(vendor_of),
            "by_rack": group(lambda p: (inv.location(p.get("locationId")) or {}).get("rack") or "No location"),
            "top_parts": [{"Part": p["name"], "On Hand": num(p.get("currentStock")), "Unit Cost": unit_cad(inv, p),
                           "Value": num(p.get("currentStock")) * unit_cad(inv, p)} for p in top
                          if num(p.get("currentStock")) * unit_cad(inv, p) > 0],
            "projects": projects, "parts_without_cost": no_cost}


# ------------------------------------------------------------------ vendor cross-reference
def annual_usage(inv: Inventory, pid, end: dt.date | None = None) -> float:
    end = end or dt.date.today()
    start = end - dt.timedelta(days=365)
    return sum(num(t.get("qty")) for t in inv.transactions if t.get("partId") == pid and t.get("mode") == "usage"
               and (d := tx_date(t)) is not None and start < d <= end)


def vendor_xref(inv: Inventory, end: dt.date | None = None):
    """(part rows, vendor rows). Part rows: every part priced by 2+ suppliers — each supplier's price in CAD,
    the cheapest, the spread, and what buying the last 12 months' usage from the cheapest would have saved
    compared with the current unit cost. Vendor rows: how each vendor compares across the parts it quotes."""
    parts, vend = [], {}
    for p in inv.parts:
        if is_discontinued(p):
            continue
        quotes = [(s["name"], inv.to_cad(s["price"], s.get("currency")), s) for s in inv.suppliers(p)
                  if s.get("price") not in ("", None)]
        if len(quotes) < 2:
            continue
        quotes.sort(key=lambda q: q[1])
        best_name, best = quotes[0][0], quotes[0][1]
        worst = quotes[-1][1]
        cur = unit_cad(inv, p) or best
        use = annual_usage(inv, p["id"], end)
        parts.append({"id": p["id"], "Part": p["name"], "Part Number": p.get("partNumber") or "",
                      "Quotes": "; ".join(f"{n} ${c:,.2f}" for n, c, _s in quotes), "Cheapest": best_name,
                      "Cheapest Price": best, "Current Cost": cur, "Spread %": round((worst - best) / best * 100, 1)
                      if best else None, "Used / 12 mo": use, "Savings / yr": max(0.0, (cur - best) * use)})
        for n, c, _s in quotes:
            v = vend.setdefault(n, {"Vendor": n, "Parts Quoted": 0, "Cheapest On": 0, "prem": [],
                                    "Extra vs Cheapest / yr": 0.0})
            v["Parts Quoted"] += 1
            if n == best_name:
                v["Cheapest On"] += 1
            if best:
                v["prem"].append((c - best) / best * 100)
            v["Extra vs Cheapest / yr"] += (c - best) * use
    vrows = []
    for v in vend.values():
        prem = v.pop("prem")
        v["Avg Premium %"] = round(sum(prem) / len(prem), 1) if prem else 0.0
        v["Lead Time"] = Inventory.lead_label(inv.lead_time_range(v["Vendor"]))
        vrows.append(v)
    parts.sort(key=lambda r: -r["Savings / yr"])
    vrows.sort(key=lambda r: (r["Avg Premium %"], -r["Parts Quoted"]))
    return parts, vrows


# ------------------------------------------------------------------ production history
def completed_on(o, audit_done: dict | None = None) -> str:
    """When a production order was finished: the audit trail's first 'Complete', else its finishing end date,
    else the due date (for completed orders)."""
    if audit_done and audit_done.get(o.get("id")):
        return audit_done[o["id"]]
    if o.get("status") == "Complete":
        return o.get("finishEnd") or o.get("fabEnd") or o.get("dueDate") or o.get("orderDate") or ""
    return ""


def production_history(inv: Inventory, start: str | None = None, end: str | None = None,
                       audit_done: dict | None = None) -> dict:
    """Completed production orders between start and end (ISO dates, inclusive): one row per order, totals per
    project, units built per month, and the parts those builds consumed (project quantities × units built)."""
    s, e = parse_date(start) or dt.date(1970, 1, 1), parse_date(end) or dt.date(2999, 12, 31)
    rows = []
    for o in inv.productionOrders:
        done = completed_on(o, audit_done)
        d = parse_date(done)
        if d is None or not (s <= d <= e):
            continue
        late = None
        if o.get("dueDate"):
            late = (d - parse_date(o["dueDate"])).days if parse_date(o["dueDate"]) else None

        def span(a, b):
            da, db = parse_date(o.get(a)), parse_date(o.get(b))
            return (db - da).days if da and db else None
        rows.append({"id": o.get("id"), "projectId": o.get("projectId"),
                     "Project": inv.project_name(o.get("projectId")) or "(deleted project)", "Qty": num(o.get("qty")),
                     "Ordered": o.get("orderDate") or "", "Due": o.get("dueDate") or "", "Completed": done,
                     "Days Late": late, "Fab Days": span("fabStart", "fabEnd"),
                     "Finishing Days": span("finishStart", "finishEnd"),
                     "Order → Done Days": (d - parse_date(o["orderDate"])).days if parse_date(o.get("orderDate"))
                     else None})
    rows.sort(key=lambda r: r["Completed"])
    proj = {}
    for r in rows:
        x = proj.setdefault(r["Project"], {"Project": r["Project"], "Orders": 0, "Units": 0.0, "On Time": 0,
                                           "with_due": 0, "lead": []})
        x["Orders"] += 1
        x["Units"] += r["Qty"]
        if r["Days Late"] is not None:
            x["with_due"] += 1
            x["On Time"] += r["Days Late"] <= 0
        if r["Order → Done Days"] is not None:
            x["lead"].append(r["Order → Done Days"])
    prows = []
    for x in proj.values():
        lead, wd = x.pop("lead"), x.pop("with_due")
        x["On Time %"] = round(x["On Time"] / wd * 100) if wd else None
        x["Avg Order → Done Days"] = round(sum(lead) / len(lead), 1) if lead else None
        del x["On Time"]
        prows.append(x)
    prows.sort(key=lambda r: -r["Units"])
    monthly = {}
    for r in rows:
        k = r["Completed"][:7]
        monthly[k] = monthly.get(k, 0.0) + r["Qty"]
    used = {}
    for r in rows:
        for p in inv.parts:
            a = inv.allocation(p, r["projectId"])
            if a and num(a.get("qty")):
                u = used.setdefault(p["id"], {"Part": p["name"], "Part Number": p.get("partNumber") or "",
                                              "Qty Used": 0.0, "Cost": 0.0})
                q = num(a.get("qty")) * r["Qty"]
                u["Qty Used"] += q
                u["Cost"] += q * unit_cad(inv, p)
    return {"orders": rows, "projects": prows, "monthly": sorted(monthly.items()),
            "parts": sorted(used.values(), key=lambda u: -u["Cost"])}


# ------------------------------------------------------------------ binning list
def binning_rows(inv: Inventory, rack: str | None = None, include_empty: bool = True, low_only: bool = False):
    """Every bin in rack / shelf / position order with the part(s) in it — the list to stock or audit bins by."""
    rows = []
    for loc in inv.sorted_locations():
        if rack and loc.get("rack") != rack:
            continue
        serial = generate_bin_serial(loc.get("rack"), loc.get("shelf"), loc.get("binPosition"))
        secs = list("abc") if loc.get("sections") else [None]
        for sec in secs:
            parts = inv.parts_at(loc["id"], sec)
            if low_only:
                parts = [p for p in parts if num(p.get("currentStock")) <= num(p.get("reorderPoint"))]
            label = Inventory.location_label(loc) + (f".{sec}" if sec else "")
            if not parts:
                if include_empty and not low_only:
                    rows.append({"Bin": serial + (f".{sec}" if sec else ""), "Location": label, "Part": "",
                                 "Part Number": "", "Size": "", "On Hand": "", "Unit": "", "Reorder Pt": "",
                                 "Count": ""})
                continue
            for p in sorted(parts, key=lambda p: p["name"].lower()):
                rows.append({"Bin": serial + (f".{sec}" if sec else ""), "Location": label, "Part": p["name"]
                             + ("  (discontinued)" if is_discontinued(p) else ""),
                             "Part Number": p.get("partNumber") or "",
                             "Size": " ".join(x for x in (p.get("size"), p.get("length")) if x),
                             "On Hand": num(p.get("currentStock")), "Unit": p.get("unit") or "",
                             "Reorder Pt": num(p.get("reorderPoint")), "Count": ""})
    if not rack and not low_only:
        loose = [p for p in inv.parts if not p.get("locationId") and num(p.get("currentStock")) > 0]
        for p in sorted(loose, key=lambda p: p["name"].lower()):
            rows.append({"Bin": "—", "Location": "No bin assigned", "Part": p["name"],
                         "Part Number": p.get("partNumber") or "", "Size": " ".join(x for x in (p.get("size"),
                                                                                         p.get("length")) if x),
                         "On Hand": num(p.get("currentStock")), "Unit": p.get("unit") or "",
                         "Reorder Pt": num(p.get("reorderPoint")), "Count": ""})
    return rows


def binning_html(rows, title="DIMS binning list", subtitle="") -> str:
    """Printable HTML (Qt's rich text subset): grouped by rack, a blank Count column to write in."""
    cols = ["Bin", "Part", "Part Number", "Size", "On Hand", "Unit", "Reorder Pt", "Count"]
    e = html.escape
    out = [f"<h2 style='margin:0'>{e(title)}</h2><p style='color:#555;margin:2px 0 8px 0'>{e(subtitle)}</p>",
           "<table width='100%' cellspacing='0' cellpadding='3' border='1' style='border-collapse:collapse;"
           "font-size:9pt'><tr>" + "".join(f"<th align='left' bgcolor='#ddd'>{c}</th>" for c in cols) + "</tr>"]
    last = None
    for r in rows:
        rack = r["Location"].split(" · ")[0]
        if rack != last:
            out.append(f"<tr><td colspan='{len(cols)}' bgcolor='#eee'><b>{e(rack)}</b></td></tr>")
            last = rack
        cells = []
        for c in cols:
            v = r.get(c, "")
            v = f"{v:g}" if isinstance(v, float) else str(v)
            al = "right" if c in ("On Hand", "Reorder Pt") else "left"
            w = " width='60'" if c == "Count" else ""
            cells.append(f"<td align='{al}'{w}>{e(v)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


# ------------------------------------------------------------------ manufactured part revisions
_REV = re.compile(r"(?i)(?:^|[\s_\-.(\[])rev(?:ision)?[\s_\-.]*([A-Z]{1,2}|\d{1,3})(?=$|[\s_\-.)\]])")


def rev_from_name(name: str) -> str | None:
    """'BRKT-104 Rev C.pdf' → 'C', 'frame_rev2.SLDPRT' → '2'."""
    m = None
    for m in _REV.finditer(Path(name).stem):
        pass
    return m.group(1).upper() if m else None


def rev_key(r: str | None):
    """Sort key: A < B < … < Z < AA; numbers by value; letters after numbers is not assumed."""
    if not r:
        return (-1, 0)
    r = r.strip().upper()
    if r.isdigit():
        return (0, int(r))
    v = 0
    for ch in r:
        if not ch.isalpha():
            return (2, r)
        v = v * 26 + (ord(ch) - 64)
    return (1, v)


def _base(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"\s+", " ", _REV.sub(" ", stem)).strip(" _-.").lower()


def _local(path: str) -> Path | None:
    if not path or path.startswith(("http://", "https://", "mailto:")):
        return None
    return Path(path)


def file_sig(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path), "mtime": int(st.st_mtime), "size": st.st_size}


def newest_sibling(path: Path):
    """(rev, path) of the highest revision of the same document in its folder (same name apart from 'Rev X'
    and the same extension), or None."""
    try:
        sibs = [f for f in path.parent.iterdir() if f.suffix.lower() == path.suffix.lower()
                and _base(f.name) == _base(path.name) and rev_from_name(f.name)]
    except OSError:
        return None
    if not sibs:
        return None
    f = max(sibs, key=lambda f: rev_key(rev_from_name(f.name)))
    return rev_from_name(f.name), f


def version_status(p) -> dict:
    """Revision check for one manufactured part. Files: the CAD model (cadFile) and the drawing (drawingUrl).
    status: ok | changed | newer | mismatch | missing | unchecked | no-file."""
    files = [(k, _local(p.get(k) or "")) for k in ("cadFile", "drawingUrl")]
    files = [(k, f) for k, f in files if f is not None]
    rev = (p.get("revision") or "").strip().upper()
    if not files:
        return {"status": "no-file", "detail": "No CAD model or drawing file linked", "revision": rev}
    notes, status = [], "ok"
    sigs = p.get("revisionFiles") or {}
    order = ["ok", "unchecked", "changed", "mismatch", "newer", "missing"]

    def worse(s):
        nonlocal status
        if order.index(s) > order.index(status):
            status = s
    for k, f in files:
        what = "Model" if k == "cadFile" else "Drawing"
        if not f.exists():
            worse("missing")
            notes.append(f"{what} not found: {f}")
            continue
        fr = rev_from_name(f.name)
        if rev and fr and rev_key(fr) != rev_key(rev):
            worse("mismatch")
            notes.append(f"{what} file is Rev {fr}, inventory says Rev {rev}")
        nb = newest_sibling(f)
        if nb and fr and rev_key(nb[0]) > rev_key(fr):
            worse("newer")
            notes.append(f"Newer {what.lower()} in the folder: {nb[1].name} (Rev {nb[0]})")
        sig = sigs.get(k)
        if not sig:
            worse("unchecked")
            notes.append(f"{what} not checked in yet")
        elif sig.get("path") != str(f) or sig.get("mtime") != int(f.stat().st_mtime) or \
                sig.get("size") != f.stat().st_size:
            worse("changed")
            notes.append(f"{what} file changed since Rev {sig.get('revision') or rev or '?'} was checked in "
                         f"({dt.datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M})")
    return {"status": status, "detail": "; ".join(notes) or "Up to date", "revision": rev}


def accept_version(p, revision: str | None = None, newest: bool = False):
    """Check the part's current files in at `revision` (default: the file names' revision, else unchanged).
    newest=True first switches each file to the newest revision found next to it."""
    if newest:
        for k in ("cadFile", "drawingUrl"):
            f = _local(p.get(k) or "")
            nb = newest_sibling(f) if f and f.exists() else None
            if nb:
                p[k] = str(nb[1])
    found = [rev_from_name(_local(p.get(k) or "").name) for k in ("cadFile", "drawingUrl") if _local(p.get(k) or "")]
    found = [r for r in found if r]
    rev = (revision or (max(found, key=rev_key) if found else p.get("revision")) or "").strip().upper()
    p["revision"] = rev
    sigs = {}
    for k in ("cadFile", "drawingUrl"):
        f = _local(p.get(k) or "")
        if f and f.exists():
            sigs[k] = {**file_sig(f), "revision": rev}
    p["revisionFiles"] = sigs
    p["revisionCheckedAt"] = today()
    return p


def version_report(inv: Inventory):
    out = []
    for p in inv.parts:
        if p.get("type") != "manufactured" or is_discontinued(p):
            continue
        st = version_status(p)
        out.append({"id": p["id"], "Part": p["name"], "Part Number": p.get("partNumber") or "",
                    "Revision": st["revision"], "Status": st["status"], "Detail": st["detail"],
                    "Checked In": p.get("revisionCheckedAt") or ""})
    rank = {"missing": 0, "newer": 1, "mismatch": 2, "changed": 3, "unchecked": 4, "no-file": 5, "ok": 6}
    return sorted(out, key=lambda r: (rank.get(r["Status"], 9), r["Part"].lower()))


def model_file(p) -> str:
    """The part's 3D model for its thumbnail: cadFile, else a drawing link that is a model."""
    for k in ("cadFile", "drawingUrl"):
        v = p.get(k) or ""
        if os.path.splitext(v)[1].lower() in MODEL_EXTS:
            return v
    return ""


MODEL_EXTS = {".stl", ".obj", ".step", ".stp", ".daedalus", ".iges", ".igs", ".brep", ".sldprt", ".sldasm", ".3mf"}
