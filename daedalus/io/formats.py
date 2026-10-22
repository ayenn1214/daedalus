"""File import / export. Qt-free.

Imports
  STEP (.step .stp)   assembly tree → one Daedalus object per part, with name, colour and placement
  IGES (.iges .igs)   surfaces/solids (sewn into a solid when closed)
  BREP (.brep)        OpenCASCADE native
  STL / OBJ / 3MF     triangle meshes → sewn, face-merged solids (so you can sketch on and cut them)
  DXF (.dxf)          2D lines/arcs/circles/polylines/splines → an editable Sketch
  SolidWorks          via io/swbridge.py (drives SolidWorks to write STEP + mates)

Exports
  STEP (assembly: object names, colours, placements) · IGES · BREP · STL · OBJ · 3MF · glTF/GLB
  DXF (a sketch's curves, in its plane)

Imported solids are stored *inside* the .daedalus file (compressed BREP), so files stay
self-contained when the original STEP/STL moves.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

IMPORT_EXT = {
    ".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges", ".brep": "brep",
    ".stl": "stl", ".obj": "obj", ".3mf": "3mf", ".dxf": "dxf",
    ".sldprt": "solidworks", ".sldasm": "solidworks",
}
EXPORT_EXT = {
    ".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges", ".brep": "brep",
    ".stl": "stl", ".obj": "obj", ".3mf": "3mf", ".gltf": "gltf", ".glb": "glb", ".dxf": "dxf",
    ".ply": "ply", ".wrl": "vrml", ".vrml": "vrml",
}
STEP_SCHEMAS = {"AP214": "AP214IS", "AP203": "AP203", "AP242": "AP242DIS"}
EXPORT_DEFAULTS = {
    "step_schema": "AP214",       # AP203 (geometry only) | AP214 (colours, names; widest support) | AP242 (PMI era)
    "units": "mm",                # mm | in   (unit written into STEP/IGES/mesh files)
    "mesh_tolerance": 0.01,       # mm chordal deviation for STL/OBJ/3MF/PLY/glTF/VRML
    "angular_tolerance": 0.1,     # rad
    "stl_ascii": False,
    "per_object": False,          # one file per object (name_Object.ext)
    "visible_only": True,
    "auto_exchange": [],          # on Save also write these next to the .daedalus file, e.g. ["step", "iges"]
}
MESH_TRIANGLE_LIMIT = 40000


class ImportError_(ValueError):
    pass


# ---------------------------------------------------------- shape <-> text ---

_cache: dict[str, object] = {}
BLOBS: dict[str, str] = {}          # sha1 -> encoded BREP; features hold "blob:<sha1>" so undo
                                    # snapshots stay small; .daedalus files carry the blobs they use


def store_blob(text: str) -> str:
    key = hashlib.sha1(text.encode("ascii")).hexdigest()
    BLOBS[key] = text
    return "blob:" + key


def blobs_for(refs) -> dict:
    out = {}
    for r in refs:
        if r and r.startswith("blob:") and r[5:] in BLOBS:
            out[r[5:]] = BLOBS[r[5:]]
    return out


def shape_to_text(shape) -> str:
    from build123d import export_brep
    buf = io.BytesIO()
    export_brep(shape, buf)
    return base64.b64encode(zlib.compress(buf.getvalue(), 6)).decode("ascii")


def text_to_shape(data: str):
    if data.startswith("blob:"):
        key = data[5:]
        if key not in BLOBS:
            raise ImportError_("Imported geometry is missing from this file")
        data = BLOBS[key]
    else:
        key = hashlib.sha1(data.encode("ascii")).hexdigest()
    if key in _cache:
        return _cache[key]
    from build123d import import_brep
    raw = zlib.decompress(base64.b64decode(data))
    with tempfile.NamedTemporaryFile(suffix=".brep", delete=False) as f:
        f.write(raw)
        tmp = f.name
    try:
        shape = import_brep(tmp)
    finally:
        os.unlink(tmp)
    _cache[key] = shape
    register_geometry_key(shape, key)
    return shape


GEOM_KEYS: dict = {}           # solid hashes → blob key: lets the viewport keep meshes on disk per geometry


def _solid_hashes(shape):
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    ex = TopExp_Explorer(shape.wrapped, TopAbs_SOLID)
    out = []
    while ex.More():
        out.append(hash(ex.Current()))
        ex.Next()
    return tuple(out)


def register_geometry_key(shape, key):
    try:
        h = _solid_hashes(shape)
        if h:
            GEOM_KEYS[h] = key
    except Exception:  # noqa: BLE001
        pass


def geometry_key(shape):
    """Content key (the blob sha1) of imported geometry, or None for modelled bodies."""
    try:
        return GEOM_KEYS.get(_solid_hashes(shape))
    except Exception:  # noqa: BLE001
        return None


def _as_shell(s):
    from OCP.TopoDS import TopoDS
    f = getattr(TopoDS, "Shell_s", None) or getattr(TopoDS, "Shell")
    return f(s)


def _nest(solids):
    """Closed shells → solids. A shell inside a bigger one is a cavity: cut it, don't fill it."""
    solids = sorted(solids, key=lambda x: -abs(x.volume))
    out = []
    for sd in solids:
        c = sd.center()
        host = next((i for i, o in enumerate(out) if o.is_inside(c) and
                     o.bounding_box().add(sd.bounding_box()).size == o.bounding_box().size), None)
        if host is None:
            out.append(sd)
        else:
            try:
                out[host] = out[host].cut(sd).solids()[0]
            except Exception:  # noqa: BLE001
                out.append(sd)
    return out


