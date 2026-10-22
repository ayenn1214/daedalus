"""v2.8 drawing annotations: GD&T feature control frames, datum feature symbols, surface finish symbols, ordinate
dimensions, revision tables, and broken views. Qt-free; everything explodes to lines / polys / text in sheet mm.

Primitives (sheet items):
  {"t": "gdt", "p": [x, y] (frame lower-left), "symbol": "position", "tol": "0.1", "dia": true, "mod": "M",
   "tol2": "", "datums": ["A", "B|M", "C"], "leader": [x, y] (optional arrow to the feature), "h": 3.5}
  {"t": "datum", "p": [x, y] (on the feature), "at": [x, y] (box centre), "name": "A"}
  {"t": "finish", "p": [x, y] (tip on the surface), "ra": "1.6", "method": "any|removal|no_removal",
   "angle": 0, "lay": "", "note": ""}
  {"t": "dim", "kind": "ordinate", "p1": origin, "p2": point, "at": text position, "axis": "x"|"y"}
"""
from __future__ import annotations

import math

GDT = {  # name: (glyph, needs datums)
    "straightness": ("⏤", False), "flatness": ("⏥", False), "circularity": ("○", False),
    "cylindricity": ("⌭", False), "profile_line": ("⌒", None), "profile_surface": ("⌓", None),
    "angularity": ("∠", True), "perpendicularity": ("⟂", True), "parallelism": ("∥", True),
    "position": ("⌖", None), "concentricity": ("◎", True), "symmetry": ("⌯", True),
    "circular_runout": ("↗", True), "total_runout": ("⌰", True),
}
MODIFIERS = {"M": "Ⓜ", "L": "Ⓛ", "F": "Ⓕ", "P": "Ⓟ", "T": "Ⓣ", "S": "Ⓢ", "U": "Ⓤ", "": ""}
FINISH_METHODS = ("any", "removal", "no_removal")


def _line(a, b, style="dim", layer="DIM"):
    return {"t": "line", "a": list(a), "b": list(b), "style": style, "layer": layer}


def _text(p, s, h, align="center", valign="middle", angle=0.0, layer="DIM"):
    return {"t": "text", "p": list(p), "text": str(s), "h": h, "align": align, "valign": valign, "angle": angle,
            "style": "text", "layer": layer}


def _rect(x0, y0, x1, y1, layer="DIM"):
    return {"t": "poly", "pts": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "closed": True, "style": "dim",
            "layer": layer}


def _arrow(tip, direction, size):
    from .render import _arrow as a
    return a(tip, direction, size)


def text_w(s, h):
    return max(len(str(s)), 1) * h * 0.62


# ------------------------------------------------------------------- GD&T ---

def gdt_cells(p) -> list[str]:
    sym = GDT.get(p.get("symbol", "position"), ("⌖", None))[0]
    tol = ("Ø" if p.get("dia") else "") + str(p.get("tol", "0.1")) + MODIFIERS.get(p.get("mod", ""), "")
    cells = [sym, tol]
    if p.get("tol2"):
        cells.append(str(p["tol2"]))
    for d in p.get("datums") or []:
        name, _, mod = str(d).partition("|")
        cells.append(name + MODIFIERS.get(mod, ""))
    return cells


def explode_gdt(p, opts=None):
    h = float(p.get("h", 3.5))
    lay = p.get("layer", "DIM")
    x, y = p.get("p", [0, 0])
    H = h * 1.9
    out = []
    cx = x
    widths = []
    for i, c in enumerate(gdt_cells(p)):
        w = H if i == 0 else max(H, text_w(c, h) + h * 0.9)
        widths.append(w)
        out.append(_rect(cx, y, cx + w, y + H, lay))
        out.append(_text([cx + w / 2, y + H / 2], c, h * (1.15 if i == 0 else 1.0), layer=lay))
        cx += w
    if p.get("leader"):
        lx, ly = p["leader"]
        start = [x, y + H / 2] if lx < x else [cx, y + H / 2]
        knee = [start[0] + (-h * 1.5 if lx < x else h * 1.5), start[1]]
        out.append(_line(start, knee, layer=lay))
        out.append(_line(knee, [lx, ly], layer=lay))
        out.append(_arrow([lx, ly], (lx - knee[0], ly - knee[1]), (opts.arrow if opts else 3.0)))
    return out


def gdt_bbox(p):
    h = float(p.get("h", 3.5))
    H = h * 1.9
    w = sum(H if i == 0 else max(H, text_w(c, h) + h * 0.9) for i, c in enumerate(gdt_cells(p)))
    x, y = p.get("p", [0, 0])
    return x, y, x + w, y + H


# ------------------------------------------------------------------ datum ---

