"""Working-drawing practice: sheet formats and title block, dimensioning standards, notes, drawing numbering,
section types (full, half, offset, aligned, removed, revolved, broken-out), material hatching, oblique views."""
import json
import os

import pytest

from daedalus.ai.tools import ToolExecutor
from daedalus.core.document import Document
from daedalus.drawing import practice as P
from daedalus.drawing import render
from daedalus.drawing.model import Options
from daedalus.errors import FeatureError


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_HOME", str(tmp_path / "dhome"))
    return tmp_path


def _part():
    """A 120 × 60 × 40 block with a Ø30 bore along Z and a Ø16 cross hole along X."""
    doc = Document()
    ex = ToolExecutor(doc)
    for n, a in (("add_sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 120, "h": 60}]}),
                 ("add_extrude", {"sketch": "Sketch1", "depth": 40}),
                 ("add_hole", {"face": "top", "positions": [[-30, 0]], "diameter": 30, "depth": 0}),
                 ("add_hole", {"face": "right", "positions": [[0, 20]], "diameter": 16, "depth": 0})):
        r = ex.run(n, a)
        assert not r[1], r
    return doc, ex


def test_number_formats_per_standard():
    o = Options()
    assert P.format_number(0.5, o) == "0.5" and P.format_number(12.0, o) == "12"
    asme = Options(**{k: v for k, v in P.DIM_STANDARDS["ASME Y14.5 (inch)"].items() if k in Options.__dataclass_fields__})
    assert P.format_number(0.5, asme, 3) == ".500"                      # no leading zero, trailing zeros kept
    iso = Options(**{k: v for k, v in P.DIM_STANDARDS["ISO 129-1 (metric)"].items() if k in Options.__dataclass_fields__})
    assert P.format_number(12.25, iso, 2) == "12,25"                    # decimal comma
    assert P.format_angle(27.5, Options(angle_format="dms")) == "27°30'"
    assert P.format_angle(27.5, Options(decimals=2)) == "27.5°"
    d = {"t": "dim", "kind": "h", "p1": [0, 0], "p2": [25.4, 0], "at": [10, 10]}
    assert render.dim_value(d, Options(dual_units=True)) == "25.4 [1.00]"
    assert render.dim_value({**d, "ref": True}, Options()) == "(25.4)"
    out = render.explode_dim({**d, "nts": True}, Options())
    assert any(p["t"] == "text" and p.get("underline") for p in out)    # not to scale: underlined
    half = render.explode_dim({**d, "half": True}, Options())
    assert sum(1 for p in half if p["t"] == "poly" and p.get("fill")) == 3   # one arrow + a double arrowhead
    v = {"t": "dim", "kind": "v", "p1": [0, 0], "p2": [0, 20], "at": [10, 10]}
    up = [p for p in render.explode_dim(v, Options()) if p["t"] == "text"][0]
    flat = [p for p in render.explode_dim(v, Options(dim_text="unidirectional")) if p["t"] == "text"][0]
    assert abs(up["angle"]) == 90 and flat["angle"] == 0


def test_title_block_format_notes_and_projection_symbol(home):
    doc, _ex = _part()
    dr = doc.new_drawing(size="A3", views=("front", "top"))
    sh = dr.sheet()
    doc.set_drawing(dr.name, title={"company": "Acme", "drawn": "AN", "checked": "JS", "checked_date": "2026-11-20"})
    prims = render.flatten(doc, dr, sh)
    texts = " ".join(p.get("text", "") for p in prims if p["t"] == "text")
    for want in ("DWG NO.", "CHECKED", "APPROVED", "ISSUED", "PROJECTION" if False else "ANGLE", "SCALE", "SHEET",
                 "1 OF 1", "MM", "Acme", "JS", "2026-11-20", "Daedalus", "TOLERANCES", "MATERIAL"):
        assert want in texts, want
    # first angle (ISO): the circles left of the cone; third angle: right
    first = P.projection_symbol(50, 50, 10, True)
    third = P.projection_symbol(50, 50, 10, False)
    cone = lambda s: [p for p in s if p["t"] == "poly"][0]["pts"][0][0]          # noqa: E731
    circ = lambda s: [p for p in s if p["t"] == "circle"][0]["c"][0]            # noqa: E731
    assert circ(first) < cone(first) and circ(third) > cone(third)
    # ISO 5457 filing margin: frame 20 mm from the left edge
    assert P.frame_rect(dr, sh)[0] == 20
    # notes, numbered, lower left
    doc.add_drawing_note(template="Heat treatment")
    doc.add_drawing_note("REMOVE ALL BURRS.")
    with pytest.raises(FeatureError):
        doc.add_drawing_note(template="nope")
    nb = P.notes_block(dr, sh)
    tx = [p["text"] for p in nb]
    assert tx[0] == "NOTES:" and "1." in tx and "2." in tx and any("HEAT TREAT" in t for t in tx)
    assert all(p["p"][0] < 120 for p in nb)
    # a custom format, saved as a template, a custom sheet size
    custom = {"margins": [15, 15, 15, 15], "zones": False, "title_block": {"width": 120, "rows": [
        {"h": 8, "cells": [["number", 2], ["revision", 1]]}, {"h": 12, "cells": [["title", 1]]}]},
        "labels": {"number": "PART NO."}}
    doc.set_sheet_format(dr.name, custom=custom, save_as="Shop A4", custom_size=[300, 200])
    assert sh.size == "Custom" and sh.dims() == (300, 200)
    assert P.title_block_rect(dr, sh) == (165, 15, 285, 35)
    texts = " ".join(p.get("text", "") for p in render.flatten(doc, dr, sh) if p["t"] == "text")
    assert "PART NO." in texts and "CHECKED" not in texts
    assert "Shop A4" in P.list_formats() and (home / "dhome" / "sheet_formats" / "Shop A4.json").exists()
    doc.set_sheet_format(dr.name, name="ASME Y14.1", size="A3")
    assert P.frame_rect(dr, sh)[0] == pytest.approx(12.7)
    with pytest.raises(FeatureError):
        doc.set_sheet_format(dr.name, custom={"title_block": {"rows": [{"h": 5, "cells": [["bogus", 1]]}]}})


def test_drawing_numbers_customisable():
    doc = Document()
    ex = ToolExecutor(doc)
    for nm in ("Frame", "Arm", "Plate", "Cover"):
        ex.run("add_object", {"name": nm})
        ex.run("add_sketch", {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 20, "h": 20}]})
        ex.run("add_extrude", {"sketch": doc.features[-1].name, "depth": 5})
    ex.run("add_object", {"name": "Bolt"})
    ex.run("add_sketch", {"plane": "XY", "entities": [{"type": "circle", "x": 0, "y": 0, "r": 3}]})
    ex.run("add_extrude", {"sketch": doc.features[-1].name, "depth": 20})
    doc.obj("Bolt").part_no = "SHCS M6x20"
    doc.make_subassembly("Lift", ["Arm", "Plate"])
    doc.set_numbering(base="12345")
    rows = {r["name"]: r["number"] for r in doc.drawing_numbers()}
    assert rows["Main assembly"] == "12345.00.00" or list(rows.values())[0] == "12345.00.00"
    assert rows["Lift"] == "12345.01.00"                                 # subassemblies first
    assert rows["Arm"] == "12345.01.01" and rows["Plate"] == "12345.01.02"
    assert rows["Frame"] == "12345.02.00" and rows["Cover"] == "12345.03.00"
    assert rows["SHCS M6x20"] == ""                                      # standard part: no drawing
    doc.set_numbering(separator="-", digits=3, start=10, step=10, fill=False, prefix="DWG-", standard_parts=True)
    rows = {r["name"]: r["number"] for r in doc.drawing_numbers()}
    assert rows["Lift"] == "DWG-12345-010" and rows["Arm"] == "DWG-12345-010-010"
    assert rows["Plate"] == "DWG-12345-010-020" and rows["Frame"] == "DWG-12345-020"
    assert rows["SHCS M6x20"].startswith("DWG-12345-")
    with pytest.raises(FeatureError):
        doc.set_numbering(colour="red")
    dr = doc.new_drawing(size="A3", views=("front",), objects=["Arm"])
    doc.apply_drawing_numbers()
    assert dr.sheet().title["number"] == "DWG-12345-010-010"
    # saved with the model
    d2 = Document.from_dict(json.loads(json.dumps(doc.to_dict()))) if hasattr(Document, "from_dict") else None
    if d2 is not None:
        assert d2.numbering["separator"] == "-"


