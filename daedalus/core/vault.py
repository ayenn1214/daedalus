"""Version control and release for a .daedalus file — no server: the history sits beside the file
(`<name>.history/`), so it works on a shared drive and travels with the part.

- Check in: a numbered, read-only snapshot with a message, author and key figures (mass, volume, features).
- Compare any two versions: features added / removed / changed (which parameters), objects, materials, variables,
  drawings, mass and volume.
- Restore a version into the working file (undoable), or open one read-only.
- Lifecycle per version: WIP → In review → Released → Obsolete. Review records approvals (who, when, note);
  release gives the next revision letter (A, B, C … skipping I O Q S X Z), writes the release package
  (STEP + drawing PDFs + BOM) to `<name> Rev X/`, adds a row to each drawing's revision table, and makes the earlier
  released revision Obsolete. Released snapshots are never changed.
Qt-free."""
from __future__ import annotations

import datetime as dt
import getpass
import gzip
import hashlib
import json
import os
from pathlib import Path

STATES = ["WIP", "In review", "Released", "Obsolete"]
LETTERS = "ABCDEFGHJKLMNPRTUVWY"                    # ASME Y14.35: no I O Q S X Z


class VaultError(RuntimeError):
    pass


def author() -> str:
    try:
        from PySide6.QtCore import QSettings
        n = QSettings("Daedalus", "Daedalus").value("user/name", "")
        if n:
            return str(n)
    except Exception:  # noqa: BLE001
        pass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def history_dir(path) -> Path:
    p = Path(path)
    return p.with_name(p.stem + ".history")


def _index_path(path) -> Path:
    return history_dir(path) / "index.json"


def load_index(path) -> dict:
    ip = _index_path(path)
    if not ip.exists():
        return {"file": Path(path).name, "versions": [], "require_approval": False, "release_dir": ""}
    return json.loads(ip.read_text(encoding="utf-8"))


def _save_index(path, idx: dict):
    ip = _index_path(path)
    ip.parent.mkdir(parents=True, exist_ok=True)
    tmp = ip.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    os.replace(tmp, ip)


def versions(path) -> list[dict]:
    return load_index(path)["versions"]


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def _doc_dict(doc) -> dict:
    d = doc.to_dict()
    d.pop("rollback", None)
    return d


def _figures(doc) -> dict:
    try:
        mp = doc.mass_properties()
    except Exception:  # noqa: BLE001
        mp = {}
    return {"mass_kg": mp.get("mass_kg"), "volume_mm3": mp.get("volume_mm3"), "features": len(doc.features),
            "objects": len(doc.objects), "drawings": len(getattr(doc, "drawings", []) or [])}


def _snap_path(path, v: int) -> Path:
    return history_dir(path) / f"v{v:04d}.json.gz"


def read_version(path, v: int) -> dict:
    sp = _snap_path(path, v)
    if not sp.exists():
        raise VaultError(f"Version {v} is missing from {history_dir(path)}")
    return json.loads(gzip.decompress(sp.read_bytes()).decode("utf-8"))


def _entry(path, v: int) -> dict:
    e = next((x for x in versions(path) if x["v"] == v), None)
    if e is None:
        raise VaultError(f"No version {v}")
    return e


def latest(path) -> dict | None:
    vs = versions(path)
    return vs[-1] if vs else None


def changed_since(doc) -> bool:
    """Is the working document different from its last checked-in version?"""
    if not doc.path:
        return True
    e = latest(doc.path)
    if e is None:
        return True
    h = hashlib.sha1(_canon(_doc_dict(doc)).encode()).hexdigest()
    return h not in (e["hash"], e.get("stamp_hash"))     # stamp_hash: the same version with its revision rows


def check_in(doc, message: str, who: str | None = None, save: bool = True) -> dict:
    """Save the document and record it as the next version. Nothing new to record → VaultError."""
    if not doc.path:
        raise VaultError("Save the part as a file first — its history is kept beside the file")
    if not (message or "").strip():
        raise VaultError("Say what changed (check-in message)")
    if save:
        doc.save(doc.path)
    d = _doc_dict(doc)
    h = hashlib.sha1(_canon(d).encode()).hexdigest()
    idx = load_index(doc.path)
    if idx["versions"] and h in (idx["versions"][-1]["hash"], idx["versions"][-1].get("stamp_hash")):
        raise VaultError(f"No changes since version {idx['versions'][-1]['v']}")
    v = (idx["versions"][-1]["v"] + 1) if idx["versions"] else 1
    full = doc.to_dict()
    sp = _snap_path(doc.path, v)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_bytes(gzip.compress(json.dumps(full).encode("utf-8"), 6))
    e = {"v": v, "date": dt.datetime.now().isoformat(timespec="seconds"), "author": who or author(),
         "message": message.strip(), "state": "WIP", "rev": "", "hash": h, "approvals": [], "history": [],
         **_figures(doc)}
    idx["versions"].append(e)
    _save_index(doc.path, idx)
    return e


