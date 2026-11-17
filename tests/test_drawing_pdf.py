"""Reading drawing PDFs: title block, BOM, revisions, notes → matched to DIMS parts, chosen fields applied."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from daedalus.inventory import drawing_pdf as D  # noqa: E402
from daedalus.inventory import model as M  # noqa: E402

app = QApplication.instance() or QApplication([])


def drawing(path, pn="C20-0040", desc="WLDMT, SHIP BRKT, HORIZ BEAM", weight="5.766 LB", rev="A", bom=True):
    """A one-page landscape drawing laid out like a SOLIDWORKS sheet (labels and values as separate text)."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter
    w = QPdfWriter(str(path))
    w.setResolution(72)
    w.setPageLayout(QPageLayout(QPageSize(QSizeF(792, 612), QPageSize.Point), QPageLayout.Portrait,
                                QMarginsF(0, 0, 0, 0)))
    p = QPainter(w)

    def t(x, y, s, size=5):
        f = QFont("Arial")
        f.setPixelSize(size)
        p.setFont(f)
        p.drawText(x, y, s)
    t(237, 40, "8.25", 8)
    t(504, 355, "DO NOT WELD", 8)
    t(504, 364, "ON THIS SIDE", 8)
    if bom:
        for x, s in ((546, "ITEM NO."), (592, "PART NUMBER"), (680, "DESCRIPTION"), (765, "QTY.")):
            t(x, 466, s, 8)
        for y, (i, n, d) in ((479, ("1", "C10-0103", "SHIP BRKT, HORIZ BEAM, BASE")),
                             (491, ("2", "C10-0110", "SHIP BRKT, HORIZ BEAM, WALL"))):
            t(561, y, i, 8)
            t(593, y, n, 8)
            t(641, y, d, 8)
            t(772, y, "1", 8)
    t(473, 502, "THE INFORMATION CONTAINED IN THIS DRAWING IS THE SOLE PROPERTY")
    t(473, 508, "OF ACME. ANY REPRODUCTION IN PART OR AS A WHOLE WITHOUT")
    for x, y, lab, val in ((650, 538, "PART NUMBER", pn), (652, 547, "DESCRIPTION", desc),
                           (660, 556, "MATERIAL", "AS SHOWN"), (669, 565, "FINISH", "DEBURR"),
                           (664, 574, "DRAWN", "DS"), (671, 583, "DATE", "251209")):
        t(x, y, lab)
        t(692, y, val, 6)
    t(729, 583, "DATE")
    t(730, 574, "APV")
    t(473, 538, "ALL DIMENSIONS IN INCHES, DUAL DIMENSION [MM] IN SQUARE BRACKETS")
    t(474, 547, "GENERAL TOLERANCES")
    t(605, 547, 'X.XXX" ± 0.005"', 6)
    t(543, 556, "ANGULAR ± 1°", 6)
    t(608, 556, 'X.XX" ± 0.010"', 6)
    t(473, 574, "PLEASE DEBURR SHARP EDGES AND REMOVE ANY SCRATCHES/SPATTER")
    t(473, 588, "APPROX. WEIGHT")
    t(531, 588, weight, 8)
    t(651, 592, "SCALE (UNLESS NOTED)", 4)
    t(703, 592, "1:2", 4)
    t(722, 592, "REV.", 4)
    t(733, 592, rev, 5)
    t(746, 592, "SHEET 1 OF 1", 4)
    t(226, 556, "REVISIONS", 7)
    for x, s in ((28, "REV."), (189, "DESCRIPTION"), (391, "DATE"), (430, "REVISED BY")):
        t(x, 568, s, 7)
    t(32, 585, "A", 7)
    t(133, 585, 'C10-0110 MATERIAL HEIGHT REDUCED BY 5/16"', 7)
    t(380, 585, "12MAR2026", 7)
    t(444, 585, "DS", 7)
    p.end()
    return str(path)


