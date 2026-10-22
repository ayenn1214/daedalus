"""Holocron inventory — data model and business logic (Qt-free).

A Python port of the Holocron Inventory System web app. Records are plain dicts with the same field names and
the data is stored under the same keys ("parts", "projects", …) as JSON strings, so a Daedalus install and the
web version can share one Supabase table or exchange files.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import math
import random
import re
import string
import time

TYPES = [("fastener", "Fastener"), ("purchased", "Purchased Part"), ("manufactured", "Manufactured Part")]
TYPE_LABEL = dict(TYPES)
HEAD_STYLES = ["Hex", "Socket Head", "Button Head", "Flat Head", "Pan Head", "Round Head", "Carriage", "Other"]
WASHER_TYPES = ["Flat", "Split", "Lock", "Fender", "Other"]
NUT_TYPES = ["Reg", "Nyloc", "Other"]
THREAD_TYPES = ["Full Threaded", "Partial Threaded"]
THREAD_STANDARDS = ["Imperial UNC", "Imperial UNF", "Metric Coarse", "Metric Fine", "Other"]
METRIC_SIZES = ["M3", "M4", "M5", "M6", "M8", "M10", "M12", "M14", "M16", "M18", "M20", "M24"]
IMPERIAL_SIZES = ['#4', '#6', '#8', '#10', '1/4"', '5/16"', '3/8"', '7/16"', '1/2"', '9/16"', '5/8"', '3/4"', '7/8"',
                  '1"']
METRIC_COARSE_PITCH = {"M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0, "M8": 1.25, "M10": 1.5, "M12": 1.75, "M14": 2.0,
                       "M16": 2.0, "M18": 2.5, "M20": 2.5, "M24": 3.0}
METRIC_FINE_PITCH = {"M5": 0.5, "M6": 0.75, "M8": 1.0, "M10": 1.25, "M12": 1.25, "M14": 1.5, "M16": 1.5, "M18": 1.5,
                     "M20": 1.5, "M24": 2.0}
UNC_TPI = {'#4': 40, '#6': 32, '#8': 32, '#10': 24, '1/4"': 20, '5/16"': 18, '3/8"': 16, '7/16"': 14, '1/2"': 13,
           '9/16"': 12, '5/8"': 11, '3/4"': 10, '7/8"': 9, '1"': 8}
UNF_TPI = {'#4': 48, '#6': 40, '#8': 36, '#10': 32, '1/4"': 28, '5/16"': 24, '3/8"': 24, '7/16"': 20, '1/2"': 20,
           '9/16"': 18, '5/8"': 18, '3/4"': 16, '7/8"': 14, '1"': 12}
HEAD_ABBR = {"Hex": "HHCS", "Socket Head": "SHCS", "Button Head": "BHCS", "Flat Head": "FHSCS", "Pan Head": "PHMS",
             "Round Head": "RHMS", "Carriage": "CB"}
FASTENER_CATEGORIES = ["Bolt", "Screw", "Nut", "Washer", "Stud", "Pin", "Rivet", "Other"]
RACKS = ["Rack A", "Rack B"]
SHELVES = [1, 2, 3, 4, 5, 6]
BIN_SIZES = [{"id": "small", "label": 'Small Bin (3 x 9 x 4.5")', "sku": "S-16264", "maxPos": 12},
             {"id": "large", "label": 'Large Bin (6 x 9 x 4.5")', "sku": "S-2332", "maxPos": 6}]
VENDOR_CATEGORIES = ["Fabrication", "Fastener", "Finishing", "Other"]
ORDER_STATUSES = ["Not Started", "In Fab", "In Finishing", "Complete"]
CLIENT_ORDER_STATUSES = ["Requested", "Confirmed", "In Production", "Shipped", "Complete"]
PART_ORDER_STATUSES = ["Ordered", "Received", "Cancelled"]

# storage keys (same as the web app) → state attribute
KEYS = {"parts": "parts", "transactions": "transactions", "projects": "projects", "locations": "locations",
        "vendors": "vendors", "production-orders": "productionOrders", "clients": "clients",
        "client-orders": "clientOrders", "part-orders": "partOrders"}


def uid() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8)) + \
        format(int(time.time() * 1000), "x")[-4:]


def now_ms() -> int:
    return int(time.time() * 1000)


def today() -> str:
    return dt.date.today().isoformat()


def int_qty(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def num(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def fmt_money(n) -> str:
    if n is None or n == "":
        return "—"
    try:
        return f"${float(n):,.2f}"
    except (TypeError, ValueError):
        return "—"


def parse_date(s):
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def fmt_date(s) -> str:
    d = parse_date(s)
    return d.strftime("%b %d, %Y") if d else "—"


def add_days(date_str, days):
    d = parse_date(date_str)
    if d is None or days is None:
        return ""
    return (d + dt.timedelta(days=int(days))).isoformat()


def days_between(a, b):
    da, db = parse_date(a), parse_date(b)
    return None if da is None or db is None else (db - da).days


def size_callout(size: str, standard: str) -> str:
    if not size:
        return ""
    if re.match(r"^M\d", size):
        pitch = (METRIC_FINE_PITCH if standard == "Metric Fine" else METRIC_COARSE_PITCH).get(size)
        return f"{size}x{pitch:g}" if pitch else size
    tpi = (UNF_TPI if standard == "Imperial UNF" else UNC_TPI).get(size)
    tag = "UNF" if standard == "Imperial UNF" else "UNC"
    return f"{size}-{tpi} {tag}" if tpi else size


def fastener_name(size, standard, head_style, thread_type, category, length) -> str:
    """Same naming as the web app: '5/8-11 UNC x 2" HHCS FT', 'M8 Hex Nut'."""
    callout = size_callout(size, standard)
    is_metric = bool(re.match(r"^M\d", size or ""))
    length = (length or "").strip()
    ltag = f"x {length}{'mm' if is_metric else chr(34)}" if length else ""
    abbr = HEAD_ABBR.get(head_style)
    if abbr:
        return " ".join(x for x in (callout, ltag, abbr + (" FT" if thread_type == "Full Threaded" else "")) if x)
    return " ".join(x for x in (callout, head_style, category) if x)


def type_options_for_category(cat):
    if cat in ("Bolt", "Screw"):
        return HEAD_STYLES
    if cat == "Washer":
        return WASHER_TYPES
    if cat == "Nut":
        return NUT_TYPES
    return ["Other"]


def bin_positions(size_id):
    size = next((b for b in BIN_SIZES if b["id"] == size_id), None)
    return list(range(1, (size["maxPos"] if size else 12) + 1))


def generate_bin_serial(rack, shelf, pos) -> str:
    letter = re.sub(r"^Rack\s*", "", rack or "", flags=re.I).strip() or "X"
    return f"{letter}{shelf}-{int(pos or 1):02d}"


def new_part(**kw) -> dict:
    p = {"id": uid(), "type": "fastener", "name": "", "partNumber": "", "qbPartNumber": "", "drawingUrl": "",
         "notes": "", "category": "", "headStyle": "", "threadType": "", "threadStandard": "", "size": "",
         "length": "", "unit": "ea", "currentStock": 0, "reorderPoint": 0, "reorderQty": 0,
         "suppliers": [{}, {}, {}], "projectAllocations": [], "unitCost": 0, "unitCostCurrency": "CAD",
         "locationId": None, "updatedAt": now_ms()}
    p.update(kw)
    return p


class Inventory:
    """All Holocron data plus the operations the web app performs on it."""

    def __init__(self):
        self.parts: list = []
        self.transactions: list = []
        self.projects: list = []
        self.locations: list = []
        self.vendors: list = []
        self.productionOrders: list = []
        self.clients: list = []
        self.clientOrders: list = []
        self.partOrders: list = []
        self.exchangeRate = 1.35
        self.syncMeta: dict = {}
        self.userName = ""

    # ------------------------------------------------------------ (de)serialise
    def load_values(self, values: dict):
        """values: storage key → JSON string (or None)."""
        for key, attr in KEYS.items():
            raw = values.get(key)
            try:
                setattr(self, attr, json.loads(raw) if raw else [])
            except (TypeError, ValueError):
                setattr(self, attr, [])
        try:
            self.exchangeRate = float(values.get("exchange-rate") or 1.35) or 1.35
        except ValueError:
            self.exchangeRate = 1.35
        try:
            self.syncMeta = json.loads(values.get("sync-meta") or "{}")
        except ValueError:
            self.syncMeta = {}

    def dump_values(self, keys=None) -> dict:
        out = {}
        for key, attr in KEYS.items():
            if keys is None or key in keys:
                out[key] = json.dumps(getattr(self, attr), separators=(",", ":"))
        if keys is None or "exchange-rate" in keys:
            out["exchange-rate"] = str(self.exchangeRate)
        if keys is None or "sync-meta" in keys:
            out["sync-meta"] = json.dumps(self.syncMeta)
        return out

    def snapshot(self):
        return copy.deepcopy(self.dump_values())

    # ------------------------------------------------------------- money
    def to_cad(self, amount, currency):
        n = num(amount)
        return n * (self.exchangeRate or 1.35) if currency == "USD" else n

    # ------------------------------------------------------------- parts
    def part(self, pid):
        return next((p for p in self.parts if p["id"] == pid), None)

    @staticmethod
    def suppliers(p):
        return [s for s in (p.get("suppliers") or []) if s and s.get("name")]

    def best_supplier(self, p):
        sups = [s for s in self.suppliers(p) if s.get("price") not in ("", None) and
                not (isinstance(s.get("price"), float) and math.isnan(s["price"]))]
        if not sups:
            return None
        return min(sups, key=lambda s: self.to_cad(s["price"], s.get("currency")))

    @staticmethod
    def stock_ratio(p, stock=None):
        stock = num(p.get("currentStock")) if stock is None else stock
        rp = num(p.get("reorderPoint"))
        if rp <= 0:
            return 1.0 if stock > 0 else 0.0
        return max(0.0, min(1.4, stock / (rp * 1.4)))

    @staticmethod
    def level(p, stock=None) -> str:
        """'red' | 'amber' | 'green' — same thresholds as the web gauges."""
        stock = num(p.get("currentStock")) if stock is None else stock
        rp = num(p.get("reorderPoint"))
        if stock <= rp:
            return "red"
        if stock <= rp * 1.5:
            return "amber"
        return "green"

    def spec_line(self, p) -> str:
        bits = []
        if p.get("type") == "fastener":
            if p.get("size"):
                bits.append(p["size"])
            if p.get("length"):
                bits.append(f"L:{p['length']}")
            for k in ("threadStandard", "headStyle", "threadType"):
                if p.get(k):
                    bits.append(p[k])
        if p.get("partNumber"):
            bits.append(p["partNumber"])
        loc = self.location_label_for(p.get("locationId"))
        if loc:
            bits.append(loc)
        return " · ".join(bits)

    def low_stock(self):
        return [p for p in self.parts if num(p.get("currentStock")) <= num(p.get("reorderPoint"))]

    def component_ids(self) -> set:
        ids = set()
        for p in self.parts:
            for c in p.get("components") or []:
                ids.add(c.get("partId"))
        return ids

    def contains_recursive(self, part_id, target_id, visited=None) -> bool:
        visited = visited or set()
        if part_id in visited:
            return False
        visited.add(part_id)
        p = self.part(part_id)
        for c in (p or {}).get("components") or []:
            if c.get("partId") == target_id or self.contains_recursive(c.get("partId"), target_id, visited):
                return True
        return False

    def duplicate_groups(self):
        groups, seen = [], set()
        for p in self.parts:
            if p["id"] in seen:
                continue
            pn, pnum = (p.get("name") or "").strip().lower(), (p.get("partNumber") or "").strip().lower()
            matches = [q for q in self.parts if q["id"] != p["id"] and q["id"] not in seen and
                       ((pn and (q.get("name") or "").strip().lower() == pn) or
                        (pnum and (q.get("partNumber") or "").strip().lower() == pnum))]
            if matches:
                g = [p, *matches]
                seen.update(x["id"] for x in g)
                groups.append(g)
        return groups

    def merge_parts(self, keep_id, merge_id):
        keep, merge = self.part(keep_id), self.part(merge_id)
        if not keep or not merge or keep is merge:
            return
        keep["currentStock"] = num(keep.get("currentStock")) + num(merge.get("currentStock"))
        for k in ("locationId", "notes", "drawingUrl"):
            if not keep.get(k) and merge.get(k):
                keep[k] = merge[k]
        if merge.get("suppliers"):
            keep.setdefault("suppliers", [{}, {}, {}])
            while len(keep["suppliers"]) < 3:
                keep["suppliers"].append({})
            for s in merge["suppliers"]:
                if not s or not s.get("name"):
                    continue
                if any(k and (k.get("name") or "").lower() == s["name"].lower() for k in keep["suppliers"]):
                    continue
                slot = next((i for i, k in enumerate(keep["suppliers"]) if not k or not k.get("name")), -1)
                if slot >= 0:
                    keep["suppliers"][slot] = s
        keep.setdefault("projectAllocations", [])
        for a in merge.get("projectAllocations") or []:
            ex = next((k for k in keep["projectAllocations"] if k["projectId"] == a["projectId"]), None)
            if ex:
                ex["qty"] = num(ex.get("qty")) + num(a.get("qty"))
            else:
                keep["projectAllocations"].append(dict(a))
        for t in self.transactions:
            if t.get("partId") == merge_id:
                t["partId"], t["partName"] = keep_id, keep["name"]
        for o in self.partOrders:
            if o.get("partId") == merge_id:
                o["partId"] = keep_id
        for p in self.parts:
            for c in p.get("components") or []:
                if c.get("partId") == merge_id:
                    c["partId"] = keep_id
        keep["updatedAt"] = now_ms()
        self.parts = [p for p in self.parts if p["id"] != merge_id]

    def delete_part(self, pid):
        self.parts = [p for p in self.parts if p["id"] != pid]
        for p in self.parts:
            if p.get("components"):
                p["components"] = [c for c in p["components"] if c.get("partId") != pid]

    # ------------------------------------------------------------ projects
    def sorted_projects(self):
        return sorted(self.projects, key=lambda p: p.get("order", p.get("createdAt", 0)) or 0)

    def active_projects(self):
        return [p for p in self.sorted_projects() if not p.get("archived")]

    def archived_projects(self):
        return [p for p in self.sorted_projects() if p.get("archived")]

    def project(self, pid):
        return next((p for p in self.projects if p["id"] == pid), None)

    def project_name(self, pid):
        p = self.project(pid)
        return p["name"] if p else None

    def project_qty(self, pid) -> float:
        p = self.project(pid)
        v = (p or {}).get("totalQtyNeeded")
        return num(v, 1.0) if v not in (None, "") else 1.0

    @staticmethod
    def allocations(p):
        return p.get("projectAllocations") or []

    def allocation(self, p, pid):
        return next((a for a in self.allocations(p) if a.get("projectId") == pid), None)

    def parts_for_project(self, pid):
        return [p for p in self.parts if self.allocation(p, pid)]

    def project_chips(self, p):
        return [n for n in (self.project_name(a.get("projectId")) for a in self.allocations(p)) if n]

    def find_or_create_project(self, name, **extra):
        name = (name or "").strip()
        if not name:
            return None
        ex = next((p for p in self.projects if p["name"].lower() == name.lower()), None)
        if ex:
            ex.update({k: v for k, v in extra.items() if v is not None})
            return ex["id"]
        order = max([p.get("order", 0) or 0 for p in self.projects] + [0]) + 1
        pr = {"id": uid(), "name": name, "createdAt": now_ms(), "order": order, **extra}
        self.projects.append(pr)
        return pr["id"]

    def set_allocation(self, p, pid, qty):
        p.setdefault("projectAllocations", [])
        a = self.allocation(p, pid)
        if a:
            a["qty"] = qty
        else:
            p["projectAllocations"].append({"projectId": pid, "qty": qty})

    def unassign(self, p, pid):
        p["projectAllocations"] = [a for a in self.allocations(p) if a.get("projectId") != pid]

    def duplicate_project(self, pid):
        pr = self.project(pid)
        clone = {"id": uid(), "name": f"{pr['name']} (Copy)", "folderPath": "", "totalQtyNeeded": pr.get("totalQtyNeeded"),
                 "createdAt": now_ms(), "order": max([p.get("order", 0) or 0 for p in self.projects] + [0]) + 1}
        self.projects.append(clone)
        n = 0
        for p in self.parts:
            a = self.allocation(p, pid)
            if a:
                p.setdefault("projectAllocations", []).append({"projectId": clone["id"], "qty": a.get("qty")})
                n += 1
        return clone, n

    def delete_project(self, pid):
        self.projects = [p for p in self.projects if p["id"] != pid]
        for p in self.parts:
            self.unassign(p, pid)

    def move_project(self, pid, delta):
        lst = self.active_projects()
        i = next((k for k, p in enumerate(lst) if p["id"] == pid), -1)
        j = i + delta
        if i < 0 or not 0 <= j < len(lst):
            return
        a, c = lst[i], lst[j]
        ta, tc = a.get("order", i), c.get("order", j)
        a["order"], c["order"] = tc, ta

    def needed_for_project(self, p, pid) -> float:
        a = self.allocation(p, pid)
        return num(a.get("qty")) * self.project_qty(pid) if a else 0.0

    def total_needed(self, p) -> float:
        return sum(num(a.get("qty")) * self.project_qty(a.get("projectId")) for a in self.allocations(p))

    def project_rows(self, pid):
        """Rows for a project table, expanding weldments: (part, depth, qty_label, needed, on_hand, embedded)."""
        out = []
        comp = self.component_ids()

        def walk(p, depth, needed, qty_label, stock_override):
            on_hand = stock_override if stock_override is not None else num(p.get("currentStock"))
            out.append((p, depth, qty_label, needed, on_hand, stock_override is not None))
            if p.get("type") == "manufactured" and p.get("isWeldment"):
                for c in p.get("components") or []:
                    child = self.part(c.get("partId"))
                    if child:
                        q = num(c.get("qty"))
                        walk(child, depth + 1, needed * q, c.get("qty"), on_hand * q)

        for p in sorted(self.parts_for_project(pid), key=lambda x: x["name"].lower()):
            if p["id"] in comp:
                continue
            walk(p, 0, self.needed_for_project(p, pid), None, None)
        return out

    def project_summary(self, pid):
        parts = self.parts_for_project(pid)
        value = sum(num(p.get("currentStock")) * self.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
                    for p in parts)
        shorts = sum(1 for p in parts if self.needed_for_project(p, pid) > num(p.get("currentStock")))
        return {"parts": len(parts), "value": value, "short": shorts, "units": self.project_qty(pid)}

    def shortages(self):
        comp = self.component_ids()
        rows = []
        for p in self.parts:
            if p["id"] in comp:
                continue
            contrib = [{"projectId": a.get("projectId"), "needed": self.needed_for_project(p, a.get("projectId"))}
                       for a in self.allocations(p)]
            contrib = [c for c in contrib if c["needed"] > 0]
            total = sum(c["needed"] for c in contrib)
            short = total - num(p.get("currentStock"))
            if short > 0 and contrib:
                rows.append({"part": p, "contributing": contrib, "totalNeeded": total, "short": short})
        return sorted(rows, key=lambda r: -r["short"])

    # ------------------------------------------------------------ reorder
    @staticmethod
    def suggested_qty(p, needed) -> int:
        stock, rp, rq = num(p.get("currentStock")), num(p.get("reorderPoint")), num(p.get("reorderQty"))
        return int(max(rq or max(rp * 2 - stock, 1), needed - stock, 1))

    def reorder_list(self, project="all", vendor="all"):
        low = self.low_stock()
        if project != "all":
            low = [p for p in low if self.allocation(p, project)]
        if vendor != "all":
            low = [p for p in low if any(s["name"] == vendor for s in self.suppliers(p))]
        return sorted(low, key=lambda p: num(p.get("currentStock")) / (num(p.get("reorderPoint")) or 1))

    def projects_with_low(self):
        low = self.low_stock()
        return [pr for pr in self.sorted_projects() if any(self.allocation(p, pr["id"]) for p in low)]

    def vendors_among_low(self):
        return sorted({s["name"] for p in self.low_stock() for s in self.suppliers(p)})

    def open_part_orders(self):
        return sorted([o for o in self.partOrders if o.get("status") == "Ordered"],
                      key=lambda o: o.get("expectedDate") or "")

    def reorder_rows(self, parts, needed_fn, vendor=None):
        rows = []
        for p in parts:
            best = self.best_supplier(p)
            sups = [s for s in self.suppliers(p) if not vendor or s["name"] == vendor]
            others = [s for s in sups if not (best and s["name"] == best["name"] and s.get("price") == best.get("price"))]
            need = needed_fn(p)
            sq = self.suggested_qty(p, need)
            unit = self.to_cad(best["price"], best.get("currency")) if best else \
                self.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
            rows.append({"Part": p["name"], "Part Number": p.get("partNumber") or "",
                         "Type": TYPE_LABEL.get(p.get("type"), p.get("type")), "On Hand": p.get("currentStock"),
                         "Reorder Point": p.get("reorderPoint"), "Needed": need or 0, "Suggested Qty": sq,
                         "Best Supplier": best["name"] if best else "",
                         "Best Supplier Price": num(best["price"]) if best else "",
                         "Best Supplier Currency": (best.get("currency") or "CAD") if best else "",
                         "Best Supplier Part #": (best.get("partNumber") or "") if best else "",
                         "Best Supplier Website": (best.get("website") or "") if best else "",
                         "Other Suppliers": "; ".join(f"{s['name']} {fmt_money(s.get('price'))} {s.get('currency') or 'CAD'}"
                                                      for s in others),
                         "Est. Cost": sq * unit})
        return rows

    # -------------------------------------------------------- transactions
    def record(self, pid, mode, qty, note=""):
        """mode: usage | receiving. Returns the transaction id (for undo)."""
        p = self.part(pid)
        if p is None or qty <= 0:
            raise ValueError("Enter a quantity greater than 0")
        p["currentStock"] = num(p.get("currentStock")) + (-qty if mode == "usage" else qty)
        p["currentStock"] = int(p["currentStock"]) if float(p["currentStock"]).is_integer() else p["currentStock"]
        p["updatedAt"] = now_ms()
        tx = {"id": uid(), "partId": pid, "partName": p["name"], "mode": mode, "qty": qty, "note": note,
              "by": self.userName or "unknown", "ts": now_ms()}
        self.transactions.append(tx)
        return tx["id"]

    def undo_transaction(self, tx_id) -> bool:
        tx = next((t for t in self.transactions if t["id"] == tx_id), None)
        if tx is None:
            return False
        p = self.part(tx.get("partId"))
        if p:
            p["currentStock"] = num(p.get("currentStock")) + (num(tx["qty"]) if tx["mode"] == "usage" else -num(tx["qty"]))
            p["updatedAt"] = now_ms()
        self.transactions = [t for t in self.transactions if t["id"] != tx_id]
        return True

    def place_order(self, pid, qty, order_date, expected_date):
        p = self.part(pid)
        best = self.best_supplier(p) if p else None
        o = {"id": uid(), "partId": pid, "qty": qty, "orderDate": order_date, "expectedDate": expected_date,
             "status": "Ordered", "vendorName": best["name"] if best else ""}
        self.partOrders.append(o)
        return o

    def receive_order(self, oid):
        o = next((x for x in self.partOrders if x["id"] == oid), None)
        if o is None:
            return
        if self.part(o.get("partId")):
            self.record(o["partId"], "receiving", num(o.get("qty")), "From part order")
        o["status"] = "Received"
        o["receivedDate"] = today()

    # ----------------------------------------------------------- vendors
    def ensure_vendor(self, name):
        name = (name or "").strip()
        if not name:
            return None
        v = next((v for v in self.vendors if v["name"].lower() == name.lower()), None)
        if v:
            return v
        v = {"id": uid(), "name": name, "category": "Other", "contactName": "", "phone": "", "email": "",
             "website": "", "notes": "", "leadTimeMinDays": None, "leadTimeMaxDays": None}
        self.vendors.append(v)
        return v

    def lead_time_range(self, name):
        if not name:
            return None
        v = next((v for v in self.vendors if v["name"].lower() == name.strip().lower()), None)
        if not v:
            return None
        lo, hi = v.get("leadTimeMinDays"), v.get("leadTimeMaxDays")
        has_lo, has_hi = lo not in (None, ""), hi not in (None, "")
        if not has_lo and not has_hi:
            return None
        a = num(lo if has_lo else hi)
        b = num(hi if has_hi else lo)
        return (int(min(a, b)), int(max(a, b)))

    def lead_time(self, name):
        r = self.lead_time_range(name)
        return r[1] if r else None

    @staticmethod
    def lead_label(r):
        if not r:
            return ""
        return f"{r[0]} day{'s' if r[0] != 1 else ''}" if r[0] == r[1] else f"{r[0]}–{r[1]} days"

    def scorecard(self):
        rec = [o for o in self.partOrders if o.get("status") == "Received" and o.get("vendorName")
               and o.get("orderDate") and o.get("receivedDate")]
        by = {}
        for o in rec:
            by.setdefault(o["vendorName"], []).append(o)
        out = []
        for v, orders in by.items():
            r = self.lead_time_range(v)
            days = [d for d in (days_between(o["orderDate"], o["receivedDate"]) for o in orders) if d is not None]
            avg = sum(days) / len(days) if days else None
            on = sum(1 for d in days if d <= r[1]) if r else None
            out.append({"vendorName": v, "orderCount": len(orders), "avgDays": avg, "range": r,
                        "onTimeRate": round(on / len(days) * 100) if r and days else None})
        return sorted(out, key=lambda s: -(s["onTimeRate"] if s["onTimeRate"] is not None else -1))

    def sorted_vendors(self):
        return sorted(self.vendors, key=lambda v: (VENDOR_CATEGORIES.index(v.get("category"))
                                                   if v.get("category") in VENDOR_CATEGORIES else 9,
                                                   v["name"].lower()))

    # ----------------------------------------------------------- clients
    def client(self, cid):
        return next((c for c in self.clients if c["id"] == cid), None)

    def find_or_create_client(self, name, contact="", phone="", email=""):
        name = name.strip()
        c = next((c for c in self.clients if c["name"].lower() == name.lower()), None)
        if c is None:
            c = {"id": uid(), "name": name, "contactName": "", "phone": "", "email": ""}
            self.clients.append(c)
        for k, v in (("contactName", contact), ("phone", phone), ("email", email)):
            if v:
                c[k] = v
        return c

    # ---------------------------------------------------------- locations
    def location(self, loc_id):
        if not loc_id:
            return None
        base = loc_id.split("::")[0]
        return next((l for l in self.locations if l["id"] == base), None)

    @staticmethod
    def location_label(loc) -> str:
        if not loc:
            return ""
        if loc.get("name"):
            return loc["name"]
        sz = {"small": "S", "large": "L"}.get(loc.get("binSize"), "")
        return f"{loc.get('rack')} · Shelf {loc.get('shelf')} · Bin {loc.get('binPosition')}" + (f" ({sz})" if sz else "")

    def location_label_for(self, loc_id) -> str:
        if not loc_id:
            return ""
        base, _, section = loc_id.partition("::")
        loc = self.location(base)
        if not loc:
            return ""
        return f"{self.location_label(loc)}.{section}" if section else self.location_label(loc)

    def location_options(self):
        opts = []
        for loc in self.sorted_locations():
            base = self.location_label(loc)
            if loc.get("sections"):
                opts += [(f"{loc['id']}::{c}", f"{base}.{c}") for c in "abc"]
            else:
                opts.append((loc["id"], base))
        return opts

    def sorted_locations(self):
        return sorted(self.locations, key=lambda l: (str(l.get("rack")), int(num(l.get("shelf"))),
                                                     int(num(l.get("binPosition")))))

    def parts_at(self, loc_id, section=None):
        if section:
            return [p for p in self.parts if p.get("locationId") == f"{loc_id}::{section}"]
        return [p for p in self.parts if (p.get("locationId") or "").split("::")[0] == loc_id]

    def find_or_create_location(self, rack, shelf, bin_size, pos):
        if not rack or not shelf or not pos:
            return None
        size = bin_size if any(b["id"] == bin_size for b in BIN_SIZES) else \
            next((b["id"] for b in BIN_SIZES if b["label"] == bin_size), "small")
        ex = next((l for l in self.locations if l.get("rack") == rack and int(num(l.get("shelf"))) == int(num(shelf))
                   and int(num(l.get("binPosition"))) == int(num(pos)) and l.get("binSize") == size), None)
        if ex:
            return ex["id"]
        loc = {"id": uid(), "rack": rack, "shelf": int(num(shelf)), "binSize": size, "binPosition": int(num(pos))}
        self.locations.append(loc)
        return loc["id"]

    def delete_location(self, loc_id):
        self.locations = [l for l in self.locations if l["id"] != loc_id]
        for p in self.parts:
            if (p.get("locationId") or "").split("::")[0] == loc_id:
                p["locationId"] = None

    # ------------------------------------------------------------ dashboard
    def stock_value(self):
        return sum(num(p.get("currentStock")) * self.to_cad(p.get("unitCost"), p.get("unitCostCurrency"))
                   for p in self.parts)

    # ------------------------------------------------------- import / export
    def export_rows(self):
        rows = []
        for p in self.parts:
            s = (p.get("suppliers") or []) + [{}, {}, {}]
            loc = self.location(p.get("locationId"))
            r = {"Type": TYPE_LABEL.get(p.get("type"), p.get("type")), "Name": p["name"],
                 "Part Number": p.get("partNumber") or "", "QuickBooks Part #": p.get("qbPartNumber") or "",
                 "Drawing URL": p.get("drawingUrl") or "", "Notes": p.get("notes") or "",
                 "Category": p.get("category") or "", "Head Style": p.get("headStyle") or "",
                 "Thread Type": p.get("threadType") or "", "Thread Standard": p.get("threadStandard") or "",
                 "Size": p.get("size") or "", "Length": p.get("length") or "", "Unit": p.get("unit") or "",
                 "Current Stock": p.get("currentStock"), "Reorder Point": p.get("reorderPoint"),
                 "Reorder Qty": p.get("reorderQty")}
            for i, L in enumerate("ABC"):
                sup = s[i] or {}
                r[f"Supplier {L} Name"] = sup.get("name") or ""
                r[f"Supplier {L} Price"] = sup.get("price") if sup.get("price") not in (None,) else ""
                r[f"Supplier {L} Currency"] = sup.get("currency") or "CAD"
                r[f"Supplier {L} Part #"] = sup.get("partNumber") or ""
                r[f"Supplier {L} Website"] = sup.get("website") or ""
            r["Project"] = "; ".join(f"{self.project_name(a.get('projectId')) or ''}" + (f":{a['qty']}" if a.get("qty") else "")
                                     for a in self.allocations(p) if self.project_name(a.get("projectId")))
            r["Unit Cost"] = p.get("unitCost")
            r["Unit Cost Currency"] = p.get("unitCostCurrency") or "CAD"
            r["Location"] = self.location_label_for(p.get("locationId"))
            r["Rack"] = loc.get("rack") if loc else ""
            r["Shelf"] = loc.get("shelf") if loc else ""
            r["Bin Size"] = loc.get("binSize") if loc else ""
            r["Bin Position"] = loc.get("binPosition") if loc else ""
            r["Bin Section"] = p["locationId"].split("::")[1] if "::" in (p.get("locationId") or "") else ""
            r["Weldment"] = "Yes" if p.get("isWeldment") else ""
            r["Components"] = "; ".join(f"{self.part(c['partId'])['name']} x{c.get('qty')}"
                                        for c in p.get("components") or [] if self.part(c.get("partId")))
            rows.append(r)
        return rows

    def log_rows(self):
        return [{"When": dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%Y-%m-%d %H:%M"), "Part": t.get("partName"),
                 "Action": {"usage": "Used", "receiving": "Received"}.get(t.get("mode"), "Adjusted"),
                 "Qty": t.get("qty"), "By": t.get("by") or "", "Note": t.get("note") or ""}
                for t in reversed(self.transactions)]

    @staticmethod
    def norm_type(raw):
        s = str(raw or "").strip().lower()
        if s.startswith("fast"):
            return "fastener"
        if s.startswith("manu"):
            return "manufactured"
        if s.startswith("purch"):
            return "purchased"
        return s if s in TYPE_LABEL else "purchased"

    def import_rows(self, rows):
        """Rows from the master workbook / CSV (same columns as the web app). Returns (added, updated)."""
        added = updated = 0

        def g(r, *keys, default=""):
            for k in keys:
                if k in r and r[k] not in (None, ""):
                    return r[k]
            return default

        for r in rows:
            name = str(g(r, "Name", "name")).strip()
            if not name:
                continue
            typ = self.norm_type(g(r, "Type", "type"))
            ex = next((p for p in self.parts if p["name"].lower() == name.lower() and p.get("type") == typ), None)
            e = ex or {}
            sups = []
            for i, L in enumerate("ABC"):
                es = ((e.get("suppliers") or []) + [{}, {}, {}])[i] or {}
                price = g(r, f"Supplier {L} Price", default=None)
                sups.append({"name": str(g(r, f"Supplier {L} Name", default=es.get("name") or "")),
                             "price": num(price) if price not in (None, "") else es.get("price", ""),
                             "currency": str(g(r, f"Supplier {L} Currency", default=es.get("currency") or "CAD")).upper(),
                             "partNumber": str(g(r, f"Supplier {L} Part #", default=es.get("partNumber") or "")),
                             "website": str(g(r, f"Supplier {L} Website", default=es.get("website") or ""))})
            raw_proj = str(g(r, "Project", "project")).strip()
            if raw_proj:
                allocs = []
                for entry in re.split(r"[,;]", raw_proj):
                    entry = entry.strip()
                    if not entry:
                        continue
                    m = re.match(r"^(.*?)(?::\s*(\d+(?:\.\d+)?))?$", entry)
                    pid = self.find_or_create_project(m.group(1) if m else entry)
                    if pid:
                        allocs.append({"projectId": pid, "qty": num(m.group(2)) if m and m.group(2) else 0})
            else:
                allocs = e.get("projectAllocations") or []
            rack, shelf = str(g(r, "Rack")).strip(), str(g(r, "Shelf")).strip()
            bsize, bpos = str(g(r, "Bin Size")).strip(), str(g(r, "Bin Position")).strip()
            bsec = str(g(r, "Bin Section")).strip().lower()
            if rack and shelf and bpos:
                lid = self.find_or_create_location(rack, shelf, bsize, bpos)
                if lid and bsec in ("a", "b", "c"):
                    self.location(lid)["sections"] = True
                    lid = f"{lid}::{bsec}"
            else:
                lid = e.get("locationId")

            def n(col, key):
                v = g(r, col, default=None)
                return num(v) if v not in (None, "") else num(e.get(key))
            rec = {"id": e.get("id") or uid(), "type": typ, "name": name,
                   "partNumber": str(g(r, "Part Number", "partNumber", default=e.get("partNumber") or "")),
                   "qbPartNumber": str(g(r, "QuickBooks Part #", "qbPartNumber", default=e.get("qbPartNumber") or "")),
                   "drawingUrl": str(g(r, "Drawing URL", "drawingUrl", default=e.get("drawingUrl") or "")),
                   "notes": str(g(r, "Notes", "notes", default=e.get("notes") or "")),
                   "category": str(g(r, "Category", "category", default=e.get("category") or "")),
                   "headStyle": str(g(r, "Head Style", default=e.get("headStyle") or "")),
                   "threadType": str(g(r, "Thread Type", default=e.get("threadType") or "")),
                   "threadStandard": str(g(r, "Thread Standard", default=e.get("threadStandard") or "")),
                   "size": str(g(r, "Size", "size", default=e.get("size") or "")),
                   "length": str(g(r, "Length", "length", default=e.get("length") or "")),
                   "unit": str(g(r, "Unit", "unit", default=e.get("unit") or "ea")),
                   "currentStock": n("Current Stock", "currentStock"), "reorderPoint": n("Reorder Point", "reorderPoint"),
                   "reorderQty": n("Reorder Qty", "reorderQty"), "suppliers": sups, "projectAllocations": allocs,
                   "unitCost": n("Unit Cost", "unitCost"),
                   "unitCostCurrency": str(g(r, "Unit Cost Currency", default=e.get("unitCostCurrency") or "CAD")).upper(),
                   "locationId": lid, "updatedAt": now_ms()}
            for k in ("currentStock", "reorderPoint", "reorderQty"):
                if float(rec[k]).is_integer():
                    rec[k] = int(rec[k])
            if ex:
                ex.update(rec)
                updated += 1
            else:
                self.parts.append(rec)
                added += 1
        # weldment components once every part exists
        for r in rows:
            name = str(g(r, "Name", "name")).strip()
            if not name:
                continue
            typ = self.norm_type(g(r, "Type", "type"))
            p = next((x for x in self.parts if x["name"].lower() == name.lower() and x.get("type") == typ), None)
            if not p:
                continue
            flag = str(g(r, "Weldment")).strip().lower()
            if flag:
                p["isWeldment"] = flag in ("yes", "true", "1")
            comp = str(g(r, "Components")).strip()
            if comp:
                cs = []
                for entry in comp.split(";"):
                    m = re.match(r"^(.+?)\s+x(\d+(?:\.\d+)?)$", entry.strip(), re.I)
                    if not m:
                        continue
                    cp = next((x for x in self.parts if x["name"].lower() == m.group(1).strip().lower()
                               and x["id"] != p["id"]), None)
                    if cp:
                        cs.append({"partId": cp["id"], "qty": num(m.group(2))})
                if cs:
                    p["components"] = cs
        self.syncMeta = {**self.syncMeta, "lastImportAt": now_ms(), "lastImportBy": self.userName or "unknown"}
        return added, updated

    def import_file(self, path):
        path = str(path)
        if path.lower().endswith(".csv"):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            it = ws.iter_rows(values_only=True)
            head = [str(h).strip() if h is not None else "" for h in next(it, [])]
            rows = [{h: ("" if v is None else v) for h, v in zip(head, r)} for r in it]
        return self.import_rows(rows)

    def export_file(self, path):
        path = str(path)
        rows = self.export_rows()
        if path.lower().endswith(".csv"):
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, list(rows[0]) if rows else ["Name"])
                w.writeheader()
                w.writerows(rows)
        else:
            write_xlsx(path, [("Inventory", rows), ("Activity Log", self.log_rows())])
        self.syncMeta = {**self.syncMeta, "lastExportAt": now_ms(), "lastExportBy": self.userName or "unknown"}
        return path


def write_xlsx(path, sheets, link_col="Best Supplier Website"):
    """sheets: [(name, rows)] — header row bold, columns sized, website cells hyperlinked."""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used = set()
    for name, rows in sheets:
        base = re.sub(r"[\\/*?:\[\]]", "-", name)[:31] or "Sheet"
        nm, i = base, 2
        while nm in used:
            nm = f"{base[:28]} {i}"
            i += 1
        used.add(nm)
        ws = wb.create_sheet(nm)
        cols = list(rows[0]) if rows else []
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([r.get(c) for c in cols])
        if link_col in cols:
            ci = cols.index(link_col) + 1
            for k in range(2, len(rows) + 2):
                cell = ws.cell(row=k, column=ci)
                if cell.value:
                    cell.hyperlink = str(cell.value)
                    cell.style = "Hyperlink"
        for j, c in enumerate(cols, start=1):
            w = max([len(str(c))] + [len(str(r.get(c) or "")) for r in rows[:200]])
            ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = min(48, max(8, w + 2))
        ws.freeze_panes = "A2"
    wb.save(path)
    return path
