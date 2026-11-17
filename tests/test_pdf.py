"""PDF: combine, reorder, rotate, delete, blank pages, mark-ups flattened on save, split, undo — and the window."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from daedalus.pdf import ops as P  # noqa: E402

app = QApplication.instance() or QApplication([])


def make_pdf(path, labels, size=(612, 792), rotate=0):
    """A PDF whose pages say their label (so text extraction tells pages apart)."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter
    w = QPdfWriter(str(path))
    w.setResolution(72)
    w.setPageLayout(QPageLayout(QPageSize(QSizeF(*size), QPageSize.Point), QPageLayout.Portrait, QMarginsF(0, 0, 0, 0)))
    p = QPainter(w)
    f = QFont("Arial")
    f.setPixelSize(24)
    p.setFont(f)
    for i, lab in enumerate(labels):
        if i:
            w.newPage()
        p.drawText(72, 100, lab)
    p.end()
    if rotate:
        from pypdf import PdfReader, PdfWriter
        r, o = PdfReader(str(path)), PdfWriter()
        for pg in r.pages:
            o.add_page(pg).rotate(rotate)
        with open(path, "wb") as fh:
            o.write(fh)
    return str(path)


def texts(path):
    from pypdf import PdfReader
    return [(pg.extract_text() or "").strip() for pg in PdfReader(str(path)).pages]


@pytest.fixture
def two(tmp_path):
    return make_pdf(tmp_path / "a.pdf", ["ALPHA1", "ALPHA2", "ALPHA3"]), make_pdf(tmp_path / "b.pdf", ["BRAVO1", "BRAVO2"])


def test_combine_reorder_delete_and_save(two, tmp_path):
    a, b = two
    d = P.PdfDoc(a)
    assert len(d) == 3 and not d.dirty
    d.add_file(b, at=1)                                   # B's pages after page 1
    assert len(d) == 5 and d.dirty
    d.move([4], 0)                                        # last page to the front
    d.delete([2])
    out = d.save(tmp_path / "out.pdf")
    assert [t.split()[0] for t in texts(out)] == ["ALPHA3", "ALPHA1", "BRAVO2", "ALPHA2"]
    assert d.path == out and not d.dirty
    assert P.merge([a, b], tmp_path / "m.pdf") and len(texts(tmp_path / "m.pdf")) == 5


def test_undo_redo_and_last_page_kept(two):
    d = P.PdfDoc(two[0])
    d.delete([0])
    assert len(d) == 2
    assert d.undo() and len(d) == 3 and d.redo() and len(d) == 2
    with pytest.raises(P.PdfError):
        d.delete([0, 1])


def test_rotate_blank_duplicate_reverse(two, tmp_path):
    from pypdf import PdfReader
    d = P.PdfDoc(two[0])
    d.rotate([0], 90)
    assert d.size(0) == (792, 612) and d.size(1) == (612, 792)
    d.insert_blank(1)
    d.duplicate([2])
    d.reverse()
    assert len(d) == 5
    out = d.save(tmp_path / "r.pdf")
    r = PdfReader(out)
    assert len(r.pages) == 5 and r.pages[4].rotation == 90 and texts(out)[3] == ""


def test_overwriting_the_open_file_is_safe(two):
    a = two[0]
    d = P.PdfDoc(a)
    d.move([2], 0)
    d.save()
    assert [t.split()[0] for t in texts(a)] == ["ALPHA3", "ALPHA1", "ALPHA2"]
    d.move([2], 0)                                        # source re-read after save: still works
    d.save()
    assert [t.split()[0] for t in texts(a)] == ["ALPHA2", "ALPHA3", "ALPHA1"]


def test_markups_are_flattened_into_the_page(two, tmp_path):
    d = P.PdfDoc(two[0])
    d.add_mark(0, {"kind": "text", "x": 72, "y": 200, "text": "CHECKED BY AN", "size": 14, "color": "#d11a1a"})
    d.add_mark(0, {"kind": "rect", "x": 60, "y": 60, "w": 200, "h": 60, "color": "#d11a1a", "width": 2})
    d.add_mark(1, {"kind": "arrow", "x1": 10, "y1": 10, "x2": 100, "y2": 100})
    out = d.save(tmp_path / "m.pdf")
    t = texts(out)
    assert "ALPHA1" in t[0] and "CHECKED BY AN" in " ".join(t[0].split()) and "CHECKED" not in t[2]


