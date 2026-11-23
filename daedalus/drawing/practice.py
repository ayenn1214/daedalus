"""Working-drawing practice: sheet formats (frame, margins, zoning, title block layout — customisable and saved as
templates), dimensioning standards (ISO 129 / ASME Y14.5 / DIN / JIS / BS presets), the projection symbol,
numbered notes with standard templates, drawing numbering (XXXXX.AA.BB… with your own format), material hatch
patterns and explode lines. Qt-free."""
from __future__ import annotations

import copy
import json
import math
import os
import re
from pathlib import Path

# ------------------------------------------------------------------ dimensioning standards
DIM_STANDARDS = {
    "ISO 129-1 (metric)": {"standard": "ISO", "units": "mm", "decimals": 1, "dim_text": "aligned", "leading_zero": True,
                           "decimal_sep": ",", "trailing_zeros": False, "text_height": 3.5, "dim_text_height": 3.5,
                           "arrow": 3.0, "angle_format": "decimal", "dual_units": False},
    "ISO 129-1 (decimal point)": {"standard": "ISO", "units": "mm", "decimals": 1, "dim_text": "aligned",
                                  "leading_zero": True, "decimal_sep": ".", "trailing_zeros": False,
                                  "text_height": 3.5, "dim_text_height": 3.5, "arrow": 3.0, "angle_format": "decimal",
                                  "dual_units": False},
    "ASME Y14.5 (inch)": {"standard": "ANSI", "units": "in", "decimals": 3, "dim_text": "unidirectional",
                          "leading_zero": False, "decimal_sep": ".", "trailing_zeros": True, "text_height": 3.2,
                          "dim_text_height": 3.2, "arrow": 3.2, "angle_format": "decimal", "dual_units": False},
    "ASME Y14.5M (metric)": {"standard": "ANSI", "units": "mm", "decimals": 1, "dim_text": "unidirectional",
                             "leading_zero": True, "decimal_sep": ".", "trailing_zeros": False, "text_height": 3.5,
                             "dim_text_height": 3.0, "arrow": 3.0, "angle_format": "decimal", "dual_units": False},
    "ASME Y14.5 dual (mm [in])": {"standard": "ANSI", "units": "mm", "decimals": 1, "dim_text": "unidirectional",
                                  "leading_zero": True, "decimal_sep": ".", "trailing_zeros": False,
                                  "text_height": 3.5, "dim_text_height": 3.0, "arrow": 3.0, "angle_format": "decimal",
                                  "dual_units": True},
    "DIN 406 / ISO": {"standard": "ISO", "units": "mm", "decimals": 1, "dim_text": "aligned", "leading_zero": True,
                      "decimal_sep": ",", "trailing_zeros": False, "text_height": 3.5, "dim_text_height": 3.5,
                      "arrow": 2.5, "angle_format": "dms", "dual_units": False},
    "JIS Z 8317": {"standard": "ANSI", "units": "mm", "decimals": 1, "dim_text": "aligned", "leading_zero": True,
                   "decimal_sep": ".", "trailing_zeros": False, "text_height": 3.5, "dim_text_height": 3.5,
                   "arrow": 3.0, "angle_format": "decimal", "dual_units": False},
    "BS 8888": {"standard": "ISO", "units": "mm", "decimals": 1, "dim_text": "aligned", "leading_zero": True,
                "decimal_sep": ".", "trailing_zeros": False, "text_height": 3.5, "dim_text_height": 3.5,
                "arrow": 3.0, "angle_format": "decimal", "dual_units": False},
}


def format_number(v: float, opts, decimals=None, trailing=None) -> str:
    """A value as the standard writes it: decimal sign, leading zero (0.5 / .500), trailing zeros."""
    dec = opts.decimals if decimals is None else decimals
    s = f"{v:.{dec}f}"
    keep = getattr(opts, "trailing_zeros", False) if trailing is None else trailing
    if not keep and "." in s:
        s = s.rstrip("0").rstrip(".")
    if not getattr(opts, "leading_zero", True):
        s = re.sub(r"^(-?)0\.", r"\1.", s)
    elif s.startswith("."):
        s = "0" + s
    if getattr(opts, "decimal_sep", ".") == ",":
        s = s.replace(".", ",")
    return s