def restore(doc, v: int):
    """Put version v into the working document (one undo step). Check in afterwards to record it."""
    d = read_version(doc.path, v)
    doc.checkpoint()
    path = doc.path
    doc._load_dict(d)
    doc.path = path
    doc.dirty = True
    doc.rebuild()
    return doc


def open_version(path, v: int):
    from .document import Document
    doc = Document()
    doc._load_dict(read_version(path, v))
    doc.rebuild()
    doc.path = None                                  # read-only copy: never saved over the file
    doc.read_only_version = (str(path), v)
    return doc


# ----------------------------------------------------------------------------------------- compare
def _feats(d):
    return {f["name"]: f for f in d.get("features", [])}


def _short(v, n=60):
    if isinstance(v, float):
        v = int(v) if v.is_integer() else round(v, 6)
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n - 1] + "…"


def diff_dicts(a: dict, b: dict) -> dict:
    fa, fb = _feats(a), _feats(b)
    added = [n for n in fb if n not in fa]
    removed = [n for n in fa if n not in fb]
    changed = []
    for n in fb:
        if n in fa and _canon(fa[n]) != _canon(fb[n]):
            pa, pb = fa[n].get("params") or {}, fb[n].get("params") or {}
            keys = sorted(k for k in set(pa) | set(pb) if _canon(pa.get(k)) != _canon(pb.get(k)))
            what = [f"{k}: {_short(pa.get(k))} → {_short(pb.get(k))}" for k in keys if k not in ("data", "cache")]
            if fa[n].get("suppressed") != fb[n].get("suppressed"):
                what.append("suppressed" if fb[n].get("suppressed") else "unsuppressed")
            if fa[n].get("obj") != fb[n].get("obj"):
                what.append(f"object: {fa[n].get('obj')} → {fb[n].get('obj')}")
            changed.append({"feature": n, "type": fb[n]["type"], "changes": what or ["edited"]})
    order_a = [n for n in fa if n in fb]
    order_b = [n for n in fb if n in fa]
    out = {"added": [{"feature": n, "type": fb[n]["type"]} for n in added],
           "removed": [{"feature": n, "type": fa[n]["type"]} for n in removed],
           "changed": changed, "reordered": order_a != order_b}
    oa = {o["name"]: o for o in a.get("objects", [])}
    ob = {o["name"]: o for o in b.get("objects", [])}
    out["objects"] = {"added": [n for n in ob if n not in oa], "removed": [n for n in oa if n not in ob],
                      "changed": [n for n in ob if n in oa and _canon(oa[n]) != _canon(ob[n])]}
    va, vb = a.get("variables") or {}, b.get("variables") or {}
    out["variables"] = {k: [va.get(k), vb.get(k)] for k in sorted(set(va) | set(vb)) if va.get(k) != vb.get(k)}
    if a.get("material") != b.get("material"):
        out["material"] = [a.get("material"), b.get("material")]
    da, db = {x.get("name"): x for x in a.get("drawings", [])}, {x.get("name"): x for x in b.get("drawings", [])}
    out["drawings"] = {"added": [n for n in db if n not in da], "removed": [n for n in da if n not in db],
                       "changed": [n for n in db if n in da and _canon(da[n]) != _canon(db[n])]}
    return out


def compare(path, a: int, b: int | None = None, doc=None) -> dict:
    """Version a vs version b (or vs the working document when b is None and doc is given)."""
    da = read_version(path, a)
    if b is None:
        if doc is None:
            raise VaultError("Compare with which version?")
        db, eb = doc.to_dict(), {"v": "working", **_figures(doc)}
    else:
        db, eb = read_version(path, b), _entry(path, b)
    ea = _entry(path, a)
    out = diff_dicts(da, db)
    for k in ("mass_kg", "volume_mm3"):
        if ea.get(k) is not None and eb.get(k) is not None and ea[k] != eb[k]:
            out[k] = [ea[k], eb[k]]
    out["from"], out["to"] = a, eb["v"]
    return out


