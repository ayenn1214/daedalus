"""Icarus tools for DIMS (Daedalus Inventory Management System): look up and change the shop inventory, record
stock, orders, projects, production, revisions, and pull the reports (trends, costs, vendor prices, production
history, binning list). Qt-free; the host gives a store getter and a commit callback (save + refresh + undo)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Callable

from ..inventory import analytics as A
from ..inventory import model as M

_S = {"type": "string"}
_N = {"type": "number"}
_B = {"type": "boolean"}

PART_FIELDS = {"name": _S, "type": {"type": "string", "enum": ["fastener", "purchased", "manufactured"]},
               "partNumber": _S, "qbPartNumber": _S, "category": _S, "size": _S, "length": _S, "headStyle": _S,
               "threadType": _S, "threadStandard": _S, "unit": _S, "reorderPoint": _N, "reorderQty": _N,
               "unitCost": _N, "unitCostCurrency": {"type": "string", "enum": ["CAD", "USD"]}, "notes": _S,
               "drawingUrl": _S, "cadFile": _S, "imageFile": _S, "revision": _S, "location": _S,
               "suppliers": {"type": "array", "items": {"type": "object", "properties": {
                   "name": _S, "price": _N, "currency": _S, "partNumber": _S, "website": _S}}}}

DIMS_TOOLS = [
    {"name": "dims_summary",
     "description": "Inventory overview: part counts by type, stock value, low stock, cross-project shortages, open "
                    "part orders, production orders in progress, discontinued parts, weekly backup status, the "
                    "data location and the user's role.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "dims_search_parts",
     "description": "Find parts. query matches name, part number, QuickBooks #, supplier, size, category, location. "
                    "Filters: type, project (name), vendor, rack, low_only (at/below reorder point), discontinued "
                    "(true = only discontinued, false = hide them). sort: name|stock|value|vendor|location.",
     "input_schema": {"type": "object", "properties": {
         "query": _S, "type": _S, "project": _S, "vendor": _S, "rack": _S, "low_only": _B, "discontinued": _B,
         "sort": _S, "limit": _N}}},
    {"name": "dims_get_part",
     "description": "Everything about one part (id, exact name or part number): spec, stock, location, suppliers "
                    "and prices, project allocations and needs, open orders, last transactions, usage trend, "
                    "revision status, discontinued / replacement.",
     "input_schema": {"type": "object", "properties": {"part": _S}, "required": ["part"]}},
    {"name": "dims_list",
     "description": "List records: projects | vendors | locations | production_orders | clients | client_orders | "
                    "part_orders (open and recent). query filters by name.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["projects", "vendors", "locations", "production_orders", "clients",
                                             "client_orders", "part_orders"]}, "query": _S},
         "required": ["kind"]}},
    {"name": "dims_reorder_list",
     "description": "Parts at/below their reorder point with suggested order quantity, cheapest supplier, price and "
                    "estimated cost; optional project / vendor filter. Also open orders already placed.",
     "input_schema": {"type": "object", "properties": {"project": _S, "vendor": _S}}},
    {"name": "dims_shortages",
     "description": "Cross-project shortages: total demand of every project a part is on vs stock.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "dims_project",
     "description": "One project's bill of parts: qty per unit, units, needed, on hand, short, cost per unit and "
                    "total, production orders.",
     "input_schema": {"type": "object", "properties": {"project": _S}, "required": ["project"]}},
    {"name": "dims_trends",
     "description": "Usage trends per month over `months` (default 12): used / received per month, per-part "
                    "average, trend %/month, months of stock left, run-out date, suggested reorder point. "
                    "part = one part only.",
     "input_schema": {"type": "object", "properties": {"months": _N, "part": _S, "limit": _N}}},
    {"name": "dims_cost_analysis",
     "description": "Stock value by type / vendor / category / rack, monthly used vs received cost, most valuable "
                    "stock, project cost per unit and cost to cover shortages.",
     "input_schema": {"type": "object", "properties": {"months": _N}}},
    {"name": "dims_vendor_prices",
     "description": "Vendor price cross-reference: parts quoted by 2+ suppliers (cheapest, spread, savings / yr at "
                    "last year's usage) and each vendor's average premium over the cheapest.",
     "input_schema": {"type": "object", "properties": {"vendor": _S, "limit": _N}}},
    {"name": "dims_production_history",
     "description": "Completed production orders between start and end (YYYY-MM-DD, default last 12 months): "
                    "orders, units per project and month, on-time %, lead times, and the parts those builds used.",
     "input_schema": {"type": "object", "properties": {"start": _S, "end": _S, "project": _S}}},
    {"name": "dims_revision_check",
     "description": "Manufactured parts vs their CAD model / drawing files: newer 'Rev' files in the folder, files "
                    "changed since checked in, revision letters that don't match the inventory.",
     "input_schema": {"type": "object", "properties": {"only_problems": _B}}},
    {"name": "dims_binning_list",
     "description": "Bins in rack / shelf / position order with the parts and stock in each (for stocking or a "
                    "count). rack filters; low_only = only bins at/below reorder point.",
     "input_schema": {"type": "object", "properties": {"rack": _S, "low_only": _B, "include_empty": _B}}},
    {"name": "dims_activity",
     "description": "Stock movements (newest first): filter by part, user (by), mode usage|receiving, since "
                    "(YYYY-MM-DD).",
     "input_schema": {"type": "object", "properties": {"part": _S, "by": _S, "mode": _S, "since": _S, "limit": _N}}},
    {"name": "dims_export",
     "description": "Write a report to Excel: inventory | reorder | trends | costs | vendor_prices | production | "
                    "binning | revisions | discontinued | backup (all sheets). Returns the file path (Documents/DIMS "
                    "unless path is given).",
     "input_schema": {"type": "object", "properties": {
         "report": {"type": "string", "enum": ["inventory", "reorder", "trends", "costs", "vendor_prices",
                                               "production", "binning", "revisions", "discontinued", "backup"]},
         "path": _S}, "required": ["report"]}},
    # ---- changes (only when the user asks)
    {"name": "dims_record",
     "description": "Record stock used or received for a part — only when the user asks. mode usage|receiving, qty, "
                    "note. Returns the new stock.",
     "input_schema": {"type": "object", "properties": {"part": _S, "mode": _S, "qty": _N, "note": _S},
                      "required": ["part", "mode", "qty"]}},
    {"name": "dims_count",
     "description": "Set a part's counted stock after a physical count (records the difference as an adjustment) — "
                    "only when the user asks.",
     "input_schema": {"type": "object", "properties": {"part": _S, "counted": _N, "note": _S},
                      "required": ["part", "counted"]}},
    {"name": "dims_add_part",
     "description": "Add a part — only when the user asks. Fastener names are generated from size / standard / head "
                    "/ length when name is omitted. location = a bin label like 'Rack A · Shelf 2 · Bin 3' or serial "
                    "'A2-03'. Warns instead of adding when a part with the same name or part number exists.",
     "input_schema": {"type": "object", "properties": {**PART_FIELDS, "currentStock": _N, "allow_duplicate": _B}}},
    {"name": "dims_update_part",
     "description": "Change fields of a part — only when the user asks (not stock: use dims_record / dims_count).",
     "input_schema": {"type": "object", "properties": {"part": _S, "fields": {"type": "object",
                                                                             "properties": PART_FIELDS}},
                      "required": ["part", "fields"]}},
    {"name": "dims_discontinue",
     "description": "Mark a part discontinued (or active again with discontinued=false), with an optional "
                    "replacement part and note — only when the user asks. Discontinued parts are never reordered.",
     "input_schema": {"type": "object", "properties": {"part": _S, "discontinued": _B, "replacement": _S,
                                                       "note": _S}, "required": ["part"]}},
    {"name": "dims_place_order",
     "description": "Log a part order (from the cheapest supplier) — only when the user asks. expected = "
                    "YYYY-MM-DD, default today + vendor lead time.",
     "input_schema": {"type": "object", "properties": {"part": _S, "qty": _N, "expected": _S},
                      "required": ["part", "qty"]}},
    {"name": "dims_receive_order",
     "description": "Receive an open part order (order id, or the part's oldest open order) — adds the stock. Only "
                    "when the user asks.",
     "input_schema": {"type": "object", "properties": {"order": _S, "part": _S}}},
    {"name": "dims_project_edit",
     "description": "Create a project or set its units (Total Qty Needed); allocate parts to it with qty per unit "
                    "(qty 0 removes) — only when the user asks.",
     "input_schema": {"type": "object", "properties": {
         "project": _S, "units": _N, "archived": _B,
         "allocate": {"type": "array", "items": {"type": "object", "properties": {"part": _S, "qty": _N}}}},
         "required": ["project"]}},
    {"name": "dims_production_order",
     "description": "Add or update a production order (id to update) — only when the user asks. status Not Started|"
                    "In Fab|In Finishing|Complete; dates YYYY-MM-DD.",
     "input_schema": {"type": "object", "properties": {
         "id": _S, "project": _S, "qty": _N, "status": _S, "orderDate": _S, "dueDate": _S, "fabStart": _S,
         "fabEnd": _S, "finishStart": _S, "finishEnd": _S}}},
    {"name": "dims_check_in_revision",
     "description": "Record a manufactured part's current model / drawing files as its revision (newest=true first "
                    "switches to the newest Rev files in the folder) — only when the user asks.",
     "input_schema": {"type": "object", "properties": {"part": _S, "revision": _S, "newest": _B},
                      "required": ["part"]}},
]
DIMS_NAMES = {t["name"] for t in DIMS_TOOLS}

DIMS_SYSTEM = """You are Icarus, the assistant inside DIMS — the Daedalus Inventory Management System a fabrication
shop uses for fasteners, purchased parts and manufactured parts: stock and bins, projects (bills of parts), production
orders, part orders, vendors and clients.

