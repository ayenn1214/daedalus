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


def _named_shapes(doc, view):
    names = view.get("objects") or [o.name for o in doc.objects if o.visible]
    poses = doc.display_poses() if view.get("exploded") else {o.name: o.pose for o in doc.objects}
    from ..core.assembly import location_of
    out = []
    for n in names:
        b = doc.result.bodies.get(n)
        if b is None:
            continue
        p = poses.get(n, [0] * 6)
        out.append((n, b.moved(location_of(p)) if any(p) else b))
    return out


def _is_std(doc, name):
    if not doc.has_obj(name):
        return False
    o = doc.obj(name)
    return bool(o.part_no) or (o.std or {}).get("kind") == "purchased"


def _halfspace(point, normal, size):
    """A big box on the side of the plane the normal points to."""
    from build123d import Box, Plane, Pos
    pl = Plane(origin=tuple(float(x) for x in point), z_dir=tuple(float(x) for x in normal))
    return pl.location * Pos(0, 0, size / 2) * Box(size, size, size)


def _planar_faces_on(shape, point, normal, tol=1e-3):
    out = []
    for f in shape.faces():
        try:
            if f.geom_type.name != "PLANE":
                continue
            c = f.center()
            nn = f.normal_at(c)
            P = np.array([c.X, c.Y, c.Z])
            if abs(abs(np.dot([nn.X, nn.Y, nn.Z], normal)) - 1) < 1e-4 and abs((P - point) @ normal) < tol:
                out.append(f)
        except Exception:  # noqa: BLE001
            pass
    return out


def _section_cut(doc, named, sec, size, opts):
    """(kept shapes, [(object, cut face)]) for a section: full, half, offset, aligned, removed or revolved.
    Standard parts (bolts, nuts, pins, keys, bearings) stay whole unless the options say to cut them."""
    typ = sec.get("type", "full")
    n = np.asarray(sec["normal"], float)
    n = n / np.linalg.norm(n)
    P0 = np.asarray(sec["point"], float)
    kept, faces = [], []
    planes = [(P0, n)]
    region = None
    if typ in ("offset",) and sec.get("planes"):
        planes = [(np.asarray(a, float), np.asarray(b, float) / np.linalg.norm(b)) for a, b in sec["planes"]]
        region = _prism(sec["polygon3d"], np.asarray(sec["depth_dir"], float), size)
    for name, s in named:
        whole_std = _is_std(doc, name) and not opts.section_fasteners
        if whole_std:
            if typ not in ("removed", "revolved"):
                kept.append(s)
            continue
        if typ == "aligned" and sec.get("rotate"):
            s = _aligned_shape(s, sec, size)
        try:
            if region is not None:
                c = s & region
            elif typ == "half":
                L = np.asarray(sec["along"], float)
                quarter = _halfspace(P0, n, size) & _halfspace(np.asarray(sec["split"], float), L, size)
                c = s - quarter
            else:
                c = s - _halfspace(P0, n, size)           # remove the side towards the viewer
        except Exception:  # noqa: BLE001
            continue
        if c is None or c.volume <= 1e-9:
            continue
        kept.append(c)
        for (pp, nn) in planes:
            for f in _planar_faces_on(c, pp, nn):
                if typ == "half":
                    fc = f.center()
                    if (np.array([fc.X, fc.Y, fc.Z]) - np.asarray(sec["split"], float)) @ np.asarray(sec["along"],
                                                                                                 float) < 0:
                        continue
                if any(f.is_same(g) for _o, g in faces):
                    continue
                faces.append((name, f))
    if not kept:
        return None
    return kept, faces


def _prism(poly3d, direction, size):
    from build123d import Face, Vector, Wire, extrude
    w = Wire.make_polygon([Vector(*q) for q in poly3d], close=True)
    f = Face(w)
    return extrude(f, amount=size, dir=Vector(*direction), both=True)


def _aligned_shape(s, sec, size):
    """Aligned section: the material on the second arm's side turned into the first arm's plane."""
    from build123d import Axis, Vector
    V = np.asarray(sec["vertex"], float)
    m = np.asarray(sec["bisector_normal"], float)
    try:
        b_side = s & _halfspace(V, m, size)
        a_side = s - _halfspace(V, m, size)
    except Exception:  # noqa: BLE001
        return s
    parts = [x for x in (a_side,) if x is not None and x.volume > 1e-9]
    if b_side is not None and b_side.volume > 1e-9:
        parts.append(b_side.rotate(Axis(Vector(*V), Vector(*sec["axis"])), float(sec["rotate"])))
    return _compound(parts) if parts else s


