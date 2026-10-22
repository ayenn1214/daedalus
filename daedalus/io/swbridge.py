"""SolidWorks bridge.

.sldprt/.sldasm are a closed format: no open library reads their geometry. If SolidWorks is
installed (Windows), this module drives it over COM to write

    <name>.step               geometry (parts, names, colours, placements)
    <name>.daedalus-mates.json  components (name, fixed, bounding box) and mates
                              (type + mate entity geometry in assembly coordinates, mm)

and Daedalus imports both: one object per component, and mates rebuilt as Daedalus mates.

Without SolidWorks on this machine, Daedalus uses those two files if they sit next to the
.sldasm/.sldprt (convert on a SolidWorks PC with the batch command below, copy them over), or
explains how to Save As STEP from SolidWorks (geometry only, no mates).

Batch (on a PC with SolidWorks):
    python -m daedalus.io.swbridge  C:\\path\\to\\folder_or_file  [--out C:\\path\\to\\out]

Needs:  pip install pywin32

NOTE: written against the documented SolidWorks API (OpenDoc6, Extension.SaveAs, IMate2,
IMateEntity2.EntityParams). It could not be run here (no Windows/SolidWorks), so treat the
first run as a test; errors are reported per mate / per file rather than aborting.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .formats import ImportError_, ImportResult, import_step


def _c(obj, name, *args):
    """Call a COM method whether pywin32 exposes it as a method or (late binding) already ran it."""
    v = getattr(obj, name)
    return v(*args) if callable(v) else v

SIDE_SUFFIX = ".daedalus-mates.json"
PROPS_SUFFIX = ".daedalus-props.json"         # custom properties + material (parts and assemblies)
LAST_CONVERT: dict[str, Path] = {}            # source path → temp folder of this session's conversion
LEGACY_SIDE_SUFFIX = ".strata-mates.json"     # written by the bridge before the rename

# Daedalus mate for each SolidWorks mate feature type name (IFeature.GetTypeName2)
SW_MATE_NAMES = {
    "MateCoincident": "coincident", "MateConcentric": "concentric", "MatePerpendicular": "perpendicular",
    "MateParallel": "parallel", "MateDistanceDim": "distance", "MateAngleDim": "angle",
    "MateLock": "lock", "MateHinge": "hinge",
}
# fallback by IMate2.Type (swMateType_e)
SW_MATE_TYPES = {0: "coincident", 1: "concentric", 2: "perpendicular", 3: "parallel", 5: "distance",
                 6: "angle", 16: "lock", 22: "hinge"}
SW_UNSUPPORTED = {4: "tangent", 7: "unknown", 8: "symmetric", 9: "cam", 10: "gear", 11: "width",
                  12: "lock to sketch", 13: "rack & pinion", 15: "path", 17: "screw", 18: "linear coupler",
                  19: "universal joint", 20: "coordinate", 21: "slot"}

SW_PART, SW_ASSEMBLY = 1, 2
OPEN_SILENT, OPEN_READONLY = 1, 2
SAVE_CURRENT, SAVE_SILENT = 0, 1


# ------------------------------------------------------------------ status ---

def available() -> tuple[bool, str]:
    """(True, "") when SolidWorks can be driven from here, else (False, why)."""
    if sys.platform != "win32":
        return False, "SolidWorks runs on Windows only"
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False, "pywin32 isn't installed (pip install pywin32)"
    try:
        import winreg
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "SldWorks.Application")
    except OSError:
        return False, "SolidWorks isn't installed on this PC"
    return True, ""


HOW_TO = ("SolidWorks files are a closed format. Options:\n"
          "• On a PC with SolidWorks: run Daedalus there (it drives SolidWorks automatically), or run\n"
          "    python -m daedalus.io.swbridge <file or folder>\n"
          "  and copy the .step + .daedalus-mates.json it writes next to the SolidWorks file.\n"
          "• Or in SolidWorks: File ▸ Save As ▸ STEP AP214 (*.step), then import the STEP here "
          "(geometry, names, colours and placement — no mates).")


# --------------------------------------------------------------------- COM ---

def _byref_int():
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def _null_dispatch():
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def _app(visible=False):
    import win32com.client
    sw = win32com.client.Dispatch("SldWorks.Application")
    try:
        sw.Visible = bool(visible)
    except Exception:  # noqa: BLE001
        pass
    return sw


def _call(obj, name, *args):
    """Property-or-method access (late-bound COM exposes some API methods as properties)."""
    v = getattr(obj, name)
    return v(*args) if callable(v) else v


def _open(sw, path: Path):
    kind = SW_ASSEMBLY if path.suffix.lower() == ".sldasm" else SW_PART
    err, warn = _byref_int(), _byref_int()
    model = sw.OpenDoc6(str(path), kind, OPEN_SILENT | OPEN_READONLY, "", err, warn)
    if model is None:
        raise ImportError_(f"SolidWorks couldn't open {path.name} (error {err.value})")
    if kind == SW_ASSEMBLY:
        try:
            model.ResolveAllLightWeightComponents(False)
        except Exception:  # noqa: BLE001
            pass
    return model, kind


def _save_step(model, out: Path):
    err, warn = _byref_int(), _byref_int()
    ok = False
    try:
        ok = model.Extension.SaveAs(str(out), SAVE_CURRENT, SAVE_SILENT, _null_dispatch(), err, warn)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        try:
            ok = model.SaveAs3(str(out), SAVE_CURRENT, SAVE_SILENT) == 0
        except Exception:  # noqa: BLE001
            ok = False
    if not ok or not out.exists():
        raise ImportError_(f"SolidWorks couldn't write STEP ({out.name}, error {err.value})")


def _entity_kind(ent, params):
    """'plane' | 'axis' | 'point' from the mate entity: by parameter count, and for the
    6-value case (plane or line) by asking the referenced geometry."""
    n = len(params)
    if n == 3:
        return "point"
    if n in (7, 8):
        return "axis"                     # cylinder / cone: point on axis, direction, radius
    if n == 6:
        ref = None
        try:
            ref = _call(ent, "Reference")
        except Exception:  # noqa: BLE001
            pass
        if ref is not None:
            try:                          # a face
                surf = _c(ref, "GetSurface")
                return "plane" if _c(surf, "IsPlane") else "axis"
            except Exception:  # noqa: BLE001
                pass
            try:                          # a reference plane feature
                if "plane" in str(_call(ref, "GetTypeName2")).lower():
                    return "plane"
            except Exception:  # noqa: BLE001
                pass
            return "axis"                 # edge / axis / sketch line
        try:                              # swMateEntity2ReferenceType_e: compare by constant name
            import win32com.client
            c = win32com.client.constants
            return "plane" if _call(ent, "ReferenceType2") == c.swMatePlane else "axis"
        except Exception:  # noqa: BLE001
            return "plane"
    return None


def _entity(ent) -> dict:
    params = [float(v) for v in (_call(ent, "EntityParams") or [])]
    kind = _entity_kind(ent, params)
    if kind is None:
        raise ValueError(f"unsupported mate entity ({len(params)} params)")
    comp = _call(ent, "ReferenceComponent")
    name = _call(comp, "Name2") if comp is not None else ""
    mm = [v * 1000.0 for v in params[:3]]
    out = {"component": name, "kind": kind, "p": mm}
    if kind == "plane":
        out["n"] = params[3:6]
    elif kind == "axis":
        out["d"] = params[3:6]
        out["r"] = params[6] * 1000.0 if len(params) > 6 else 0.0
    return out


def _mate_value(feat, mate, mtype):
    if mtype not in ("distance", "angle"):
        return 0.0
    try:
        d = _c(feat, "GetDefinition")
        if mtype == "distance":
            return float(_call(d, "Distance")) * 1000.0
        import math
        return math.degrees(float(_call(d, "Angle")))
    except Exception:  # noqa: BLE001
        pass
    try:
        dim = _call(mate, "DisplayDimension2", 0).GetDimension2(0)
        v = float(dim.SystemValue)
        import math
        return v * 1000.0 if mtype == "distance" else math.degrees(v)
    except Exception:  # noqa: BLE001
        return None                       # Daedalus measures it from the placed parts


def _get_components(asm, top_level_only=False):
    last = None
    for target in (asm, "IAssemblyDoc"):
        try:
            obj = asm if target is asm else __import__("win32com.client", fromlist=["CastTo"]).CastTo(asm, target)
            return obj.GetComponents(top_level_only) or []
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _components(asm) -> list[dict]:
    out = []
    for c in _get_components(asm, False):
        try:
            if _call(c, "IsSuppressed"):
                continue
            box = c.GetBox(False, False)
            out.append({"name": _call(c, "Name2"), "file": _call(c, "GetPathName"),
                        "fixed": bool(_call(c, "IsFixed")),
                        "box": [float(v) * 1000.0 for v in box] if box else None})
        except Exception:  # noqa: BLE001
            continue
    return out


def _mates(model) -> tuple[list[dict], list[dict]]:
    mates, skipped = [], []
    feat = _c(model, "FirstFeature")
    while feat is not None:
        if _call(feat, "GetTypeName2") == "MateGroup":
            sub = _c(feat, "GetFirstSubFeature")
            while sub is not None:
                name = _call(sub, "Name")
                try:
                    _one_mate(sub, name, mates, skipped)
                except Exception as e:  # noqa: BLE001
                    skipped.append({"name": name, "reason": str(e)})
                sub = _c(sub, "GetNextSubFeature")
        feat = _c(feat, "GetNextFeature")
    return mates, skipped


def _one_mate(sub, name, mates, skipped):
    tname = _call(sub, "GetTypeName2")
    try:
        if _call(sub, "IsSuppressed"):
            return
    except Exception:  # noqa: BLE001
        pass
    mate = _c(sub, "GetSpecificFeature2")
    mtype = SW_MATE_NAMES.get(tname)
    if mtype is None:
        try:
            code = int(_call(mate, "Type"))
        except Exception:  # noqa: BLE001
            code = -1
        mtype = SW_MATE_TYPES.get(code)
        if mtype is None:
            skipped.append({"name": name, "reason": f"{SW_UNSUPPORTED.get(code, tname)} mates aren't supported"})
            return
    count = int(_call(mate, "MateEntityCount"))
    ents = [_entity(_call(mate, "MateEntity", i)) for i in range(count)]
    value = _mate_value(sub, mate, mtype)
    if mtype == "hinge" and count >= 4:        # concentric pair + face pair
        mates.append({"name": name, "type": "hinge", "a": ents[0], "b": ents[1], "value": 0.0})
        mates.append({"name": name + "-faces", "type": "coincident", "a": ents[2], "b": ents[3], "value": 0.0})
        return
    if count != 2:
        skipped.append({"name": name, "reason": f"{count} references (Daedalus mates join two)"})
        return
    mates.append({"name": name, "type": mtype, "a": ents[0], "b": ents[1], "value": value})


def convert(path, out_dir=None, sw=None, visible=False) -> tuple[Path, Path | None]:
    """Open a .sldprt/.sldasm in SolidWorks; write STEP (+ mates JSON for assemblies)."""
    ok, why = available()
    if not ok:
        raise ImportError_(why + "\n\n" + HOW_TO)
    path = Path(path).resolve()
    out_dir = Path(out_dir) if out_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    sw = sw or _app(visible)
    model, kind = _open(sw, path)
    try:
        step = out_dir / (path.stem + ".step")
        _save_step(model, step)
        try:
            (out_dir / (path.stem + PROPS_SUFFIX)).write_text(json.dumps(_properties(model, kind), indent=1))
        except Exception:  # noqa: BLE001  (properties are a bonus; geometry matters)
            pass
        side = None
        if kind == SW_ASSEMBLY:
            mates, skipped = _mates(model)
            data = {"source": path.name, "units": "mm", "coords": "assembly",
                    "components": _components(model), "mates": mates, "skipped": skipped}
            side = out_dir / (path.stem + SIDE_SUFFIX)
            side.write_text(json.dumps(data, indent=1))
        return step, side
    finally:
        try:
            sw.CloseDoc(_c(model, "GetTitle"))
        except Exception:  # noqa: BLE001
            pass


def _get_prop(mgr, name):
    """Resolved value of one custom property (Get4/Get5/Get6 return (ret, val, resolved, …) via pywin32)."""
    for meth, args in (("Get6", (name, False)), ("Get5", (name, False)), ("Get4", (name, False))):
        try:
            r = getattr(mgr, meth)(*args)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(r, tuple):
            vals = [v for v in r[1:] if isinstance(v, str)]
            if vals:
                return vals[1] if len(vals) > 1 and vals[1] else vals[0]
    return None


def _properties(model, kind) -> dict:
    """File-level custom properties, overlaid with the active configuration's, plus material."""
    props, cfg = {}, ""
    try:
        cfg = model.ConfigurationManager.ActiveConfiguration.Name
    except Exception:  # noqa: BLE001
        pass
    for scope in ("", cfg):
        if scope is None:
            continue
        try:
            mgr = model.Extension.CustomPropertyManager(scope)
            names = _c(mgr, "GetNames") or ()
        except Exception:  # noqa: BLE001
            continue
        for n in names:
            v = _get_prop(mgr, n)
            if v not in (None, ""):
                props[str(n)] = str(v)
    material = ""
    if kind == SW_PART:
        try:
            r = model.GetMaterialPropertyName2(cfg, "")
            material = r[0] if isinstance(r, tuple) else r
        except Exception:  # noqa: BLE001
            pass
    try:
        title = _c(model, "GetTitle")
    except Exception:  # noqa: BLE001
        title = ""
    return {"properties": props, "material": material or "", "configuration": cfg, "title": title}


