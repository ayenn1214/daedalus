"""3D snapping and inferencing (Qt/VTK-free).

Snap types, in priority order (SolidWorks names in brackets where they differ):
  endpoint (end points and sketch points) · center (centre points, arc/hole centres) · midpoint · quadrant ·
  intersection (edge × edge) · origin · face center · center of mass · bounding-box center ·
  perpendicular (foot from the previous point) · tangent (to an arc/circle from the previous point) ·
  on edge (nearest) · on face (point on the surface under the cursor, with its normal) · extension (beyond
  the end of a straight edge) · along X / Y / Z (from the previous point) · parallel (to a straight edge,
  from the previous point) · aligned (horizontal/vertical to points: on a world axis through a vertex) ·
  length and angle increments, grid (applied by the caller's placement).
Everything is found in screen space through a caller-supplied projection, so it works in any view.
"""
from __future__ import annotations

import math

import numpy as np

KINDS = ["endpoint", "center", "midpoint", "quadrant", "intersection", "origin", "face center", "center of mass",
         "bbox center", "perpendicular", "tangent", "on edge", "on face", "extension", "along axis", "parallel",
         "aligned"]
LABELS = {"endpoint": "End points and sketch points", "center": "Centre points (arcs, holes)",
          "midpoint": "Midpoints", "quadrant": "Quadrant points", "intersection": "Intersections",
          "origin": "Origin", "face center": "Face centres", "center of mass": "Centre of mass",
          "bbox center": "Bounding-box centre", "perpendicular": "Perpendicular", "tangent": "Tangent",
          "on edge": "Nearest point on edges", "on face": "On faces (surface under the cursor)",
          "extension": "Edge extensions", "along axis": "Along X / Y / Z inference lines",
          "parallel": "Parallel to edges", "aligned": "Horizontal / vertical to points"}
RANK = {"endpoint": 0, "center": 0, "origin": 0, "midpoint": 1, "quadrant": 1, "intersection": 1,
        "face center": 2, "center of mass": 2, "bbox center": 2, "perpendicular": 3, "tangent": 3,
        "on edge": 4, "extension": 5, "along axis": 6, "parallel": 6, "aligned": 7, "on face": 8}
DEFAULTS = {k: True for k in KINDS}
DEFAULTS.update({"center of mass": False, "bbox center": False, "extension": True})
NUMERIC = {"tolerance_px": 10.0, "length_step": 0.0, "angle_step": 15.0, "grid": 0.0}
SETTINGS = {"enabled": True, "smart_mates": True, "auto_relations": True, **DEFAULTS, **NUMERIC}

_cache: dict = {}
AXES = {"X": np.array([1.0, 0, 0]), "Y": np.array([0, 1.0, 0]), "Z": np.array([0, 0, 1.0])}


# --------------------------------------------------------------- candidates ---

