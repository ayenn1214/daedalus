"""Automatic CAM: look at a part, decide how to hold it and what to cut, pick tools from what the shop has,
build the operations and post G-code.

analyze()  — per machining direction: holes (by size, through / blind, tapped), pocket floors and steps,
             smallest inside corner radius, features that can't be reached from that side.
recommend() — best side to machine from, and whether a second setup (flip) is needed.
build()    — writes the CAM setup + tool table + operations into the document and plans the toolpaths.
"""
from __future__ import annotations

import math

import numpy as np

from . import cam

DIRS = list(cam.ORIENT)
ENDMILLS = [20, 16, 12, 10, 8, 6, 5, 4, 3, 2]
DRILLS = sorted({round(x * 0.1, 1) for x in range(10, 205, 5)} | {2.5, 3.3, 4.2, 5.0, 6.8, 8.5, 10.2, 6.6, 9.0,
                                                                     11.0, 13.5, 17.5, 3.4, 4.5, 5.5})
FACE_MILLS = [50, 63, 80]


def parse_sizes(text, default):
    """'16, 12, 10 8' → [16.0, 12.0, 10.0, 8.0]."""
    out = []
    for tok in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        try:
            v = float(tok)
            if v > 0:
                out.append(v)
        except ValueError:
            continue
    return sorted(set(out), reverse=True) or list(default)


def _is_inside(body, p):
    try:
        from build123d import Vector
        return body.is_inside(Vector(*p))
    except Exception:  # noqa: BLE001
        return False


def analyze(body, orient="+Z", threads=()) -> dict:
    """Features reachable from the top when the part is turned so `orient` faces the spindle."""
    b = cam.orient_body(body, orient)
    bb = b.bounding_box()
    top, bottom = bb.max.Z, bb.min.Z
    holes = cam.vertical_holes(b)
    reach, hidden = [], []
    for h in holes:
        through = h["z0"] <= bottom + 1e-3 and h["z1"] >= top - 1e-3
        above = (h["x"], h["y"], h["z1"] + 0.05)
        opens_up = through or not _is_inside(b, above)
        h = {**h, "d": 2 * h["r"], "through": through, "depth": h["z1"] - h["z0"]}
        (reach if opens_up else hidden).append(h)
    floors = []
    for i, f, z in cam.floor_faces(b, top):
        c = f.center()
        in_hole = any(math.hypot(c.X - h["x"], c.Y - h["y"]) <= h["r"] + 1e-3 and abs(z - h["z0"]) < 1e-3
                      for h in holes)                          # a blind hole's bottom isn't a pocket floor
        if in_hole or z <= bottom + 1e-3:
            continue
        if not _is_inside(b, (c.X, c.Y, top + 1.0)):
            floors.append({"face": i, "z": z, "area": f.area, "depth": top - z})
    # inside corner radii of pockets (concave vertical cylinders that aren't whole holes)
    corner = None
    for f in b.faces():
        if f.geom_type.name != "CYLINDER":
            continue
        try:
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            ad = BRepAdaptor_Surface(f.wrapped)
            cyl = ad.Cylinder()
            d = cyl.Axis().Direction()
            if abs(abs(d.Z()) - 1) > 1e-6:
                continue
            span = abs(ad.LastUParameter() - ad.FirstUParameter())
            if span >= 2 * math.pi - 1e-3:
                continue
            c = f.center()
            n = f.normal_at(c)
            loc = cyl.Axis().Location()
            radial = np.array([c.X - loc.X(), c.Y - loc.Y()])
            if np.dot(radial, [n.X, n.Y]) < 0:                 # concave
                r = cyl.Radius()
                corner = r if corner is None else min(corner, r)
        except Exception:  # noqa: BLE001
            continue
    down = 0.0
    down_n = 0
    side_holes = 0
    for f in b.faces():
        try:
            n = f.normal_at(f.center())
        except Exception:  # noqa: BLE001
            continue
        if f.geom_type.name == "PLANE" and n.Z < -0.5 and f.center().Z > bottom + 1e-3:
            c = f.center()
            if any(math.hypot(c.X - h["x"], c.Y - h["y"]) <= h["r"] + 1e-3 for h in hidden):
                continue                                       # bottom of a hole from the far side
            down += f.area                                     # faces looking down above the bottom
            down_n += 1
        if f.geom_type.name == "CYLINDER":
            try:
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                d = BRepAdaptor_Surface(f.wrapped).Cylinder().Axis().Direction()
                if abs(d.Z()) < 1e-3:
                    side_holes += 1
            except Exception:  # noqa: BLE001
                pass
    tapped = []
    for th in threads or ():
        try:
            tapped.append(th)
        except Exception:  # noqa: BLE001
            pass
    top_faces = [f for f in b.faces() if f.geom_type.name == "PLANE" and f.normal_at().Z > 0.999
                 and abs(f.center().Z - top) < 1e-3]
    score = len(reach) + 3 * len(floors) - len(hidden) - 3 * down_n - 0.5 * side_holes
    return {"orient": orient, "size": [bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, top - bottom],
            "holes": reach, "hidden_holes": hidden, "floors": floors, "corner_r": corner,
            "down_area": down, "side_features": side_holes, "flat_top": bool(top_faces), "score": score}


