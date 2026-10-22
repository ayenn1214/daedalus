"""Turn a drawing sheet into flat sheet-space primitives, and paint/export them with Qt (PDF, SVG, PNG).

flatten() is Qt-free: views are transformed to sheet mm, automatic annotations added, the title block,
border, BOM table and balloons generated. explode_dim() turns a dimension into lines, arrow heads and
text. paint() draws the result with a QPainter.
"""
from __future__ import annotations

import datetime as _dt
import math

from .model import STYLES, Drawing, Sheet, scale_factor, scale_text
from .views import fmt, prim_bbox

LAYER_OF = {"visible": "VISIBLE", "hidden": "HIDDEN", "center": "CENTER", "dim": "DIM", "thin": "THIN",
            "hatch": "HATCH", "text": "TEXT", "border": "BORDER", "section": "SECTION",
            "construction": "CONSTRUCTION"}

TITLE_FIELDS = ["title", "number", "material", "author", "checked", "date", "company", "revision"]


# ------------------------------------------------------------ geometry -----

def _xf(p, view):
    s = view["scale"]
    return [view["x"] + p[0] * s, view["y"] + p[1] * s]


def transform(prim, view):
    """A view-local primitive in sheet mm."""
    s = view["scale"]
    q = dict(prim)
    t = prim["t"]
    if t == "line":
        q["a"], q["b"] = _xf(prim["a"], view), _xf(prim["b"], view)
    elif t == "poly":
        q["pts"] = [_xf(p, view) for p in prim["pts"]]
    elif t in ("circle", "arc"):
        q["c"], q["r"] = _xf(prim["c"], view), prim["r"] * s
    elif t == "text":
        q["p"] = _xf(prim["p"], view)
    elif t == "hatch":
        q["loops"] = [[_xf(p, view) for p in lp] for lp in prim["loops"]]
    elif t == "dim":
        for k in ("p1", "p2", "at", "c"):
            if k in prim:
                q[k] = _xf(prim[k], view)
        if "r" in prim:
            q["r"] = prim["r"] * s
        q["_unit"] = 1.0 / s                      # sheet mm → model mm
    elif t == "balloon":
        q["p"], q["at"] = _xf(prim["p"], view), _xf(prim["at"], view) if "at" in prim else None
    return q


def dim_value(d, opts):
    """Displayed text of a dimension."""
    if d.get("kind") == "ordinate":
        from .annot import ordinate_value
        return ordinate_value(d, opts)
    if d.get("text") and "<>" not in d["text"]:
        return d["text"]
    k = d.get("_unit", 1.0)
    kind = d.get("kind", "aligned")
    p1, p2 = d.get("p1", [0, 0]), d.get("p2", [0, 0])
    if kind == "h":
        v = abs(p2[0] - p1[0]) * k
        s = fmt(v, opts)
    elif kind == "v":
        v = abs(p2[1] - p1[1]) * k
        s = fmt(v, opts)
    elif kind == "dia":
        s = "Ø" + fmt(2 * d.get("r", 0) * k, opts)
    elif kind == "rad":
        s = "R" + fmt(d.get("r", 0) * k, opts)
    elif kind == "angle":
        c = d.get("c", p1)
        a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
        a2 = math.atan2(p2[1] - c[1], p2[0] - c[0])
        s = f"{abs(math.degrees((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)):.{max(0, opts.decimals - 1)}f}°"
    else:
        v = math.hypot(p2[0] - p1[0], p2[1] - p1[1]) * k
        s = fmt(v, opts)
    if d.get("tol"):
        s += f" {d['tol']}"
    if d.get("text"):
        s = d["text"].replace("<>", s)
    return s


def _arrow(tip, direction, size):
    """A filled arrow head (as a closed poly) pointing along direction at tip."""
    dx, dy = direction
    L = math.hypot(dx, dy) or 1.0
    dx, dy = dx / L, dy / L
    w = size / 3.0
    bx, by = tip[0] - dx * size, tip[1] - dy * size
    return {"t": "poly", "pts": [list(tip), [bx - dy * w, by + dx * w], [bx + dy * w, by - dx * w]], "closed": True,
            "fill": True, "style": "dim"}