def describe(d: dict) -> str:
    """The comparison as readable lines."""
    L = []
    for x in d.get("added", []):
        L.append(f"+ {x['feature']} ({x['type']})")
    for x in d.get("removed", []):
        L.append(f"− {x['feature']} ({x['type']})")
    for x in d.get("changed", []):
        L.append(f"~ {x['feature']}: " + "; ".join(x["changes"]))
    if d.get("reordered"):
        L.append("~ feature order changed")
    o = d.get("objects") or {}
    for k, sym in (("added", "+"), ("removed", "−"), ("changed", "~")):
        for n in o.get(k, []):
            L.append(f"{sym} object {n}")
    for k, (x, y) in (d.get("variables") or {}).items():
        L.append(f"~ variable {k}: {x} → {y}")
    if d.get("material"):
        L.append(f"~ material: {d['material'][0]} → {d['material'][1]}")
    dr = d.get("drawings") or {}
    for k, sym in (("added", "+"), ("removed", "−"), ("changed", "~")):
        for n in dr.get(k, []):
            L.append(f"{sym} drawing {n}")
    if d.get("mass_kg"):
        a, b = d["mass_kg"]
        L.append(f"mass {a:g} → {b:g} kg ({(b - a) / a * 100:+.1f} %)" if a else f"mass {a} → {b} kg")
    if d.get("volume_mm3") and not d.get("mass_kg"):
        a, b = d["volume_mm3"]
        L.append(f"volume {a:g} → {b:g} mm³")
    return "\n".join(L) or "No differences"


# ----------------------------------------------------------------------------------------- lifecycle
def next_letter(path) -> str:
    revs = [e["rev"] for e in versions(path) if e.get("rev")]
    if not revs:
        return "A"
    last = revs[-1]
    if last in LETTERS and LETTERS.index(last) < len(LETTERS) - 1:
        return LETTERS[LETTERS.index(last) + 1]
    return last + "A"


def _log(e, what, who, note=""):
    e.setdefault("history", []).append({"what": what, "by": who, "date": dt.datetime.now().isoformat(
        timespec="seconds"), "note": note})


def submit(path, v: int, who: str | None = None, note: str = "") -> dict:
    idx = load_index(path)
    e = next(x for x in idx["versions"] if x["v"] == v)
    if e["state"] != "WIP":
        raise VaultError(f"Version {v} is {e['state']} — only work in progress goes to review")
    e["state"] = "In review"
    e["approvals"] = []
    _log(e, "Submitted for review", who or author(), note)
    _save_index(path, idx)
    return e


def review(path, v: int, approve: bool, who: str | None = None, note: str = "") -> dict:
    idx = load_index(path)
    e = next(x for x in idx["versions"] if x["v"] == v)
    if e["state"] != "In review":
        raise VaultError(f"Version {v} isn't in review")
    who = who or author()
    e["approvals"].append({"by": who, "approved": bool(approve), "note": note,
                           "date": dt.datetime.now().isoformat(timespec="seconds")})
    if not approve:
        e["state"] = "WIP"
        _log(e, "Rejected", who, note)
    else:
        _log(e, "Approved", who, note)
    _save_index(path, idx)
    return e


def set_require_approval(path, on: bool):
    idx = load_index(path)
    idx["require_approval"] = bool(on)
    _save_index(path, idx)