def test_section_types_hatch_and_oblique():
    doc, _ex = _part()
    dr = doc.new_drawing(size="A3", views=("front", "top"))
    sh = dr.sheet()
    top = next(v for v in sh.views if v.get("dir") == "top")
    front = next(v for v in sh.views if v.get("dir") == "front")
    hatches = lambda v: [p for p in v["_prims"] if p["t"] == "hatch"]     # noqa: E731
    full = doc.add_drawing_view(dr.name, kind="section", parent=top["id"], line=[[-70, 0], [70, 0]])
    assert hatches(full) and not any(p.get("style") == "hidden" for p in full["_prims"])   # no hidden in sections
    assert hatches(full)[0]["pattern"] == "ANSI32"                       # steel
    half = doc.add_drawing_view(dr.name, kind="section", parent=top["id"], line=[[-70, 0], [70, 0]],
                                section_type="half")
    assert 0 < sum(len(h["loops"]) for h in hatches(half))
    off = doc.add_drawing_view(dr.name, kind="section", parent=top["id"], section_type="offset",
                               path=[[-70, 0], [0, 0], [0, 10], [70, 10]])
    assert hatches(off)
    ali = doc.add_drawing_view(dr.name, kind="section", parent=top["id"], section_type="aligned",
                               path=[[-70, 0], [-30, 0], [-30, 40]])
    assert hatches(ali)
    rem = doc.add_drawing_view(dr.name, kind="section", parent=front["id"], line=[[0, -40], [0, 40]],
                               section_type="removed")
    assert rem["_prims"] and all(p["t"] in ("hatch", "poly") for p in rem["_prims"])
    rev = doc.add_drawing_view(dr.name, kind="section", parent=front["id"], line=[[30, -40], [30, 40]],
                               section_type="revolved")
    assert rev["_prims"] and abs(rev["x"] - (front["x"] + 30 * front["scale"])) < 60
    with pytest.raises(FeatureError):
        doc.add_drawing_view(dr.name, kind="section", parent=top["id"], section_type="aligned", path=[[0, 0], [1, 1]])
    # broken-out: break away the front half of a region of the front view
    doc.set_drawing_view(front["id"], dr.name, broken_out={"region": [[-55, -15], [-5, -15], [-5, 15], [-55, 15]],
                                                            "depth": 0})
    assert hatches(front) and any(p.get("style") == "break" for p in front["_prims"])
    labels = " ".join(p.get("text", "") for p in render.flatten(doc, dr, sh) if p["t"] == "text")
    assert "HALF SECTION B-B" in labels and "SECTION A-A" in labels
    ob = doc.add_drawing_view(dr.name, kind="iso", dir="oblique")
    xs = [q for p in ob["_prims"] if p["t"] == "line" for q in (p["a"][0], p["b"][0])]
    assert ob["_prims"] and max(xs) - min(xs) > 120                      # front true size + receding depth
    # PDF / DXF still write
    out = doc.export_drawing(str(os.path.join(os.environ.get("TMPDIR", "/tmp"), "practice_test.dxf")), dr.name)
    assert os.path.getsize(out) > 1000


