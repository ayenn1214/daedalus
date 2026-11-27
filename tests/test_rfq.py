"""Requests for quote: grouping by vendor, the email, the .eml draft with its attachment, logging and entering the
quotes that come back; the dims_rfq tool and the dialog."""
import email
import email.policy
import json
import os
import types

from daedalus.inventory import model as M
from daedalus.inventory import rfq as R


def inv_with_parts():
    inv = M.Inventory()
    inv.vendors = [{"id": "v1", "name": "Fastenal", "email": "chris@fastenal.example", "contactName": "Chris Lee"},
                   {"id": "v2", "name": "McMaster-Carr", "email": "", "contactName": ""}]
    inv.parts = [
        M.new_part(id="p1", name="M8x25 SHCS", type="fastener", size="M8", length="25", currentStock=10,
                   reorderPoint=50, reorderQty=200, partNumber="F-0825",
                   suppliers=[{"name": "Fastenal", "price": 0.21, "currency": "CAD", "partNumber": "11112233"},
                              {"name": "McMaster-Carr", "price": 0.3, "currency": "USD", "partNumber": "91290A396"}]),
        M.new_part(id="p2", name="6205-2RS bearing", type="purchased", currentStock=1, reorderPoint=4, reorderQty=10,
                   suppliers=[{"name": "McMaster-Carr", "price": 9.5, "currency": "USD", "partNumber": "6455K16"}]),
        M.new_part(id="p3", name="Gas spring 200N", type="purchased", currentStock=0, reorderPoint=2, reorderQty=4),
    ]
    P = __import__("daedalus.inventory.purchase_order", fromlist=["x"])
    st = P.settings(inv)
    st.update(companyName="Rangate", phone="604-555-0100")
    P.save_settings(inv, st)
    return inv


def test_group_and_compose():
    inv = inv_with_parts()
    items = R.from_reorder(inv)
    assert {p["id"] for p, _q in items} == {"p1", "p2", "p3"}
    g = R.group(inv, items)
    assert sorted(g) == ["Fastenal", "McMaster-Carr", R.NO_VENDOR] and len(g["McMaster-Carr"]) == 2
    assert sorted(R.group(inv, items, every_supplier=False)) == ["Fastenal", "McMaster-Carr", R.NO_VENDOR]
    assert [p["id"] for p, _ in R.group(inv, items, every_supplier=False)["Fastenal"]] == ["p1"]
    m = R.compose(inv, "Fastenal", g["Fastenal"], due="2026-12-04")
    assert m["to"] == "chris@fastenal.example" and m["rfq"] == "RFQ-0001"
    assert m["subject"] == "Request for quote RFQ-0001 — Rangate"
    assert m["body"].startswith("Hi Chris,") and "your #11112233" in m["body"] and "our #F-0825" in m["body"]
    assert "200 ea M8x25 SHCS" in m["body"] and "Rangate" in m["body"] and "{" not in m["body"]
    assert "last" not in m["body"]
    assert "last 0.21" in R.compose(inv, "Fastenal", g["Fastenal"], prices=True)["body"].replace("$", "")
    url = R.mailto(m)
    assert url.startswith("mailto:chris@fastenal.example?subject=Request%20for%20quote")
    assert R.mailto({**m, "body": "x" * 5000}) is None