def release(path, v: int, who: str | None = None, note: str = "", package: bool = True, doc=None) -> dict:
    """Release version v: next revision letter, release package, revision rows, older release → Obsolete."""
    idx = load_index(path)
    e = next((x for x in idx["versions"] if x["v"] == v), None)
    if e is None:
        raise VaultError(f"No version {v}")
    if e["state"] in ("Released", "Obsolete"):
        raise VaultError(f"Version {v} is already {e['state']}")
    approved = any(a["approved"] for a in e.get("approvals", []))
    if idx.get("require_approval") and not (e["state"] == "In review" and approved):
        raise VaultError("This part needs an approval before release — submit it for review first")
    who = who or author()
    letter = next_letter(path)
    for x in idx["versions"]:
        if x["state"] == "Released":
            x["state"] = "Obsolete"
            _log(x, f"Superseded by Rev {letter}", who)
    e["state"], e["rev"] = "Released", letter
    e["released"] = {"by": who, "date": dt.date.today().isoformat(), "note": note}
    _log(e, f"Released as Rev {letter}", who, note)
    _save_index(path, idx)
    if package:
        try:
            e["package"] = write_package(path, v, letter, note or e["message"], who)
        except Exception as ex:  # noqa: BLE001
            e["package_error"] = str(ex)
        idx = load_index(path)
        nxt = next(x for x in idx["versions"] if x["v"] == v)
        nxt.update({k: e[k] for k in ("package", "package_error") if k in e})
        _save_index(path, idx)
    try:
        from .. import m365
        m365.notify("released", f"Released: {Path(path).stem} Rev {letter}", note or e["message"], {
            "Version": v, "By": who, "Mass": f"{e.get('mass_kg')} kg",
            "Package": str(release_dir(path, letter)) if e.get("package") else "—"}, key=f"{path}:{letter}")
    except Exception:  # noqa: BLE001
        pass
    if doc is not None and doc.path and Path(doc.path) == Path(path) and not changed_since(doc):
        _stamp(doc, letter, note or e["message"], who)        # the open part's drawings get the revision row
        try:
            doc.save(doc.path)
            idx = load_index(path)
            x = next(x for x in idx["versions"] if x["v"] == v)
            x["stamp_hash"] = hashlib.sha1(_canon(_doc_dict(doc)).encode()).hexdigest()
            _save_index(path, idx)
        except Exception:  # noqa: BLE001
            pass
    return e


def _stamp(doc, letter, description, who):
    for dr in getattr(doc, "drawings", []) or []:
        for sh in getattr(dr, "sheets", []) or []:
            if not any(str(r.get("rev")) == letter for r in sh.revisions):
                sh.revisions.append({"rev": letter, "description": description, "date": dt.date.today().isoformat(),
                                     "approved": who, "zone": ""})


def obsolete(path, v: int, who: str | None = None, note: str = "") -> dict:
    idx = load_index(path)
    e = next(x for x in idx["versions"] if x["v"] == v)
    e["state"] = "Obsolete"
    _log(e, "Made obsolete", who or author(), note)
    _save_index(path, idx)
    return e


def release_dir(path, letter: str) -> Path:
    idx = load_index(path)
    base = Path(idx.get("release_dir") or Path(path).parent)
    return base / f"{Path(path).stem} Rev {letter}"


def write_package(path, v: int, letter: str, description: str = "", who: str = "") -> list[str]:
    """STEP of the released version, a PDF of each drawing (with the revision row), BOM CSV, and a release note."""
    doc = open_version(path, v)
    _stamp(doc, letter, description, who)
    out = release_dir(path, letter)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(path).stem} Rev {letter}"
    files = []
    if doc.result.body is not None:
        files.append(str(doc.export(out / f"{stem}.step")))
    for dr in getattr(doc, "drawings", []) or []:
        try:
            files.append(str(doc.export_drawing(out / f"{stem} {dr.name}.pdf", dr.name)))
        except Exception:  # noqa: BLE001
            pass
    try:
        if len(doc.objects) > 1:
            doc.export_bom(out / f"{stem} BOM.csv")
            files.append(str(out / f"{stem} BOM.csv"))
    except Exception:  # noqa: BLE001
        pass
    e = _entry(path, v)
    note = out / f"{stem} release note.txt"
    note.write_text("\n".join([f"{Path(path).stem} — Rev {letter}", f"Released {dt.date.today().isoformat()} by {who}",
                               f"Version {v}: {e['message']}", description and f"Note: {description}" or "",
                               f"Mass {e.get('mass_kg')} kg · volume {e.get('volume_mm3')} mm³",
                               "", "Files:"] + [f"  {Path(f).name}" for f in files]), encoding="utf-8")
    files.append(str(note))
    return files


def status(doc, modified: bool | None = None) -> dict:
    """For the status bar: latest version, released revision, and whether the working file has changed."""
    if not doc.path or not history_dir(doc.path).exists():
        return {"tracked": False}
    vs = versions(doc.path)
    rel = next((e for e in reversed(vs) if e["state"] == "Released"), None)
    return {"tracked": bool(vs), "latest": vs[-1]["v"] if vs else 0, "state": vs[-1]["state"] if vs else "",
            "released": rel["rev"] if rel else "", "modified": changed_since(doc) if modified is None else modified}
