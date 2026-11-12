"""DIMS wishlist: trends, cost analysis, vendor price cross-reference, production history, binning list,
discontinued parts, MFG revision check, thumbnails, bug report, and Icarus inside DIMS."""
import datetime as dt
import json
import os
import time

import pytest

from daedalus.inventory import analytics as A
from daedalus.inventory import model as M
from daedalus.inventory.store import InventoryStore

TODAY = dt.date(2026, 11, 12)


def ts(d: dt.date):
    return int(time.mktime(d.timetuple()) * 1000) + 12 * 3600 * 1000


def sample(tmp_path=None):
    inv = M.Inventory()
    inv.exchangeRate = 1.4
    inv.vendors = [{"id": "v1", "name": "Fastenal", "leadTimeMinDays": 3, "leadTimeMaxDays": 7},
                   {"id": "v2", "name": "McMaster", "leadTimeMinDays": 2, "leadTimeMaxDays": 2}]
    loc = inv.find_or_create_location("Rack A", 2, "small", 3)
    bolt = M.new_part(id="bolt", name='3/8-16 UNC x 1" HHCS', currentStock=100, reorderPoint=40, unitCost=0.30,
                      locationId=loc, suppliers=[{"name": "Fastenal", "price": 0.30, "currency": "CAD"},
                                                 {"name": "McMaster", "price": 0.15, "currency": "USD"}, {}])
    nut = M.new_part(id="nut", name="3/8-16 Hex Nut", currentStock=10, reorderPoint=50, unitCost=0.1,
                     suppliers=[{"name": "Fastenal", "price": 0.1, "currency": "CAD"}, {}, {}])
    old = M.new_part(id="old", name="Old Caster", type="purchased", currentStock=2, reorderPoint=10,
                     discontinued=True, replacementId="nut")
    brk = M.new_part(id="brk", name="Bracket", type="manufactured", currentStock=5, unitCost=12)
    inv.parts = [bolt, nut, old, brk]
    pid = inv.find_or_create_project("Lift Cart", totalQtyNeeded=4)
    inv.set_allocation(bolt, pid, 8)
    inv.set_allocation(old, pid, 1)
    # usage growing month over month
    for i, q in enumerate([5, 10, 15, 20, 25, 30]):
        m = TODAY.month - 5 + i
        y = TODAY.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        inv.transactions.append({"id": f"t{i}", "partId": "bolt", "partName": bolt["name"], "mode": "usage",
                                 "qty": q, "ts": ts(dt.date(y, m, 10)), "by": "Dan"})
    inv.transactions.append({"id": "r1", "partId": "bolt", "partName": bolt["name"], "mode": "receiving", "qty": 50,
                             "ts": ts(dt.date(2026, 10, 3)), "by": "Dan"})
    inv.productionOrders = [
        {"id": "po1", "projectId": pid, "qty": 2, "status": "Complete", "orderDate": "2026-08-01",
         "dueDate": "2026-08-20", "fabStart": "2026-08-02", "fabEnd": "2026-08-10", "finishStart": "2026-08-11",
         "finishEnd": "2026-08-25"},
        {"id": "po2", "projectId": pid, "qty": 3, "status": "Complete", "orderDate": "2026-09-01",
         "dueDate": "2026-09-30", "finishEnd": "2026-09-20"},
        {"id": "po3", "projectId": pid, "qty": 1, "status": "In Fab", "orderDate": "2026-11-01"}]
    return inv


def test_discontinued_never_reordered_but_still_short():
    inv = sample()
    low = {p["id"] for p in inv.low_stock()}
    assert "nut" in low and "old" not in low
    rows = A.discontinued_rows(inv)
    assert rows[0]["Part"] == "Old Caster" and rows[0]["Replacement"] == "3/8-16 Hex Nut" and \
        rows[0]["Needed by Projects"] == 4


