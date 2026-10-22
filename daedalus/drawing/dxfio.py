"""2D file exchange for drawings: DXF (read/write, R12–R2018), DWG (through the free ODA File Converter when it
is installed), SVG (read; write is in render.py), and HPGL/PLT read.

DWF is a closed Autodesk viewing format with no public writer/reader library; use PDF for viewing exchange.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from .model import STYLES, Drawing, Sheet
from .render import LAYER_OF, dim_value, explode, flatten

READ_2D = {".dxf": "DXF", ".dwg": "DWG (via ODA File Converter)", ".svg": "SVG", ".plt": "HPGL", ".hpgl": "HPGL"}
WRITE_2D = {".dxf": "DXF", ".dwg": "DWG (via ODA File Converter)", ".pdf": "PDF", ".svg": "SVG", ".png": "PNG"}
DXF_VERSIONS = {"R12": "R12", "R2000": "R2000", "R2004": "R2004", "R2007": "R2007", "R2010": "R2010",
                "R2013": "R2013", "R2018": "R2018"}

LINETYPE_OF = {"hidden": "HIDDEN", "center": "CENTER_MM", "section": "PHANTOM_MM", "construction": "DASHED_MM"}


class DwgUnavailable(RuntimeError):
    pass


def _find_oda():
    """The ODA installer puts the version in the folder name; point ezdxf at the newest one."""
    import glob
    import sys
    if sys.platform != "win32":
        return
    hits = []
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            hits += glob.glob(os.path.join(root, "ODA", "ODAFileConverter*", "ODAFileConverter.exe"))
    if hits:
        import ezdxf
        try:
            ezdxf.options.set("odafc-addon", "win_exec_path", sorted(hits)[-1])
        except Exception:  # noqa: BLE001
            pass


def dwg_available() -> bool:
    try:
        from ezdxf.addons import odafc
        if not odafc.is_installed():
            _find_oda()
        return bool(odafc.is_installed())
    except Exception:  # noqa: BLE001
        return False


DWG_HELP = ("DWG needs the free ODA File Converter (opendesign.com/guestfiles/oda_file_converter). Install it, "
            "then restart Daedalus — DWG open/save will work through it. Until then use DXF, which every CAD "
            "program (AutoCAD, DraftSight, LibreCAD, SolidWorks…) opens.")


def _aci(hexcol):
    from ezdxf import colors
    h = hexcol.lstrip("#")
    return colors.rgb2int((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))


# ------------------------------------------------------------------ write ---

def write_dxf(doc, drawing: Drawing, path, sheet=None, version="R2010", native_dims=True, all_sheets=False):
    """Write a sheet (or every sheet, side by side) to DXF with layers, linetypes and real DIMENSION entities."""
    import ezdxf
    path = Path(path)
    if path.suffix.lower() == ".dwg":
        return write_dwg(doc, drawing, path, sheet, version)
    dx = ezdxf.new(DXF_VERSIONS.get(version, "R2010"), setup=True, units=4)     # mm
    msp = dx.modelspace()
    opts = drawing.options
    st = dx.dimstyles.get("EZDXF") if "EZDXF" in dx.dimstyles else dx.dimstyles.new("EZDXF")
    st.dxf.dimtxt = opts.dim_text_height
    st.dxf.dimasz = opts.arrow
    st.dxf.dimdec = opts.decimals
    st.dxf.dimexe = 2.0
    st.dxf.dimexo = 1.0
    st.dxf.dimgap = 0.8
    try:
        st.dxf.dimtad = 1
        st.dxf.dimtih = 0
        st.dxf.dimtoh = 0
    except Exception:  # noqa: BLE001
        pass
    for style, name in LINETYPE_OF.items():          # ISO-ish patterns in mm, from the drawing styles
        if name not in dx.linetypes:
            dash = STYLES[style][1]
            pat = [sum(dash)] + [v if i % 2 == 0 else -v for i, v in enumerate(dash)]
            dx.linetypes.add(name, pattern=pat, description=f"{name} " + "__ " * 3)
    layers_made = set()

    def layer(name, style="visible", color=None):
        name = (name or "0").replace("/", "_")
        if name not in layers_made and name != "0":
            if name not in dx.layers:
                lw = int(round(STYLES.get(style, STYLES["visible"])[0] * 100))
                lw = min([0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158,
                          200, 211], key=lambda v: abs(v - lw))
                ly = dx.layers.add(name, lineweight=lw)
                lt = LINETYPE_OF.get(style)
                if lt and lt in dx.linetypes:
                    ly.dxf.linetype = lt
                if color:
                    try:
                        ly.rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
                    except Exception:  # noqa: BLE001
                        pass
            layers_made.add(name)
        return name

    sheets = drawing.sheets if all_sheets else [sheet or drawing.sheet()]
    xoff = 0.0
    for sh in sheets:
        for nm, info in sh.layers.items():
            layer(nm, "visible", info.get("color"))
        prims = flatten(doc, drawing, sh)
        for p in prims:
            _write_prim(msp, p, layer, opts, xoff, native_dims, dx)
        W = sh.dims()[0]
        xoff += (W or 0) + 50
    dx.header["$LTSCALE"] = 1.0
    dx.header["$INSUNITS"] = 4 if opts.units == "mm" else 1
    dx.saveas(str(path))
    return str(path)


def _write_prim(msp, p, layer, opts, xoff, native_dims, dx):
    t = p["t"]
    style = p.get("style", "visible")
    attrs = {"layer": layer(p.get("layer") or LAYER_OF.get(style, "0"), style)}
    lt = LINETYPE_OF.get(style)
    if lt and lt in dx.linetypes:
        attrs["linetype"] = lt
    if p.get("color"):
        attrs["true_color"] = _aci(p["color"])

    def X(q):
        return (q[0] + xoff, q[1])

    if t == "line":
        msp.add_line(X(p["a"]), X(p["b"]), dxfattribs=attrs)
    elif t == "poly":
        if p.get("fill") and len(p["pts"]) == 3:
            msp.add_solid([X(q) for q in p["pts"]], dxfattribs=attrs)
        else:
            msp.add_lwpolyline([X(q) for q in p["pts"]], close=bool(p.get("closed")), dxfattribs=attrs)
    elif t == "circle":
        msp.add_circle(X(p["c"]), p["r"], dxfattribs=attrs)
    elif t == "arc":
        msp.add_arc(X(p["c"]), p["r"], p["a0"], p["a1"], dxfattribs=attrs)
    elif t == "text":
        txt = str(p.get("text", ""))
        al = {"left": 1, "center": 2, "right": 3}.get(p.get("align", "left"), 1)
        va = {"top": 0, "middle": 1, "bottom": 2}.get(p.get("valign", "bottom"), 2)
        mt = msp.add_mtext(txt.replace("\n", "\\P"), dxfattribs={**attrs, "char_height": p.get("h", 3.5),
                                                                  "rotation": p.get("angle", 0.0)})
        mt.dxf.insert = X(p["p"])
        mt.dxf.attachment_point = va * 3 + al
    elif t == "hatch":
        h = msp.add_hatch(dxfattribs=attrs)
        if p.get("solid"):
            h.set_solid_fill()
        else:
            h.set_pattern_fill(p.get("pattern", "ANSI31"), scale=p.get("spacing", opts.hatch_spacing) / 3.175)
        for i, lp in enumerate(p["loops"]):
            if len(lp) > 2:
                h.paths.add_polyline_path([X(q) for q in lp], is_closed=True, flags=1 if i == 0 else 0)
    elif t == "dim" and native_dims:
        try:
            _native_dim(msp, p, attrs, opts, X)
        except Exception:  # noqa: BLE001
            for q in explode([p], opts):
                _write_prim(msp, q, lambda n, s="visible": attrs["layer"], opts, xoff, False, dx)
    elif t in ("dim", "balloon"):
        for q in explode([p], opts):
            _write_prim(msp, q, lambda n, s="visible", a=attrs: a["layer"], opts, xoff, False, dx)


def _native_dim(msp, p, attrs, opts, X):
    txt = dim_value(p, opts)
    kind = p.get("kind", "aligned")
    da = {k: v for k, v in attrs.items() if k != "linetype"}
    k = p.get("_unit", 1.0)
    over = {"dimlfac": k} if abs(k - 1) > 1e-9 else {}
    custom = p.get("text") or None
    tx = custom and txt or "<>"
    if kind in ("h", "v", "aligned"):
        if kind == "aligned":
            dist = math.hypot(p["p2"][0] - p["p1"][0], p["p2"][1] - p["p1"][1])
            ux, uy = (p["p2"][0] - p["p1"][0]) / dist, (p["p2"][1] - p["p1"][1]) / dist
            off = (p["at"][0] - p["p1"][0]) * -uy + (p["at"][1] - p["p1"][1]) * ux
            dim = msp.add_aligned_dim(p1=X(p["p1"]), p2=X(p["p2"]), distance=off, text=tx, dimstyle="EZDXF",
                                      override=over, dxfattribs=da)
        else:
            dim = msp.add_linear_dim(base=X(p["at"]), p1=X(p["p1"]), p2=X(p["p2"]), angle=0 if kind == "h" else 90,
                                     text=tx, dimstyle="EZDXF", override=over, dxfattribs=da)
    elif kind == "dia":
        c = p["c"]
        ang = math.degrees(math.atan2(p["at"][1] - c[1], p["at"][0] - c[0]))
        dim = msp.add_diameter_dim(center=X(c), radius=p["r"], angle=ang, text=custom and txt or "<>",
                                   dimstyle="EZDXF", override=over, dxfattribs=da)
    elif kind == "rad":
        c = p["c"]
        ang = math.degrees(math.atan2(p["at"][1] - c[1], p["at"][0] - c[0]))
        dim = msp.add_radius_dim(center=X(c), radius=p["r"], angle=ang, text=tx, dimstyle="EZDXF", override=over,
                                 dxfattribs=da)
    elif kind == "angle":
        c = p["c"]
        dim = msp.add_angular_dim_3p(base=X(p["at"]), center=X(c), p1=X(p["p1"]), p2=X(p["p2"]), dimstyle="EZDXF",
                                     dxfattribs=da)
    else:
        raise ValueError(kind)
    dim.render()


def write_dwg(doc, drawing, path, sheet=None, version="R2018"):
    if not dwg_available():
        raise DwgUnavailable(DWG_HELP)
    import tempfile

    from ezdxf.addons import odafc
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / (Path(path).stem + ".dxf")
        write_dxf(doc, drawing, tmp, sheet, version)
        odafc.convert(str(tmp), str(path), version=version, replace=True)
    return str(path)


# ------------------------------------------------------------------- read ---

def read_2d(path) -> Drawing:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".dxf":
        import ezdxf
        from ezdxf import recover
        try:
            dx = ezdxf.readfile(str(path))
        except ezdxf.DXFStructureError:
            dx, _ = recover.readfile(str(path))
        return from_ezdxf(dx, path)
    if ext == ".dwg":
        if not dwg_available():
            raise DwgUnavailable(DWG_HELP)
        from ezdxf.addons import odafc
        return from_ezdxf(odafc.readfile(str(path)), path)
    if ext == ".svg":
        return read_svg(path)
    if ext in (".plt", ".hpgl"):
        return read_hpgl(path)
    if ext == ".dwf" or ext == ".dwfx":
        raise ValueError("DWF/DWFx is a closed Autodesk viewing format and can't be read. Ask for DXF, DWG or PDF.")
    raise ValueError(f"Unsupported 2D format: {ext}")


def _hex(e, dx):
    try:
        if e.dxf.hasattr("true_color"):
            r, g, b = e.rgb
            return f"#{r:02x}{g:02x}{b:02x}"
        aci = e.dxf.color
        if aci in (0, 256):
            ly = dx.layers.get(e.dxf.layer)
            aci = ly.color if ly else 7
            if ly is not None and ly.rgb:
                r, g, b = ly.rgb
                return f"#{r:02x}{g:02x}{b:02x}"
        if aci == 7:
            return None
        from ezdxf.colors import aci2rgb
        r, g, b = aci2rgb(abs(aci))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:  # noqa: BLE001
        return None


def _style_of(e, dx):
    try:
        lt = e.dxf.linetype
        if lt.upper() in ("BYLAYER", ""):
            ly = dx.layers.get(e.dxf.layer)
            lt = ly.dxf.linetype if ly else "CONTINUOUS"
        lt = lt.upper()
        if "HIDDEN" in lt or "DASH" in lt:
            return "hidden"
        if "CENTER" in lt or "DASHDOT" in lt:
            return "center"
        if "PHANTOM" in lt:
            return "section"
    except Exception:  # noqa: BLE001
        pass
    return "visible"


def _entity_prims(e, dx, depth=0):
    """Daedalus primitives for one DXF entity (blocks and dimensions exploded)."""
    t = e.dxftype()
    out = []
    lay = e.dxf.layer if e.dxf.hasattr("layer") else "0"
    base = {"layer": lay, "style": _style_of(e, dx)}
    col = _hex(e, dx)
    if col and col.lower() not in ("#ffffff", "#000000"):
        base["color"] = col
    if t == "LINE":
        out.append({"t": "line", "a": [e.dxf.start.x, e.dxf.start.y], "b": [e.dxf.end.x, e.dxf.end.y], **base})
    elif t == "CIRCLE":
        out.append({"t": "circle", "c": [e.dxf.center.x, e.dxf.center.y], "r": e.dxf.radius, **base})
    elif t == "ARC":
        out.append({"t": "arc", "c": [e.dxf.center.x, e.dxf.center.y], "r": e.dxf.radius,
                    "a0": e.dxf.start_angle, "a1": e.dxf.end_angle, **base})
    elif t in ("LWPOLYLINE", "POLYLINE"):
        try:
            closed = e.closed if t == "LWPOLYLINE" else e.is_closed
            pts = []
            has_bulge = False
            if t == "LWPOLYLINE":
                has_bulge = any(abs(b) > 1e-12 for *_x, b in e.get_points("xyb"))
            if has_bulge or t == "POLYLINE":
                for v in e.virtual_entities():
                    out += _entity_prims(v, dx, depth + 1)
                for q in out:
                    q.update(base)
                return out
            pts = [[x, y] for x, y in e.get_points("xy")]
            out.append({"t": "poly", "pts": pts, "closed": bool(closed), **base})
        except Exception:  # noqa: BLE001
            pass
    elif t in ("SPLINE", "ELLIPSE"):
        try:
            pts = [[v.x, v.y] for v in e.flattening(0.02)]
            out.append({"t": "poly", "pts": pts, "closed": False, **base})
        except Exception:  # noqa: BLE001
            pass
    elif t in ("TEXT", "ATTRIB"):
        p = e.dxf.insert
        try:
            if e.dxf.halign or e.dxf.valign:
                p = e.dxf.align_point
        except Exception:  # noqa: BLE001
            pass
        al = {0: "left", 1: "center", 2: "right", 4: "center"}.get(e.dxf.get("halign", 0), "left")
        out.append({"t": "text", "p": [p.x, p.y], "text": e.dxf.text, "h": e.dxf.height or 2.5,
                    "angle": e.dxf.get("rotation", 0.0), "align": al, **base, "style": "text"})
    elif t == "MTEXT":
        p = e.dxf.insert
        ap = e.dxf.get("attachment_point", 1)
        out.append({"t": "text", "p": [p.x, p.y], "text": e.plain_text(), "h": e.dxf.get("char_height", 2.5),
                    "angle": e.get_rotation() if hasattr(e, "get_rotation") else e.dxf.get("rotation", 0.0),
                    "align": ["left", "center", "right"][(ap - 1) % 3],
                    "valign": ["top", "middle", "bottom"][(ap - 1) // 3], **base, "style": "text"})
    elif t == "DIMENSION":
        d = _dim_from_dxf(e)
        if d is not None:
            d.update({"layer": lay})
            out.append(d)
        else:
            for v in e.virtual_entities():
                out += _entity_prims(v, dx, depth + 1)
    elif t == "HATCH":
        loops = []
        try:
            for bp in e.paths:
                if hasattr(bp, "vertices"):
                    loops.append([[v[0], v[1]] for v in bp.vertices])
                else:
                    from ezdxf.path import from_hatch_edge_path
                    pth = from_hatch_edge_path(bp)
                    loops.append([[v.x, v.y] for v in pth.flattening(0.05)])
        except Exception:  # noqa: BLE001
            pass
        if loops:
            out.append({"t": "hatch", "loops": loops, "pattern": e.dxf.pattern_name,
                        "solid": bool(e.dxf.solid_fill), **base, "style": "hatch"})
    elif t in ("SOLID", "TRACE", "3DFACE"):
        try:
            v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx3, e.dxf.vtx2]
            out.append({"t": "poly", "pts": [[q.x, q.y] for q in v], "closed": True, "fill": True, **base})
        except Exception:  # noqa: BLE001
            pass
    elif t == "POINT":
        out.append({"t": "circle", "c": [e.dxf.location.x, e.dxf.location.y], "r": 0.3, **base})
    elif t in ("INSERT", "LEADER", "MLEADER", "MULTILEADER") and depth < 8:
        try:
            for v in e.virtual_entities():
                for q in _entity_prims(v, dx, depth + 1):
                    if q.get("layer") == "0":
                        q["layer"] = lay
                    out.append(q)
            if t == "INSERT":
                for a in e.attribs:
                    out += _entity_prims(a, dx, depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return out


def _dim_from_dxf(e):
    try:
        dt = e.dimtype
        txt = e.dxf.get("text", "")
        txt = "" if txt in ("", "<>") else txt
        if dt in (0, 1):                     # linear / aligned
            p1, p2, at = e.dxf.defpoint2, e.dxf.defpoint3, e.dxf.defpoint
            ang = e.dxf.get("angle", 0.0) if dt == 0 else None
            kind = "aligned"
            if ang is not None and abs(ang % 180) < 1e-6:
                kind = "h"
            elif ang is not None and abs(ang % 180 - 90) < 1e-6:
                kind = "v"
            elif ang is not None:
                a12 = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
                if abs((ang - a12 + 90) % 180 - 90) > 1e-4:
                    return None
            at = e.dxf.get("text_midpoint", at)
            return {"t": "dim", "kind": kind, "p1": [p1.x, p1.y], "p2": [p2.x, p2.y], "at": [at.x, at.y],
                    "text": txt}
        if dt in (3, 4):                     # diameter / radius
            if dt == 3:
                a, b = e.dxf.defpoint, e.dxf.defpoint4
                c = [(a.x + b.x) / 2, (a.y + b.y) / 2]
                r = math.hypot(a.x - b.x, a.y - b.y) / 2
                at = e.dxf.get("text_midpoint", b)
            else:
                cc, b = e.dxf.defpoint, e.dxf.defpoint4
                c, r = [cc.x, cc.y], math.hypot(cc.x - b.x, cc.y - b.y)
                at = e.dxf.get("text_midpoint", b)
            return {"t": "dim", "kind": "dia" if dt == 3 else "rad", "c": c, "r": r, "p1": [b.x, b.y],
                    "p2": [b.x, b.y], "at": [at.x, at.y], "text": txt}
    except Exception:  # noqa: BLE001
        return None
    return None


def from_ezdxf(dx, path=None) -> Drawing:
    msp = dx.modelspace()
    items = []
    for e in msp:
        try:
            items += _entity_prims(e, dx)
        except Exception:  # noqa: BLE001
            continue
    layers = {}
    for ly in dx.layers:
        col = "#000000"
        try:
            if ly.rgb:
                col = "#%02x%02x%02x" % tuple(ly.rgb)
            elif ly.color not in (7, -7):
                from ezdxf.colors import aci2rgb
                col = "#%02x%02x%02x" % tuple(aci2rgb(abs(ly.color)))
        except Exception:  # noqa: BLE001
            pass
        layers[ly.dxf.name] = {"color": col, "visible": ly.is_on() and not ly.is_frozen()}
    for it in items:
        layers.setdefault(it.get("layer", "0"), {"color": "#000000", "visible": True})
    sh = Sheet(name="Model", size="Model space", scale=[1, 1], items=items, layers=layers or {"0": {}},
               border=False)
    d = Drawing(name=Path(path).stem if path else "Drawing", sheets=[sh], linked=False, source=str(path or ""))
    try:
        if dx.header.get("$INSUNITS", 4) == 1:
            d.options.units = "in"
    except Exception:  # noqa: BLE001
        pass
    return d


def read_svg(path) -> Drawing:
    from svgelements import SVG, Circle, Ellipse, Path as SPath, Polygon, Polyline, Rect, SimpleLine, Text
    svg = SVG.parse(str(path), ppi=25.4)      # 1 user unit = 1 mm at 25.4 ppi
    items = []
    H = float(svg.height or 0)
    for el in svg.elements():
        try:
            col = None
            if getattr(el, "stroke", None) is not None and el.stroke.value is not None:
                col = el.stroke.hex[:7]
            base = {"layer": "0", "style": "visible"}
            if col and col.lower() not in ("#000000",):
                base["color"] = col
            if isinstance(el, Text):
                items.append({"t": "text", "p": [el.x, H - el.y], "text": el.text, "h": (el.font_size or 3.5) * 0.7,
                              **base, "style": "text"})
                continue
            if isinstance(el, Circle) and not el.transform.is_identity() is False:
                pass
            if isinstance(el, (SPath, Rect, Circle, Ellipse, Polygon, Polyline, SimpleLine)):
                p = SPath(el) if not isinstance(el, SPath) else el
                if len(p) == 0:
                    continue
                for sub in p.as_subpaths():
                    sp = SPath(sub)
                    L = sp.length(error=1e-3)
                    if not L:
                        continue
                    n = max(2, min(400, int(L / 0.5)))
                    pts = [sp.point(i / n) for i in range(n + 1)]
                    pts = [[q.x, H - q.y] for q in pts]
                    closed = bool(sp.segments()) and sp.segments()[-1].__class__.__name__ == "Close"
                    if len(pts) == 2 or (len(sp.segments()) == 2 and sp.segments()[1].__class__.__name__ == "Line"):
                        items.append({"t": "line", "a": pts[0], "b": pts[-1], **base})
                    else:
                        items.append({"t": "poly", "pts": _simplify(pts), "closed": closed, **base})
        except Exception:  # noqa: BLE001
            continue
    sh = Sheet(name="Model", size="Model space", scale=[1, 1], items=items, border=False)
    return Drawing(name=Path(path).stem, sheets=[sh], linked=False, source=str(path))


def _simplify(pts, tol=0.01):
    """Drop collinear points (Ramer–Douglas–Peucker)."""
    if len(pts) < 3:
        return pts
    import numpy as np
    P = np.asarray(pts, float)

    def rdp(i, j):
        a, b = P[i], P[j]
        ab = b - a
        L = np.hypot(*ab)
        if j <= i + 1:
            return [i]
        seg = P[i + 1:j]
        if L < 1e-12:
            dist = np.hypot(*(seg - a).T)
        else:
            dist = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            return rdp(i, i + 1 + k) + rdp(i + 1 + k, j)
        return [i]

    idx = rdp(0, len(P) - 1) + [len(P) - 1]
    return P[idx].tolist()


def read_hpgl(path) -> Drawing:
    """HPGL/PLT plotter files: PU/PD/PA/PR/CI, 40 plotter units per mm."""
    txt = Path(path).read_text(errors="ignore").replace("\n", "").replace("\r", "")
    items, pos, down, rel = [], [0.0, 0.0], False, False
    run = []

    def flush():
        nonlocal run
        if len(run) > 1:
            items.append({"t": "poly", "pts": run, "closed": False, "layer": "0", "style": "visible"})
        run = []

    for cmd in txt.split(";"):
        cmd = cmd.strip()
        if len(cmd) < 2:
            continue
        op, args = cmd[:2].upper(), cmd[2:]
        nums = [float(v) / 40.0 for v in args.replace(" ", ",").split(",") if v.strip()]
        if op in ("PU", "PD", "PA", "PR"):
            if op == "PU":
                flush()
                down = False
            elif op == "PD":
                down = True
            elif op == "PA":
                rel = False
            elif op == "PR":
                rel = True
            for i in range(0, len(nums) - 1, 2):
                q = [pos[0] + nums[i], pos[1] + nums[i + 1]] if rel else [nums[i], nums[i + 1]]
                if down:
                    if not run:
                        run = [list(pos)]
                    run.append(q)
                pos = q
        elif op == "CI" and nums:
            items.append({"t": "circle", "c": list(pos), "r": nums[0], "layer": "0", "style": "visible"})
    flush()
    sh = Sheet(name="Model", size="Model space", scale=[1, 1], items=items, border=False)
    return Drawing(name=Path(path).stem, sheets=[sh], linked=False, source=str(path))


def save_2d(doc, drawing: Drawing, path, sheet=None, **kw):
    """Save/export by extension: .dxf .dwg .pdf .svg .png."""
    ext = Path(path).suffix.lower()
    from . import render
    if ext == ".dxf":
        return write_dxf(doc, drawing, path, sheet, kw.get("version", "R2010"), kw.get("native_dims", True))
    if ext == ".dwg":
        return write_dwg(doc, drawing, path, sheet, kw.get("version", "R2018"))
    if ext == ".pdf":
        return render.export_pdf(doc, drawing, path, [sheet] if sheet else None)
    if ext == ".svg":
        return render.export_svg(doc, drawing, path, sheet)
    if ext == ".png":
        return render.export_png(doc, drawing, path, sheet, kw.get("dpi", 200))
    if ext in (".dwf", ".dwfx"):
        raise ValueError("DWF is a closed Autodesk format with no open writer. Export PDF (for viewing) or "
                         "DWG/DXF (for editing) instead.")
    raise ValueError(f"Unsupported 2D export format: {ext}")