def split_parts(name, part, pose=None, color=None):
    """One ImportedPart per separate solid (name, name-2, …)."""
    sols = list(part.solids())
    if len(sols) <= 1:
        return [ImportedPart(name, part, list(pose or [0.0] * 6), color)]
    from build123d import Compound
    return [ImportedPart(name if i == 0 else f"{name}-{i + 1}", to_part(Compound([sd])),
                         list(pose or [0.0] * 6), color) for i, sd in enumerate(sols)]


def to_part(shape):
    """Any solid-bearing shape → Part (a compound of its solids). Note Part(solid.wrapped)
    reports zero volume, so always go through a Compound."""
    from build123d import Compound, Part, Shape
    if isinstance(shape, Part):
        return shape
    if not hasattr(shape, "solids"):
        shape = Shape.cast(shape)
    return Part(Compound(list(shape.solids())).wrapped)


def as_solid(shape):
    """Best effort: a Part made of solids from whatever came in (solids, shells, faces)."""
    from build123d import Compound, Part, Solid
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    solids = list(shape.solids()) if hasattr(shape, "solids") else []
    if solids:
        return to_part(Compound(solids))
    faces = list(shape.faces())
    if not faces:
        raise ImportError_("No solids or surfaces in the file")
    sew = BRepBuilderAPI_Sewing(1e-3)
    for f in faces:
        sew.Add(f.wrapped)
    sew.Perform()
    sewn = sew.SewedShape()
    built = []
    exp = TopExp_Explorer(sewn, TopAbs_SHELL)
    while exp.More():
        try:
            mk = BRepBuilderAPI_MakeSolid(_as_shell(exp.Current()))
            s = Solid(mk.Solid())
            if s.volume < 0:
                s = Solid(s.wrapped.Reversed())
            if abs(s.volume) > 1e-9:
                built.append(s)
        except Exception:  # noqa: BLE001
            pass
        exp.Next()
    if not built:
        raise ImportError_("Surfaces don't close into a solid (open shell) — import as a solid isn't possible")
    return to_part(Compound(_nest(built)))


def unify_faces(shape):
    """Merge coplanar / co-cylindrical faces (meshes and some IGES come in as many small faces)."""
    from build123d import Shape
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    try:
        u = ShapeUpgrade_UnifySameDomain(shape.wrapped, True, True, True)
        u.Build()
        return to_part(Shape.cast(u.Shape()))
    except Exception:  # noqa: BLE001
        return shape


# ------------------------------------------------------------------ result ---

@dataclass
class ImportedPart:
    name: str
    shape: object                         # solid in its own coordinates
    pose: list = field(default_factory=lambda: [0.0] * 6)
    color: str | None = None
    fixed: bool = False
    proto: str | None = None              # instances of one STEP part share this key (and the same shape)
    blob: str | None = None               # serialized geometry (from the background import worker)


@dataclass
class ImportResult:
    kind: str                             # step | iges | brep | mesh | dxf
    parts: list = field(default_factory=list)          # [ImportedPart]
    sketch: dict | None = None            # DXF: sketch2d geometry
    notes: list = field(default_factory=list)
    mates: dict | None = None             # SolidWorks bridge: mates/components JSON


# ------------------------------------------------------------------- STEP ---

def _loc_to_pose(loc):
    import numpy as np
    from ..core.assembly import rotvec
    t = loc.wrapped.Transformation()
    M = np.eye(4)
    for i in range(3):
        for j in range(4):
            M[i, j] = t.Value(i + 1, j + 1)
    return [float(M[0, 3]), float(M[1, 3]), float(M[2, 3])] + [float(v) for v in rotvec(M[:3, :3])]


def _srgb_hex(rgb) -> str:
    """Quantity_Color → '#rrggbb' in sRGB (OCCT stores colours as linear RGB)."""
    try:
        from OCP.Quantity import Quantity_TypeOfColor
        r, g, b = rgb.Values(Quantity_TypeOfColor.Quantity_TOC_sRGB)
    except Exception:  # noqa: BLE001
        r, g, b = rgb.Red(), rgb.Green(), rgb.Blue()
    c = [max(0, min(255, round(v * 255))) for v in (r, g, b)]
    return "#%02x%02x%02x" % tuple(c)


def _hex(color):
    if color is None:
        return None
    try:
        r, g, b, *_ = tuple(color)
    except Exception:  # noqa: BLE001
        try:
            r, g, b, *_ = color.to_tuple()
        except Exception:  # noqa: BLE001
            return None
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _trsf_to_pose(trsf):
    import numpy as np
    from ..core.assembly import rotvec
    M = np.eye(4)
    for i in range(3):
        for j in range(4):
            M[i, j] = trsf.Value(i + 1, j + 1)
    return [float(M[0, 3]), float(M[1, 3]), float(M[2, 3])] + [float(v) for v in rotvec(M[:3, :3])]


def import_step(path, progress=None) -> ImportResult:
    """Fast STEP reader: OCCT's XCAF reader walked directly. Parts used many times (fasteners) are read once
    and shared by all their instances. Falls back to build123d's reader if the fast path fails."""
    try:
        return import_step_fast(path, progress)
    except ImportError_:
        raise
    except Exception:  # noqa: BLE001 — unusual files: the slower, more forgiving reader
        return import_step_b123d(path)


