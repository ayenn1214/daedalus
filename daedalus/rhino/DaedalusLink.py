#! python3
# -*- coding: utf-8 -*-
"""Daedalus Link for Rhino (Rhino 7 IronPython and Rhino 8 Python 3).

Run it once per Rhino session:  _-RunPythonScript "<this file>"   (CAD Link ▸ Install Rhino plug-in shows the
exact command; put it in Options ▸ General ▸ "Run these commands every time Rhino starts" to make it permanent).

It listens on 127.0.0.1 (random port, a token on every request), runs each request on Rhino's UI thread and
answers JSON — the same protocol as the Fusion add-in. Daedalus finds the port and token in
~/.daedalus/rhino-link.json. Running the script again restarts the listener.
"""
import binascii
import json
import os
import threading
import traceback

try:                                            # Rhino 8 (CPython 3)
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:                             # Rhino 7 (IronPython 2.7)
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

import Rhino
import scriptcontext as sc
import System

LINK_FILE = os.path.join(os.path.expanduser("~"), ".daedalus", "rhino-link.json")
STICKY = "daedalus_link_server"
SOLID_TYPES = None


def _doc():
    d = Rhino.RhinoDoc.ActiveDoc
    if d is None:
        raise RuntimeError("No Rhino document is open")
    return d


def _k(doc):
    """Model units → millimetres."""
    return Rhino.RhinoMath.UnitScale(doc.ModelUnitSystem, Rhino.UnitSystem.Millimeters)


def _types():
    T = Rhino.DocObjects.ObjectType
    return (T.Brep, T.Extrusion, T.Mesh, T.SubD, T.InstanceReference)


def _objects(doc):
    s = Rhino.DocObjects.ObjectEnumeratorSettings()
    s.NormalObjects = True
    s.LockedObjects = True
    s.HiddenObjects = True
    return [o for o in doc.Objects.GetObjectList(s) if o.ObjectType in _types()]


def _user_strings(nvc):
    out = {}
    if nvc is None:
        return out
    for key in nvc.AllKeys:
        out[str(key)] = str(nvc.Get(key))
    return out


def _doc_strings(doc):
    out = {}
    for i in range(doc.Strings.Count):
        key = doc.Strings.GetKey(i)
        if key and not str(key).startswith("."):
            out[str(key)] = str(doc.Strings.GetValue(i))
    return out


def _brep_of(o):
    g = o.Geometry
    if isinstance(g, Rhino.Geometry.Extrusion):
        return g.ToBrep()
    if isinstance(g, Rhino.Geometry.Brep):
        return g
    return None


def _volume(objs, k):
    vol, cx, cy, cz = 0.0, 0.0, 0.0, 0.0
    for o in objs:
        b = _brep_of(o)
        if b is None or not b.IsSolid:
            continue
        mp = Rhino.Geometry.VolumeMassProperties.Compute(b)
        if mp is None:
            continue
        v = mp.Volume
        c = mp.Centroid
        vol += v
        cx += c.X * v
        cy += c.Y * v
        cz += c.Z * v
    if vol <= 0:
        return None, None
    return vol * k ** 3, [cx / vol * k, cy / vol * k, cz / vol * k]


def _name(o, i):
    if o.ObjectType == Rhino.DocObjects.ObjectType.InstanceReference:
        return "%s:%d" % (o.InstanceDefinition.Name, i)
    n = o.Attributes.Name
    if n:
        return str(n)
    layer = Rhino.RhinoDoc.ActiveDoc.Layers[o.Attributes.LayerIndex]
    return "%s:%d" % (layer.Name, i)


def _key(o):
    if o.ObjectType == Rhino.DocObjects.ObjectType.InstanceReference:
        return "rhino:block:" + str(o.InstanceDefinition.Name)
    return "rhino:obj:" + str(o.Id)


def _find(doc, key):
    if key.startswith("rhino:obj:"):
        o = doc.Objects.FindId(System.Guid(key[len("rhino:obj:"):]))
        if o is None:
            raise RuntimeError("Object not found: " + key)
        return "obj", o
    if key.startswith("rhino:block:"):
        idef = doc.InstanceDefinitions.Find(key[len("rhino:block:"):])
        if idef is None:
            raise RuntimeError("Block not found: " + key)
        return "block", idef
    raise RuntimeError("Unknown key " + key)