def recommend(body, threads=()) -> dict:
    """Analyses for all six sides, best first; `second` is the side to finish what the first can't reach."""
    res = sorted((analyze(body, d, threads) for d in DIRS), key=lambda a: (-a["score"], a["orient"] != "+Z"))
    best = res[0]
    opp = {"+Z": "-Z", "-Z": "+Z", "+X": "-X", "-X": "+X", "+Y": "-Y", "-Y": "+Y"}[best["orient"]]
    second = next(a for a in res if a["orient"] == opp)
    need2 = bool(best["hidden_holes"]) or best["down_area"] > 1e-3
    return {"best": best, "all": res, "second": second if need2 else None}


def _tool(tools, kind, dia, name, pitch=0.0, flutes=None):
    for t in tools:
        if t["kind"] == kind and abs(t["diameter"] - dia) < 1e-6:
            return t["number"]
    n = max([t["number"] for t in tools], default=0) + 1
    tools.append({"number": n, "kind": kind, "diameter": round(float(dia), 4),
                  "flutes": flutes or (2 if kind in ("drill", "spot", "tap") else 3), "name": name,
                  "rpm": 0.0, "feed": 0.0, "pitch": pitch})
    return n


def build(doc, name, opts: dict) -> dict:
    """Make the CAM program. opts: orient, post, units, origin, margin [x,y,z], max_rpm, coolant, program,
    endmills [mm], drills [mm], face_mill (mm or 0), facing, pockets, drilling, spot, tapping, profile,
    cycle, retract, peck, breakthrough. Returns {summary, warnings, tools, ops}."""
    body = doc.placed_bodies().get(name)
    if body is None:
        raise ValueError(f"{name} has no solid")
    orient = opts.get("orient", "+Z")
    threads = [t for t in doc.result.threads.values() if t.get("object", name) == name] \
        if hasattr(doc.result, "threads") else []
    a = analyze(body, orient, threads)
    b = cam.orient_body(body, orient)
    warn = []
    tools: list = []
    ops: list = []
    mills = parse_sizes(opts.get("endmills"), ENDMILLS)
    drills = parse_sizes(opts.get("drills"), DRILLS)
    sx, sy, _sz = a["size"]
    # facing
    if opts.get("facing", True) and float((opts.get("margin") or [2, 2, 1])[2]) > 0:
        fm = float(opts.get("face_mill") or 0)
        if fm > 0:
            ops.append({"type": "facing", "tool": _tool(tools, "face", fm, f"Ø{fm:g} face mill", flutes=5)})
        else:
            big = mills[0]
            ops.append({"type": "facing", "tool": _tool(tools, "flat", big, f"Ø{big:g} end mill")})
    # pockets: largest end mill that fits every inside corner (and the CAM fit check)
    if opts.get("pockets", True) and a["floors"]:
        limit = 2 * a["corner_r"] * 0.98 if a["corner_r"] else max(mills)
        fit = [m for m in mills if m <= limit + 1e-9] or [min(mills)]
        chosen = None
        for m in fit:
            trial_tools = [{"number": 1, "kind": "flat", "diameter": m, "flutes": 3, "name": "", "rpm": 0.0,
                            "feed": 0.0, "pitch": 0.0}]
            trial = {**cam.default_setup(), **_setup(opts, name), "tools": trial_tools,
                     "ops": [{"type": "pocket", "tool": 1}]}
            try:
                r = cam.plan(doc, trial)
                if not any("doesn't fit" in n for n in r["ops"][0].notes):
                    chosen = m
                    break
            except Exception:  # noqa: BLE001
                continue
        if chosen is None:
            chosen = fit[-1]
            warn.append(f"Some pocket areas are narrower than the smallest end mill (Ø{chosen:g}) — they'll be "
                        "left; add a smaller tool")
        if not a["corner_r"]:
            warn.append(f"Pocket inside corners are sharp — they'll be left at R{chosen / 2:g} (add corner radii "
                        "in the model, or plan EDM / a corner-cleaning op)")
        if a["corner_r"] and chosen / 2 > a["corner_r"] + 1e-6:
            warn.append(f"Inside corners R{a['corner_r']:g} are smaller than the Ø{chosen:g} tool — corners stay "
                        f"R{chosen / 2:g}")
        ops.append({"type": "pocket", "tool": _tool(tools, "flat", chosen, f"Ø{chosen:g} end mill")})
    # holes
    holes = [h for h in a["holes"] if not (opts.get("second") and h["through"])]
    thread_at = {}
    for th in threads:
        try:
            o = th["axis"][0]
            thread_at[(round(o[0], 3), round(o[1], 3))] = th
        except Exception:  # noqa: BLE001
            continue
    if holes and opts.get("drilling", True):
        if opts.get("spot", True):
            sp = min(10.0, max(4.0, max(h["d"] for h in holes) + 1))
            ops.append({"type": "drill", "tool": _tool(tools, "spot", 10.0 if sp > 6 else 6.0,
                                                       "90° spot drill"), "cycle": "G82", "dwell": 0.2,
                        "retract": opts.get("retract", "initial")})
        for d in sorted({round(h["d"], 3) for h in holes}):
            exact = [x for x in drills if abs(x - d) <= 0.02]
            if exact:
                dr = exact[0]
            else:
                smaller = [x for x in drills if x < d]
                dr = max(smaller) if smaller else min(drills)
                n = sum(1 for h in holes if abs(h["d"] - d) < 1e-3)
                warn.append(f"No Ø{d:g} drill in your list — {n} hole(s) drilled Ø{dr:g}; ream / bore / "
                            "interpolate to size")
            if opts.get("second") and all(h["through"] for h in holes if abs(h["d"] - d) < 1e-3):
                continue                                         # through holes were drilled in setup 1
            op = {"type": "drill", "tool": _tool(tools, "drill", dr, f"Ø{dr:g} drill"), "diameter": d,
                  "cycle": opts.get("cycle", "auto"), "retract": opts.get("retract", "initial")}
            if opts.get("second"):
                op["skip_through"] = True
            if opts.get("peck"):
                op["peck"] = float(opts["peck"])
            if opts.get("breakthrough") is not None:
                op["breakthrough"] = float(opts["breakthrough"])
            ops.append(op)
        if opts.get("tapping", True) and thread_at:
            for d in sorted({round(h["d"], 3) for h in holes}):
                ths = [thread_at.get((round(h["x"], 3), round(h["y"], 3))) for h in holes if abs(h["d"] - d) < 1e-3]
                ths = [t for t in ths if t]
                if ths:
                    th = ths[0]
                    ops.append({"type": "drill", "tool": _tool(tools, "tap", float(th["major"]), f"{th['size']} tap",
                                                               float(th["pitch"])), "diameter": d, "cycle": "G84"})
    if a["hidden_holes"]:
        warn.append(f"{len(a['hidden_holes'])} hole(s) open on the other side — machine them in a second setup "
                    f"({_opposite(orient)})")
    if a["down_area"] > 1e-3:
        warn.append("Some faces look away from the spindle (undercuts / steps on the far side) — second setup "
                    "or a different fixture needed")
    if a["side_features"]:
        warn.append(f"{a['side_features']} side feature(s) (horizontal holes / bosses) can't be cut from this "
                    "side — use a side setup or a 4th axis")
    # outside profile: cut the part free last
    if opts.get("profile", True):
        prof = [m for m in mills if m <= 16] or mills
        pm = prof[0]
        ops.append({"type": "profile", "tool": _tool(tools, "flat", pm, f"Ø{pm:g} end mill")})
    if not ops:
        raise ValueError("Nothing to machine with the chosen options")
    # tool numbers in order of first use, starting at T1
    order = []
    for o in ops:
        if o["tool"] not in order:
            order.append(o["tool"])
    remap = {old: i + 1 for i, old in enumerate(order)}
    tools = sorted(({**t, "number": remap[t["number"]]} for t in tools if t["number"] in remap),
                   key=lambda t: t["number"])
    for o in ops:
        o["tool"] = remap[o["tool"]]
    setup = {**cam.default_setup(), **_setup(opts, name), "tools": tools, "ops": ops}
    doc.checkpoint()
    doc.cam = setup
    doc.dirty = True
    summary = doc.cam_plan()
    for o in summary.get("operations", []):
        for n in o.get("notes", []):
            if n not in warn:
                warn.append(f"{o['name']}: {n}")
    doc._emit()
    return {"summary": summary, "warnings": warn, "analysis": a, "tools": tools, "ops": ops}


