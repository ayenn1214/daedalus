"""Drawing operations shared by the full Document and the light 2D document (drawings module on its own).
Needs from the host: drawings, dirty, checkpoint(), _emit(); model views also use objects, obj(), result."""
from __future__ import annotations

import math
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
                iso = next((v for v in placed if v["dir"] in ("iso", "dimetric", "trimetric", "oblique")), placed[0])
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
                         feature: str | None = None, breaks=None, section_type: str | None = None, path=None):
        """Add a view: projected/iso/oblique (dir), section (line [[x,y],[x,y]] in the parent view, or path for an
        offset / aligned section; section_type full | half | offset | aligned | removed | revolved), detail
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
            if kind == "iso" or dir in ("iso", "dimetric", "trimetric", "oblique"):
                v["kind"], v["hidden"] = "iso", v.get("hidden", False)
            if par is not None:
                v["parent"], v["center"] = par["id"], par.get("center")
        elif kind == "section":
            pts = [list(map(float, q)) for q in (path or line or [])]
            styp = section_type or ("offset" if len(pts) > 2 else "full")
            if styp not in ("full", "half", "offset", "aligned", "removed", "revolved"):
                raise FeatureError("section_type: full | half | offset | aligned | removed | revolved")
            if len(pts) < 2:
                raise FeatureError("A section needs line=[[x1,y1],[x2,y2]] (or path=[…] for offset / aligned) in the "
                                   "parent view's mm")
            if styp == "aligned" and len(pts) != 3:
                raise FeatureError("An aligned section needs path=[end A, vertex, end B] (two arms meeting at the "
                                   "centre)")
            if styp == "offset" and len(pts) < 3:
                raise FeatureError("An offset section needs path=[p1, p2, p3, …] (the cutting line, bent at 90°)")
            d, u, r = (np.asarray(q, float) for q in par["_basis"])
            c = np.asarray(par["center"], float)
            P3 = [c + r * q[0] + u * q[1] for q in pts]
            sec = {"name": nxt, "type": styp, "line": [pts[0], pts[-1]] if styp != "offset" else [pts[0], pts[1]]}
            if styp == "aligned":
                A, V, B = P3
                a_ = (A - V) / np.linalg.norm(A - V)
                b_ = (B - V) / np.linalg.norm(B - V)
                ang = math.degrees(math.atan2(np.dot(np.cross(b_, -a_), d), np.dot(b_, -a_)))
                w = a_ + b_
                w = w / np.linalg.norm(w) if np.linalg.norm(w) > 1e-9 else np.cross(d, a_)
                m = np.cross(d, w)
                m = m if np.dot(m, b_) > 0 else -m
                end = V - a_ * np.linalg.norm(B - V)
                L3 = end - A
                n = np.cross(L3, d)
                n = n / np.linalg.norm(n) * (-1 if flip else 1)
                sec.update(point=list(A), normal=list(n), path=pts, vertex=list(V), axis=list(d), rotate=float(ang),
                           bisector_normal=list(m))
            elif styp == "offset":
                L0 = P3[1] - P3[0]
                n = np.cross(L0, d)
                n = n / np.linalg.norm(n) * (-1 if flip else 1)
                planes = []
                for p_, q_ in zip(P3, P3[1:]):
                    nn = np.cross(q_ - p_, d)
                    if np.linalg.norm(nn) > 1e-9:
                        planes.append([list(p_), list(nn / np.linalg.norm(nn))])
                BIG = 1e4
                e0 = np.asarray(pts[0]) - (np.asarray(pts[1]) - pts[0]) / np.linalg.norm(np.subtract(pts[1], pts[0])) * BIG
                ek = np.asarray(pts[-1]) + (np.asarray(pts[-1]) - pts[-2]) / np.linalg.norm(np.subtract(pts[-1], pts[-2])) * BIG
                n2 = np.array([n @ r, n @ u])
                n2 = n2 / (np.linalg.norm(n2) or 1)
                poly2 = [e0, *[np.asarray(q) for q in pts], ek, ek - n2 * BIG, e0 - n2 * BIG]
                sec.update(point=list(P3[0]), normal=list(n), path=pts, planes=planes,
                           polygon3d=[list(c + r * q[0] + u * q[1]) for q in poly2], depth_dir=list(d))
                L3 = L0
            else:
                L3 = P3[1] - P3[0]
                if np.linalg.norm(L3) < 1e-9:
                    raise FeatureError("Section line has zero length")
                n = np.cross(L3, d)
                n = n / np.linalg.norm(n) * (-1 if flip else 1)
                sec.update(point=list(P3[0]), normal=list(n))
                if styp == "half":
                    sec.update(along=list(L3 / np.linalg.norm(L3)), split=list(c))
            look = -n
            lk = np.array([look @ r, look @ u])
            sec["look2d"] = list(lk / (np.linalg.norm(lk) or 1))
            v.update(parent=par["id"], dir="custom", vector=list(n), up=list(d if abs(n @ d) < 0.99 else u),
                     section=sec, center=par.get("center"))
            v["up"] = list(np.cross(n, L3 / np.linalg.norm(L3)))
            if styp == "revolved":
                v["up"] = list(L3 / np.linalg.norm(L3))
                v["annotate"] = False
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
        if kind == "section" and v["section"]["type"] == "revolved" and (x is None or y is None):
            a_, b_ = v["section"]["line"]
            mx, my = (a_[0] + b_[0]) / 2, (a_[1] + b_[1]) / 2
            x0, y0, x1, y1 = v.get("_bbox", (0, 0, 0, 0))
            x = par["x"] + mx * par["scale"] - (x0 + x1) / 2 * v["scale"]
            y = par["y"] + my * par["scale"] - (y0 + y1) / 2 * v["scale"]
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
            from ..drawing.practice import title_block_rect
            tb = title_block_rect(dr, sh)
            taken.append((tb[0] - 5, 0, W, tb[3] + 5))     # title block
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
                         y: float | None = None, hidden: bool | None = None, broken_out=None):
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
        if broken_out is not None:
            if broken_out and len(broken_out.get("region") or []) < 3:
                raise FeatureError("broken_out: {region: [[x,y], …] (≥3 points, view mm), depth: mm from the view "
                                   "centre towards you (0 = through the middle)}")
            if broken_out:
                v["broken_out"] = {"region": [list(map(float, q)) for q in broken_out["region"]],
                                   "depth": float(broken_out.get("depth", 0.0))}
            else:
                v.pop("broken_out", None)
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return v

    # ------------------------------------------------------------ working-drawing practice
    def add_drawing_note(self, text: str | None = None, drawing: str | None = None, sheet: str | None = None,
                         template: str | None = None, index: int | None = None) -> list:
        """A numbered note (lower left of the sheet): your text or a standard template (practice.NOTE_TEMPLATES)."""
        from ..drawing.practice import NOTE_TEMPLATES
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        if template:
            if template not in NOTE_TEMPLATES:
                raise FeatureError(f"template: one of {list(NOTE_TEMPLATES)}")
            text = text or NOTE_TEMPLATES[template]
        if not (text or "").strip():
            raise FeatureError("Give the note's text (or a template)")
        self.checkpoint()
        if index is None:
            sh.notes.append(text.strip())
        else:
            sh.notes.insert(int(index), text.strip())
        self.dirty = True
        self._emit()
        return sh.notes

    def set_drawing_notes(self, notes: list, drawing: str | None = None, sheet: str | None = None) -> list:
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        self.checkpoint()
        sh.notes = [str(n).strip() for n in notes if str(n).strip()]
        self.dirty = True
        self._emit()
        return sh.notes

    def set_numbering(self, **opts) -> dict:
        """Drawing-number format for the model (XXXXX.AA.BB…): base, separator, digits, start, step, fill,
        subassemblies_first, standard_parts, prefix, suffix, order, levels."""
        from ..drawing.practice import NUMBERING
        bad = [k for k in opts if k not in NUMBERING]
        if bad:
            raise FeatureError(f"Unknown numbering option(s) {bad}. Options: {list(NUMBERING)}")
        self.checkpoint()
        cur = dict(getattr(self, "numbering", None) or {})
        cur.update(opts)
        self.numbering = cur
        self.dirty = True
        self._emit()
        return cur

    def drawing_numbers(self, **opts) -> list:
        from ..drawing.practice import drawing_numbers
        o = dict(getattr(self, "numbering", None) or {})
        o.update(opts)
        return drawing_numbers(self, o)

    def apply_drawing_numbers(self) -> dict:
        """Write each drawing's number into its title block (from the model's numbering)."""
        from ..drawing.practice import number_for
        self.checkpoint()
        out = {}
        for dr in self.drawings:
            for sh in dr.sheets:
                objs = sorted({o for v in sh.views for o in (v.get("objects") or [])})
                num = number_for(self, objs, getattr(self, "numbering", None))
                if num:
                    sh.title["number"] = num
                    out[f"{dr.name}/{sh.name}"] = num
        self.dirty = True
        self._emit()
        return out

    def set_sheet_format(self, drawing: str | None = None, sheet: str | None = None, name: str | None = None,
                         custom: dict | None = None, save_as: str | None = None, size: str | None = None,
                         custom_size=None, all_sheets: bool = False) -> dict:
        """The sheet's frame, margins, zones and title block layout: a named format (built-in or saved), or a
        custom one ({margins [l,b,r,t], zones, zone_size, frame_weight, projection_symbol, labels,
        title_block {width, rows [{h, cells [[field, width], …]}]}}); save_as keeps it as a template."""
        from ..drawing import practice as P
        from ..drawing import views as dv
        dr = self.drawing(drawing)
        sheets = dr.sheets if all_sheets else [dr.sheet(sheet)]
        if name and name not in P.list_formats():
            raise FeatureError(f"No sheet format '{name}'. Formats: {list(P.list_formats())}")
        if custom:
            try:
                P.check_format({**P.FORMATS["ISO 5457 (filing margin)"], **custom})
            except ValueError as e:
                raise FeatureError(str(e)) from None
        if custom_size is not None:
            w, h = (float(q) for q in custom_size)
            if w < 50 or h < 50:
                raise FeatureError("custom_size: [width, height] mm, at least 50 × 50")
        self.checkpoint()
        for sh in sheets:
            if name:
                sh.format = {"name": name}
            if custom:
                sh.format = {"custom": dict(custom)}
            if custom_size is not None:
                sh.custom_size = [w, h]
                sh.size = "Custom"
            elif size:
                sh.size = size
        if save_as:
            fm = P.sheet_format(dr, sheets[0])
            P.save_format(save_as, fm)
        dv.regenerate(self, dr)
        self.dirty = True
        self._emit()
        return P.sheet_format(dr, sheets[0])

    def set_dimension_standard(self, standard: str, drawing: str | None = None) -> dict:
        """Dimension the drawing to a standard: ISO 129-1, ASME Y14.5 (inch / metric / dual), DIN 406, JIS,
        BS 8888 — text orientation, decimal sign, leading / trailing zeros, units, projection angle, sizes."""
        from ..drawing.practice import DIM_STANDARDS
        if standard not in DIM_STANDARDS:
            raise FeatureError(f"standard: one of {list(DIM_STANDARDS)}")
        return self.set_drawing(drawing, options={**DIM_STANDARDS[standard], "dim_standard": standard}).options.to_dict()

    def add_dimension_set(self, kind: str, points, axis: str = "x", at=None, step: float = 8.0,
                          drawing: str | None = None, sheet: str | None = None) -> list:
        """Chain (feature to feature) or baseline (all from one datum) dimensions through points, sheet mm."""
        from ..drawing.practice import dimension_set
        dr = self.drawing(drawing)
        sh = dr.sheet(sheet)
        try:
            dims = dimension_set(kind, points, axis, at, step)
        except ValueError as e:
            raise FeatureError(str(e)) from None
        self.checkpoint()
        for dm in dims:
            dm.setdefault("layer", "DIM")
            sh.items.append(dm)
        self.dirty = True
        self._emit()
        return dims

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