def explode_dim(d, opts):
    """Lines, arrow heads and text for a dimension (sheet mm)."""
    if d.get("kind") == "ordinate":
        from .annot import explode_ordinate
        return explode_ordinate(d, opts)
    A, th = opts.arrow, opts.dim_text_height
    txt = dim_value(d, opts)
    out = []
    kind = d.get("kind", "aligned")
    p1, p2, at = d.get("p1", [0, 0]), d.get("p2", [0, 0]), d.get("at", [0, 0])
    lay = d.get("layer", "DIM")

    def L(a, b):
        out.append({"t": "line", "a": list(a), "b": list(b), "style": "dim", "layer": lay})

    def T(p, ang=0.0, align="center"):
        out.append({"t": "text", "p": list(p), "text": txt, "h": th, "angle": ang, "align": align, "style": "text",
                    "layer": lay})

    if kind in ("h", "v", "aligned"):
        if kind == "h":
            u = (1.0, 0.0)
        elif kind == "v":
            u = (0.0, 1.0)
        else:
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            Ln = math.hypot(dx, dy) or 1.0
            u = (dx / Ln, dy / Ln)
        n = (-u[1], u[0])
        off = (at[0] - p1[0]) * n[0] + (at[1] - p1[1]) * n[1]
        q1 = [p1[0] + n[0] * off, p1[1] + n[1] * off]
        t2 = (p2[0] - p1[0]) * u[0] + (p2[1] - p1[1]) * u[1]
        q2 = [q1[0] + u[0] * t2, q1[1] + u[1] * t2]
        sg = 1 if off >= 0 else -1
        gap, ext = 1.0, 2.0
        for p, q in ((p1, q1), (p2, q2)):
            L([p[0] + n[0] * gap * sg, p[1] + n[1] * gap * sg], [q[0] + n[0] * ext * sg, q[1] + n[1] * ext * sg])
        L(q1, q2)
        dirn = (q2[0] - q1[0], q2[1] - q1[1])
        if math.hypot(*dirn) > 2.5 * A:
            out.append(_arrow(q1, (-dirn[0], -dirn[1]), A))
            out.append(_arrow(q2, dirn, A))
        else:                                       # arrows outside on short dims
            out.append(_arrow(q1, dirn, A))
            out.append(_arrow(q2, (-dirn[0], -dirn[1]), A))
        ang = math.degrees(math.atan2(u[1], u[0]))
        if ang > 90.0001 or ang < -89.9999:
            ang -= 180 if ang > 0 else -180
        tpos = (at[0] - p1[0]) * u[0] + (at[1] - p1[1]) * u[1]
        tpos = min(max(tpos, min(0, t2)), max(0, t2)) if abs(t2) > 3 * th else t2 / 2
        ar = math.radians(ang)
        up = (-math.sin(ar), math.cos(ar))
        mid = [q1[0] + u[0] * tpos + up[0] * 0.8, q1[1] + u[1] * tpos + up[1] * 0.8]
        out.append({"t": "text", "p": mid, "text": txt, "h": th, "angle": ang, "align": "center", "valign": "bottom",
                    "style": "text", "layer": lay})
    elif kind in ("dia", "rad"):
        c, r = d.get("c", p1), d.get("r", 0)
        dx, dy = at[0] - c[0], at[1] - c[1]
        Ln = math.hypot(dx, dy) or 1.0
        u = (dx / Ln, dy / Ln)
        edge = [c[0] + u[0] * r, c[1] + u[1] * r]
        if kind == "dia" and d.get("through", False):
            far = [c[0] - u[0] * r, c[1] - u[1] * r]
            L(far, at)
            out.append(_arrow(far, (-u[0], -u[1]), A))
        elif kind == "rad":
            L(c, at)
        else:
            L(edge, at)
        out.append(_arrow(edge, u if Ln < r else (-u[0], -u[1]), A))
        right = at[0] >= c[0]
        tw = len(txt) * th * 0.62
        shelf = [at[0] + (tw + 1 if right else -tw - 1), at[1]]
        L(at, shelf)
        T([at[0] + (0.5 if right else -0.5), at[1] + 0.8], 0, "left" if right else "right")
        out[-1]["valign"] = "bottom"
    elif kind == "angle":
        c = d.get("c", p1)
        r = math.hypot(at[0] - c[0], at[1] - c[1])
        a1 = math.degrees(math.atan2(p1[1] - c[1], p1[0] - c[0]))
        a2 = math.degrees(math.atan2(p2[1] - c[1], p2[0] - c[0]))
        if (a2 - a1) % 360 > 180:
            a1, a2 = a2, a1
        out.append({"t": "arc", "c": list(c), "r": r, "a0": a1, "a1": a2, "style": "dim", "layer": lay})
        for a, sgn in ((a1, 1), (a2, -1)):
            ra = math.radians(a)
            tip = [c[0] + r * math.cos(ra), c[1] + r * math.sin(ra)]
            out.append(_arrow(tip, (math.sin(ra) * sgn, -math.cos(ra) * sgn), A))
        am = math.radians(a1 + ((a2 - a1) % 360) / 2)
        T([c[0] + (r + 1 + th / 2) * math.cos(am), c[1] + (r + 1 + th / 2) * math.sin(am)])
    elif kind == "leader":
        L(p1, at)
        out.append(_arrow(p1, (p1[0] - at[0], p1[1] - at[1]), A))
        T([at[0] + 0.8, at[1]], 0, "left")
    return out