def _broken_out(doc, named, view, center, d, u, r, size, opts):
    """Broken-out section: material inside the region, in front of the depth plane, is broken away; the
    exposed faces are hatched and a freehand break line drawn round the region."""
    bo = view["broken_out"]
    reg = bo["region"]
    pts3 = [list(center + r * q[0] + u * q[1]) for q in reg]
    prism = _prism(pts3, d, size)
    depth_pt = center + d * float(bo.get("depth", 0.0))
    remove = prism & _halfspace(depth_pt, d, size)
    kept, hatch = [], []
    for k, (name, s) in enumerate(named):
        try:
            c = s if (_is_std(doc, name) and not opts.section_fasteners) else s - remove
        except Exception:  # noqa: BLE001
            c = s
        kept.append(c)
        for f in _planar_faces_on(c, depth_pt, d):
            loops = []
            for w in [f.outer_wire(), *f.inner_wires()]:
                pts = []
                for e in w.edges():
                    m = 2 if e.geom_type.name == "LINE" else 24
                    pts += [_project_point([q.X, q.Y, q.Z], center, d, u, r)
                            for q in (e.position_at(i / m) for i in range(m))]
                loops.append(pts)
            from .practice import hatch_for
            mat = doc.material_of(name)
            hatch.append({"t": "hatch", "loops": loops, "pattern": hatch_for(mat) if opts.material_hatch else "ANSI31",
                          "style": "hatch", "spacing": opts.hatch_spacing, "angle": 45.0 if k % 2 == 0 else 135.0})
    return _compound(kept), hatch, [_freehand(reg)]