def read_props(path) -> dict | None:
    """Properties written by convert(): next to the file (if not older than it) or from this session's
    temporary conversion."""
    path = Path(path)
    for folder in (path.parent, LAST_CONVERT.get(str(path.resolve()))):
        if folder is None:
            continue
        f = Path(folder) / (path.stem + PROPS_SUFFIX)
        if f.exists() and (folder != path.parent or f.stat().st_mtime >= path.stat().st_mtime - 2):
            try:
                return json.loads(f.read_text())
            except ValueError:
                return None
    return None


def convert_cached(path) -> tuple[Path, Path | None]:
    """convert() into a temp folder once per session per file."""
    key = str(Path(path).resolve())
    tmp = LAST_CONVERT.get(key)
    if tmp is not None:
        step = tmp / (Path(path).stem + ".step")
        side = tmp / (Path(path).stem + SIDE_SUFFIX)
        if step.exists():
            return step, (side if side.exists() else None)
    tmp = Path(tempfile.mkdtemp(prefix="daedalus-sw-"))
    out = convert(path, tmp)
    LAST_CONVERT[key] = tmp
    return out


# ------------------------------------------------------------------ import ---

def _sidecars(path: Path):
    step = next((path.with_suffix(s) for s in (".step", ".stp", ".STEP", ".STP") if path.with_suffix(s).exists()), None)
    side = path.parent / (path.stem + SIDE_SUFFIX)
    if not side.exists():
        side = path.parent / (path.stem + LEGACY_SIDE_SUFFIX)
    if step is None:
        return None, None
    fresh = step.stat().st_mtime >= path.stat().st_mtime - 2
    return (step if fresh else None), (side if side.exists() and fresh else None)