def format_angle(deg: float, opts) -> str:
    if getattr(opts, "angle_format", "decimal") == "dms":
        d = abs(deg)
        D = int(d)
        m = (d - D) * 60
        M = int(m)
        S = round((m - M) * 60)
        if S == 60:
            M, S = M + 1, 0
        if M == 60:
            D, M = D + 1, 0
        return f"{D}°" + (f"{M}'" if M or S else "") + (f'{S}"' if S else "")
    return format_number(abs(deg), opts, max(0, opts.decimals - 1)) + "°"


# ------------------------------------------------------------------ title block fields
FIELDS = {  # key: label printed in the cell
    "title": "TITLE", "company": "COMPANY", "number": "DWG NO.", "material": "MATERIAL", "drawn": "DRAWN",
    "checked": "CHECKED", "approved": "APPROVED", "issued": "ISSUED", "scale": "SCALE", "size": "SIZE",
    "sheet": "SHEET", "revision": "REV", "projection": "PROJECTION", "units": "UNITS", "tolerance": "TOLERANCES",
    "finish": "SURFACE FINISH", "cad": "CAD", "file": "FILE", "source": "SOURCE", "mass": "MASS", "date": "DATE",
    "logo": "", "treatment": "TREATMENT", "project": "PROJECT", "customer": "CUSTOMER", "blank": "",
}
SIGNED = ("drawn", "checked", "approved", "issued")      # name + date (key_date)
EDITABLE = ["title", "company", "number", "material", "drawn", "drawn_date", "checked", "checked_date", "approved",
            "approved_date", "issued", "issued_date", "revision", "tolerance", "finish", "treatment", "project",
            "customer", "logo"]

# ------------------------------------------------------------------ sheet formats
_DEF_ROWS = [  # bottom → top; cells [field, relative width]
    {"h": 5, "cells": [["cad", 1]]},
    {"h": 9, "cells": [["scale", 1], ["size", 1], ["sheet", 1], ["revision", 1], ["projection", 1]]},
    {"h": 9, "cells": [["drawn", 1], ["checked", 1], ["approved", 1], ["issued", 1], ["units", 1]]},
    {"h": 9, "cells": [["number", 3], ["material", 2]]},
    {"h": 8, "cells": [["tolerance", 3], ["finish", 2]]},
    {"h": 16, "cells": [["title", 3], ["company", 2]]},
]
FORMATS = {
    "ISO 5457 (filing margin)": {"margins": [20, 10, 10, 10], "zones": True, "zone_size": 100, "frame_weight": 0.7,
                                 "title_block": {"width": 180, "rows": _DEF_ROWS}, "projection_symbol": True,
                                 "trim_marks": False},
    "ISO (equal margins)": {"margins": [10, 10, 10, 10], "zones": True, "zone_size": 100, "frame_weight": 0.7,
                            "title_block": {"width": 180, "rows": _DEF_ROWS}, "projection_symbol": True},
    "ASME Y14.1": {"margins": [12.7, 12.7, 12.7, 12.7], "zones": True, "zone_size": 110, "frame_weight": 0.7,
                   "title_block": {"width": 178, "rows": [
                       {"h": 5, "cells": [["cad", 1]]},
                       {"h": 8, "cells": [["size", 1], ["scale", 1], ["mass", 1], ["sheet", 1], ["revision", 1]]},
                       {"h": 9, "cells": [["number", 3], ["projection", 1], ["units", 1]]},
                       {"h": 9, "cells": [["drawn", 1], ["checked", 1], ["approved", 1], ["material", 2]]},
                       {"h": 8, "cells": [["tolerance", 3], ["finish", 2]]},
                       {"h": 16, "cells": [["company", 2], ["title", 3]]}]},
                   "projection_symbol": True},
    "Compact": {"margins": [10, 10, 10, 10], "zones": False, "zone_size": 100, "frame_weight": 0.5,
                "title_block": {"width": 150, "rows": [
                    {"h": 8, "cells": [["scale", 1], ["sheet", 1], ["revision", 1], ["projection", 1]]},
                    {"h": 8, "cells": [["number", 2], ["drawn", 1], ["material", 1]]},
                    {"h": 14, "cells": [["title", 3], ["company", 1]]}]},
                "projection_symbol": True},
}
DEFAULT_FORMAT = {"ISO": "ISO 5457 (filing margin)", "ANSI": "ASME Y14.1"}


