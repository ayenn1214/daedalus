"""More CAD programs for the CAD Link: Rhino, Autodesk Inventor and Solid Edge.

Rhino      a small Python script running inside Rhino (daedalus/rhino/DaedalusLink.py) answers the same JSON
           protocol as the Fusion add-in, so RhinoBackend is FusionBackend with Rhino's conventions (objects /
           blocks keyed "rhino:…", STEP in and out, no mates).
Inventor   COM ("Inventor.Application"): documents, iProperties, occurrences, STEP export / import, views.
Solid Edge COM ("SolidEdge.Application"): documents, properties, occurrences, STEP export / import, views.

Written against the documented APIs; they could not be run here (no Windows / no licences), so every call is
guarded and errors name the API step that failed (CAD Link ▸ Diagnose shows the connection checks).
All lengths in/out are millimetres (Inventor's API works in cm, Solid Edge's in m).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .backend import (ASSEMBLY, DRAWING, PART, Backend, FakeBackend, SWComponent, SWDoc, SWError, _c, _com_err,
                      _pids)
from .fusion_backend import FakeFusionBackend, FusionBackend

RHINO_LINK = Path.home() / ".daedalus" / "rhino-link.json"
RHINO_SETUP = ("Rhino isn't linked. In Daedalus: CAD Link ▸ Install Rhino link…, then in Rhino type the command it "
               "shows (_-RunPythonScript \"…DaedalusLink.py\"). Add it to Rhino Options ▸ General ▸ \"Run these "
               "commands every time Rhino starts\" to make it permanent.")

APPS = {           # app key: (label, has a demo session)
    "solidworks": ("SolidWorks", True),
    "fusion": ("Fusion", True),
    "rhino": ("Rhino", True),
    "inventor": ("Inventor", False),
    "solidedge": ("Solid Edge", False),
}


def label(app: str) -> str:
    return APPS.get(app, (app.title(), False))[0]


def _safe(name):
    import re
    return re.sub(r'[<>:"/\\|?*]+', "-", str(name)).strip() or "part"


# ====================================================================== Rhino
class RhinoBackend(FusionBackend):
    name = "Rhino"
    label = "Rhino"
    app = "rhino"
    native_suffix = None               # Rhino imports STEP directly
    native_key = None
    window_prefix = "*Rhino"           # "*": the window title contains it ("Untitled - Rhino 8")
    LINK = RHINO_LINK
    SETUP = RHINO_SETUP

    def insert_component(self, path, at_mm=(0, 0, 0)):
        if Path(path).suffix.lower() not in (".step", ".stp", ".3dm", ".igs", ".iges", ".stl", ".obj"):
            raise SWError("Rhino inserts STEP / 3DM / IGES / STL / OBJ files")
        r = self._call("insert_step", path=str(path), at_mm=list(at_mm), name=Path(path).stem)
        self._last_inserted_key = r.get("key")
        return r["name"]

    def mate_to_selection(self, component, selected, kind="concentric"):
        raise SWError("Rhino has no mates — the part was placed at the origin; position it with the Gumball")

    def open_doc(self, path):
        self._call("open_step", path=str(path))            # imported into the open Rhino document
        return self.active_doc()

    def export(self, path):
        p = Path(path)
        if p.suffix.lower() not in (".step", ".stp", ".igs", ".iges", ".stl", ".3dm", ".obj"):
            raise SWError(f"Rhino can't export {p.suffix} from here (step, iges, stl, 3dm, obj)")
        self._call("export", path=str(path))
        if not p.exists():
            raise SWError(f"Rhino didn't write {p.name}")
        return str(path)

    def run_macro(self, path, module="", proc="main"):
        if not Path(path).exists():
            raise SWError(f"Script not found: {path}")
        self._call("command", id=f'_-RunPythonScript "{path}"')

    def run_command(self, command_id):
        self._call("command", id=str(command_id))


class FakeRhinoBackend(FakeFusionBackend):
    """Simulated Rhino session: objects keyed rhino:obj:…, user text as properties, no mates."""
    name = "Rhino (demo)"
    app = "rhino"
    window_prefix = "*Rhino"
    KEY, TOP, LABEL = "rhino:obj:", "rhino://", "Rhino"

    def __init__(self, root=None):
        super().__init__(root)
        self.docs[self.active].title = "Cam Lock Fixture.3dm"

    def app_info(self):
        return {"name": "Rhino (demo)", "revision": "8.12", "year": None}

    def mate_to_selection(self, component, selected, kind="concentric"):
        raise SWError("Rhino has no mates — the part was placed at the origin; position it with the Gumball")

    def insert_component(self, path, at_mm=(0, 0, 0)):
        if Path(path).suffix.lower() not in (".step", ".stp", ".3dm", ".igs", ".iges", ".stl", ".obj"):
            raise SWError("Rhino inserts STEP / 3DM / IGES / STL / OBJ files")
        return super().insert_component(Path(path).with_suffix(".step"), at_mm)

    def open_doc(self, path):
        return FakeBackend.open_doc(self, f"{self.TOP}{Path(path).stem}")

    def export(self, path):
        p = Path(path)
        if p.suffix.lower() not in (".step", ".stp", ".igs", ".iges", ".stl", ".3dm", ".obj"):
            raise SWError(f"Rhino can't export {p.suffix} from here (step, iges, stl, 3dm, obj)")
        return FakeBackend.export(self, path)


# ====================================================================== COM base
class _ComCad(Backend):
    """Common plumbing for COM-automated CAD programs (attach to the running instance, lazily)."""
    progids: tuple = ()
    exe = ""

    def __init__(self, lazy=False):
        if sys.platform != "win32":
            raise SWError(f"{self.name} runs on Windows only")
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError as e:
            raise SWError(f"pywin32 isn't usable ({e}). Install it: \"{sys.executable}\" -m pip install pywin32") from e
        self._app = None
        self.last_error = ""
        if not lazy:
            self._attach()
        else:
            try:
                self._attach()
            except SWError:
                pass

    def _attach(self):
        import pythoncom
        import win32com.client
        try:
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        errs = []
        for pid in self.progids:
            try:
                self._app = win32com.client.GetActiveObject(pid)
                break
            except Exception as e:  # noqa: BLE001
                errs.append(f"{pid}: {_com_err(e)}")
        if self._app is None:
            running = bool(_pids(self.exe)) if self.exe else False
            self.last_error = (f"{self.name} is running but didn't answer ({'; '.join(errs)}) — wait until it has "
                               "fully started, and run Daedalus and it at the same privilege level"
                               if running else f"{self.name} isn't running — start it and the Link connects")
            raise SWError(self.last_error)
        self.last_error = ""
        return self._app

    @property
    def appobj(self):
        if self._app is None:
            self._attach()
        return self._app

    def reconnect(self):
        self._app = None
        return self._attach()

    def connected(self):
        try:
            self.app_info()
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = getattr(self, "last_error", "") or str(e)
            self._app = None
            return False

    def export_with_mates(self, out_dir):
        d = self.active_doc()
        if d is None:
            raise SWError(f"No {self.name} document is active")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = _safe(Path(d.title).stem)
        step = self.export(str(out / f"{stem}.step"))
        from ..io import swbridge as B
        props = out / (stem + B.PROPS_SUFFIX)
        props.write_text(json.dumps({"properties": d.properties, "material": d.material, "configuration": "",
                                     "title": d.title}))
        return step, None, str(props)

    def _need_doc(self):
        d = self._doc()
        if d is None:
            raise SWError(f"No document is open in {self.name}")
        return d


def _items(coll):
    """1-based COM collection → list (Count / Item(i)); falls back to iteration."""
    try:
        n = int(_c(coll, "Count"))
        return [coll.Item(i) for i in range(1, n + 1)]
    except Exception:  # noqa: BLE001
        try:
            return list(coll)
        except Exception:  # noqa: BLE001
            return []


# ====================================================================== Inventor
INV_PART, INV_ASM, INV_DRW = 12290, 12291, 12292
INV_VIEWS = {   # Inventor is Y-up: eye direction, up
    "Front": ((0, 0, 1), (0, 1, 0)), "Back": ((0, 0, -1), (0, 1, 0)), "Top": ((0, 1, 0), (0, 0, -1)),
    "Bottom": ((0, -1, 0), (0, 0, 1)), "Right": ((1, 0, 0), (0, 1, 0)), "Left": ((-1, 0, 0), (0, 1, 0)),
    "Isometric": ((1, 1, 1), (0, 1, 0)), "Dimetric": ((1, 0.6, 1), (0, 1, 0)), "Trimetric": ((1, 0.8, 0.6), (0, 1, 0)),
}


class InventorBackend(_ComCad):
    name = "Inventor"
    app = "inventor"
    native_suffix = ".ipt"
    native_key = "inv_path"
    window_prefix = "Autodesk Inventor"
    progids = ("Inventor.Application",)
    exe = "Inventor.exe"

    def _doc(self):
        try:
            return self.appobj.ActiveDocument
        except Exception:  # noqa: BLE001
            return None

    def app_info(self):
        a = self.appobj
        try:
            v = _c(a.SoftwareVersion, "DisplayVersion")
        except Exception:  # noqa: BLE001
            v = "?"
        return {"name": "Inventor", "revision": str(v), "year": None}

    @staticmethod
    def _kind(t):
        return {INV_ASM: ASSEMBLY, INV_DRW: DRAWING}.get(int(t or 0), PART)

    def _props(self, doc):
        out = {}
        for ps in _items(doc.PropertySets):
            sname = str(_c(ps, "Name"))
            if sname not in ("Inventor User Defined Properties", "Design Tracking Properties",
                             "Inventor Summary Information", "Inventor Document Summary Information"):
                continue
            for p in _items(ps):
                try:
                    v = _c(p, "Value")
                    if v not in (None, "") and not hasattr(v, "_oleobj_"):
                        out[str(_c(p, "Name"))] = str(v)
                except Exception:  # noqa: BLE001
                    continue
        return out

    def _mass(self, doc):
        try:
            mp = doc.ComponentDefinition.MassProperties
            c = mp.CenterOfMass
            return (float(mp.Mass), float(mp.Volume) * 1000.0,
                    [float(c.X) * 10, float(c.Y) * 10, float(c.Z) * 10])
        except Exception:  # noqa: BLE001
            return None, None, None

    def _material(self, doc):
        for path in (("ActiveMaterial", "DisplayName"), ("ComponentDefinition", "Material", "Name")):
            try:
                o = doc
                for a in path:
                    o = getattr(o, a)
                return str(o)
            except Exception:  # noqa: BLE001
                continue
        return ""

    def active_doc(self):
        d = self._doc()
        if d is None:
            return None
        kind = self._kind(_c(d, "DocumentType"))
        m, v, c = self._mass(d) if kind != DRAWING else (None, None, None)
        return SWDoc(path=str(_c(d, "FullFileName")), title=str(_c(d, "DisplayName")), kind=kind, configuration="",
                     properties=self._props(d), material=self._material(d), mass_kg=m, volume_mm3=v, center_mm=c)

    def components(self, top_level_only=False):
        d = self._need_doc()
        if self._kind(_c(d, "DocumentType")) != ASSEMBLY:
            return []
        cd = d.ComponentDefinition
        occs = _items(cd.Occurrences if top_level_only else cd.Occurrences.AllLeafOccurrences)
        out = []
        for o in occs:
            try:
                ref = o.Definition.Document
                path = str(_c(ref, "FullFileName"))
                props = self._props(ref)
                mat = self._material(ref)
                mass = self._mass(ref)[0]
            except Exception:  # noqa: BLE001
                path, props, mat, mass = "", {}, "", None
            try:
                parent = str(o.ParentOccurrence.Name) if o.ParentOccurrence is not None else ""
            except Exception:  # noqa: BLE001
                parent = ""
            out.append(SWComponent(name=str(o.Name), path=path, configuration="",
                                   suppressed=bool(getattr(o, "Suppressed", False)),
                                   fixed=bool(getattr(o, "Grounded", False)), properties=props, material=mat,
                                   mass_kg=mass, parent=parent,
                                   is_assembly=int(getattr(o, "DefinitionDocumentType", 0) or 0) == INV_ASM))
        return out

    def selection(self):
        d = self._doc()
        if d is None:
            return []
        out = []
        for i, s in enumerate(_items(d.SelectSet)):
            out.append({"index": i + 1, "type": str(getattr(s, "Type", "")), "component": ""})
        return out

    def _doc_for(self, path):
        if not path:
            return self._need_doc()
        for d in _items(self.appobj.Documents):
            if str(_c(d, "FullFileName")).lower() == str(path).lower():
                return d
        return self.appobj.Documents.Open(str(path), False)

    def get_properties(self, path=None, configuration=""):
        return self._props(self._doc_for(path))

    def set_properties(self, props, path=None, configuration=""):
        d = self._doc_for(path)
        ps = d.PropertySets.Item("Inventor User Defined Properties")
        for k, v in props.items():
            try:
                ps.Item(str(k)).Value = str(v)
            except Exception:  # noqa: BLE001
                ps.Add(str(v), str(k))

    def insert_component(self, path, at_mm=(0, 0, 0)):
        d = self._need_doc()
        if self._kind(_c(d, "DocumentType")) != ASSEMBLY:
            raise SWError("Open an assembly in Inventor first")
        tg = self.appobj.TransientGeometry
        m = tg.CreateMatrix()
        m.SetTranslation(tg.CreateVector(at_mm[0] / 10.0, at_mm[1] / 10.0, at_mm[2] / 10.0))
        occ = d.ComponentDefinition.Occurrences.Add(str(path), m)
        return str(occ.Name)

    def mate_to_selection(self, component, selected, kind="concentric"):
        raise SWError("Add the constraint in Inventor — Daedalus placed the part")

    def open_doc(self, path):
        self.appobj.Documents.Open(str(path), True)
        return self.active_doc()

    def import_step(self, step, save_as):
        d = self.appobj.Documents.Open(str(step), False)
        try:
            d.SaveAs(str(save_as), False)
        finally:
            try:
                d.Close(True)
            except Exception:  # noqa: BLE001
                pass
        return str(save_as)

    def export(self, path):
        d = self._need_doc()
        p = Path(path)
        if p.suffix.lower() not in (".step", ".stp", ".igs", ".iges", ".stl", ".sat", ".pdf", ".dxf", ".dwg"):
            raise SWError(f"Inventor can't save a copy as {p.suffix}")
        d.SaveAs(str(p), True)                                # True: save a copy (the translator picks the format)
        if not p.exists():
            raise SWError(f"Inventor didn't write {p.name}")
        return str(p)

    def rebuild(self):
        self._need_doc().Update()

    def save(self):
        self._need_doc().Save()

    def view(self, name):
        if name not in INV_VIEWS:
            raise SWError(f"Unknown view {name}")
        (dx, dy, dz), up = INV_VIEWS[name]
        v = self.appobj.ActiveView
        cam = v.Camera
        tg = self.appobj.TransientGeometry
        t = cam.Target
        dist = max(1.0, t.DistanceTo(cam.Eye))
        n = (dx * dx + dy * dy + dz * dz) ** 0.5
        cam.Eye = tg.CreatePoint(t.X + dx / n * dist, t.Y + dy / n * dist, t.Z + dz / n * dist)
        cam.UpVector = tg.CreateUnitVector(*up)
        cam.Apply()
        v.Fit()

    def zoom_fit(self):
        self.appobj.ActiveView.Fit()

    def run_macro(self, path, module="", proc="main"):
        raise SWError("Run iLogic rules / VBA macros from Inventor's Manage tab")

    def run_command(self, command_id):
        self.appobj.CommandManager.ControlDefinitions.Item(str(command_id)).Execute()


# ====================================================================== Solid Edge
SE_DRAFT, SE_ASM = 2, 3
SE_VIEWS = {"Front": "Front", "Back": "Back", "Top": "Top", "Bottom": "Bottom", "Right": "Right", "Left": "Left",
            "Isometric": "Iso", "Dimetric": "Dimetric", "Trimetric": "Trimetric"}


class SolidEdgeBackend(_ComCad):
    name = "Solid Edge"
    app = "solidedge"
    native_suffix = ".par"
    native_key = "se_path"
    window_prefix = "Solid Edge"
    progids = ("SolidEdge.Application",)
    exe = "Edge.exe"

    def _doc(self):
        try:
            return self.appobj.ActiveDocument
        except Exception:  # noqa: BLE001
            return None

    def app_info(self):
        return {"name": "Solid Edge", "revision": str(_c(self.appobj, "Version")), "year": None}

    @staticmethod
    def _kind(t):
        return {SE_ASM: ASSEMBLY, SE_DRAFT: DRAWING}.get(int(t or 0), PART)

    def _props(self, doc):
        out = {}
        try:
            sets = _items(doc.Properties)
        except Exception:  # noqa: BLE001
            return out
        for ps in sets:
            for p in _items(ps):
                try:
                    v = _c(p, "Value")
                    if v not in (None, "") and not hasattr(v, "_oleobj_"):
                        out[str(_c(p, "Name"))] = str(v)
                except Exception:  # noqa: BLE001
                    continue
        return out

    def active_doc(self):
        d = self._doc()
        if d is None:
            return None
        return SWDoc(path=str(_c(d, "FullName")), title=str(_c(d, "Name")), kind=self._kind(_c(d, "Type")),
                     configuration="", properties=self._props(d))

    def components(self, top_level_only=False):
        d = self._need_doc()
        if self._kind(_c(d, "Type")) != ASSEMBLY:
            return []
        out = []
        for o in _items(d.Occurrences):
            path = str(getattr(o, "OccurrenceFileName", "") or "")
            props = {}
            try:
                props = self._props(o.OccurrenceDocument)
            except Exception:  # noqa: BLE001
                pass
            out.append(SWComponent(name=str(o.Name), path=path, configuration="",
                                   suppressed=not bool(getattr(o, "Visible", True)),
                                   fixed=bool(getattr(o, "IsGround", False)), properties=props,
                                   is_assembly=bool(getattr(o, "Subassembly", False))))
        return out

    def selection(self):
        try:
            ss = self.appobj.ActiveSelectSet
        except Exception:  # noqa: BLE001
            return []
        return [{"index": i + 1, "type": str(getattr(s, "Type", "")), "component": ""}
                for i, s in enumerate(_items(ss))]

    def _doc_for(self, path):
        if not path:
            return self._need_doc()
        for d in _items(self.appobj.Documents):
            if str(_c(d, "FullName")).lower() == str(path).lower():
                return d
        return self.appobj.Documents.Open(str(path))

    def get_properties(self, path=None, configuration=""):
        return self._props(self._doc_for(path))

    def set_properties(self, props, path=None, configuration=""):
        d = self._doc_for(path)
        ps = d.Properties.Item("Custom")
        for k, v in props.items():
            try:
                ps.Item(str(k)).Value = str(v)
            except Exception:  # noqa: BLE001
                ps.Add(str(k), str(v))
        try:
            ps.Save()
        except Exception:  # noqa: BLE001
            pass

    def insert_component(self, path, at_mm=(0, 0, 0)):
        d = self._need_doc()
        if self._kind(_c(d, "Type")) != ASSEMBLY:
            raise SWError("Open an assembly in Solid Edge first")
        occ = d.Occurrences.AddByFilename(str(path))
        if any(at_mm):
            try:
                occ.Move(at_mm[0] / 1000.0, at_mm[1] / 1000.0, at_mm[2] / 1000.0)
            except Exception:  # noqa: BLE001
                pass
        return str(occ.Name)

    def mate_to_selection(self, component, selected, kind="concentric"):
        raise SWError("Add the relationship in Solid Edge — Daedalus placed the part")

    def open_doc(self, path):
        self.appobj.Documents.Open(str(path))
        return self.active_doc()

    def import_step(self, step, save_as):
        d = self.appobj.Documents.Open(str(step))
        try:
            d.SaveAs(str(save_as))
        finally:
            try:
                d.Close(False)
            except Exception:  # noqa: BLE001
                pass
        return str(save_as)

    def export(self, path):
        d = self._need_doc()
        p = Path(path)
        if p.suffix.lower() not in (".step", ".stp", ".igs", ".iges", ".stl", ".x_t", ".pdf", ".dxf"):
            raise SWError(f"Solid Edge can't save a copy as {p.suffix}")
        d.SaveCopyAs(str(p))
        if not p.exists():
            raise SWError(f"Solid Edge didn't write {p.name}")
        return str(p)

    def rebuild(self):
        d = self._need_doc()
        for m in ("Recompute", "UpdateAll", "Update"):
            if hasattr(d, m):
                try:
                    _c(d, m)
                    return
                except Exception:  # noqa: BLE001
                    continue

    def save(self):
        self._need_doc().Save()

    def view(self, name):
        if name not in SE_VIEWS:
            raise SWError(f"Unknown view {name}")
        v = self.appobj.ActiveWindow.View
        v.ApplyNamedView(SE_VIEWS[name])
        v.Fit()

    def zoom_fit(self):
        self.appobj.ActiveWindow.View.Fit()

    def run_macro(self, path, module="", proc="main"):
        raise SWError("Run Solid Edge macros from Solid Edge (Tools ▸ Macros)")

    def run_command(self, command_id):
        self.appobj.StartCommand(int(command_id))


# ====================================================================== diagnostics
def diagnose(app) -> list:
    if app == "rhino":
        rows = [("Link file", RHINO_LINK.exists(), str(RHINO_LINK) if RHINO_LINK.exists() else
                 f"{RHINO_LINK} missing — the Daedalus script isn't running in Rhino. " + RHINO_SETUP)]
        if RHINO_LINK.exists():
            try:
                v = RhinoBackend(timeout=5)._call("ping", timeout=5) or {}
                rows.append(("Ping", True, f"Rhino {v.get('version', '?')}"))
            except SWError as e:
                rows.append(("Ping", False, f"{e} — if Rhino was restarted, run the script again"))
        return rows
    cls = {"inventor": InventorBackend, "solidedge": SolidEdgeBackend}[app]
    if sys.platform != "win32":
        return [("Windows", False, f"{cls.name} Link needs Windows")]
    rows = []
    try:
        import win32com.client  # noqa: F401
        rows.append(("pywin32", True, "loaded"))
    except Exception as e:  # noqa: BLE001
        return [("pywin32", False, f"{e} — fix: \"{sys.executable}\" -m pip install --upgrade pywin32")]
    pids = _pids(cls.exe)
    rows.append((f"{cls.name} running", bool(pids), f"{cls.exe} pid {', '.join(map(str, pids))}" if pids else
                 f"{cls.exe} not found — start {cls.name}"))
    import win32com.client
    for pid in cls.progids:
        try:
            a = win32com.client.GetActiveObject(pid)
            rows.append((f"Attach {pid}", True, "ok"))
            b = cls.__new__(cls)
            b._app, b.last_error = a, ""
            rows.append(("API call", True, str(b.app_info())))
            d = b.active_doc()
            rows.append(("Active document", None, d.title if d else "none"))
        except Exception as e:  # noqa: BLE001
            rows.append((f"Attach {pid}", False, _com_err(e) + " — Daedalus and the CAD program must run at the "
                                                               "same privilege level"))
    return rows


def connect(app, demo=False, root=None, lazy=False) -> Backend:
    if app == "rhino":
        if demo:
            return FakeRhinoBackend(root or tempfile.mkdtemp(prefix="rhino-demo-"))
        return RhinoBackend(lazy=lazy)
    if demo:
        raise SWError(f"No demo session for {label(app)}")
    if app == "inventor":
        return InventorBackend(lazy=lazy)
    if app == "solidedge":
        return SolidEdgeBackend(lazy=lazy)
    raise SWError(f"Unknown CAD program {app}")
