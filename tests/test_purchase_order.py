"""Create PO from the buy list: generic company settings (logo, address, ship-to, footer, numbering), PDF layout,
template import from a PO PDF, vendor box, file name, parts logged as ordered."""
import base64
import io
import os

from daedalus.inventory import model as M
from daedalus.inventory import purchase_order as P


def sample():
    inv = M.Inventory()
    inv.vendors = [{"id": "v1", "name": "Acme Fab", "contactName": "Pat Lee", "address": "12 Main St",
                    "city": "Springfield", "province": "OR", "postalCode": "97477", "country": "USA",
                    "leadTimeMinDays": 5, "leadTimeMaxDays": 10}]
    a = M.new_part(id="a", name="Cross Clamp Tubes", partNumber="M10-0131", type="purchased",
                   suppliers=[{"name": "Acme Fab", "price": 12.5, "currency": "CAD", "partNumber": "AF-77"}])
    b = M.new_part(id="b", name="300x100 Vacuum Cup", type="purchased",
                   suppliers=[{"name": "Acme Fab", "price": 4, "currency": "CAD"}])
    inv.parts = [a, b]
    return inv


def _logo():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 80), (30, 60, 120)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def test_settings_numbering_and_blocks():
    inv = sample()
    st = P.settings(inv)
    assert st["companyName"] == "" and P.next_number(st) == "PO-1001"     # generic out of the box
    P.save_settings(inv, {**st, "companyName": "Widget Co", "address": "1 Way\nTown ST 00000", "phone": "555-0100",
                          "website": "https://widget.example", "poPrefix": "WC-", "nextPo": 7})
    st = P.settings(inv)
    assert P.company_lines(st) == ["Widget Co", "1 Way", "Town ST 00000", "555-0100", "https://widget.example"]
    assert P.ship_to(st) == "Widget Co\n1 Way\nTown ST 00000" and P.next_number(st) == "WC-0007"
    P.used_number(inv, "WC-0007")
    assert P.next_number(P.settings(inv)) == "WC-0008"
    P.used_number(inv, "SPECIAL-1")                                         # other numbers leave the counter
    assert P.settings(inv)["nextPo"] == 8
    assert P.vendor_block(inv.vendors[0]) == "Acme Fab\nPat Lee\n12 Main St\nSpringfield, OR 97477\nUSA"
    inv.vendors[0]["poBlock"] = "Acme Fabrication Ltd\nBox 9"
    assert P.vendor_block(inv.vendors[0]) == "Acme Fabrication Ltd\nBox 9"
    assert P.file_name(st, "3 Amigos Manufacturing", "LL114513AA", "Vacuum System", "2026-11-17") == \
        "261117_3_Amigos_Manufacturing_Widget_PO_LL114513AA_Vacuum_System.pdf"


def test_pdf_and_template_roundtrip(tmp_path):
    inv = sample()
    st = {**P.DEFAULTS, "companyName": "Widget Co", "address": "1 Way\nTown ST 00000", "phone": "555-0100",
          "website": "https://widget.example", "footer": "Tax # 123\nThanks.", "logo": _logo(), "poTag": "WCPO"}
    lines = P.lines_from_parts(inv, [(inv.part("a"), 3), (inv.part("b"), 2)], "Acme Fab")
    assert lines[0]["mpn"] == "AF-77" and lines[0]["description"] == "M10-0131\nCross Clamp Tubes"
    assert P.total(lines) == 45.5
    po = {"number": "WC-0001", "date": "2026-11-17", "vendor": P.vendor_block(inv.vendors[0]),
          "lines": lines, "currency": "CAD"}
    out = tmp_path / "261117_Acme_Fab_Widget_WCPO_WC-0001_Job.pdf"
    P.write_pdf(out, st, po)
    from pypdf import PdfReader
    text = PdfReader(str(out)).pages[0].extract_text()
    for s in ("Purchase Order", "WC-0001", "11/17/26", "Widget Co", "Acme Fab", "Springfield, OR 97477",
              "Cross Clamp Tubes", "AF-77", "37.50", "CAD 45.50", "Tax # 123", "Ship To"):
        assert s in text, s
    t = P.read_template(out)                                       # our own PO reads back as a template
    assert t["companyName"] == "Widget Co" and t["phone"] == "555-0100" and t["website"] == "https://widget.example"
    assert t["address"] == "1 Way\nTown ST 00000" and t["footer"] == "Tax # 123\nThanks."
    assert t["shipTo"].startswith("Widget Co") and t["logo"] and t["poNumber"] == "WC-0001"
    assert t["vendor"][0] == "Acme Fab" and t["fileTag"] == "Widget" and t["poTag"] == "WCPO"
    # many lines → pages
    many = lines * 20
    P.write_pdf(tmp_path / "long.pdf", st, {**po, "lines": many})
    r = PdfReader(str(tmp_path / "long.pdf"))
    assert len(r.pages) > 1 and "CAD 910.00" in r.pages[-1].extract_text()