def explode_datum(p, opts=None):
    h = float(p.get("h", 3.5))
    lay = p.get("layer", "DIM")
    tip, at = p.get("p", [0, 0]), p.get("at", [0, 10])
    dx, dy = at[0] - tip[0], at[1] - tip[1]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    s = h * 0.9                                               # filled triangle on the feature
    base = [tip[0] + ux * s, tip[1] + uy * s]
    tri = {"t": "poly", "pts": [list(tip) if False else [tip[0] - uy * s * 0.6, tip[1] + ux * s * 0.6],
                                [tip[0] + uy * s * 0.6, tip[1] - ux * s * 0.6], base], "closed": True,
           "fill": True, "style": "dim", "layer": lay}
    B = h * 1.9
    box_near = [at[0] - ux * B / 2, at[1] - uy * B / 2]
    return [tri, _line(base, box_near, layer=lay), _rect(at[0] - B / 2, at[1] - B / 2, at[0] + B / 2, at[1] + B / 2, lay),
            _text(at, p.get("name", "A"), h * 1.1, layer=lay)]


# ---------------------------------------------------------- surface finish ---

def explode_finish(p, opts=None):
    h = float(p.get("h", 3.0))
    lay = p.get("layer", "DIM")
    tip = p.get("p", [0, 0])
    ang = math.radians(float(p.get("angle", 0.0)))

    def R(x, y):
        return [tip[0] + x * math.cos(ang) - y * math.sin(ang), tip[1] + x * math.sin(ang) + y * math.cos(ang)]
    s = h * 1.4
    left = R(-s * math.tan(math.radians(30)), s)
    right = R(2 * s * math.tan(math.radians(30)), 2 * s)
    out = [_line(left, tip, "thin", lay), _line(tip, right, "thin", lay)]
    m = p.get("method", "any")
    if m == "removal":
        out.append(_line(left, R(s * math.tan(math.radians(30)), s), "thin", lay))
    elif m == "no_removal":
        r = s * 0.32
        out.append({"t": "circle", "c": R(0, s * 0.62), "r": r, "style": "thin", "layer": lay})
    if p.get("ra") or p.get("note"):
        bar_end = R(2 * s * math.tan(math.radians(30)) + h * 4.5, 2 * s)
        out.append(_line(right, bar_end, "thin", lay))
        if p.get("ra"):
            out.append(_text(R(2 * s * math.tan(math.radians(30)) + h * 0.4, 2 * s + h * 0.35),
                             f"Ra {p['ra']}", h * 0.85, "left", "bottom", math.degrees(ang), lay))
        if p.get("note"):
            out.append(_text(R(2 * s * math.tan(math.radians(30)) + h * 0.4, 2 * s - h * 0.2), p["note"], h * 0.75,
                             "left", "top", math.degrees(ang), lay))
    if p.get("lay"):
        out.append(_text(R(s * 0.9, s * 0.35), p["lay"], h * 0.75, "left", "middle", math.degrees(ang), lay))
    return out


# ----------------------------------------------------------- ordinate dims ---

def ordinate_value(d, opts):
    from .views import fmt
    ax = 0 if d.get("axis", "x") == "x" else 1
    k = d.get("_unit", 1.0)
    v = abs(d.get("p2", [0, 0])[ax] - d.get("p1", [0, 0])[ax]) * k
    s = fmt(v, opts)
    if d.get("text"):
        s = d["text"].replace("<>", s) if "<>" in d["text"] else d["text"]
    return s


def explode_ordinate(d, opts):
    th = opts.dim_text_height
    lay = d.get("layer", "DIM")
    p2, at = d.get("p2", [0, 0]), d.get("at", [0, 0])
    txt = ordinate_value(d, opts)
    out = []
    if d.get("axis", "x") == "x":                             # x value: vertical leader, vertical text
        sg = 1 if at[1] >= p2[1] else -1
        jog_y = at[1] - sg * 3.0
        out.append(_line([p2[0], p2[1] + sg * 1.0], [p2[0], jog_y], layer=lay))
        out.append(_line([p2[0], jog_y], [at[0], at[1] - sg * 1.5], layer=lay))
        out.append(_line([at[0], at[1] - sg * 1.5], at, layer=lay))
        out.append(_text([at[0] - 0.6, at[1] + sg * 0.8], txt, th, "left" if sg > 0 else "right", "bottom", 90.0, lay))
    else:
        sg = 1 if at[0] >= p2[0] else -1
        jog_x = at[0] - sg * 3.0
        out.append(_line([p2[0] + sg * 1.0, p2[1]], [jog_x, p2[1]], layer=lay))
        out.append(_line([jog_x, p2[1]], [at[0] - sg * 1.5, at[1]], layer=lay))
        out.append(_line([at[0] - sg * 1.5, at[1]], at, layer=lay))
        out.append(_text([at[0] + sg * 0.8, at[1] + 0.6], txt, th, "left" if sg > 0 else "right", "bottom", 0.0, lay))
    return out


