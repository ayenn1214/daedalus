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


def test_server_chat_parses_postgres_times_and_fetches_from_zero():
    """Windows / Python 3.10: no fromtimestamp before 1970, and Postgres trims fraction zeros (.38047)."""
    calls = []

    class FakeStore:
        schema = 3

        def _rest(self, method, path, body=None, headers=None):
            calls.append((method, path))
            if method == "GET":
                return 200, [{"id": "a", "at": "2026-11-16T23:16:08.52567+00:00", "by_name": "Daniel",
                              "channel": "general", "body": "Hello @Ankur", "user_id": "u"}]
            return 201, [{"id": body["id"], "at": "2026-11-16 23:16:15.888873+00", "by_name": body["by_name"],
                          "channel": "general", "body": body["body"]}]

        def _check_write(self, code, res, what):
            pass
    ch = C.ServerChat(FakeStore())
    got = ch.fetch(-1)                                                     # empty panel asks from "before" 1970
    assert "at=gt.1970-01-01T00%3A00%3A00.000Z" in calls[0][1] or "at=gt.1970-01-01T00:00:00.000Z" in calls[0][1]
    assert got[0]["by"] == "Daniel" and got[0]["at"] == C.ts_ms("2026-11-16T23:16:08.525670Z")
    m = ch.post(C.new_message("Ankur", "hi"))
    assert m["by"] == "Ankur" and m["at"] > got[0]["at"]
    assert C.ts_ms("2026-11-16T15:16:08-08:00") == C.ts_ms("2026-11-16T23:16:08Z")


def test_shift_click_anything(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QLabel, QTableWidget, QTreeWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    inv, pid = shop()
    inv.vendors.append({"id": "v1", "name": "Acme Steel"})
    assert C.resolve(inv, ["M10-0005"]) == ("part", "tab") and C.resolve(inv, ["× 4  FLAT, TAB"]) == ("part", "tab")
    assert C.resolve(inv, ["RLC"]) == ("project", pid) and C.resolve(inv, ["acme steel"]) == ("vendor", "v1")
    assert C.resolve(inv, ["M30-0001 — WLDMT, FRAME"]) == ("part", "frame") and C.resolve(inv, ["nothing"]) is None
    assert C.resolve(inv, [], ["frame"]) == ("part", "frame")
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.resize(1400, 900)
    page.show()

    def shift_click(w, pt):
        QApplication.sendEvent(w, QMouseEvent(QEvent.MouseButtonPress, QPointF(pt), QPointF(w.mapToGlobal(pt)),
                                              Qt.LeftButton, Qt.LeftButton, Qt.ShiftModifier))
        app.processEvents()
    page.goto("vendors")
    app.processEvents()
    root = page.pages["vendors"].widget()
    views = [v for v in root.findChildren(QTableWidget) + root.findChildren(QTreeWidget) if v.isVisible()
             and v.model().rowCount()]
    t = views[0]
    shift_click(t.viewport(), t.visualRect(t.model().index(0, 0)).center())     # a vendor row (untagged)
    assert "[Acme Steel]" in page.chat.input.text()
    page.chat.input.clear()
    page.goto("dashboard")
    app.processEvents()
    lab = next(x for x in page.pages["dashboard"].widget().findChildren(QLabel) if x.isVisible() and x.text())
    shift_click(lab, lab.rect().center())                                         # any label → linked or quoted
    assert page.chat.input.text().strip() != ""
    page.close()


def test_chat_pops_out_and_docks(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    QSettings("Daedalus", "Daedalus").setValue("dims/chat_popped", False)
    st = InventoryStore(tmp_path / "inv.json")
    page = InventoryPage(st, lite=True)
    page.show()
    page.toggle_chat(True)
    assert page.chat.isVisible() and page.split.indexOf(page.chat) >= 0
    page.pop_chat(True)
    app.processEvents()
    w = page._chat_win
    assert w is not None and w.isVisible() and page.chat.window() is w and page.split.indexOf(page.chat) < 0
    assert page.chat.pop_btn.text().endswith("Dock") and page.chat.pin_btn.isVisible()
    page.chat.pin_btn.setChecked(True)
    assert w.windowFlags() & __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WindowStaysOnTopHint
    page.toggle_chat(False)                                   # 💬 hides the window …
    assert not w.isVisible() and not page.chat_btn.isChecked()
    page.toggle_chat(True)                                    # … and brings it back popped out
    assert w.isVisible()
    assert page.link_to_chat("project", "nope") in (True, False)
    page.pop_chat(False)
    app.processEvents()
    assert page._chat_win is None and page.split.indexOf(page.chat) >= 0 and page.chat.isVisible()
    assert page.chat.pop_btn.text().endswith("Pop out")
    QSettings("Daedalus", "Daedalus").setValue("dims/chat_popped", False)
    QSettings("Daedalus", "Daedalus").setValue("dims/chat_visible", False)
    page.close()