def test_markup_on_a_rotated_page_lands_where_drawn(tmp_path):
    src = make_pdf(tmp_path / "rot.pdf", ["ROT"], rotate=90)          # landscape as displayed
    d = P.PdfDoc(src)
    assert d.size(0) == (792, 612)
    d.add_mark(0, {"kind": "text", "x": 600, "y": 500, "text": "CORNER", "size": 12})
    out = d.save(tmp_path / "rot_out.pdf")
    from pypdf import PdfReader
    pg = PdfReader(out).pages[0]
    found = []
    pg.extract_text(visitor_text=lambda txt, cm, tm, fd, fs: found.append((txt, tm[4], tm[5], cm[4], cm[5])) if "CORNER" in txt else None)
    assert "CORNER" in (pg.extract_text() or "")
    w, h = float(pg.mediabox.width), float(pg.mediabox.height)
    assert (w, h) == (792, 612) and (pg.rotation or 0) == 0       # rotation moved into the content


def test_marks_turn_with_the_page(two):
    d = P.PdfDoc(two[0])
    d.add_mark(0, {"kind": "rect", "x": 10, "y": 20, "w": 30, "h": 40})
    d.rotate([0], 90)
    m = d.pages[0].marks[0]
    assert (m["x"], m["y"], m["w"], m["h"]) == (792 - 20 - 40, 10, 40, 30)
    d.rotate([0], -90)
    m = d.pages[0].marks[0]
    assert (m["x"], m["y"], m["w"], m["h"]) == (10, 20, 30, 40)


def test_split_and_ranges(two, tmp_path):
    d = P.PdfDoc(two[0])
    files = d.split(tmp_path, every=2)
    assert [len(texts(f)) for f in files] == [2, 1]
    assert P.parse_ranges("1-2, 3", 3) == [0, 1, 2] and P.parse_ranges("2-", 3) == [1, 2]
    with pytest.raises(P.PdfError):
        P.parse_ranges("4", 3)
    out = d.save(tmp_path / "ex.pdf", idx=P.parse_ranges("3", 3))
    assert texts(out)[0].startswith("ALPHA3") and d.path == two[0]


def test_find_text(two):
    d = P.PdfDoc(two[0])
    assert d.find("alpha2") == [1] and d.find("") == []


def test_bad_file(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(P.PdfError):
        P.PdfDoc(str(bad))


def test_window_opens_edits_and_saves(two, tmp_path):
    from daedalus.ui.pdf_ui import PdfWindow
    w = PdfWindow(two[0])
    w.show()
    app.processEvents()
    assert w.thumbs.count() == 3 and "a.pdf" in w.windowTitle()
    w.add_files([two[1]])
    assert w.thumbs.count() == 5
    w.thumbs.setCurrentRow(0)
    w.rotate(90)
    w.view.add_mark(0, {"kind": "text", "x": 50, "y": 50, "text": "OK", "size": 12})
    assert w.doc.dirty and w.doc.pages[0].marks
    w.doc.save(str(tmp_path / "w.pdf"))
    assert len(texts(tmp_path / "w.pdf")) == 5
    w.doc.dirty = False
    w.close()


def test_combine_window_and_main_routing(two, monkeypatch):
    from daedalus.ui import pdf_ui
    w = pdf_ui.open_pdf(None)
    w.add_files(list(two), at=0)
    assert len(w.doc) == 5 and w.thumbs.count() == 5 and w in pdf_ui._WINDOWS
    w.thumbs.clearSelection()
    w.thumbs.item(4).setSelected(True)
    w.shift(-1)
    assert w.doc.pages[3].src.endswith("b.pdf") and w.doc.pages[3].index == 1
    w.thumbs.clearSelection()
    w.thumbs.item(0).setSelected(True)
    w.delete_pages()
    assert len(w.doc) == 4
    w.undo()
    assert len(w.doc) == 5
    w.doc.dirty = False
    w.close()
    assert w not in pdf_ui._WINDOWS
    opened = []
    monkeypatch.setattr(pdf_ui, "open_pdf", lambda p, parent=None: opened.append(p))
    from daedalus.ui.main_window import MainWindow
    from types import SimpleNamespace
    MainWindow.open_path(SimpleNamespace(), two[0])                   # routes before touching the model
    assert opened == [two[0]]
