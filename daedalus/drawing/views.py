"""Drawing views from the 3D model: hidden-line projections, sections, details, flat patterns,
plus automatic dimensions, centre marks and hole/thread callouts. Qt-free."""
from __future__ import annotations

import math

import numpy as np

from .model import VIEW_DIRS, Drawing, Options, Sheet, new_id, scale_factor

PROJ = {"ISO": {"top": (0, -1), "bottom": (0, 1), "left": (1, 0), "right": (-1, 0), "back": (2, 0)},
        "ANSI": {"top": (0, 1), "bottom": (0, -1), "left": (-1, 0), "right": (1, 0), "back": (2, 0)}}


# --------------------------------------------------------------- helpers ---

def _shapes(doc, view):
    names = view.get("objects") or [o.name for o in doc.objects if o.visible]
    poses = doc.display_poses() if view.get("exploded") else {o.name: o.pose for o in doc.objects}
    from ..core.assembly import location_of
    out = []
    for n in names:
        b = doc.result.bodies.get(n)
        if b is None:
            continue
        p = poses.get(n, [0] * 6)
        out.append(b.moved(location_of(p)) if any(p) else b)
    return out


def _compound(shapes):
    from build123d import Compound
    return shapes[0] if len(shapes) == 1 else Compound(shapes)


def _basis(direction, up):
    d = np.asarray(direction, float)
    d = d / np.linalg.norm(d)
    u = np.asarray(up, float)
    u = u - d * (u @ d)
    if np.linalg.norm(u) < 1e-9:
        u = np.array([0, 1.0, 0]) if abs(d[1]) < 0.9 else np.array([1.0, 0, 0])
        u = u - d * (u @ d)
    u = u / np.linalg.norm(u)
    r = np.cross(u, d)            # screen right
    return d, u, r


def _edge_prims(edges, hidden=False):
    out = []
    style = "hidden" if hidden else "visible"
    for e in edges:
        gt = e.geom_type.name
        try:
            if gt == "LINE":
                a, b = e.position_at(0), e.position_at(1)
                out.append({"t": "line", "a": [a.X, a.Y], "b": [b.X, b.Y], "style": style})
            elif gt == "CIRCLE":
                c = e.arc_center
                r = e.radius
                if e.is_closed:
                    out.append({"t": "circle", "c": [c.X, c.Y], "r": r, "style": style})
                else:
                    p0, p1, pm = e.position_at(0), e.position_at(1), e.position_at(0.5)
                    a0 = math.degrees(math.atan2(p0.Y - c.Y, p0.X - c.X))
                    a1 = math.degrees(math.atan2(p1.Y - c.Y, p1.X - c.X))
                    am = math.degrees(math.atan2(pm.Y - c.Y, pm.X - c.X))
                    if not ((am - a0) % 360 <= (a1 - a0) % 360):
                        a0, a1 = a1, a0
                    out.append({"t": "arc", "c": [c.X, c.Y], "r": r, "a0": a0, "a1": a1, "style": style})
            else:
                n = 32
                pts = [e.position_at(i / n) for i in range(n + 1)]
                P = np.array([[p.X, p.Y] for p in pts])
                ext = P.max(0) - P.min(0)
                if ext.max() < 1e-7:
                    continue
                c0 = P.mean(0)
                _, sv, vt = np.linalg.svd(P - c0)
                if sv[1] < 1e-6 * max(1.0, sv[0]):           # edge-on curve → a straight line
                    t = (P - c0) @ vt[0]
                    a, b = c0 + vt[0] * t.min(), c0 + vt[0] * t.max()
                    out.append({"t": "line", "a": [float(a[0]), float(a[1])], "b": [float(b[0]), float(b[1])],
                                "style": style})
                else:
                    out.append({"t": "poly", "pts": P.tolist(), "closed": False, "style": style})
        except Exception:  # noqa: BLE001
            continue
    return out


def _on_segment(q, a, b, tol=1e-4):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L2 = ax * ax + ay * ay
    if L2 < 1e-12:
        return False
    t = ((q[0] - a[0]) * ax + (q[1] - a[1]) * ay) / L2
    if t < -1e-6 or t > 1 + 1e-6:
        return False
    px, py = a[0] + t * ax - q[0], a[1] + t * ay - q[1]
    return px * px + py * py < tol * tol


