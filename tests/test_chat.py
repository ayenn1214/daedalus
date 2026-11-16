"""DIMS team chat: item links (Shift-click), file backend, panel."""
import os

from daedalus.inventory import chat as C
from daedalus.inventory import model as M


def shop():
    inv = M.Inventory()
    inv.userName = "Ankur"
    pid = inv.find_or_create_project("RLC")
    tab = M.new_part(id="tab", name="FLAT, TAB", partNumber="M10-0005", type="manufactured", currentStock=3)
    frame = M.new_part(id="frame", name="WLDMT, FRAME", partNumber="M30-0001", type="manufactured", isWeldment=True,
                       components=[{"partId": "tab", "qty": 4}])
    bolt = M.new_part(id="bolt", name="3/8 x 1 HHCS", type="fastener")
    inv.parts = [tab, frame, bolt]
    inv.set_allocation(frame, pid, 1)
    return inv, pid


def test_tokens_and_quality():
    inv, pid = shop()
    t = C.link_for(inv, "part", "tab")
    assert t == "{{part:tab|M10-0005 — FLAT, TAB}}" or t.startswith("{{part:tab|")
    body = f"is {t} cut yet? see {C.link_for(inv, 'project', pid)}"
    ps = C.parts_of(body)
    assert [p[0] for p in ps] == ["text", "link", "text", "link"] and ps[1][1:3] == ("part", "tab")
    assert C.plain(body).startswith("is [") and "[RLC]" in C.plain(body)
    assert C.quality(inv, "part", "tab") == "rare" and C.quality(inv, "part", "frame") == "epic"
    assert C.quality(inv, "part", "bolt") == "common" and C.quality(inv, "project", pid) == "legendary"
    assert C.quality(inv, "part", "nope") == "gone" and C.link_for(inv, "part", "nope") is None
    assert "Stock 3" in C.tooltip(inv, "part", "tab") and "RLC" in C.tooltip(inv, "project", pid)
    assert C.token("part", "x", "a {b} | [c]") == "{{part:x|a b  c}}"
    assert C.mentions("hey @dan look", "Dan Smith") and not C.mentions("hey dan", "Dan")


def test_file_chat_roundtrip(tmp_path):
    ch = C.FileChat(tmp_path / "inv.chat.jsonl")
    a = ch.post(C.new_message("Ankur", "hello"))
    b = ch.post(C.new_message("Dan", "hi"))
    assert [m["body"] for m in ch.fetch()] == ["hello", "hi"]
    assert ch.fetch(b["at"]) == [] and {m["id"] for m in ch.fetch(a["at"] - 1)} == {a["id"], b["id"]}
    ch.delete(a["id"])
    assert [m["body"] for m in ch.fetch()] == ["hi"]


def test_chat_panel_shift_click_and_open(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv, pid = shop()
    st.save()
    page = InventoryPage(st, lite=True)
    page.inv.userName = "Ankur"
    page.resize(1400, 900)
    page.show()
    page.goto("parts")
    app.processEvents()
    from PySide6.QtWidgets import QTableWidget
    t = next(x for x in page._browser.findChildren(QTableWidget) if x.rowCount() == 3 and x.isVisible())
    rect = t.visualItemRect(t.item(0, 2))
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(rect.center()), QPointF(t.viewport().mapToGlobal(rect.center())),
                     Qt.LeftButton, Qt.LeftButton, Qt.ShiftModifier)
    QApplication.sendEvent(t.viewport(), ev)                                # Shift-click a part row
    app.processEvents()
    assert page.chat.isVisible() and page.chat.input.text().startswith("[")
    page.chat.input.setText(page.chat.input.text() + "ready to cut?")
    page.chat.send()
    msgs = C.FileChat(tmp_path / "inv.chat.jsonl").fetch()
    assert len(msgs) == 1 and C.LINK_RE.search(msgs[0]["body"]) and msgs[0]["by"] == "Ankur"
    assert "dims:part:" in page.chat.view.toHtml()
    pid_linked = C.LINK_RE.search(msgs[0]["body"]).group(2)
    page.goto("projects")
    page.open_link("part", pid_linked)                                     # clicking the link opens the part
    app.processEvents()
    assert page.current_key() == "parts" and page.parts_sel == pid_linked
    # someone else posts → unread badge while chat is hidden
    page.toggle_chat(False)
    C.FileChat(tmp_path / "inv.chat.jsonl").post(C.new_message("Dan", "@Ankur yes, tomorrow"))
    page.chat.poll()
    assert page.chat.unread == 1 and "1" in page.chat_btn.text()
    page.toggle_chat(True)
    assert page.chat.unread == 0
    page.close()
