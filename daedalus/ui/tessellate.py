"""Kernel shapes → VTK polydata. Per-face tessellation keeps a triangle→face
map so the viewport can pick and highlight B-rep faces.

Fast path: OCCT meshes the whole body once (in parallel), triangles and edge polylines are read straight from
the mesh into numpy arrays, and results are cached by shape identity — so the 1 500 identical screws of an
imported assembly are tessellated once and share one mesh.
"""
from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np
from . import vtklite as vtk

_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_CACHE_MAX = 600


def shape_key(body):
    """Identity of a body's geometry: the same underlying solids at the same placement → the same key (the
    1 500 copies of one imported screw share it). OCCT hashes shapes by their TShape + location."""
    w = body.wrapped
    try:
        t = w.Location().Transformation()
        loc = tuple(round(t.Value(i, j), 9) for i in (1, 2, 3) for j in (1, 2, 3, 4))
    except Exception:  # noqa: BLE001
        loc = ()
    try:
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        ex = TopExp_Explorer(w, TopAbs_SOLID)
        parts = []
        while ex.More():
            parts.append(hash(ex.Current()))
            ex.Next()
        ident = tuple(parts) if parts else (hash(w),)
    except Exception:  # noqa: BLE001
        ident = (hash(w),)
    return (ident, int(w.Orientation()), loc)


QUALITY = {"fine": (1.0, 0.2), "normal": (1.5, 0.3), "draft": (3.0, 0.45)}   # (tolerance scale, angle rad)
_quality = ["normal"]


def set_quality(q: str):
    if q in QUALITY and q != _quality[0]:
        _quality[0] = q
        clear_cache()


def quality() -> str:
    return _quality[0]


def default_angle() -> float:
    return QUALITY[_quality[0]][1]


def auto_tolerance(body, base=0.05):
    """Chordal deflection scaled to the part and the display quality: fine for small parts, coarser for big
    plates/frames."""
    k = QUALITY[_quality[0]][0]
    try:
        # not build123d's bounding_box(): its "optimal" box first wipes the shape's mesh (BRepTools.Clean)
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        box = Bnd_Box()
        BRepBndLib.Add_s(body.wrapped, box, True)
        x0, y0, z0, x1, y1, z1 = box.Get()
        diag = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
    except Exception:  # noqa: BLE001
        return base * k
    return round(min(1.0, max(base * k, diag * 2e-4 * k)), 3)


def is_cached(body, tol=None, ang=None, face_colors=None, hidden=frozenset()) -> bool:
    tol = auto_tolerance(body) if tol is None else tol
    ang = default_angle() if ang is None else ang
    ck = (tuple(map(tuple, face_colors)) if face_colors else None)
    return (shape_key(body), round(tol, 6), ang, frozenset(hidden), ck) in _CACHE


def mesh_missing(shape, tol, ang):
    """Mesh only the faces that have no triangulation fine enough yet — after an edit that's just the faces the
    edit made (meshing the whole body again re-walks every face)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    as_face = getattr(TopoDS, "Face_s", None) or TopoDS.Face
    w = shape.wrapped if hasattr(shape, "wrapped") else shape
    todo = []
    seen = set()
    ex = TopExp_Explorer(w, TopAbs_FACE)
    while ex.More():
        f = as_face(ex.Current())
        ex.Next()
        h = hash(f)
        if h in seen:
            continue
        seen.add(h)
        tri = BRep_Tool.Triangulation_s(f, TopLoc_Location())
        if tri is None or tri.Deflection() > tol * 1.01:
            todo.append(f)
    if not todo:
        return 0
    if len(todo) == len(seen):
        BRepMesh_IncrementalMesh(w, tol, False, ang, True)
        return len(todo)
    comp = TopoDS_Compound()
    bb = BRep_Builder()
    bb.MakeCompound(comp)
    for f in todo:
        bb.Add(comp, f)
    BRepMesh_IncrementalMesh(comp, tol, False, ang, True)
    return len(todo)


def premesh(bodies, ang=None):
    """Mesh many bodies in one OCCT call per tolerance group, so all CPU cores work across every face of the
    batch (meshing one small body at a time leaves most cores idle)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopoDS import TopoDS_Compound
    ang = default_angle() if ang is None else ang
    groups: dict = {}
    seen = set()
    for b in bodies:
        k = shape_key(b)
        if k in seen:
            continue
        seen.add(k)
        tol = auto_tolerance(b)
        disk = _disk_path(b, tol, ang, frozenset())
        if disk is not None and disk.exists():
            continue                                   # mesh comes from the disk cache
        groups.setdefault(tol, []).append(b)
    for tol, bs in groups.items():
        comp = TopoDS_Compound()
        bb = BRep_Builder()
        bb.MakeCompound(comp)
        for b in bs:
            bb.Add(comp, b.wrapped)
        try:
            mesh_missing(comp, tol, ang)
        except Exception:  # noqa: BLE001
            try:
                BRepMesh_IncrementalMesh(comp, tol, False, ang, True)
            except Exception:  # noqa: BLE001
                pass