def _drop_covered(hidden, visible):
    """Hidden edges lying on top of visible ones (back silhouettes) are not drawn."""
    vl = [p for p in visible if p["t"] == "line"]
    vc = [(round(p["c"][0], 4), round(p["c"][1], 4), round(p["r"], 4)) for p in visible if p["t"] in ("circle", "arc")]
    out, seen = [], set()
    for h in hidden:
        if h["t"] == "line":
            k = tuple(sorted([(round(h["a"][0], 4), round(h["a"][1], 4)), (round(h["b"][0], 4), round(h["b"][1], 4))]))
            if k in seen:
                continue
            seen.add(k)
        if h["t"] == "line" and any(_on_segment(h["a"], v["a"], v["b"]) and _on_segment(h["b"], v["a"], v["b"])
                                    for v in vl):
            continue
        if h["t"] == "circle" and (round(h["c"][0], 4), round(h["c"][1], 4), round(h["r"], 4)) in vc:
            continue
        out.append(h)
    return out


def prim_bbox(prims):
    xs, ys = [], []
    for p in prims:
        t = p["t"]
        if t == "line":
            xs += [p["a"][0], p["b"][0]]
            ys += [p["a"][1], p["b"][1]]
        elif t == "poly":
            xs += [q[0] for q in p["pts"]]
            ys += [q[1] for q in p["pts"]]
        elif t in ("circle", "arc"):
            xs += [p["c"][0] - p["r"], p["c"][0] + p["r"]]
            ys += [p["c"][1] - p["r"], p["c"][1] + p["r"]]
        elif t == "hatch":
            for lp in p["loops"]:
                xs += [q[0] for q in lp]
                ys += [q[1] for q in lp]
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def _project_point(P, origin, d, u, r):
    v = np.asarray(P, float) - origin
    return [float(v @ r), float(v @ u)]


# ---------------------------------------------------------------- views ---