def _run(cmd):
    if not Rhino.RhinoApp.RunScript(cmd, False):
        raise RuntimeError("Rhino didn't run: " + cmd + " (finish the active command first)")


def _q(path):
    return '"' + str(path).replace('"', "") + '"'


# ------------------------------------------------------------------- operations
def op_ping(_):
    return {"version": str(Rhino.RhinoApp.Version)}


def op_active_doc(_):
    doc = Rhino.RhinoDoc.ActiveDoc
    if doc is None:
        return None
    objs = _objects(doc)
    blocks = [o for o in objs if o.ObjectType == Rhino.DocObjects.ObjectType.InstanceReference]
    vol, c = _volume(objs, _k(doc))
    title = str(doc.Name or "Untitled")
    return {"path": str(doc.Path) if doc.Path else "rhino://" + title, "title": title,
            "kind": "assembly" if (blocks or len(objs) > 1) else "part", "configuration": "",
            "properties": _doc_strings(doc), "material": "", "mass_kg": None, "volume_mm3": vol, "center_mm": c,
            "key": None}


def op_components(req):
    doc = _doc()
    out, count = [], {}
    k = _k(doc)
    for o in _objects(doc):
        key = _key(o)
        count[key] = count.get(key, 0) + 1
        props = _user_strings(o.Attributes.GetUserStrings())
        if o.ObjectType == Rhino.DocObjects.ObjectType.InstanceReference:
            try:
                props.update(_user_strings(o.InstanceDefinition.GetUserStrings()))
            except Exception:  # noqa: BLE001
                pass
        vol, _c = _volume([o], k)
        out.append({"name": _name(o, count[key]), "path": key, "configuration": "", "suppressed": bool(o.IsHidden),
                    "fixed": bool(o.IsLocked), "properties": props, "material": "", "mass_kg": None,
                    "volume_mm3": vol, "parent": "", "is_assembly": False})
    return out


def op_selection(_):
    doc = _doc()
    out = []
    for i, o in enumerate(doc.Objects.GetSelectedObjects(False, False)):
        out.append({"index": i + 1, "type": str(o.ObjectType), "component": _name(o, 1), "id": str(o.Id)})
    return out


def op_get_properties(req):
    doc = _doc()
    key = req.get("key")
    if not key:
        return _doc_strings(doc)
    kind, it = _find(doc, key)
    if kind == "obj":
        return _user_strings(it.Attributes.GetUserStrings())
    return _user_strings(it.GetUserStrings())


def op_set_properties(req):
    doc = _doc()
    key = req.get("key")
    props = req.get("properties") or {}
    if not key:
        for n, v in props.items():
            doc.Strings.SetString(str(n), str(v))
        return True
    kind, it = _find(doc, key)
    if kind == "obj":
        attr = it.Attributes.Duplicate()
        for n, v in props.items():
            attr.SetUserString(str(n), str(v))
        doc.Objects.ModifyAttributes(it, attr, True)
    else:
        for n, v in props.items():
            it.SetUserString(str(n), str(v))
    return True


def _import(path, at_mm=None, name=None):
    doc = _doc()
    before = set(str(o.Id) for o in _objects(doc))
    _run("_-Import " + _q(path) + " _Enter _Enter")
    new = [o for o in _objects(doc) if str(o.Id) not in before]
    if not new:
        raise RuntimeError("Rhino didn't import " + os.path.basename(path))
    if at_mm and any(at_mm):
        k = _k(doc)
        xf = Rhino.Geometry.Transform.Translation(at_mm[0] / k, at_mm[1] / k, at_mm[2] / k)
        for o in new:
            doc.Objects.Transform(o, xf, True)
        new = [o for o in _objects(doc) if str(o.Id) not in before]
    if name:
        for i, o in enumerate(new):
            attr = o.Attributes.Duplicate()
            attr.Name = name if len(new) == 1 else "%s-%d" % (name, i + 1)
            doc.Objects.ModifyAttributes(o, attr, True)
    doc.Views.Redraw()
    return new


def op_insert_step(req):
    new = _import(req["path"], req.get("at_mm"), req.get("name"))
    return {"name": _name(new[0], 1), "key": _key(new[0])}