def _setup(opts, name):
    out = {"object": name}
    for k in ("post", "units", "origin", "margin", "max_rpm", "coolant", "program", "orient", "safe_z",
              "clearance_z"):
        if opts.get(k) is not None:
            out[k] = opts[k]
    return out


def _opposite(o):
    return {"+Z": "-Z", "-Z": "+Z", "+X": "-X", "-X": "+X", "+Y": "-Y", "-Y": "+Y"}[o]


def describe(a: dict) -> list[str]:
    """Plain-language analysis lines for the dialog / copilot."""
    lines = [f"Part {a['size'][0]:.1f} × {a['size'][1]:.1f} × {a['size'][2]:.1f} mm machined from {a['orient']}"]
    if a["holes"]:
        by = {}
        for h in a["holes"]:
            k = (round(h["d"], 2), h["through"])
            by[k] = by.get(k, 0) + 1
        lines.append("Holes: " + ", ".join(f"{n}× Ø{d:g} {'through' if t else 'blind'}"
                                           for (d, t), n in sorted(by.items())))
    else:
        lines.append("No holes reachable from this side")
    if a["floors"]:
        deepest = max(f["depth"] for f in a["floors"])
        lines.append(f"Pockets / steps: {len(a['floors'])} floor(s), deepest {deepest:.2f} mm")
    if a["corner_r"]:
        lines.append(f"Smallest inside corner R{a['corner_r']:.2f} → end mill ≤ Ø{2 * a['corner_r']:.2f}")
    if a["hidden_holes"]:
        lines.append(f"{len(a['hidden_holes'])} hole(s) only reachable from {_opposite(a['orient'])}")
    if a["down_area"] > 1e-3:
        lines.append(f"{a['down_area']:.0f} mm² of faces point away from the spindle")
    if a["side_features"]:
        lines.append(f"{a['side_features']} side feature(s) need another setup")
    return lines