def ordinate_set(origin, points, axis="x", offset=10.0, unit=None) -> list[dict]:
    """Ordinate dimensions from `origin` to each point, text lined up `offset` mm beyond the furthest point."""
    ax = 0 if axis == "x" else 1
    other = 1 - ax
    line = (max(q[other] for q in [origin, *points]) + offset) if offset >= 0 else \
        (min(q[other] for q in [origin, *points]) + offset)
    out = []
    seen = []
    for q in [origin, *points]:
        if any(abs(q[ax] - s_) < 1e-6 for s_ in seen):
            continue
        seen.append(q[ax])
        at = [q[0], line] if axis == "x" else [line, q[1]]
        d = {"t": "dim", "kind": "ordinate", "axis": axis, "p1": list(origin), "p2": list(q), "at": at}
        if unit:
            d["unit"] = unit
        out.append(d)
    return out


# ---------------------------------------------------------- revision table ---

REV_COLS = [("ZONE", 14), ("REV", 12), ("DESCRIPTION", 72), ("DATE", 24), ("APPROVED", 24)]


def revision_table(sheet) -> list[dict]:
    revs = getattr(sheet, "revisions", []) or []
    if not revs:
        return []
    W, H = sheet.dims()
    if not W:
        return []
    m = 10.0
    tw = sum(w for _, w in REV_COLS)
    rh = 6.0
    x0, ytop = W - m - tw, H - m
    out = [_rect(x0, ytop - rh, x0 + tw, ytop, "REVISIONS"),
           _text([x0 + tw / 2, ytop - rh / 2], "REVISIONS", 3.0, layer="REVISIONS")]
    y = ytop - rh
    for row in [None, *revs]:
        out.append(_rect(x0, y - rh, x0 + tw, y, "REVISIONS"))
        x = x0
        vals = [c for c, _ in REV_COLS] if row is None else [row.get("zone", ""), row.get("rev", ""),
                                                             row.get("description", ""), row.get("date", ""),
                                                             row.get("approved", "")]
        for (lab, w), v in zip(REV_COLS, vals):
            out.append(_line([x, y - rh], [x, y], "thin", "REVISIONS"))
            out.append(_text([x + 1.2, y - rh + 1.6], str(v)[: int(w / 1.9)], 2.3 if row is None else 2.6, "left",
                             "bottom", 0.0, "REVISIONS"))
            x += w
        y -= rh
    return out


def next_revision(revs) -> str:
    if not revs:
        return "A"
    last = str(revs[-1].get("rev", "A")).strip().upper()
    if last.isdigit():
        return str(int(last) + 1)
    letters = "ABCDEFGHJKLMNPRTUVWY"                          # ASME skips I O Q S X Z
    if last in letters and letters.index(last) < len(letters) - 1:
        return letters[letters.index(last) + 1]
    if len(last) == 1:
        return last + "A"
    return last + "1"


# ------------------------------------------------------------- broken views ---

def apply_breaks(view) -> float:
    """Remove the break regions from a computed view and close the gaps. Returns nothing useful; stores the
    removed length per axis in view["_break_shift"] (so dimensions still show true lengths)."""
    breaks = sorted(view.get("breaks") or [], key=lambda b: b.get("a", 0))
    if not breaks or not view.get("_prims"):
        view["_break_shift"] = {"x": 0.0, "y": 0.0}
        return 0.0
    s = max(view.get("_scale", view.get("scale", 1.0)), 1e-9)
    gap = 6.0 / s                                            # 6 sheet-mm gap whatever the scale
    prims = view["_prims"]
    shift = {"x": 0.0, "y": 0.0}
    zig = []
    for b in sorted(breaks, key=lambda b: -b.get("a", 0)):    # right / top first so earlier ones stay put
        ax = 0 if b.get("axis", "x") == "x" else 1
        lo, hi = sorted((float(b["a"]), float(b["b"])))
        if hi - lo <= gap:
            continue
        prims = _cut_band(prims, ax, lo, hi, (hi - lo) - gap)
        shift["xy"[ax]] += (hi - lo) - gap
        zig.append((ax, lo, lo + gap))
    bb = _bbox(prims)
    for ax, a, b in zig:
        for pos in (a, b):
            prims.append(_zigzag(ax, pos, bb))
    view["_prims"] = prims
    from .views import prim_bbox
    view["_bbox"] = prim_bbox(prims)
    view["_circles"] = [{"c": p["c"], "r": p["r"]} for p in prims if p["t"] == "circle" and p.get("style") == "visible"]
    view["_break_shift"] = shift
    return shift["x"] + shift["y"]


