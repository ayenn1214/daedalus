"""Geometric selectors.

Downstream features (fillet, chamfer, shell, hole) reference geometry by
*description* ("top", "vertical", "circular") rather than by edge/face index.
Indices get renumbered whenever upstream features change — the topological
naming problem. Descriptive selectors survive most parameter edits.

Edge selector grammar:  term ("&" term)*   — intersection
                        expr ("|" expr)*    — union
Terms: all, top, bottom, front, back, left, right,
       vertical, parallel_x, parallel_y, circular, linear,
       at(x,y,z)  — the one edge passing closest to a point (what clicking an edge in the view records)
Face selectors: top, bottom, front, back, left, right, or at(x,y,z) — the face closest to a point.
"""
from __future__ import annotations

import re

from build123d import Axis, GeomType

FACE_SELECTORS = ["top", "bottom", "front", "back", "left", "right"]
EDGE_TERMS = [
    "all", "top", "bottom", "front", "back", "left", "right",
    "vertical", "parallel_x", "parallel_y", "circular", "linear", "at(x,y,z)",
]
_AT = re.compile(r"^at\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)$")


def at_expr(points) -> str:
    """Selector text for picked points: 'at(1,2,3)|at(4,5,6)'."""
    return "|".join("at({:.4g},{:.4g},{:.4g})".format(*p) for p in points)


def _nearest(shapes, term):
    from build123d import Vector
    m = _AT.match(term)
    p = Vector(*(float(v) for v in m.groups()))
    best, bd = None, None
    for s in shapes:
        try:
            d = s.distance_to(p)
        except Exception:  # noqa: BLE001
            continue
        if bd is None or d < bd:
            best, bd = s, d
    if best is None:
        raise SelectorError(f"Nothing near {term}")
    return best

_SIDE = {
    "top": (Axis.Z, -1), "bottom": (Axis.Z, 0),
    "back": (Axis.Y, -1), "front": (Axis.Y, 0),
    "right": (Axis.X, -1), "left": (Axis.X, 0),
}


class SelectorError(ValueError):
    pass


def select_face(solid, name: str):
    name = name.strip().lower()
    if _AT.match(name):
        return _nearest(solid.faces(), name)
    if name not in _SIDE:
        raise SelectorError(f"Unknown face selector '{name}'. Use one of {FACE_SELECTORS}")
    axis, idx = _SIDE[name]
    faces = solid.faces().sort_by(axis)
    if not faces:
        raise SelectorError("Body has no faces")
    return faces[idx]


def _edges_for_term(solid, term: str):
    term = term.strip().lower()
    edges = solid.edges()
    if term == "all":
        return list(edges)
    if _AT.match(term):
        return [_nearest(edges, term)]
    if term in _SIDE:
        axis, idx = _SIDE[term]
        groups = edges.group_by(axis)
        return list(groups[idx])
    if term == "vertical":
        return list(edges.filter_by(Axis.Z))
    if term == "parallel_x":
        return list(edges.filter_by(Axis.X))
    if term == "parallel_y":
        return list(edges.filter_by(Axis.Y))
    if term == "circular":
        return [e for e in edges if e.geom_type == GeomType.CIRCLE]
    if term == "linear":
        return [e for e in edges if e.geom_type == GeomType.LINE]
    raise SelectorError(f"Unknown edge selector '{term}'. Terms: {EDGE_TERMS}")


def select_edges(solid, expr: str):
    """Evaluate an edge selector expression. Returns a list of edges."""
    result: dict[int, object] = {}
    for union_part in expr.split("|"):
        terms = [t for t in union_part.split("&") if t.strip()]
        if not terms:
            continue
        sets = [_edges_for_term(solid, t) for t in terms]
        keep = sets[0]
        for other in sets[1:]:
            hashes = {hash(e) for e in other}
            keep = [e for e in keep if hash(e) in hashes]
        for e in keep:
            result[hash(e)] = e
    edges = list(result.values())
    if not edges:
        raise SelectorError(f"Selector '{expr}' matched no edges")
    return edges