def import_step_fast(path, progress=None) -> ImportResult:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Quantity import Quantity_ColorRGBA
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_Tool
    try:
        from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
    except ImportError:  # older OCP
        from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import (XCAFDoc_ColorCurv, XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorTool,
                             XCAFDoc_DocumentTool, XCAFDoc_ShapeTool)
    from build123d import Shape

    def tick(stage, i=0, n=0):
        if progress:
            progress(stage, i, n)

    tick("Reading STEP")
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(False)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ImportError_("Couldn't read the STEP file")
    tick("Building geometry")
    if not reader.Transfer(doc):
        raise ImportError_("The STEP file has no transferable geometry")
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    def name_of(label):
        nm = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), nm):
            v = TCollection_AsciiString(nm.Get()).ToCString().strip()
            return "" if is_generic_name(v) else v
        return ""

    def color_label(label):
        c = Quantity_ColorRGBA()
        for typ in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
            if XCAFDoc_ColorTool.GetColor_s(label, typ, c):
                rgb = c.GetRGB()
                return _srgb_hex(rgb)
        return None

    def color_shape(shape):
        c = Quantity_ColorRGBA()
        for typ in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if ct.GetColor(shape, typ, c):
                rgb = c.GetRGB()
                return _srgb_hex(rgb)
        return None

    def entry(label):
        a = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, a)
        return a.ToCString()

    res = ImportResult("step")
    protos: dict = {}                 # label entry → (part shape | None, name, colour)
    leaves: list = []                 # (proto entry, instance name, location, inherited colour)

    def walk(label, loc, inh_color, inst_name):
        if st.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                ref = TDF_Label()
                if XCAFDoc_ShapeTool.IsReference_s(comp):
                    XCAFDoc_ShapeTool.GetReferredShape_s(comp, ref)
                else:
                    ref = comp
                walk(ref, loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(comp)),
                     color_label(comp) or inh_color, name_of(comp))
            return
        leaves.append((entry(label), label, inst_name, loc, inh_color))

    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        r = roots.Value(i)
        walk(r, TopLoc_Location(), color_label(r), name_of(r))
    n = len(leaves)
    stem = Path(path).stem
    for k, (key, label, inst_name, loc, inh_color) in enumerate(leaves):
        if k % 25 == 0:
            tick("Preparing parts", k, n)
        if key not in protos:
            topo = XCAFDoc_ShapeTool.GetShape_s(label)
            pname = name_of(label)
            try:
                part = as_solid(Shape.cast(topo))
            except ImportError_ as e:
                res.notes.append(f"{pname or 'part'}: {e}")
                part = None
            except Exception:  # noqa: BLE001
                part = None
            protos[key] = (part, pname, color_label(label) or (color_shape(topo) if part is not None else None))
        part, pname, pcolor = protos[key]
        if part is None:
            continue
        name = inst_name if inst_name and inst_name != pname and not inst_name.startswith("=>") else ""
        res.parts.append(ImportedPart(name or pname or stem, part, _trsf_to_pose(loc.Transformation()),
                                      inh_color or pcolor, proto=key))
        res.parts[-1]._from_product = not name
    if not res.parts:
        raise ImportError_("No solid parts found in the STEP file")
    number_instances(res.parts)
    shared = n - len(protos)
    if shared > 0:
        res.notes.append(f"{n} parts, {len(protos)} unique — repeated parts share one geometry")
    tick("Done", n, n)
    return res


_GENERIC_NAME = re.compile(
    r"^(?:(?:NAUO|NEXT_ASSEMBLY_USAGE_OCCURRENCE|SHAPE_REPRESENTATION|ADVANCED_BREP_SHAPE_REPRESENTATION|"
    r"MANIFOLD_SOLID_BREP|PRODUCT_DEFINITION|SDR|PDS)[-_ #]?\d*"
    r"|SOLID|SHELL|COMPOUND|COMPSOLID|FACE|OPEN_SHELL|CLOSED_SHELL|PART|BODY|UNNAMED|NONE|NULL|\d+)$", re.I)


def is_generic_name(v: str) -> bool:
    """Exporter placeholders rather than names: SolidWorks writes component instances as NAUO1, NAUO2… (the
    STEP NEXT_ASSEMBLY_USAGE_OCCURRENCE id); others write SOLID, BODY12, 'Unnamed', bare numbers."""
    v = (v or "").strip()
    return not v or bool(_GENERIC_NAME.match(v)) or v.startswith("=>")


def number_instances(parts):
    """Parts named after their product get SolidWorks-style instance numbers when the product is used more
    than once: Bracket-1, Bracket-2…"""
    from collections import Counter
    count = Counter(p.name for p in parts if getattr(p, "_from_product", False))
    seen = Counter()
    for p in parts:
        if getattr(p, "_from_product", False) and count[p.name] > 1:
            seen[p.name] += 1
            p.name = f"{p.name}-{seen[p.name]}"