def formats_dir() -> Path:
    return Path(os.environ.get("DAEDALUS_HOME") or Path.home() / ".daedalus") / "sheet_formats"


def _safe(name):
    return re.sub(r"[^\w\- ().]", "_", name).strip() or "format"


def list_formats() -> dict:
    """Built-in formats plus the ones you saved (your saved ones win on a name clash)."""
    out = copy.deepcopy(FORMATS)
    d = formats_dir()
    if d.exists():
        for p in sorted(d.glob("*.json")):
            try:
                f = json.loads(p.read_text(encoding="utf-8"))
                out[f.get("name") or p.stem] = {k: v for k, v in f.items() if k != "name"}
            except (OSError, ValueError):
                pass
    return out


def save_format(name: str, fmt: dict) -> Path:
    check_format(fmt)
    d = formats_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_safe(name)}.json"
    p.write_text(json.dumps({"name": name, **fmt}, indent=2), encoding="utf-8")
    return p


def delete_format(name: str) -> bool:
    p = formats_dir() / f"{_safe(name)}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def check_format(fmt: dict):
    m = fmt.get("margins", [10, 10, 10, 10])
    if len(m) != 4 or any(float(x) < 0 for x in m):
        raise ValueError("margins: [left, bottom, right, top] in mm, ≥ 0")
    tb = fmt.get("title_block") or {}
    for r in tb.get("rows", []):
        if float(r.get("h", 0)) <= 0:
            raise ValueError("Every title block row needs a height > 0")
        for c in r.get("cells", []):
            if c[0] not in FIELDS:
                raise ValueError(f"Unknown title block field '{c[0]}' — use one of {list(FIELDS)}")
            if float(c[1]) <= 0:
                raise ValueError("Cell widths must be > 0")


def sheet_format(drawing, sheet) -> dict:
    """The sheet's format: its own (customised), or the named one, or the standard's default."""
    fm = getattr(sheet, "format", None) or {}
    if fm.get("custom"):
        base = copy.deepcopy(FORMATS[DEFAULT_FORMAT.get(drawing.options.standard, "ISO 5457 (filing margin)")])
        base.update(fm["custom"])
        return base
    allf = list_formats()
    name = fm.get("name") or DEFAULT_FORMAT.get(drawing.options.standard, "ISO 5457 (filing margin)")
    return copy.deepcopy(allf.get(name) or FORMATS["ISO 5457 (filing margin)"])


def title_block_rect(drawing, sheet):
    """(x0, y0, x1, y1) of the title block on the sheet."""
    W, H = sheet.dims()
    f = sheet_format(drawing, sheet)
    L, B, R, T = (float(x) for x in f.get("margins", [10, 10, 10, 10]))
    tb = f.get("title_block") or {}
    w = min(float(tb.get("width", 180)), W - L - R)
    h = sum(float(r["h"]) for r in tb.get("rows", []))
    return W - R - w, B, W - R, B + h


def frame_rect(drawing, sheet):
    W, H = sheet.dims()
    L, B, R, T = (float(x) for x in sheet_format(drawing, sheet).get("margins", [10, 10, 10, 10]))
    return L, B, W - R, H - T


# ------------------------------------------------------------------ primitives
def _line(a, b, style="thin", layer="BORDER"):
    return {"t": "line", "a": list(a), "b": list(b), "style": style, "layer": layer}


