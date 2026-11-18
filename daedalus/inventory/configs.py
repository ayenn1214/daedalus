"""Project customizer: configurations of one project (e.g. RFP length 1006 / 1308 / short, plus an extra beam).

A project keeps its common parts as usual (projectAllocations, qty per unit). What changes between builds lives in
option groups on the project:

  project["options"] = [
    {"id", "name": "Length", "kind": "one",            # pick exactly one choice
     "choices": [{"id", "name": "1308", "default": true, "lines": [{"partId", "qty"}]}, …]},
    {"id", "name": "Add-ons", "kind": "any",           # pick any (each choice on / off)
     "choices": [{"id", "name": "4th beam", "lines": [{"partId": "beam", "qty": 1}]}]},
  ]

A configuration is {groupId: [choiceId, …]}, plus optional per-order quantity changes of the project's top-level
MFG parts: config["_qty"] = {partId: change per unit} (e.g. +1 of a weldment, −2 of a plate; 0 never stored). Order lines and production orders carry one ("config"); everything
that counts parts for a build (demand, kits, BOM export, finishing, purchasing) adds the chosen choices' lines per
unit on top of the common parts. Without a config the defaults apply.
"""
from __future__ import annotations

from .model import num, uid

ONE, ANY = "one", "any"
QTY = "_qty"                                   # per-order qty changes of top-level MFG parts


def qty_changes(config) -> dict:
    """{partId: change per unit} of a configuration (non-zero only)."""
    q = (config or {}).get(QTY) or {}
    return {k: num(v) for k, v in q.items() if k and num(v)} if isinstance(q, dict) else {}


def groups(project) -> list:
    return (project or {}).get("options") or []


def has_options(project) -> bool:
    return any(g.get("choices") for g in groups(project))


def new_group(name="Option", kind=ONE) -> dict:
    return {"id": uid(), "name": name, "kind": kind, "choices": []}


def new_choice(name="Choice", default=False) -> dict:
    return {"id": uid(), "name": name, "default": bool(default), "lines": []}


def default_config(project) -> dict:
    cfg = {}
    for g in groups(project):
        ch = g.get("choices") or []
        if not ch:
            continue
        if g.get("kind", ONE) == ONE:
            d = next((c for c in ch if c.get("default")), ch[0])
            cfg[g["id"]] = [d["id"]]
        else:
            cfg[g["id"]] = [c["id"] for c in ch if c.get("default")]
    return cfg


def normalize(project, config) -> dict:
    """A valid configuration: unknown groups / choices dropped, a 'one' group always has exactly one choice."""
    base = default_config(project)
    if not config:
        return base
    out = {}
    q = qty_changes(config)
    if q:
        out[QTY] = q
    for g in groups(project):
        ids = {c["id"] for c in g.get("choices") or []}
        if not ids:
            continue
        got = [c for c in (config.get(g["id"]) or []) if c in ids]
        if g.get("kind", ONE) == ONE:
            out[g["id"]] = got[:1] or base.get(g["id"], [])
        else:
            out[g["id"]] = got if g["id"] in config else base.get(g["id"], [])
    return out


def key(project, config) -> str:
    """Stable text for a configuration ('' = no options) — order lines with the same project + key merge."""
    cfg = normalize(project, config)
    q = cfg.pop(QTY, {})
    k = ";".join(f"{g}={','.join(sorted(c))}" for g, c in sorted(cfg.items()) if c)
    if q:
        k += ";qty=" + ",".join(f"{p}:{v:g}" for p, v in sorted(q.items()))
    return k


def label(project, config, short=True) -> str:
    """'1308 · 4th beam' (short) or 'Length 1308 · Add-ons 4th beam'."""
    cfg = normalize(project, config)
    bits = []
    for g in groups(project):
        names = [c["name"] for c in g.get("choices") or [] if c["id"] in cfg.get(g["id"], [])]
        if names:
            bits.append(", ".join(names) if short else f"{g['name']} {', '.join(names)}")
        elif not short and g.get("kind") == ANY and g.get("choices"):
            bits.append(f"{g['name']}: none")
    n = len(cfg.get(QTY) or {})
    if n:
        bits.append(f"{n} qty change{'s' if n != 1 else ''}")
    return " · ".join(bits)