def import_step_b123d(path) -> ImportResult:
    from build123d import Location
    from build123d import import_step as b_import_step
    root = b_import_step(str(path))
    res = ImportResult("step")

    def has_solids(s):
        try:
            return bool(s.solids())
        except Exception:  # noqa: BLE001
            return False

    def walk(node, parent_loc, inherited_color, path_names):
        loc = parent_loc * node.location if parent_loc is not None else node.location
        color = getattr(node, "color", None) or inherited_color
        kids = list(getattr(node, "children", []) or [])
        label = (node.label or "").strip()
        if is_generic_name(label):
            label = ""
        if kids:
            for k in kids:
                walk(k, loc, color, path_names + ([label] if label else []))
            return
        if not has_solids(node) and not list(node.faces()):
            return
        local = node.located(Location())          # geometry in the part's own frame
        try:
            shape = as_solid(local)
        except ImportError_ as e:
            res.notes.append(f"{label or 'part'}: {e}")
            return
        res.parts.append(ImportedPart(label or (path_names[-1] if path_names else Path(path).stem),
                                      shape, _loc_to_pose(loc), _hex(color)))

    kids = list(getattr(root, "children", []) or [])
    if kids:
        walk(root, None, getattr(root, "color", None), [])
    else:
        res.parts.append(ImportedPart(root.label or Path(path).stem, as_solid(root), [0.0] * 6,
                                      _hex(getattr(root, "color", None))))
    if not res.parts:
        raise ImportError_("No solid parts found in the STEP file")
    return res


# ------------------------------------------------------------- IGES / BREP ---

def import_iges(path) -> ImportResult:
    from build123d import Shape
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IGESControl import IGESControl_Reader
    r = IGESControl_Reader()
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise ImportError_("Couldn't read the IGES file")
    r.TransferRoots()
    shape = Shape.cast(r.OneShape())
    solid = unify_faces(as_solid(shape))
    return ImportResult("iges", split_parts(Path(path).stem, solid))


def import_brep_file(path) -> ImportResult:
    from build123d import import_brep
    return ImportResult("brep", split_parts(Path(path).stem, as_solid(import_brep(str(path)))))


# ------------------------------------------------------------------ meshes ---

def mesh_to_solid(vertices, triangles):
    """Triangle mesh → sewn solid with coplanar faces merged."""
    from build123d import Part
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing)
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    if len(triangles) > MESH_TRIANGLE_LIMIT:
        raise ImportError_(f"Mesh has {len(triangles):,} triangles — over the {MESH_TRIANGLE_LIMIT:,} limit for "
                           "conversion to a solid. Decimate it first (e.g. in MeshLab/Blender).")
    pts = [gp_Pnt(*map(float, v)) for v in vertices]
    sew = BRepBuilderAPI_Sewing(1e-4)
    n = 0
    for a, b, c in triangles:
        if a == b or b == c or a == c:
            continue
        pa, pb, pc = pts[a], pts[b], pts[c]
        if pa.Distance(pb) < 1e-9 or pb.Distance(pc) < 1e-9 or pa.Distance(pc) < 1e-9:
            continue
        poly = BRepBuilderAPI_MakePolygon(pa, pb, pc, True)
        if not poly.IsDone():
            continue
        mf = BRepBuilderAPI_MakeFace(poly.Wire(), True)
        if mf.IsDone():
            sew.Add(mf.Face())
            n += 1
    if n == 0:
        raise ImportError_("Mesh has no usable triangles")
    sew.Perform()
    sewn = sew.SewedShape()
    exp = TopExp_Explorer(sewn, TopAbs_SHELL)
    from build123d import Compound, Solid
    solids = []
    while exp.More():
        try:
            s = Solid(BRepBuilderAPI_MakeSolid(_as_shell(exp.Current())).Solid())
            if s.volume < 0:
                s = Solid(s.wrapped.Reversed())
            if abs(s.volume) > 1e-9:
                solids.append(s)
        except Exception:  # noqa: BLE001
            pass
        exp.Next()
    if not solids:
        raise ImportError_("Mesh isn't watertight — can't make a solid from it")
    return unify_faces(to_part(Compound(_nest(solids))))


def _dedupe(vertices, triangles, tol=1e-6):
    key, out_v, remap = {}, [], []
    for v in vertices:
        k = (round(v[0] / tol), round(v[1] / tol), round(v[2] / tol))
        if k not in key:
            key[k] = len(out_v)
            out_v.append(v)
        remap.append(key[k])
    return out_v, [(remap[a], remap[b], remap[c]) for a, b, c in triangles]


def read_stl(path):
    import struct
    data = Path(path).read_bytes()
    verts, tris = [], []
    if data[:5].lower() == b"solid" and b"facet" in data[:400]:
        cur = []
        for line in data.decode("ascii", "replace").splitlines():
            parts = line.split()
            if parts and parts[0] == "vertex":
                cur.append(tuple(float(x) for x in parts[1:4]))
                if len(cur) == 3:
                    i = len(verts)
                    verts += cur
                    tris.append((i, i + 1, i + 2))
                    cur = []
    else:
        (n,) = struct.unpack_from("<I", data, 80)
        off = 84
        for k in range(n):
            vals = struct.unpack_from("<12f", data, off)
            off += 50
            i = len(verts)
            verts += [vals[3:6], vals[6:9], vals[9:12]]
            tris.append((i, i + 1, i + 2))
    return _dedupe(verts, tris)


