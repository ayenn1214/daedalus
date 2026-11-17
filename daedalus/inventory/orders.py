"""Customer orders → production → purchasing. Qt-free.

Order (client-orders): PO, name, projects (project + qty). Draft until confirmed; confirming creates one production
order per project line and that is what creates demand (projects themselves only describe one unit). Every change
is logged; a change to a confirmed order (a change order) updates its production orders and flags them unread.

Production order: arrives unread (like an email) for everyone; the fab PO is confirmed first, then the purchasing
checklist is built from the project's parts — fasteners, powder coating, zinc plating, purchased parts — one PO per
vendor, and any line can be moved to another vendor to split a PO. Each PO is Yes (sent) / No / Not needed; when
every one is Yes or Not needed the build gets a finish estimate."""
from __future__ import annotations

from .model import Inventory, add_days, days_between, now_ms, num, today, uid

DRAFT, CONFIRMED, SHIPPED, CANCELLED = "Draft", "Confirmed", "Shipped", "Cancelled"
ORDER_FLOW = [DRAFT, CONFIRMED, SHIPPED, CANCELLED]
_LEGACY = {"Requested": DRAFT, "Confirmed": CONFIRMED, "In Production": CONFIRMED, "Shipped": SHIPPED,
           "Complete": SHIPPED}
GROUPS = ["Fasteners", "Powder coating", "Zinc plating", "Purchased parts"]
CLOSED_PROD = ("Complete", "Cancelled")
YES, NO, NA = "yes", "no", "na"
PO_STATES = [(NO, "No"), (YES, "Yes"), (NA, "Not needed")]
DONE = (YES, NA)


# ------------------------------------------------------------------ orders
def status(o) -> str:
    s = o.get("status") or DRAFT
    return s if s in ORDER_FLOW else _LEGACY.get(s, DRAFT)


def order_name(inv: Inventory, o) -> str:
    return o.get("name") or (inv.client(o.get("clientId")) or {}).get("name") or ""


def _lines(lines, inv: Inventory | None = None) -> dict:
    """{(projectId, config key): qty} — the same project in two configurations stays two lines."""
    from . import configs as C
    out: dict = {}
    for ln in lines or []:
        if ln.get("projectId") and num(ln.get("qty")) > 0:
            k = (ln["projectId"], C.key(inv.project(ln["projectId"]), ln.get("config")) if inv else "")
            out[k] = out.get(k, 0) + num(ln.get("qty"))
    return out


def _configs(lines, inv) -> dict:
    from . import configs as C
    return {(ln["projectId"], C.key(inv.project(ln["projectId"]), ln.get("config"))):
            C.normalize(inv.project(ln["projectId"]), ln.get("config"))
            for ln in lines or [] if ln.get("projectId")}


def line_name(inv: Inventory, pid, config=None) -> str:
    from . import configs as C
    pr = inv.project(pid)
    lab = C.label(pr, config) if C.has_options(pr) else ""
    return (inv.project_name(pid) or "(deleted project)") + (f" ({lab})" if lab else "")


def _log(o, by, what, changes=None):
    o.setdefault("changes", []).append({"at": now_ms(), "by": by or "unknown", "rev": o.get("rev", 0),
                                        "what": what, "changes": changes or []})


