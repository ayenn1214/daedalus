"""Geometry fingerprints and similarity for standard parts.

The fingerprint doesn't depend on position or orientation:
    volume, surface area, principal moments of inertia (normalised by volume^(5/3) → pure shape),
    face-type mix (planes / cylinders / cones / spheres / tori / free-form), sorted cylinder diameters
    (holes, shafts, bosses), face and edge counts.
Mirror images have identical fingerprints (flagged as "identical or mirrored").
"""
from __future__ import annotations

import math
from difflib import SequenceMatcher

KINDS = ("PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS", "OTHER")


def fingerprint(bodies) -> dict:
    from build123d import Compound
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
    from OCP.GProp import GProp_GProps
    bodies = bodies if isinstance(bodies, (list, tuple)) else [bodies]
    shape = Compound(list(bodies)) if len(bodies) > 1 else bodies[0]
    vp = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, vp)
    V = abs(vp.Mass())
    sp = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape.wrapped, sp)
    A = sp.Mass()
    pm = vp.PrincipalProperties()
    ix, iy, iz = pm.Moments()
    mom = sorted(abs(x) / (V ** (5 / 3)) for x in (ix, iy, iz)) if V > 1e-9 else [0, 0, 0]
    hist = dict.fromkeys(KINDS, 0)
    diam = []
    kind_map = {GeomAbs_Plane: "PLANE", GeomAbs_Cylinder: "CYLINDER", GeomAbs_Cone: "CONE",
                GeomAbs_Sphere: "SPHERE", GeomAbs_Torus: "TORUS"}
    faces = shape.faces()
    for f in faces:
        ad = BRepAdaptor_Surface(f.wrapped)
        k = kind_map.get(ad.GetType(), "OTHER")
        hist[k] += 1
        if k == "CYLINDER":
            diam.append(round(2 * ad.Cylinder().Radius(), 2))
    # principal extents: box of the shape in its own inertia frame ≈ sqrt of moments → use bbox of sorted dims
    bb = shape.bounding_box()
    return {"v": 1, "volume": round(V, 4), "area": round(A, 4), "moments": [round(m, 6) for m in mom],
            "faces": hist, "n_faces": len(faces), "n_edges": len(shape.edges()),
            "diameters": sorted(set(diam)), "box": sorted([round(bb.size.X, 3), round(bb.size.Y, 3),
                                                           round(bb.size.Z, 3)])}


def _rel(a, b):
    """1 when equal, → 0 as they differ (relative)."""
    a, b = float(a), float(b)
    if a == b:
        return 1.0
    m = max(abs(a), abs(b))
    return max(0.0, 1.0 - abs(a - b) / m) if m > 0 else 1.0


def _set_sim(a, b, tol=0.02):
    a, b = list(a), list(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    hit = 0
    pool = list(b)
    for x in a:
        j = next((i for i, y in enumerate(pool) if abs(x - y) <= max(tol, 0.005 * max(x, y))), None)
        if j is not None:
            hit += 1
            pool.pop(j)
    return hit / max(len(a), len(b))


def geometry_score(f1: dict, f2: dict) -> tuple[float, list[str]]:
    """0…1 and the reasons. Weighted: size (volume, area) 40 %, shape (inertia) 30 %, features 30 %."""
    if not f1 or not f2:
        return 0.0, []
    sv, sa = _rel(f1["volume"], f2["volume"]), _rel(f1["area"], f2["area"])
    sm = sum(_rel(a, b) for a, b in zip(f1["moments"], f2["moments"])) / 3
    h1, h2 = f1["faces"], f2["faces"]
    tot = sum(max(h1.get(k, 0), h2.get(k, 0)) for k in KINDS) or 1
    sh = sum(min(h1.get(k, 0), h2.get(k, 0)) for k in KINDS) / tot
    sd = _set_sim(f1.get("diameters", []), f2.get("diameters", []))
    score = 0.2 * sv + 0.2 * sa + 0.3 * sm + 0.15 * sh + 0.15 * sd
    why = []
    if sv > 0.995 and sa > 0.995 and sm > 0.995 and sh == 1 and sd == 1:
        why.append("identical geometry (or a mirror image)")
    else:
        if sv > 0.97:
            why.append(f"volume within {100 * (1 - sv):.1f} %")
        if sm > 0.97:
            why.append("same proportions")
        if sd == 1 and f1.get("diameters"):
            why.append("same hole/shaft diameters")
        elif sd >= 0.5 and f1.get("diameters"):
            why.append("shares diameters")
        if sh > 0.9:
            why.append("same face types")
        b1, b2 = f1.get("box"), f2.get("box")
        if b1 and b2 and all(_rel(a, b) > 0.98 for a, b in zip(b1, b2)):
            why.append("same overall size")
    return score, why


def text_score(a: dict, b: dict) -> tuple[float, list[str]]:
    """Strong signals from the part info."""
    why = []
    s = 0.0

    def eq(k):
        x, y = (a.get(k) or "").strip().lower(), (b.get(k) or "").strip().lower()
        return bool(x) and x == y
    if eq("part_number"):
        why.append("same part number")
        s = 1.0
    if eq("vendor_part_number") and (a.get("vendor") or "").lower() == (b.get("vendor") or "").lower():
        why.append("same vendor part number")
        s = max(s, 0.99)
    na, nb = (a.get("name") or "").lower(), (b.get("name") or "").lower()
    if na and nb:
        r = SequenceMatcher(None, na, nb).ratio()
        if r > 0.85:
            why.append("similar name" if r < 1 else "same name")
            s = max(s, 0.6 + 0.3 * r)
    return s, why


def compare(fp: dict | None, info: dict, other: dict) -> dict:
    g, gw = geometry_score(fp, other.get("signature")) if fp else (0.0, [])
    t, tw = text_score(info or {}, other)
    score = max(g, t)
    if g > 0.75 and t > 0.6:
        score = min(1.0, max(g, t) + 0.05)
    level = "identical" if (g >= 0.985 or t >= 0.99) else "very similar" if score >= 0.92 else \
        "similar" if score >= 0.8 else ""
    return {"score": round(score, 4), "geometry": round(g, 4), "text": round(t, 4), "level": level,
            "reasons": tw + gw}


__all__ = ["fingerprint", "compare", "geometry_score", "text_score", "math"]
