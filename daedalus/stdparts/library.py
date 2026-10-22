"""Standard parts library: parts saved once, pulled into any project.

The library is a folder (local, or on a shared drive for a team):

    <library>/<id>/part.daedalus   the part — full feature history when it was modelled in Daedalus,
                                   or the imported solid for STEP/IGES/STL sources
    <library>/<id>/info.json       part number, name, category, kind, description, vendor, cost, …
    <library>/<id>/thumb.png       preview

Inserting links the part file by default, so editing the library part updates every project that uses it
(on open, or Update links). Inserted objects carry the part number and description into the BOM and the
inventory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

KINDS = ["purchased", "manufactured", "fastener", "hardware", "electrical", "other"]
FIELDS = ["name", "part_number", "category", "kind", "description", "material", "vendor", "vendor_part_number",
          "unit_cost", "currency", "url", "tags", "notes", "sw_path", "fusion_path"]
SOURCE_EXT = (".daedalus", ".strata", ".step", ".stp", ".iges", ".igs", ".brep", ".stl", ".obj", ".3mf",
              ".sldprt", ".sldasm")


class LibraryError(ValueError):
    pass


def default_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Daedalus" / "standard-parts"


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()).strip("-._")
    return s[:48] or "part"


def _obj_name(s: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_]+", "_", (s or "").strip()).strip("_")
    if not n or not n[0].isalpha():
        n = "P_" + n
    return n[:40]


class StandardLibrary:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_root()
        self._loaded: dict = {}               # file → (mtime, document): read a STEP once for check + add

    # ------------------------------------------------------------------ reading
    def _dirs(self):
        if not self.root.exists():
            return []
        return [d for d in sorted(self.root.iterdir()) if d.is_dir() and not d.name.startswith(".")
                and (d / "info.json").exists() and (d / "part.daedalus").exists()]

    def _read(self, d: Path) -> dict | None:
        try:
            info = json.loads((d / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        info["id"] = d.name
        info["path"] = str(d / "part.daedalus")
        info["thumb"] = str(d / "thumb.png") if (d / "thumb.png").exists() else ""
        return info

    def parts(self, query: str = "", category: str | None = None, kind: str | None = None) -> list[dict]:
        q = (query or "").lower().split()
        out = []
        for d in self._dirs():
            info = self._read(d)
            if info is None:
                continue
            if category and category != "All" and (info.get("category") or "") != category:
                continue
            if kind and kind != "All" and (info.get("kind") or "") != kind:
                continue
            hay = " ".join(str(info.get(k) or "") for k in ("name", "part_number", "category", "description",
                                                              "vendor", "vendor_part_number", "material", "tags",
                                                              "notes", "kind")).lower()
            if all(t in hay for t in q):
                out.append(info)
        return sorted(out, key=lambda i: ((i.get("category") or "~").lower(), (i.get("name") or "").lower()))

    def get(self, pid: str) -> dict:
        d = self.root / pid
        info = self._read(d) if (d / "info.json").exists() else None
        if info is None:
            raise LibraryError(f"No standard part '{pid}' in {self.root}")
        return info

    def find(self, key: str) -> dict:
        """By id, part number or name (case-insensitive)."""
        k = (key or "").strip().lower()
        if (self.root / key).is_dir():
            return self.get(key)
        for i in self.parts():
            if k in ((i.get("part_number") or "").lower(), (i.get("name") or "").lower()):
                return i
        raise LibraryError(f"No standard part '{key}'")

    def by_part_number(self, pn: str) -> dict | None:
        pn = (pn or "").strip().lower()
        if not pn:
            return None
        return next((i for i in self.parts() if (i.get("part_number") or "").strip().lower() == pn), None)

    def categories(self) -> list[str]:
        return sorted({i.get("category") or "" for i in self.parts()} - {""}, key=str.lower)

    # ------------------------------------------------------------------ file info
    @staticmethod
    def file_info(path) -> dict:
        """Part number, name, description, material, vendor … read from the file (see metadata.py), with the
        material matched to the material library. Notes are keys starting with '_'."""
        from ..core import materials as matlib
        from .metadata import match_material, read
        meta = read(path)
        if meta.get("material"):
            hit = match_material(meta["material"], [m.name for m in matlib.LIBRARY])
            meta["_material_text"] = meta["material"]
            if hit:
                meta["material"] = hit
            else:
                meta.pop("material")
        return meta

    @staticmethod
    def _set_material(part, info):
        """Use the chosen material for the part's mass (free text that doesn't match the library is kept as
        information only)."""
        from ..core import materials as matlib
        from .metadata import match_material
        m = info.get("material")
        if not m:
            return
        hit = match_material(m, [x.name for x in part.all_materials()]) or (
            m if m in [x.name for x in matlib.LIBRARY] else None)
        if hit:
            part.material = hit
            for o in part.objects:
                o.material = None
            info["material"] = hit
            part.rebuild()

    # ------------------------------------------------------------------ writing
    def _new_dir(self, info: dict) -> Path:
        base = _slug(info.get("part_number") or info.get("name"))
        d = self.root / base
        if d.exists():
            d = self.root / f"{base}-{uuid.uuid4().hex[:5]}"
        d.mkdir(parents=True)
        return d

    def _clean(self, info: dict) -> dict:
        name = (info.get("name") or "").strip()
        if not name:
            raise LibraryError("Give the standard part a name")
        out = {k: info.get(k) for k in FIELDS if info.get(k) not in (None, "", [])}
        out["name"] = name
        out["kind"] = out.get("kind") if out.get("kind") in KINDS else "purchased"
        if "unit_cost" in out:
            try:
                out["unit_cost"] = float(out["unit_cost"])
            except (TypeError, ValueError):
                out.pop("unit_cost")
        if isinstance(out.get("tags"), str):
            out["tags"] = [t.strip() for t in out["tags"].split(",") if t.strip()]
        return out

    def _write(self, d: Path, part_doc, info: dict, existing: dict | None = None, source: str = ""):
        from ..core.document import FeatureError
        body = None
        try:
            bodies = part_doc.placed_bodies()
            body = next(iter(bodies.values()), None)
        except FeatureError:
            pass
        if body is None:
            raise LibraryError("The part has no solid body")
        tmp = d / "part.daedalus.tmp"
        part_doc.save(tmp)
        os.replace(tmp, d / "part.daedalus")
        now = int(time.time())
        bb = body.bounding_box()
        meta = {**(existing or {}), **info}
        for k in ("id", "path", "thumb"):
            meta.pop(k, None)
        meta.update({"version": int((existing or {}).get("version", 0)) + 1, "updated": now,
                     "created": (existing or {}).get("created", now),
                     "volume_mm3": round(float(body.volume), 3),
                     "mass_kg": round(float(body.volume) * part_doc.material_of(None).density / 1e6, 5),
                     "size_mm": [round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)],
                     "material": info.get("material") or part_doc.material_of(None).name})
        if source:
            meta["source"] = source
        try:
            from .similarity import fingerprint
            meta["signature"] = fingerprint(list(part_doc.placed_bodies().values()))
        except Exception:  # noqa: BLE001
            meta.pop("signature", None)
        (d / "info.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        try:
            from .thumbs import render_png
            render_png(list(part_doc.placed_bodies().values()), d / "thumb.png")
        except Exception:  # noqa: BLE001  (no OpenGL: library works without previews)
            pass
        return self._read(d)

    def add_object(self, doc, obj: str, info: dict, replace: str | None = None) -> dict:
        """Save one object of a document as a standard part (replace = id of a part to overwrite)."""
        info = self._clean({"material": doc.material_of(obj).name, **info})
        part = object_document(doc, obj, _obj_name(info["name"]))
        self._set_material(part, info)
        existing = self.get(replace) if replace else None
        return self._commit(part, info, existing, f"{Path(doc.path).name if doc.path else 'Untitled'} · {obj}")

    def _commit(self, part, info, existing, source):
        d = Path(existing["path"]).parent if existing else self._new_dir(info)
        try:
            return self._write(d, part, info, existing, source=source)
        except Exception:
            if not existing:
                shutil.rmtree(d, ignore_errors=True)
            raise

    def add_file(self, path: str | Path, info: dict | None = None, replace: str | None = None) -> dict:
        """STEP / IGES / BREP / STL / OBJ / 3MF / .daedalus file → standard part."""
        from ..core.document import Document, _load_any
        path = Path(path)
        if path.suffix.lower() not in SOURCE_EXT:
            raise LibraryError(f"Can't add {path.suffix} files (use {', '.join(SOURCE_EXT)})")
        found = self.file_info(path)
        from .metadata import suggestions
        info = self._clean({**suggestions(found), **{k: v for k, v in (info or {}).items() if v not in (None, "")}})
        src = self._load(path)
        bodies = src.placed_bodies()
        if not bodies:
            raise LibraryError(f"{path.name} has no solid bodies")
        if len(src.objects) > 1 or path.suffix.lower() not in (".daedalus", ".strata"):
            part = _solid_document(list(bodies.values()), _obj_name(info["name"]), src.material_of(None).name,
                                   source=path.name)
        else:
            part = src
            part.path = None
        self._set_material(part, info)
        existing = self.get(replace) if replace else None
        return self._commit(part, info, existing, path.name)

    def _load(self, path):
        from ..core.document import _load_any
        path = Path(path)
        key, mt = str(path.resolve()), path.stat().st_mtime
        hit = self._loaded.get(key)
        if hit and hit[0] == mt:
            return hit[1]
        doc = _load_any(path)
        self._loaded = {key: (mt, doc)}
        return doc

    # ------------------------------------------------------------------ similarity
    def signature(self, info: dict) -> dict | None:
        """Fingerprint of a library part (computed and stored for parts saved before fingerprints existed)."""
        if info.get("signature"):
            return info["signature"]
        from ..core.document import Document
        from .similarity import fingerprint
        try:
            part = Document.open(info["path"])
            sig = fingerprint(list(part.placed_bodies().values()))
        except Exception:  # noqa: BLE001
            return None
        f = Path(info["path"]).parent / "info.json"
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            meta["signature"] = sig
            f.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            pass
        info["signature"] = sig
        return sig

    def similar(self, bodies=None, info: dict | None = None, exclude: str | None = None,
                threshold: float = 0.8, limit: int = 5) -> list[dict]:
        """Library parts that look like these bodies and/or this part info, best first:
        [{**part info, "match": {score, geometry, text, level, reasons}}]."""
        from .similarity import compare, fingerprint
        fp = None
        if bodies is not None:
            try:
                fp = fingerprint(bodies)
            except Exception:  # noqa: BLE001
                fp = None
        out = []
        for other in self.parts():
            if other["id"] == exclude:
                continue
            if fp is not None:
                self.signature(other)
            m = compare(fp, info or {}, other)
            if m["score"] >= threshold and m["level"]:
                out.append({**other, "match": m})
        return sorted(out, key=lambda x: -x["match"]["score"])[:limit]

    def similar_to_object(self, doc, obj: str, info: dict | None = None, **kw) -> list[dict]:
        body = doc.result.bodies.get(obj)
        return self.similar([body] if body is not None else None, info, **kw)

    def similar_to_file(self, path, info: dict | None = None, **kw) -> list[dict]:
        try:
            bodies = list(self._load(path).placed_bodies().values()) or None
        except Exception:  # noqa: BLE001
            bodies = None
        return self.similar(bodies, info, **kw)

    def duplicates(self, threshold: float = 0.985) -> list[list[dict]]:
        """Groups of library parts with identical geometry or the same part number."""
        parts = self.parts()
        for p in parts:
            self.signature(p)
        from .similarity import compare
        groups, seen = [], set()
        for i, a in enumerate(parts):
            if a["id"] in seen:
                continue
            g = [a]
            for b in parts[i + 1:]:
                if b["id"] not in seen:
                    m = compare(a.get("signature"), a, b)
                    if m["geometry"] >= threshold or m["text"] >= 0.99:
                        g.append(b)
                        seen.add(b["id"])
            if len(g) > 1:
                groups.append(g)
        return groups

    def update_info(self, pid: str, info: dict) -> dict:
        cur = self.get(pid)
        d = Path(cur["path"]).parent
        new = self._clean({**cur, **info})
        meta = {**cur, **new, "updated": int(time.time())}
        for k in ("id", "path", "thumb"):
            meta.pop(k, None)
        for k in FIELDS:
            if k in info and info[k] in (None, ""):
                meta.pop(k, None)
        meta["name"] = new["name"]
        (d / "info.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return self._read(d)

    def refresh_from_file(self, pid: str) -> dict:
        """Re-read size/mass/thumbnail after the part file was edited and saved in Daedalus."""
        from ..core.document import Document
        cur = self.get(pid)
        d = Path(cur["path"]).parent
        part = Document.open(cur["path"])
        info = {k: cur[k] for k in FIELDS if k in cur}
        return self._write(d, part, info, cur)

    def remove(self, pid: str):
        """Moves the part to <library>/.trash (projects with a linked copy keep their embedded geometry)."""
        d = self.root / pid
        if not d.is_dir():
            raise LibraryError(f"No standard part '{pid}'")
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(d), str(trash / f"{pid}-{int(time.time())}"))

    # ------------------------------------------------------------------ using
    def insert(self, doc, pid: str, link: bool = True, at=None, checkpoint=True) -> str:
        """Pull a standard part into a document as a new object. Returns the object name."""
        info = self.find(pid)
        if checkpoint:
            doc.checkpoint()
        name = doc.insert_part_file(info["path"], link=link, checkpoint=False, name=_obj_name(info["name"]))
        o = doc.obj(name)
        for f in doc.features:
            if f.obj == name and f.TYPE == "import":
                f.params["source"] = f"{info['name']} (standard part)"
        o.part_no = info.get("part_number") or info["name"]
        o.description = info.get("description") or info["name"]
        o.unit_cost = info.get("unit_cost")
        o.std = {"id": info["id"], "name": info["name"], "kind": info.get("kind"), "vendor": info.get("vendor"),
                 "category": info.get("category"), "version": info.get("version"), "linked": bool(link)}
        if at is not None:
            o.pose = [float(at[0]), float(at[1]), float(at[2]), *o.pose[3:]]
        doc.dirty = True
        doc.rebuild()
        return name

    def usage(self, doc) -> list[dict]:
        """Standard parts in a document: [{object, id, name, linked, library_version, used_version}]."""
        out = []
        for o in doc.objects:
            if not o.std:
                continue
            try:
                cur = self.get(o.std["id"]).get("version")
            except LibraryError:
                cur = None
            out.append({"object": o.name, "id": o.std["id"], "name": o.std.get("name"),
                        "linked": o.std.get("linked"), "library_version": cur, "used_version": o.std.get("version")})
        return out


# ---------------------------------------------------------------------- helpers

def object_document(doc, obj: str, new_name: str | None = None):
    """A standalone document holding one object of `doc` with its full feature history (falls back to the
    solid when the history can't stand alone, e.g. it references another object)."""
    from ..core.document import Document
    d = doc.to_dict()
    feats = [f for f in d.get("features", []) if f.get("obj", d["objects"][0]["name"]) == obj]
    o = next(x for x in d["objects"] if x["name"] == obj)
    o = {**o, "pose": [0.0] * 6, "fixed": True, "visible": True, "part_no": None, "std": None,
         "description": None, "unit_cost": None}
    mat = doc.material_of(obj).name
    o["material"] = None
    part = Document()
    ok = False
    if feats:
        dd = {**d, "objects": [o], "active_object": obj, "features": feats, "mates": [], "studies": [],
              "drawings": [], "explode": {}, "cam": {}, "material": mat, "rollback": None,
              "imported_step": None}
        dd.pop("physics", None)
        try:
            part._load_dict(dd)
            part.rebuild()
            ok = bool(part.result.bodies.get(obj)) and not part.result.errors
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        body = doc.result.bodies.get(obj)
        if body is None:
            raise LibraryError(f"{obj} has no solid body")
        part = _solid_document([body], obj, mat, source=f"{obj}")
    if new_name and new_name != obj and not part.has_obj(new_name):
        part.set_object(obj, checkpoint=False, new_name=new_name)
    part.rebuild()
    part.path = None
    part.dirty = False
    return part


def _solid_document(bodies, name, material, source=""):
    from build123d import Compound

    from ..core.assembly import Obj
    from ..core.document import Document
    from ..io.formats import shape_to_text, store_blob, to_part
    shape = to_part(Compound(bodies)) if len(bodies) > 1 else bodies[0]
    part = Document()
    part.objects = [Obj(name, fixed=True)]
    part.active_object = name
    part.material = material
    part.add("import", {"data": store_blob(shape_to_text(shape)), "source": source or name, "format": "brep",
                        "link": "", "mtime": 0}, name="Solid", rebuild=True, checkpoint=False, obj=name)
    return part