# -------------------------------------------------------- sheet extras -----

def _rect(x0, y0, x1, y1, style="border", layer="BORDER"):
    return {"t": "poly", "pts": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "closed": True, "style": style,
            "layer": layer}


def _text(p, s, h=3.5, align="left", layer="TEXT", valign="bottom", angle=0):
    return {"t": "text", "p": list(p), "text": str(s), "h": h, "align": align, "valign": valign, "angle": angle,
            "style": "text", "layer": layer}


def border_and_title(doc, drawing: Drawing, sheet: Sheet):
    W, H = sheet.dims()
    if not W or not sheet.border:
        return []
    m = 10.0
    out = [_rect(0, 0, W, H, "thin", "BORDER"), _rect(m, m, W - m, H - m)]
    # zone ticks
    nx, ny = max(2, int(W // 100)), max(2, int(H // 100))
    for i in range(1, nx):
        x = m + (W - 2 * m) * i / nx
        out += [{"t": "line", "a": [x, m], "b": [x, m - 4], "style": "thin", "layer": "BORDER"},
                {"t": "line", "a": [x, H - m], "b": [x, H - m + 4], "style": "thin", "layer": "BORDER"}]
    for i in range(nx):
        x = m + (W - 2 * m) * (i + 0.5) / nx
        out += [_text([x, m - 3.2], i + 1, 2.5, "center", "BORDER", "middle"),
                _text([x, H - m + 3.2], i + 1, 2.5, "center", "BORDER", "middle")]
    for j in range(ny):
        y = m + (H - 2 * m) * (j + 0.5) / ny
        ch = chr(ord("A") + ny - 1 - j)
        out += [_text([m - 5, y], ch, 2.5, "center", "BORDER", "middle"),
                _text([W - m + 5, y], ch, 2.5, "center", "BORDER", "middle")]
    # title block, bottom right, 180 × 40
    tw, thh = min(180.0, W - 2 * m), 40.0
    x0, y0 = W - m - tw, m
    out.append(_rect(x0, y0, W - m, y0 + thh))
    t = {"title": doc.name if hasattr(doc, "name") else "Part", "number": "", "material": "", "author": "",
         "checked": "", "date": _dt.date.today().isoformat(), "company": "",
         "revision": (getattr(sheet, "revisions", None) or [{}])[-1].get("rev", "A") or "A", **sheet.title}
    if not t.get("material") and getattr(doc, "objects", None):
        try:
            mats = {doc.material_of(o.name).name for o in doc.objects if o.visible}
            t["material"] = ", ".join(sorted(mats))[:40]
        except Exception:  # noqa: BLE001
            pass
    cols = [x0, x0 + tw * 0.25, x0 + tw * 0.5, x0 + tw * 0.75, W - m]
    rows = [y0, y0 + 10, y0 + 20, y0 + 40]
    for x in cols[1:-1]:
        out.append({"t": "line", "a": [x, y0], "b": [x, y0 + 20], "style": "thin", "layer": "BORDER"})
    for y in rows[1:-1]:
        out.append({"t": "line", "a": [x0, y], "b": [W - m, y], "style": "thin", "layer": "BORDER"})
    cells = [("SCALE", scale_text(sheet.scale)), ("SHEET", f"{drawing.sheets.index(sheet) + 1} / {len(drawing.sheets)}"),
             ("SIZE", sheet.size), ("REV", t["revision"]),
             ("DRAWN", t["author"]), ("CHECKED", t["checked"]), ("DATE", t["date"]), ("DWG NO", t["number"])]
    for i, (lab, val) in enumerate(cells):
        cx, cy = cols[i % 4], rows[1 - i // 4]
        out.append(_text([cx + 1.5, cy + 7], lab, 1.8, layer="BORDER"))
        out.append(_text([cx + 1.5, cy + 1.6], val, 3.0, layer="TITLE"))
    out.append(_text([x0 + 2, y0 + 33], t["title"], 6.0, layer="TITLE"))
    out.append(_text([x0 + 2, y0 + 23], f"MATERIAL: {t['material']}", 2.5, layer="TITLE"))
    if t["company"]:
        out.append(_text([W - m - 2, y0 + 33], t["company"], 3.5, "right", "TITLE"))
    proj = "FIRST ANGLE" if drawing.options.standard == "ISO" else "THIRD ANGLE"
    unit = "mm" if drawing.options.units == "mm" else "inch"
    out.append(_text([W - m - 2, y0 + 23], f"{proj} PROJECTION · DIMENSIONS IN {unit.upper()}", 2.0, "right",
                     "BORDER"))
    return out


def bom_table(doc, sheet: Sheet, at=None):
    try:
        rows = doc.bom()
    except Exception:  # noqa: BLE001
        return []
    if not rows:
        return []
    W, H = sheet.dims()
    cols = [("ITEM", 12), ("PART NO.", 38), ("DESCRIPTION", 70), ("MATERIAL", 40), ("QTY", 12)]
    tw = sum(w for _, w in cols)
    rh = 6.0
    x0 = (at or [W - 10 - tw, 0])[0]
    ytop = (at or [0, 10 + 40 + rh * (len(rows) + 1)])[1]
    out = []
    y = ytop
    for i, row in enumerate([None, *rows]):
        out.append(_rect(x0, y - rh, x0 + tw, y, "thin", "BOM"))
        x = x0
        vals = (["ITEM", "PART NO.", "DESCRIPTION", "MATERIAL", "QTY"] if row is None else
                [row["item"], row.get("part", ""), row.get("description", ""),
                 row.get("material", ""), row.get("qty", 1)])
        for (lab, w), v in zip(cols, vals):
            out.append({"t": "line", "a": [x, y - rh], "b": [x, y], "style": "thin", "layer": "BOM"})
            out.append(_text([x + 1.2, y - rh + 1.6], str(v)[: int(w / 1.9)], 2.5 if row is None else 2.8, layer="BOM"))
            x += w
        y -= rh
    return out


def flatten(doc, drawing: Drawing, sheet: Sheet, include_views=True):
    """Every primitive of the sheet in sheet mm, dimensions kept as 'dim' prims."""
    opts = drawing.options
    out = list(border_and_title(doc, drawing, sheet)) if doc is not None else []
    for v in sheet.views if include_views else []:
        if "_prims" not in v or "scale" not in v:
            continue
        for p in v.get("_prims", []):
            q = transform(p, v)
            q.setdefault("layer", LAYER_OF.get(q.get("style", "visible"), "0"))
            q["view"] = v["id"]
            out.append(q)
        for p in v.get("_auto", []):
            q = transform(p, v)
            q.setdefault("layer", "DIM" if q["t"] == "dim" else LAYER_OF.get(q.get("style", "thin"), "0"))
            q["view"] = v["id"]
            out.append(q)
        x0, y0, x1, y1 = v.get("_bbox", (0, 0, 0, 0))
        label = v.get("label") or ""
        if v.get("kind") == "section":
            label = label or f"SECTION {v.get('section', {}).get('name', 'A')}-{v.get('section', {}).get('name', 'A')}"
        if v.get("kind") == "detail":
            label = label or f"DETAIL {v.get('detail', {}).get('name', 'B')}"
        if label:
            if v["scale"] != scale_factor(sheet.scale):
                label += f" ({_scale_str(v['scale'])})"
            out.append(_text([v["x"], v["y"] + y0 * v["scale"] - 8], label, 3.5, "center", "TEXT", "top"))
        for sv in sheet.views:                       # section line / detail circle markers on the parent
            if sv.get("parent") == v["id"] and sv.get("kind") == "section" and sv.get("section", {}).get("line"):
                a, b = sv["section"]["line"]
                out.append({**transform({"t": "line", "a": a, "b": b}, v), "style": "section", "layer": "SECTION"})
                nm = sv["section"].get("name", "A")
                for pt in (a, b):
                    out.append(_text(_xf(pt, v), nm, 5, "center", "TEXT", "middle"))
            if sv.get("parent") == v["id"] and sv.get("kind") == "detail":
                dt = sv["detail"]
                out.append({**transform({"t": "circle", "c": dt["c"], "r": dt["r"]}, v), "style": "thin",
                            "layer": "THIN"})
                c = _xf(dt["c"], v)
                out.append(_text([c[0] + dt["r"] * v["scale"] + 2, c[1] + dt["r"] * v["scale"]],
                                 dt.get("name", "B"), 5, "left", "TEXT"))
        if drawing.options.balloons and v.get("balloons"):
            from .views import balloons
            for i, b in enumerate(balloons(doc, sheet, v)):
                p = _xf(b["p"], v)
                ang = math.radians(30 + 37 * i)
                at = [p[0] + 18 * math.cos(ang), p[1] + 18 * math.sin(ang)]
                out.append({"t": "balloon", "p": p, "at": at, "text": str(b["item"]), "style": "dim", "layer": "DIM"})
    if drawing.options.bom and any(v.get("bom") for v in sheet.views):
        out += bom_table(doc, sheet)
    from .annot import revision_table
    out += revision_table(sheet)
    for it in sheet.items:
        if not sheet.layers.get(it.get("layer", "0"), {}).get("visible", True):
            continue
        q = dict(it)
        q.setdefault("layer", "0")
        if q["t"] == "dim" and "_unit" not in q:
            q["_unit"] = q.get("unit") or (1.0 / scale_factor(sheet.scale) if sheet.dims()[0] else 1.0)
        out.append(q)
    return out


def _scale_str(s):
    return f"{s:g}:1" if s >= 1 else f"1:{1 / s:g}"


def explode(prims, opts):
    """Replace dims and balloons with plain geometry."""
    out = []
    from .annot import explode_v28
    for p in prims:
        v28 = explode_v28(p, opts) if p["t"] in ("gdt", "datum", "finish") else None
        if v28 is not None:
            out += v28
        elif p["t"] == "dim":
            out += explode_dim(p, opts)
        elif p["t"] == "balloon":
            p0, at = p["p"], p["at"]
            r = 4.0
            dx, dy = p0[0] - at[0], p0[1] - at[1]
            L = math.hypot(dx, dy) or 1
            out.append({"t": "line", "a": [at[0] + dx / L * r, at[1] + dy / L * r], "b": p0, "style": "dim",
                        "layer": p.get("layer", "DIM")})
            out.append({"t": "circle", "c": p0, "r": 0.6, "style": "dim", "fill": True, "layer": p.get("layer", "DIM")})
            out.append({"t": "circle", "c": at, "r": r, "style": "dim", "layer": p.get("layer", "DIM")})
            out.append(_text(at, p["text"], 3.5, "center", p.get("layer", "DIM"), "middle"))
        else:
            out.append(p)
    return out


def extents(prims):
    bb = prim_bbox([p for p in prims if p["t"] != "text"])
    xs, ys = [bb[0], bb[2]], [bb[1], bb[3]]
    for p in prims:
        if p["t"] == "text":
            xs.append(p["p"][0])
            ys.append(p["p"][1])
        if p["t"] in ("dim", "balloon") and p.get("at"):
            xs.append(p["at"][0])
            ys.append(p["at"][1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


# ------------------------------------------------------------------ Qt ------

def _hatch_lines(loops, spacing, angle=45.0):
    """Hatch segments clipped to the loops (even-odd), pure python."""
    a = math.radians(angle)
    u = (math.cos(a), math.sin(a))
    n = (-u[1], u[0])
    pts = [q for lp in loops for q in lp]
    if not pts:
        return []
    proj = [q[0] * n[0] + q[1] * n[1] for q in pts]
    lo, hi = min(proj), max(proj)
    segs = []
    edges = [(lp[i], lp[(i + 1) % len(lp)]) for lp in loops for i in range(len(lp))]
    k = math.floor(lo / spacing) + 1
    while k * spacing < hi:
        c = k * spacing
        ts = []
        for p, q in edges:
            dp, dq = p[0] * n[0] + p[1] * n[1] - c, q[0] * n[0] + q[1] * n[1] - c
            if (dp > 0) != (dq > 0):
                f = dp / (dp - dq)
                x, y = p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f
                ts.append(x * u[0] + y * u[1])
        ts.sort()
        for i in range(0, len(ts) - 1, 2):
            a0, a1 = ts[i], ts[i + 1]
            segs.append(([n[0] * c + u[0] * a0, n[1] * c + u[1] * a0], [n[0] * c + u[0] * a1, n[1] * c + u[1] * a1]))
        k += 1
    return segs


def paint(painter, prims, opts, k=4.0, oy=0.0, ox=0.0, screen=True, selected=(), layers=None, hover=None):
    """Draw exploded sheet primitives. Device = (x*k + ox, oy - y*k)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen

    def P(q):
        return QPointF(q[0] * k + ox, oy - q[1] * k)

    pens = {}

    def pen_for(p, sel=False):
        style = p.get("style", "visible")
        lw, dash, col = STYLES.get(style, STYLES["visible"])
        if layers and p.get("layer") in layers and layers[p["layer"]].get("color") and not p.get("color"):
            c = layers[p["layer"]]["color"]
            col = c if c.lower() not in ("#000000", "#ffffff") else col
        col = p.get("color") or col
        if sel:
            col = "#1f7ae0"
        key = (style, col, sel)
        if key in pens:
            return pens[key]
        w = lw * k
        pen = QPen(QColor(col))
        pen.setWidthF(max(w, 1.0) if screen else w)
        pen.setCapStyle(Qt.RoundCap if not dash else Qt.FlatCap)
        if dash:
            ww = max(pen.widthF(), 1e-3)
            pen.setDashPattern([max(0.5, d * k / ww) for d in dash])
        pens[key] = pen
        return pen

    font_cache = {}
    for idx, p in enumerate(prims):
        t = p["t"]
        sel = id(p) in selected or p.get("_sel")
        painter.setPen(pen_for(p, sel))
        painter.setBrush(Qt.NoBrush)
        if t == "line":
            painter.drawLine(P(p["a"]), P(p["b"]))
        elif t == "poly":
            pts = [P(q) for q in p["pts"]]
            if len(pts) < 2:
                continue
            path = QPainterPath(pts[0])
            for q in pts[1:]:
                path.lineTo(q)
            if p.get("closed"):
                path.closeSubpath()
            if p.get("fill"):
                painter.setBrush(QBrush(painter.pen().color()))
            painter.drawPath(path)
        elif t == "circle":
            c, r = P(p["c"]), p["r"] * k
            if p.get("fill"):
                painter.setBrush(QBrush(painter.pen().color()))
            painter.drawEllipse(c, r, r)
        elif t == "arc":
            c, r = p["c"], p["r"]
            span = (p["a1"] - p["a0"]) % 360 or 360
            rect = QRectF(c[0] * k + ox - r * k, oy - c[1] * k - r * k, 2 * r * k, 2 * r * k)
            painter.drawArc(rect, int(round(p["a0"] * 16)), int(round(span * 16)))
        elif t == "hatch":
            path = QPainterPath()
            path.setFillRule(Qt.OddEvenFill)
            for lp in p["loops"]:
                if len(lp) > 2:
                    path.moveTo(P(lp[0]))
                    for q in lp[1:]:
                        path.lineTo(P(q))
                    path.closeSubpath()
            if p.get("solid"):
                painter.fillPath(path, QColor(p.get("color", "#000000")))
                continue
            for a, b in _hatch_lines(p["loops"], p.get("spacing", opts.hatch_spacing), p.get("angle", 45.0)):
                painter.drawLine(P(a), P(b))
        elif t == "text":
            h = p.get("h", opts.text_height)
            px = max(h * k, 1.0)
            key = (p.get("font", opts.font), round(px * 4))
            f = font_cache.get(key)
            if f is None:
                f = QFont(key[0])
                f.setPixelSize(max(1, int(round(px * 1.0 / 0.72))))   # cap height ≈ 0.72 em
                font_cache[key] = f
            painter.setFont(f)
            painter.save()
            painter.translate(P(p["p"]))
            if p.get("angle"):
                painter.rotate(-p["angle"])
            fm = painter.fontMetrics()
            lines = str(p.get("text", "")).split("\n")
            lh = fm.height()
            tw = max(fm.horizontalAdvance(s) for s in lines) if lines else 0
            al = p.get("align", "left")
            dx = -tw / 2 if al == "center" else (-tw if al == "right" else 0)
            va = p.get("valign", "bottom")
            total = lh * len(lines)
            top = {"bottom": -total + fm.descent(), "middle": -total / 2, "top": 0}.get(va, -total)
            for i, s in enumerate(lines):
                lw_ = fm.horizontalAdvance(s)
                ddx = (tw - lw_) / 2 if al == "center" else ((tw - lw_) if al == "right" else 0)
                painter.drawText(QPointF(dx + ddx, top + fm.ascent() + i * lh), s)
            painter.restore()
        if hover is not None and idx == hover:
            pass


_APP = None


def _ensure_app():
    """Fonts need a QGuiApplication; make an offscreen one for headless use (scripts, tests)."""
    global _APP
    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        import os
        import sys
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication     # a full QApplication so widgets work later too
        _APP = QApplication([])
    return QCoreApplication.instance()


def sheet_prims(doc, drawing, sheet):
    return explode(flatten(doc, drawing, sheet), drawing.options)


def _page_extents(doc, drawing, sheet, prims):
    W, H = sheet.dims()
    if W:
        return 0.0, 0.0, W, H
    x0, y0, x1, y1 = extents(prims)
    pad = max(x1 - x0, y1 - y0) * 0.03 + 2
    return x0 - pad, y0 - pad, x1 + pad, y1 + pad


def export_pdf(doc, drawing: Drawing, path, sheets=None):
    """Vector PDF, one page per sheet, true scale."""
    _ensure_app()
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
    w = QPdfWriter(str(path))
    w.setResolution(1200)
    w.setTitle(drawing.name)
    w.setCreator("Daedalus")
    painter = None
    sheets = sheets or drawing.sheets
    for i, sh in enumerate(sheets):
        prims = sheet_prims(doc, drawing, sh)
        x0, y0, x1, y1 = _page_extents(doc, drawing, sh, prims)
        size = QPageSize(QSizeF(x1 - x0, y1 - y0), QPageSize.Millimeter, sh.name, QPageSize.ExactMatch)
        w.setPageLayout(QPageLayout(size, QPageLayout.Portrait, QMarginsF(0, 0, 0, 0)))
        if i == 0:
            painter = QPainter(w)
        else:
            w.newPage()
        k = 1200 / 25.4
        paint(painter, prims, drawing.options, k=k, ox=-x0 * k, oy=y1 * k, screen=False, layers=sh.layers)
    if painter:
        painter.end()
    return str(path)


def export_svg(doc, drawing: Drawing, path, sheet=None):
    _ensure_app()
    from PySide6.QtCore import QRect, QSize
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgGenerator
    sh = sheet or drawing.sheet()
    prims = sheet_prims(doc, drawing, sh)
    x0, y0, x1, y1 = _page_extents(doc, drawing, sh, prims)
    k = 90 / 25.4 * 1.0
    gen = QSvgGenerator()
    gen.setFileName(str(path))
    gen.setTitle(drawing.name)
    gen.setDescription("Daedalus drawing")
    gen.setResolution(90)
    gen.setSize(QSize(int((x1 - x0) * k) + 1, int((y1 - y0) * k) + 1))
    gen.setViewBox(QRect(0, 0, int((x1 - x0) * k) + 1, int((y1 - y0) * k) + 1))
    painter = QPainter(gen)
    paint(painter, prims, drawing.options, k=k, ox=-x0 * k, oy=y1 * k, screen=False, layers=sh.layers)
    painter.end()
    return str(path)


def export_png(doc, drawing: Drawing, path, sheet=None, dpi=200):
    _ensure_app()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    sh = sheet or drawing.sheet()
    prims = sheet_prims(doc, drawing, sh)
    x0, y0, x1, y1 = _page_extents(doc, drawing, sh, prims)
    k = dpi / 25.4
    img = QImage(max(1, int((x1 - x0) * k)), max(1, int((y1 - y0) * k)), QImage.Format_RGB32)
    img.fill(Qt.white)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    paint(painter, prims, drawing.options, k=k, ox=-x0 * k, oy=y1 * k, screen=False, layers=sh.layers)
    painter.end()
    img.save(str(path))
    return str(path)