def test_trends_and_costs():
    inv = sample()
    ms, rows = A.trend_rows(inv, 6, end=TODAY)
    r = next(r for r in rows if r["id"] == "bolt")
    assert r["series"] == [5, 10, 15, 20, 25, 30] and r["Avg / Month"] == 17.5 and r["Trend %/mo"] > 25
    assert r["Months Left"] == round(100 / 17.5, 1)
    assert r["Suggested Reorder Pt"] == round(17.5 / 30.4 * 2 * 1.5)       # cheapest = McMaster, 2 days
    flow = A.usage_by_month(inv, ms)
    assert flow["received"][ms.index("2026-10")] == 50 and flow["used_cost"][-1] == pytest.approx(30 * 0.30)
    c = A.cost_summary(inv, 6, end=TODAY)
    assert c["stock_value"] == pytest.approx(100 * 0.3 + 10 * 0.1 + 5 * 12)
    proj = c["projects"][0]
    assert proj["Cost / Unit"] == pytest.approx(8 * 0.3) and proj["Units"] == 4


def test_vendor_xref():
    inv = sample()
    parts, vend = A.vendor_xref(inv, end=TODAY)
    p = parts[0]
    assert p["Cheapest"] == "McMaster" and p["Cheapest Price"] == pytest.approx(0.15 * 1.4)
    assert p["Used / 12 mo"] == 105 and p["Savings / yr"] == pytest.approx((0.30 - 0.21) * 105)
    fast = next(v for v in vend if v["Vendor"] == "Fastenal")
    assert fast["Cheapest On"] == 0 and fast["Avg Premium %"] > 40


def test_production_history_and_audit_dates():
    inv = sample()
    h = A.production_history(inv, "2026-01-01", "2026-12-31")
    assert [r["id"] for r in h["orders"]] == ["po1", "po2"]
    assert h["orders"][0]["Days Late"] == 5 and h["orders"][1]["Days Late"] == -10
    pr = h["projects"][0]
    assert pr["Units"] == 5 and pr["On Time %"] == 50
    assert {p["Part"]: p["Qty Used"] for p in h["parts"]}['3/8-16 UNC x 1" HHCS'] == 40
    h2 = A.production_history(inv, "2026-10-01", "2026-12-31", {"po2": "2026-10-02"})
    assert [r["id"] for r in h2["orders"]] == ["po2"]


def test_binning_list():
    inv = sample()
    rows = A.binning_rows(inv)
    assert rows[0]["Bin"] == "A2-03" and rows[0]["Part"].startswith("3/8-16 UNC")
    assert any(r["Location"] == "No bin assigned" and r["Part"] == "Bracket" for r in rows)
    html = A.binning_html(rows, "x", "y")
    assert "A2-03" in html and "Count" in html


def test_revision_check(tmp_path):
    inv = sample()
    p = inv.part("brk")
    d1 = tmp_path / "BRKT-104 Rev B.pdf"
    d1.write_text("b")
    p.update(drawingUrl=str(d1), revision="B")
    assert A.version_status(p)["status"] == "unchecked"
    A.accept_version(p)
    assert A.version_status(p)["status"] == "ok" and p["revision"] == "B"
    os.utime(d1, (time.time() + 60, time.time() + 60))
    assert A.version_status(p)["status"] == "changed"
    (tmp_path / "BRKT-104 Rev C.pdf").write_text("c")
    st = A.version_status(p)
    assert st["status"] == "newer" and "Rev C" in st["detail"]
    A.accept_version(p, newest=True)
    assert p["revision"] == "C" and p["drawingUrl"].endswith("Rev C.pdf") and A.version_status(p)["status"] == "ok"
    p["revision"] = "D"
    assert A.version_status(p)["status"] == "mismatch"
    assert A.rev_from_name("frame_rev12.SLDPRT") == "12" and A.rev_key("AA") > A.rev_key("Z")


