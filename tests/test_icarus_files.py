"""Icarus takes Excel / CSV and PDF attachments: read into the prompt, read further, and imported into DIMS."""
import json
import os

import pytest

from daedalus.ai import attachments as AT
from daedalus.inventory import model as M
from daedalus.inventory.store import InventoryStore


def _xlsx(path, rows, title="Sheet1"):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _pdf(path, lines):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    w = QPdfWriter(str(path))
    w.setPageSize(QPageSize(QPageSize.Letter))
    qp = QPainter(w)
    for i, t in enumerate(lines):
        qp.drawText(200, 400 + i * 300, t)
    qp.end()
    return path


def test_sheet_and_pdf_text(tmp_path):
    x = _xlsx(tmp_path / "v.xlsx", [["Vendor Name", "Phone [1]"], ["ACME LTD", "604-555-0100"], [None, None],
                                     ["BETA INC", 6045550101]])
    t = AT.sheet_text(x)
    assert "## Sheet 'Sheet1' — 3 rows" in t and "2| ACME LTD | 604-555-0100" in t and "6045550101" in t
    assert AT.read_rows(x)[1] == {"Vendor Name": "BETA INC", "Phone [1]": 6045550101}
    (tmp_path / "c.csv").write_text("Part Number;Count\nP00-0001;12\n")
    assert AT.read_rows(tmp_path / "c.csv") == [{"Part Number": "P00-0001", "Count": "12"}]
    p = _pdf(tmp_path / "q.pdf", ["QUOTE 4471", "SHCS M8x20 qty 200 $0.31"])
    d = AT.describe(p)
    assert d["kind"] == "pdf" and "QUOTE 4471" in d["text"] and d["data"][:4] == b"%PDF"
    assert AT.describe(x)["kind"] == "sheet" and AT.kind_of("a.PNG") == "image"
    with pytest.raises(ValueError):
        AT.describe(tmp_path / "x.docx")


def test_dims_import_file_preview_then_apply(tmp_path):
    from daedalus.ai.dims_tools import DimsToolExecutor
    st = InventoryStore(tmp_path / "inv.json")
    st.inv.parts = [M.new_part(id="a", name="Nut", partNumber="P00-0001", currentStock=5)]
    st.inv.vendors = [{"id": "v", "name": "Sabre", "contactName": "Dan"}]
    st.save()
    ex = DimsToolExecutor(lambda: st)
    ex.attachments["counts.xlsx"] = str(_xlsx(tmp_path / "counts.xlsx", [["Part Number", "Count"],
                                                                          ["P00-0001", 12], ["ZZZ", 1]]))
    ex.attachments["vendors.xlsx"] = str(_xlsx(tmp_path / "vendors.xlsx", [
        ["Vendor Name", "Contact [1]", "Ship From City"], ["SABRE INDUSTRIAL SUPPLIES", "ALLEN", "SURREY"],
        ["NEW CO", "AMY", "DELTA"]]))
    out, err = ex.run("dims_import_file", {"file": "counts.xlsx", "kind": "counts"})
    r = json.loads(out)
    assert not err and r["changed"] == [{"part": "Nut", "was": 5, "now": 12}] and r["not_found"] == ["ZZZ"]
    assert st.inv.part("a")["currentStock"] == 5                         # preview changed nothing
    out, err = ex.run("dims_import_file", {"file": "counts.xlsx", "kind": "counts", "apply": True})
    assert not err and st.inv.part("a")["currentStock"] == 12 and st.inv.transactions[-1]["mode"] == "receiving"
    out, err = ex.run("dims_import_file", {"file": "vendors.xlsx", "kind": "vendors", "apply": True})
    r = json.loads(out)
    assert r["added"] == 1 and r["updated"] == 1 and r["kept_existing"][0]["kept"] == "Dan"
    assert InventoryStore(tmp_path / "inv.json").inv.vendors[0]["city"] == "SURREY"   # saved
    out, err = ex.run("dims_read_file", {"file": "vendors.xlsx", "start": 1, "limit": 1})
    assert "SABRE" in out and "NEW CO" not in out
    out, err = ex.run("dims_read_file", {"file": "nope.xlsx"})
    assert err and "attached" in out


def test_panel_attaches_files_and_sends_them(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ai.dims_tools import DIMS_TOOLS, DimsToolExecutor
    from daedalus.ui import copilot_panel as CP
    st = InventoryStore(tmp_path / "inv.json")
    ex = DimsToolExecutor(lambda: st)
    panel = CP.CopilotPanel(None, executor=ex, tools=DIMS_TOOLS, system="x")
    x = _xlsx(tmp_path / "Potential Vendors.xlsx", [["Vendor Name", "Status"], ["ACME", "Active"]])
    pdf = _pdf(tmp_path / "quote.pdf", ["QUOTE 4471"])
    assert panel.attach_path(str(x)) and panel.attach_path(str(pdf))
    assert not panel.attach_path(str(tmp_path / "x.docx"))
    assert "Potential Vendors.xlsx" in panel.attach_lbl.text() and "quote.pdf" in panel.attach_lbl.text()
    sent = {}
    monkeypatch.setattr(panel, "_start", lambda prompt, images, show=True, documents=None: sent.update(
        prompt=prompt, images=images, documents=documents))
    monkeypatch.setattr(panel, "_ensure_copilot", lambda key=None: None)
    monkeypatch.setattr(panel, "kind", lambda: "claude")
    monkeypatch.setattr(panel, "api_key", lambda: "k")
    panel.input.setPlainText("add these vendors")
    panel.send()
    assert sent["prompt"].endswith("add these vendors") and "ACME | Active" in sent["prompt"]
    assert 'name="quote.pdf"' in sent["prompt"] and sent["documents"][0][:4] == b"%PDF"
    assert ex.attachments["Potential Vendors.xlsx"].endswith("Potential Vendors.xlsx")
    assert panel.attach_lbl.text() == ""
    monkeypatch.setattr(panel, "kind", lambda: "ollama")
    monkeypatch.setattr(panel.model, "currentText", lambda: "qwen")
    panel.attach_path(str(pdf))
    panel.input.setPlainText("what's the quote?")
    panel.send()
    assert "QUOTE 4471" in sent["prompt"] and not sent["documents"]      # local models get the text