def candidates(body, key=None):
    """Snap geometry of a body in its own coordinates (cached):
    {"pts": (N,3), "kinds": [...], "edges": [(M,3) polylines], "lines": [(a, b)], "circles": [(c, n, r)],
     "faces": [shape faces], "shape": body}."""
    k = key if key is not None else id(body)
    hit = _cache.get(k)
    if hit is not None and hit[0] is body:
        return hit[1]
    pts, kinds, polys, lines, circles = [], [], [], [], []

    def add(p, kind):
        pts.append((float(p.X), float(p.Y), float(p.Z)))
        kinds.append(kind)
    try:
        for v in body.vertices():
            add(v, "endpoint")
        for e in body.edges():
            gt = e.geom_type.name
            try:
                if gt == "LINE":
                    a, b = e.position_at(0), e.position_at(1)
                    add(e.position_at(0.5), "midpoint")
                    lines.append((np.array([a.X, a.Y, a.Z]), np.array([b.X, b.Y, b.Z])))
                    polys.append(np.array([[a.X, a.Y, a.Z], [b.X, b.Y, b.Z]]))
                    continue
                if gt == "CIRCLE":
                    c = e.arc_center
                    add(c, "center")
                    try:
                        nrm = e.normal()
                        n = np.array([nrm.X, nrm.Y, nrm.Z])
                    except Exception:  # noqa: BLE001
                        n = np.array([0, 0, 1.0])
                    circles.append((np.array([c.X, c.Y, c.Z]), n, float(e.radius), e.is_closed))
                    if e.is_closed:
                        for u in (0.0, 0.25, 0.5, 0.75):
                            add(e.position_at(u), "quadrant")
                    else:
                        add(e.position_at(0.5), "midpoint")
                n = 24
                polys.append(np.array([[q.X, q.Y, q.Z] for q in (e.position_at(i / n) for i in range(n + 1))]))
            except Exception:  # noqa: BLE001
                continue
        from build123d import CenterOf
        faces = list(body.faces())
        for f in faces:
            if f.geom_type.name == "PLANE":
                try:
                    add(f.center(CenterOf.MASS), "face center")
                except Exception:  # noqa: BLE001
                    pass
        try:
            add(body.center(CenterOf.MASS), "center of mass")
        except Exception:  # noqa: BLE001
            pass
        bb = body.bounding_box()
        add(bb.center(), "bbox center")
    except Exception:  # noqa: BLE001
        faces = []
    out = {"pts": np.array(pts, float).reshape(-1, 3), "kinds": kinds, "edges": polys, "lines": lines,
           "circles": circles, "faces": faces, "shape": body}
    if len(_cache) > 64:
        _cache.clear()
    _cache[k] = (body, out)
    return out


def sketch_candidates(points: dict, lines: list, exclude=()):
    """Snap geometry of a 3D sketch: points {id: xyz}, lines [(id, a_id, b_id)]."""
    pts, kinds, ids, polys, segs, refs = [], [], [], [], [], []
    for pid, p in points.items():
        if pid in exclude:
            continue
        pts.append(tuple(p))
        kinds.append("endpoint")
        ids.append({"point": pid})
    for lid, a, b in lines:
        A, B = np.array(points[a], float), np.array(points[b], float)
        pts.append(tuple((A + B) / 2))
        kinds.append("midpoint")
        ids.append({"line": lid})
        polys.append(np.array([A, B]))
        segs.append((A, B))
        refs.append(lid)
    return {"pts": np.array(pts, float).reshape(-1, 3), "kinds": kinds, "ids": ids, "edges": polys,
            "lines": segs, "line_ids": refs, "circles": [], "faces": [], "shape": None}


def _prep(cand):
    """Flattened arrays for vectorised snapping (cached on the candidate dict)."""
    pr = cand.get("_prep")
    if pr is not None:
        return pr
    L = np.array([[a, b] for a, b in cand["lines"]], float).reshape(-1, 2, 3)
    A, B, pi = [], [], []
    for k, poly in enumerate(cand["edges"]):
        poly = np.asarray(poly, float)
        if len(poly) < 2:
            continue
        A.append(poly[:-1])
        B.append(poly[1:])
        pi.append(np.full(len(poly) - 1, k))
    pr = {"L": L,
          "SA": np.concatenate(A) if A else np.zeros((0, 3)),
          "SB": np.concatenate(B) if B else np.zeros((0, 3)),
          "SP": np.concatenate(pi) if pi else np.zeros(0, int)}
    cand["_prep"] = pr
    return pr


def _seg_dist2d(Da, Db, cur):
    """Distance from cur to each 2D segment Da→Db, and the clamped parameter."""
    ab = Db - Da
    L2 = np.einsum("ij,ij->i", ab, ab)
    t = np.where(L2 < 1e-9, 0.0, np.einsum("ij,ij->i", cur - Da, ab) / np.where(L2 < 1e-9, 1.0, L2))
    t = np.clip(t, 0.0, 1.0)
    q = Da + ab * t[:, None]
    return np.hypot(q[:, 0] - cur[0], q[:, 1] - cur[1]), t, L2


def transform(pts, M):
    if M is None or len(pts) == 0:
        return np.asarray(pts, float)
    return (np.c_[pts, np.ones(len(pts))] @ np.asarray(M).T)[:, :3]


def _dir(M, d):
    return np.asarray(d, float) if M is None else np.asarray(M)[:3, :3] @ np.asarray(d, float)


