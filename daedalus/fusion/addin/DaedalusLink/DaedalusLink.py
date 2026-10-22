"""Daedalus CAD Link — Fusion add-in.

Fusion's API only runs inside Fusion, on its main thread. This add-in listens on 127.0.0.1 (random port, a
token required on every request) and hands each request to Fusion's main thread through a custom event.
Daedalus finds the port and token in ~/.daedalus/fusion-link.json, written while the add-in runs.

Lengths over the wire are millimetres and masses kilograms (Fusion's API works in centimetres).
Written against the Fusion API reference; first run on a real Fusion install is the test — every request
returns the exception text of the call that failed.
"""
import json
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import adsk.core
import adsk.fusion

EVENT_ID = "DaedalusLinkRequest"
ATTR_GROUP = "Daedalus"
LINK_FILE = os.path.join(os.path.expanduser("~"), ".daedalus", "fusion-link.json")
CM = 10.0                                   # mm per cm

_app = None
_ui = None
_handlers = []
_server = None
_event = None
_token = None
_pending = {}


# ----------------------------------------------------------------- helpers
def _design():
    d = adsk.fusion.Design.cast(_app.activeProduct)
    if d is None:
        raise RuntimeError("No Fusion design is active (switch to the Design workspace)")
    return d


def _props(comp):
    out = {}
    try:
        if comp.partNumber:
            out["PartNo"] = comp.partNumber
    except Exception:  # noqa: BLE001
        pass
    try:
        if comp.description:
            out["Description"] = comp.description
    except Exception:  # noqa: BLE001
        pass
    try:
        for a in comp.attributes.itemsByGroup(ATTR_GROUP) or []:
            out[a.name] = a.value
    except Exception:  # noqa: BLE001
        pass
    return out


def _mass(comp):
    try:
        pp = comp.getPhysicalProperties(adsk.fusion.CalculationAccuracy.LowCalculationAccuracy)
        c = pp.centerOfMass
        return float(pp.mass), float(pp.volume) * 1000.0, [c.x * CM, c.y * CM, c.z * CM]
    except Exception:  # noqa: BLE001
        return None, None, None


def _material(comp):
    try:
        if comp.material:
            return comp.material.name
    except Exception:  # noqa: BLE001
        pass
    try:
        b = comp.bRepBodies.item(0) if comp.bRepBodies.count else None
        return b.material.name if b and b.material else ""
    except Exception:  # noqa: BLE001
        return ""


def _key(comp):
    try:
        return "fusion:" + comp.id
    except Exception:  # noqa: BLE001
        return "fusion:" + comp.name


def _component(design, key):
    if not key or key in ("root", design.rootComponent.name) or key == _key(design.rootComponent):
        return design.rootComponent
    for c in design.allComponents:
        if _key(c) == key or c.name == key:
            return c
    raise RuntimeError(f"Component {key} not found")


def _occurrence(design, name):
    for o in design.rootComponent.allOccurrences:
        if o.fullPathName == name or o.name == name:
            return o
    raise RuntimeError(f"Occurrence {name} not found")


def _doc_path(doc):
    try:
        if doc.dataFile:
            return "fusion://" + doc.dataFile.id
    except Exception:  # noqa: BLE001
        pass
    return "fusion://unsaved/" + doc.name


# ----------------------------------------------------------------- operations
def op_ping(_):
    return {"version": _app.version, "user": getattr(_app.currentUser, "displayName", "")}


def op_active_doc(_):
    d = adsk.fusion.Design.cast(_app.activeProduct)
    if d is None:
        return None
    doc = _app.activeDocument
    root = d.rootComponent
    mass, vol, c = _mass(root)
    return {"path": _doc_path(doc), "title": doc.name, "kind": "assembly" if root.occurrences.count else "part",
            "configuration": "", "properties": _props(root), "material": _material(root), "mass_kg": mass,
            "volume_mm3": vol, "center_mm": c, "key": _key(root)}


def op_components(req):
    d = _design()
    occs = d.rootComponent.occurrences if req.get("top_level_only") else d.rootComponent.allOccurrences
    out = []
    for o in occs:
        comp = o.component
        parent = ""
        try:
            if o.assemblyContext:
                parent = o.assemblyContext.fullPathName
        except Exception:  # noqa: BLE001
            pass
        out.append({"name": o.fullPathName, "path": _key(comp), "configuration": "",
                    "suppressed": not o.isLightBulbOn, "fixed": bool(o.isGrounded), "properties": _props(comp),
                    "material": _material(comp), "mass_kg": _mass(comp)[0], "parent": parent,
                    "is_assembly": comp.occurrences.count > 0, "component": comp.name})
    return out


