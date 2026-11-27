"""Version control and release beside the file: check in, compare, restore, review, release (letters, package,
revision rows, obsolete), and the read-only snapshots."""
import json

import pytest

from daedalus.ai.tools import ToolExecutor
from daedalus.core import vault as V
from daedalus.core.document import Document

RECT = {"plane": "XY", "entities": [{"type": "rect", "x": 0, "y": 0, "w": 40, "h": 20}]}


def _part(tmp_path):
    doc = Document()
    ex = ToolExecutor(doc)
    assert not ex.run("add_sketch", RECT)[1]
    assert not ex.run("add_extrude", {"sketch": "Sketch1", "depth": 5})[1]
    doc.save(tmp_path / "Bracket.daedalus")
    return doc, ex


def test_check_in_compare_restore(tmp_path):
    doc, ex = _part(tmp_path)
    assert V.status(doc) == {"tracked": False}
    e1 = V.check_in(doc, "First cut", who="AN")
    assert e1["v"] == 1 and e1["state"] == "WIP" and e1["features"] == 2 and e1["mass_kg"] > 0
    assert (tmp_path / "Bracket.history" / "v0001.json.gz").exists()
    with pytest.raises(V.VaultError, match="No changes"):
        V.check_in(doc, "again")
    assert not V.changed_since(doc)
    ex.run("edit_feature", {"name": "Extrude1", "params": {"depth": 8}})
    assert V.changed_since(doc)
    live = V.compare(doc.path, 1, doc=doc)
    assert live["changed"][0]["feature"] == "Extrude1" and "depth: 5 → 8" in live["changed"][0]["changes"][0]
    ex.run("add_fillet", {"edges": "all", "radius": 1})
    e2 = V.check_in(doc, "Thicker, rounded", who="AN")
    d = V.compare(doc.path, 1, 2)
    txt = V.describe(d)
    assert "+ Fillet1" in txt and "~ Extrude1: depth: 5 → 8" in txt and "mass" in txt and d["mass_kg"][1] > d["mass_kg"][0]
    assert V.describe(V.compare(doc.path, 2, 2)) == "No differences"
    V.restore(doc, 1)
    assert [f.name for f in doc.features] == ["Sketch1", "Extrude1"] and doc.features[1].params["depth"] == 5
    doc.undo()
    assert len(doc.features) == 3
    ro = V.open_version(doc.path, 1)
    assert ro.path is None and ro.read_only_version[1] == 1 and len(ro.features) == 2
    with pytest.raises(V.VaultError, match="message"):
        V.check_in(doc, "  ")
    assert [e["v"] for e in V.versions(doc.path)] == [1, 2] and e2["author"] == "AN"


def test_review_release_package(tmp_path):
    doc, ex = _part(tmp_path)
    assert not ex.run("new_drawing", {"name": "Bracket"})[1]
    V.check_in(doc, "Ready", who="AN")
    V.set_require_approval(doc.path, True)
    with pytest.raises(V.VaultError, match="approval"):
        V.release(doc.path, 1, who="AN")
    V.submit(doc.path, 1, who="AN")
    V.review(doc.path, 1, False, who="JL", note="add a chamfer")
    assert V.versions(doc.path)[0]["state"] == "WIP"
    V.submit(doc.path, 1, who="AN")
    V.review(doc.path, 1, True, who="JL")
    e = V.release(doc.path, 1, who="JL", note="First release", doc=doc)
    assert e["state"] == "Released" and e["rev"] == "A" and not e.get("package_error"), e.get("package_error")
    files = [f.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for f in e["package"]]
    assert "Bracket Rev A.step" in files and "Bracket Rev A Bracket.pdf" in files and \
        "Bracket Rev A release note.txt" in files
    assert (tmp_path / "Bracket Rev A" / "Bracket Rev A.step").stat().st_size > 1000
    assert doc.drawings[0].sheets[0].revisions[-1]["rev"] == "A" and not V.changed_since(doc)
    with pytest.raises(V.VaultError, match="already Released"):
        V.release(doc.path, 1)
    ex.run("edit_feature", {"name": "Extrude1", "params": {"depth": 6}})
    V.check_in(doc, "6 mm")
    V.submit(doc.path, 2)
    V.review(doc.path, 2, True, who="JL")
    e2 = V.release(doc.path, 2, who="JL", doc=doc)
    vs = V.versions(doc.path)
    assert e2["rev"] == "B" and vs[0]["state"] == "Obsolete" and vs[1]["state"] == "Released"
    assert [h["what"] for h in vs[0]["history"]][-1] == "Superseded by Rev B"
    st = V.status(doc)
    assert st["released"] == "B" and st["latest"] == 2 and not st["modified"]
    json.loads((tmp_path / "Bracket.history" / "index.json").read_text())


def test_letters_skip_asme(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "versions", lambda p: [{"rev": "H"}])
    assert V.next_letter("x") == "J"
    monkeypatch.setattr(V, "versions", lambda p: [{"rev": "Y"}])
    assert V.next_letter("x") == "YA"
    with pytest.raises(V.VaultError, match="Save the part"):
        V.check_in(Document(), "x")


def test_versions_dialog(tmp_path, monkeypatch):
    import os
    import types
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.ui import vault_ui
    doc, ex = _part(tmp_path)
    host = QWidget()
    win = types.SimpleNamespace(doc=doc, save_doc=lambda: True, _update_title=lambda: None)
    monkeypatch.setattr(vault_ui.CheckInDialog, "exec", lambda self: (self.msg.setText("First"), 1)[1])
    d = vault_ui.VersionsDialog(win, parent=host)
    assert "Not released" in d.head.text() and d.table.rowCount() == 0
    d.check_in()
    assert d.table.rowCount() == 1 and d.table.item(0, 5).text() == "First"
    ex.run("edit_feature", {"name": "Extrude1", "params": {"depth": 9}})
    d.check_in()
    d.table.selectRow(1)
    d.compare()
    assert "depth: 5 → 9" in d.detail.toPlainText()
    d.table.selectRow(0)
    monkeypatch.setattr(vault_ui.QInputDialog, "getText", lambda *a, **k: ("first release", True))
    monkeypatch.setattr(vault_ui.QMessageBox, "information", lambda *a, **k: None)
    d.release()
    assert d.table.item(0, 1).text() == "A" and "Rev A" in d.head.text()
    d.show_detail()
    assert "Released as Rev A" in d.detail.toPlainText() and "Bracket Rev A.step" in d.detail.toPlainText()
