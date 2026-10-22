"""Import hub: bring a part in from anywhere in Daedalus and keep it connected.

Sources
  file       any part file on disk (.daedalus, STEP, IGES, BREP, STL)
  project    other Daedalus projects (recent files and project folders)
  std        the standard-parts library (your saved parts)
  fastener   the ISO / ASME fastener and bearing library
  inventory  Holocron inventory parts that have CAD: a standard part, a fastener spec, or a CAD file in
             the part's drawing link

Connected means:
  * linked to its source (files, projects, standard parts): Links / open re-reads it when the source changes;
  * tagged with where it came from (object.source) so the BOM, inventory hand-off ("Send BOM") and the design
    tree can find the same part again;
  * optionally mated to the face you picked: a flat face → coincident with the part's bottom face; a hole or
    shaft → concentric (fasteners and bearings get the full head/nut/washer stack).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .features import FeatureError

PART_EXTS = (".daedalus", ".strata", ".step", ".stp", ".iges", ".igs", ".brep", ".stl")
SOURCES = {"file": "Files", "project": "Projects", "std": "Standard parts", "fastener": "Fasteners & bearings",
           "inventory": "Inventory"}


# ------------------------------------------------------------------ sources
def project_items(dirs=(), recent=(), exclude: str | None = None, limit: int = 300) -> list[dict]:
    """Daedalus projects from recent files plus *.daedalus files in the given folders (one level deep)."""
    seen, out = set(), []
    ex = str(Path(exclude).resolve()) if exclude else None

    def add(p: Path, tag):
        try:
            rp = str(p.resolve())
        except OSError:
            return
        if rp in seen or rp == ex or not p.is_file():
            return
        seen.add(rp)
        st = p.stat()
        out.append({"source": "project", "id": rp, "name": p.stem, "path": rp, "mtime": st.st_mtime,
                    "detail": f"{tag} · {p.parent.name}", "linkable": True})

    for r in recent:
        if r and str(r).lower().endswith((".daedalus", ".strata")):
            add(Path(r), "recent")
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        cands = list(d.glob("*.daedalus")) + list(d.glob("*/*.daedalus"))
        for p in sorted(cands, key=lambda q: q.stat().st_mtime if q.exists() else 0, reverse=True):
            add(p, "folder")
            if len(out) >= limit:
                break
    return out[:limit]


def std_items(query: str = "") -> list[dict]:
    try:
        from ..stdparts import get_library
        lib = get_library()
        parts = lib.parts(query)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in parts:
        bits = [p.get("part_number") or "", p.get("category") or "", p.get("vendor") or "",
                f"v{p.get('version', 1)}"]
        out.append({"source": "std", "id": p["id"], "name": p["name"], "detail": " · ".join(b for b in bits if b),
                    "path": p.get("path"), "linkable": True, "info": p})
    return out


def fastener_items(query: str = "") -> list[dict]:
    from . import standards as S
    out = []
    for kind, std in S.KINDS.items():
        sizes = list(S.BEARINGS) if kind == "ball bearing" else list(S.ALL)
        for size in sizes:
            name = S.designation(kind, size).split(" (")[0] if kind != "ball bearing" else S.designation(kind, size)
            out.append({"source": "fastener", "id": f"{kind}|{size}", "name": f"{kind.title()} {size}",
                        "detail": std, "kind": kind, "size": size, "linkable": False, "keys": name,
                        "needs_length": kind in ("socket head cap screw", "hex bolt", "flat head screw")})
    q = query.strip().lower()
    return [i for i in out if not q or all(t in (i["name"] + " " + i["detail"] + " " + i["keys"]).lower()
                                           for t in q.split())]


_SIZE_RE = re.compile(r'^(M\d+(?:\.\d+)?|#\d+|\d+/\d+"?|\d+"?)')


def _fastener_from_inventory(p: dict):
    """(kind, size, length_mm) for an inventory fastener, or None when it doesn't map to the library."""
    from . import standards as S
    cat, head = (p.get("category") or "").lower(), (p.get("headStyle") or "").lower()
    kind = None
    if cat == "screw" and "socket" in head:
        kind = "socket head cap screw"
    elif cat == "screw" and "flat" in head:
        kind = "flat head screw"
    elif cat == "bolt" and "hex" in head:
        kind = "hex bolt"
    elif cat == "nut":
        kind = "hex nut"
    elif cat == "washer":
        kind = "washer"
    if kind is None:
        return None
    size = (p.get("size") or "").strip()
    if size in S.ALL:
        lib_size = size
    else:                                       # '1/4"' → '1/4-20'
        head_sz = size.replace('"', "")
        lib_size = next((k for k in S.INCH if k.split("-")[0] == head_sz), None)
        if lib_size is None:
            return None
    length = 0.0
    if kind in ("socket head cap screw", "hex bolt", "flat head screw"):
        raw = str(p.get("length") or "").replace('"', "").replace("mm", "").strip()
        if not raw:
            return None
        try:
            if "/" in raw:
                whole, _, frac = raw.partition("-") if "-" in raw else ("0", "", raw)
                n, d = frac.split("/")
                val = float(whole or 0) + float(n) / float(d)
            else:
                val = float(raw)
        except ValueError:
            return None
        length = val if S.ALL[lib_size].system == "ISO" else val * S.IN
    return kind, lib_size, length


