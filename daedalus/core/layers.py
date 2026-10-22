"""Layers: named groups of features with colour, visibility, lock and an
"in model" switch.

• visible   — display only: the layer's faces and sketches are hidden in the view.
• locked    — features on the layer can't be edited, moved, renamed or deleted (by anyone, Claude included).
• in_model  — off = every feature on the layer is left out of the rebuild (like suppressing them all).
• colour    — faces made by the layer's features are drawn in it.

Faces of the final body are attributed to the feature that made them by
testing each face against the tool solids features leave in the build
context, latest first. Fillets/chamfers inherit the layer of the body they
modify; faces nothing claims (an imported STEP) go to the default layer.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

DEFAULT = "Default"
PALETTE = ["#b4b8bf", "#d9822b", "#4f9fd9", "#6cbf6c", "#c96bc9", "#e0c341", "#e06c6c", "#5fc9c0"]


@dataclass
class Layer:
    name: str
    color: str = PALETTE[0]
    visible: bool = True
    locked: bool = False
    in_model: bool = True

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in ("name", "color", "visible", "locked", "in_model") if k in d})


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[^\s/\\][^/\\]{0,40}", name or "")) and name.strip() == name


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) != 6:
        return (0.7, 0.72, 0.75)
    return tuple(int(h[i: i + 2], 16) / 255 for i in (0, 2, 4))


MESH_TOL = 0.01      # sample-point mesh deflection (mm); matching tolerance is a little above it


def _point_on_face(face, mesh_tol=MESH_TOL):
    """A point guaranteed to lie on the (trimmed) face: centre of its largest mesh triangle."""
    try:
        verts, tris = face.tessellate(mesh_tol, 0.15)
        best, area = None, -1.0
        for a, b, c in tris:
            A, B, C = verts[a], verts[b], verts[c]
            ar = ((B - A).cross(C - A)).length
            if ar > area:
                best, area = (A + B + C) * (1 / 3), ar
        if best is not None:
            return best
    except Exception:  # noqa: BLE001
        pass
    return face.center()


def attribute_faces(body, tools: dict, order: list[str], tol: float = MESH_TOL * 2) -> list[str | None]:
    """For each face of `body` (in body.faces() order) return the name of the latest
    feature (by `order`) whose tool solid touches the face, or None."""
    if body is None:
        return []
    faces = list(body.faces())
    cands = [(name, tools[name][0]) for name in reversed(order) if name in tools]
    if len(cands) == 1:                       # one feature made every face (e.g. an imported part)
        return [cands[0][0]] * len(faces)
    try:                                      # keep meshing proportional on very large parts
        diag = body.bounding_box().diagonal
    except Exception:  # noqa: BLE001
        diag = 0.0
    mesh_tol = max(MESH_TOL, diag * 2e-5)
    tol = max(tol, mesh_tol * 2)
    boxes = {}
    for name, tool in cands:
        try:
            bb = tool.bounding_box()
            boxes[name] = (bb.min.X - tol, bb.min.Y - tol, bb.min.Z - tol,
                           bb.max.X + tol, bb.max.Y + tol, bb.max.Z + tol)
        except Exception:  # noqa: BLE001
            boxes[name] = None
    out: list[str | None] = []
    for f in faces:
        p = _point_on_face(f, mesh_tol)
        owner = None
        for name, tool in cands:
            bx = boxes[name]
            if bx and not (bx[0] <= p.X <= bx[3] and bx[1] <= p.Y <= bx[4] and bx[2] <= p.Z <= bx[5]):
                continue
            try:
                if tool.distance_to(p) <= tol:
                    owner = name
                    break
            except Exception:  # noqa: BLE001
                continue
        out.append(owner)
    return out