def _text(p, s, h=3.5, align="left", layer="TEXT", valign="bottom", angle=0, **kw):
    return {"t": "text", "p": list(p), "text": str(s), "h": h, "align": align, "valign": valign, "angle": angle,
            "style": "text", "layer": layer, **kw}


def _rect(x0, y0, x1, y1, style="border", layer="BORDER"):
    return {"t": "poly", "pts": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "closed": True, "style": style,
            "layer": layer}


def projection_symbol(cx, cy, h, first: bool, layer="BORDER"):
    """ISO 128 / ASME projection symbol (a truncated cone): its circles left of the cone for first angle, right
    of it for third angle."""
    D, d, L, g = h, h * 0.5, h * 1.0, h * 0.45
    total = L + g + D
    x0 = cx - total / 2
    out = []
    if first:
        cc = [x0 + D / 2, cy]
        tx = x0 + D + g
    else:
        tx = x0
        cc = [x0 + L + g + D / 2, cy]
    out += [{"t": "poly", "pts": [[tx, cy - D / 2], [tx + L, cy - d / 2], [tx + L, cy + d / 2], [tx, cy + D / 2]],
             "closed": True, "style": "visible", "layer": layer},
            {"t": "circle", "c": cc, "r": D / 2, "style": "visible", "layer": layer},
            {"t": "circle", "c": cc, "r": d / 2, "style": "visible", "layer": layer},
            _line([x0 - 1, cy], [x0 + total + 1, cy], "center", layer),
            _line([cc[0], cy - D / 2 - 1], [cc[0], cy + D / 2 + 1], "center", layer)]
    return out


def _wrap(s, width_mm, h):
    per = max(8, int(width_mm / (h * 0.62)))
    out = []
    for para in str(s).split("\n"):
        words, line = para.split(), ""
        for w in words:
            if line and len(line) + 1 + len(w) > per:
                out.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        out.append(line)
    return out


def title_block(doc, drawing, sheet, values: dict):
    """The title block drawn from the sheet format's rows."""
    f = sheet_format(drawing, sheet)
    x0, y0, x1, y1 = title_block_rect(drawing, sheet)
    out = [_rect(x0, y0, x1, y1)]
    y = y0
    for row in f["title_block"]["rows"]:
        h = float(row["h"])
        tot = sum(float(c[1]) for c in row["cells"])
        x = x0
        if y > y0:
            out.append(_line([x0, y], [x1, y]))
        for i, (key, wt) in enumerate(row["cells"]):
            w = (x1 - x0) * float(wt) / tot
            if i:
                out.append(_line([x, y], [x, y + h]))
            out += _cell(key, x, y, w, h, values, drawing, f)
            x += w
        y += h
    return out


def _cell(key, x, y, w, h, v, drawing, fmt):
    lab = (fmt.get("labels") or {}).get(key, FIELDS.get(key, key.upper()))
    out = []
    if key == "projection":
        first = drawing.options.standard == "ISO"
        sz = min(h * 0.6, w * 0.2)
        if fmt.get("projection_symbol", True):
            out += projection_symbol(x + w - sz * 1.4 - 1.5, y + h / 2, sz, first)
        out.append(_text([x + 1.2, y + h / 2], "FIRST\nANGLE" if first else "THIRD\nANGLE", min(1.6, h * 0.2),
                         valign="middle", layer="BORDER"))
        return out
    if key == "logo":
        if v.get("logo"):
            out.append({"t": "image", "p": [x + 1, y + 1], "w": w - 2, "h": h - 2, "path": v["logo"],
                        "layer": "TITLE"})
        return out
    if key == "blank":
        return out
    val = str(v.get(key, "") or "")
    if lab:
        out.append(_text([x + 1.2, y + h - 1.0], lab, min(1.8, h * 0.25), valign="top", layer="BORDER"))
    if key == "title":
        hh = min(6.0, h * 0.38)
        lines = _wrap(val, w - 3, hh)[:2]
        out.append(_text([x + 2, y + 2], "\n".join(lines), hh, layer="TITLE"))
    elif key in SIGNED:
        out.append(_text([x + 1.2, y + 1.2], val, min(2.6, h * 0.32), layer="TITLE"))
        if v.get(f"{key}_date"):
            out.append(_text([x + w - 1, y + h - 1.0], v[f"{key}_date"], min(1.6, h * 0.22), "right", "TITLE",
                             "top"))
    elif key in ("cad", "file", "source"):
        out[-1:] = []
        out.append(_text([x + 1.2, y + h / 2], val, min(1.8, h * 0.45), layer="BORDER", valign="middle"))
    else:
        labh = min(1.8, h * 0.25) if lab else 0.0
        room = h - labh - 2.2                              # below the label, above the cell's bottom margin
        hh = max(1.4, min(3.0 if key != "number" else 3.8, room))
        lines = _wrap(val, w - 2.4, hh)[: max(1, int(room / (hh * 1.25)))]
        out.append(_text([x + 1.2, y + 1.0], "\n".join(lines), hh, layer="TITLE"))
    return out