# ---------------------------------------------------------------- helpers ---

def _closest_line_ray(P, d, ro, rd):
    """Parameter s on line P + s·d closest to the ray ro + t·rd."""
    w0 = P - ro
    a, b, c = d @ d, d @ rd, rd @ rd
    dd, e = d @ w0, rd @ w0
    den = a * c - b * b
    if abs(den) < 1e-12:
        return None
    return (b * e - c * dd) / den


def _seg_closest(p1, q1, p2, q2):
    """Closest points between segments p1q1 and p2q2."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = d1 @ d1, d2 @ d2, d2 @ r
    if a < 1e-18 or e < 1e-18:
        return None
    c = d1 @ r
    b = d1 @ d2
    den = a * e - b * b
    s = np.clip((b * f - c * e) / den, 0, 1) if den > 1e-18 else 0.0
    t = (b * s + f) / e
    if t < 0:
        t, s = 0.0, np.clip(-c / a, 0, 1)
    elif t > 1:
        t, s = 1.0, np.clip((b - c) / a, 0, 1)
    return p1 + d1 * s, p2 + d2 * t


def ray_hit(cand, M, ro, rd):
    """First intersection of a world ray with the body: (world point, world normal, face index) or None."""
    shape = cand.get("shape")
    if shape is None:
        return None
    try:
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
        Minv = np.linalg.inv(M) if M is not None else None
        o = transform(np.array([ro]), Minv)[0] if Minv is not None else np.asarray(ro, float)
        d = (Minv[:3, :3] @ rd) if Minv is not None else np.asarray(rd, float)
        d = d / np.linalg.norm(d)
        # cheap reject: ray vs. local bounding box (slab test)
        bb = cand.get("_bb")
        if bb is None:
            try:
                b = shape.bounding_box()
                bb = (np.array([b.min.X, b.min.Y, b.min.Z]), np.array([b.max.X, b.max.Y, b.max.Z]))
            except Exception:  # noqa: BLE001
                bb = False
            cand["_bb"] = bb
        if bb is not False:
            pad = 1e-6 + 1e-6 * float(np.linalg.norm(bb[1] - bb[0]))
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / np.where(np.abs(d) < 1e-15, 1e-15, d)
                t1, t2 = (bb[0] - pad - o) * inv, (bb[1] + pad - o) * inv
            if np.max(np.minimum(t1, t2)) > np.min(np.maximum(t1, t2)) or np.min(np.maximum(t1, t2)) < 0:
                return None
        # loaded once per body: per-face classifiers with their own bounding boxes
        si = cand.get("_isect")
        if si is None:
            from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
            si = IntCurvesFace_ShapeIntersector()
            si.Load(shape.wrapped, 1e-6)
            cand["_isect"] = si
        si.PerformNearest(gp_Lin(gp_Pnt(*o), gp_Dir(*d)), 1e-9, 1e100)
        if not si.IsDone() or si.NbPnt() < 1:
            return None
        best = (si.WParameter(1), si.Pnt(1), si.Face(1))
        p = np.array([best[1].X(), best[1].Y(), best[1].Z()])
        fi = None
        for i, f in enumerate(cand["faces"]):
            if f.wrapped.IsSame(best[2]):
                fi = i
                break
        n = None
        if fi is not None:
            from build123d import Vector
            nv = cand["faces"][fi].normal_at(Vector(*p))
            n = _dir(M, [nv.X, nv.Y, nv.Z])
        return transform(np.array([p]), M)[0], n, fi, p
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------- find ---

def find(sets, project, x, y, tol=10.0, opts=None, anchor=None, ray=None, origin=True):
    """Best snap near screen point (x, y).

    sets: [(tag, candidates, 4×4 local→world matrix or None)]; project: (N,3) world → (N,2) px;
    anchor: previous point (world) for perpendicular / tangent / along-axis / parallel inference;
    ray: (origin, direction) world pick ray through the cursor (on-face, extensions, inference lines).
    Returns {"p": world xyz, "kind", "tag", "local", "guides": [(a, b)], "normal", "face", "refs"} or None.
    """
    o = {**SETTINGS, **(opts or {})}
    if not o.get("enabled", True):
        return None
    tol = float(o.get("tolerance_px", tol) or tol)
    best = None
    cur = np.array([x, y], float)

    def offer(kind, pw, tag=None, local=None, guides=(), refs=None, normal=None, face=None, dist=None):
        nonlocal best
        if not o.get(kind, True):
            return
        if dist is None:
            D = project(np.array([pw]))[0]
            dist = float(np.hypot(*(D - cur)))
        if dist > tol:
            return
        score = (RANK[kind], dist)
        if best is None or score < best[0]:
            best = (score, {"p": [float(v) for v in pw], "kind": kind, "tag": tag,
                            "local": None if local is None else [float(v) for v in local],
                            "guides": [([float(v) for v in a], [float(v) for v in b]) for a, b in guides],
                            "refs": refs or {}, "normal": None if normal is None else [float(v) for v in normal],
                            "face": face})

    world_lines = []                     # (tag, a, b) for inference
    for tag, cand, M in sets:
        P = cand["pts"]
        if len(P):
            W = transform(P, M)
            Dp = project(W)
            d = np.hypot(Dp[:, 0] - x, Dp[:, 1] - y)
            ids = cand.get("ids")
            for i in np.nonzero(d <= tol)[0]:
                offer(cand["kinds"][i], W[i], tag, P[i], refs=ids[i] if ids else None, dist=float(d[i]))
        near_lines = []
        pr = _prep(cand)
        lids = cand.get("line_ids")
        nL = len(pr["L"])
        if nL:
            WL = transform(pr["L"].reshape(-1, 3), M).reshape(-1, 2, 3)
            for li in range(nL):
                world_lines.append((tag, WL[li, 0], WL[li, 1], lids[li] if lids else None))
            DL = project(WL.reshape(-1, 3)).reshape(-1, 2, 2)
            dl, _t, _l2 = _seg_dist2d(DL[:, 0], DL[:, 1], cur)
            for li in np.nonzero(dl <= tol * 2)[0]:
                near_lines.append((WL[li, 0], WL[li, 1], int(li)))
        else:
            WL = np.zeros((0, 2, 3))
        # intersections of nearby straight edges
        if o.get("intersection", True) and len(near_lines) > 1:
            scale = max((np.linalg.norm(B - A) for A, B, _ in near_lines), default=1.0)
            for i in range(len(near_lines)):
                for j in range(i + 1, len(near_lines)):
                    r = _seg_closest(near_lines[i][0], near_lines[i][1], near_lines[j][0], near_lines[j][1])
                    if r is not None and np.linalg.norm(r[0] - r[1]) < 1e-6 * scale + 1e-9:
                        offer("intersection", r[0], tag)
        # perpendicular foot from the anchor; extension beyond the ends
        for A, B, li in near_lines:
            d = B - A
            if anchor is not None:
                s = float((np.asarray(anchor) - A) @ d / (d @ d))
                if 0 <= s <= 1:
                    lid = cand.get("line_ids")[li] if cand.get("line_ids") else None
                    offer("perpendicular", A + d * s, tag, refs={"line": lid} if lid else None,
                          guides=[(anchor, A + d * s)])
        if ray is not None and o.get("extension", True) and nL:
            ro, rd = (np.asarray(v, float) for v in ray)
            A, d = WL[:, 0], WL[:, 1] - WL[:, 0]
            w0 = A - ro
            a_, b_, c_ = np.einsum("ij,ij->i", d, d), d @ rd, float(rd @ rd)
            dd, e_ = np.einsum("ij,ij->i", d, w0), w0 @ rd
            den = a_ * c_ - b_ * b_
            ok = np.abs(den) >= 1e-12
            sv = np.where(ok, (b_ * e_ - c_ * dd) / np.where(ok, den, 1.0), 0.5)
            keep = ok & ~((sv >= 0) & (sv <= 1)) & (sv >= -4) & (sv <= 5)
            idx = np.nonzero(keep)[0]
            if len(idx):
                Q = A[idx] + d[idx] * sv[idx, None]
                DQ = project(Q)
                dq = np.hypot(DQ[:, 0] - x, DQ[:, 1] - y)
                for k in np.nonzero(dq <= tol)[0]:
                    i = idx[k]
                    end = WL[i, 1] if sv[i] > 1 else WL[i, 0]
                    offer("extension", Q[k], tag, guides=[(end, Q[k])], dist=float(dq[k]))
        # tangent from the anchor to circles
        if anchor is not None and o.get("tangent", True):
            for c, n, r, closed in cand["circles"]:
                C = transform(np.array([c]), M)[0]
                N = _dir(M, n)
                N = N / (np.linalg.norm(N) or 1)
                q = np.asarray(anchor, float) - N * float((np.asarray(anchor) - C) @ N)   # anchor in circle plane
                v = q - C
                dd = float(np.linalg.norm(v))
                if dd <= r + 1e-9:
                    continue
                u = v / dd
                w = np.cross(N, u)
                ang = math.acos(r / dd)
                for sgn in (1, -1):
                    tpt = C + r * (math.cos(ang) * u + sgn * math.sin(ang) * w)
                    offer("tangent", tpt, tag, guides=[(anchor, tpt)])
        # nearest point on edges
        if o.get("on edge", True) and (best is None or best[0][0] > RANK["on edge"]) and len(pr["SA"]):
            SA, SB = pr["SA"], pr["SB"]
            WA, WB = transform(SA, M), transform(SB, M)
            DS = project(np.vstack([WA, WB]))
            n = len(SA)
            ds, ts, l2 = _seg_dist2d(DS[:n], DS[n:], cur)
            ds = np.where(l2 < 1e-9, np.inf, ds)
            j = int(np.argmin(ds))
            if ds[j] <= tol:
                t = float(ts[j])
                pw = WA[j] + (WB[j] - WA[j]) * t
                pl = SA[j] + (SB[j] - SA[j]) * t
                refs = None
                poly = cand["edges"][int(pr["SP"][j])]
                if lids and len(poly) == 2:
                    k = next((i for i, (A, B) in enumerate(cand["lines"])
                              if np.allclose(A, poly[0]) and np.allclose(B, poly[1])), None)
                    if k is not None:
                        refs = {"on_line": lids[k]}
                offer("on edge", pw, tag, pl, refs=refs, dist=float(ds[j]))
    if origin and o.get("origin", True):
        offer("origin", np.zeros(3), None)
    if best is not None and best[0][0] <= RANK["on edge"]:
        return best[1]
    # inference lines from the anchor (along X/Y/Z, parallel to edges) and alignment with vertices
    if ray is not None and anchor is not None:
        ro, rd = (np.asarray(v, float) for v in ray)
        A0 = np.asarray(anchor, float)
        if o.get("along axis", True):
            for name, ax in AXES.items():
                s = _closest_line_ray(A0, ax, ro, rd)
                if s is not None and abs(s) > 1e-9:
                    q = A0 + ax * s
                    offer("along axis", q, None, refs={"axis": name}, guides=[(A0, q)])
        if o.get("parallel", True):
            seen = set()
            for tag, A, B, _lid in world_lines:
                d = B - A
                L = float(np.linalg.norm(d))
                if L < 1e-9:
                    continue
                d = d / L
                key = tuple(np.round(np.abs(d), 4))
                if key in seen or max(abs(d)) > 0.9999:      # axis directions are covered by along-axis
                    continue
                seen.add(key)
                s = _closest_line_ray(A0, d, ro, rd)
                if s is not None and abs(s) > 1e-9:
                    q = A0 + d * s
                    offer("parallel", q, tag, refs={"direction": d.tolist()}, guides=[(A0, q), (A, B)])
    if ray is not None and o.get("aligned", True) and (best is None or best[0][0] > RANK["aligned"]):
        ro, rd = (np.asarray(v, float) for v in ray)
        for tag, cand, M in sets:
            P = cand["pts"]
            if not len(P):
                continue
            W = transform(P[[i for i, k in enumerate(cand["kinds"]) if k in ("endpoint", "center", "midpoint")]],
                          M)
            Dp = project(W)
            near = np.nonzero(np.hypot(Dp[:, 0] - x, Dp[:, 1] - y) < tol * 12)[0][:40]
            for i in near:
                for name, ax in AXES.items():
                    s = _closest_line_ray(W[i], ax, ro, rd)
                    if s is not None and abs(s) > 1e-6:
                        q = W[i] + ax * s
                        offer("aligned", q, tag, refs={"axis": name, "with": W[i].tolist()}, guides=[(W[i], q)])
    if best is not None:
        return best[1]
    # on the surface under the cursor
    if ray is not None and o.get("on face", True):
        ro, rd = (np.asarray(v, float) for v in ray)
        hits = []
        for tag, cand, M in sets:
            h = ray_hit(cand, M, ro, rd)
            if h is not None:
                hits.append((float((h[0] - ro) @ rd), tag, h))
        if hits:
            _, tag, (pw, n, fi, pl) = min(hits, key=lambda q: q[0])
            return {"p": pw.tolist(), "kind": "on face", "tag": tag, "local": pl.tolist(), "guides": [],
                    "refs": {}, "normal": None if n is None else list(map(float, n)), "face": fi}
    return None


def increments(anchor, p, length_step=0.0, angle_step=0.0, plane_normal=None):
    """Round the distance from anchor (length_step) and the direction in a plane (angle_step)."""
    a, q = np.asarray(anchor, float), np.asarray(p, float)
    v = q - a
    L = float(np.linalg.norm(v))
    if L < 1e-12:
        return q.tolist(), None
    note = None
    if angle_step and plane_normal is not None:
        n = np.asarray(plane_normal, float)
        n = n / np.linalg.norm(n)
        vp = v - n * (v @ n)
        if np.linalg.norm(vp) > 1e-9:
            e1 = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
            e1 = e1 - n * (e1 @ n)
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(n, e1)
            ang = math.degrees(math.atan2(vp @ e2, vp @ e1))
            k = round(ang / angle_step) * angle_step
            if abs(ang - k) < max(2.0, angle_step * 0.15):
                r = math.radians(k)
                vp = (math.cos(r) * e1 + math.sin(r) * e2) * np.linalg.norm(vp)
                v = vp + n * (v @ n)
                note = f"{k:g}°"
    if length_step:
        L = float(np.linalg.norm(v))
        Ln = max(length_step, round(L / length_step) * length_step)
        v = v / L * Ln
        note = (note + " · " if note else "") + f"{Ln:g} mm"
    return (a + v).tolist(), note


def vtk_projector(renderer):
    """(N,3) world → (N,2) display coordinates for a VTK renderer (vectorised)."""
    st = {}

    def _setup():
        cam = renderer.GetActiveCamera()
        w, h = renderer.GetSize()
        M = cam.GetCompositeProjectionTransformMatrix(renderer.GetTiledAspectRatio(), -1, 1)
        st["A"] = np.array([[M.GetElement(i, j) for j in range(4)] for i in range(4)]).T
        st["wh"] = (w, h)
        st["o"] = renderer.GetOrigin()

    def project(W):
        W = np.asarray(W, float).reshape(-1, 3)
        if not len(W):
            return np.zeros((0, 2))
        if not st:
            _setup()
        A, (w, h), (ox, oy) = st["A"], st["wh"], st["o"]
        H = W @ A[:3] + A[3]
        ndc = H[:, :2] / H[:, 3:4]
        return np.c_[(ndc[:, 0] + 1) / 2 * w + ox, (ndc[:, 1] + 1) / 2 * h + oy]
    return project


def vtk_ray(renderer, x, y):
    renderer.SetDisplayPoint(x, y, 0.0)
    renderer.DisplayToWorld()
    p0 = np.array(renderer.GetWorldPoint())
    renderer.SetDisplayPoint(x, y, 1.0)
    renderer.DisplayToWorld()
    p1 = np.array(renderer.GetWorldPoint())
    a, b = p0[:3] / p0[3], p1[:3] / p1[3]
    d = b - a
    return a, d / np.linalg.norm(d)
