import json
import math
import os
import time

import pytest

from daedalus.ai.tools import TOOLS, ToolExecutor
from daedalus.core.document import Document
from daedalus.core.features import FeatureError
from daedalus.io import formats
from daedalus.io import swbridge


def two_part_doc():
    d = Document()
    d.set_object("Object1", new_name="Base")
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 40},
                                  {"type": "circle", "x": 0, "y": 0, "r": 5, "mode": "subtract"}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    d.add_object("Pin", "#3366cc", pose=[0, 0, 4, 0, 0, 0.5])
    d.add("sketch", {"entities": [{"type": "circle", "x": 0, "y": 0, "r": 5}]}, obj="Pin")
    d.add("extrude", {"sketch": "Sketch2", "depth": 30}, obj="Pin")
    return d


BASE_V = 40 * 40 * 10 - math.pi * 25 * 10
PIN_V = math.pi * 25 * 30


def test_step_assembly_roundtrip(tmp_path):
    d = two_part_doc()
    d.export(tmp_path / "a.step")
    e = Document()
    s = e.import_file(tmp_path / "a.step")
    assert s["objects"] == ["Base", "Pin"]
    pin = e.obj("Pin")
    assert pin.color == "#3366cc"
    assert pin.pose == pytest.approx([0, 0, 4, 0, 0, 0.5], abs=1e-6)
    assert e.result.bodies["Base"].volume == pytest.approx(BASE_V, rel=1e-6)
    assert e.result.bodies["Pin"].volume == pytest.approx(PIN_V, rel=1e-6)
    assert not e.result.errors
    # one undo step removes the whole import
    e.undo()
    assert [f.TYPE for f in e.features] == []


@pytest.mark.parametrize("ext,rel", [("iges", 1e-6), ("brep", 1e-6), ("stl", 2e-3), ("obj", 2e-3), ("3mf", 2e-3)])
def test_roundtrip_formats(tmp_path, ext, rel):
    d = two_part_doc()
    d.export(tmp_path / f"a.{ext}")
    e = Document()
    s = e.import_file(tmp_path / f"a.{ext}")
    assert len(s["objects"]) == 2                    # separate solids → separate objects
    vols = sorted(b.volume for b in e.placed_bodies().values())
    assert vols == pytest.approx(sorted([BASE_V, PIN_V]), rel=rel)
    if ext == "3mf":
        assert "#3366cc" in [o.color for o in e.objects]


def test_other_exports_write(tmp_path):
    d = two_part_doc()
    for ext in ("glb", "gltf"):
        d.export(tmp_path / f"a.{ext}")
        assert (tmp_path / f"a.{ext}").stat().st_size > 100
    with pytest.raises(FeatureError):
        d.export(tmp_path / "a.xyz")