def op_joint(_):
    raise RuntimeError("Rhino has no mates or joints — the part was placed; move it with Gumball")


def op_open_step(req):
    _import(req["path"], None, os.path.splitext(os.path.basename(req["path"]))[0])
    return True


def op_export(req):
    doc = _doc()
    path = req["path"]
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".step", ".stp", ".igs", ".iges", ".stl", ".3dm", ".obj"):
        raise RuntimeError("Rhino can't export " + ext + " from here (step, iges, stl, 3dm, obj)")
    was = [o.Id for o in doc.Objects.GetSelectedObjects(False, False)]
    doc.Objects.UnselectAll()
    for o in _objects(doc):
        if not o.IsHidden:
            doc.Objects.Select(o.Id, True)
    try:
        _run("_-Export " + _q(path) + " _Enter _Enter")
    finally:
        doc.Objects.UnselectAll()
        for i in was:
            doc.Objects.Select(i, True)
        doc.Views.Redraw()
    if not os.path.exists(path):
        raise RuntimeError("Rhino didn't write " + os.path.basename(path))
    return path


def op_rebuild(_):
    _doc().Views.Redraw()
    return True


def op_save(_):
    _run("_-Save _Enter")
    return True


VIEWS = {"Front": "_Front", "Back": "_Back", "Left": "_Left", "Right": "_Right", "Top": "_Top",
         "Bottom": "_Bottom", "Isometric": "_Perspective", "Trimetric": "_Perspective", "Dimetric": "_Perspective"}


def op_view(req):
    v = VIEWS.get(req.get("name"))
    if v is None:
        raise RuntimeError("Unknown view " + str(req.get("name")))
    _run("_-SetView _World " + v)
    return True


def op_fit(_):
    _run("_Zoom _All _Extents")
    return True


def op_command(req):
    _run(str(req.get("id")))
    return True


OPS = dict((k[3:], v) for k, v in list(globals().items()) if k.startswith("op_"))


# ------------------------------------------------------------------- plumbing
def _on_ui(fn, req, timeout):
    done = threading.Event()
    box = {}

    def run():
        try:
            box["r"] = {"ok": True, "data": fn(req)}
        except Exception as e:  # noqa: BLE001
            box["r"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        finally:
            done.set()
    Rhino.RhinoApp.InvokeOnUiThread(System.Action(run))
    if not done.wait(timeout):
        return {"ok": False, "error": "Rhino is busy (timed out) — finish the active command and try again"}
    return box["r"]


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    token = None

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj)
        if not isinstance(b, bytes):
            b = b.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.headers.get("X-Daedalus-Token") != _Handler.token:
            return self._reply(403, {"ok": False, "error": "bad token"})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            req = json.loads(raw)
        except ValueError:
            return self._reply(400, {"ok": False, "error": "bad JSON"})
        fn = OPS.get(req.get("op"))
        if fn is None:
            return self._reply(200, {"ok": False, "error": "Unknown operation %s" % req.get("op")})
        try:
            return self._reply(200, _on_ui(fn, req, float(req.get("timeout", 120))))
        except Exception:  # noqa: BLE001
            return self._reply(500, {"ok": False, "error": traceback.format_exc()})


def stop():
    old = sc.sticky.get(STICKY)
    if old is not None:
        try:
            old.shutdown()
            old.server_close()
        except Exception:  # noqa: BLE001
            pass
        sc.sticky[STICKY] = None
    try:
        if os.path.exists(LINK_FILE):
            os.remove(LINK_FILE)
    except OSError:
        pass


def start():
    stop()
    _Handler.token = binascii.hexlify(os.urandom(18)).decode("ascii")
    srv = _Server(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    sc.sticky[STICKY] = srv
    folder = os.path.dirname(LINK_FILE)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(LINK_FILE, "w") as f:
        json.dump({"port": srv.server_address[1], "token": _Handler.token, "pid": os.getpid()}, f)
    Rhino.RhinoApp.WriteLine("Daedalus Link: listening on 127.0.0.1:%d" % srv.server_address[1])


if __name__ == "__main__":
    try:
        start()
    except Exception:  # noqa: BLE001
        Rhino.RhinoApp.WriteLine("Daedalus Link failed to start:\n" + traceback.format_exc())