def test_title_block_bom_revisions_and_notes(tmp_path):
    d = D.parse(drawing(tmp_path / "C20-0040.pdf"))
    assert d["source"] == "text"
    assert d["partNumber"] == "C20-0040" and d["description"] == "WLDMT, SHIP BRKT, HORIZ BEAM"
    assert d["finish"] == "DEBURR" and d["material"] is None and d["materialNote"] == "AS SHOWN"
    assert d["weightLb"] == pytest.approx(5.766) and d["rev"] == "A" and d["drawnBy"] == "DS"
    assert d["drawnDate"] == "251209" and d["scale"] == "1:2" and d["sheet"] == "1 of 1"
    assert [(b["item"], b["partNumber"], b["description"], b["qty"]) for b in d["bom"]] == [
        (1, "C10-0103", "SHIP BRKT, HORIZ BEAM, BASE", 1), (2, "C10-0110", "SHIP BRKT, HORIZ BEAM, WALL", 1)]
    assert d["revisions"] == [{"rev": "A", "description": 'C10-0110 MATERIAL HEIGHT REDUCED BY 5/16"',
                               "date": "12MAR2026", "by": "DS"}]
    assert "DO NOT WELD ON THIS SIDE" in d["notes"]
    assert not any("PROPERTY" in n or "ACME" in n for n in d["notes"])
    assert d["tolerances"].get("X.XXX") == "0.005"


def test_kg_weight_and_no_bom(tmp_path):
    d = D.parse(drawing(tmp_path / "c10.pdf", pn="C10-0103", desc="SHIP BRKT, HORIZ BEAM, BASE", weight="2.0 KG",
                        bom=False))
    assert d["partNumber"] == "C10-0103" and d["bom"] == [] and d["weightLb"] == pytest.approx(4.40924, rel=1e-3)


def test_existing_part_only_fills_blanks_by_default_and_bom_links(tmp_path):
    d = D.parse(drawing(tmp_path / "C20-0040.pdf"))
    inv = M.Inventory()
    wl = M.new_part(name="Ship bracket weldment", partNumber="C20-0040", type="manufactured", revision="-")
    base = M.new_part(name="Base", partNumber="C10-0103", type="manufactured")
    inv.parts = [wl, base]
    pr = D.proposals(inv, d)
    assert pr["part"] is wl and pr["isWeldment"]
    f = {x["key"]: x for x in pr["fields"]}
    assert f["description"]["current"] == "Ship bracket weldment" and not f["description"]["same"]
    assert f["weightLb"]["current"] is None and f["revision"]["new"] == "A"
    assert [b["part"] is base for b in pr["bom"]] == [True, False]
    res = D.apply(inv, d, pr, {"weightLb", "revision", "drawingUrl", "finish"}, bom=True, create_missing=True)
    assert res["part"] is wl and wl["name"] == "Ship bracket weldment"           # name not ticked: kept
    assert wl["weightLb"] == pytest.approx(5.766) and wl["revision"] == "A" and wl["drawingUrl"].endswith(".pdf")
    assert wl["finish"]["process"] == "DEBURR" and wl["isWeldment"]
    wall = next(p for p in inv.parts if p.get("partNumber") == "C10-0110")
    assert [c["partId"] for c in wl["components"]] == [base["id"], wall["id"]] and res["created"] == [wall]
    assert wl["revisionHistory"][0]["date"] == "12MAR2026"
    D.apply(inv, d, D.proposals(inv, d), set(), bom=False)                     # again: history not doubled
    assert len(wl["revisionHistory"]) == 1


def test_new_part_is_created(tmp_path):
    d = D.parse(drawing(tmp_path / "x.pdf", bom=False))
    inv = M.Inventory()
    pr = D.proposals(inv, d)
    assert pr["part"] is None
    res = D.apply(inv, d, pr, {f["key"] for f in pr["fields"]})
    p = res["part"]
    assert p["partNumber"] == "C20-0040" and p["name"] == "WLDMT, SHIP BRKT, HORIZ BEAM" and p["type"] == "manufactured"
    assert D.apply(M.Inventory(), d, D.proposals(M.Inventory(), d), set(), create_part=False)["part"] is None


