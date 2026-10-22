"""Tetrahedral meshing of a solid with gmsh, keeping track of which mesh triangles lie on which
B-rep face (so fixtures and loads picked on faces land on the right nodes)."""
from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field

import numpy as np

_lock = threading.Lock()          # gmsh is a process-wide singleton


class MeshError(RuntimeError):
    pass


@dataclass
class SimMesh:
    p: np.ndarray                 # (n, 3) node coordinates, mm
    t: np.ndarray                 # (4, m) tetra node indices
    faces: dict = field(default_factory=dict)   # B-rep face index -> (k, 3) boundary triangles (node ids)
    size: float = 0.0

    @property
    def n_nodes(self):
        return len(self.p)

    @property
    def n_elems(self):
        return self.t.shape[1]


def default_size(body) -> float:
    bb = body.bounding_box()
    dims = sorted([bb.size.X, bb.size.Y, bb.size.Z])
    # ~ 2 elements through the thinnest dimension, ≤ 1/25 of the longest
    return float(max(0.5, min(dims[-1] / 25.0, max(dims[0] / 2.0, dims[-1] / 60.0))))


def mesh_solid(body, size: float | None = None, refine_faces: dict | None = None) -> SimMesh:
    """refine_faces: {face index: local size} for mesh controls."""
    import gmsh
    from build123d import export_brep
    size = float(size or default_size(body))
    fd, path = tempfile.mkstemp(suffix=".brep")
    os.close(fd)
    export_brep(body, path)
    faces = list(body.faces())
    with _lock:
        gmsh.initialize(interruptible=False)
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("General.NumThreads", 1)   # deterministic meshes
            gmsh.model.add("daedalus")
            gmsh.model.occ.importShapes(path)
            gmsh.model.occ.synchronize()
            vols = gmsh.model.getEntities(3)
            if not vols:
                raise MeshError("Nothing solid to mesh")
            gmsh.option.setNumber("Mesh.MeshSizeMax", size)
            gmsh.option.setNumber("Mesh.MeshSizeMin", size / 8)
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 16)
            gmsh.option.setNumber("Mesh.Algorithm3D", 1)
            gmsh.option.setNumber("Mesh.Optimize", 1)
            surf = [t for _, t in gmsh.model.getEntities(2)]
            fmap = _match_faces(gmsh, surf, faces)
            if refine_faces:
                inv = {fi: tag for tag, fi in fmap.items()}
                for fi, s in refine_faces.items():
                    if fi in inv:
                        pts = gmsh.model.getBoundary([(2, inv[fi])], recursive=True)
                        gmsh.model.mesh.setSize(pts, float(s))
            try:
                gmsh.model.mesh.generate(3)
            except Exception as e:  # noqa: BLE001
                raise MeshError(f"Meshing failed: {e}. Try a different element size.") from e
            tags, xyz, _ = gmsh.model.mesh.getNodes()
            idx = np.full(int(tags.max()) + 1, -1, int)
            idx[tags.astype(int)] = np.arange(len(tags))
            p = xyz.reshape(-1, 3)
            tets = []
            for _, vt in vols:
                et, _, en = gmsh.model.mesh.getElements(3, vt)
                for typ, nodes in zip(et, en):
                    if typ == 4:
                        tets.append(nodes.reshape(-1, 4))
            if not tets:
                raise MeshError("No tetrahedra produced")
            t = idx[np.vstack(tets).astype(int)]
            out = {}
            for tag in surf:
                et, _, en = gmsh.model.mesh.getElements(2, tag)
                tri = [n.reshape(-1, 3) for typ, n in zip(et, en) if typ == 2]
                if tri and tag in fmap:
                    out[fmap[tag]] = idx[np.vstack(tri).astype(int)]
        finally:
            gmsh.finalize()
            os.unlink(path)
    # keep only nodes used by tets (gmsh may keep geometry points)
    used = np.unique(t)
    remap = np.full(len(p), -1, int)
    remap[used] = np.arange(len(used))
    t = remap[t]
    out = {k: remap[v] for k, v in out.items()}
    t = _orient(p[used], t)
    return SimMesh(p[used], t.T.copy(), out, size)


def _orient(p, t):
    a, b, c, d = (p[t[:, i]] for i in range(4))
    vol = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)
    bad = vol < 0
    t[bad, 1], t[bad, 2] = t[bad, 2].copy(), t[bad, 1].copy()
    return t


def _match_faces(gmsh, surf, faces):
    """gmsh surface tag -> build123d face index, by area and centre (order usually matches)."""
    info = []
    for tag in surf:
        a = gmsh.model.occ.getMass(2, tag)
        c = np.array(gmsh.model.occ.getCenterOfMass(2, tag))
        info.append((tag, a, c))
    from build123d import CenterOf
    fin = []
    for f in faces:
        c = f.center(CenterOf.MASS)               # centre of area (gmsh's), not the parametric middle
        fin.append((f.area, np.array([c.X, c.Y, c.Z])))
    scale = max(1.0, max((np.linalg.norm(c) for _, c in fin), default=1.0))
    out, used = {}, set()
    if len(info) == len(fin) and all(abs(a - fa) <= 1e-6 * max(1, fa) + 1e-6 and np.linalg.norm(c - fc) < 1e-6 * scale + 1e-6
                                     for (_, a, c), (fa, fc) in zip(info, fin)):
        return {tag: i for i, (tag, _, _) in enumerate(info)}
    for tag, a, c in info:
        best, bs = None, float("inf")
        for i, (fa, fc) in enumerate(fin):
            if i in used:
                continue
            sc = abs(a - fa) / max(fa, 1e-9) + np.linalg.norm(c - fc) / scale
            if sc < bs:
                best, bs = i, sc
        if best is not None and bs < 1e-3:
            out[tag] = best
            used.add(best)
    return out