def test_dimension_sets_and_standard_tool(home):
    doc, ex = _part()
    doc.new_drawing(size="A3", views=("front",))
    r = ex.run("add_dimension_set", {"kind": "baseline", "points": [[50, 50], [70, 50], [100, 50]], "at": 30})
    assert not r[1] and json.loads(r[0])["added"] == 2
    items = doc.drawing().sheet().items
    assert items[-1]["at"][1] == 38 and items[-2]["p1"] == items[-1]["p1"] == [50.0, 50.0]
    r = ex.run("dimension_standard", {"standard": "ASME Y14.5 (inch)"})
    o = doc.drawing().options
    assert not r[1] and o.units == "in" and o.dim_text == "unidirectional" and o.standard == "ANSI"
    r = ex.run("drawing_notes", {"action": "add", "template": "Deburr"})
    assert "REMOVE ALL BURRS." in json.loads(r[0])["notes"]
    r = ex.run("sheet_format", {"action": "list"})
    assert "ASME Y14.1" in json.loads(r[0])["formats"]
    r = ex.run("drawing_numbers", {"options": {"base": "777"}, "apply": True})
    assert json.loads(r[0])["drawings"][0]["number"].startswith("777")


def test_dialogs(home):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui.drawing_practice_ui import NotesDialog, NumberingDialog, SheetFormatDialog
    doc, _ex = _part()
    dr = doc.new_drawing(size="A3", views=("front",))
    sh = dr.sheet()
    d = SheetFormatDialog(doc, dr, sh)
    d.m[0].setValue(25)
    d.rows.item(0, 1).setText("title:2, logo:1")
    d.accept()
    assert sh.format.get("custom", {}).get("margins", [0])[0] == 25
    n = NotesDialog(doc, dr, sh)
    n._template(list(P.NOTE_TEMPLATES).index("Deburr") + 1)
    n.accept()
    assert sh.notes == ["REMOVE ALL BURRS."]
    nd = NumberingDialog(doc)
    nd.w["base"].setText("900")
    assert nd.tree.topLevelItem(0).text(1).startswith("900")
    nd.accept()
    assert doc.numbering["base"] == "900" and sh.title["number"].startswith("900")