def _box_mesh(x0, y0, z0, s):
    v = [(x0 + i * s, y0 + j * s, z0 + k * s) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    idx = lambda i, j, k: i * 4 + j * 2 + k  # noqa: E731
    quads = [(idx(0, 0, 0), idx(0, 1, 0), idx(1, 1, 0), idx(1, 0, 0)), (idx(0, 0, 1), idx(1, 0, 1), idx(1, 1, 1), idx(0, 1, 1)),
             (idx(0, 0, 0), idx(1, 0, 0), idx(1, 0, 1), idx(0, 0, 1)), (idx(0, 1, 0), idx(0, 1, 1), idx(1, 1, 1), idx(1, 1, 0)),
             (idx(0, 0, 0), idx(0, 0, 1), idx(0, 1, 1), idx(0, 1, 0)), (idx(1, 0, 0), idx(1, 1, 0), idx(1, 1, 1), idx(1, 0, 1))]
    tris = []
    for a, b, c, dd in quads:
        tris += [(a, b, c), (a, c, dd)]
    return v, tris


def _write_stl(path, meshes):
    lines = ["solid t"]
    for v, tris in meshes:
        for a, b, c in tris:
            lines += ["facet normal 0 0 0", "outer loop"] + [f"vertex {x} {y} {z}" for x, y, z in (v[a], v[b], v[c])] + \
                     ["endloop", "endfacet"]
    lines.append("endsolid t")
    path.write_text("\n".join(lines))


def test_stl_box_merges_faces(tmp_path):
    _write_stl(tmp_path / "b.stl", [_box_mesh(0, 0, 0, 10)])
    e = Document()
    e.import_file(tmp_path / "b.stl")
    b = e.result.body
    assert b.volume == pytest.approx(1000)
    assert len(b.faces()) == 6                       # 12 triangles merged into 6 planar faces
    # build on it: a hole through the top
    e.add("sketch", {"plane": "top", "entities": [{"type": "circle", "x": 5, "y": 5, "r": 2}]})
    e.add("extrude", {"sketch": "Sketch1", "operation": "cut", "end": "through_all"})
    assert not e.result.errors
    assert e.result.body.volume == pytest.approx(1000 - math.pi * 4 * 10, rel=1e-6)


def test_stl_cavity_not_filled(tmp_path):
    v, t = _box_mesh(0, 0, 0, 10)
    vi, ti = _box_mesh(3, 3, 3, 4)
    ti = [(a, c, b) for a, b, c in ti]               # inner shell faces inward
    _write_stl(tmp_path / "c.stl", [(v, t), (vi, ti)])
    e = Document()
    s = e.import_file(tmp_path / "c.stl")
    assert len(s["objects"]) == 1
    assert e.result.body.volume == pytest.approx(1000 - 64)


def _plate_dxf(path, units=1):
    import ezdxf
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = units
    m = doc.modelspace()
    b = math.tan(math.radians(90) / 4)
    r = 0.25
    m.add_lwpolyline([(r, 0, 0), (4 - r, 0, b), (4, r, 0), (4, 2 - r, b), (4 - r, 2, 0), (r, 2, b), (0, 2 - r, 0), (0, r, b)],
                     format="xyb", close=True)
    m.add_circle((1, 1), 0.25)
    m.add_line((2.5, 0.5), (3.5, 0.5))
    m.add_line((3.5, 0.5), (3.5, 1.5))
    m.add_arc((3, 1.5), 0.5, 0, 180)
    m.add_line((2.5, 1.5), (2.5, 0.5))
    m.add_line((-1, 1), (5, 1))                      # centre line → construction
    m.add_text("note")
    doc.saveas(str(path))
    return (4 * 2 - (4 - math.pi) * r * r - math.pi * 0.25 ** 2 - (1 + math.pi * 0.25 / 2)) * 25.4 ** 2


def test_dxf_import_extrude_and_export(tmp_path):
    area = _plate_dxf(tmp_path / "plate.dxf")
    d = Document()
    s = d.import_file(tmp_path / "plate.dxf")
    assert s["kind"] == "dxf" and s["features"] == ["plate"]
    assert any("TEXT" in n for n in s["notes"]) and any("construction" in n for n in s["notes"])
    d.add("extrude", {"sketch": "plate", "depth": 5})
    assert not d.result.errors
    assert d.result.body.volume == pytest.approx(area * 5, rel=1e-9)
    n = d.export_sketch_dxf("plate", tmp_path / "out.dxf")
    assert n >= 13
    e = Document()
    s2 = e.import_file(tmp_path / "out.dxf")
    e.add("extrude", {"sketch": s2["features"][0], "depth": 5})
    assert e.result.body.volume == pytest.approx(area * 5, rel=1e-9)


def test_large_dxf_is_quick(tmp_path):
    import ezdxf
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    n = 400
    doc.modelspace().add_lwpolyline([(50 * math.cos(2 * math.pi * i / n), 50 * math.sin(2 * math.pi * i / n))
                                     for i in range(n)], close=True)
    doc.saveas(str(tmp_path / "big.dxf"))
    d = Document()
    t = time.perf_counter()
    s = d.import_file(tmp_path / "big.dxf")
    d.add("extrude", {"sketch": s["features"][0], "depth": 2})
    assert time.perf_counter() - t < 20
    assert d.result.body.volume == pytest.approx(0.5 * n * 50 ** 2 * math.sin(2 * math.pi / n) * 2, rel=1e-6)


def test_save_open_keeps_geometry_and_undo_stays_small(tmp_path):
    d = two_part_doc()
    d.export(tmp_path / "a.step")
    e = Document()
    e.import_file(tmp_path / "a.step")
    assert all(f.params["data"].startswith("blob:") for f in e.features)
    e.add("sketch", {"entities": [{"type": "circle", "x": 15, "y": 15, "r": 2}]}, obj="Base")
    assert len(e._undo[-1]) < 20000                  # snapshots reference geometry, don't copy it
    e.save(tmp_path / "x.daedalus")
    formats.BLOBS.clear()
    formats._cache.clear()
    f = Document.open(tmp_path / "x.daedalus")
    assert f.result.bodies["Pin"].volume == pytest.approx(PIN_V, rel=1e-6)
    assert not f.result.errors


def _sw_files(tmp_path):
    d = two_part_doc()
    d.obj("Pin").pose = [0, 0, 4, 0, 0, 0]
    (tmp_path / "asm.sldasm").write_text("fake")
    time.sleep(0.02)
    d.export(tmp_path / "asm.step")
    side = {"components": [{"name": "Base-1", "fixed": True, "box": [-20, -20, 0, 20, 20, 10]},
                           {"name": "Pin-1", "fixed": False, "box": [-5, -5, 4, 5, 5, 34]}],
            "mates": [{"name": "Concentric1", "type": "concentric",
                       "a": {"component": "Base-1", "kind": "axis", "p": [0, 0, 5], "d": [0, 0, 1], "r": 5},
                       "b": {"component": "Pin-1", "kind": "axis", "p": [0, 0, 20], "d": [0, 0, 1], "r": 5}, "value": 0},
                      {"name": "Distance1", "type": "distance",
                       "a": {"component": "Base-1", "kind": "plane", "p": [0, 0, 10], "n": [0, 0, 1]},
                       "b": {"component": "Pin-1", "kind": "plane", "p": [0, 0, 34], "n": [0, 0, 1]}, "value": None},
                      {"name": "Gear1", "type": "gear", "a": {}, "b": {}}],
            "skipped": [{"name": "Width1", "reason": "width mates aren't supported"}]}
    (tmp_path / ("asm" + swbridge.SIDE_SUFFIX)).write_text(json.dumps(side))


def test_solidworks_sidecar_mates(tmp_path):
    _sw_files(tmp_path)
    e = Document()
    s = e.import_file(tmp_path / "asm.sldasm")
    assert s["kind"] == "solidworks"
    assert s["mates"] == ["Concentric1", "Distance1"]
    assert e.obj("Base").fixed
    assert e.mate("Distance1").value == pytest.approx(24)
    assert e.result.assembly.dof == 1                # spins about the pin axis
    assert any("Gear1" in n for n in s["notes"]) and any("Width1" in n for n in s["notes"])
    e.drive("Distance1", 24, 10, 4)
    assert e.obj("Pin").pose[2] == pytest.approx(-10, abs=1e-4)


def test_solidworks_without_solidworks_explains(tmp_path):
    (tmp_path / "p.sldprt").write_text("fake")
    with pytest.raises(FeatureError) as ei:
        Document().import_file(tmp_path / "p.sldprt")
    assert "STEP" in str(ei.value)


class _Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_swbridge_mate_extraction_with_fake_com():
    base = _Fake(Name2="Base-1")
    pin = _Fake(Name2="Pin-1")
    face_plane = _Fake(GetSurface=lambda: _Fake(IsPlane=lambda: True))
    edge = _Fake(GetCurve=lambda: None)
    ents = [_Fake(EntityParams=[0, 0, 0.005, 0, 0, 1, 0.005], ReferenceComponent=base, Reference=None),
            _Fake(EntityParams=[0, 0, 0.02, 0, 0, 1, 0.005], ReferenceComponent=pin, Reference=None),
            _Fake(EntityParams=[0, 0, 0.01, 0, 0, 1], ReferenceComponent=base, Reference=face_plane),
            _Fake(EntityParams=[0, 0, 0.034, 1, 0, 0], ReferenceComponent=pin, Reference=edge)]

    def feat(tname, idx, value=None, mtype=0):
        mate = _Fake(Type=mtype, MateEntityCount=len(idx), MateEntity=lambda i: ents[idx[i]])
        definition = _Fake(Distance=value) if value is not None else None
        return _Fake(Name=tname + "1", GetTypeName2=tname, IsSuppressed=False, GetSpecificFeature2=lambda: mate,
                     GetDefinition=lambda: definition)

    mates, skipped = [], []
    swbridge._one_mate(feat("MateConcentric", [0, 1]), "Concentric1", mates, skipped)
    swbridge._one_mate(feat("MateDistanceDim", [2, 2], value=0.024), "Distance1", mates, skipped)
    swbridge._one_mate(feat("MateHinge", [0, 1, 2, 2]), "Hinge1", mates, skipped)
    swbridge._one_mate(feat("MateGear", [0, 1], mtype=10), "Gear1", mates, skipped)
    swbridge._one_mate(feat("MateParallel", [2, 3]), "Par1", mates, skipped)
    assert [m["type"] for m in mates] == ["concentric", "distance", "hinge", "coincident", "parallel"]
    assert mates[0]["a"] == {"component": "Base-1", "kind": "axis", "p": [0, 0, 5], "d": [0, 0, 1], "r": 5}
    assert mates[1]["value"] == pytest.approx(24) and mates[1]["a"]["kind"] == "plane"
    assert mates[4]["b"]["kind"] == "axis"            # an edge → line/axis reference
    assert skipped == [{"name": "Gear1", "reason": "gear mates aren't supported"}]


def test_claude_file_tools(tmp_path):
    names = {t["name"] for t in TOOLS}
    assert {"import_file", "export_file"} <= names and "add_import" not in names
    d = two_part_doc()
    ex = ToolExecutor(d)
    out, err = ex.run("export_file", {"path": str(tmp_path / "a.step")})
    assert not err, out
    out, err = ex.run("export_file", {"path": str(tmp_path / "s.dxf"), "sketch": "Sketch1"})
    assert not err, out
    e = Document()
    ex = ToolExecutor(e)
    out, err = ex.run("import_file", {"path": str(tmp_path / "a.step")})
    assert not err and json.loads(out)["objects"] == ["Base", "Pin"]
    state = json.dumps(ex.model_state())
    assert "blob:" not in state and len(state) < 20000
    out, err = ex.run("edit_feature", {"name": "Pin", "params": {"data": "x"}})
    assert err
    out, err = ex.run("import_file", {"path": str(tmp_path / "missing.step")})
    assert err


def test_solidworks_nauo_names(tmp_path):
    """SolidWorks writes instance names as NAUO1, NAUO2… — use the part names, numbered like SolidWorks."""
    import re
    from build123d import Box, Compound, Pos, export_step
    from daedalus.io.formats import import_step_fast, is_generic_name
    b, c = Box(10, 10, 10), Box(5, 5, 20)
    asm = Compound(children=[Pos(0, 0, 0) * b, Pos(30, 0, 0) * b, Pos(60, 0, 0) * c])
    for ch, lab in zip(asm.children, ["Bracket", "Bracket", "Pin"]):
        ch.label = lab
    p = tmp_path / "a.step"
    export_step(asm, str(p))
    k = [0]

    def sub(_m):
        k[0] += 1
        return f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('NAUO{k[0]}','NAUO{k[0]}',"
    p.write_text(re.sub(r"NEXT_ASSEMBLY_USAGE_OCCURRENCE\('[^']*','[^']*',", sub, p.read_text()))
    assert [x.name for x in import_step_fast(p).parts] == ["Bracket-1", "Bracket-2", "Pin"]
    assert is_generic_name("NAUO12") and not is_generic_name("Part1") and not is_generic_name("M40-0001")