def zones(drawing, sheet):
    f = sheet_format(drawing, sheet)
    if not f.get("zones", True):
        return []
    W, H = sheet.dims()
    L, B, R, T = frame_rect(drawing, sheet)
    size = float(f.get("zone_size", 100))
    nx, ny = max(2, int(round((R - L) / size))), max(2, int(round((T - B) / size)))
    out = []
    for i in range(1, nx):
        x = L + (R - L) * i / nx
        out += [_line([x, B], [x, B - 4]), _line([x, T], [x, T + 4])]
    for j in range(1, ny):
        y = B + (T - B) * j / ny
        out += [_line([L, y], [L - 4, y]), _line([R, y], [R + 4, y])]
    for i in range(nx):
        x = L + (R - L) * (i + 0.5) / nx
        out += [_text([x, B / 2], i + 1, 2.5, "center", "BORDER", "middle"),
                _text([x, T + (H - T) / 2], i + 1, 2.5, "center", "BORDER", "middle")]
    for j in range(ny):
        y = B + (T - B) * (j + 0.5) / ny
        ch = chr(ord("A") + ny - 1 - j)
        out += [_text([L / 2, y], ch, 2.5, "center", "BORDER", "middle"),
                _text([R + (W - R) / 2, y], ch, 2.5, "center", "BORDER", "middle")]
    return out


def zone_of(drawing, sheet, p) -> str:
    """Zone reference (e.g. B4) of a sheet point."""
    f = sheet_format(drawing, sheet)
    L, B, R, T = frame_rect(drawing, sheet)
    size = float(f.get("zone_size", 100))
    nx, ny = max(2, int(round((R - L) / size))), max(2, int(round((T - B) / size)))
    i = min(nx - 1, max(0, int((p[0] - L) / (R - L) * nx)))
    j = min(ny - 1, max(0, int((p[1] - B) / (T - B) * ny)))
    return f"{chr(ord('A') + ny - 1 - j)}{i + 1}"


# ------------------------------------------------------------------ notes
NOTE_TEMPLATES = {
    "Dimensions": "ALL DIMENSIONS APPLY AFTER SURFACE TREATMENT.",
    "Units": "UNLESS OTHERWISE STATED ALL DIMENSIONS ARE IN MM.",
    "Tolerances": "UNLESS OTHERWISE STATED: LINEAR ±0.1, ANGULAR ±0.5°.",
    "Break edges": "BREAK ALL SHARP EDGES 0.5 × 45° MAX.",
    "Heat treatment": "HEAT TREAT IN ACCORDANCE WITH <standard> TO <hardness>.",
    "Welding": "FUSION WELD IN ACCORDANCE WITH <standard>.",
    "Plating / coating": "<CADMIUM> PLATE IN ACCORDANCE WITH <standard>.",
    "Surface preparation": "SURFACE TO BE CLEANED AND POLISHED FOR VACUUM USE.",
    "Inspection": "RADIOGRAPHIC INSPECTION OF ALL WELDS IN ACCORDANCE WITH <standard>.",
    "Threaded fasteners": "TIGHTENING TORQUE OF THREADED FASTENERS SHOULD BE AS LISTED IN TORQUE TABLE.",
    "Threaded inserts": "CLEAN THREADED INSERTS AND INSTALL PER MANUFACTURER'S INSTRUCTIONS.",
    "Deburr": "REMOVE ALL BURRS.",
    "Packaging": "PLACE IN BAG AFTER FINAL ELECTROPOLISHING.",
    "Paint": "PAINT <colour> <spec> AFTER FABRICATION.",
}