def qty_label(inv, config) -> str:
    """'BASE WELDMENT +1, PLATE −2' — the qty changes in words."""
    out = []
    for k, v in qty_changes(config).items():
        p = inv.part(k)
        out.append(f"{(p or {}).get('partNumber') or (p or {}).get('name') or '?'} {'+' if v > 0 else '−'}{abs(v):g}")
    return ", ".join(out)


def lines(project, config) -> dict:
    """{partId: qty per unit} the configuration adds on top of the common parts — a qty change can be negative
    (fewer of a common part); callers add these to the common quantities and drop anything at or below zero."""
    cfg = normalize(project, config)
    out: dict = dict(cfg.get(QTY) or {})
    for g in groups(project):
        for c in g.get("choices") or []:
            if c["id"] in cfg.get(g["id"], []):
                for ln in c.get("lines") or []:
                    if ln.get("partId") and num(ln.get("qty")) > 0:
                        out[ln["partId"]] = out.get(ln["partId"], 0.0) + num(ln.get("qty"))
    return out


def option_parts(project, inv=None) -> set:
    if inv is not None and project and getattr(inv, "_memo", None) is not None:
        return inv.memo(("option_parts", project["id"]), lambda: _option_parts(project, inv))
    return _option_parts(project, inv)


def _option_parts(project, inv=None) -> set:
    out = {ln.get("partId") for g in groups(project) for c in g.get("choices") or []
           for ln in c.get("lines") or [] if ln.get("partId")}
    if inv is not None and project:                    # parts some open build changes the qty of
        for cfg, _u in builds(inv, project["id"]):
            out |= set(qty_changes(cfg))
    return out


def builds(inv, pid) -> list:
    """[(config, units)] being built for a project: open production orders, else the old planned qty at defaults."""
    if getattr(inv, "_memo", None) is not None:
        return inv.memo(("builds", pid), lambda: _builds(inv, pid))
    return _builds(inv, pid)


def _builds(inv, pid) -> list:
    from .orders import CLOSED_PROD
    pr = inv.project(pid)
    prods = [p for p in inv.productionOrders if p.get("projectId") == pid]
    if prods:
        return [(normalize(pr, p.get("config")), num(p.get("qty"), 1)) for p in prods
                if p.get("status") not in CLOSED_PROD]
    q = inv.legacy_qty(pid)
    return [(default_config(pr), q)] if q else []


def option_need(inv, part, pid) -> float:
    """Units of `part` the project's chosen options need (top level: before exploding a weldment)."""
    pr = inv.project(pid)
    if part["id"] not in option_parts(pr, inv):
        return 0.0
    a = inv.allocation(part, pid)
    base = num(a.get("qty")) if a else 0.0
    tot = 0.0
    for cfg, u in builds(inv, pid):
        add = lines(pr, cfg).get(part["id"], 0.0)
        tot += max(add, -base) * u                       # can't take away more than the project has
    return tot


def option_projects(inv, part) -> list:
    if getattr(inv, "_memo", None) is not None:
        def index():
            ix: dict = {}
            for pr in inv.projects:
                for k in option_parts(pr, inv):
                    ix.setdefault(k, []).append(pr["id"])
            return ix
        return list(inv.memo("option_projects", index).get(part["id"], []))
    return [pr["id"] for pr in inv.projects if part["id"] in option_parts(pr, inv)]


def top_mfg(inv, pid) -> list:
    """[(part, qty per unit)] — the project's top-level MFG parts: weldments / assemblies and MFG parts that
    aren't inside one. These are what an order line can change the qty of."""
    on = inv.parts_for_project(pid)
    inside = {c.get("partId") for p in on for c in (p.get("components") or [])}
    out = []
    for p in on:
        if p["id"] in inside or p.get("type") != "manufactured":
            continue
        a = inv.allocation(p, pid)
        q = num(a.get("qty")) if a else 0.0
        if q > 0:
            out.append((p, q))
    return sorted(out, key=lambda x: (not x[0].get("components"), (x[0].get("partNumber") or "~").lower(),
                                      (x[0].get("name") or "").lower()))
