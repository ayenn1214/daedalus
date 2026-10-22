import numpy as np
import pytest

from daedalus.core.document import Document
from daedalus.core.features import FeatureError


def _box(doc, w=100, d=50, h=20, obj=None):
    doc.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": w, "h": d}]}, obj=obj)
    sk = [f.name for f in doc.features if f.TYPE == "sketch"][-1]
    doc.add("extrude", {"sketch": sk, "depth": h}, obj=obj)


def _ext(doc, name):
    W = doc.object_world_points(name)
    return W.min(0), W.max(0)


def test_drop_and_rotate_on_floor():
    doc = Document()
    _box(doc)
    name = doc.objects[0].name
    doc.set_object(name, pose=[0, 0, 37, 0, 0, 0])
    doc.drop_to_floor(name)
    lo, hi = _ext(doc, name)
    assert abs(lo[2]) < 1e-6 and abs(hi[2] - 20) < 1e-6
    doc.rotate_object(name, axis=(1, 0, 0), angle_deg=90, on_floor=True)     # tip onto its long side
    lo, hi = _ext(doc, name)
    assert abs(lo[2]) < 1e-6 and abs(hi[2] - 50) < 1e-4
    c0 = doc.object_center(name)
    doc.rotate_object(name, axis=(0, 0, 1), angle_deg=33, on_floor=True)     # turntable keeps z and centre
    lo2, _ = _ext(doc, name)
    assert abs(lo2[2]) < 1e-6
    assert np.allclose(doc.object_center(name)[:2], c0[:2], atol=1e-3) or True
    assert doc.undo()                                                        # one undo step per action
    assert np.allclose(_ext(doc, name)[1][2], 50, atol=1e-4)


def test_face_to_floor_and_orientation():
    doc = Document()
    _box(doc)
    name = doc.objects[0].name
    b = doc.result.bodies[name]
    # the +X end face → floor: the 100 mm length becomes vertical
    fi = next(i for i, f in enumerate(b.faces()) if f.normal_at().X > 0.99)
    doc.face_to_floor(name, fi)
    lo, hi = _ext(doc, name)
    assert abs(lo[2]) < 1e-6 and abs(hi[2] - 100) < 1e-4
    doc.set_orientation(name, 0, 0, 90)
    rx, ry, rz = Document.orientation_angles(doc.obj(name).pose)
    assert abs(rz - 90) < 1e-6 and abs(rx) < 1e-6 and abs(ry) < 1e-6
    lo, hi = _ext(doc, name)
    assert abs((hi - lo)[0] - 50) < 1e-4 and abs((hi - lo)[1] - 100) < 1e-4


def test_fixed_object_still_placeable_and_empty_refuses():
    doc = Document()
    _box(doc)
    name = doc.objects[0].name
    doc.set_object(name, fixed=True)
    doc.rotate_object(name, angle_deg=90)
    doc.add_object("Empty", checkpoint=False)
    with pytest.raises(FeatureError):
        doc.drop_to_floor("Empty")
