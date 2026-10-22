"""2D drawing / drafting model. Qt-free.

A Drawing holds sheets. A sheet has views (projections of the 3D model, regenerated when the model
changes) and items (2D entities in sheet millimetres: lines, arcs, circles, polylines, text, dimensions,
hatches) on layers. The same model serves model drawings and plain 2D documents opened from DXF/DWG/SVG.

Primitive dicts (shared by views and items):
  {"t": "line", "a": [x, y], "b": [x, y]}
  {"t": "poly", "pts": [[x, y], ...], "closed": bool}
  {"t": "circle", "c": [x, y], "r": r}
  {"t": "arc", "c": [x, y], "r": r, "a0": deg, "a1": deg}        (counter-clockwise a0 → a1)
  {"t": "text", "p": [x, y], "text": str, "h": mm, "angle": deg, "align": "left|center|right"}
  {"t": "hatch", "loops": [[[x, y], ...], ...], "pattern": "ANSI31", "spacing": mm}
  {"t": "dim", "kind": "h|v|aligned|dia|rad|angle", "p1": [x, y], "p2": [x, y], "at": [x, y], "text": "",
   "c": [x, y] (dia/rad centre), "r": r}
  v2.8 (see drawing/annot.py): {"t": "gdt" …} feature control frames, {"t": "datum" …}, {"t": "finish" …},
  and dims of kind "ordinate" (p1 = origin, p2 = point, axis x|y).
Each primitive may carry "style": visible | hidden | center | thin | dim | section | border | text,
"layer" and "color" ("#rrggbb").
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field

SHEETS = {  # name: (width, height) landscape, mm
    "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
    "Letter (A)": (279.4, 215.9), "Tabloid (B)": (431.8, 279.4), "ANSI C": (558.8, 431.8),
    "ANSI D": (863.6, 558.8), "ANSI E": (1117.6, 863.6), "Model space": (0, 0),
}
SCALES = [(50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 2.5), (1, 5), (1, 10), (1, 20),
          (1, 25), (1, 50), (1, 100), (1, 200)]
STYLES = {  # line weight mm, dash pattern (mm on/off) or None, colour
    "visible": (0.5, None, "#000000"), "hidden": (0.25, (3.0, 1.5), "#000000"),
    "center": (0.18, (8.0, 1.5, 1.5, 1.5), "#000000"), "thin": (0.18, None, "#000000"),
    "dim": (0.18, None, "#000000"), "section": (0.35, (10.0, 2.0, 2.0, 2.0), "#000000"),
    "border": (0.7, None, "#000000"), "text": (0.25, None, "#000000"), "hatch": (0.13, None, "#000000"),
    "construction": (0.13, (2.0, 2.0), "#7a8794"),
}
VIEW_DIRS = {  # name: (direction from the part to the viewer, up)
    "front": ((0, -1, 0), (0, 0, 1)), "back": ((0, 1, 0), (0, 0, 1)), "top": ((0, 0, 1), (0, 1, 0)),
    "bottom": ((0, 0, -1), (0, -1, 0)), "right": ((1, 0, 0), (0, 0, 1)), "left": ((-1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)), "dimetric": ((1, -2, 1.2), (0, 0, 1)), "trimetric": ((1, -1.6, 0.9), (0, 0, 1)),
}


def new_id():
    return uuid.uuid4().hex[:8]


@dataclass
class Options:
    standard: str = "ISO"            # ISO (first-angle) | ANSI (third-angle)
    units: str = "mm"                # dimension units: mm | in
    decimals: int = 1
    text_height: float = 3.5
    dim_text_height: float = 3.0
    arrow: float = 3.0
    hidden_lines: bool = True
    center_marks: bool = True
    auto_dims: bool = True
    hole_callouts: bool = True
    tangent_edges: bool = True
    bom: bool = True
    balloons: bool = True
    hatch_spacing: float = 3.0
    font: str = "Arial"
    dxf_version: str = "R2010"
    snap: bool = True
    grid: float = 0.0                # sheet-mm snap grid, 0 = off

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


@dataclass
class Sheet:
    name: str = "Sheet1"
    size: str = "A3"
    landscape: bool = True
    scale: list = field(default_factory=lambda: [1, 1])
    title: dict = field(default_factory=dict)       # title block fields
    views: list = field(default_factory=list)       # view dicts
    items: list = field(default_factory=list)       # 2D primitives in sheet mm
    layers: dict = field(default_factory=lambda: {"0": {"color": "#000000", "visible": True}})
    border: bool = True
    revisions: list = field(default_factory=list)   # [{"rev", "description", "date", "approved", "zone"}]

    def dims(self):
        w, h = SHEETS.get(self.size, (420, 297))
        return (w, h) if self.landscape else (h, w)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: copy.deepcopy(v) for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Drawing:
    name: str = "Drawing1"
    sheets: list = field(default_factory=list)       # [Sheet]
    options: Options = field(default_factory=Options)
    linked: bool = True                              # views come from the 3D model
    source: str = ""                                 # file it was opened from (2D documents)

    def sheet(self, name=None) -> Sheet:
        if not self.sheets:
            self.sheets.append(Sheet())
        if name is None:
            return self.sheets[0]
        for s in self.sheets:
            if s.name == name:
                return s
        raise KeyError(f"No sheet '{name}'")

    def to_dict(self):
        return {"name": self.name, "sheets": [s.to_dict() for s in self.sheets], "options": self.options.to_dict(),
                "linked": self.linked, "source": self.source}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("name", "Drawing1"), [Sheet.from_dict(s) for s in d.get("sheets", [])],
                   Options.from_dict(d.get("options")), d.get("linked", True), d.get("source", ""))


def scale_text(scale) -> str:
    a, b = scale
    return f"{a:g}:{b:g}"


def scale_factor(scale) -> float:
    return float(scale[0]) / float(scale[1])