def _cached(key):
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
    return hit


def _store(key, val):
    _CACHE[key] = val
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def clear_cache():
    _CACHE.clear()
    _FACE_CACHE.clear()


# ------------------------------------------------------------------ fast mesh
def _mesh_arrays(body, faces, tol, ang, hidden):
    """(points (N,3), triangles (M,3), face index per triangle (M,), edge polylines [ (K,3) ]) from OCCT's mesh."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    as_face = getattr(TopoDS, "Face_s", None) or TopoDS.Face
    as_edge = getattr(TopoDS, "Edge_s", None) or TopoDS.Edge
    try:
        mesh_missing(body, tol, ang)
    except Exception:  # noqa: BLE001
        BRepMesh_IncrementalMesh(body.wrapped, tol, False, ang, True)
    pts, tris, fids, lines = [], [], [], []
    seen_edges = set()
    base = 0
    for fi, face in enumerate(faces):
        if fi in hidden:
            continue
        fw = face.wrapped
        fkey = (hash(fw), int(fw.Orientation()), tol, ang)
        hit = _FACE_CACHE.get(fkey)
        if hit is None:
            hit = _face_arrays(fw, as_face, as_edge, BRep_Tool, TopAbs_EDGE, TopAbs_REVERSED, TopExp_Explorer,
                               TopLoc_Location)
            _FACE_CACHE[fkey] = hit
            if len(_FACE_CACHE) > _FACE_CACHE_MAX:
                _FACE_CACHE.popitem(last=False)
        else:
            _FACE_CACHE.move_to_end(fkey)
        if hit is None:
            continue
        nodes, t, elines = hit
        pts.append(nodes)
        tris.append(t + base)
        fids.append(np.full(len(t), fi, dtype=np.int32))
        for h, ln in elines:                                  # shared edges drawn once
            if h not in seen_edges:
                seen_edges.add(h)
                lines.append(ln)
        base += len(nodes)
    if not pts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int32), []
    return np.vstack(pts), np.vstack(tris), np.concatenate(fids), lines


_FACE_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_FACE_CACHE_MAX = 60000


def _face_arrays(fw, as_face, as_edge, BRep_Tool, TopAbs_EDGE, TopAbs_REVERSED, TopExp_Explorer, TopLoc_Location):
    """(nodes, triangles, [(edge hash, polyline)]) of one meshed face — cached by the face's identity, so an
    edit re-reads only the faces it actually changed."""
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(as_face(fw), loc)
    if tri is None:
        return None
    n, m = tri.NbNodes(), tri.NbTriangles()
    if not n or not m:
        return None
    node = tri.Node
    nodes = np.array([(p.X(), p.Y(), p.Z()) for p in map(node, range(1, n + 1))], dtype=np.float64)
    if not loc.IsIdentity():
        tr = loc.Transformation()
        R = np.array([[tr.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)])
        T = np.array([tr.Value(i, 4) for i in (1, 2, 3)])
        nodes = nodes @ R.T + T
    tg = tri.Triangle
    t = np.array([tg(j).Get() for j in range(1, m + 1)], dtype=np.int64) - 1
    if fw.Orientation() == TopAbs_REVERSED:
        t = t[:, [0, 2, 1]]
    elines = []
    exp = TopExp_Explorer(fw, TopAbs_EDGE)
    seen = set()
    while exp.More():
        e = as_edge(exp.Current())
        exp.Next()
        h = hash(e)
        if h in seen or BRep_Tool.Degenerated_s(e):
            continue
        try:
            poly = BRep_Tool.PolygonOnTriangulation_s(e, tri, loc)
        except Exception:  # noqa: BLE001
            poly = None
        if poly is None:
            continue
        seen.add(h)
        ids = poly.Nodes()
        idx = [ids.Value(k) - 1 for k in range(ids.Lower(), ids.Upper() + 1)]
        if len(idx) >= 2:
            elines.append((h, nodes[idx]))
    return nodes, t, elines


# ------------------------------------------------------------------ disk cache (imported geometry)
def _cache_dir():
    from pathlib import Path
    d = Path.home() / ".daedalus" / "meshcache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _disk_path(body, tol, ang, hidden):
    """Imported parts never change, so their meshes are kept on disk by geometry content — reopening a big
    imported assembly skips meshing altogether."""
    if hidden:
        return None
    try:
        from ..io.formats import geometry_key
        gk = geometry_key(body)
    except Exception:  # noqa: BLE001
        gk = None
    if not gk:
        return None
    try:
        return _cache_dir() / f"{gk}_{tol:.3f}_{ang:.2f}.npz"
    except OSError:
        return None


def _disk_load(path):
    try:
        if not path.exists():
            return None
        z = np.load(path)
        lines = np.split(z["lp"], z["lo"]) if len(z["lo"]) else ([z["lp"]] if len(z["lp"]) else [])
        return z["p"], z["t"], z["f"], [ln for ln in lines if len(ln) >= 2]
    except Exception:  # noqa: BLE001
        return None


def _disk_save(path, points, tris, fids, lines):
    try:
        lp = np.vstack(lines) if lines else np.zeros((0, 3))
        lo = np.cumsum([len(ln) for ln in lines])[:-1] if lines else np.zeros(0, dtype=np.int64)
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, p=points, t=tris, f=fids, lp=lp, lo=lo)
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _to_vtk(points, tris, fids, face_colors):
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    pd = vtk.vtkPolyData()
    vp = vtk.vtkPoints()
    vp.SetData(numpy_to_vtk(np.ascontiguousarray(points, dtype=np.float64), deep=True))
    pd.SetPoints(vp)
    cells = vtk.vtkCellArray()
    if len(tris):
        legacy = np.empty((len(tris), 4), dtype=np.int64)
        legacy[:, 0] = 3
        legacy[:, 1:] = tris
        cells.SetCells(len(tris), numpy_to_vtkIdTypeArray(legacy.ravel(), deep=True))
    pd.SetPolys(cells)
    fa = numpy_to_vtk(np.ascontiguousarray(fids, dtype=np.int32), deep=True, array_type=vtk.VTK_INT)
    fa.SetName("face_id")
    pd.GetCellData().AddArray(fa)
    if face_colors and len(fids):
        pal = (np.asarray(face_colors, dtype=np.float64) * 255).round().astype(np.uint8)
        ca = numpy_to_vtk(np.ascontiguousarray(pal[fids]), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
        ca.SetName("colors")
        pd.GetCellData().SetScalars(ca)
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(pd)
    normals.SetFeatureAngle(35)
    normals.SplittingOn()
    normals.ConsistencyOn()
    normals.Update()
    return normals.GetOutput()


def _lines_vtk(lines):
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    pd = vtk.vtkPolyData()
    if not lines:
        return pd
    allp = np.vstack(lines)
    vp = vtk.vtkPoints()
    vp.SetData(numpy_to_vtk(np.ascontiguousarray(allp, dtype=np.float64), deep=True))
    conn, off = [], 0
    for ln in lines:
        k = len(ln)
        conn.append(np.concatenate(([k], np.arange(off, off + k))))
        off += k
    cells = vtk.vtkCellArray()
    cells.SetCells(len(lines), numpy_to_vtkIdTypeArray(np.concatenate(conn).astype(np.int64), deep=True))
    pd.SetPoints(vp)
    pd.SetLines(cells)
    return pd


def body_meshes(body, tol: float | None = None, ang: float | None = None, face_colors=None,
                hidden=frozenset()):
    """(polydata, faces, edges polydata) for a body, cached by shape identity + options."""
    tol = auto_tolerance(body) if tol is None else tol
    ang = default_angle() if ang is None else ang
    ck = (tuple(map(tuple, face_colors)) if face_colors else None)
    key = (shape_key(body), round(tol, 6), ang, frozenset(hidden), ck)
    hit = _cached(key)
    if hit is not None:
        return hit
    faces = list(body.faces())
    try:
        disk = _disk_path(body, tol, ang, hidden)
        got = _disk_load(disk) if disk else None
        if got is not None and (len(got[2]) == 0 or int(got[2].max()) < len(faces)):
            points, tris, fids, lines = got
        else:
            points, tris, fids, lines = _mesh_arrays(body, faces, tol, ang, hidden)
            if disk is not None:
                _disk_save(disk, points, tris, fids, lines)
        out = (_to_vtk(points, tris, fids, face_colors), faces, _lines_vtk(lines))
    except Exception:  # noqa: BLE001 — anything odd: the slow, per-face path
        pd, faces = _body_polydata_slow(body, tol, ang, face_colors, hidden)
        from build123d import Compound
        shown = [f for i, f in enumerate(faces) if i not in hidden]
        out = (pd, faces, edges_polydata(Compound(shown)) if shown else vtk.vtkPolyData())
    _store(key, out)
    return out


def body_polydata(body, tol: float = 0.05, ang: float = 0.2, face_colors=None, hidden=frozenset()):
    """Returns (polydata, faces) where cell data 'face_id' indexes `faces`.
    face_colors: optional RGB (0..1) per face → cell colour scalars. hidden: face indices to leave out."""
    pd, faces, _edges = body_meshes(body, tol, ang, face_colors, hidden)
    return pd, faces


# ------------------------------------------------------------------ fallbacks
def _body_polydata_slow(body, tol, ang, face_colors=None, hidden=frozenset()):
    pts, polys = vtk.vtkPoints(), vtk.vtkCellArray()
    face_ids = vtk.vtkIntArray()
    face_ids.SetName("face_id")
    cols = vtk.vtkUnsignedCharArray()
    cols.SetNumberOfComponents(3)
    cols.SetName("colors")
    faces = list(body.faces())
    for fi, face in enumerate(faces):
        if fi in hidden:
            continue
        try:
            verts, tris = face.tessellate(tol, ang)
        except Exception:  # noqa: BLE001
            continue
        base = pts.GetNumberOfPoints()
        for v in verts:
            pts.InsertNextPoint(v.X, v.Y, v.Z)
        for a, b, c in tris:
            polys.InsertNextCell(3)
            polys.InsertCellPoint(base + a)
            polys.InsertCellPoint(base + b)
            polys.InsertCellPoint(base + c)
            face_ids.InsertNextValue(fi)
            if face_colors:
                cr, cg, cb = face_colors[fi]
                cols.InsertNextTuple3(int(cr * 255), int(cg * 255), int(cb * 255))
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(polys)
    pd.GetCellData().AddArray(face_ids)
    if face_colors:
        pd.GetCellData().SetScalars(cols)
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(pd)
    normals.SetFeatureAngle(35)
    normals.SplittingOn()
    normals.ConsistencyOn()
    normals.Update()
    return normals.GetOutput(), faces


def edges_polydata(shape, chord: float = 0.5):
    pts, lines = vtk.vtkPoints(), vtk.vtkCellArray()
    for e in shape.edges():
        try:
            n = max(2, min(200, int(math.ceil(e.length / chord)) + 1))
            n = 2 if e.geom_type.name == "LINE" else max(n, 24)
            ps = e.positions([i / (n - 1) for i in range(n)])
        except Exception:  # noqa: BLE001
            continue
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(len(ps))
        for i, p in enumerate(ps):
            line.GetPointIds().SetId(i, pts.InsertNextPoint(p.X, p.Y, p.Z))
        lines.InsertNextCell(line)
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetLines(lines)
    return pd
