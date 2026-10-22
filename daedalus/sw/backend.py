"""SolidWorks session backends.

ComBackend   drives the running SolidWorks over COM (Windows + pywin32). Written against the documented
             SolidWorks API (ISldWorks, IModelDoc2, IAssemblyDoc, IComponent2, ICustomPropertyManager,
             IModelDocExtension, ISelectionMgr). It could not be run here (no Windows/SolidWorks), so errors are
             caught per call and reported with the API call that failed.
FakeBackend  an in-memory SolidWorks session (assembly tree, properties, selection, exports) used by the tests
             and by `python -m daedalus --sw --demo`, so every Link feature can be exercised without SolidWorks.

All lengths in/out of this module are millimetres; masses kg.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PART, ASSEMBLY, DRAWING = 1, 2, 3
KIND_NAME = {PART: "part", ASSEMBLY: "assembly", DRAWING: "drawing"}
EXT_KIND = {".sldprt": PART, ".sldasm": ASSEMBLY, ".slddrw": DRAWING}
VIEWS = {"Front": 1, "Back": 2, "Left": 3, "Right": 4, "Top": 5, "Bottom": 6, "Isometric": 7, "Trimetric": 8,
         "Dimetric": 9}
# property names other tools use for the same thing (first hit wins when reading)
PN_KEYS = ("PartNo", "Part Number", "PartNumber", "Part No", "Number")
DESC_KEYS = ("Description", "DESCRIPTION", "Desc")
HOLOCRON_KEY = "HolocronId"
STD_KEY = "DaedalusStdId"


class SWError(RuntimeError):
    pass


def _c(obj, name, *args):
    """Call a COM method. pywin32's late binding runs zero-argument methods as soon as they're looked up
    (``doc.GetType`` is already the int), so ``doc.GetType()`` would fail with "'int' object is not callable";
    this works either way."""
    v = getattr(obj, name)
    return v(*args) if callable(v) else v


@dataclass
class SWDoc:
    path: str
    title: str
    kind: int
    configuration: str = "Default"
    properties: dict = field(default_factory=dict)
    material: str = ""
    mass_kg: float | None = None
    volume_mm3: float | None = None
    center_mm: list | None = None

    def to_dict(self):
        return {"path": self.path, "title": self.title, "kind": KIND_NAME.get(self.kind, "?"),
                "configuration": self.configuration, "properties": dict(self.properties), "material": self.material,
                "mass_kg": self.mass_kg, "volume_mm3": self.volume_mm3, "center_mm": self.center_mm}


@dataclass
class SWComponent:
    name: str                      # instance name, e.g. "Bracket-1"
    path: str
    configuration: str = "Default"
    suppressed: bool = False
    fixed: bool = False
    properties: dict = field(default_factory=dict)
    material: str = ""
    mass_kg: float | None = None
    parent: str = ""               # sub-assembly instance name ("" = top level)
    is_assembly: bool = False

    def to_dict(self):
        return {k: getattr(self, k) for k in ("name", "path", "configuration", "suppressed", "fixed", "properties",
                                               "material", "mass_kg", "parent", "is_assembly")}


def first_prop(props: dict, keys) -> str:
    low = {k.lower(): v for k, v in props.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return ""


# ====================================================================== interface
class Backend:
    name = "?"
    app = "solidworks"            # which CAD program ("solidworks" | "fusion")
    native_suffix = ".SLDPRT"     # library parts are converted to this once; None = insert the STEP directly
    native_key = "sw_path"        # standard-library field remembering the program's own file
    window_prefix = "SOLIDWORKS"  # main-window title, for placing the δ hotbar over it

    def connected(self) -> bool: ...
    def app_info(self) -> dict: ...
    def active_doc(self) -> SWDoc | None: ...
    def components(self, top_level_only=False) -> list[SWComponent]: ...
    def selection(self) -> list[dict]: ...
    def get_properties(self, path: str | None = None, configuration: str = "") -> dict: ...
    def set_properties(self, props: dict, path: str | None = None, configuration: str = ""): ...
    def insert_component(self, path: str, at_mm=(0, 0, 0)) -> str: ...
    def mate_to_selection(self, component: str, selected: dict, kind: str = "concentric") -> list[str]: ...
    def open_doc(self, path: str) -> SWDoc: ...
    def import_step(self, step: str, save_as: str) -> str: ...
    def export(self, path: str) -> str: ...
    def export_with_mates(self, out_dir: str) -> tuple[str, str | None, str | None]: ...
    def rebuild(self): ...
    def save(self): ...
    def view(self, name: str): ...
    def zoom_fit(self): ...
    def run_macro(self, path: str, module: str = "", proc: str = "main"): ...
    def run_command(self, command_id: int): ...


# ====================================================================== COM
class ComBackend(Backend):
    name = "SolidWorks"

    def __init__(self, attach_only=True, lazy=False):
        """lazy: don't fail when SolidWorks isn't reachable yet — keep trying on every status poll (the Link
        window then connects as soon as SolidWorks is up)."""
        if sys.platform != "win32":
            raise SWError("SolidWorks runs on Windows only")
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError as e:
            raise SWError(f"pywin32 isn't usable ({e}). Install it into this Python: "
                          f"\"{sys.executable}\" -m pip install --upgrade pywin32") from e
        self.attach_only = attach_only
        self._sw = None
        self.last_error = ""
        try:
            self._attach()
        except SWError:
            if not lazy:
                raise

    def _attach(self):
        import pythoncom
        import win32com.client
        try:
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        errors = []
        for progid in sw_progids():
            try:
                self._sw = win32com.client.GetActiveObject(progid)
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{progid}: {_com_err(e)}")
        if self._sw is None:
            obj = _rot_lookup("sldworks")              # registered under a moniker but not the ProgID
            if obj is not None:
                self._sw = win32com.client.Dispatch(obj)
        if self._sw is None and not self.attach_only:
            self._sw = win32com.client.Dispatch("SldWorks.Application")
            self._sw.Visible = True
        if self._sw is None:
            running = sw_process_running()
            if running and is_elevated_mismatch():
                msg = ("SolidWorks is running but Windows blocks the connection: SolidWorks and Daedalus run at "
                       "different privilege levels. Start both normally (not 'Run as administrator'), or both "
                       "as administrator.")
            elif running:
                msg = ("SolidWorks is running but hasn't registered for automation yet — wait until it has fully "
                       "started (close any licence / welcome dialog), then Connect. Help ▸ Diagnose shows details.")
            else:
                msg = "SolidWorks isn't running — start it and the Link connects automatically"
            self.last_error = msg
            raise SWError(msg)
        self.last_error = ""
        return self._sw

    @property
    def sw(self):
        if self._sw is None:
            self._attach()
        return self._sw

    def reconnect(self):
        self._sw = None
        return self._attach()

    # helpers -----------------------------------------------------------------
    @staticmethod
    def _call(obj, name, *args):
        v = getattr(obj, name)
        return v(*args) if callable(v) else v

    @staticmethod
    def _byref_int():
        import pythoncom
        import win32com.client
        return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

    @staticmethod
    def _null():
        import pythoncom
        import win32com.client
        return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

    def _model(self, required=True):
        m = self.sw.ActiveDoc
        if m is None and required:
            raise SWError("No document is open in SolidWorks")
        return m

    def _doc_for(self, path):
        if not path:
            return self._model()
        m = self.sw.GetOpenDocumentByName(str(path))
        if m is None:
            raise SWError(f"{Path(path).name} isn't open in SolidWorks")
        return m

    def _mass(self, model):
        try:
            mp = _c(model.Extension, "CreateMassProperty")
            c = mp.CenterOfMass
            return float(mp.Mass), float(mp.Volume) * 1e9, [float(v) * 1000 for v in c] if c else None
        except Exception:  # noqa: BLE001
            return None, None, None

    def _props(self, model, configuration=""):
        from ..io.swbridge import _get_prop
        out = {}
        scopes = [""]
        if configuration:
            scopes.append(configuration)
        for scope in scopes:
            try:
                mgr = model.Extension.CustomPropertyManager(scope)
                for n in _c(mgr, "GetNames") or ():
                    v = _get_prop(mgr, n)
                    if v not in (None, ""):
                        out[str(n)] = str(v)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _material(self, model, cfg):
        try:
            r = model.GetMaterialPropertyName2(cfg, "")
            return (r[0] if isinstance(r, tuple) else r) or ""
        except Exception:  # noqa: BLE001
            return ""

    # interface ---------------------------------------------------------------
    def _revision(self):
        return self._call(self.sw, "RevisionNumber")

    def connected(self):
        try:
            return bool(self._revision())
        except SWError:
            return False
        except Exception:  # noqa: BLE001
            try:                                    # SolidWorks restarted: the old COM object is dead
                self.reconnect()
                return bool(self._revision())
            except Exception:  # noqa: BLE001
                return False

    def app_info(self):
        try:
            rev = self._revision()
        except Exception:  # noqa: BLE001
            rev = "?"
        major = str(rev).split(".")[0]
        year = 1992 + int(major) if major.isdigit() else None       # 32 → 2024
        return {"name": "SolidWorks", "revision": rev, "year": year}

    def active_doc(self):
        m = self._model(required=False)
        if m is None:
            return None
        kind = int(_c(m, "GetType"))
        try:
            cfg = m.ConfigurationManager.ActiveConfiguration.Name
        except Exception:  # noqa: BLE001
            cfg = ""
        mass, vol, c = self._mass(m) if kind in (PART, ASSEMBLY) else (None, None, None)
        return SWDoc(path=_c(m, "GetPathName"), title=_c(m, "GetTitle"), kind=kind, configuration=cfg,
                     properties=self._props(m, cfg), material=self._material(m, cfg) if kind == PART else "",
                     mass_kg=mass, volume_mm3=vol, center_mm=c)

    def components(self, top_level_only=False):
        m = self._model()
        if int(_c(m, "GetType")) != ASSEMBLY:
            return []
        out = []
        from ..io.swbridge import _get_components
        for c in _get_components(m, bool(top_level_only)):
            try:
                path = self._call(c, "GetPathName")
                cfg = self._call(c, "ReferencedConfiguration")
                cm = self._call(c, "GetModelDoc2")
                props = self._props(cm, cfg) if cm is not None else {}
                mat, mass = "", None
                if cm is not None and int(_c(cm, "GetType")) == PART:
                    mat = self._material(cm, cfg)
                    mass = self._mass(cm)[0]
                parent = ""
                try:
                    p = _c(c, "GetParent")
                    parent = self._call(p, "Name2") if p is not None else ""
                except Exception:  # noqa: BLE001
                    pass
                out.append(SWComponent(
                    name=self._call(c, "Name2"), path=path, configuration=cfg or "",
                    suppressed=bool(self._call(c, "IsSuppressed")), fixed=bool(self._call(c, "IsFixed")),
                    properties=props, material=mat, mass_kg=mass, parent=parent,
                    is_assembly=str(path).lower().endswith(".sldasm")))
            except Exception:  # noqa: BLE001
                continue
        return out

    def selection(self):
        m = self._model()
        sm = m.SelectionManager
        out = []
        for i in range(1, int(sm.GetSelectedObjectCount2(-1)) + 1):
            try:
                typ = int(sm.GetSelectedObjectType3(i, -1))
                obj = sm.GetSelectedObject6(i, -1)
                comp = sm.GetSelectedObjectsComponent4(i, -1)
                item = {"index": i, "type": typ, "object": obj,
                        "component": self._call(comp, "Name2") if comp is not None else ""}
                if typ == 2:                                  # swSelFACES
                    s = _c(obj, "GetSurface")
                    item["surface"] = "cylinder" if _c(s, "IsCylinder") else ("plane" if _c(s, "IsPlane") else "other")
                    if _c(s, "IsCylinder"):
                        p = s.CylinderParams            # origin xyz, axis xyz, radius (m)
                        item["radius_mm"] = float(p[6]) * 1000
                        item["axis"] = [float(v) for v in p[3:6]]
                        item["point_mm"] = [float(v) * 1000 for v in p[0:3]]
                out.append(item)
            except Exception:  # noqa: BLE001
                continue
        return out

    def get_properties(self, path=None, configuration=""):
        m = self._doc_for(path)
        return self._props(m, configuration)

    def set_properties(self, props, path=None, configuration=""):
        m = self._doc_for(path)
        mgr = m.Extension.CustomPropertyManager(configuration or "")
        for k, v in props.items():
            # Add3(name, swCustomInfoText=30, value, swCustomPropertyReplaceValue=2)
            r = mgr.Add3(str(k), 30, str(v), 2)
            if isinstance(r, int) and r not in (0, 1):
                raise SWError(f"Couldn't set property {k} (code {r})")
        try:
            _c(m, "SetSaveFlag")
        except Exception:  # noqa: BLE001
            pass

    def _open_silent(self, path):
        kind = EXT_KIND.get(Path(path).suffix.lower(), PART)
        err, warn = self._byref_int(), self._byref_int()
        m = self.sw.OpenDoc6(str(path), kind, 1, "", err, warn)    # swOpenDocOptions_Silent
        if m is None:
            raise SWError(f"SolidWorks couldn't open {Path(path).name} (error {err.value})")
        return m

    def insert_component(self, path, at_mm=(0, 0, 0)):
        asm = self._model()
        if int(_c(asm, "GetType")) != ASSEMBLY:
            raise SWError("Make an assembly the active document to insert parts")
        asm_title = _c(asm, "GetTitle")
        part = self._open_silent(path)            # AddComponent needs the file loaded
        try:
            err = self._byref_int()
            self.sw.ActivateDoc3(asm_title, False, 0, err)
            x, y, z = (float(v) / 1000 for v in at_mm)
            comp = asm.AddComponent5(str(path), 0, "", False, "", x, y, z)
            if comp is None:
                raise SWError(f"SolidWorks didn't insert {Path(path).name} (AddComponent5)")
            return self._call(comp, "Name2")
        finally:
            try:
                if _c(part, "GetTitle") != asm_title:
                    self.sw.CloseDoc(_c(part, "GetTitle"))
            except Exception:  # noqa: BLE001
                pass

    def mate_to_selection(self, component, selected, kind="concentric"):
        """Concentric (hole) or coincident (face) mate between a previously selected face and the closest
        matching face of the inserted component."""
        asm = self._model()
        from ..io.swbridge import _get_components
        comp = next((c for c in _get_components(asm, False) if self._call(c, "Name2") == component), None)
        if comp is None:
            raise SWError(f"{component} not found in the assembly")
        want_cyl = selected.get("surface") == "cylinder"
        best, bscore = None, None
        for body in comp.GetBodies3(0, None) or []:          # swSolidBody
            for f in _c(body, "GetFaces") or []:
                s = _c(f, "GetSurface")
                if want_cyl and _c(s, "IsCylinder"):
                    score = abs(float(s.CylinderParams[6]) * 1000 - selected.get("radius_mm", 0))
                elif not want_cyl and _c(s, "IsPlane"):
                    score = -float(_c(f, "GetArea"))
                else:
                    continue
                if bscore is None or score < bscore:
                    best, bscore = f, score
        if best is None:
            raise SWError(f"{component} has no {'round' if want_cyl else 'flat'} face to mate")
        asm.ClearSelection2(True)
        ok1 = selected["object"].Select4(False, self._null())
        ok2 = best.Select4(True, self._null())
        if not (ok1 and ok2):
            raise SWError("Couldn't re-select the mate faces")
        mtype = 1 if want_cyl else 0                            # swMateCONCENTRIC / swMateCOINCIDENT
        err = self._byref_int()
        mate = asm.AddMate5(mtype, 2, False, 0, 0, 0, 1, 1, 0, 0, 0, False, False, 0, err)
        asm.ClearSelection2(True)
        if mate is None:
            raise SWError(f"SolidWorks rejected the mate (error {err.value})")
        try:
            _c(asm, "EditRebuild3")
        except Exception:  # noqa: BLE001
            pass
        return [self._call(mate, "Name")] if hasattr(mate, "Name") else ["mate"]

    def open_doc(self, path):
        if Path(path).suffix.lower() in (".step", ".stp", ".igs", ".iges", ".x_t"):
            err = self._byref_int()
            m = self.sw.LoadFile4(str(path), "r", self.sw.GetImportFileData(str(path)), err)
            if m is None:
                raise SWError(f"SolidWorks couldn't import {Path(path).name} (error {err.value})")
            return self.active_doc()
        m = self._open_silent(path)
        err = self._byref_int()
        self.sw.ActivateDoc3(_c(m, "GetTitle"), False, 0, err)
        return self.active_doc()

    def import_step(self, step, save_as):
        """Open a STEP in SolidWorks and save it as a native part (for library parts / fasteners)."""
        err = self._byref_int()
        data = self.sw.GetImportFileData(str(step))
        m = self.sw.LoadFile4(str(step), "r", data, err)
        if m is None:
            raise SWError(f"SolidWorks couldn't import {Path(step).name} (error {err.value})")
        try:
            e, w = self._byref_int(), self._byref_int()
            ok = m.Extension.SaveAs(str(save_as), 0, 1, self._null(), e, w)
            if not ok:
                raise SWError(f"Couldn't save {Path(save_as).name} (error {e.value})")
        finally:
            try:
                self.sw.CloseDoc(_c(m, "GetTitle"))
            except Exception:  # noqa: BLE001
                pass
        return str(save_as)

    def export(self, path):
        """Save a copy of the active document (format from the extension). Tries the SaveAs variants of
        different SolidWorks versions, since late-bound COM can reject one with 'Member not found'."""
        m = self._model()
        p = str(path)
        Path(p).unlink(missing_ok=True)
        e, w = self._byref_int(), self._byref_int()
        attempts = [
            ("Extension.SaveAs3", lambda: m.Extension.SaveAs3(p, 0, 1, self._null(), self._null(), e, w)),
            ("Extension.SaveAs", lambda: m.Extension.SaveAs(p, 0, 1, self._null(), e, w)),
            ("typed IModelDoc2 Extension.SaveAs",
             lambda: _typed(m, "IModelDoc2").Extension.SaveAs(p, 0, 1, None, e, w)),
            ("SaveAs3", lambda: m.SaveAs3(p, 0, 1)),
            ("SaveAs", lambda: m.SaveAs(p)),
        ]
        tried = []
        for name, fn in attempts:
            try:
                fn()
            except Exception as ex:  # noqa: BLE001
                tried.append(f"{name}: {_com_err(ex)}")
                continue
            if Path(p).exists():
                return p
            tried.append(f"{name}: no file written (error {getattr(e, 'value', '?')})")
        raise SWError(f"SolidWorks couldn't export {Path(p).name}.\nTried:\n  " + "\n  ".join(tried))

    def export_with_mates(self, out_dir):
        """STEP + mates JSON + properties of the active document (as the batch bridge writes them). Only the
        STEP is essential: properties and mates are best-effort, with what failed recorded in the JSON."""
        import json

        from ..io import swbridge as B
        m = self._model()
        kind = int(_c(m, "GetType"))
        stem = Path(_c(m, "GetPathName") or _c(m, "GetTitle")).stem
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        step = out / (stem + ".step")
        self.export(str(step))
        props = out / (stem + B.PROPS_SUFFIX)
        try:
            props.write_text(json.dumps(B._properties(m, kind), indent=1))
        except Exception:  # noqa: BLE001
            props = None
        side = None
        self.last_notes = []
        if kind == ASSEMBLY:
            notes = []
            try:
                comps = B._components(_typed(m, "IAssemblyDoc"))
            except Exception as ex:  # noqa: BLE001
                comps = []
                notes.append(f"components: {_com_err(ex)}")
            try:
                mates, skipped = B._mates(m)
            except Exception as ex:  # noqa: BLE001
                mates, skipped = [], [{"name": "(all mates)", "reason": f"couldn't read mates: {_com_err(ex)}"}]
            self.last_notes = notes + [f"{x['name']}: {x['reason']}" for x in skipped]
            side = out / (stem + B.SIDE_SUFFIX)
            side.write_text(json.dumps({"source": Path(_c(m, "GetPathName") or stem).name, "units": "mm",
                                        "coords": "assembly", "components": comps, "mates": mates,
                                        "skipped": skipped}, indent=1))
        return str(step), str(side) if side else None, str(props) if props else None

    def rebuild(self):
        self._model().ForceRebuild3(False)

    def save(self):
        e, w = self._byref_int(), self._byref_int()
        if not self._model().Save3(1, e, w):                    # swSaveAsOptions_Silent
            raise SWError(f"Save failed (error {e.value})")

    def view(self, name):
        m = self._model()
        m.ShowNamedView2("", VIEWS[name])
        _c(m, "ViewZoomtofit2")

    def zoom_fit(self):
        _c(self._model(), "ViewZoomtofit2")

    def run_macro(self, path, module="", proc="main"):
        mods = [module] if module else [Path(path).stem, Path(path).stem + "1", "Module1"]
        last = None
        for mod in mods:
            err = self._byref_int()
            try:
                if self.sw.RunMacro2(str(path), mod, proc or "main", 1, err):
                    return
                last = err.value
            except Exception as e:  # noqa: BLE001
                last = e
        raise SWError(f"Macro {Path(path).name} didn't run ({last})")

    def run_command(self, command_id):
        if not self.sw.RunCommand(int(command_id), ""):
            raise SWError(f"SolidWorks command {command_id} didn't run")


# ====================================================================== fake
class FakeBackend(Backend):
    """A pretend SolidWorks session: one assembly with parts, properties, selection and exports."""
    name = "SolidWorks (demo)"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or tempfile.mkdtemp(prefix="sw-demo-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.log: list[str] = []
        a = self.root / "Lift Cart.SLDASM"
        self.docs: dict[str, SWDoc] = {}
        self.comps: list[SWComponent] = []
        self._sel: list[dict] = []
        self.files: dict[str, dict] = {}               # path → {"size": (x,y,z) mm} for exports
        self.active = str(a)
        self.docs[str(a)] = SWDoc(str(a), a.name, ASSEMBLY, properties={"Description": "Lift cart assembly",
                                                                          "Project": "P25605"})
        for name, size, props, mat, mass, n in (
                ("Base Frame", (900, 600, 60), {"PartNo": "RG-3001", "Description": "Welded base frame"},
                 "ASTM A36 Steel", 21.4, 1),
                ("Lift Arm", (500, 60, 20), {"PartNo": "RG-3002", "Description": "Lift arm, 1/2 plate"},
                 "ASTM A36 Steel", 4.7, 2),
                ("Caster Plate", (120, 100, 10), {"Description": "Caster mount plate"}, "6061-T6 (SS)", 0.32, 4),
                ("SHCS M8x25", (13, 13, 33), {"PartNo": "SHCS M8 x 25 (ISO)", "Description": "Socket head cap screw"},
                 "Alloy Steel", 0.017, 16)):
            p = self.root / f"{name}.SLDPRT"
            p.write_text("demo part")
            self.docs[str(p)] = SWDoc(str(p), p.name, PART, properties=dict(props), material=mat, mass_kg=mass)
            self.files[str(p)] = {"size": size}
            for i in range(n):
                self.comps.append(SWComponent(f"{name}-{i + 1}", str(p), properties=self.docs[str(p)].properties,
                                              material=mat, mass_kg=mass, fixed=(name == "Base Frame")))
        self.files[str(a)] = {"size": (900, 600, 700)}
        a.write_text("demo assembly")

    def _doc(self, path=None) -> SWDoc:
        p = str(path or self.active)
        if p not in self.docs:
            raise SWError(f"{Path(p).name} isn't open in SolidWorks")
        return self.docs[p]

    def connected(self):
        return True

    def app_info(self):
        return {"name": "SolidWorks (demo)", "revision": "32.0.0", "year": 2024}

    def active_doc(self):
        d = self._doc()
        if d.kind == ASSEMBLY:
            d.mass_kg = round(sum(c.mass_kg or 0 for c in self.comps if not c.suppressed), 4)
        return d

    def components(self, top_level_only=False):
        return [c for c in self.comps if not (top_level_only and c.parent)]

    def selection(self):
        return list(self._sel)

    def select_hole(self, component="Caster Plate-1", radius_mm=4.5):
        """Demo helper: pretend the user clicked a hole face."""
        self._sel = [{"index": 1, "type": 2, "object": None, "component": component, "surface": "cylinder",
                      "radius_mm": radius_mm, "axis": [0, 0, 1], "point_mm": [0, 0, 0]}]

    def get_properties(self, path=None, configuration=""):
        return dict(self._doc(path).properties)

    def set_properties(self, props, path=None, configuration=""):
        d = self._doc(path)
        d.properties.update({k: str(v) for k, v in props.items()})
        self.log.append(f"props {Path(d.path).name} {props}")

    def insert_component(self, path, at_mm=(0, 0, 0)):
        if self._doc().kind != ASSEMBLY:
            raise SWError("Make an assembly the active document to insert parts")
        p = str(path)
        if p not in self.docs:
            self.docs[p] = SWDoc(p, Path(p).name, PART)
        n = sum(1 for c in self.comps if c.path == p) + 1
        name = f"{Path(p).stem}-{n}"
        d = self.docs[p]
        self.comps.append(SWComponent(name, p, properties=d.properties, material=d.material, mass_kg=d.mass_kg))
        self.log.append(f"insert {name}")
        return name

    def mate_to_selection(self, component, selected, kind="concentric"):
        m = f"{'Concentric' if selected.get('surface') == 'cylinder' else 'Coincident'}{len(self.log) + 1}"
        self.log.append(f"mate {m} {component} → {selected.get('component')}")
        self._sel = []
        return [m]

    def open_doc(self, path):
        p = str(path)
        if p not in self.docs:
            kind = EXT_KIND.get(Path(p).suffix.lower(), PART)
            self.docs[p] = SWDoc(p, Path(p).name, kind)
        self.active = p
        return self.docs[p]

    def import_step(self, step, save_as):
        Path(save_as).write_text(f"demo native part from {Path(step).name}")
        self.docs[str(save_as)] = SWDoc(str(save_as), Path(save_as).name, PART)
        self.log.append(f"import {Path(step).name} → {Path(save_as).name}")
        return str(save_as)

    def _write_step(self, path):
        """A real STEP of boxes standing in for the components, so Daedalus can open it."""
        from build123d import Box, Compound, Location, export_step
        d = self._doc()
        if d.kind == ASSEMBLY:
            parts, x = [], 0.0
            for c in self.comps:
                if c.suppressed:
                    continue
                sx, sy, sz = self.files.get(c.path, {}).get("size", (50, 50, 50))
                b = Box(sx, sy, sz).moved(Location((x + sx / 2, 0, sz / 2)))
                b.label = c.name
                parts.append(b)
                x += sx + 20
            shape = Compound(children=parts)
        else:
            sx, sy, sz = self.files.get(d.path, {}).get("size", (50, 50, 50))
            shape = Box(sx, sy, sz)
            shape.label = Path(d.path).stem
        export_step(shape, str(path))

    def export(self, path):
        p = Path(path)
        if p.suffix.lower() in (".step", ".stp"):
            self._write_step(p)
        else:
            p.write_text(f"demo export of {self._doc().title}")
        self.log.append(f"export {p.name}")
        return str(p)

    def export_with_mates(self, out_dir):
        import json

        from ..io import swbridge as B
        d = self._doc()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(d.path).stem
        step = out / (stem + ".step")
        self.export(step)
        props = out / (stem + B.PROPS_SUFFIX)
        props.write_text(json.dumps({"properties": d.properties, "material": d.material,
                                     "configuration": d.configuration, "title": d.title}))
        return str(step), None, str(props)

    def rebuild(self):
        self.log.append("rebuild")

    def save(self):
        self.log.append("save")

    def view(self, name):
        if name not in VIEWS:
            raise SWError(f"Unknown view {name}")
        self.log.append(f"view {name}")

    def zoom_fit(self):
        self.log.append("zoom fit")

    def run_macro(self, path, module="", proc="main"):
        if not os.path.exists(path):
            raise SWError(f"Macro not found: {path}")
        self.log.append(f"macro {Path(path).name}")

    def run_command(self, command_id):
        self.log.append(f"command {command_id}")


# ---------------------------------------------------------------- Windows helpers / diagnostics
def _typed(obj, iface):
    """Early-bound view of a COM object as `iface` (e.g. IAssemblyDoc) when the type library allows it —
    some members are 'not found' through the late-bound default interface."""
    try:
        import win32com.client
        return win32com.client.CastTo(obj, iface)
    except Exception:  # noqa: BLE001
        return obj


def explain(e) -> str:
    """User-facing text for an error from the Link, naming the SolidWorks API step for COM errors."""
    if isinstance(e, SWError):
        return str(e)
    import traceback
    where = ""
    try:
        fr = [f for f in traceback.extract_tb(e.__traceback__) if "daedalus" in f.filename]
        if fr:
            f = fr[-1]
            where = f"\n\nAt: {Path(f.filename).name}:{f.lineno} {f.name}() — {(f.line or '').strip()}"
    except Exception:  # noqa: BLE001
        pass
    kind = "SolidWorks API error" if e.__class__.__name__ == "com_error" else e.__class__.__name__
    return f"{kind}: {_com_err(e)}{where}\n\n(Copy this message — it shows which call failed.)"


def _com_err(e) -> str:
    try:
        hr = e.args[0] if e.args else None
        known = {-2147221021: "not in the Running Object Table (MK_E_UNAVAILABLE)",
                 -2147221005: "ProgID not registered (CO_E_CLASSSTRING)",
                 -2147024891: "access denied (E_ACCESSDENIED)",
                 -2147352573: "Member not found (DISP_E_MEMBERNOTFOUND)",
                 -2147352570: "Unknown name (DISP_E_UNKNOWNNAME)",
                 -2147352571: "Type mismatch (DISP_E_TYPEMISMATCH)",
                 -2147352562: "Bad parameter count (DISP_E_BADPARAMCOUNT)"}
        if isinstance(hr, int):
            return f"0x{hr & 0xFFFFFFFF:08X} {known.get(hr, e.args[1] if len(e.args) > 1 else '')}".strip()
    except Exception:  # noqa: BLE001
        pass
    return str(e)


def sw_progids() -> list[str]:
    """'SldWorks.Application' plus the versioned ProgIDs registered on this machine (newest first)."""
    out = ["SldWorks.Application"]
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CurVer") as k:
                cur = winreg.QueryValueEx(k, "")[0]
                if cur:
                    out.append(cur)
        except OSError:
            pass
        for n in range(40, 19, -1):
            name = f"SldWorks.Application.{n}"
            try:
                winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, name).Close()
                out.append(name)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(out))


def _rot_entries() -> list[tuple[str, object]]:
    try:
        import pythoncom
        rot = pythoncom.GetRunningObjectTable()
        ctx = pythoncom.CreateBindCtx(0)
        out = []
        for mk in rot.EnumRunning():
            try:
                out.append((mk.GetDisplayName(ctx, None), mk))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


def _rot_lookup(fragment: str):
    import pythoncom
    for name, mk in _rot_entries():
        if fragment.lower() in name.lower():
            try:
                obj = pythoncom.GetRunningObjectTable().GetObject(mk)
                return obj.QueryInterface(pythoncom.IID_IDispatch)
            except Exception:  # noqa: BLE001
                continue
    return None


def _pids(image="SLDWORKS.exe") -> list[int]:
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == image.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def sw_process_running() -> bool:
    return bool(_pids())


def _elevated_pid(pid=None):
    """True/False whether a process (default: this one) runs elevated; None if Windows won't say (which, for
    another process, usually means it is elevated and we are not)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
        h = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return None
        elev = wintypes.DWORD()
        size = wintypes.DWORD()
        ok = adv.GetTokenInformation(tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size))
        k32.CloseHandle(tok)
        if pid is not None:
            k32.CloseHandle(h)
        return bool(elev.value) if ok else None
    except Exception:  # noqa: BLE001
        return None