def test_not_a_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    with pytest.raises(D.DrawingError):
        D.parse(bad)


def test_dialog_reviews_and_applies(tmp_path):
    from daedalus.ui.drawing_read_ui import ReadDrawingsDialog
    inv = M.Inventory()
    inv.parts = [M.new_part(name="Old name", partNumber="C20-0040", type="manufactured")]
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    saved = []
    dlg = ReadDrawingsDialog(inv, [drawing(tmp_path / "a.pdf"), str(bad)], saved.append)
    assert dlg.list.count() == 2 and dlg.items[0]["prop"]["part"] is inv.parts[0] and "error" in dlg.items[1]
    assert "description" not in dlg.items[0]["keys"] and "weightLb" in dlg.items[0]["keys"]
    dlg.list.setCurrentRow(1)
    dlg.list.setCurrentRow(0)
    dlg.apply()
    assert saved and inv.parts[0]["weightLb"] == pytest.approx(5.766) and inv.parts[0]["name"] == "Old name"


def test_new_parts_get_a_project_unless_inside_a_bom(tmp_path):
    from daedalus.ui.drawing_read_ui import ReadDrawingsDialog
    inv = M.Inventory()
    pid = inv.find_or_create_project("RFP 1308")
    inv.find_or_create_project("RFP")
    folder = tmp_path / "Product" / "RFP 1308" / "Drawings"
    folder.mkdir(parents=True)
    top = drawing(folder / "C20-0040.pdf")                                    # weldment with C10-0103 in its BOM
    child = drawing(folder / "C10-0103.pdf", pn="C10-0103", desc="SHIP BRKT, HORIZ BEAM, BASE", bom=False)
    assert D.project_from_path(inv, top) == pid and D.folder_project_name(top) == "RFP 1308"
    dlg = ReadDrawingsDialog(inv, [top, child], lambda m: None)
    a, b = dlg.items
    assert a["state"] == b["state"] == "new" and a["project"] == pid and b["parent"] == "C20-0040" \
        and b["project"] is None
    for i in range(2):
        dlg.list.setCurrentRow(i)                                             # both detail views build
    a["projQty"] = 2
    dlg.apply()
    wl = next(p for p in inv.parts if p["partNumber"] == "C20-0040")
    br = next(p for p in inv.parts if p["partNumber"] == "C10-0103")
    assert inv.allocation(wl, pid)["qty"] == 2 and not inv.allocations(br)
    assert [c["partId"] for c in wl["components"]] == [br["id"]] and wl["isWeldment"]     # linked within the batch
    inv2 = M.Inventory()
    dlg2 = ReadDrawingsDialog(inv2, [child], lambda m: None)
    it = dlg2.items[0]
    it["newProject"] = "Ship Brackets"
    dlg2.apply()
    pr = next(p for p in inv2.projects if p["name"] == "Ship Brackets")
    assert inv2.allocation(inv2.parts[0], pr["id"])["qty"] == 1


def test_folder_pick_keeps_newest_revision(tmp_path):
    from daedalus.ui.drawing_read_ui import newest_only
    paths = [str(tmp_path / n) for n in ("C20-0040 Rev A.pdf", "C20-0040 Rev C.pdf", "C20-0040 Rev B.pdf",
                                         "C10-0103.pdf")]
    assert sorted(newest_only(paths)) == sorted([paths[1], paths[3]])


def test_same_part_twice_newest_file_wins(tmp_path):
    import time
    from daedalus.ui.drawing_read_ui import ReadDrawingsDialog
    old = drawing(tmp_path / "old.pdf", weight="1.0 LB")
    time.sleep(0.01)
    new = drawing(tmp_path / "new.pdf", weight="2.0 LB")
    os.utime(old, (time.time() - 100, time.time() - 100))
    inv = M.Inventory()
    dlg = ReadDrawingsDialog(inv, [new, old], lambda m: None)
    assert [it["state"] for it in dlg.items] == ["new", "duplicate"]
    dlg.apply()
    assert len(inv.parts) == 1 and inv.parts[0]["weightLb"] == 2.0
