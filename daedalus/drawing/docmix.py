"""Drawing operations shared by the full Document and the light 2D document (drawings module on its own).
Needs from the host: drawings, dirty, checkpoint(), _emit(); model views also use objects, obj(), result."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..errors import FeatureError


class DrawingDocMixin:
    def drawing(self, name: str | None = None):
        if not self.drawings:
            raise FeatureError("No drawings yet — create one with new_drawing")
        if name is None:
            return self.drawings[0]
        for d in self.drawings:
            if d.name == name:
                return d
        raise FeatureError(f"No drawing named '{name}'. Drawings: {[d.name for d in self.drawings]}")

    def new_drawing(self, name: str | None = None, size: str = "A3", views=("front", "top", "right", "iso"),
                    objects=None, standard: str | None = None, units: str | None = None, landscape: bool = True,
                    title: dict | None = None, bom: bool | None = None, flat: str | None = None):
        """A drawing sheet with a standard view set laid out at an automatic scale."""
        from ..drawing import views as dv
        from ..drawing.model import SHEETS, Drawing, Sheet
        if size not in SHEETS:
            raise FeatureError(f"Sheet size must be one of {list(SHEETS)}")
        bad = [v for v in views if v not in ("front", "back", "top", "bottom", "right", "left", "iso", "dimetric",
                                              "trimetric")]
        if bad:
            raise FeatureError(f"Unknown view(s) {bad}")
        for o in objects or []:
            self.obj(o)
        name = name or f"Drawing{len(self.drawings) + 1}"
        if any(d.name == name for d in self.drawings):
            raise FeatureError(f"A drawing named '{name}' exists")
        self.checkpoint()
        dr = Drawing(name=name)
        if standard:
            dr.options.standard = standard
        if units:
            dr.options.units = units
        sh = Sheet(size=size, landscape=landscape, title=dict(title or {}))
        dr.sheets.append(sh)
        if flat:
            if flat not in self.result.flats:
                raise FeatureError(f"'{flat}' isn't a built sheet-metal feature")
            v = {"id": dv.new_id(), "kind": "flat", "feature": flat, "label": "FLAT PATTERN"}
            dv.compute(self, v, dr.options)
            x0, y0, x1, y1 = v["_bbox"]
            W, H = sh.dims()
            sh.scale = dv.nice_scale(x1 - x0, y1 - y0, W - 60, H - 90)
            from ..drawing.model import scale_factor
            v.update(x=W * 0.42 - (x0 + x1) / 2 * scale_factor(sh.scale), y=H * 0.58 - (y0 + y1) / 2 *
                     scale_factor(sh.scale), scale=scale_factor(sh.scale))
            sh.views.append(v)
        elif views:
            placed = dv.standard_views(self, dr, sh, tuple(views), objects=list(objects or []))
            multi = len([o for o in self.objects if self.result.bodies.get(o.name) is not None]) > 1
            if placed and (bom if bom is not None else multi):
                iso = next((v for v in placed if v["dir"] in ("iso", "dimetric", "trimetric")), placed[0])
                iso["balloons"] = True
                iso["bom"] = True
        self.drawings.append(dr)
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return dr

    def add_drawing_view(self, drawing: str | None = None, kind: str = "projected", dir: str = "front",
                         x: float | None = None, y: float | None = None, scale: float | None = None,
                         parent: str | None = None, line=None, flip: bool = False, center=None, radius=None,
                         label: str = "", objects=None, sheet: str | None = None, exploded=False, hidden=None,
                         feature: str | None = None, breaks=None):
        """Add a view: projected/iso (dir), section (line [[x,y],[x,y]] in the parent view), detail
        (center [x,y] + radius in the parent view) or flat (sheet-metal feature)."""
        from ..drawing import views as dv
        from ..drawing.model import VIEW_DIRS, scale_factor
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        if kind not in ("projected", "base", "iso", "section", "detail", "flat", "auxiliary"):
            raise FeatureError("kind: projected | base | iso | section | detail | flat | auxiliary")
        byid = {v["id"]: v for v in sh.views}
        bylabel = {v.get("label") or v["id"]: v for v in sh.views}
        par = None
        if parent:
            par = byid.get(parent) or bylabel.get(parent) or next((v for v in sh.views if v.get("dir") == parent),
                                                                 None)
            if par is None:
                raise FeatureError(f"No view '{parent}' on {sh.name}")
        elif kind in ("section", "detail", "auxiliary"):
            par = next((v for v in sh.views if v.get("kind") in ("base", "projected")), None)
            if par is None:
                raise FeatureError("Section/detail/auxiliary views need a parent view")
        if par is not None and "_prims" not in par:
            dv.regenerate(self, dr)
        W, H = sh.dims()
        s_sheet = scale_factor(sh.scale)
        v = {"id": dv.new_id(), "kind": kind, "objects": list(objects or (par or {}).get("objects", [])),
             "label": label, "exploded": exploded}
        if hidden is not None:
            v["hidden"] = hidden
        letters = [vv.get("section", {}).get("name") for vv in sh.views] + \
                  [vv.get("detail", {}).get("name") for vv in sh.views]
        nxt = next(ch for ch in "ABCDEFGHJKLMNPRSTUVWXYZ" if ch not in letters)
        if kind in ("projected", "base", "iso"):
            if dir not in VIEW_DIRS:
                raise FeatureError(f"dir must be one of {list(VIEW_DIRS)}")
            v["dir"] = dir
            if kind == "iso" or dir in ("iso", "dimetric", "trimetric"):
                v["kind"], v["hidden"] = "iso", v.get("hidden", False)
            if par is not None:
                v["parent"], v["center"] = par["id"], par.get("center")
        elif kind == "section":
            if not line or len(line) != 2:
                raise FeatureError("A section needs line=[[x1,y1],[x2,y2]] in the parent view's mm")
            d, u, r = (np.asarray(q, float) for q in par["_basis"])
            c = np.asarray(par["center"], float)
            a3 = c + r * line[0][0] + u * line[0][1]
            b3 = c + r * line[1][0] + u * line[1][1]
            L3 = b3 - a3
            if np.linalg.norm(L3) < 1e-9:
                raise FeatureError("Section line has zero length")
            n = np.cross(L3, d)
            n = n / np.linalg.norm(n) * (-1 if flip else 1)
            v.update(parent=par["id"], dir="custom", vector=list(n), up=list(d if abs(n @ d) < 0.99 else u),
                     section={"point": list(a3), "normal": list(n), "line": [list(line[0]), list(line[1])],
                              "name": nxt}, center=par.get("center"))
            v["up"] = list(np.cross(n, L3 / np.linalg.norm(L3)))
        elif kind == "auxiliary":
            if not line or len(line) != 2:
                raise FeatureError("An auxiliary view needs line=[[x1,y1],[x2,y2]] along an edge in the parent view")
            d, u, r = (np.asarray(q, float) for q in par["_basis"])
            e2 = np.asarray(line[1], float) - np.asarray(line[0], float)
            if np.linalg.norm(e2) < 1e-9:
                raise FeatureError("Auxiliary edge has zero length")
            e2 = e2 / np.linalg.norm(e2)
            n2 = np.array([-e2[1], e2[0]]) * (-1 if flip else 1)
            vec = r * n2[0] + u * n2[1]
            v.update(parent=par["id"], dir="custom", vector=list(vec), up=list(d), center=par.get("center"),
                     aux={"line": [list(line[0]), list(line[1])], "name": nxt})
            v["label"] = label or f"VIEW {nxt}"
        elif kind == "detail":
            if center is None or radius is None:
                raise FeatureError("A detail needs center=[x,y] and radius in the parent view's mm")
            v.update(parent=par["id"], detail={"c": list(center), "r": float(radius), "name": nxt})
            scale = scale or par.get("scale", s_sheet) * 2
        elif kind == "flat":
            if feature not in self.result.flats:
                raise FeatureError(f"'{feature}' isn't a built sheet-metal feature")
            v.update(feature=feature, label=label or "FLAT PATTERN")
        v["scale"] = float(scale or (par or {}).get("scale", s_sheet))
        v["_scale"] = v["scale"]
        if breaks:
            v["breaks"] = [dict(b) for b in breaks]
        self.checkpoint()
        sh.views.append(v)
        dv.regenerate(self, dr)
        if x is None or y is None:                  # first free spot on a coarse grid, right of / below the parent
            x0, y0, x1, y1 = v.get("_bbox", (0, 0, 0, 0))
            vw, vh = (x1 - x0) * v["scale"], (y1 - y0) * v["scale"]
            taken = []
            for o in sh.views:
                if o is v or "x" not in o:
                    continue
                a, b, c_, d_ = o.get("_bbox", (0, 0, 0, 0))
                taken.append((o["x"] + a * o["scale"] - 8, o["y"] + b * o["scale"] - 14, o["x"] + c_ * o["scale"] + 8,
                              o["y"] + d_ * o["scale"] + 8))
            taken.append((W - 200, 0, W, 60))             # title block
            best = None
            for gy in np.arange(H - 25 - vh / 2, 25 + vh / 2, -5.0):
                for gx in np.arange(25 + vw / 2, W - 25 - vw / 2, 5.0):
                    box = (gx - vw / 2, gy - vh / 2, gx + vw / 2, gy + vh / 2)
                    if all(box[2] < t[0] or box[0] > t[2] or box[3] < t[1] or box[1] > t[3] for t in taken):
                        best = (gx, gy)
                        break
                if best:
                    break
            best = best or (W * 0.7, H * 0.5)
            x = best[0] - (x0 + x1) / 2 * v["scale"]
            y = best[1] - (y0 + y1) / 2 * v["scale"]
        v["x"], v["y"] = float(x), float(y)
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return v

    def set_drawing(self, drawing: str | None = None, sheet: str | None = None, options: dict | None = None,
                    title: dict | None = None, size: str | None = None, landscape: bool | None = None,
                    scale=None, rename: str | None = None):
        from ..drawing import views as dv
        from ..drawing.model import SHEETS, Options
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        if options:
            bad = [k for k in options if k not in Options.__dataclass_fields__]
            if bad:
                raise FeatureError(f"Unknown drawing option(s) {bad}. Options: {list(Options.__dataclass_fields__)}")
        if size and size not in SHEETS:
            raise FeatureError(f"Sheet size must be one of {list(SHEETS)}")
        self.checkpoint()
        if options:
            for k, val in options.items():
                setattr(dr.options, k, type(getattr(dr.options, k))(val))
        if title:
            sh.title.update({k: str(v) for k, v in title.items()})
        if size:
            sh.size = size
        if landscape is not None:
            sh.landscape = bool(landscape)
        if scale:
            a, b = (scale if isinstance(scale, (list, tuple)) else [float(q) for q in str(scale).split(":")])
            old = sh.scale[0] / sh.scale[1]
            sh.scale = [a, b]
            for v in sh.views:
                if abs(v.get("scale", old) - old) < 1e-9 or v.get("kind") != "detail":
                    v["scale"] = a / b if v.get("kind") != "detail" else v["scale"] * (a / b) / old
        if rename:
            dr.name = rename
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return dr

    def set_drawing_view(self, view: str, drawing: str | None = None, sheet: str | None = None, breaks=None,
                         label: str | None = None, scale: float | None = None, x: float | None = None,
                         y: float | None = None, hidden: bool | None = None):
        """Change a view: breaks [{"axis": "x"|"y", "a": lo, "b": hi}] in view mm (broken view; [] = none),
        label, scale, position, hidden lines."""
        from ..drawing import views as dv
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        v = next((q for q in sh.views if q["id"] == view or (q.get("label") or "") == view or q.get("dir") == view),
                 None)
        if v is None:
            raise FeatureError(f"No view '{view}' on {sh.name}")
        self.checkpoint()
        if breaks is not None:
            for b in breaks:
                if b.get("axis", "x") not in ("x", "y") or "a" not in b or "b" not in b:
                    raise FeatureError("breaks: [{axis: x|y, a, b}]")
            v["breaks"] = [dict(b) for b in breaks]
        if label is not None:
            v["label"] = label
        if scale:
            v["scale"] = float(scale)
        if x is not None:
            v["x"] = float(x)
        if y is not None:
            v["y"] = float(y)
        if hidden is not None:
            v["hidden"] = bool(hidden)
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return v

    def add_revision(self, description: str, drawing: str | None = None, sheet: str | None = None,
                     rev: str | None = None, date: str | None = None, approved: str = "", zone: str = "") -> dict:
        """Add a row to the sheet's revision table (rev letter auto-increments); the title block REV follows."""
        import datetime as _dt

        from ..drawing.annot import next_revision
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        self.checkpoint()
        row = {"rev": rev or next_revision(sh.revisions), "description": str(description),
               "date": date or _dt.date.today().isoformat(), "approved": approved, "zone": zone}
        sh.revisions.append(row)
        self.dirty = True
        self._emit()
        return row

    def add_ordinate_dims(self, origin, points, axis: str = "x", drawing: str | None = None,
                          sheet: str | None = None, offset: float = 10.0) -> list[dict]:
        """Ordinate dimensions (sheet mm) from an origin point to each point, aligned on one line."""
        from ..drawing.annot import ordinate_set
        from ..drawing.model import scale_factor
        if axis not in ("x", "y"):
            raise FeatureError("axis: x | y")
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        items = ordinate_set(origin, points, axis, offset, unit=1.0 / scale_factor(sh.scale) if sh.dims()[0] else None)
        self.checkpoint()
        sh.items.extend(items)
        self.dirty = True
        self._emit()
        return items

    def add_drawing_item(self, item: dict, drawing: str | None = None, sheet: str | None = None):
        """A 2D entity in sheet mm (see daedalus.drawing.model for the primitive schema)."""
        if item.get("t") not in ("line", "poly", "circle", "arc", "text", "hatch", "dim", "balloon", "gdt", "datum",
                                 "finish"):
            raise FeatureError("item t: line | poly | circle | arc | text | hatch | dim | balloon | gdt | datum | "
                               "finish")
        if item.get("t") == "gdt":
            from ..drawing.annot import GDT
            if item.get("symbol", "position") not in GDT:
                raise FeatureError(f"GD&T symbol must be one of {list(GDT)}")
        dr = self.drawing(drawing)
        self.checkpoint()
        dr.sheet(sheet).items.append(dict(item))
        self.dirty = True
        self._emit()
        return item

    def delete_drawing(self, name: str):
        dr = self.drawing(name)
        self.checkpoint()
        self.drawings.remove(dr)
        self._emit()

    def regenerate_drawings(self):
        from ..drawing import views as dv
        for dr in self.drawings:
            if dr.linked:
                dv.regenerate(self, dr)

    def export_drawing(self, path: str | Path, drawing: str | None = None, sheet: str | None = None, **kw) -> str:
        """PDF (all sheets), SVG, PNG, DXF or DWG (DWG needs the ODA File Converter)."""
        from ..drawing import dxfio
        from ..drawing import views as dv
        dr = self.drawing(drawing)
        if dr.linked:
            dv.regenerate(self, dr)
        try:
            return dxfio.save_2d(self, dr, path, dr.sheet(sheet) if sheet else None, **kw)
        except dxfio.DwgUnavailable as e:
            raise FeatureError(str(e)) from e
        except ValueError as e:
            raise FeatureError(str(e)) from e

    def open_2d(self, path: str | Path):
        """Open a DXF/DWG/SVG/HPGL file as an editable 2D drawing."""
        from ..drawing import dxfio
        try:
            dr = dxfio.read_2d(path)
        except dxfio.DwgUnavailable as e:
            raise FeatureError(str(e)) from e
        except ValueError as e:
            raise FeatureError(str(e)) from e
        base, i = dr.name, 2
        while any(d.name == dr.name for d in self.drawings):
            dr.name = f"{base} ({i})"
            i += 1
        self.checkpoint()
        self.drawings.append(dr)
        self.dirty = True
        self._emit()
        return dr