def save_order(inv: Inventory, o, po: str, name: str, lines: list, by: str = "") -> dict:
    """Create (o=None, as a Draft) or change an order. A change to a confirmed order is a change order: logged with
    a new revision and pushed to its production orders."""
    po, name = (po or "").strip(), (name or "").strip()
    if not name:
        raise ValueError("Enter the order name")
    cfgs = _configs(lines, inv)
    new_lines = [{"projectId": k[0], "qty": q, **({"config": cfgs[k]} if cfgs.get(k) else {})}
                 for k, q in _lines(lines, inv).items()]
    if not new_lines:
        raise ValueError("Add at least one project with a quantity")
    if o is None:
        o = {"id": uid(), "poNumber": po, "name": name, "projectLines": new_lines, "status": DRAFT, "rev": 0,
             "orderDate": today(), "createdAt": now_ms()}
        _log(o, by, "created", [f"{line_name(inv, l['projectId'], l.get('config'))} ×{l['qty']:g}" for l in new_lines])
        inv.clientOrders.append(o)
        return o
    diff = []
    if po != (o.get("poNumber") or ""):
        diff.append(f"PO: {o.get('poNumber') or '—'} → {po or '—'}")
    if name != order_name(inv, o):
        diff.append(f"Name: {order_name(inv, o) or '—'} → {name}")
    old, new = _lines(o.get("projectLines"), inv), _lines(new_lines, inv)
    names = {**{k: line_name(inv, k[0], c) for k, c in _configs(o.get("projectLines"), inv).items()},
             **{k: line_name(inv, k[0], c) for k, c in _configs(new_lines, inv).items()}}
    for k in sorted(set(old) | set(new), key=lambda k: names.get(k, "")):
        a, b = old.get(k, 0), new.get(k, 0)
        if a != b:
            pn = names.get(k) or inv.project_name(k[0]) or "(deleted project)"
            diff.append(f"{pn}: added ×{b:g}" if not a else f"{pn}: removed (was ×{a:g})" if not b
                        else f"{pn}: ×{a:g} → ×{b:g}")
    if not diff:
        return o
    o.update(poNumber=po, name=name, projectLines=new_lines)
    if status(o) == CONFIRMED:
        o["rev"] = o.get("rev", 0) + 1
        _log(o, by, f"change order {o['rev']}", diff)
        sync_production(inv, o, by, f"Change order {o['rev']}: " + "; ".join(diff))
    else:
        _log(o, by, "edited", diff)
    return o


def confirm(inv: Inventory, o, by: str = "") -> list[dict]:
    """Confirm an order: it becomes demand — one production order per project line, unread for everyone."""
    if status(o) == CONFIRMED:
        return linked_production(inv, o)
    if status(o) != DRAFT:
        raise ValueError(f"A {status(o).lower()} order can't be confirmed")
    o["status"] = CONFIRMED
    o["confirmedAt"] = now_ms()
    o["confirmedBy"] = by or "unknown"
    _log(o, by, "confirmed")
    return sync_production(inv, o, by, "From confirmed order")


def set_status(inv: Inventory, o, new: str, by: str = ""):
    """Ship or cancel. Cancelling cancels production that hasn't finished."""
    if new not in (SHIPPED, CANCELLED, DRAFT):
        raise ValueError(new)
    old = status(o)
    if new == old:
        return
    o["status"] = new
    if new == SHIPPED:
        o["shippedDate"] = today()
    _log(o, by, new.lower() if new != DRAFT else "back to draft")
    if new in (CANCELLED, DRAFT):
        for p in linked_production(inv, o):
            if p.get("status") not in CLOSED_PROD:
                p["status"] = "Cancelled"
                _prod_event(p, by, f"Order {'cancelled' if new == CANCELLED else 'moved back to draft'}")
                p["readBy"] = []


def linked_production(inv: Inventory, o) -> list[dict]:
    return [p for p in inv.productionOrders if p.get("clientOrderId") == o["id"]]


def sync_production(inv: Inventory, o, by: str, note: str) -> list[dict]:
    """Make the order's production match its lines: one open production order per project with the order's qty."""
    from . import configs as C
    want = _lines(o.get("projectLines"), inv)
    cfgs = _configs(o.get("projectLines"), inv)
    have = {}
    for p in linked_production(inv, o):
        if p.get("status") != "Cancelled":
            have.setdefault((p.get("projectId"), C.key(inv.project(p.get("projectId")), p.get("config"))), p)
    for k, q in want.items():
        pid = k[0]
        p = have.get(k)
        if p is None:
            p = {"id": uid(), "projectId": pid, "qty": q, "status": "Not Started", "clientOrderId": o["id"],
                 **({"config": cfgs[k]} if cfgs.get(k) else {}),
                 "poNumber": o.get("poNumber") or "", "orderName": order_name(inv, o), "orderDate": today(),
                 "dueDate": o.get("dueDate") or "", "readBy": [], "createdAt": now_ms()}
            _prod_event(p, by, note)
            inv.productionOrders.append(p)
        elif num(p.get("qty")) != q or p.get("poNumber") != (o.get("poNumber") or ""):
            if num(p.get("qty")) != q:
                _prod_event(p, by, f"{note} (qty {num(p.get('qty')):g} → {q:g})")
            p.update(qty=q, poNumber=o.get("poNumber") or "", orderName=order_name(inv, o), readBy=[])
    for k, p in have.items():
        if k not in want and p.get("status") not in CLOSED_PROD:
            p["status"] = "Cancelled"
            p["readBy"] = []
            _prod_event(p, by, f"{note} (removed from the order)")
    return linked_production(inv, o)


