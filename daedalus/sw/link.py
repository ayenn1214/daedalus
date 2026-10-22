"""SolidWorks ⇄ Daedalus subsystems.

SolidWorks keeps the 3D modelling. Link adds, on the open SolidWorks document:
  BOM & inventory   the assembly's BOM → Holocron (project allocation, stock check, shortfalls); inventory ids and
                    part numbers written back to SolidWorks custom properties
  library           your Daedalus standard parts and the ISO/ASME fastener library inserted into the SolidWorks
                    assembly (converted to native parts once, cached), mated to the selected hole/face;
                    the active SolidWorks part saved to the standard library
  round trip        the active document → Daedalus (geometry, names, mates, properties) for simulation, CAM,
                    drawings, physics; a Daedalus document → SolidWorks as STEP
  commands          views, rebuild, save, export, macros, SolidWorks command ids (for the δ hotbar)
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .backend import (ASSEMBLY, DESC_KEYS, HOLOCRON_KEY, PART, PN_KEYS, STD_KEY, Backend, SWError,
                      first_prop)

COST_KEYS = ("Cost", "UnitCost", "Unit Cost", "Price")
VIRTUAL = ("fusion:", "rhino:")          # component keys of programs whose parts have no files


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "-", name).strip() or "part"


def _base_name(c) -> str:
    """Display name of a component's part: the file name (SolidWorks) or the component name (Fusion)."""
    p = str(c.path)
    if p.startswith(VIRTUAL):
        return str(c.name).split("+")[-1].rsplit(":", 1)[0]
    return Path(p).stem


def _is_file(path) -> bool:
    return bool(path) and not str(path).startswith(VIRTUAL) and Path(path).parent.is_dir()


class _BomDoc:
    """Duck-typed stand-in for a Daedalus Document so the inventory hand-off code works on SolidWorks BOMs."""

    def __init__(self, rows, path):
        self._rows = rows
        self.path = Path(path) if path else None
        self.features = []
        self.result = SimpleNamespace(cutlists={})

    def bom(self):
        return self._rows


