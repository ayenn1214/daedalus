"""Project profile: everything about one project on one page — weight, size, budget, vendors, options,
assemblies, production and files. Pure data (no Qt); the page is daedalus/ui/project_profile_ui.py.

Weights and costs roll up the BOM: a part's own weight / price when it has one, otherwise the sum of its parts
(so a weldment with no weight of its own still counts through its pieces). Anything that can't be worked out is
counted as missing, and every figure says how complete it is.
"""
from __future__ import annotations

from . import model as M
from . import mrp as R
from . import project_export as X

TYPES = ["MFG parts", "Purchased parts", "Fasteners", "Other"]
OPEN_PO = ("Ordered", "Partial", "Shipped", "Backordered")


# ------------------------------------------------------------------ roll-ups
def own_price(inv, p) -> float | None:
    """Unit cost (CAD) of the part itself: its unit cost, else the cheapest supplier price."""
    c = X.cost(inv, p)
    if c > 0:
        return c
    b = inv.best_supplier(p)
    if b:
        v = inv.to_cad(b["price"], b.get("currency"))
        return v if v > 0 else None
    return None


def rolled(inv, p, key: str, seen=None) -> tuple[float, bool]:
    """(value, complete) for weight ('w') or cost ('c') of one part: its own value, else its parts' total."""
    seen = seen or set()
    own = M.num(p.get("weightLb")) if key == "w" else (own_price(inv, p) or 0)
    if own > 0:
        return own, True
    kids = [c for c in p.get("components") or [] if inv.part(c.get("partId"))]
    if not kids or p["id"] in seen:
        return 0.0, False
    tot, ok = 0.0, True
    for c in kids:
        v, k = rolled(inv, inv.part(c["partId"]), key, seen | {p["id"]})
        tot += v * M.num(c.get("qty"), 1)
        ok = ok and k
    return tot, ok and tot > 0


def lead_days(inv, p) -> int:
    best = 0
    for s in inv.suppliers(p):
        d = M.num(s.get("leadDays"))
        if not d:
            v = next((v for v in inv.vendors if (v.get("name") or "").lower() == (s.get("name") or "").lower()), None)
            d = M.num((v or {}).get("leadTimeMaxDays") or (v or {}).get("leadTimeMinDays"))
        best = max(best, int(d or 0))
    return best


def vendor_of(inv, p) -> str:
    v = X.vendor(inv, p)
    return (v or {}).get("name") or ""