def progress(inv: Inventory, o) -> list[dict]:
    """Where each project on the order is in production."""
    out = []
    for p in linked_production(inv, o):
        out.append({"project": line_name(inv, p.get("projectId"), p.get("config")), "qty": num(p.get("qty")),
                    "status": p.get("status") or "Not Started", "stage": stage(inv, p), "id": p["id"]})
    return out


# ------------------------------------------------------------------ production
def _prod_event(p, by, what):
    p.setdefault("events", []).append({"at": now_ms(), "by": by or "unknown", "what": what})


def is_unread(p, user: str) -> bool:
    return (user or "unknown") not in (p.get("readBy") or [])


def mark_read(p, user: str) -> bool:
    if not is_unread(p, user):
        return False
    p.setdefault("readBy", []).append(user or "unknown")
    return True


def units_on_order(inv: Inventory, pid) -> float | None:
    """Units of a project to build: open production orders. None when the project has never had a production order
    (the caller can fall back to an old planned quantity)."""
    prods = [p for p in inv.productionOrders if p.get("projectId") == pid]
    if not prods:
        return None
    return sum(num(p.get("qty"), 1) for p in prods if p.get("status") not in CLOSED_PROD)


def _state(v) -> str:
    if v is True:
        return YES
    return v if v in (YES, NO, NA) else NO


def _label(state) -> str:
    return dict(PO_STATES)[_state(state)]


def fab_state(p) -> str:
    return _state(p.get("fabPo", p.get("fabPoSent")))


def set_fab_po(p, state: str, po_number: str = "", vendor: str = "", by: str = ""):
    """Fab PO: yes (sent) / no / na (not needed — e.g. made in-house)."""
    state = _state(state)
    p["fabPo"] = state
    p["fabPoNumber"] = po_number.strip()
    p["fabVendor"] = vendor.strip()
    p["fabPoAt"] = now_ms()
    _prod_event(p, by, f"Fab PO: {_label(state)}" + (f" — {po_number.strip()}" if state == YES and po_number.strip()
                                                   else ""))


def _finish_process(p) -> str:
    f = p.get("finish")
    return ((f or {}).get("process") if isinstance(f, dict) else f) or ""


def _finish_vendor(p) -> str:
    f = p.get("finish")
    return ((f or {}).get("vendor") if isinstance(f, dict) else "") or ""


def group_of(p) -> str | None:
    t = p.get("type")
    if t == "fastener":
        return "Fasteners"
    if t == "purchased":
        return "Purchased parts"
    fin = _finish_process(p).lower()
    if "powder" in fin:
        return "Powder coating"
    if "zinc" in fin or "plat" in fin:
        return "Zinc plating"
    return None                                      # fabricated: covered by the fab PO


def requirements(inv: Inventory, p) -> dict:
    """{partId: qty} for the whole build: every part on the project × order qty, through every BOM level."""
    from .mrp import explode
    from .configs import lines as option_lines
    req: dict = {}
    q = num(p.get("qty"), 1)
    for pid, n in option_lines(inv.project(p.get("projectId")), p.get("config")).items():
        for part_id, e in explode(inv, pid, n * q, False).items():
            req[part_id] = req.get(part_id, 0) + e["gross"]
    comp = inv.component_ids()
    for part in inv.parts_for_project(p.get("projectId")):
        if part["id"] in comp:
            continue
        a = inv.allocation(part, p.get("projectId"))
        n = num(a.get("qty")) * q if a else 0
        if n <= 0:
            continue
        for pid, e in explode(inv, part["id"], n, False).items():
            req[pid] = req.get(pid, 0) + e["gross"]
    return req


def default_vendor(inv: Inventory, part, group) -> str:
    if group in ("Powder coating", "Zinc plating"):
        return _finish_vendor(part) or "No finisher set"
    sups = inv.suppliers(part)
    best = inv.best_supplier(part)
    return (best or (sups[0] if sups else {})).get("name") or "No vendor set"