def is_elevated_mismatch() -> bool:
    me = _elevated_pid()
    for pid in _pids():
        other = _elevated_pid(pid)
        if other is None and me is False:
            return True
        if other is not None and me is not None and other != me:
            return True
    return False


def diagnose(app="solidworks") -> list[tuple[str, bool | None, str]]:
    """Connection checks: (check, ok/None=info, detail incl. the fix)."""
    import platform
    rows = []
    rows.append(("Python", None, f"{sys.version.split()[0]} {platform.architecture()[0]} — {sys.executable}"))
    if "WindowsApps" in sys.executable:
        rows.append(("Python install", False, "Microsoft Store Python is sandboxed and can't always reach other "
                                              "apps over COM. Install Python from python.org and reinstall Daedalus."))
    if app == "fusion":
        from .fusion_backend import diagnose as fdiag
        return rows + fdiag()
    if app in ("rhino", "inventor", "solidedge"):
        from .more_backends import diagnose as mdiag
        return rows + mdiag(app)
    if sys.platform != "win32":
        return rows + [("Windows", False, "SolidWorks Link needs Windows")]
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
        import pywintypes
        rows.append(("pywin32", True, f"loaded ({os.path.dirname(pywintypes.__file__)})"))
    except Exception as e:  # noqa: BLE001
        rows.append(("pywin32", False, f"{e} — fix: \"{sys.executable}\" -m pip install --upgrade pywin32"))
        return rows
    pids = _pids()
    rows.append(("SolidWorks running", bool(pids), f"SLDWORKS.exe pid {', '.join(map(str, pids))}" if pids else
                 "SLDWORKS.exe not found — start SolidWorks"))
    me = _elevated_pid()
    rows.append(("Daedalus elevated", None, "yes (Run as administrator)" if me else "no"))
    for pid in pids:
        other = _elevated_pid(pid)
        same = (other == me) if other is not None else (me is True)
        rows.append((f"SolidWorks {pid} elevated", same,
                     ("same level as Daedalus" if same else
                      "different privilege level from Daedalus — Windows blocks COM between them. Start both "
                      "normally (or both as administrator).") +
                     ("" if other is not None else " (Windows hid its level: it's probably running as administrator)")))
    progs = sw_progids()
    rows.append(("Registered ProgIDs", len(progs) > 1 or None, ", ".join(progs)))
    rot = [n for n, _m in _rot_entries()]
    sw_rot = [n for n in rot if "sldworks" in n.lower() or "solidworks" in n.lower()]
    rows.append(("Running Object Table", bool(sw_rot) or (None if not pids else False),
                 ", ".join(sw_rot) if sw_rot else f"no SolidWorks entry among {len(rot)} — SolidWorks still starting, "
                                                  "or a privilege mismatch"))
    import win32com.client
    got = None
    for pid_ in progs:
        try:
            got = win32com.client.GetActiveObject(pid_)
            rows.append((f"Attach {pid_}", True, "ok"))
            break
        except Exception as e:  # noqa: BLE001
            rows.append((f"Attach {pid_}", False, _com_err(e)))
    if got is not None:
        try:
            rev = ComBackend._call(got, "RevisionNumber")
            rows.append(("API call", True, f"RevisionNumber = {rev}"))
            doc = got.ActiveDoc
            rows.append(("Active document", None, ComBackend._call(doc, "GetTitle") if doc is not None else "none"))
        except Exception as e:  # noqa: BLE001
            rows.append(("API call", False, _com_err(e)))
    return rows


def connect(demo=False, root=None, app="solidworks", lazy=False) -> Backend:
    if app in ("rhino", "inventor", "solidedge"):
        from .more_backends import connect as mconnect
        return mconnect(app, demo=demo, root=root, lazy=lazy)
    if app == "fusion":
        from .fusion_backend import FakeFusionBackend, FusionBackend
        return FakeFusionBackend(root) if demo else FusionBackend(lazy=lazy)
    if demo:
        return FakeBackend(root)
    return ComBackend(lazy=lazy)
