"""Team file paths stored as ~/… (open from each person's own user folder) and part revision tracking."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from daedalus.inventory import model as M  # noqa: E402


def test_paths_in_a_user_folder_are_stored_as_home_relative(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert M.portable_path("C:/Users/danie/Rangate Dropbox/Ops/C20-0040.pdf") == "~/Rangate Dropbox/Ops/C20-0040.pdf"
    assert M.portable_path(r"C:\Users\danie\Rangate Dropbox\a.SLDDRW") == "~/Rangate Dropbox/a.SLDDRW"
    assert M.portable_path(str(tmp_path / "Dropbox" / "x.pdf")) == "~/Dropbox/x.pdf"
    for keep in ("https://example.com/a.pdf", r"\\server\share\a.pdf", "D:/Eng/a.pdf", ""):
        assert M.portable_path(keep) == keep


def test_stored_paths_open_from_this_users_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    f = tmp_path / "Rangate Dropbox" / "Ops" / "C20-0040.pdf"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert Path(M.local_path("~/Rangate Dropbox/Ops/C20-0040.pdf")) == f
    assert Path(M.local_path("C:/Users/danie/Rangate Dropbox/Ops/C20-0040.pdf")) == f     # older, someone else's
    assert M.local_path("https://x/y") == "https://x/y"
    from daedalus.inventory import analytics as A
    p = M.new_part(name="Bracket", type="manufactured", drawingUrl="C:/Users/danie/Rangate Dropbox/Ops/C20-0040.pdf")
    assert A._local(p["drawingUrl"]) == f


def test_next_revision():
    assert [M.next_revision(r) for r in ("", "-", "A", "C", "Z", "AZ", "1", "09")] == \
        ["A", "A", "B", "D", "AA", "BA", "2", "10"]


def test_part_dialog_tracks_revisions_for_any_part_and_saves_portable_paths(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QInputDialog
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from daedalus.ui.inventory_dialogs import PartDialog
    inv = M.Inventory()
    inv.userName = "Ankur"
    p = M.new_part(name="Caster", type="purchased", revision="A",
                   drawingUrl="C:/Users/danie/Rangate Dropbox/caster.pdf",
                   revisionHistory=[{"rev": "A", "description": "First", "date": "2026-01-01", "by": "DS"}])
    inv.parts = [p]
    d = PartDialog(inv, p)
    assert not d.rev_row.isHidden() and d.drawing.text() == str(tmp_path / "Rangate Dropbox" / "caster.pdf")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("New bracket hole", True)))
    d._new_rev()
    v = d.values()
    assert v["revision"] == "B" and v["drawingUrl"] == "~/Rangate Dropbox/caster.pdf"
    assert [h["rev"] for h in v["revisionHistory"]] == ["A", "B"] and v["revisionHistory"][1]["by"] == "Ankur"
    assert d.rev_hist_btn.text() == "History (2)"