- Look things up with the dims_* tools before answering; never guess stock, prices or locations. Quote numbers with
  units and currency (costs are CAD unless a supplier price says USD).
- Change data (record usage/receiving, counts, parts, orders, projects, production orders, revisions, discontinued)
  only when the user asks for that change. If the part is ambiguous, list the candidates and ask. Each change is
  saved to the shared inventory immediately and appears in the activity log under the user's name; it can be
  undone from the toast in the app.
- The user's role limits what can be saved (viewer: nothing; floor: stock movements only). If a change is refused,
  say so plainly.
- Reports (trends, cost analysis, vendor prices, production history, binning list, revision check) can be exported
  to Excel with dims_export — offer it when a table is long.
- Fastener names follow the shop format, e.g. '5/8-11 UNC x 2" HHCS FT', 'M8 Hex Nut'.
- Answer briefly: the figure or list the user needs, then one useful next step if there is one. Use short tables for
  lists of parts. No preamble."""

DIMS_EXAMPLES = [
    "What's low on stock, and what would it cost to reorder from the cheapest vendors?",
    "Which fasteners are we using faster than last quarter? When do they run out?",
    "Received 200 of 3/8-16 UNC x 1\" HHCS — PO 4471",
    "Compare vendor prices: where are we overpaying?",
    "Production history for the last 6 months, by project",
    "Any manufactured parts with a newer drawing revision?",
]


def _j(o):
    return json.dumps(o, default=str)


def _part_brief(inv, p):
    best = inv.best_supplier(p)
    return {"id": p["id"], "name": p["name"], "type": p.get("type"), "partNumber": p.get("partNumber") or "",
            "stock": M.num(p.get("currentStock")), "unit": p.get("unit") or "ea",
            "reorderPoint": M.num(p.get("reorderPoint")), "location": inv.location_label_for(p.get("locationId")),
            "unitCostCAD": round(A.unit_cad(inv, p), 4), "bestSupplier": best["name"] if best else "",
            "projects": inv.project_chips(p), **({"discontinued": True} if p.get("discontinued") else {})}


class DimsToolExecutor:
    """Runs dims_* tools. store_fn() → the live store; commit(msg, undo) saves + refreshes (the inventory page's
    commit) — without a page the store is saved directly."""

    def __init__(self, store_fn: Callable, commit: Callable | None = None):
        self.store_fn = store_fn
        self.commit_fn = commit

    @property
    def store(self):
        return self.store_fn()

    @property
    def inv(self) -> M.Inventory:
        return self.store.inv

    def model_state(self):
        return None

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            fn = getattr(self, "_" + name)
        except AttributeError:
            return f"Unknown tool {name}", True
        try:
            return fn(dict(args or {})), False
        except (ValueError, KeyError, LookupError) as e:
            return f"{e}", True
        except Exception as e:  # noqa: BLE001
            return f"{e.__class__.__name__}: {e}", True

    # ------------------------------------------------------------ helpers
    def _commit(self, msg, undo=None):
        if self.commit_fn is not None:
            self.commit_fn(msg, undo)
        else:
            self.store.save()

    def find_part(self, ref: str):
        ref = str(ref or "").strip()
        if not ref:
            raise ValueError("Which part? Give its name, part number or id")
        inv = self.inv
        p = inv.part(ref)
        if p:
            return p
        low = ref.lower()
        for key in ("name", "partNumber", "qbPartNumber"):
            hits = [x for x in inv.parts if str(x.get(key) or "").strip().lower() == low]
            if len(hits) == 1:
                return hits[0]
        words = low.replace('"', " ").split()
        hits = [x for x in inv.parts if all(w in " ".join(str(x.get(k) or "") for k in (
            "name", "partNumber", "qbPartNumber", "size", "category")).lower().replace('"', " ") for w in words)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LookupError(f"No part matches '{ref}'")
        raise LookupError(f"'{ref}' matches {len(hits)} parts — which one? " +
                          "; ".join(f"{x['name']} (id {x['id']})" for x in hits[:12]))

    def find_project(self, ref: str, create=False):
        ref = str(ref or "").strip()
        inv = self.inv
        pr = inv.project(ref) or next((x for x in inv.projects if x["name"].lower() == ref.lower()), None)
        if pr is None:
            hits = [x for x in inv.projects if ref.lower() in x["name"].lower()]
            if len(hits) == 1:
                pr = hits[0]
            elif len(hits) > 1 and not create:
                raise LookupError(f"'{ref}' matches projects: " + ", ".join(x["name"] for x in hits))
        if pr is None and create:
            pid = inv.find_or_create_project(ref)
            pr = inv.project(pid)
        if pr is None:
            raise LookupError(f"No project '{ref}'")
        return pr

    def _location_id(self, ref):
        if not ref:
            return None
        ref = ref.strip().lower()
        for lid, lab in self.inv.location_options():
            loc = self.inv.location(lid)
            serial = M.generate_bin_serial(loc.get("rack"), loc.get("shelf"), loc.get("binPosition")).lower()
            sec = lid.split("::")[1] if "::" in lid else ""
            if ref in (lab.lower(), serial + (f".{sec}" if sec else ""), lid.lower()):
                return lid
        raise LookupError(f"No bin '{ref}' — use a label like 'Rack A · Shelf 2 · Bin 3' or 'A2-03'")

    # ------------------------------------------------------------ read
    def _dims_summary(self, a):
        inv, st = self.inv, self.store
        from ..inventory import backup as BK
        prod = [o for o in inv.productionOrders if o.get("status") != "Complete"]
        return _j({"parts": len(inv.parts), "by_type": {k: sum(p.get("type") == k for p in inv.parts)
                                                        for k, _l in M.TYPES},
                   "stock_value_cad": round(inv.stock_value(), 2), "low_stock": len(inv.low_stock()),
                   "shortages": len(inv.shortages()), "open_part_orders": len(inv.open_part_orders()),
                   "production_in_progress": [{"project": inv.project_name(o.get("projectId")), "qty": o.get("qty"),
                                               "status": o.get("status"), "due": o.get("dueDate")} for o in prod],
                   "active_projects": [p["name"] for p in inv.active_projects()],
                   "discontinued": sum(1 for p in inv.parts if p.get("discontinued")),
                   "weekly_backup_due": BK.is_due(inv.syncMeta or {}),
                   "data": st.location(), "role": getattr(st, "role", None) or "owner (file)",
                   "user": inv.userName})

    def _dims_search_parts(self, a):
        inv = self.inv
        lst = list(inv.parts)
        if a.get("type"):
            t = M.Inventory.norm_type(a["type"])
            lst = [p for p in lst if p.get("type") == t]
        if a.get("project"):
            pr = self.find_project(a["project"])
            lst = [p for p in lst if inv.allocation(p, pr["id"])]
        if a.get("vendor"):
            v = a["vendor"].lower()
            lst = [p for p in lst if any(v in s["name"].lower() for s in inv.suppliers(p))]
        if a.get("rack"):
            lst = [p for p in lst if (inv.location(p.get("locationId")) or {}).get("rack", "").lower()
                   == a["rack"].lower()]
        if a.get("low_only"):
            low = {p["id"] for p in inv.low_stock()}
            lst = [p for p in lst if p["id"] in low]
        if a.get("discontinued") is True:
            lst = [p for p in lst if p.get("discontinued")]
        elif a.get("discontinued") is False:
            lst = [p for p in lst if not p.get("discontinued")]
        q = (a.get("query") or "").lower().strip()
        if q:
            def hay(p):
                return " ".join(str(x) for x in (p.get("name"), p.get("partNumber"), p.get("qbPartNumber"),
                                                 p.get("size"), p.get("category"), p.get("headStyle"),
                                                 *[s["name"] for s in inv.suppliers(p)],
                                                 inv.location_label_for(p.get("locationId")))).lower()
            words = q.replace('"', " ").split()
            lst = [p for p in lst if all(w in hay(p).replace('"', " ") for w in words)]
        keys = {"stock": lambda p: M.num(p.get("currentStock")),
                "value": lambda p: -M.num(p.get("currentStock")) * A.unit_cad(inv, p),
                "vendor": lambda p: ((inv.best_supplier(p) or {}).get("name") or "~").lower(),
                "location": lambda p: inv.location_label_for(p.get("locationId")) or "~"}
        lst.sort(key=keys.get(a.get("sort") or "", lambda p: p["name"].lower()))
        n = int(a.get("limit") or 50)
        return _j({"count": len(lst), "parts": [_part_brief(inv, p) for p in lst[:n]],
                   **({"truncated": True} if len(lst) > n else {})})

    def _dims_get_part(self, a):
        inv = self.inv
        p = self.find_part(a["part"])
        ms, rows = A.trend_rows(inv, 12)
        tr = next((r for r in rows if r["id"] == p["id"]), None)
        tx = [t for t in inv.transactions if t.get("partId") == p["id"]][-15:]
        rep = A.replacement(inv, p)
        out = {**_part_brief(inv, p), "spec": inv.spec_line(p), "category": p.get("category"),
               "qbPartNumber": p.get("qbPartNumber"), "notes": p.get("notes"), "drawing": p.get("drawingUrl"),
               "cadFile": p.get("cadFile"), "reorderQty": M.num(p.get("reorderQty")),
               "unitCost": p.get("unitCost"), "unitCostCurrency": p.get("unitCostCurrency"),
               "suppliers": inv.suppliers(p),
               "allocations": [{"project": inv.project_name(x.get("projectId")), "qtyPerUnit": x.get("qty"),
                                "needed": inv.needed_for_project(p, x.get("projectId"))} for x in inv.allocations(p)],
               "totalNeeded": inv.total_needed(p),
               "openOrders": [o for o in inv.open_part_orders() if o.get("partId") == p["id"]],
               "recentTransactions": [{"when": dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
                                       "mode": t.get("mode"), "qty": t.get("qty"), "by": t.get("by"),
                                       "note": t.get("note")} for t in reversed(tx)],
               "trend": {k: tr[k] for k in ("Avg / Month", "Trend %/mo", "Months Left", "Runs Out",
                                             "Suggested Reorder Pt")} if tr else None}
        if p.get("components"):
            out["components"] = [{"part": (inv.part(c["partId"]) or {}).get("name"), "qty": c.get("qty")}
                                 for c in p["components"]]
        if p.get("type") == "manufactured":
            out["revisionCheck"] = A.version_status(p)
        if p.get("discontinued"):
            out.update(discontinuedDate=p.get("discontinuedDate"), replacement=rep["name"] if rep else None,
                       discontinuedNote=p.get("discontinuedNote"))
        return _j(out)

    def _dims_list(self, a):
        inv = self.inv
        q = (a.get("query") or "").lower()
        k = a["kind"]
        if k == "projects":
            rows = [{"name": pr["name"], "archived": bool(pr.get("archived")), **inv.project_summary(pr["id"])}
                    for pr in inv.sorted_projects()]
        elif k == "vendors":
            rows = [{**{x: v.get(x) for x in ("name", "category", "contactName", "phone", "email", "website")},
                     "leadTime": M.Inventory.lead_label(inv.lead_time_range(v["name"]))} for v in inv.sorted_vendors()]
            sc = {s["vendorName"]: s for s in inv.scorecard()}
            for r in rows:
                if r["name"] in sc:
                    r["onTimeRate%"], r["avgDays"] = sc[r["name"]]["onTimeRate"], sc[r["name"]]["avgDays"]
        elif k == "locations":
            rows = [{"name": inv.location_label(lo), "bin": M.generate_bin_serial(lo.get("rack"), lo.get("shelf"),
                                                                                  lo.get("binPosition")),
                     "parts": [p["name"] for p in inv.parts_at(lo["id"])]} for lo in inv.sorted_locations()]
        elif k == "production_orders":
            rows = [{"id": o["id"], "project": inv.project_name(o.get("projectId")),
                     **{x: o.get(x) for x in ("qty", "status", "orderDate", "dueDate", "fabStart", "fabEnd",
                                             "finishStart", "finishEnd")}}
                    for o in sorted(inv.productionOrders, key=lambda o: o.get("dueDate") or "")]
        elif k == "clients":
            rows = [{x: c.get(x) for x in ("name", "contactName", "phone", "email")} for c in inv.clients]
        elif k == "client_orders":
            rows = [{**{x: o.get(x) for x in ("id", "status", "orderDate", "dueDate", "notes")},
                     "client": (inv.client(o.get("clientId")) or {}).get("name")} for o in inv.clientOrders]
        else:
            rows = [{**o, "part": (inv.part(o.get("partId")) or {}).get("name")}
                    for o in sorted(inv.partOrders, key=lambda o: o.get("orderDate") or "", reverse=True)[:80]]
        if q:
            rows = [r for r in rows if q in json.dumps(r, default=str).lower()]
        return _j({"count": len(rows), k: rows[:120]})

    def _dims_reorder_list(self, a):
        inv = self.inv
        proj = self.find_project(a["project"])["id"] if a.get("project") else "all"
        vend = a.get("vendor") or "all"
        parts = inv.reorder_list(proj, vend)
        fn = (lambda p: inv.needed_for_project(p, proj)) if proj != "all" else inv.total_needed
        rows = inv.reorder_rows(parts, fn, None if vend == "all" else vend)
        on_order = {}
        for o in inv.open_part_orders():
            on_order[o.get("partId")] = on_order.get(o.get("partId"), 0) + M.num(o.get("qty"))
        for p, r in zip(parts, rows):
            r["On Order"] = on_order.get(p["id"], 0)
        return _j({"count": len(rows), "estimated_total_cad": round(sum(r["Est. Cost"] for r in rows), 2),
                   "rows": rows})

    def _dims_shortages(self, a):
        inv = self.inv
        return _j([{"part": r["part"]["name"], "stock": M.num(r["part"].get("currentStock")),
                    "totalNeeded": r["totalNeeded"], "short": r["short"],
                    "projects": [{"project": inv.project_name(c["projectId"]), "needed": c["needed"]}
                                 for c in r["contributing"]],
                    **({"discontinued": True} if r["part"].get("discontinued") else {})} for r in inv.shortages()])

    def _dims_project(self, a):
        inv = self.inv
        pr = self.find_project(a["project"])
        rows = []
        for p in inv.parts_for_project(pr["id"]):
            al = inv.allocation(p, pr["id"])
            need = inv.needed_for_project(p, pr["id"])
            rows.append({"part": p["name"], "qtyPerUnit": M.num(al.get("qty")), "needed": need,
                         "onHand": M.num(p.get("currentStock")), "short": max(0.0, need - M.num(p.get("currentStock"))),
                         "unitCostCAD": round(A.unit_cad(inv, p), 4),
                         **({"discontinued": True} if p.get("discontinued") else {})})
        per_unit = sum(r["qtyPerUnit"] * r["unitCostCAD"] for r in rows)
        return _j({"project": pr["name"], "units": inv.project_qty(pr["id"]), "costPerUnitCAD": round(per_unit, 2),
                   "totalCAD": round(per_unit * inv.project_qty(pr["id"]), 2), "parts": rows,
                   "productionOrders": [{x: o.get(x) for x in ("id", "qty", "status", "dueDate")}
                                        for o in inv.productionOrders if o.get("projectId") == pr["id"]]})

    def _dims_trends(self, a):
        inv = self.inv
        ms, rows = A.trend_rows(inv, int(a.get("months") or 12))
        if a.get("part"):
            p = self.find_part(a["part"])
            rows = [r for r in rows if r["id"] == p["id"]]
            flow = A.usage_by_month(inv, ms, p["id"])
        else:
            flow = A.usage_by_month(inv, ms)
        n = int(a.get("limit") or 40)
        return _j({"months": ms, "used_per_month": flow["used"], "received_per_month": flow["received"],
                   "parts": [{k: v for k, v in r.items() if k != "id"} for r in rows[:n]]})

    def _dims_cost_analysis(self, a):
        c = A.cost_summary(self.inv, int(a.get("months") or 12))
        return _j({**{k: v for k, v in c.items() if k != "flow"}, "used_cost_per_month": c["flow"]["used_cost"],
                   "received_cost_per_month": c["flow"]["received_cost"]})

    def _dims_vendor_prices(self, a):
        parts, vend = A.vendor_xref(self.inv)
        if a.get("vendor"):
            v = a["vendor"].lower()
            parts = [r for r in parts if v in r["Quotes"].lower()]
            vend = [r for r in vend if v in r["Vendor"].lower()]
        n = int(a.get("limit") or 40)
        return _j({"vendors": vend, "parts": [{k: x for k, x in r.items() if k != "id"} for r in parts[:n]],
                   "possible_savings_per_year": round(sum(r["Savings / yr"] for r in parts), 2)})

    def _audit(self):
        st = self.store
        if hasattr(st, "status_history"):
            try:
                return st.status_history("production-orders", "status", "Complete")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _dims_production_history(self, a):
        end = a.get("end") or dt.date.today().isoformat()
        start = a.get("start") or (dt.date.today() - dt.timedelta(days=365)).isoformat()
        h = A.production_history(self.inv, start, end, self._audit())
        if a.get("project"):
            name = self.find_project(a["project"])["name"]
            h["orders"] = [r for r in h["orders"] if r["Project"] == name]
            h["projects"] = [r for r in h["projects"] if r["Project"] == name]
        return _j({"start": start, "end": end, **{k: v for k, v in h.items()},
                   "orders": [{k: v for k, v in r.items() if k not in ("id", "projectId")} for r in h["orders"]]})

    def _dims_revision_check(self, a):
        rows = A.version_report(self.inv)
        if a.get("only_problems", True):
            rows = [r for r in rows if r["Status"] not in ("ok", "no-file")]
        return _j(rows)

    def _dims_binning_list(self, a):
        return _j(A.binning_rows(self.inv, a.get("rack"), bool(a.get("include_empty", False)),
                                 bool(a.get("low_only"))))

    def _dims_activity(self, a):
        inv = self.inv
        tx = list(reversed(inv.transactions))
        if a.get("part"):
            pid = self.find_part(a["part"])["id"]
            tx = [t for t in tx if t.get("partId") == pid]
        if a.get("by"):
            tx = [t for t in tx if a["by"].lower() in str(t.get("by") or "").lower()]
        if a.get("mode"):
            tx = [t for t in tx if t.get("mode") == ("usage" if a["mode"].lower().startswith("u") else "receiving")]
        if a.get("since"):
            s = M.parse_date(a["since"])
            tx = [t for t in tx if s and (A.tx_date(t) or s) >= s]
        n = int(a.get("limit") or 40)
        return _j({"count": len(tx), "entries": [
            {"when": dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%Y-%m-%d %H:%M"), "part": t.get("partName"),
             "mode": t.get("mode"), "qty": t.get("qty"), "by": t.get("by"), "note": t.get("note")} for t in tx[:n]]})

    def _dims_export(self, a):
        inv = self.inv
        rep = a["report"]
        today = dt.date.today().isoformat()
        if rep == "inventory":
            sheets = [("Inventory", inv.export_rows())]
        elif rep == "reorder":
            sheets = [("Reorder", json.loads(self._dims_reorder_list({}))["rows"])]
        elif rep == "trends":
            ms, rows = A.trend_rows(inv, 12)
            sheets = [("Trends", [{**{k: v for k, v in r.items() if k not in ("id", "series")},
                                   **dict(zip(ms, r["series"]))} for r in rows])]
        elif rep == "costs":
            c = A.cost_summary(inv)
            sheets = [("Top Parts", c["top_parts"]), ("Projects", c["projects"]),
                      ("By Vendor", [{"Vendor": k, "Value": v} for k, v in c["by_vendor"]])]
        elif rep == "vendor_prices":
            parts, vend = A.vendor_xref(inv)
            sheets = [("Parts", [{k: v for k, v in r.items() if k != "id"} for r in parts]), ("Vendors", vend)]
        elif rep == "production":
            h = A.production_history(inv, None, None, self._audit())
            sheets = [("Orders", [{k: v for k, v in r.items() if k not in ("id", "projectId")} for r in h["orders"]]),
                      ("By Project", h["projects"]), ("Parts Used", h["parts"])]
        elif rep == "binning":
            sheets = [("Binning list", A.binning_rows(inv))]
        elif rep == "revisions":
            sheets = [("Revisions", [{k: v for k, v in r.items() if k != "id"} for r in A.version_report(inv)])]
        elif rep == "discontinued":
            sheets = [("Discontinued", A.discontinued_rows(inv))]
        else:
            from ..inventory import backup as BK
            path = a.get("path") or str(_out_dir() / BK.default_name())
            BK.write_backup(inv, path)
            return _j({"ok": True, "path": path})
        path = a.get("path") or str(_out_dir() / f"DIMS-{rep.replace('_', '-')}-{today}.xlsx")
        M.write_xlsx(path, sheets)
        return _j({"ok": True, "path": path, "rows": sum(len(r) for _n, r in sheets)})

    # ------------------------------------------------------------ write
    def _dims_record(self, a):
        p = self.find_part(a["part"])
        mode = "usage" if str(a["mode"]).lower().startswith(("u", "use", "out")) else "receiving"
        qty = float(a["qty"])
        tx = self.inv.record(p["id"], mode, qty, a.get("note") or "via Icarus")
        self._commit(f"{'Used' if mode == 'usage' else 'Received'} {qty:g} × {p['name']}",
                     lambda: (self.inv.undo_transaction(tx), self._commit("Undone")))
        return _j({"ok": True, "part": p["name"], "mode": mode, "qty": qty, "stock": M.num(p.get("currentStock"))})

    def _dims_count(self, a):
        p = self.find_part(a["part"])
        diff = float(a["counted"]) - M.num(p.get("currentStock"))
        if diff == 0:
            return _j({"ok": True, "part": p["name"], "stock": M.num(p.get("currentStock")), "changed": False})
        tx = self.inv.record(p["id"], "receiving" if diff > 0 else "usage", abs(diff),
                             (a.get("note") or "Physical count") + f" (counted {float(a['counted']):g})")
        self._commit(f"{p['name']} counted: {float(a['counted']):g}",
                     lambda: (self.inv.undo_transaction(tx), self._commit("Undone")))
        return _j({"ok": True, "part": p["name"], "adjustment": diff, "stock": M.num(p.get("currentStock"))})

    def _apply_fields(self, p, f):
        f = dict(f)
        if "location" in f:
            p["locationId"] = self._location_id(f.pop("location"))
        if "type" in f:
            f["type"] = M.Inventory.norm_type(f["type"])
        if "revision" in f:
            f["revision"] = str(f["revision"]).strip().upper()
        if "suppliers" in f:
            sups = [{"name": s.get("name", ""), "price": s.get("price", ""), "currency": s.get("currency") or "CAD",
                     "partNumber": s.get("partNumber", ""), "website": s.get("website", "")}
                    for s in f.pop("suppliers")][:3]
            p["suppliers"] = sups + [{}] * (3 - len(sups))
            for s in sups:
                self.inv.ensure_vendor(s["name"])
        for k, v in f.items():
            if k in PART_FIELDS or k == "currentStock":
                p[k] = v
        if p.get("type") == "fastener" and not f.get("name") and p.get("size"):
            n = M.fastener_name(p.get("size", ""), p.get("threadStandard", ""), p.get("headStyle", ""),
                                p.get("threadType", ""), p.get("category", ""), p.get("length", ""))
            if n and not p.get("name"):
                p["name"] = n
        p["updatedAt"] = M.now_ms()

    def _dims_add_part(self, a):
        inv = self.inv
        allow = a.pop("allow_duplicate", False)
        p = M.new_part()
        self._apply_fields(p, a)
        if not p.get("name"):
            raise ValueError("Give the part a name (or a fastener size, standard and head style)")
        if not allow:
            dup = [x for x in inv.parts if x["name"].lower() == p["name"].lower() or
                   (p.get("partNumber") and (x.get("partNumber") or "").lower() == p["partNumber"].lower())]
            if dup:
                return _j({"ok": False, "reason": "a similar part exists — ask the user, then pass "
                                                  "allow_duplicate=true or update the existing part",
                           "existing": [_part_brief(inv, x) for x in dup]})
        inv.parts.append(p)
        self._commit(f"Added {p['name']}", lambda: (inv.parts.remove(p), self._commit("Undone")))
        return _j({"ok": True, "part": _part_brief(inv, p)})

    def _dims_update_part(self, a):
        p = self.find_part(a["part"])
        before = json.loads(json.dumps(p))
        f = {k: v for k, v in (a.get("fields") or {}).items() if k != "currentStock"}
        self._apply_fields(p, f)
        self._commit(f"Updated {p['name']}", lambda: (p.clear(), p.update(before), self._commit("Undone")))
        return _j({"ok": True, "part": _part_brief(self.inv, p), "changed": sorted(f)})

    def _dims_discontinue(self, a):
        p = self.find_part(a["part"])
        on = a.get("discontinued", True)
        before = {k: p.get(k) for k in ("discontinued", "discontinuedDate", "replacementId", "discontinuedNote")}
        p["discontinued"] = bool(on)
        if on:
            p["discontinuedDate"] = p.get("discontinuedDate") or dt.date.today().isoformat()
            if a.get("replacement"):
                r = self.find_part(a["replacement"])
                if r["id"] == p["id"]:
                    raise ValueError("A part can't replace itself")
                p["replacementId"] = r["id"]
            if a.get("note") is not None:
                p["discontinuedNote"] = a["note"]
        p["updatedAt"] = M.now_ms()
        self._commit(f"{p['name']} {'discontinued' if on else 'active again'}",
                     lambda: (p.update(before), self._commit("Undone")))
        rep = A.replacement(self.inv, p)
        return _j({"ok": True, "part": p["name"], "discontinued": p["discontinued"],
                   "replacement": rep["name"] if rep else None, "stillNeeded": self.inv.total_needed(p)})

    def _dims_place_order(self, a):
        inv = self.inv
        p = self.find_part(a["part"])
        best = inv.best_supplier(p)
        lead = inv.lead_time(best["name"]) if best else None
        exp = a.get("expected") or (M.add_days(M.today(), lead) if lead else "")
        o = inv.place_order(p["id"], float(a["qty"]), M.today(), exp)
        self._commit(f"Ordered {float(a['qty']):g} × {p['name']}",
                     lambda: (inv.partOrders.remove(o), self._commit("Undone")))
        return _j({"ok": True, "order": o, "part": p["name"]})

    def _dims_receive_order(self, a):
        inv = self.inv
        if a.get("order"):
            o = next((x for x in inv.partOrders if x["id"] == a["order"]), None)
        else:
            pid = self.find_part(a["part"])["id"]
            o = next((x for x in inv.open_part_orders() if x.get("partId") == pid), None)
        if o is None or o.get("status") != "Ordered":
            raise LookupError("No open order found")
        inv.receive_order(o["id"])
        p = inv.part(o.get("partId"))
        self._commit(f"Received order: {o.get('qty')} × {(p or {}).get('name', '?')}")
        return _j({"ok": True, "order": o, "stock": M.num((p or {}).get("currentStock"))})

    def _dims_project_edit(self, a):
        inv = self.inv
        pr = self.find_project(a["project"], create=True)
        if a.get("units") is not None:
            pr["totalQtyNeeded"] = float(a["units"])
        if a.get("archived") is not None:
            pr["archived"] = bool(a["archived"])
        done = []
        for al in a.get("allocate") or []:
            p = self.find_part(al["part"])
            q = float(al.get("qty") or 0)
            if q:
                inv.set_allocation(p, pr["id"], q)
            else:
                inv.unassign(p, pr["id"])
            p["updatedAt"] = M.now_ms()
            done.append({"part": p["name"], "qty": q})
        pr["updatedAt"] = M.now_ms()
        self._commit(f"Project {pr['name']} updated")
        return _j({"ok": True, "project": pr["name"], "units": inv.project_qty(pr["id"]), "allocated": done})

    def _dims_production_order(self, a):
        inv = self.inv
        o = next((x for x in inv.productionOrders if x["id"] == a.get("id")), None) if a.get("id") else None
        if o is None:
            if not a.get("project"):
                raise ValueError("Which project is the production order for?")
            o = {"id": M.uid(), "projectId": self.find_project(a["project"])["id"], "qty": 1, "status": "Not Started",
                 "orderDate": M.today()}
            inv.productionOrders.append(o)
        elif a.get("project"):
            o["projectId"] = self.find_project(a["project"])["id"]
        if a.get("status") and a["status"] not in M.ORDER_STATUSES:
            raise ValueError(f"status must be one of {M.ORDER_STATUSES}")
        for k in ("qty", "status", "orderDate", "dueDate", "fabStart", "fabEnd", "finishStart", "finishEnd"):
            if a.get(k) is not None:
                o[k] = a[k]
        self._commit(f"Production order saved ({inv.project_name(o['projectId'])})")
        return _j({"ok": True, "order": o})

    def _dims_check_in_revision(self, a):
        p = self.find_part(a["part"])
        if p.get("type") != "manufactured":
            raise ValueError(f"{p['name']} isn't a manufactured part")
        A.accept_version(p, a.get("revision"), bool(a.get("newest")))
        p["updatedAt"] = M.now_ms()
        self._commit(f"{p['name']} checked in at Rev {p.get('revision') or '—'}")
        return _j({"ok": True, "part": p["name"], "revision": p.get("revision"), "check": A.version_status(p)})


def _out_dir() -> Path:
    d = Path(os.path.expanduser("~")) / "Documents" / "DIMS"
    d.mkdir(parents=True, exist_ok=True)
    return d