# ------------------------------------------------------------------ the profile
def profile(inv, pid: str, config=None) -> dict:
    pr = inv.project(pid)
    if pr is None:
        raise ValueError("No such project")
    top = X.top_level(inv, pid, config)
    every: dict = {}                                        # partId → qty per unit through every BOM level
    for p, q in top:
        for part_id, e in R.explode(inv, p["id"], q, False).items():
            every[part_id] = every.get(part_id, 0.0) + e["gross"]
    parts = {k: inv.part(k) for k in every if inv.part(k)}

    # totals from the top level down
    w_tot, w_known, c_tot, c_known = 0.0, 0, 0.0, 0
    for p, q in top:
        w, wk = rolled(inv, p, "w")
        c, ck = rolled(inv, p, "c")
        w_tot += w * q
        c_tot += c * q
        w_known += wk
        c_known += ck

    # by type (lines = distinct parts at any level)
    types = {t: {"lines": 0, "qty": 0.0, "weight": 0.0, "wKnown": 0, "cost": 0.0, "priced": 0} for t in TYPES}
    for k, q in every.items():
        p = parts.get(k)
        if p is None:
            continue
        t = types[X.group(p)]
        t["lines"] += 1
        t["qty"] += q
        if p.get("type") != "manufactured" or not p.get("components"):
            w = M.num(p.get("weightLb"))
            if w > 0:
                t["weight"] += w * q
                t["wKnown"] += 1
        c = own_price(inv, p)
        if c:
            t["priced"] += 1
            if not p.get("components"):
                t["cost"] += c * q
    mfg_top = [(p, q) for p, q in top if p.get("type") == "manufactured"]
    types["MFG parts"]["weight"] = sum(rolled(inv, p, "w")[0] * q for p, q in mfg_top)

    # finishing for one unit
    fin = {"zincLb": 0.0, "zinc": 0.0, "zincParts": 0, "powderParts": 0, "powder": 0.0, "needQuote": 0}
    try:
        from . import finishing as F
        src = {"kind": "project", "id": pid, "units": 1, "config": config}
        z, pc = F.zinc_rows(inv, src), F.powder_rows(inv, src)
        fin.update(zincLb=z["totalLb"], zinc=z["total"], zincParts=len(z["rows"]), powderParts=len(pc["rows"]),
                   powder=pc["total"], needQuote=pc["needQuote"])
    except Exception:  # noqa: BLE001
        pass

    # vendors: who supplies what on this project, open part orders
    vend: dict = {}
    for k, p in parts.items():
        n = vendor_of(inv, p)
        if n:
            v = vend.setdefault(n, {"name": n, "parts": 0, "openPOs": 0, "kinds": set()})
            v["parts"] += 1
            v["kinds"].add(X.group(p))
        f = X.finisher(p)
        if f:
            v = vend.setdefault(f, {"name": f, "parts": 0, "openPOs": 0, "kinds": set()})
            v["parts"] += 1
            v["kinds"].add("Finishing")
    for o in inv.partOrders:
        if o.get("projectId") == pid and o.get("status") in OPEN_PO and o.get("vendorName"):
            v = vend.setdefault(o["vendorName"], {"name": o["vendorName"], "parts": 0, "openPOs": 0, "kinds": set()})
            v["openPOs"] += 1
    vendors = sorted(({**v, "kinds": sorted(v["kinds"])} for v in vend.values()), key=lambda v: (-v["parts"],
                                                                                                 v["name"].lower()))

    # longest lead
    lead = max(((lead_days(inv, p), p) for p in parts.values()), key=lambda x: x[0], default=(0, None))

    # options: what each choice adds
    from . import configs as C
    opts = []
    for g in C.groups(pr):
        ch = []
        for c in g.get("choices") or []:
            add_w = add_c = 0.0
            n = 0
            for ln in c.get("lines") or []:
                p = inv.part(ln.get("partId"))
                if p is None:
                    continue
                q = M.num(ln.get("qty"), 1)
                n += 1
                add_w += rolled(inv, p, "w")[0] * q
                add_c += rolled(inv, p, "c")[0] * q
            ch.append({"id": c["id"], "name": c.get("name") or "", "lines": n, "weight": add_w, "cost": add_c,
                       "default": bool(c.get("default"))})
        opts.append({"id": g["id"], "name": g.get("name") or "", "kind": g.get("kind") or "one", "choices": ch})

    # assemblies / weldments at the top level
    from . import analytics as A
    asm = []
    for p, q in top:
        if not p.get("components"):
            continue
        short = 0
        for c in p["components"]:
            ch = inv.part(c.get("partId"))
            if ch is not None and M.num(ch.get("currentStock")) < M.num(c.get("qty"), 1) * q:
                short += 1
        st = A.version_status(p) if (p.get("drawingUrl") or p.get("cadFile")) else {"status": "no-file", "detail": ""}
        asm.append({"part": p, "qty": q, "weight": rolled(inv, p, "w"), "kids": len(p["components"]),
                    "short": short, "stock": M.num(p.get("currentStock")), "file": st})

    # files & revisions over the MFG parts
    mfg = [p for p in parts.values() if p.get("type") == "manufactured"]
    linked = [p for p in mfg if p.get("drawingUrl")]
    stale = []
    for p in mfg:
        if p.get("drawingUrl") or p.get("cadFile"):
            s = A.version_status(p)
            if s["status"] in ("changed", "newer", "mismatch"):
                stale.append((p, s))

    # production
    from . import orders as O
    prods = [o for o in inv.productionOrders if o.get("projectId") == pid]
    open_prods = [o for o in prods if o.get("status") not in O.CLOSED_PROD]
    year = M.today()[:4]
    built = sum(M.num(o.get("qty"), 1) for o in prods if o.get("status") == "Complete"
                and str(o.get("completedDate") or o.get("updatedDate") or "")[:4] == year)

    return {
        "project": pr, "top": top, "every": every, "parts": parts,
        "weight": w_tot, "weightKnown": w_known, "lines": len(top),
        "cost": c_tot, "costKnown": c_known,
        "types": types, "finishing": fin, "vendors": vendors,
        "lead": {"days": lead[0], "part": lead[1], "vendor": vendor_of(inv, lead[1]) if lead[1] else ""},
        "options": opts, "assemblies": asm,
        "files": {"mfg": len(mfg), "linked": len(linked), "stale": stale},
        "production": {"open": open_prods, "all": prods, "builtThisYear": built,
                       "inProduction": sum(M.num(o.get("qty"), 1) for o in open_prods)},
        "model": pr.get("modelFile") or "", "size": pr.get("modelSize") or None,
    }


def fmt_size(size: dict | None) -> str:
    """'2,410 × 1,380 × 2,150 mm (94.9 × 54.3 × 84.6 in)' — the model's bounding box, longest first."""
    if not size:
        return ""
    d = sorted([M.num(size.get(k)) for k in ("x", "y", "z")], reverse=True)
    if not any(d):
        return ""
    mm = " × ".join(f"{v:,.0f}" for v in d)
    inch = " × ".join(f"{v / 25.4:,.1f}" for v in d)
    return f"{mm} mm ({inch} in)"