def test_thumbnails(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.inventory import thumbs as T
    monkeypatch.setattr(T, "cache_dir", lambda: tmp_path / "c")
    import numpy as np
    # a unit cube as a binary STL
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6],
         [3, 0, 4], [3, 4, 7]]
    import struct
    data = b"\0" * 80 + struct.pack("<I", len(f)) + b"".join(
        struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v[i]) for i in t) + b"\0\0" for t in f)
    stl = tmp_path / "cube.stl"
    stl.write_bytes(data)
    vv, ff = T.read_stl(stl)
    assert len(vv) == 8 and len(T.feature_edges(vv, ff)) == 12        # 12 cube edges, no face diagonals
    p = {"cadFile": str(stl)}
    out = T.make(T.source_for(p))
    assert out and out.exists() and T.cached(p) == out
    sw = tmp_path / "x.SLDPRT"
    png = out.read_bytes()
    sw.write_bytes(b"junk" * 100 + png + b"more")
    assert T.sw_preview(sw) == png


def test_dims_tools(tmp_path):
    from daedalus.ai.dims_tools import DIMS_TOOLS, DimsToolExecutor
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = sample()
    st.save()
    commits = []
    ex = DimsToolExecutor(lambda: st, lambda msg, undo=None: (commits.append((msg, undo)), st.save()))

    def run(tool, **a):
        out, err = ex.run(tool, a)
        assert not err, out
        return json.loads(out)
    assert len(DIMS_TOOLS) >= 20
    assert run("dims_summary")["low_stock"] == 1
    assert run("dims_search_parts", query="3/8 nut")["parts"][0]["id"] == "nut"
    assert run("dims_search_parts", vendor="mcmaster")["count"] == 1
    assert run("dims_search_parts", discontinued=True)["parts"][0]["id"] == "old"
    gp = run("dims_get_part", part="bolt")
    assert gp["trend"]["Avg / Month"] > 0 and gp["allocations"][0]["needed"] == 32
    assert run("dims_reorder_list")["rows"][0]["Part"] == "3/8-16 Hex Nut"
    assert run("dims_vendor_prices")["possible_savings_per_year"] > 0
    assert run("dims_production_history", start="2026-01-01")["projects"][0]["Units"] == 5
    assert run("dims_project", project="lift")["costPerUnitCAD"] == pytest.approx(2.4)
    r = run("dims_record", part="3/8-16 Hex Nut", mode="receiving", qty=40, note="PO 1")
    assert r["stock"] == 50 and commits[-1][1] is not None
    commits[-1][1]()                                                     # undo
    assert st.inv.part("nut")["currentStock"] == 10
    assert run("dims_count", part="nut", counted=12)["adjustment"] == 2
    out, err = ex.run("dims_record", {"part": "3/8", "mode": "usage", "qty": 1})
    assert err and "matches" in out                                      # ambiguous → asks
    a = run("dims_add_part", type="fastener", size="M8", threadStandard="Metric Coarse", headStyle="Socket Head",
            category="Screw", length="20", location="A2-03", unitCost=0.2)
    assert a["part"]["name"] == "M8x1.25 x 20mm SHCS" and a["part"]["location"].startswith("Rack A")
    assert not run("dims_add_part", name="M8x1.25 x 20mm SHCS")["ok"]   # duplicate guarded
    run("dims_update_part", part="nut", fields={"reorderPoint": 5, "suppliers": [{"name": "Grainger", "price": 0.08}]})
    assert st.inv.part("nut")["reorderPoint"] == 5 and any(v["name"] == "Grainger" for v in st.inv.vendors)
    d = run("dims_discontinue", part="nut", replacement="bolt", note="switching")
    assert d["discontinued"] and d["replacement"].startswith("3/8-16 UNC")
    o = run("dims_place_order", part="bolt", qty=100)["order"]
    assert o["vendorName"] == "McMaster" and o["expectedDate"]
    assert run("dims_receive_order", part="bolt")["stock"] == 200
    run("dims_project_edit", project="New Job", units=3, allocate=[{"part": "bolt", "qty": 2}])
    assert run("dims_project", project="New Job")["units"] == 3
    po = run("dims_production_order", project="New Job", qty=3, status="In Fab", dueDate="2026-12-01")["order"]
    run("dims_production_order", id=po["id"], status="Complete")
    assert any(x["status"] == "Complete" and x["id"] == po["id"] for x in st.inv.productionOrders)
    exp = run("dims_export", report="vendor_prices", path=str(tmp_path / "v.xlsx"))
    assert (tmp_path / "v.xlsx").exists() and exp["ok"]
    run("dims_export", report="backup", path=str(tmp_path / "b.xlsx"))
    assert len(run("dims_binning_list")) >= 1 and run("dims_activity", by="dan")["count"] >= 7
    # reloaded from disk: everything was saved
    st2 = InventoryStore(tmp_path / "inv.json")
    assert st2.inv.part("nut")["discontinued"]