def notes_block(drawing, sheet, h=2.8):
    """Numbered notes, lower left of the frame (NOTES: 1. … 2. …)."""
    notes = [n for n in (getattr(sheet, "notes", None) or []) if str(n).strip()]
    if not notes:
        return []
    L, B, R, T = frame_rect(drawing, sheet)
    tx0 = title_block_rect(drawing, sheet)[0]
    width = min(160.0, tx0 - L - 12)
    lines = []
    for i, n in enumerate(notes):
        w = _wrap(n, width - 6, h)
        lines.append((f"{i + 1}.", w[0]))
        lines += [("", s) for s in w[1:]]
    lh = h * 1.55
    x, y = L + 5, B + 5 + lh * len(lines)
    out = [_text([x, y + lh * 0.3], "NOTES:", h * 1.15, layer="NOTES")]
    for k, (num, s) in enumerate(lines):
        yy = y - lh * (k + 1) + lh * 0.3
        if num:
            out.append(_text([x, yy], num, h, layer="NOTES"))
        out.append(_text([x + 6, yy], s, h, layer="NOTES"))
    return out


# ------------------------------------------------------------------ drawing numbering
NUMBERING = {"base": "10000", "separator": ".", "digits": 2, "start": 1, "step": 1, "fill": True,
             "subassemblies_first": True, "standard_parts": False, "prefix": "", "suffix": "", "order": "model",
             "levels": 0}
NUMBERING_HELP = {
    "base": "Main assembly number (the XXXXX part)", "separator": "Between levels (. - _ or none)",
    "digits": "Digits per level (2 → 01, 3 → 001); a list like 2,3 sets each level",
    "start": "First number at each level", "step": "Increment (1 → 01 02 03, 10 → 10 20 30)",
    "fill": "Pad with zeros to full depth (XXXXX.01.00)",
    "subassemblies_first": "Subassemblies before loose parts",
    "standard_parts": "Also number standard / purchased parts",
    "prefix": "Before every number", "suffix": "After every number", "order": "model (as built) | name",
    "levels": "Levels below the main assembly (0 = as deep as the model)",
}


def _numbering(opts: dict | None) -> dict:
    o = dict(NUMBERING)
    o.update({k: v for k, v in (opts or {}).items() if k in NUMBERING})
    return o


def _is_standard(o) -> bool:
    return bool(o.part_no) or (o.std or {}).get("kind") == "purchased"


def drawing_tree(doc, standard_parts=True):
    """[(kind, name, objects, children)] under the main assembly: subassemblies and parts (instances of one part
    are one entry: one drawing)."""
    subs = list(getattr(doc, "subassemblies", []) or [])

    def kids(group):
        out, seen = [], set()
        for s in subs:
            if s.get("parent") == group:
                out.append(["subassembly", s["name"], doc.subassembly_members(s["name"]), None])
        for o in doc.objects:
            if (o.group or None) != group or doc.result.bodies.get(o.name) is None:
                continue
            key = o.part_no or (o.instance_of if o.instance_of and doc.has_obj(o.instance_of) else o.name)
            if key in seen:
                for e in out:
                    if e[0] in ("part", "standard") and e[3] == key:
                        e[2].append(o.name)
                continue
            seen.add(key)
            kind = "standard" if _is_standard(o) else "part"
            if kind == "standard" and not standard_parts:
                continue
            out.append([kind, o.part_no or (key if key != o.name and doc.has_obj(key) else o.name), [o.name], key])
        return out

    def build(group):
        res = []
        for e in kids(group):
            ch = build(e[1]) if e[0] == "subassembly" else []
            res.append((e[0], e[1], e[2], ch))
        return res

    return build(None)


