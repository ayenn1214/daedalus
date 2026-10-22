"""Manufacturability checks on the tessellated part: mould/cast draft and undercuts, wall thickness, and 3D-print
overhangs. Each returns per-vertex values on an unshared triangle mesh (flat colour per triangle) for the
result viewer, plus a summary for Claude."""
from __future__ import annotations

import math

import numpy as np

CHECKS = {
    "draft": "Draft angle against a pull direction; red = undercut / negative draft, yellow = below minimum",
    "thickness": "Wall thickness by ray casting through the part; red = thinner than the minimum",
    "overhang": "3D-print overhangs steeper than the limit from the build direction (need supports)",
}


def _mesh(body, tol=0.05):
    v, t = body.tessellate(tol, 0.2)
    V = np.array([[q.X, q.Y, q.Z] for q in v], float)
    T = np.array(t, np.int64)
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    n = np.cross(b - a, c - a)
    area = np.linalg.norm(n, axis=1) / 2
    keep = area > 1e-12
    T, a, b, c, n, area = T[keep], a[keep], b[keep], c[keep], n[keep], area[keep]
    n = n / np.linalg.norm(n, axis=1)[:, None]
    # make sure normals point out (signed volume positive)
    if np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) < 0:
        n = -n
        T = T[:, [0, 2, 1]]
    return V, T, n, area, (a + b + c) / 3


def _locator(V, T):
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    pd = vtk.vtkPolyData()
    pts = vtk.vtkPoints()
    pts.SetData(numpy_to_vtk(np.ascontiguousarray(V), deep=True))
    pd.SetPoints(pts)
    cells = np.hstack([np.full((len(T), 1), 3, np.int64), T]).ravel()
    ca = vtk.vtkCellArray()
    ca.SetCells(len(T), numpy_to_vtkIdTypeArray(cells, deep=True))
    pd.SetPolys(ca)
    loc = vtk.vtkCellLocator()
    loc.SetDataSet(pd)
    loc.BuildLocator()
    return loc, pd


def _first_hit(loc, p0, p1):
    import vtk
    t = vtk.reference(0.0)
    x = [0.0, 0.0, 0.0]
    pc = [0.0, 0.0, 0.0]
    sub = vtk.reference(0)
    cid = vtk.reference(0)
    if loc.IntersectWithLine(p0, p1, 1e-9, t, x, pc, sub, cid):
        return math.dist(p0, x)
    return None


def _unshared(V, T, per_tri):
    P = V[T].reshape(-1, 3)
    tris = np.arange(len(P)).reshape(-1, 3)
    vals = np.repeat(per_tri, 3)
    return P, tris, vals