def checklist(inv: Inventory, p) -> dict:
    """{group: {vendor: {"lines": [...], "state": yes/no/na, "poNumber": str}}} — one PO per vendor in each group. A line
    moved to another vendor (split) follows p['poVendor']."""
    over = p.get("poVendor") or {}
    checks = p.get("poChecks") or {}
    out: dict = {}
    for pid, qty in requirements(inv, p).items():
        part = inv.part(pid)
        g = group_of(part) if part else None
        if g is None:
            continue
        vend = over.get(f"{g}|{pid}") or default_vendor(inv, part, g)
        box = out.setdefault(g, {}).setdefault(vend, {"lines": []})
        box["lines"].append({"partId": pid, "name": part["name"], "partNumber": part.get("partNumber") or "",
                             "qty": qty, "onHand": num(part.get("currentStock")),
                             "toOrder": max(0.0, qty - num(part.get("currentStock")))})
    for g, vends in out.items():
        for vend, box in vends.items():
            c = checks.get(f"{g}|{vend}") or {}
            box["state"] = _state(c.get("state", c.get("sent")))
            box["poNumber"] = c.get("poNumber") or ""
            box["lines"].sort(key=lambda l: (l["partNumber"] or "~", l["name"].lower()))
    return {g: out[g] for g in GROUPS if g in out}


def move_line(p, group: str, part_id: str, vendor: str, by: str = ""):
    """Split: send one line to another vendor (its own PO)."""
    p.setdefault("poVendor", {})[f"{group}|{part_id}"] = vendor.strip()
    _prod_event(p, by, f"{group}: line moved to {vendor.strip()}")


def set_po(p, group: str, vendor: str, state: str, po_number: str = "", by: str = ""):
    """A vendor PO in the checklist: yes (sent) / no / na (not needed — enough in stock, customer-supplied …)."""
    state = _state(state)
    p.setdefault("poChecks", {})[f"{group}|{vendor}"] = {"state": state, "poNumber": po_number.strip(),
                                                          "at": now_ms(), "by": by or "unknown"}
    _prod_event(p, by, f"{group} PO to {vendor}: {_label(state)}"
                       + (f" — {po_number.strip()}" if state == YES and po_number.strip() else ""))


def po_counts(inv: Inventory, p) -> tuple[int, int]:
    """(done, total) POs, the fab PO included — done = sent (yes) or not needed."""
    cl = checklist(inv, p)
    boxes = [b for vends in cl.values() for b in vends.values()]
    return (int(fab_state(p) in DONE) + sum(b["state"] in DONE for b in boxes), 1 + len(boxes))


def all_pos_done(inv: Inventory, p) -> bool:
    s, n = po_counts(inv, p)
    return s == n


def set_finish(p, days: int, by: str = ""):
    """Time needed to finish once purchasing is done."""
    p["finishDays"] = int(days)
    p["finishBy"] = add_days(today(), int(days))
    if p.get("status") in (None, "", "Not Started"):
        p["status"] = "In Fab"
    _prod_event(p, by, f"Finish estimate: {int(days)} day(s) → {p['finishBy']}")


def set_finish_by(p, date: str, by: str = ""):
    """Finish date typed or picked directly; the day count follows (from today)."""
    if not date:
        p.pop("finishBy", None)
        p.pop("finishDays", None)
        _prod_event(p, by, "Finish estimate cleared")
        return
    p["finishBy"] = date
    p["finishDays"] = max(0, days_between(today(), date) or 0)
    if p.get("status") in (None, "", "Not Started"):
        p["status"] = "In Fab"
    _prod_event(p, by, f"Finish by {date}")


DATE_FIELDS = {"orderDate": "Order date", "dueDate": "Due date", "fabStart": "Fab start", "fabEnd": "Fab end",
               "finishStart": "Finishing start", "finishEnd": "Finishing end"}


def set_date(p, key: str, date: str, by: str = ""):
    if key not in DATE_FIELDS or (p.get(key) or "") == (date or ""):
        return False
    if date:
        p[key] = date
    else:
        p.pop(key, None)
    _prod_event(p, by, f"{DATE_FIELDS[key]}: {date or 'cleared'}")
    return True


def stage(inv: Inventory, p) -> str:
    """One line for where a production order is."""
    st = p.get("status") or "Not Started"
    if st in CLOSED_PROD:
        return st
    if fab_state(p) not in DONE:
        return "Waiting on fab PO"
    s, n = po_counts(inv, p)
    if s < n:
        return f"POs {s}/{n} done"
    if p.get("finishBy"):
        return f"{st} · finish by {p['finishBy']}"
    return "POs done — needs finish estimate"