def inventory_items(query: str = "", inv=None) -> list[dict]:
    """Inventory parts that can be brought into CAD."""
    if inv is None:
        try:
            from ..inventory.store import get_store
            inv = get_store().inv
        except Exception:  # noqa: BLE001
            return []
    q = query.strip().lower()
    out = []
    for p in getattr(inv, "parts", []):
        text = " ".join(str(p.get(k) or "") for k in ("name", "partNumber", "category", "size", "notes")).lower()
        if q and not all(t in text for t in q.split()):
            continue
        how, extra = None, {}
        if p.get("stdPartId"):
            how, extra = "std", {"std_id": p["stdPartId"]}
        else:
            fx = _fastener_from_inventory(p) if p.get("type") == "fastener" else None
            if fx:
                how, extra = "fastener", {"kind": fx[0], "size": fx[1], "length": fx[2]}
            else:
                url = (p.get("drawingUrl") or "").strip()
                if url.startswith("file://"):
                    url = url[7:]
                if url and url.lower().endswith(PART_EXTS) and os.path.exists(url):
                    how, extra = "file", {"path": url}
        if how is None:
            continue
        stock = p.get("currentStock")
        out.append({"source": "inventory", "id": p["id"], "name": p.get("name") or p.get("partNumber") or "?",
                    "detail": " · ".join(b for b in (p.get("partNumber") or "", p.get("category") or "",
                                                       f"{stock:g} in stock" if isinstance(stock, (int, float)) else "",
                                                       {"std": "standard part", "fastener": "library fastener",
                                                        "file": "CAD file"}[how]) if b),
                    "via": how, "linkable": how != "fastener", "part": p, **extra})
    return out


def file_item(path: str) -> dict:
    p = Path(path)
    if p.suffix.lower() not in PART_EXTS:
        raise FeatureError(f"Can't import {p.suffix or 'that file'} — parts: {', '.join(PART_EXTS)}")
    return {"source": "file", "id": str(p.resolve()), "name": p.stem, "path": str(p.resolve()),
            "detail": p.suffix.lstrip(".").upper(), "linkable": True}


def search(query: str = "", sources=None, dirs=(), recent=(), exclude=None, inv=None) -> list[dict]:
    sources = set(sources or SOURCES)
    out = []
    if "project" in sources:
        q = query.strip().lower()
        out += [i for i in project_items(dirs, recent, exclude) if not q or q in i["name"].lower()]
    if "std" in sources:
        out += std_items(query)
    if "inventory" in sources:
        out += inventory_items(query, inv)
    if "fastener" in sources:
        out += fastener_items(query)
    return out


def resolve(source: str, ident: str, inv=None) -> dict:
    """An importable item from its source and id (as returned by search)."""
    if source == "file":
        return file_item(ident)
    if source == "project":
        p = Path(ident)
        if not p.is_file():
            raise FeatureError(f"Project not found: {ident}")
        return {"source": "project", "id": str(p.resolve()), "name": p.stem, "path": str(p.resolve()),
                "detail": "", "linkable": True}
    if source == "fastener":
        kind, _, size = ident.partition("|")
        hit = next((i for i in fastener_items() if i["kind"] == kind and i["size"] == size), None)
    elif source == "std":
        hit = next((i for i in std_items() if i["id"] == ident), None)
    elif source == "inventory":
        hit = next((i for i in inventory_items(inv=inv) if i["id"] == ident), None)
    else:
        raise FeatureError(f"Unknown import source '{source}'")
    if hit is None:
        raise FeatureError(f"Nothing called '{ident}' in {SOURCES.get(source, source)}")
    return hit