def test_eml_draft_has_the_sheet():
    inv = inv_with_parts()
    g = R.group(inv, R.from_reorder(inv))
    m = R.compose(inv, "McMaster-Carr", g["McMaster-Carr"])
    for kind, ctype in (("csv", "text/csv"),
                        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")):
        msg = email.message_from_bytes(R.eml_bytes(m, kind), policy=email.policy.default)
        assert msg["X-Unsent"] == "1" and msg["Subject"] == m["subject"] and msg["To"] == ""
        parts = [p for p in msg.walk() if p.get_filename()]
        assert len(parts) == 1 and parts[0].get_content_type() == ctype
        assert parts[0].get_filename() == f"RFQ-0001_McMaster-Carr.{kind}"
    assert "Hi McMaster-Carr" in m["body"]                      # no contact on file → the vendor's name
    no_att = email.message_from_bytes(R.eml_bytes(m, "none"))
    assert not [p for p in no_att.walk() if p.get_filename()]


def test_log_quote_and_sheet():
    inv = inv_with_parts()
    g = R.group(inv, R.from_reorder(inv))
    mails = [R.compose(inv, n, it) for n, it in g.items() if n != R.NO_VENDOR]
    R.log(inv, mails)
    st = R.settings(inv)
    assert st["next"] == 2 and R.next_number(st) == "RFQ-0002" and len(R.open_requests(inv)) == 2
    e = next(x for x in R.open_requests(inv) if x["vendor"] == "McMaster-Carr")
    filled = mails[1]["csv"] if mails[1]["vendor"] == "McMaster-Carr" else mails[0]["csv"]
    rows = filled.splitlines()
    rows[1] = rows[1].replace(",USD,,", ",USD,,").split(",")
    rows[1][8] = "$0.27"
    rows[1] = ",".join(rows[1])
    prices = R.read_quote_csv("\n".join(rows), e, inv)
    assert prices == {"p1": 0.27}
    n = R.enter_quote(inv, e["id"], {**prices, "p2": "8.75"}, lead_days=5, currency="USD")
    assert n == 2
    s1 = next(s for s in inv.part("p1")["suppliers"] if s["name"] == "McMaster-Carr")
    s2 = inv.part("p2")["suppliers"][0]
    assert s1["price"] == 0.27 and s2["price"] == 8.75 and s2["quoteRef"] == "RFQ-0001"
    v = next(v for v in inv.vendors if v["name"] == "McMaster-Carr")
    assert v["leadTimeMaxDays"] == 5 and len(R.open_requests(inv)) == 1
    json.dumps(inv.syncMeta)                                     # stays storable


def test_dims_rfq_tool():
    from daedalus.ai.dims_tools import DimsToolExecutor
    inv = inv_with_parts()
    saved = []
    store = types.SimpleNamespace(inv=inv, save=lambda: saved.append(1))
    ex = DimsToolExecutor(lambda: store)
    out, err = ex.run("dims_rfq", {"why": "low"})
    assert not err
    j = json.loads(out)
    assert j["rfq"] == "RFQ-0001" and {e["vendor"] for e in j["emails"]} == {"Fastenal", "McMaster-Carr",
                                                                            R.NO_VENDOR}
    assert not j["logged"] and not saved
    out, err = ex.run("dims_rfq", {"parts": [{"part": "6205-2RS bearing", "qty": 20}], "log": True})
    j = json.loads(out)
    assert not err and j["count"] == 1 and "20 ea 6205-2RS bearing" in j["emails"][0]["body"] and saved
    assert R.settings(inv)["next"] == 2


def test_rfq_dialog(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from daedalus.ui import rfq_ui
    inv = inv_with_parts()
    commits = []
    page = types.SimpleNamespace(inv=inv, commit=lambda msg, undo=None: commits.append(msg))
    opened = []
    monkeypatch.setattr(rfq_ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(rfq_ui, "drafts_dir", lambda: tmp_path)
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    d = rfq_ui.RFQDialog(page, R.from_reorder(inv), parent=host)
    assert d.vendors.count() == 3 and d.subject.text().startswith("Request for quote RFQ-0001")
    d.body.setPlainText(d.body.toPlainText() + "\nPS edited")
    d.open_draft()
    assert opened and opened[0].endswith(".eml") and "PS edited" in email.message_from_bytes(
        d.last_draft.read_bytes(), policy=email.policy.default).get_body(("plain",)).get_content()
    d.vendors.setCurrentRow(1)
    d.copy()
    assert "Subject: Request for quote" in QApplication.clipboard().text()
    d.finish()
    assert commits and "2 vendor" in commits[-1] and len(R.open_requests(inv)) == 2
    q = rfq_ui.QuotesDialog(page, parent=host)
    assert q.pick.count() == 2 and q.table.rowCount() >= 1
    q.table.item(0, 4).setText("1.23")
    q.lead.setText("3")
    q.save()
    assert "price(s)" in commits[-1] and len(R.open_requests(inv)) == 1