def test_log_orders():
    inv = sample()
    po = {"number": "PO-1001", "date": "2026-11-17",
          "lines": P.lines_from_parts(inv, [(inv.part("a"), 3)], "Acme Fab") +
          [{"partId": None, "mpn": "", "description": "Drill Hole", "qty": 2, "rate": 0}]}
    out = P.log_orders(inv, po, "Acme Fab", None)
    assert len(out) == 1 and out[0]["poNumber"] == "PO-1001" and out[0]["qty"] == 3
    assert out[0]["status"] == "Ordered" and out[0]["expectedDate"] == "2026-11-27"


def test_po_dialog_from_buy_list(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import po_ui as PU
    from daedalus.ui.inventory_ui import InventoryPage
    from daedalus.ui.project_bom import BuyListWidget
    inv = sample()
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    w = BuyListWidget(page, page.inv.parts, lambda p: 3)
    items, first = w.po_items()
    assert set(items) == {"Acme Fab"} and len(items["Acme Fab"]) == 2
    d = PU.PurchaseOrderDialog(page, items, "Acme Fab", job="Vacuum System")
    assert d.number.text() == "PO-1001" and "Springfield, OR 97477" in d.vblock.toPlainText()
    assert d.table.rowCount() == 2 and "Your company isn't set" in d.who.text()
    # PO settings: generic company, logo from a file
    sd = PU.POSettingsDialog(page.inv, d)
    sd.name.setText("Widget Co")
    sd.address.setPlainText("1 Way")
    from PIL import Image
    Image.new("RGB", (200, 60), (200, 0, 0)).save(tmp_path / "logo.png")
    sd.load_logo_file(str(tmp_path / "logo.png"))
    assert sd.value()["logo"] and sd.value()["companyName"] == "Widget Co"
    P.save_settings(page.inv, sd.value())
    d.load_settings()
    assert "Widget Co" in d.who.text() and d.ship.toPlainText() == "Widget Co\n1 Way"
    d._row({"partId": None, "mpn": "", "description": "Drill Hole", "qty": 2, "rate": 5})
    assert d.total.text().endswith("CAD 59.50")                # 3×12.5 + 3×4 + 2×5
    out = tmp_path / "po.pdf"
    d.save(str(out))
    assert out.exists() and d.result() == PU.QDialog.Accepted
    o = [x for x in page.inv.partOrders if x["poNumber"] == "PO-1001"]
    assert len(o) == 2 and P.settings(page.inv)["nextPo"] == 1002
    assert P.file_name(P.settings(page.inv), "Acme Fab", "PO-1001", "Vacuum System", "2026-11-17") == \
        "261117_Acme_Fab_Widget_PO_PO-1001_Vacuum_System.pdf"


def test_dashboard_company_profile_and_po_from_scratch(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import po_ui as PU
    from daedalus.ui.inventory_ui import InventoryPage
    inv = sample()
    inv.vendors.append({"id": "v2", "name": "Bolt Bros"})
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.goto("dashboard")
    app.processEvents()
    w = page.pages["dashboard"].widget()
    labels = " ".join(l.text() for l in w.findChildren(QLabel))
    assert "COMPANY PROFILE" in labels and "Set your company name" in labels
    btns = {b.text() for b in w.findChildren(QPushButton)}
    assert {"Edit company profile…", "Create PO…"} <= btns
    P.save_settings(page.inv, {**P.settings(page.inv), "companyName": "Widget Co", "address": "1 Way"})
    page.commit()
    app.processEvents()
    labels = " ".join(l.text() for l in page.pages["dashboard"].widget().findChildren(QLabel))
    assert "Widget Co" in labels and "next PO-1001" in labels
    # PO from scratch: every vendor to pick, parts added by hand (that vendor's first)
    d = PU.PurchaseOrderDialog(page, {}, None)
    names = [d.vendor.itemData(i) for i in range(d.vendor.count())]
    assert names == ["Acme Fab", "Bolt Bros"] and d.table.rowCount() == 0
    assert [c[1] for c in d.part_choices()][:2] == ["b", "a"] or {c[1] for c in d.part_choices()[:2]} == {"a", "b"}
    d.add_part("a", 4)
    assert d.table.rowCount() == 1 and d.table.item(0, 0).text() == "AF-77" and d.total.text().endswith("CAD 50.00")
    d.vendor.setCurrentIndex(1)                               # another vendor → its (empty) lines
    assert d.table.rowCount() == 0 and d.vblock.toPlainText() == "Bolt Bros"