def analyze(body, check="draft", pull=(0, 0, 1), min_draft=1.0, min_wall=1.0, max_overhang=45.0, tol=0.05):
    """Returns {"p", "tris", "values", "label", "unit", "rng", "reverse", "summary"} in the body's frame."""
    V, T, n, area, cen = _mesh(body, tol)
    total = float(area.sum())
    size = float(np.linalg.norm(V.max(0) - V.min(0)))
    if check == "draft":
        d = np.asarray(pull, float)
        d /= np.linalg.norm(d)
        cosang = np.clip(n @ d, -1, 1)
        draft = 90.0 - np.degrees(np.arccos(np.abs(cosang)))      # 0 = vertical wall, 90 = flat
        # undercuts: a face is moulded by the half it faces; shadowed in that direction → undercut
        loc, _ = _locator(V, T)
        side = np.where(cosang >= 0, 1.0, -1.0)
        under = np.zeros(len(T), bool)
        eps = 1e-3 * max(size, 1)
        for i in np.nonzero(np.abs(cosang) > 1e-3)[0]:
            p0 = cen[i] + n[i] * eps + d * side[i] * eps
            if _first_hit(loc, tuple(p0), tuple(p0 + d * side[i] * size * 2)) is not None:
                under[i] = True
        vals = np.where(under, -5.0, np.where(np.abs(cosang) < 1e-3, 0.0, draft))
        vals = np.minimum(vals, 10.0)
        a_under = float(area[under].sum())
        a_low = float(area[(~under) & (draft < min_draft)].sum())
        P, tris, vv = _unshared(V, T, vals)
        return {"p": P, "tris": tris, "values": vv, "label": "Draft", "unit": "°", "rng": (-5.0, 10.0),
                "reverse": True, "summary": {
                    "check": "draft", "pull": list(map(float, d)), "min_draft_deg": min_draft,
                    "undercut_area_pct": round(100 * a_under / total, 2),
                    "insufficient_draft_area_pct": round(100 * a_low / total, 2),
                    "verdict": "undercuts — needs side actions or a split" if a_under > 1e-6 * total else
                    ("add draft to vertical faces" if a_low > 1e-6 * total else "mouldable as drawn")}}
    if check == "thickness":
        loc, _ = _locator(V, T)
        th = np.full(len(T), np.nan)
        eps = 1e-4 * max(size, 1)
        for i in range(len(T)):
            p0 = cen[i] - n[i] * eps
            h = _first_hit(loc, tuple(p0), tuple(p0 - n[i] * size * 2))
            if h is not None:
                th[i] = h + eps
        thin = np.nan_to_num(th, nan=size) < min_wall
        P, tris, vv = _unshared(V, T, np.nan_to_num(th, nan=np.nanmax(th) if np.isfinite(th).any() else 0))
        fin = th[np.isfinite(th)]
        return {"p": P, "tris": tris, "values": vv, "label": "Wall thickness", "unit": "mm",
                "rng": (0.0, float(min(np.percentile(fin, 95), 4 * max(min_wall, 0.1)))) if fin.size else None,
                "reverse": True, "summary": {
                    "check": "thickness", "min_wall_mm": min_wall,
                    "thinnest_mm": round(float(fin.min()), 3) if fin.size else None,
                    "median_mm": round(float(np.median(fin)), 3) if fin.size else None,
                    "thin_area_pct": round(100 * float(area[thin].sum()) / total, 2),
                    "verdict": "thin walls below the minimum" if thin.any() else "all walls above the minimum"}}
    if check == "overhang":
        b = np.asarray(pull, float)
        b /= np.linalg.norm(b)
        down = -(n @ b)                              # 1 = facing straight down
        ang = np.degrees(np.arcsin(np.clip(down, -1, 1)))   # angle below horizontal… 90 = flat underside
        # angle from vertical for downward faces: overhang = 90 - angle between n and -b
        over = np.degrees(np.arccos(np.clip(down, -1, 1)))  # 0 = flat ceiling, 90 = vertical wall
        zmin = float((V @ b).min())
        on_plate = np.abs(cen @ b - zmin) < 1e-3 * max(size, 1) + 1e-6
        needs = (down > 0) & (over < 90 - max_overhang) & ~on_plate
        vals = np.where(down > 0, 90.0 - over, 0.0)        # 0 safe … 90 flat ceiling
        vals[on_plate] = 0.0
        P, tris, vv = _unshared(V, T, vals)
        a = float(area[needs].sum())
        return {"p": P, "tris": tris, "values": vv, "label": "Overhang", "unit": "°", "rng": (0.0, 90.0),
                "reverse": False, "summary": {
                    "check": "overhang", "build_dir": list(map(float, b)), "limit_deg": max_overhang,
                    "support_area_mm2": round(a, 1), "support_area_pct": round(100 * a / total, 2),
                    "verdict": "needs supports (or reorient)" if a > 1e-6 * total else "prints without supports"}}
    raise ValueError(f"check must be one of {list(CHECKS)}")


def best_orientation(body, max_overhang=45.0):
    """Of the six axis directions, the build direction with the least support area."""
    best = None
    for d in ((0, 0, 1), (0, 0, -1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        s = analyze(body, "overhang", d, max_overhang=max_overhang)["summary"]
        if best is None or s["support_area_mm2"] < best[1]:
            best = (d, s["support_area_mm2"])
    return {"build_dir": list(best[0]), "support_area_mm2": best[1]}