def drawing_numbers(doc, opts: dict | None = None) -> list[dict]:
    """Every drawing the model needs, numbered XXXXX.AA.BB… (main assembly, subassemblies, parts), per your
    numbering options. Rows: level, kind, name, number, objects."""
    o = _numbering(opts)
    tree = drawing_tree(doc, True)

    def depth(nodes):
        return 1 + max((depth(n[3]) for n in nodes), default=0) if nodes else 0
    levels = int(o["levels"]) or max(1, depth(tree))
    digs = o["digits"]
    if isinstance(digs, str):
        digs = [int(x) for x in re.split(r"[,\s]+", digs) if x.strip()]
    if not isinstance(digs, (list, tuple)):
        digs = [int(digs)]
    digs = list(digs) + [digs[-1]] * (levels + 1)
    sep = str(o["separator"])

    def num(path):
        parts = [str(o["base"])]
        for i, k in enumerate(path):
            parts.append(str(int(o["start"]) + k * int(o["step"])).zfill(digs[i]))
        if o["fill"]:
            parts += ["0" * digs[i] for i in range(len(path), levels)]
        return f"{o['prefix']}{sep.join(parts)}{o['suffix']}"

    rows = [{"level": 0, "kind": "assembly", "name": getattr(doc, "name", "") or "Main assembly", "number": num([]),
             "objects": [x.name for x in doc.objects if doc.result.bodies.get(x.name) is not None]}]

    def walk(nodes, path):
        if path and len(path) >= levels:
            return
        ordered = list(nodes)
        if o["order"] == "name":
            ordered.sort(key=lambda n: str(n[1]).lower())
        if o["subassemblies_first"]:
            ordered.sort(key=lambda n: 0 if n[0] == "subassembly" else 1)
        k = 0
        for kind, name, objs, ch in ordered:
            if kind == "standard" and not o["standard_parts"]:
                rows.append({"level": len(path) + 1, "kind": kind, "name": name, "number": "", "objects": objs,
                             "note": "standard part — no detail drawing"})
                continue
            rows.append({"level": len(path) + 1, "kind": kind, "name": name, "number": num(path + [k]),
                         "objects": objs})
            if ch:
                walk(ch, path + [k])
            k += 1

    walk(tree, [])
    return rows


def number_for(doc, objects: list | None, opts: dict | None = None) -> str:
    """The drawing number for a drawing of these objects (all = the main assembly)."""
    rows = drawing_numbers(doc, opts)
    want = set(objects or [])
    if not want or want >= set(rows[0]["objects"]):
        return rows[0]["number"]
    for r in rows[1:]:
        if r["number"] and set(r["objects"]) == want:
            return r["number"]
    for r in rows[1:]:
        if r["number"] and want <= set(r["objects"]) and r["kind"] == "subassembly":
            return r["number"]
    one = next(iter(want)) if len(want) == 1 else None
    for r in rows[1:]:
        if one and one in r["objects"] and r["number"]:
            return r["number"]
    return ""


def object_numbers(doc, opts: dict | None = None) -> dict:
    """{object name: drawing number} — for the parts list's DWG NO. column."""
    out = {}
    for r in drawing_numbers(doc, opts)[1:]:
        if r["kind"] in ("part", "standard") and r["number"]:
            for n in r["objects"]:
                out[n] = r["number"]
    return out


