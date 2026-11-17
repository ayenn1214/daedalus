"""Project customizer: configurations of one project (e.g. RFP length 1006 / 1308 / short, plus an extra beam).

A project keeps its common parts as usual (projectAllocations, qty per unit). What changes between builds lives in
option groups on the project:

  project["options"] = [
    {"id", "name": "Length", "kind": "one",            # pick exactly one choice
     "choices": [{"id", "name": "1308", "default": true, "lines": [{"partId", "qty"}]}, …]},
    {"id", "name": "Add-ons", "kind": "any",           # pick any (each choice on / off)
     "choices": [{"id", "name": "4th beam", "lines": [{"partId": "beam", "qty": 1}]}]},
  ]

A configuration is {groupId: [choiceId, …]}. Order lines and production orders carry one ("config"); everything
that counts parts for a build (demand, kits, BOM export, finishing, purchasing) adds the chosen choices' lines per
unit on top of the common parts. Without a config the defaults apply.
"""
from __future__ import annotations

from .model import num, uid

ONE, ANY = "one", "any"


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
    return ";".join(f"{g}={','.join(sorted(c))}" for g, c in sorted(cfg.items()) if c)


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
    return " · ".join(bits)


def lines(project, config) -> dict:
    """{partId: qty per unit} the configuration adds on top of the common parts."""
    cfg = normalize(project, config)
    out: dict = {}
    for g in groups(project):
        for c in g.get("choices") or []:
            if c["id"] in cfg.get(g["id"], []):
                for ln in c.get("lines") or []:
                    if ln.get("partId") and num(ln.get("qty")) > 0:
                        out[ln["partId"]] = out.get(ln["partId"], 0.0) + num(ln.get("qty"))
    return out


def option_parts(project) -> set:
    return {ln.get("partId") for g in groups(project) for c in g.get("choices") or []
            for ln in c.get("lines") or [] if ln.get("partId")}


def builds(inv, pid) -> list:
    """[(config, units)] being built for a project: open production orders, else the old planned qty at defaults."""
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
    if not has_options(pr) or part["id"] not in option_parts(pr):
        return 0.0
    return sum(lines(pr, cfg).get(part["id"], 0.0) * u for cfg, u in builds(inv, pid))


def option_projects(inv, part) -> list:
    return [pr["id"] for pr in inv.projects if part["id"] in option_parts(pr)]