def op_selection(_):
    out = []
    sels = _ui.activeSelections
    for i in range(sels.count):
        ent = sels.item(i).entity
        item = {"index": i + 1, "type": ent.objectType, "component": ""}
        face = adsk.fusion.BRepFace.cast(ent)
        if face:
            item["token"] = face.entityToken
            try:
                if face.assemblyContext:
                    item["component"] = face.assemblyContext.fullPathName
            except Exception:  # noqa: BLE001
                pass
            g = face.geometry
            if g.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
                cyl = adsk.core.Cylinder.cast(g)
                item.update({"surface": "cylinder", "radius_mm": cyl.radius * CM,
                             "axis": [cyl.axis.x, cyl.axis.y, cyl.axis.z],
                             "point_mm": [cyl.origin.x * CM, cyl.origin.y * CM, cyl.origin.z * CM]})
            elif g.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
                p = face.pointOnFace
                item.update({"surface": "plane", "point_mm": [p.x * CM, p.y * CM, p.z * CM]})
            else:
                item["surface"] = "other"
        out.append(item)
    return out


def op_get_properties(req):
    return _props(_component(_design(), req.get("key")))


def op_set_properties(req):
    comp = _component(_design(), req.get("key"))
    for k, v in (req.get("properties") or {}).items():
        v = "" if v is None else str(v)
        if k.lower() in ("partno", "part number", "partnumber"):
            comp.partNumber = v
        elif k.lower() == "description":
            comp.description = v
        else:
            comp.attributes.add(ATTR_GROUP, str(k), v)
    return {"ok": True}


def op_insert_step(req):
    d = _design()
    root = d.rootComponent
    before = {o.entityToken for o in root.occurrences}
    im = _app.importManager
    opts = im.createSTEPImportOptions(req["path"])
    ok = im.importToTarget(opts, root)
    if not ok:
        raise RuntimeError("Fusion didn't import " + os.path.basename(req["path"]))
    new = [o for o in root.occurrences if o.entityToken not in before]
    if not new:
        raise RuntimeError("Import finished but no new component appeared")
    occ = new[-1]
    at = req.get("at_mm") or [0, 0, 0]
    if any(at):
        m = adsk.core.Matrix3D.create()
        m.translation = adsk.core.Vector3D.create(at[0] / CM, at[1] / CM, at[2] / CM)
        occ.transform2 = m
    if req.get("name"):
        try:
            occ.component.name = req["name"]
        except Exception:  # noqa: BLE001
            pass
    return {"name": occ.fullPathName, "key": _key(occ.component)}


def op_joint(req):
    """Joint between the inserted occurrence and the face the user selected (cylinder → revolute on the
    axis, flat face → rigid on the face centre)."""
    d = _design()
    root = d.rootComponent
    target = d.findEntityByToken(req["token"])
    if not target:
        raise RuntimeError("The selected face is gone — select it again")
    target = adsk.fusion.BRepFace.cast(target[0])
    occ = _occurrence(d, req["occurrence"])
    want_cyl = target.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType
    r_target = adsk.core.Cylinder.cast(target.geometry).radius if want_cyl else 0
    best, score = None, None
    for body in occ.bRepBodies:
        for f in body.faces:
            st = f.geometry.surfaceType
            if want_cyl and st == adsk.core.SurfaceTypes.CylinderSurfaceType:
                sc = abs(adsk.core.Cylinder.cast(f.geometry).radius - r_target)
            elif not want_cyl and st == adsk.core.SurfaceTypes.PlaneSurfaceType:
                sc = -f.area
            else:
                continue
            if score is None or sc < score:
                best, score = f, sc
    if best is None:
        raise RuntimeError(f"{occ.name} has no {'round' if want_cyl else 'flat'} face to joint")
    kp = adsk.fusion.JointKeyPointTypes.CenterKeyPoint
    if want_cyl:
        g_new = adsk.fusion.JointGeometry.createByNonPlanarFace(best, kp)
        g_tgt = adsk.fusion.JointGeometry.createByNonPlanarFace(target, kp)
    else:
        g_new = adsk.fusion.JointGeometry.createByPlanarFace(best, None, kp)
        g_tgt = adsk.fusion.JointGeometry.createByPlanarFace(target, None, kp)
    inp = root.joints.createInput(g_new, g_tgt)
    if want_cyl:
        inp.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    else:
        inp.setAsRigidJointMotion()
    j = root.joints.add(inp)
    return {"joints": [j.name]}