# ------------------------------------------------------------------ hatching
HATCH_PATTERNS = {  # families: (angle offset °, spacing ×, phase 0..1, dashed)
    "ANSI31": [(0, 1, 0, False)],                          # general use, cast iron
    "ANSI32": [(0, 1, 0, False), (0, 1, 0.18, False)],     # steel
    "ANSI33": [(0, 1, 0, False), (0, 1, 0.5, True)],       # bronze, brass, copper
    "ANSI34": [(0, 2, 0, False), (0, 2, 0.12, False), (0, 2, 0.24, False)],   # plastic, rubber
    "ANSI37": [(0, 1, 0, False), (90, 1, 0, False)],       # lead, zinc, magnesium; sound / heat insulation
    "ANSI38": [(0, 1, 0, False), (90, 2, 0, False)],       # aluminium
    "SOLID": [],
}
MATERIAL_HATCH = [("Aluminum", "ANSI38"), ("Copper alloy", "ANSI33"), ("Magnesium", "ANSI37"),
                  ("Zinc", "ANSI37"), ("Plastic", "ANSI34"), ("Elastomer", "ANSI34"), ("Stainless", "ANSI32"),
                  ("Steel", "ANSI32"), ("Titanium", "ANSI32"), ("Nickel", "ANSI32"), ("Cast iron", "ANSI31")]


def hatch_for(material) -> str:
    cat = getattr(material, "category", "") or ""
    name = getattr(material, "name", "") or str(material)
    for key, pat in MATERIAL_HATCH:
        if key.lower() in cat.lower() or key.lower() in name.lower():
            return pat
    return "ANSI31"


def hatch_families(pattern: str):
    return HATCH_PATTERNS.get(pattern, HATCH_PATTERNS["ANSI31"])


# ------------------------------------------------------------------ chain / baseline dimensions
def dimension_set(kind: str, points, axis="x", at=None, step=8.0, unit=None):
    """Chain (feature to feature) or baseline (all from the first point) dimensions along x or y.
    at = the dimension line position (y for axis x, x for axis y) of the first row."""
    pts = sorted([list(map(float, p)) for p in points], key=lambda q: q[0 if axis == "x" else 1])
    if len(pts) < 2:
        raise ValueError("Give at least two points")
    k = "h" if axis == "x" else "v"
    if at is None:
        at = (max(q[1] for q in pts) + 10) if axis == "x" else (max(q[0] for q in pts) + 10)
    out = []
    if kind == "chain":
        for a, b in zip(pts, pts[1:]):
            pos = [(a[0] + b[0]) / 2, at] if axis == "x" else [at, (a[1] + b[1]) / 2]
            out.append({"t": "dim", "kind": k, "p1": a, "p2": b, "at": pos})
    elif kind == "baseline":
        for i, b in enumerate(pts[1:]):
            off = at + i * step
            pos = [(pts[0][0] + b[0]) / 2, off] if axis == "x" else [off, (pts[0][1] + b[1]) / 2]
            out.append({"t": "dim", "kind": k, "p1": pts[0], "p2": b, "at": pos})
    else:
        raise ValueError("kind: chain | baseline")
    if unit:
        for d in out:
            d["unit"] = unit
    return out


# ------------------------------------------------------------------ oblique
def oblique(shape, depth_dir=(0, 1, 0), angle=45.0, ratio=0.5):
    """Shear a shape so its front orthographic view is an oblique view (cabinet ratio 0.5, cavalier 1.0):
    receding depth goes up-right at the angle."""
    from build123d import Shape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ
    a = math.radians(angle)
    k = ratio
    # front view: x right, z up, depth +y (away from the viewer) → x += k·cos·y, z += k·sin·y
    m = gp_Mat(1, k * math.cos(a), 0, 0, 1, 0, 0, k * math.sin(a), 1)
    g = gp_GTrsf(m, gp_XYZ(0, 0, 0))
    t = BRepBuilderAPI_GTransform(shape.wrapped, g, True)
    return Shape.cast(t.Shape())


# ------------------------------------------------------------------ section helpers
def section_label(v) -> str:
    sec = v.get("section") or {}
    nm = sec.get("name", "A")
    typ = sec.get("type", "full")
    if typ == "revolved":
        return ""
    return {"half": f"HALF SECTION {nm}-{nm}", "removed": f"SECTION {nm}-{nm}", "offset": f"SECTION {nm}-{nm}",
            "aligned": f"SECTION {nm}-{nm}"}.get(typ, f"SECTION {nm}-{nm}")
