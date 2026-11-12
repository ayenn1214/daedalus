"""Startup modules: boot only the parts of Daedalus you need.

  3d         modelling: sketches, features, assemblies, CAM, Icarus (loads the CAD kernel + 3D view)
  sim        simulation: FEA, CFD, physics, design studies (needs the 3D view — loads it, modelling tools hidden
             unless 3d is on too)
  drawing    drawings and DXF / DWG / SVG / HPGL editing
  inventory  Holocron inventory: parts, stock, projects, vendors, reorder

Without 3d and sim, Daedalus opens a light window (no CAD kernel, no 3D view) that boots in about a second;
the 3D workspace can still be opened from it later.

Choose with:  python -m daedalus --only inventory      (or --modules drawing,inventory)
              Help ▸ Startup modules…  (saved; used when no flag is given)
"""
from __future__ import annotations

MODULES: dict[str, tuple[str, str]] = {
    "3d": ("3D modelling", "Sketches, features, surfaces, sheet metal, assemblies, CAM, Icarus"),
    "sim": ("Simulation", "FEA, CFD, physics, design studies, topology"),
    "drawing": ("Drawings", "2D drawings from the model; DXF / DWG / SVG / HPGL editing"),
    "inventory": ("Inventory", "Holocron: parts, stock, projects, vendors, reorder, shortages"),
}
ALL = frozenset(MODULES)
ALIASES = {"3D": "3d", "model": "3d", "modelling": "3d", "modeling": "3d", "cad": "3d", "simulation": "sim",
           "fea": "sim", "drawings": "drawing", "2d": "drawing", "dxf": "drawing", "inv": "inventory",
           "holocron": "inventory", "all": "all", "full": "all"}
SETTINGS_KEY = "modules/enabled"

# ribbon tabs and sidebar islands owned by each module (everything else is shared)
TABS = {"3d": {"Model", "Features", "Surfaces", "Assembly", "Manufacture", "Icarus"},
        "sim": {"Simulation"}, "drawing": {"Drawing"}, "inventory": {"Inventory"}}
ISLANDS = {"3d": {"mates", "cam", "layers"}, "sim": {"physics", "studies"}}


class ModuleError(ValueError):
    pass


def parse(text: str | None) -> frozenset:
    """'drawing,inventory' / 'sim' / 'all' → frozenset of module keys."""
    if text is None or not str(text).strip():
        return ALL
    out = set()
    for raw in str(text).replace(" ", ",").split(","):
        k = raw.strip()
        if not k:
            continue
        k = ALIASES.get(k, ALIASES.get(k.lower(), k.lower()))
        if k == "all":
            return ALL
        if k not in MODULES:
            raise ModuleError(f"Unknown module '{raw}'. Modules: {', '.join(MODULES)} (or all)")
        out.add(k)
    return frozenset(out) or ALL


def from_argv(argv: list[str]) -> frozenset | None:
    """--only X / --modules X / --only=X → modules, or None when not given."""
    for i, a in enumerate(argv):
        for flag in ("--only", "--modules"):
            if a == flag and i + 1 < len(argv):
                return parse(argv[i + 1])
            if a.startswith(flag + "="):
                return parse(a.split("=", 1)[1])
    return None


def strip_args(argv: list[str]) -> list[str]:
    """argv without the module flags (so the value isn't taken for a file name)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--only", "--modules"):
            skip = True
            continue
        if a.startswith(("--only=", "--modules=")):
            continue
        out.append(a)
    return out


def needs_3d(mods) -> bool:
    return bool({"3d", "sim"} & set(mods))


def describe(mods) -> str:
    mods = set(mods)
    if mods >= ALL:
        return "All modules"
    return " + ".join(MODULES[k][0] for k in MODULES if k in mods)


def load(settings) -> frozenset:
    try:
        return parse(settings.value(SETTINGS_KEY, "all"))
    except ModuleError:
        return ALL


def save(settings, mods):
    settings.setValue(SETTINGS_KEY, "all" if set(mods) >= ALL else ",".join(k for k in MODULES if k in mods))


def hidden_tabs(mods) -> set[str]:
    return set().union(*(TABS[k] for k in MODULES if k not in mods))


def hidden_islands(mods) -> set[str]:
    return set().union(*(ISLANDS.get(k, set()) for k in MODULES if k not in mods))