def op_export(req):
    d = _design()
    path = req["path"]
    root = d.rootComponent
    em = d.exportManager
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        opts = em.createSTEPExportOptions(path, root)
    elif ext == ".stl":
        opts = em.createSTLExportOptions(root, path)
    elif ext == ".f3d":
        opts = em.createFusionArchiveExportOptions(path, root)
    elif ext in (".iges", ".igs"):
        opts = em.createIGESExportOptions(path, root)
    else:
        raise RuntimeError(f"Fusion can't export {ext} from a design (step, stl, f3d, iges)")
    if not em.execute(opts):
        raise RuntimeError("Export failed")
    return {"path": path}


def op_open_step(req):
    im = _app.importManager
    opts = im.createSTEPImportOptions(req["path"])
    doc = im.importToNewDocument(opts)
    return {"title": doc.name if doc else os.path.basename(req["path"])}


def op_rebuild(_):
    _design().computeAll()
    return {"ok": True}


def op_save(_):
    doc = _app.activeDocument
    if not doc.isSaved and not doc.dataFile:
        raise RuntimeError("Save the design once in Fusion first (it needs a project/folder)")
    if not doc.save("Saved from Daedalus"):
        raise RuntimeError("Fusion didn't save")
    return {"ok": True}


VIEWS = {"Front": "FrontViewOrientation", "Back": "BackViewOrientation", "Left": "LeftViewOrientation",
         "Right": "RightViewOrientation", "Top": "TopViewOrientation", "Bottom": "BottomViewOrientation",
         "Isometric": "IsoTopRightViewOrientation"}


def op_view(req):
    vp = _app.activeViewport
    cam = vp.camera
    cam.viewOrientation = getattr(adsk.core.ViewOrientations, VIEWS[req["name"]])
    cam.isFitView = True
    vp.camera = cam
    return {"ok": True}


def op_fit(_):
    _app.activeViewport.fit()
    return {"ok": True}


def op_command(req):
    cd = _ui.commandDefinitions.itemById(str(req["id"]))
    if cd is None:
        raise RuntimeError(f"No Fusion command '{req['id']}'")
    cd.execute()
    return {"ok": True}


OPS = {k[3:]: v for k, v in globals().items() if k.startswith("op_")}


# ----------------------------------------------------------------- plumbing
class _OnRequest(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            rid = json.loads(args.additionalInfo)["id"]
        except Exception:  # noqa: BLE001
            return
        job = _pending.get(rid)
        if job is None:
            return
        try:
            fn = OPS.get(job["req"].get("op"))
            if fn is None:
                raise RuntimeError(f"Unknown operation {job['req'].get('op')}")
            job["result"] = {"ok": True, "data": fn(job["req"])}
        except Exception as e:  # noqa: BLE001
            job["result"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        job["done"].set()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.headers.get("X-Daedalus-Token") != _token:
            return self._reply(403, {"ok": False, "error": "bad token"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._reply(400, {"ok": False, "error": "bad JSON"})
        rid = secrets.token_hex(8)
        job = {"req": req, "done": threading.Event(), "result": None}
        _pending[rid] = job
        try:
            _app.fireCustomEvent(EVENT_ID, json.dumps({"id": rid}))
            if not job["done"].wait(float(req.get("timeout", 120))):
                return self._reply(504, {"ok": False, "error": "Fusion is busy (timed out) — finish the open "
                                                               "command and try again"})
            return self._reply(200, job["result"])
        finally:
            _pending.pop(rid, None)


def run(context):
    global _app, _ui, _server, _event, _token
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        _event = _app.registerCustomEvent(EVENT_ID)
        h = _OnRequest()
        _event.add(h)
        _handlers.append(h)
        _token = secrets.token_urlsafe(24)
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        os.makedirs(os.path.dirname(LINK_FILE), exist_ok=True)
        with open(LINK_FILE, "w") as f:
            json.dump({"port": _server.server_address[1], "token": _token, "pid": os.getpid()}, f)
    except Exception:  # noqa: BLE001
        if _ui:
            _ui.messageBox("Daedalus Link failed to start:\n" + traceback.format_exc())


def stop(context):
    global _server
    try:
        if _server:
            _server.shutdown()
            _server = None
        if _app:
            _app.unregisterCustomEvent(EVENT_ID)
        if os.path.exists(LINK_FILE):
            os.remove(LINK_FILE)
    except Exception:  # noqa: BLE001
        pass