class SWLink:
    def __init__(self, backend: Backend, cache_dir: str | Path | None = None, library=None, store=None):
        self.b = backend
        self.cache = Path(cache_dir or Path.home() / ".daedalus" / "sw-cache")
        self._library = library
        self._store = store

    # ------------------------------------------------------------------ deps
    @property
    def library(self):
        if self._library is None:
            from ..stdparts import get_library
            self._library = get_library()
        return self._library

    @property
    def store(self):
        if self._store is None:
            from ..inventory.store import get_store
            self._store = get_store()
        return self._store

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        try:
            ok = self.b.connected()
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return {"connected": False}
        doc = self.b.active_doc()
        return {"connected": True, "app": self.b.app_info(), "document": doc.to_dict() if doc else None}

    def _active(self, kinds=(PART, ASSEMBLY)):
        d = self.b.active_doc()
        if d is None:
            raise SWError("Open a part or assembly in SolidWorks first")
        if d.kind not in kinds:
            raise SWError("Make a " + " or ".join({PART: "part", ASSEMBLY: "assembly"}[k] for k in kinds)
                          + " the active SolidWorks document")
        return d

    # ------------------------------------------------------------------ BOM
    def bom(self, include_subassemblies=False) -> list[dict]:
        """Parts list of the active assembly (or the part itself), Daedalus BOM row format."""
        d = self._active()
        if d.kind == PART:
            comps = [SimpleNamespace(name=Path(d.path).stem, path=d.path, configuration=d.configuration,
                                     suppressed=False, properties=d.properties, material=d.material,
                                     mass_kg=d.mass_kg, is_assembly=False)]
        else:
            comps = [c for c in self.b.components() if not c.suppressed
                     and (include_subassemblies or not c.is_assembly)]
        rows: dict = {}
        for c in comps:
            key = (str(c.path).lower(), c.configuration or "")
            props = c.properties or {}
            pn = first_prop(props, PN_KEYS)
            base = _base_name(c)
            r = rows.get(key)
            if r is None:
                r = rows[key] = {
                    "part": pn or base, "description": first_prop(props, DESC_KEYS) or base,
                    "material": c.material or "", "qty": 0, "unit_mass_kg": round(float(c.mass_kg or 0), 4),
                    "unit_cost": round(_num(first_prop(props, COST_KEYS)), 2), "objects": [], "file": str(c.path),
                    "configuration": c.configuration or "", "part_no": pn}
                std = first_prop(props, (STD_KEY,))
                if std:
                    r["standard"] = {"id": std, "name": r["description"], "kind": None}
                inv = first_prop(props, (HOLOCRON_KEY,))
                if inv:
                    r["inventory_id"] = inv
            r["qty"] += 1
            r["objects"].append(c.name)
        out = sorted(rows.values(), key=lambda r: r["part"].lower())
        for i, r in enumerate(out, start=1):
            r["item"] = i
            r["total_mass_kg"] = round(r["unit_mass_kg"] * r["qty"], 4)
            r["total_cost"] = round(r["unit_cost"] * r["qty"], 2)
        return out

    def _bomdoc(self):
        d = self._active()
        return _BomDoc(self.bom(), d.path)

    def stock_report(self, units: float = 1.0) -> list[dict]:
        from ..inventory.cad import stock_report
        return stock_report(self.store.inv, self._bomdoc(), units)

    def send_bom(self, project: str, units: float | None = None, write_back: bool = True) -> dict:
        """BOM → Holocron project (new parts created, existing ones linked); then HolocronId (+ PartNo when the
        SolidWorks part has none) written back to each part's custom properties."""
        from ..inventory.cad import send_bom
        doc = self._bomdoc()
        rows = doc.bom()
        rep = send_bom(self.store.inv, doc, project, units=units, weldments=False)
        self.store.save()
        written, failed = 0, []
        if write_back:
            inv = self.store.inv
            for row, r in zip(rows, rep["rows"]):
                p = inv.part(r["id"])
                props = {HOLOCRON_KEY: r["id"]}
                if not row.get("part_no") and p and p.get("partNumber"):
                    props["PartNo"] = p["partNumber"]
                try:
                    self.b.set_properties(props, path=row["file"])
                    written += 1
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{Path(row['file']).name}: {e}")
        rep["written_back"] = written
        rep["write_failed"] = failed
        return rep

    # ------------------------------------------------------------------ properties
    def properties(self, path=None) -> dict:
        return self.b.get_properties(path)

    def set_properties(self, props: dict, path=None):
        self.b.set_properties(props, path)

    # ------------------------------------------------------------------ library → SolidWorks
    def _native(self, step: Path, target: Path) -> str:
        if not self.b.native_suffix:                       # e.g. Fusion: imports STEP directly
            return str(step)
        target = target.with_suffix(self.b.native_suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_mtime >= step.stat().st_mtime:
            return str(target)
        return self.b.import_step(str(step), str(target))

    def _place(self, native: str, props: dict, mate: bool) -> dict:
        sel = [s for s in self.b.selection() if s.get("surface") in ("cylinder", "plane")] if mate else []
        at = sel[0].get("point_mm", (0, 0, 0)) if sel else (0, 0, 0)
        name = self.b.insert_component(native, at)
        try:
            self.b.set_properties(props, path=native)
        except Exception:  # noqa: BLE001
            pass
        mates, note = [], ""
        if sel:
            try:
                mates = self.b.mate_to_selection(name, sel[0])
            except Exception as e:  # noqa: BLE001
                note = f"inserted but not mated: {e}"
        elif mate:
            note = "select a hole or face in SolidWorks first to mate it automatically"
        return {"component": name, "file": native, "mates": mates, "note": note}

    def insert_fastener(self, kind: str, size: str, length_mm: float = 0.0, mate: bool = True) -> dict:
        from build123d import export_step

        from ..core import standards as S
        desig = S.designation(kind, size, length_mm)
        base = self.cache / "fasteners" / _safe(desig)
        step = base.with_suffix(".step")
        if not step.exists():
            step.parent.mkdir(parents=True, exist_ok=True)
            export_step(S.model(kind, size, length_mm), str(step))
        native = self._native(step, base.with_suffix(".SLDPRT"))
        props = {"PartNo": desig, "Description": f"{kind.title()} {size}" + (f" x {length_mm:g}" if length_mm else ""),
                 "Standard": S.KINDS.get(kind, "")}
        return {"designation": desig, **self._place(native, props, mate)}

    def insert_std(self, pid: str, mate: bool = True) -> dict:
        lib = self.library
        info = lib.find(pid)
        native = info.get(self.b.native_key) if self.b.native_key else None
        if not native or not Path(native).exists():
            from ..core.document import Document
            src = Path(info["path"])
            folder = src.parent / "solidworks"
            step = folder / "part.step"
            if not step.exists() or step.stat().st_mtime < src.stat().st_mtime:
                folder.mkdir(parents=True, exist_ok=True)
                Document.open(str(src)).export(str(step))
            native = self._native(step, folder / f"{_safe(info['name'])}.SLDPRT")
        props = {STD_KEY: info["id"], "PartNo": info.get("part_number") or info["name"],
                 "Description": info.get("description") or info["name"]}
        if info.get("vendor"):
            props["Vendor"] = info["vendor"]
        return {"name": info["name"], **self._place(native, props, mate)}

    def save_active_to_std(self, info: dict) -> dict:
        """The active SolidWorks part → standard library (geometry via STEP, properties carried over); the
        native SolidWorks file is remembered so inserting it back into SolidWorks uses the original."""
        d = self._active((PART,))
        info = dict(info)
        info.setdefault("name", Path(d.path).stem if _is_file(d.path) else d.title)
        info.setdefault("part_number", first_prop(d.properties, PN_KEYS))
        info.setdefault("description", first_prop(d.properties, DESC_KEYS))
        tmp = Path(tempfile.mkdtemp(prefix="sw-std-"))
        step = Path(self.b.export(str(tmp / (_safe(info["name"]) + ".step"))))
        saved = self.library.add_file(step, info)
        if d.path and self.b.native_key and Path(d.path).exists():
            saved = self.library.update_info(saved["id"], {self.b.native_key: d.path})
        try:
            self.b.set_properties({STD_KEY: saved["id"]})
        except Exception:  # noqa: BLE001
            pass
        return saved

    # ------------------------------------------------------------------ round trip
    def to_daedalus(self, out_dir=None) -> str:
        """Export the active document for Daedalus: returns a path Daedalus opens (STEP + mates + properties
        sit next to a marker named like the SolidWorks file, which the SolidWorks importer reads)."""
        d = self._active()
        out = Path(out_dir or tempfile.mkdtemp(prefix="sw-to-daedalus-"))
        out.mkdir(parents=True, exist_ok=True)
        if self.b.app != "solidworks":                    # Fusion: STEP keeps names, colours, placements
            return self.b.export(str(out / (_safe(Path(d.title).stem) + ".step")))
        marker = out / Path(d.path or d.title).name
        if marker.suffix.lower() not in (".sldprt", ".sldasm"):
            marker = marker.with_suffix(".SLDASM" if d.kind == ASSEMBLY else ".SLDPRT")
        marker.write_text("")                      # older than the exports → they count as fresh
        import os
        import time
        t = time.time() - 10
        os.utime(marker, (t, t))
        step, side, props = self.b.export_with_mates(str(out))
        want = out / (marker.stem + ".step")
        if Path(step) != want:
            Path(step).replace(want)
        return str(marker)

    def from_daedalus(self, doc_or_path) -> str:
        """A Daedalus document (or .daedalus file) → STEP → opened in SolidWorks."""
        from ..core.document import Document
        doc = Document.open(str(doc_or_path)) if isinstance(doc_or_path, (str, Path)) else doc_or_path
        name = Path(doc.path).stem if getattr(doc, "path", None) else "Daedalus part"
        out = Path(tempfile.mkdtemp(prefix="daedalus-to-sw-")) / f"{_safe(name)}.step"
        doc.export(str(out))
        self.b.open_doc(str(out))
        return str(out)

    # ------------------------------------------------------------------ CAD ⇄ CAD
    def transfer_to(self, other: "SWLink", as_component: bool = True) -> dict:
        """The active document here → the other CAD program (STEP), with its custom properties. Into the
        other side's active assembly when there is one, else opened as a new document there."""
        d = self._active()
        tmp = Path(tempfile.mkdtemp(prefix="cad-transfer-"))
        stem = _safe(Path(d.path).stem if _is_file(d.path) else d.title)
        step = Path(self.b.export(str(tmp / f"{stem}.step")))
        props = dict(d.properties)
        td = other.b.active_doc()
        if as_component and td is not None and td.kind == ASSEMBLY:
            native = other._native(step, other.cache / "transfer" / f"{stem}.part")
            name = other.b.insert_component(native)
            try:
                other.b.set_properties(props, path=native)
            except Exception:  # noqa: BLE001
                pass
            return {"mode": "inserted", "component": name, "into": td.title, "from": d.title}
        nd = other.b.open_doc(str(step))
        try:
            other.b.set_properties(props)
        except Exception:  # noqa: BLE001
            pass
        return {"mode": "opened", "title": nd.title if nd else stem, "from": d.title}

    # ------------------------------------------------------------------ commands
    def command(self, action: str, arg=None):
        """Hotbar / copilot actions on the active SolidWorks document."""
        if action == "view":
            return self.b.view(arg)
        if action == "fit":
            return self.b.zoom_fit()
        if action == "rebuild":
            return self.b.rebuild()
        if action == "save":
            return self.b.save()
        if action == "macro":
            return self.b.run_macro(arg)
        if action == "command":
            try:
                return self.b.run_command(int(arg))
            except (TypeError, ValueError):                # Fusion / Rhino / Inventor commands are names
                return self.b.run_command(str(arg))
        if action == "export":
            d = self._active((PART, ASSEMBLY)) if str(arg).lower() not in ("pdf", "dxf") else self.b.active_doc()
            if d is None:
                raise SWError("Open a document in SolidWorks first")
            if _is_file(d.path):
                base = Path(d.path)
            else:
                (self.cache / "exports").mkdir(parents=True, exist_ok=True)
                base = self.cache / "exports" / _safe(Path(d.title).stem)
            return self.b.export(str(base.with_suffix("." + str(arg).lower())))
        raise SWError(f"Unknown action {action}")