def _bbox(prims):
    from .views import prim_bbox
    return prim_bbox(prims)


def _zigzag(ax, pos, bb):
    lo, hi = (bb[1], bb[3]) if ax == 0 else (bb[0], bb[2])
    n = 6
    amp = (hi - lo) * 0.03 + 0.5
    pts = []
    for i in range(n + 1):
        t = lo - amp + (hi - lo + 2 * amp) * i / n
        o = pos + (amp if i % 2 else -amp) * 0.5 * (0 if i in (0, n) else 1)
        pts.append([o, t] if ax == 0 else [t, o])
    return {"t": "poly", "pts": pts, "closed": False, "style": "thin"}


def _sample(p):
    t = p["t"]
    if t == "line":
        return [p["a"], p["b"]]
    if t == "poly":
        return p["pts"] + ([p["pts"][0]] if p.get("closed") else [])
    if t in ("circle", "arc"):
        a0, a1 = (0.0, 360.0) if t == "circle" else (p["a0"], p["a1"] if p["a1"] > p["a0"] else p["a1"] + 360)
        n = 72
        return [[p["c"][0] + p["r"] * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
                 p["c"][1] + p["r"] * math.sin(math.radians(a0 + (a1 - a0) * i / n))] for i in range(n + 1)]
    return []


def _clip_poly(pts, ax, lo, hi):
    """Pieces of a polyline outside (lo, hi) on axis ax."""
    out, run = [], []

    def inside(q):
        return lo < q[ax] < hi

    def cross(a, b, v):
        t = (v - a[ax]) / (b[ax] - a[ax])
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
    for i, q in enumerate(pts):
        if i == 0:
            if not inside(q):
                run = [list(q)]
            continue
        a = pts[i - 1]
        ia, iq = inside(a), inside(q)
        if not ia and not iq:
            if (a[ax] <= lo and q[ax] >= hi) or (a[ax] >= hi and q[ax] <= lo):   # jumps over the band
                v1, v2 = (lo, hi) if a[ax] <= lo else (hi, lo)
                run.append(cross(a, q, v1))
                out.append(run)
                run = [cross(a, q, v2), list(q)]
            else:
                run.append(list(q))
        elif not ia and iq:
            run.append(cross(a, q, lo if a[ax] <= lo else hi))
            out.append(run)
            run = []
        elif ia and not iq:
            run = [cross(a, q, lo if q[ax] <= lo else hi), list(q)]
    if len(run) > 1:
        out.append(run)
    return [r for r in out if len(r) > 1]


def _cut_band(prims, ax, lo, hi, dshift):
    out = []

    def sh(q):
        q = list(q)
        if q[ax] >= hi - 1e-9:
            q[ax] -= dshift
        return q
    for p in prims:
        t = p["t"]
        if t == "text":
            q = dict(p)
            if p["p"][ax] > lo:
                if p["p"][ax] < hi:
                    continue
                q["p"] = sh(p["p"])
            out.append(q)
            continue
        if t == "hatch":
            loops = []
            for lp in p["loops"]:
                pieces = _clip_poly(lp + [lp[0]], ax, lo, hi)
                for pc in pieces:
                    loops.append([sh(q) for q in pc])
            if loops:
                out.append({**p, "loops": loops})
            continue
        pts = _sample(p)
        if not pts:
            out.append(p)
            continue
        vals = [q[ax] for q in pts]
        if max(vals) <= lo + 1e-9 or min(vals) >= hi - 1e-9:
            if min(vals) >= hi - 1e-9:                         # wholly beyond: move it
                q = dict(p)
                for k in ("a", "b", "c"):
                    if k in q:
                        q[k] = sh(q[k]) if k != "c" else [q["c"][0] - (dshift if ax == 0 else 0),
                                                          q["c"][1] - (dshift if ax == 1 else 0)]
                if "pts" in q:
                    q["pts"] = [sh(v) for v in q["pts"]]
                out.append(q)
            else:
                out.append(p)
            continue
        for run in _clip_poly(pts, ax, lo, hi):
            out.append({"t": "poly", "pts": [sh(q) for q in run], "closed": False,
                        "style": p.get("style", "visible")})
    return out


def explode_v28(p, opts):
    t = p["t"]
    if t == "gdt":
        return explode_gdt(p, opts)
    if t == "datum":
        return explode_datum(p, opts)
    if t == "finish":
        return explode_finish(p, opts)
    return None
