"""Doodle recogniser: realistic hand noise (sub-pixel jitter + slow wobble), fixed seeds."""
import math
import random

import pytest

from daedalus.core import Document
from daedalus.core.doodle import DoodleOptions, doodle_to_geometry, recognise_stroke


def hand(pts, rng, j=0.7, wob=2.0):
    ph1, ph2 = rng.uniform(0, 6.3), rng.uniform(0, 6.3)
    n = max(len(pts) - 1, 1)
    return [(x + wob * math.sin(6 * i / n + ph1) + rng.uniform(-j, j),
             y + wob * math.sin(5 * i / n + ph2) + rng.uniform(-j, j)) for i, (x, y) in enumerate(pts)]


def line(a, b, step=4):
    n = max(2, int(math.dist(a, b) / step))
    return [(a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n) for t in range(n + 1)]


def poly(*pts):
    out = []
    for a, b in zip(pts, pts[1:]):
        out += line(a, b)[:-1]
    return out + [pts[-1]]


def circ(cx, cy, r, a0=0, a1=360):
    n = max(8, int(abs(a1 - a0) / 360 * 2 * math.pi * r / 4))
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def sc(pts, s=1.5):
    return [(x * s, y * s) for x, y in pts]


SHAPES = {
    "rect": (poly((0, 0), (300, 0), (300, 160), (0, 160), (0, 0)), "llll"),
    "triangle": (poly((0, 0), (200, 0), (60, 150), (0, 0)), "lll"),
    "circle": (circ(0, 0, 70, 20, 385), "c"),
    "open L": (poly((0, 0), (160, 0), (160, 120)), "ll"),
    "slot": (poly((0, 0), (150, 0)) + circ(150, 30, 30, -90, 90)[1:] + poly((150, 60), (0, 60))
             + circ(0, 30, 30, 90, 270)[1:], "lala"),
    "bracket": (poly((0, 0), (240, 0), (240, 30), (30, 30), (30, 180), (0, 180), (0, 0)), "llllll"),
}


@pytest.mark.parametrize("seed", [1, 2, 3, 5, 9])
@pytest.mark.parametrize("name", list(SHAPES))
def test_recognises_shapes(name, seed):
    rng = random.Random(seed * 101 + len(name))
    pts, want = SHAPES[name]
    prims, closed = recognise_stroke(hand(sc(pts), rng), 450)
    got = "".join(p.kind[0] for p in prims)
    assert got == want or (closed and len(got) == len(want) and got in want + want), got


def test_arc_radius_recovered():
    rng = random.Random(3)
    pts, _ = SHAPES["slot"]
    prims, _ = recognise_stroke(hand(sc(pts), rng), 450)
    radii = sorted(p.r / 1.5 for p in prims if p.kind == "arc")
    assert radii == pytest.approx([30, 30], abs=4)


def test_doodle_to_solid_plate_with_holes():
    rng = random.Random(7)
    strokes = [poly((0, 0), (300, 0), (300, 160), (0, 160), (0, 0)), circ(70, 80, 30, 10, 380),
               circ(230, 80, 30, 200, 570)]
    g = doodle_to_geometry([hand(sc(s), rng) for s in strokes], DoodleOptions(size_mm=150))
    kinds = sorted(c["type"] for c in g["curves"])
    assert kinds == ["circle", "circle", "line", "line", "line", "line"]
    hv = sorted(k["type"] for k in g["constraints"])
    assert hv == ["horizontal", "horizontal", "vertical", "vertical"]
    doc = Document()
    doc.add("sketch", {"geometry": g})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 10})
    assert not doc.result.errors
    bb = doc.mass_properties()["bbox_mm"]
    assert bb[0] == pytest.approx(150, abs=1) and bb[1] == pytest.approx(80, abs=4)
    # two holes of ~Ø30 removed from a ~150×80 plate
    assert doc.result.body.volume == pytest.approx(10 * (bb[0] * bb[1] - 2 * math.pi * 15 ** 2), rel=0.06)


def test_separate_strokes_join_into_closed_loop():
    rng = random.Random(11)
    strokes = [line((0, 0), (300, 0)), line((302, -3), (300, 160)), line((298, 162), (0, 160)),
               line((-2, 158), (0, 2))]
    g = doodle_to_geometry([hand(sc(s), rng) for s in strokes], DoodleOptions(size_mm=200))
    doc = Document()
    doc.add("sketch", {"geometry": g})
    doc.add("extrude", {"sketch": "Sketch1", "depth": 5})
    assert not doc.result.errors
    assert doc.mass_properties()["bbox_mm"][0] == pytest.approx(200, abs=1)