def import_solidworks(path) -> ImportResult:
    path = Path(path)
    step, side = _sidecars(path)
    notes = []
    if step is not None:
        notes.append(f"Used {step.name}" + (f" + {side.name}" if side else "") + " converted earlier")
    else:
        ok, why = available()
        if not ok:
            raise ImportError_(f"Can't read {path.name} here: {why}.\n\n{HOW_TO}")
        step, side = convert_cached(path)
        notes.append("Converted with SolidWorks")
    res = import_step(step)
    res.kind = "solidworks"
    res.notes = notes + res.notes
    if side is not None:
        res.mates = json.loads(side.read_text())
        sk = res.mates.get("skipped", [])
        if sk:
            notes.append(f"{len(sk)} mate(s) not convertible")
    elif path.suffix.lower() == ".sldasm":
        res.notes.append("No mates file — parts are placed as in SolidWorks but not mated")
    return res


# --------------------------------------------------------------------- CLI ---

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m daedalus.io.swbridge",
                                 description="Convert SolidWorks parts/assemblies to STEP + Daedalus mates JSON")
    ap.add_argument("paths", nargs="+", help=".sldprt/.sldasm files or folders")
    ap.add_argument("--out", help="output folder (default: next to each file)")
    ap.add_argument("--visible", action="store_true", help="show the SolidWorks window")
    a = ap.parse_args(argv)
    ok, why = available()
    if not ok:
        print(why + "\n\n" + HOW_TO)
        return 2
    files = []
    for p in map(Path, a.paths):
        if p.is_dir():
            files += [f for f in sorted(p.rglob("*")) if f.suffix.lower() in (".sldprt", ".sldasm")
                      and not f.name.startswith("~$")]
        else:
            files.append(p)
    sw = _app(a.visible)
    bad = 0
    for f in files:
        try:
            step, side = convert(f, a.out, sw=sw)
            extra = ""
            if side:
                d = json.loads(side.read_text())
                extra = f", {len(d['mates'])} mates" + (f" ({len(d['skipped'])} skipped)" if d["skipped"] else "")
            print(f"OK   {f.name} → {step.name}{extra}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {f.name}: {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