def compute(doc, view, opts: Options):
    """Fill view["_prims"] (view-local mm, true size) and view["_bbox"], view["_circles"]."""
    kind = view.get("kind", "base")
    if kind == "detail":
        return                                       # done after its parent
    if kind == "flat":
        fl = doc.result.flats.get(view.get("feature", ""))
        prims = []
        if fl is not None:
            from ..core.features2 import flat_pattern_face
            for face in flat_pattern_face(fl):
                for w in [face.outer_wire(), *face.inner_wires()]:
                    for e in w.edges():
                        pts = [e.position_at(i / 16) for i in range(17)] if e.geom_type.name != "LINE" else \
                            [e.position_at(0), e.position_at(1)]
                        prims.append({"t": "poly", "pts": [[q.X, q.Y] for q in pts], "closed": False,
                                      "style": "visible"})
            for b in fl["bends"]:
                prims.append({"t": "line", "a": list(b["line"][0]), "b": list(b["line"][1]), "style": "center"})
                (x0, y0), (x1, y1) = b["line"]
                prims.append({"t": "text", "p": [(x0 + x1) / 2, (y0 + y1) / 2 + 1.5],
                              "text": f"{b['direction'].upper()} {b['angle']:g}° R{b['radius']:g}", "h": 2.5,
                              "align": "center", "style": "text", "fixed_size": True})
        view["_prims"], view["_bbox"], view["_circles"] = prims, prim_bbox(prims), []
        return
    shapes = _shapes(doc, view)
    if not shapes:
        view["_prims"], view["_bbox"], view["_circles"] = [], (0, 0, 0, 0), []
        return
    whole = _compound(shapes)
    bb = whole.bounding_box()
    center = np.array([(bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2, (bb.min.Z + bb.max.Z) / 2])
    if view.get("center") is None:
        view["center"] = [float(v) for v in center]
    center = np.asarray(view["center"], float)
    dname = view.get("dir", "front")
    direction, up = (view["vector"], view.get("up", (0, 0, 1))) if dname == "custom" else VIEW_DIRS[dname]
    d, u, r = _basis(direction, up)
    hatch = []
    if kind == "section":
        sec = view["section"]
        P0, n = np.asarray(sec["point"], float), np.asarray(sec["normal"], float)
        n = n / np.linalg.norm(n)
        d, u, r = _basis(n, up if abs(np.dot(np.asarray(up, float), n)) < 0.99 else (0, 1, 0))
        from build123d import Box, Face, Location, Plane, Pos
        size = bb.diagonal * 4 + 10
        pl = Plane(origin=tuple(P0), z_dir=tuple(n))
        keep = pl.location * Pos(0, 0, -size / 2) * Box(size, size, size)      # the side away from the viewer
        cut = []
        for s in shapes:
            try:
                c = s & keep
                if c.volume > 1e-9:
                    cut.append(c)
            except Exception:  # noqa: BLE001
                pass
        if not cut:
            view["_prims"], view["_bbox"], view["_circles"] = [], (0, 0, 0, 0), []
            return
        whole = _compound(cut)
        disc = Face.make_rect(size, size, pl)
        for s in cut:
            try:
                f = s & disc
                for face in f.faces():
                    loops = []
                    for w in [face.outer_wire(), *face.inner_wires()]:
                        pts = []
                        for e in w.edges():
                            m = 2 if e.geom_type.name == "LINE" else 24
                            pts += [_project_point([q.X, q.Y, q.Z], center, d, u, r)
                                    for q in (e.position_at(i / m) for i in range(m))]
                        loops.append(pts)
                    hatch.append({"t": "hatch", "loops": loops, "pattern": "ANSI31", "style": "hatch",
                                  "spacing": opts.hatch_spacing})
            except Exception:  # noqa: BLE001
                pass
    viewer = center + d * (bb.diagonal * 10 + 1000)
    vis, hid = whole.project_to_viewport(tuple(viewer), tuple(u), tuple(center))
    vprims = _edge_prims(vis)
    hprims = _drop_covered(_edge_prims(hid, hidden=True), vprims) if view.get("hidden", opts.hidden_lines) else []
    prims = vprims + hprims
    # the projection's 2D frame: x right, y up, origin at the look-at point — normalise to (r, u)
    prims = hatch + prims
    circles = []
    for p in prims:
        if p["t"] == "circle" and p.get("style") == "visible":
            circles.append({"c": p["c"], "r": p["r"]})
    view["_prims"], view["_bbox"], view["_circles"] = prims, prim_bbox(prims), circles
    view["_basis"] = (list(d), list(u), list(r))


def _clip_circle(prims, c, R):
    """Pieces of the primitives inside a circle (for detail views)."""
    out = []
    cx, cy = c

    def inside(q):
        return (q[0] - cx) ** 2 + (q[1] - cy) ** 2 <= R * R

    for p in prims:
        t = p["t"]
        if t in ("line", "poly", "circle", "arc"):
            if t == "line":
                pts = [p["a"], p["b"]]
                n = 40
                pts = [[p["a"][0] + (p["b"][0] - p["a"][0]) * i / n, p["a"][1] + (p["b"][1] - p["a"][1]) * i / n]
                       for i in range(n + 1)]
            elif t == "poly":
                pts = p["pts"]
            else:
                a0, a1 = (0, 360) if t == "circle" else (p["a0"], p["a1"] if p["a1"] > p["a0"] else p["a1"] + 360)
                m = 64
                pts = [[p["c"][0] + p["r"] * math.cos(math.radians(a0 + (a1 - a0) * i / m)),
                        p["c"][1] + p["r"] * math.sin(math.radians(a0 + (a1 - a0) * i / m))] for i in range(m + 1)]
            run = []
            for q in pts:
                if inside(q):
                    run.append(q)
                else:
                    if len(run) > 1:
                        out.append({"t": "poly", "pts": run, "closed": False, "style": p.get("style", "visible")})
                    run = []
            if len(run) > 1:
                out.append({"t": "poly", "pts": run, "closed": False, "style": p.get("style", "visible")})
        elif t == "hatch":
            loops = [[q for q in lp if inside(q)] for lp in p["loops"]]
            loops = [lp for lp in loops if len(lp) > 2]
            if loops:
                out.append({**p, "loops": loops})
    out.append({"t": "circle", "c": [cx, cy], "r": R, "style": "thin"})
    return out


# -------------------------------------------------------- annotations -----

def fmt(v, opts: Options):
    if opts.units == "in":
        return f"{v / 25.4:.{max(opts.decimals, 2) + 1}f}"
    s = f"{v:.{opts.decimals}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def auto_annotations(doc, view, opts: Options):
    """Overall dimensions, centre marks and hole callouts for an orthographic view (view-local)."""
    out = []
    if view.get("kind") not in ("base", "projected", "section", "flat", "auxiliary") or view.get("dir") in (
            "iso", "dimetric", "trimetric"):
        return out
    x0, y0, x1, y1 = view.get("_bbox", (0, 0, 0, 0))
    if x1 - x0 < 1e-6 and y1 - y0 < 1e-6:
        return out
    sf = 1.0 / max(view.get("_scale", 1.0), 1e-9)     # sheet mm → view mm
    gap = 8 * sf
    if opts.auto_dims:
        bs = view.get("_break_shift") or {}
        hd = {"t": "dim", "kind": "h", "p1": [x0, y0], "p2": [x1, y0], "at": [(x0 + x1) / 2, y0 - gap], "auto": True}
        vd = {"t": "dim", "kind": "v", "p1": [x0, y0], "p2": [x0, y1], "at": [x0 - gap, (y0 + y1) / 2], "auto": True}
        if bs.get("x"):                       # broken view: the dimension still states the true length
            hd["text"] = fmt(x1 - x0 + bs["x"], opts)
        if bs.get("y"):
            vd["text"] = fmt(y1 - y0 + bs["y"], opts)
        out += [hd, vd]
    circles = view.get("_circles", [])
    if opts.center_marks:
        for c in circles:
            e = c["r"] + 2 * sf
            cx, cy = c["c"]
            out.append({"t": "line", "a": [cx - e, cy], "b": [cx + e, cy], "style": "center", "auto": True})
            out.append({"t": "line", "a": [cx, cy - e], "b": [cx, cy + e], "style": "center", "auto": True})
    if opts.hole_callouts and circles:
        groups = {}
        for c in circles:
            groups.setdefault(round(2 * c["r"], 3), []).append(c)
        threads = _threads_in_view(doc, view)

        def centres(cs):
            return {(round(q["c"][0], 2), round(q["c"][1], 2)) for q in cs}
        # concentric groups at the same holes (counterbores, countersinks seen from above) → one callout
        extra, merged = {}, set()
        for dia in sorted(groups):
            if dia in merged:
                continue
            for big in sorted(d for d in groups if d > dia and d not in merged):
                if centres(groups[big]) == centres(groups[dia]):
                    extra.setdefault(dia, []).append(big)
                    merged.add(big)
        k = 0
        for dia, cs in sorted(groups.items(), key=lambda kv: -kv[0]):
            if dia in merged:
                continue
            c = max(cs, key=lambda q: (q["c"][0], q["c"][1]))
            label = f"{len(cs)}× " if len(cs) > 1 else ""
            th = next((t for t in threads if abs(t["c"][0] - c["c"][0]) < 0.05 and abs(t["c"][1] - c["c"][1]) < 0.05),
                      None)
            if th:
                txt = f"{label}{th['size']}-{th['fit']}" + (f" ↧{fmt(th['depth'], opts)}" if th.get("depth") else "")
            else:
                txt = f"{label}Ø{fmt(dia, opts)}"
            for big in extra.get(dia, []):
                txt += f" ⌴Ø{fmt(big, opts)}"
            ang = math.radians(45 + 15 * k)
            k += 1
            p_edge = [c["c"][0] + c["r"] * math.cos(ang), c["c"][1] + c["r"] * math.sin(ang)]
            at = [p_edge[0] + 10 * sf * math.cos(ang), p_edge[1] + 10 * sf * math.sin(ang)]
            out.append({"t": "dim", "kind": "dia", "c": c["c"], "r": c["r"], "p1": p_edge, "p2": p_edge, "at": at,
                        "text": txt, "auto": True})
    return out


def _threads_in_view(doc, view):
    out = []
    basis = view.get("_basis")
    if not basis:
        return out
    d, u, r = (np.asarray(v) for v in basis)
    center = np.asarray(view["center"], float)
    from ..core.assembly import pose_matrix
    for th in doc.result.threads.values():
        o, ax = (np.asarray(v, float) for v in th["axis"])
        obj = th.get("object")
        if obj and doc.has_obj(obj):
            M = pose_matrix(doc.obj(obj).pose)
            o = (M @ np.append(o, 1.0))[:3]
            ax = M[:3, :3] @ ax
        if abs(abs(ax @ d) - 1) > 1e-3:
            continue
        out.append({"c": _project_point(o, center, d, u, r), **th})
    return out


# ------------------------------------------------------------- layout -----

def nice_scale(extent_w, extent_h, avail_w, avail_h):
    from .model import SCALES
    for a, b in SCALES:
        s = a / b
        if extent_w * s <= avail_w and extent_h * s <= avail_h:
            return [a, b]
    return [1, 200]


def standard_views(doc, drawing: Drawing, sheet: Sheet, which=("front", "top", "right", "iso"), objects=None):
    """Lay out a standard view set on a sheet with an automatic scale."""
    opts = drawing.options
    W, H = sheet.dims()
    base = {"id": new_id(), "kind": "base", "dir": which[0], "objects": objects or [], "label": "",
            "hidden": opts.hidden_lines}
    compute(doc, base, opts)
    x0, y0, x1, y1 = base["_bbox"]
    bw, bh = x1 - x0, y1 - y0
    # extents of the projected neighbours
    others = []
    for name in which[1:]:
        v = {"id": new_id(), "kind": "iso" if name in ("iso", "dimetric", "trimetric") else "projected", "dir": name,
             "objects": objects or [], "parent": base["id"], "hidden": opts.hidden_lines if name not in
             ("iso", "dimetric", "trimetric") else False, "center": base["center"]}
        compute(doc, v, opts)
        others.append(v)
    ext_w = bw + sum((v["_bbox"][2] - v["_bbox"][0]) for v in others if v["dir"] in ("right", "left", "iso")) + 1e-6
    ext_h = bh + sum((v["_bbox"][3] - v["_bbox"][1]) for v in others if v["dir"] in ("top", "bottom")) + 1e-6
    avail_w, avail_h = (W - 40) * 0.75, (H - 70) * 0.8
    sheet.scale = nice_scale(ext_w, ext_h, avail_w, avail_h) if W else [1, 1]
    s = scale_factor(sheet.scale)
    gap = 25.0
    cx, cy = (W - 20) * 0.38, (H + 30) * 0.42
    base.update(x=cx, y=cy, scale=s)
    placed = [base]
    rules = PROJ[opts.standard]
    for v in others:
        vw, vh = (v["_bbox"][2] - v["_bbox"][0]) * s, (v["_bbox"][3] - v["_bbox"][1]) * s
        if v["dir"] in ("iso", "dimetric", "trimetric"):
            v.update(x=W - 20 - vw / 2 - 15, y=H - 20 - vh / 2 - 15, scale=s)
        else:
            dx, dy = rules.get(v["dir"], (1, 0))
            v.update(x=cx + dx * (bw * s / 2 + gap + vw / 2), y=cy + dy * (bh * s / 2 + gap + vh / 2), scale=s)
        placed.append(v)
    sheet.views.extend(placed)
    return placed


def regenerate(doc, drawing: Drawing):
    """Recompute every view (after model changes) and the automatic annotations."""
    opts = drawing.options
    for sheet in drawing.sheets:
        byid = {v["id"]: v for v in sheet.views}
        for v in sheet.views:
            v["_scale"] = float(v.get("scale", scale_factor(sheet.scale)))
            if v.get("kind") != "detail":
                try:
                    compute(doc, v, opts)
                    if v.get("breaks"):
                        from .annot import apply_breaks
                        apply_breaks(v)
                except Exception as e:  # noqa: BLE001
                    v["_prims"], v["_bbox"], v["_circles"] = [], (0, 0, 0, 0), []
                    v["_error"] = str(e)
        for v in sheet.views:
            if v.get("kind") == "detail":
                par = byid.get(v.get("parent"))
                if par is None:
                    v["_prims"], v["_bbox"] = [], (0, 0, 0, 0)
                    continue
                dt = v["detail"]
                v["_prims"] = _clip_circle(par.get("_prims", []), dt["c"], dt["r"])
                v["_bbox"] = prim_bbox(v["_prims"])
                v["_circles"] = []
        for v in sheet.views:
            v["_auto"] = auto_annotations(doc, v, opts) if v.get("annotate", True) else []
    return drawing


def balloons(doc, sheet: Sheet, view):
    """Item balloons for an assembly view: one per BOM row, at a visible object's projected centre."""
    if not view.get("_basis"):
        return []
    rows = doc.bom()
    d, u, r = (np.asarray(x) for x in view["_basis"])
    center = np.asarray(view["center"], float)
    poses = doc.display_poses() if view.get("exploded") else {o.name: o.pose for o in doc.objects}
    from ..core.assembly import pose_matrix
    out = []
    used = []
    for row in rows:
        n = row["objects"][0]
        b = doc.result.bodies.get(n)
        if b is None:
            continue
        c = b.center()
        P = (pose_matrix(poses[n]) @ np.array([c.X, c.Y, c.Z, 1.0]))[:3]
        q = _project_point(P, center, d, u, r)
        out.append({"item": row["item"], "p": q})
        used.append(q)
    return out
