"""Fusion backend for the CAD Link: talks to the DaedalusLink add-in running inside Fusion.

Components have no files in Fusion (designs live in the cloud), so a component's "path" here is a key
("fusion:<component id>"). Library parts are inserted from STEP directly — no native conversion step.
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .backend import ASSEMBLY, PART, Backend, FakeBackend, SWComponent, SWDoc, SWError

LINK_FILE = Path.home() / ".daedalus" / "fusion-link.json"
KIND = {"part": PART, "assembly": ASSEMBLY}
SETUP = ("Fusion isn't linked. In Daedalus: CAD Link ▸ Install Fusion add-in, then in Fusion: Utilities ▸ "
         "Add-Ins ▸ Scripts and Add-Ins ▸ Add-Ins ▸ DaedalusLink ▸ Run (tick Run on Startup).")


def diagnose() -> list:
    rows = []
    rows.append(("Link file", LINK_FILE.exists(), str(LINK_FILE) if LINK_FILE.exists() else
                 f"{LINK_FILE} missing — the DaedalusLink add-in isn't running in Fusion. " + SETUP))
    if not LINK_FILE.exists():
        return rows
    try:
        cfg = json.loads(LINK_FILE.read_text())
        rows.append(("Add-in port", True, str(cfg.get("port"))))
    except ValueError as e:
        return rows + [("Link file", False, f"unreadable: {e}")]
    try:
        v = FusionBackend(timeout=5)._call("ping", timeout=5) or {}
        rows.append(("Ping", True, f"Fusion {v.get('version', '?')}"))
    except SWError as e:
        rows.append(("Ping", False, f"{e} — if Fusion was restarted, stop and Run the add-in again"))
    return rows


class FusionBackend(Backend):
    """Talks JSON over localhost HTTP to a Daedalus add-in running inside the CAD program (Fusion here; Rhino
    uses the same protocol with its own add-in script)."""
    name = "Fusion"
    label = "Fusion"
    LINK = LINK_FILE
    SETUP = SETUP
    app = "fusion"
    native_suffix = None
    native_key = None
    window_prefix = "Autodesk Fusion"

    def __init__(self, link_file: str | Path | None = None, timeout: float = 120.0, lazy=False):
        self.link_file = Path(link_file or self.LINK)
        self.timeout = timeout
        self._cfg = None
        self._doc_key = None
        self.last_error = ""
        try:
            self._call("ping", timeout=5)
        except SWError as e:
            self.last_error = str(e)
            if not lazy:
                raise

    # transport -----------------------------------------------------------------
    def _config(self):
        if self._cfg is None:
            if not self.link_file.exists():
                raise SWError(self.SETUP)
            try:
                self._cfg = json.loads(self.link_file.read_text())
            except ValueError as e:
                raise SWError(f"Bad {self.label} link file {self.link_file}: {e}") from None
        return self._cfg

    def _call(self, op, timeout=None, **args):
        cfg = self._config()
        body = json.dumps({"op": op, "timeout": timeout or self.timeout, **args}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Daedalus-Token": cfg["token"]})
        try:
            with urllib.request.urlopen(req, timeout=(timeout or self.timeout) + 5) as r:
                out = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read().decode())
            except ValueError:
                raise SWError(f"{self.label} add-in error {e.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            self._cfg = None                                  # restarted → new port/token
            raise SWError(f"{self.label} isn't answering — is it running with the DaedalusLink add-in? "
                          + self.SETUP) from None
        if not out.get("ok"):
            raise SWError(out.get("error") or f"{self.label} add-in error")
        return out.get("data")

    # interface -------------------------------------------------------------------
    def connected(self):
        try:
            self._call("ping", timeout=5)
            self.last_error = ""
            return True
        except SWError as e:
            self.last_error = str(e)
            return False

    def reconnect(self):
        self._cfg = None
        return self.connected()

    def app_info(self):
        v = self._call("ping", timeout=5) or {}
        return {"name": self.label, "revision": v.get("version", "?"), "year": None}

    def active_doc(self):
        d = self._call("active_doc")
        if not d:
            return None
        self._doc_key = d.get("key")
        return SWDoc(path=d["path"], title=d["title"], kind=KIND.get(d["kind"], PART),
                     configuration=d.get("configuration", ""), properties=d.get("properties") or {},
                     material=d.get("material", ""), mass_kg=d.get("mass_kg"), volume_mm3=d.get("volume_mm3"),
                     center_mm=d.get("center_mm"))

    def components(self, top_level_only=False):
        return [SWComponent(name=c["name"], path=c["path"], configuration="", suppressed=c["suppressed"],
                            fixed=c["fixed"], properties=c.get("properties") or {}, material=c.get("material", ""),
                            mass_kg=c.get("mass_kg"), parent=c.get("parent", ""), is_assembly=c["is_assembly"])
                for c in self._call("components", top_level_only=top_level_only) or []]

    def selection(self):
        return self._call("selection") or []

    def _key(self, path):
        if not path or str(path).startswith("fusion://"):
            return None                                       # the active design's root component
        if str(path).lower().endswith((".step", ".stp")):     # a STEP we just inserted → its component
            return getattr(self, "_last_inserted_key", None)
        return path

    def get_properties(self, path=None, configuration=""):
        return self._call("get_properties", key=self._key(path))

    def set_properties(self, props, path=None, configuration=""):
        self._call("set_properties", key=self._key(path), properties=props)

    def insert_component(self, path, at_mm=(0, 0, 0)):
        if Path(path).suffix.lower() not in (".step", ".stp"):
            raise SWError("Fusion inserts STEP files")
        r = self._call("insert_step", path=str(path), at_mm=list(at_mm), name=Path(path).stem)
        self._last_inserted_key = r.get("key")
        return r["name"]

    def mate_to_selection(self, component, selected, kind="concentric"):
        if not selected.get("token"):
            raise SWError("Select a face in Fusion first")
        return self._call("joint", occurrence=component, token=selected["token"])["joints"]

    def open_doc(self, path):
        if Path(path).suffix.lower() not in (".step", ".stp"):
            raise SWError("Fusion opens STEP files from here (designs open from the Fusion data panel)")
        self._call("open_step", path=str(path))
        return self.active_doc()

    def import_step(self, step, save_as):
        return str(step)                                      # Fusion inserts STEP directly

    def export(self, path):
        self._call("export", path=str(path))
        if not Path(path).exists():
            raise SWError(f"Fusion didn't write {Path(path).name}")
        return str(path)

    def export_with_mates(self, out_dir):
        d = self.active_doc()
        if d is None:
            raise SWError("No Fusion design is active")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = _safe(d.title)
        step = self.export(str(out / f"{stem}.step"))
        from ..io import swbridge as B
        props = out / (stem + B.PROPS_SUFFIX)
        props.write_text(json.dumps({"properties": d.properties, "material": d.material, "configuration": "",
                                     "title": d.title}))
        return step, None, str(props)

    def rebuild(self):
        self._call("rebuild")

    def save(self):
        self._call("save")

    def view(self, name):
        self._call("view", name=name)

    def zoom_fit(self):
        self._call("fit")

    def run_macro(self, path, module="", proc="main"):
        raise SWError("Fusion scripts run from Fusion's Scripts and Add-Ins dialog")

    def run_command(self, command_id):
        self._call("command", id=str(command_id))


def _safe(name):
    import re
    return re.sub(r'[<>:"/\\|?*]+', "-", str(name)).strip() or "design"


class FakeFusionBackend(FakeBackend):
    """Simulated Fusion session (a cam-lock fixture design) with Fusion's conventions: component keys instead
    of files, STEP inserted directly, joints instead of mates, string command ids."""
    name = "Fusion (demo)"
    app = "fusion"
    native_suffix = None
    native_key = None
    window_prefix = "Autodesk Fusion"
    KEY, TOP, LABEL = "fusion:", "fusion://demo/", "Fusion"

    def __init__(self, root=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="fusion-demo-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = []
        self._sel = []
        self.files = {}
        self.docs = {}
        self.comps = []
        top = f"{self.TOP}Cam Lock Fixture"
        self.active = top
        self.docs[top] = SWDoc(top, "Cam Lock Fixture v3", ASSEMBLY, configuration="",
                               properties={"Description": "Quick-release cam lock fixture"})
        self.files[top] = {"size": (300, 160, 90)}
        for name, size, props, mat, mass, n in (
                ("Base Plate", (300, 160, 12), {"PartNo": "RG-5101", "Description": "Fixture base plate"},
                 "Aluminum 6061", 1.55, 1),
                ("Cam Lever", (120, 30, 10), {"PartNo": "RG-5102", "Description": "Cam lever"}, "Steel", 0.21, 2),
                ("Pivot Pin", (10, 10, 40), {"Description": "Pivot pin D10"}, "Steel", 0.025, 2)):
            key = f"{self.KEY}{name.replace(' ', '')}"
            self.docs[key] = SWDoc(key, name, PART, configuration="", properties=dict(props), material=mat,
                                   mass_kg=mass)
            self.files[key] = {"size": size}
            for i in range(n):
                self.comps.append(SWComponent(f"{name}:{i + 1}", key, configuration="",
                                              properties=self.docs[key].properties, material=mat, mass_kg=mass,
                                              fixed=(name == "Base Plate")))

    def app_info(self):
        return {"name": "Fusion (demo)", "revision": "2.0.21000", "year": None}

    def insert_component(self, path, at_mm=(0, 0, 0)):
        if Path(path).suffix.lower() not in (".step", ".stp"):
            raise SWError("Fusion inserts STEP files")
        key = f"{self.KEY}{Path(path).stem}"
        if key not in self.docs:
            self.docs[key] = SWDoc(key, Path(path).stem, PART, configuration="")
        n = sum(1 for c in self.comps if c.path == key) + 1
        name = f"{Path(path).stem}:{n}"
        d = self.docs[key]
        self.comps.append(SWComponent(name, key, configuration="", properties=d.properties, material=d.material,
                                      mass_kg=d.mass_kg))
        self._last_inserted_key = key
        self.log.append(f"insert {name}")
        return name

    def set_properties(self, props, path=None, configuration=""):
        if path and str(path).lower().endswith((".step", ".stp")):   # a just-inserted STEP: its component
            path = getattr(self, "_last_inserted_key", None) or f"{self.KEY}{Path(path).stem}"
        super().set_properties(props, path, configuration)

    def mate_to_selection(self, component, selected, kind="concentric"):
        j = f"{'Revolute' if selected.get('surface') == 'cylinder' else 'Rigid'}{len(self.log) + 1}"
        self.log.append(f"joint {j} {component} → {selected.get('component')}")
        self._sel = []
        return [j]

    def select_hole(self, component="Base Plate:1", radius_mm=5.0):
        self._sel = [{"index": 1, "type": "adsk::fusion::BRepFace", "component": component, "surface": "cylinder",
                      "radius_mm": radius_mm, "axis": [0, 0, 1], "point_mm": [0, 0, 0], "token": "demo-token"}]

    def open_doc(self, path):
        if Path(path).suffix.lower() not in (".step", ".stp"):
            raise SWError("Fusion opens STEP files from here (designs open from the Fusion data panel)")
        key = f"{self.TOP}{Path(path).stem}"
        self.docs[key] = SWDoc(key, Path(path).stem, PART, configuration="")
        self.files[key] = {"size": (60, 40, 20)}
        self.active = key
        self.log.append(f"open {Path(path).name}")
        return self.docs[key]

    def import_step(self, step, save_as):
        return str(step)

    def export(self, path):
        p = Path(path)
        if p.suffix.lower() not in (".step", ".stp", ".stl", ".f3d", ".iges", ".igs"):
            raise SWError(f"Fusion can't export {p.suffix} from a design (step, stl, f3d, iges)")
        return super().export(path)

    def run_macro(self, path, module="", proc="main"):
        raise SWError("Fusion scripts run from Fusion's Scripts and Add-Ins dialog")

    def run_command(self, command_id):
        self.log.append(f"command {command_id}")