# ------------------------------------------------------------------ import
def import_item(doc, item: dict, link: bool = True, target: dict | None = None, length: float | None = None,
                nut: bool = False, washer: bool = False, name: str | None = None, checkpoint: bool = True) -> dict:
    """Bring `item` into `doc`. target = a face pick {"object", "index", "point" (world), "local"} to connect to.
    Returns {"objects": [...], "mates": [...], "connected": bool, "message": str}."""
    if checkpoint:
        doc.checkpoint()
    src = item["source"]
    via = item.get("via") if src == "inventory" else src
    tag = {"system": src, "id": item.get("id"), "name": item.get("name"), "linked": bool(link and item.get("linkable"))}
    if via == "fastener":
        kind, size = item["kind"], item["size"]
        L = float(length if length is not None else item.get("length") or 0.0)
        if item.get("needs_length", kind in ("socket head cap screw", "hex bolt", "flat head screw")) and L <= 0:
            raise FeatureError("Pick a length for the screw / bolt")
        tgt = None
        if target and target.get("object"):
            tgt = {"obj": target["object"], "face": target.get("index"), "near": target.get("point")}
        try:
            r = doc.insert_part(kind, size, L, target=tgt, nut=nut, washer=washer, checkpoint=False)
        except FeatureError:
            if tgt is None:
                raise
            r = doc.insert_part(kind, size, L, target=None, nut=nut, washer=washer, checkpoint=False)
            r["message"] = "Picked face isn't a hole — inserted unmated"
        for n in r["objects"][:1]:
            doc.obj(n).source = tag
        if src == "inventory":
            _tag_inventory(doc, r["objects"][:1], item)
        doc.rebuild()
        return {"objects": r["objects"], "mates": r["mates"], "connected": bool(r["mates"]),
                "message": r.get("message", "")}
    if via == "std":
        from ..stdparts import LibraryError, get_library
        try:
            n = get_library().insert(doc, item.get("std_id") or item["id"], link=link, checkpoint=False)
        except LibraryError as e:
            raise FeatureError(str(e)) from e
    elif via in ("file", "project"):
        path = item.get("path")
        if not path or not os.path.exists(path):
            raise FeatureError(f"Source file not found: {path}")
        n = doc.insert_part_file(path, link=link, checkpoint=False, name=name or item.get("name"))
        if via == "project":
            for f in doc.features:
                if f.obj == n and f.TYPE == "import":
                    f.params["source"] = f"{Path(path).name} (project)"
    else:
        raise FeatureError(f"Unknown import source '{src}'")
    o = doc.obj(n)
    o.source = {**tag, "path": item.get("path") or (o.std or {}).get("path")}
    if src == "inventory":
        _tag_inventory(doc, [n], item)
    out = {"objects": [n], "mates": [], "connected": False, "message": ""}
    if target and target.get("object") and doc.has_obj(target["object"]) and target["object"] != n:
        try:
            out["mates"] = connect(doc, n, target)
            out["connected"] = bool(out["mates"])
        except FeatureError as e:
            out["message"] = f"Placed at the picked point; not mated ({e})"
    return out


def _tag_inventory(doc, names, item):
    p = item.get("part") or {}
    for n in names:
        o = doc.obj(n)
        o.part_no = p.get("partNumber") or o.part_no or p.get("name")
        o.description = p.get("name") or o.description
        if p.get("unitCost"):
            o.unit_cost = float(p["unitCost"])
        o.source = {**(o.source or {}), "system": "inventory", "id": p.get("id") or item.get("id"),
                    "name": p.get("name")}


def connect(doc, name: str, target: dict) -> list[str]:
    """Mate a freshly imported object to a picked face: flat → coincident (its bottom face on the picked face),
    cylinder → concentric (its closest cylinder on the picked axis). Returns mate names."""
    import numpy as np
    tobj, fi = target["object"], int(target["index"])
    body = doc.result.bodies.get(tobj)
    if body is None:
        raise FeatureError(f"{tobj} has no body")
    face = list(body.faces())[fi]
    gt = face.geom_type.name
    o = doc.obj(name)
    p = np.asarray(target.get("point") or [0, 0, 0], float)
    o.pose = [float(p[0]), float(p[1]), float(p[2])] + list(o.pose[3:])      # start near the pick
    tref = {"face_index": fi}
    if target.get("local") is not None:
        tref["pick"] = list(target["local"])
    if gt == "PLANE":
        m = doc.add_mate("coincident", {"obj": tobj, "ref": tref}, {"obj": name, "ref": {"face": "bottom"}},
                         checkpoint=False)
        return [m.name]
    if gt == "CYLINDER":
        nb = doc.result.bodies.get(name)
        if nb is None or not any(f.geom_type.name == "CYLINDER" for f in nb.faces()):
            raise FeatureError("the part has no round face to put on that axis")
        m = doc.add_mate("concentric", {"obj": tobj, "ref": tref}, {"obj": name, "ref": {"axis_near": [0, 0, 0]}},
                         checkpoint=False)
        return [m.name]
    raise FeatureError(f"can't mate to a {gt.lower()} face")