def read_obj(path):
    """→ {object name: (vertices, triangles)}."""
    verts, groups, cur = [], {}, "Mesh"
    for line in Path(path).read_text(errors="replace").splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts.append(tuple(float(x) for x in p[1:4]))
        elif p[0] in ("o", "g") and len(p) > 1:
            cur = " ".join(p[1:])
        elif p[0] == "f":
            idx = []
            for tok in p[1:]:
                i = int(tok.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            groups.setdefault(cur, [])
            for k in range(1, len(idx) - 1):
                groups[cur].append((idx[0], idx[k], idx[k + 1]))
    out = {}
    for name, tris in groups.items():
        used = sorted({i for t in tris for i in t})
        m = {old: new for new, old in enumerate(used)}
        out[name] = _dedupe([verts[i] for i in used], [(m[a], m[b], m[c]) for a, b, c in tris])
    return out


def read_3mf(path):
    """→ [(name, vertices, triangles, pose, colour)] — build items with their transforms."""
    import xml.etree.ElementTree as ET
    import numpy as np
    from ..core.assembly import rotvec
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".model")), None)
        if not name:
            raise ImportError_("No 3D model in this 3MF")
        root = ET.fromstring(z.read(name))
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"m": ""}
    unit = root.get("unit", "millimeter")
    scale = {"micron": 0.001, "millimeter": 1, "centimeter": 10, "inch": 25.4, "foot": 304.8, "meter": 1000}.get(unit, 1)
    q = (lambda t: f"m:{t}") if ns["m"] else (lambda t: t)
    tag = (lambda t: f"{{{ns['m']}}}{t}") if ns["m"] else (lambda t: t)
    mats = {}
    for bm in root.iter(tag("basematerials")):
        mats[bm.get("id")] = [(b.get("displaycolor") or "")[:7] or None for b in bm.findall(q("base"), ns)]
    objects = {}
    for ob in root.iter(f"{{{ns['m']}}}object" if ns["m"] else "object"):
        mesh = ob.find(q("mesh"), ns)
        if mesh is None:
            continue
        vs = [(float(v.get("x")) * scale, float(v.get("y")) * scale, float(v.get("z")) * scale)
              for v in mesh.find(q("vertices"), ns)]
        ts = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in mesh.find(q("triangles"), ns)]
        col = None
        if ob.get("pid") in mats:
            lst = mats[ob.get("pid")]
            k = int(ob.get("pindex", 0) or 0)
            col = lst[k].lower() if k < len(lst) and lst[k] else None
        objects[ob.get("id")] = (ob.get("name") or f"Object {ob.get('id')}", vs, ts, col)
    out = []
    build = root.find(q("build"), ns)
    items = list(build) if build is not None else [None]
    for it in items:
        oid = it.get("objectid") if it is not None else next(iter(objects), None)
        if oid not in objects:
            continue
        nm, vs, ts, col = objects[oid]
        pose = [0.0] * 6
        tr = it.get("transform") if it is not None else None
        if tr:
            m = [float(x) for x in tr.split()]
            R = np.array([[m[0], m[3], m[6]], [m[1], m[4], m[7]], [m[2], m[5], m[8]]])
            t = np.array(m[9:12]) * scale
            pose = list(t) + list(rotvec(R))
        out.append((nm, *_dedupe(vs, ts), pose, col))
    return out


def import_mesh(path) -> ImportResult:
    ext = Path(path).suffix.lower()
    res = ImportResult("mesh")
    if ext == ".stl":
        v, t = read_stl(path)
        res.parts += split_parts(Path(path).stem, mesh_to_solid(v, t))
    elif ext == ".obj":
        for name, (v, t) in read_obj(path).items():
            try:
                res.parts += split_parts(name, mesh_to_solid(v, t))
            except ImportError_ as e:
                res.notes.append(f"{name}: {e}")
    elif ext == ".3mf":
        for name, v, t, pose, color in read_3mf(path):
            try:
                res.parts += split_parts(name, mesh_to_solid(v, t), pose, color)
            except ImportError_ as e:
                res.notes.append(f"{name}: {e}")
    if not res.parts:
        raise ImportError_("; ".join(res.notes) or "Nothing importable in the mesh")
    if any(len(p.shape.faces()) > 60 for p in res.parts):
        res.notes.append("Curved areas stay faceted (a mesh has no true curves) — flat faces were merged "
                         "so you can sketch on and cut them")
    return res


# -------------------------------------------------------------------- DXF ---

DXF_UNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0254 * 1e-3, 9: 0.0254, 10: 914.4}