def test_main_icarus_has_dims_tools(tmp_path):
    from daedalus.ai.tools import TOOLS, ToolExecutor
    from daedalus.core.document import Document
    assert "dims_trends" in {t["name"] for t in TOOLS}
    ex = ToolExecutor(Document())
    ex.inventory_store = InventoryStore(tmp_path / "inv.json")
    ex.inventory_store.inv = sample()
    out, err = ex.run("dims_summary", {})
    assert not err and json.loads(out)["parts"] == 4
    out, err = ex.run("dims_record", {"part": "nope", "mode": "usage", "qty": 1})
    assert err


def test_lite_window_does_not_load_the_kernel():
    import subprocess
    import sys
    code = ("import sys; import daedalus.ui.copilot_panel, daedalus.ai.dims_tools, daedalus.ui.inventory_extras_ui;"
            "print('daedalus.core.document' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, cwd=os.path.dirname(os.path.dirname(
                             os.path.abspath(__file__))))
    assert out.stdout.strip() == "False", out.stderr


def test_ui(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    from daedalus.ui import inventory_dialogs as D
    from daedalus.ui import inventory_extras_ui as X
    from daedalus.ui.bug_report import BugReportDialog
    from daedalus.ui.inventory_ui import InventoryPage
    monkeypatch.setattr("daedalus.ui.bug_report.report_dir", lambda: tmp_path)
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = sample()
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1400, 900)
    page.show()
    for key in ("parts", "analytics", "locations", "shortages", "dashboard"):
        page.goto(key)
        app.processEvents()
    page.goto("analytics")
    view = page.pages["analytics"].widget()
    for i in range(view.tabs.count()):
        view.tabs.setCurrentIndex(i)
        app.processEvents()
    page.parts_filter.update(sort="vendor", vendor="Fastenal", discontinued=True)
    page.goto("parts")
    app.processEvents()
    for dlg in (X.BinningDialog(st.inv, page), X.VersionCheckDialog(st.inv, lambda m: None, page),
                D.PartDialog(st.inv, st.inv.part("old"), page), D.PartDialog(st.inv, st.inv.part("brk"), page)):
        dlg.show()
        app.processEvents()
        dlg.close()
    d = D.PartDialog(st.inv, st.inv.part("brk"), page)
    d.rev.setText("c")
    d.disc_box.setChecked(True)
    vals = d.values()
    assert vals["revision"] == "C" and vals["discontinued"] and vals["discontinuedDate"]
    b = X.BinningDialog(st.inv, page)
    b.pdf_path = tmp_path / "bins.pdf"
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getSaveFileName", lambda *a, **k: (str(tmp_path / "bins.pdf"), ""))
    b.pdf()
    assert (tmp_path / "bins.pdf").stat().st_size > 1000
    r = BugReportDialog(page, context={"Area": "test"}, inv=st.inv, commit=lambda m: None)
    r.title.setText("Stock wrong")
    r.what.setPlainText("It went negative")
    r.contact.setText("shop@example.com")
    r.save()
    assert "Stock wrong" in next(tmp_path.glob("report-*.txt")).read_text() and st.inv.syncMeta["bugReportEmail"]
    assert next(tmp_path.glob("screenshot-*.png")).stat().st_size > 0
    page.toggle_icarus(True)
    app.processEvents()
    assert page.icarus.isVisible() and page.icarus_btn.isChecked()
    out, err = page.icarus.executor.run("dims_record", {"part": "bolt", "mode": "usage", "qty": 3})
    assert not err and st.inv.part("bolt")["currentStock"] == 97
    page.toggle_icarus(False)
    assert not page.icarus.isVisible()
    page.close()