def _freehand(reg, amp=0.9):
    """A wavy break line round a region (closed)."""
    out = []
    pts = list(reg) + [reg[0]]
    k = 0
    for a, b in zip(pts, pts[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(2, int(L / 3))
        nx, ny = (-(b[1] - a[1]) / (L or 1), (b[0] - a[0]) / (L or 1))
        for i in range(n):
            t = i / n
            w = amp * math.sin(k * 1.7)
            k += 1
            out.append([a[0] + (b[0] - a[0]) * t + nx * w, a[1] + (b[1] - a[1]) * t + ny * w])
    out.append(out[0])
    return {"t": "poly", "pts": out, "closed": True, "style": "break"}


def _explode_lines(doc, view, center, d, u, r):
    """Explode lines: from where each part sits in the assembly to where the exploded view shows it."""
    off = getattr(doc, "explode", {}) or {}
    out = []
    names = view.get("objects") or [o.name for o in doc.objects if o.visible]
    from ..core.assembly import pose_matrix
    for nme in names:
        if nme not in off or doc.result.bodies.get(nme) is None:
            continue
        o = doc.obj(nme)
        c = doc.result.bodies[nme].center()
        P = (pose_matrix(o.pose) @ np.array([c.X, c.Y, c.Z, 1.0]))[:3]
        Q = P + np.asarray(off[nme][:3], float)
        a, b = _project_point(P, center, d, u, r), _project_point(Q, center, d, u, r)
        if math.hypot(b[0] - a[0], b[1] - a[1]) > 1.0:
            out.append((a, b))
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
    named = _named_shapes(doc, view)
    if not named:
        view["_prims"], view["_bbox"], view["_circles"] = [], (0, 0, 0, 0), []
        return
    shapes = [b for _n, b in named]
    whole = _compound(shapes)
    bb = whole.bounding_box()
    center = np.array([(bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2, (bb.min.Z + bb.max.Z) / 2])
    if view.get("center") is None:
        view["center"] = [float(v) for v in center]
    center = np.asarray(view["center"], float)
    dname = view.get("dir", "front")
    direction, up = (view["vector"], view.get("up", (0, 0, 1))) if dname == "custom" else VIEW_DIRS[dname]
    d, u, r = _basis(direction, up)
    hatch, outline = [], []
    size = bb.diagonal * 4 + 10
    if kind == "section":
        sec = view["section"]
        n = np.asarray(sec["normal"], float)
        n = n / np.linalg.norm(n)
        d, u, r = _basis(n, up if abs(np.dot(np.asarray(up, float), n)) < 0.99 else (0, 1, 0))
        res = _section_cut(doc, named, sec, size, opts)
        if res is None:
            view["_prims"], view["_bbox"], view["_circles"] = [], (0, 0, 0, 0), []
            return
        kept, faces = res
        for k, (oname, f) in enumerate(faces):
            loops = []
            for w in [f.outer_wire(), *f.inner_wires()]:
                pts = []
                for e in w.edges():
                    m = 2 if e.geom_type.name == "LINE" else 24
                    pts += [_project_point([q.X, q.Y, q.Z], center, d, u, r)
                            for q in (e.position_at(i / m) for i in range(m))]
                loops.append(pts)
            if not loops or len(loops[0]) < 3:
                continue
            idx = [nm for nm, _b in named].index(oname) if oname in [nm for nm, _b in named] else k
            mat = doc.material_of(oname) if oname and doc.has_obj(oname) else None
            from .practice import hatch_for
            pat = hatch_for(mat) if (opts.material_hatch and mat is not None) else "ANSI31"
            hatch.append({"t": "hatch", "loops": loops, "pattern": pat, "style": "hatch",
                          "spacing": opts.hatch_spacing * (1.0 if idx % 3 != 2 else 0.7),
                          "angle": 45.0 if idx % 2 == 0 else 135.0, "object": oname})
            if sec.get("type") in ("removed", "revolved"):
                for lp in loops:
                    outline.append({"t": "poly", "pts": lp, "closed": True, "style": "visible"})
        if sec.get("type") in ("removed", "revolved"):
            prims = hatch + outline
            view["_prims"], view["_bbox"], view["_circles"] = prims, prim_bbox(prims), []
            view["_basis"] = (list(d), list(u), list(r))
            return
        whole = _compound(kept)
    elif view.get("broken_out"):
        whole, hatch, brk = _broken_out(doc, named, view, center, d, u, r, size, opts)
        outline += brk
    if dname == "oblique":
        from .practice import oblique
        whole = oblique(whole)
        bb2 = whole.bounding_box()
        center = np.array([(bb2.min.X + bb2.max.X) / 2, (bb2.min.Y + bb2.max.Y) / 2, (bb2.min.Z + bb2.max.Z) / 2])
    viewer = center + d * (bb.diagonal * 10 + 1000)
    vis, hid = whole.project_to_viewport(tuple(viewer), tuple(u), tuple(center))
    vprims = _edge_prims(vis)
    hidden_on = view.get("hidden", False if kind == "section" else opts.hidden_lines)
    hprims = _drop_covered(_edge_prims(hid, hidden=True), vprims) if hidden_on and dname != "oblique" else []
    prims = hatch + vprims + hprims + outline
    circles = []
    for p in prims:
        if p["t"] == "circle" and p.get("style") == "visible":
            circles.append({"c": p["c"], "r": p["r"]})
    view["_prims"], view["_bbox"], view["_circles"] = prims, prim_bbox(prims), circles
    view["_basis"] = (list(d), list(u), list(r))
    if view.get("exploded"):
        view["_explode_lines"] = _explode_lines(doc, view, center, d, u, r)


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

def fmt(v, opts: Options, secondary=False):
    """A length in the drawing's units, written per its dimensioning standard; secondary = the other unit
    (dual dimensioning)."""
    from .practice import format_number
    inch = (opts.units == "in") != bool(secondary)
    if secondary:
        return format_number(v / 25.4, opts, 2, trailing=True) if inch else format_number(v, opts, 1)
    if inch:
        dec = opts.decimals if opts.dim_standard else max(opts.decimals, 2) + 1
        return format_number(v / 25.4, opts, dec)
    return format_number(v, opts, opts.decimals)


def auto_annotations(doc, view, opts: Options):
    """Overall dimensions, centre marks and hole callouts for an orthographic view (view-local)."""
    out = []
    if view.get("kind") not in ("base", "projected", "section", "flat", "auxiliary") or view.get("dir") in (
            "iso", "dimetric", "trimetric", "oblique") or (view.get("section") or {}).get("type") in ("revolved",):
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
        v = {"id": new_id(), "kind": "iso" if name in ("iso", "dimetric", "trimetric", "oblique") else "projected", "dir": name,
             "objects": objects or [], "parent": base["id"], "hidden": opts.hidden_lines if name not in
             ("iso", "dimetric", "trimetric", "oblique") else False, "center": base["center"]}
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
        if v["dir"] in ("iso", "dimetric", "trimetric", "oblique"):
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