def import_dxf(path, flatten_tol=0.05) -> ImportResult:
    """DXF → sketch2d geometry (lines, arcs, circles). Splines/ellipses become short lines.
    Blocks are exploded. Endpoints that touch are merged so closed outlines extrude."""
    import ezdxf
    from ezdxf import disassemble
    doc = ezdxf.readfile(str(path))
    k = DXF_UNITS_MM.get(int(doc.header.get("$INSUNITS", 4) or 4), 1.0) or 1.0
    msp = doc.modelspace()
    from ..core import sketch2d as sk
    g = sk.empty_geometry()
    counters: dict[str, int] = {}

    def nid(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    grid: dict[tuple, list] = {}
    cell = 1e-3                                       # mm: ends closer than this are the same point

    def P(x, y):
        X, Y = x * k, y * k
        gx, gy = round(X / cell), round(Y / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for pid in grid.get((gx + dx, gy + dy), ()):
                    q = g["points"][pid]
                    if abs(q[0] - X) <= cell and abs(q[1] - Y) <= cell:
                        return pid
        pid = nid("p")
        g["points"][pid] = [X, Y]
        grid.setdefault((gx, gy), []).append(pid)
        return pid

    def line(a, b):
        if math.dist(a, b) * k < 1e-6:
            return
        g["curves"].append({"id": nid("l"), "type": "line", "a": P(*a), "b": P(*b)})

    def arc(c, r, a0, a1):          # degrees, CCW a0 → a1
        a = (c[0] + r * math.cos(math.radians(a0)), c[1] + r * math.sin(math.radians(a0)))
        b = (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1)))
        cid = nid("p")
        g["points"][cid] = [c[0] * k, c[1] * k]
        g["curves"].append({"id": nid("a"), "type": "arc", "c": cid, "a": P(*a), "b": P(*b)})

    def bulge_seg(p0, p1, bulge):
        if abs(bulge) < 1e-9:
            return line(p0, p1)
        theta = 4 * math.atan(bulge)
        chord = math.dist(p0, p1)
        r = chord / (2 * math.sin(abs(theta) / 2))
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        d = r * math.cos(abs(theta) / 2)
        nx, ny = -(p1[1] - p0[1]) / chord, (p1[0] - p0[0]) / chord
        sgn = 1 if bulge > 0 else -1
        c = (mx + sgn * nx * d, my + sgn * ny * d)
        a0 = math.degrees(math.atan2(p0[1] - c[1], p0[0] - c[0]))
        a1 = math.degrees(math.atan2(p1[1] - c[1], p1[0] - c[0]))
        if bulge > 0:
            arc(c, r, a0, a1)
        else:
            arc(c, r, a1, a0)

    skipped = {}
    for e in disassemble.recursive_decompose(msp):
        t = e.dxftype()
        try:
            if t == "LINE":
                line((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
            elif t == "CIRCLE":
                c = e.dxf.center
                cid = nid("p")
                g["points"][cid] = [c.x * k, c.y * k]
                g["curves"].append({"id": nid("c"), "type": "circle", "c": cid, "r": e.dxf.radius * k})
            elif t == "ARC":
                arc((e.dxf.center.x, e.dxf.center.y), e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle)
            elif t == "LWPOLYLINE":
                vs = list(e.get_points("xyb"))
                n = len(vs)
                segs = n if e.closed else n - 1
                for i in range(segs):
                    x0, y0, b = vs[i]
                    x1, y1, _ = vs[(i + 1) % n]
                    bulge_seg((x0, y0), (x1, y1), b)
            elif t == "POLYLINE" and not e.is_3d_polyline:
                vs = [(v.dxf.location.x, v.dxf.location.y, v.dxf.get("bulge", 0)) for v in e.vertices]
                n = len(vs)
                segs = n if e.is_closed else n - 1
                for i in range(segs):
                    bulge_seg(vs[i][:2], vs[(i + 1) % n][:2], vs[i][2])
            elif t in ("SPLINE", "ELLIPSE"):
                fl = [(p.x, p.y) for p in e.flattening(flatten_tol / k)]
                for a, b in zip(fl, fl[1:]):
                    line(a, b)
            else:
                skipped[t] = skipped.get(t, 0) + 1
        except Exception:  # noqa: BLE001
            skipped[t] = skipped.get(t, 0) + 1
    if not g["curves"]:
        raise ImportError_("No lines, arcs or circles in the DXF")
    loose = _mark_open_chains(g)
    res = ImportResult("dxf", sketch=g)
    if loose:
        res.notes.append(f"{loose} curve(s) not part of a closed outline were set to construction")
    if skipped:
        res.notes.append("Skipped: " + ", ".join(f"{n}× {t}" for t, n in sorted(skipped.items())))
    return res


def _mark_open_chains(g) -> int:
    """Curves hanging off closed outlines (a centre line, a lone spline) would make the
    whole sketch an open profile; peel them off as construction geometry."""
    from collections import Counter
    live = [c for c in g["curves"] if c["type"] != "circle"]
    n = 0
    while True:
        deg = Counter()
        for c in live:
            deg[c["a"]] += 1
            deg[c["b"]] += 1
        dangling = [c for c in live if deg[c["a"]] == 1 or deg[c["b"]] == 1 or c["a"] == c["b"] and c["type"] == "line"]
        if not dangling:
            return n
        for c in dangling:
            c["construction"] = True
            live.remove(c)
            n += 1


# --------------------------------------------------------------- dispatch ---

def import_file(path, progress=None) -> ImportResult:
    ext = Path(path).suffix.lower()
    kind = IMPORT_EXT.get(ext)
    if kind is None:
        raise ImportError_(f"Can't import '{ext}' files. Supported: " + ", ".join(sorted(IMPORT_EXT)))
    if not Path(path).exists():
        raise ImportError_(f"No such file: {path}")
    if kind == "step":
        return import_step(path, progress)
    if kind == "iges":
        return import_iges(path)
    if kind == "brep":
        return import_brep_file(path)
    if kind in ("stl", "obj", "3mf"):
        return import_mesh(path)
    if kind == "dxf":
        return import_dxf(path)
    if kind == "solidworks":
        from . import swbridge
        return swbridge.import_solidworks(path)
    raise ImportError_(f"Unsupported: {ext}")


# ------------------------------------------------------------------ export ---

def export_assembly(doc, path, options=None):
    """Write the document. STEP keeps object names, colours and placements as an assembly.

    options (see EXPORT_DEFAULTS): STEP schema, units, mesh tolerance, ASCII STL, one file per object."""
    from build123d import Color, Compound, export_brep, export_gltf, export_obj, export_step, export_stl
    from ..core.assembly import location_of
    from ..core.layers import hex_to_rgb
    opt = {**EXPORT_DEFAULTS, **(getattr(doc, "export_options", None) or {}), **(options or {})}
    ext = Path(path).suffix.lower()
    kind = EXPORT_EXT.get(ext)
    if kind is None:
        raise ImportError_(f"Can't export '{ext}'. Supported: " + ", ".join(sorted(EXPORT_EXT)))
    if kind == "dxf":
        raise ImportError_("DXF exports a sketch or a drawing — use Export sketch as DXF or a Drawing")
    parts, cols = [], []
    for o in doc.objects:
        b = doc.result.bodies.get(o.name)
        if b is None or (opt["visible_only"] and not o.visible):
            continue
        placed = b.moved(location_of(o.pose)) if any(o.pose) else b
        placed = placed.__class__(placed.wrapped) if hasattr(placed, "wrapped") else placed
        placed.label = o.name
        placed.color = Color(*hex_to_rgb(o.color))
        parts.append(placed)
        cols.append(o.color)
    if not parts:
        raise ImportError_("Nothing to export")
    if opt["per_object"] and len(parts) > 1:
        out = []
        base = Path(path)
        for p_, c in zip(parts, cols):
            fn = base.with_name(f"{base.stem}_{p_.label}{base.suffix}")
            _write(kind, [p_], [c], fn, opt, p_.label)
            out.append(str(fn))
        return out[0]
    _write(kind, parts, cols, Path(path), opt, Path(path).stem)
    return str(path)


def _write(kind, parts, cols, path, opt, name):
    from build123d import Compound, Unit, export_brep, export_gltf, export_obj, export_step, export_stl
    inch = opt["units"] in ("in", "inch")
    tol, atol = float(opt["mesh_tolerance"]), float(opt["angular_tolerance"])
    whole = Compound(children=parts, label=name) if len(parts) > 1 else parts[0]
    path = str(path)
    if kind == "step":
        _write_step(whole, path, STEP_SCHEMAS.get(opt["step_schema"], "AP214IS"), inch, name)
    elif kind == "iges":
        from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer
        from OCP.Interface import Interface_Static
        IGESControl_Controller.Init_s()
        Interface_Static.SetIVal_s("write.iges.brep.mode", 1)      # faces as B-rep solids (MSBO), not trimmed surfaces
        w = IGESControl_Writer("IN" if inch else "MM", 1)
        w.AddShape(Compound(parts).wrapped)
        w.ComputeModel()
        if not w.Write(path):
            raise ImportError_("IGES write failed")
    elif kind == "brep":
        export_brep(Compound(parts), path)
    elif kind in ("stl", "obj", "ply", "vrml"):
        comp = Compound(parts)
        if inch:
            comp = comp.scale(1 / 25.4)
        if kind == "stl":
            export_stl(comp, path, tolerance=tol, angular_tolerance=atol, ascii_format=bool(opt["stl_ascii"]))
        elif kind == "obj":
            export_obj(comp, path, linear_deflection=tol, angular_deflection=atol, include_uvs=False)
        elif kind == "ply":
            _write_ply(comp, path, tol, atol, parts, cols)
        else:
            from OCP.BRepMesh import BRepMesh_IncrementalMesh
            from OCP.VrmlAPI import VrmlAPI_Writer
            BRepMesh_IncrementalMesh(comp.wrapped, tol, False, atol, True)
            w = VrmlAPI_Writer()
            if not w.Write(comp.wrapped, path, 2):
                raise ImportError_("VRML write failed")
    elif kind in ("gltf", "glb"):
        export_gltf(whole, path, binary=(kind == "glb"), linear_deflection=max(tol, 0.01), angular_deflection=atol)
    elif kind == "3mf":
        write_3mf(parts, path, tol=tol, colors=cols)


def _write_step(shape, path, schema, inch, name):
    """STEP through XDE (names, colours, assembly structure) with a chosen schema and length unit."""
    from build123d import Unit
    from build123d.exporters3d import _create_xde
    from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_Controller, STEPControl_StepModelType
    from OCP.TCollection import TCollection_HAsciiString
    from OCP.XSControl import XSControl_WorkSession
    doc = _create_xde(shape, Unit.MM, auto_naming=True)
    from OCP.Message import Message, Message_Gravity
    for printer in Message.DefaultMessenger_s().Printers():
        printer.SetTraceLevel(Message_Gravity.Message_Fail)
    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.schema", schema)
    Interface_Static.SetCVal_s("write.step.unit", "INCH" if inch else "MM")
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 1)
    try:
        writer = STEPCAFControl_Writer(XSControl_WorkSession(), True)
        writer.SetColorMode(True)
        writer.SetLayerMode(True)
        writer.SetNameMode(True)
        writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
        header = APIHeaderSection_MakeHeader(writer.Writer().Model())
        if header.IsDone():
            header.SetName(TCollection_HAsciiString(name))
            header.SetOriginatingSystem(TCollection_HAsciiString("Daedalus"))
        if int(writer.Write(str(path))) != 1:
            raise ImportError_("STEP write failed")
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
        Interface_Static.SetCVal_s("write.step.schema", "AP214IS")


def _write_ply(comp, path, tol, atol, parts, cols):
    """Binary little-endian PLY with per-vertex colours."""
    import struct
    verts, faces, colors = [], [], []
    from ..core.layers import hex_to_rgb
    for p_, c in zip(parts, cols):
        v, t = p_.tessellate(tol, atol)
        base = len(verts)
        rgb = tuple(int(round(x * 255)) for x in hex_to_rgb(c)[:3])
        verts += [(q.X, q.Y, q.Z) for q in v]
        colors += [rgb] * len(v)
        faces += [(a + base, b + base, c_ + base) for a, b, c_ in t]
    with open(path, "wb") as f:
        f.write((f"ply\nformat binary_little_endian 1.0\ncomment Daedalus\nelement vertex {len(verts)}\n"
                 "property float x\nproperty float y\nproperty float z\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\n"
                 f"element face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n").encode())
        for (x, y, z), (r, g, b) in zip(verts, colors):
            f.write(struct.pack("<fffBBB", x, y, z, r, g, b))
        for a, b, c in faces:
            f.write(struct.pack("<Biii", 3, a, b, c))


def write_3mf(parts, path, tol=0.01, colors=None):
    """3MF (mm): one mesh object per part, placed in world coordinates, with display colours."""
    from xml.sax.saxutils import quoteattr
    objs, items = [], []
    colors = colors or ["#b4b8bf"] * len(parts)
    bases = "".join(f'<base name={quoteattr(p.label or f"Part {i}")} displaycolor="{c.upper()}FF"/>'
                    for i, (p, c) in enumerate(zip(parts, colors), start=1))
    for i, p in enumerate(parts, start=1):
        verts, tris = p.tessellate(tol, 0.1)
        v = "".join(f'<vertex x="{q.X:.5f}" y="{q.Y:.5f}" z="{q.Z:.5f}"/>' for q in verts)
        t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
        objs.append(f'<object id="{i}" type="model" pid="1000" pindex="{i - 1}" '
                    f'name={quoteattr(p.label or f"Part {i}")}>'
                    f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>")
        items.append(f'<item objectid="{i}"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><basematerials id="1000">{bases}</basematerials>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.'
          '3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)


def export_sketch_dxf(doc, sketch_name, path):
    """A sketch's curves (plane-local, mm) → DXF with real LINE/ARC/CIRCLE entities."""
    import ezdxf
    from ..core import sketch2d as sk
    f = doc.get(sketch_name)
    if f.TYPE != "sketch":
        raise ImportError_("Pick a 2D sketch to export as DXF")
    out = ezdxf.new("R2010", setup=True)
    out.header["$INSUNITS"] = 4
    msp = out.modelspace()
    n = 0
    geom = f.params.get("geometry") or {}
    if geom.get("curves"):
        res = sk.solve(geom)
        g = res.geometry if res.ok else sk.normalize(geom)
        P = g["points"]
        for c in g["curves"]:
            layer = "CONSTRUCTION" if c.get("construction") else "0"
            if c["type"] == "line":
                msp.add_line(P[c["a"]], P[c["b"]], dxfattribs={"layer": layer})
            elif c["type"] == "circle":
                msp.add_circle(P[c["c"]], c["r"], dxfattribs={"layer": layer})
            elif c["type"] == "point":
                msp.add_point(P[c["a"]], dxfattribs={"layer": layer})
            elif c["type"] == "text":
                t = msp.add_text(sk.text_value(c), height=float(c.get("height", 10)),
                                 rotation=float(c.get("angle", 0)), dxfattribs={"layer": layer})
                t.set_placement(tuple(P[c["a"]]))
            elif c["type"] == "spline":
                pts = [tuple(P[q]) for q in c["pts"]] + ([tuple(P[c["pts"][0]])] if c.get("closed") else [])
                msp.add_spline(fit_points=[(x, y, 0) for x, y in pts], dxfattribs={"layer": layer})
            elif c["type"] in ("ellipse", "conic", "equation"):
                msp.add_lwpolyline(sk.samples(g, c), dxfattribs={"layer": layer})
            else:
                cx, cy = P[c["c"]]
                ax, ay = P[c["a"]]
                bx, by = P[c["b"]]
                msp.add_arc((cx, cy), math.hypot(ax - cx, ay - cy), math.degrees(math.atan2(ay - cy, ax - cx)),
                            math.degrees(math.atan2(by - cy, bx - cx)), dxfattribs={"layer": layer})
            n += 1
    # quick-shape entities: use the built profile's edges in plane coordinates
    shown = doc.result.sketches.get(sketch_name)
    if f.params.get("entities") and shown is not None:
        shape, plane = shown
        for e in shape.edges():
            loc = [plane.to_local_coords(p) for p in e.positions([i / 24 for i in range(25)])]
            if e.geom_type.name == "LINE":
                msp.add_line((loc[0].X, loc[0].Y), (loc[-1].X, loc[-1].Y))
            else:
                msp.add_lwpolyline([(p.X, p.Y) for p in loc])
            n += 1
    if not n:
        raise ImportError_("That sketch has nothing to export")
    out.saveas(str(path))
    return n
