"""DIMS team chat panel. Shift-click a part, project or order anywhere in DIMS to drop a link into the message —
the link shows as [label] in its rarity colour, hovering shows the record as it is now, clicking opens it."""
from __future__ import annotations

import datetime as dt
import html
import threading

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTextBrowser, QToolTip, QVBoxLayout, QWidget,
)

from ..inventory import chat as C
from ..inventory.chat import GENERAL
from . import theme

# item "rarity" colours (dark theme, light theme)
QUALITY = {"poor": ("#9d9d9d", "#6b6b6b"), "common": ("#e6e6e6", "#3a3a3a"), "uncommon": ("#1eff00", "#1a8f00"),
           "rare": ("#3d9bff", "#0b5bc4"), "epic": ("#b867ff", "#7d2bc9"), "legendary": ("#ff8000", "#c25e00"),
           "artifact": ("#e6cc80", "#a0812b"), "heirloom": ("#00ccff", "#0088aa"), "gone": ("#ff5050", "#c01010")}

LINK_ROLE = Qt.UserRole + 77            # table items carry (kind, id) here so Shift-click can link them


def colour(q: str) -> str:
    dark = "light" not in theme._mode()
    a, b = QUALITY.get(q, QUALITY["common"])
    return a if dark else b


def tag(item, kind: str, id_: str):
    """Mark a table item as linkable (Shift-click → chat)."""
    if item is not None and id_:
        item.setData(LINK_ROLE, (kind, str(id_)))
    return item


class ChatPanel(QWidget):
    open_link = Signal(str, str)        # kind, id
    unread_changed = Signal(int)
    pop_requested = Signal(bool)        # True: own window, False: back beside DIMS
    _fetched = Signal(object)

    def set_popped(self, on: bool):
        self.popped = on
        self.pop_btn.setText("⇲ Dock" if on else "⧉ Pop out")
        self.pop_btn.setToolTip("Put the chat back beside DIMS" if on else
                                "Open the chat in its own window — move and resize it anywhere, e.g. a second screen")
        self.pin_btn.setVisible(on)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.popped = False
        self.msgs: list[dict] = []
        self.links: dict[str, str] = {}     # "[label]" in the input → token
        self.unread = 0
        self._busy = False
        self._error = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        head = QHBoxLayout()
        t = QLabel("<b>Team chat</b>")
        head.addWidget(t)
        self.status = QLabel()
        self.status.setObjectName("muted")
        head.addWidget(self.status, 1)
        from PySide6.QtWidgets import QToolButton
        self.pin_btn = QToolButton()
        self.pin_btn.setText("📌")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setAutoRaise(True)
        self.pin_btn.setToolTip("Keep the chat window on top of other windows")
        self.pin_btn.hide()
        head.addWidget(self.pin_btn)
        self.pop_btn = QToolButton()
        self.pop_btn.setAutoRaise(True)
        self.pop_btn.clicked.connect(lambda: self.pop_requested.emit(not self.popped))
        head.addWidget(self.pop_btn)
        self.set_popped(False)
        v.addLayout(head)
        from PySide6.QtWidgets import QComboBox
        rooms_row = QHBoxLayout()
        self.room_box = QComboBox()
        self.room_box.setToolTip("Team room, direct messages and groups")
        self.room_box.activated.connect(lambda i: self.switch_room(self.room_box.itemData(i)))
        rooms_row.addWidget(self.room_box, 1)
        self.elsewhere = QLabel()
        self.elsewhere.setToolTip("Unread in your other conversations — pick them in the list")
        rooms_row.addWidget(self.elsewhere)
        self.new_btn = QToolButton()
        self.new_btn.setText("+ New")
        self.new_btn.setToolTip("Direct message one person, or start a group")
        self.new_btn.clicked.connect(self.new_conversation)
        rooms_row.addWidget(self.new_btn)
        self.more_btn = QToolButton()
        self.more_btn.setText("⋯")
        self.more_btn.setToolTip("Rename the group, add people, or leave it")
        self.more_btn.clicked.connect(self._room_menu)
        rooms_row.addWidget(self.more_btn)
        v.addLayout(rooms_row)
        self.room = GENERAL
        self.rooms: dict[str, dict] = {}
        self.people: dict[str, str] = {}          # id → name
        self.unread_by: dict[str, int] = {}
        self._polls = 0
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._clicked)
        self.view.highlighted.connect(self._hover)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.view, 1)
        tip = QLabel("Shift-click a part, project or order to link it")
        tip.setObjectName("muted")
        v.addWidget(tip)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Message the team…  (@name to mention)")
        self.input.setMaxLength(C.MAX_LEN)
        self.input.returnPressed.connect(self.send)
        row.addWidget(self.input, 1)
        b = QPushButton("Send")
        b.setObjectName("primary")
        b.clicked.connect(self.send)
        row.addWidget(b)
        v.addLayout(row)
        self._fetched.connect(self._on_fetched)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(4000)
        self.backend = None
        self.rebind()

    # ------------------------------------------------------------ backend
    def rebind(self):
        """(Re)attach to the page's current store (it changes when switching file ⇄ team server)."""
        self.backend = C.chat_for(self.page.store)
        self._key = self._store_key()
        self.msgs, self.unread, self.unread_by = [], 0, {}
        self.rooms, self.people = {}, {}
        from PySide6.QtCore import QSettings
        self.room = QSettings("Daedalus", "Daedalus").value("dims/chat_room") or GENERAL
        self._fill_rooms()
        self.render()
        self.poll()
        self.load_rooms()

    # ------------------------------------------------------------ rooms
    def me(self):
        be = self.backend
        return be.me(self.page.inv) if be is not None else None

    def load_rooms(self, then=None):
        """People and rooms from the backend (a thread for the server)."""
        be = self.backend
        if be is None or not getattr(be, "rooms_ready", False):
            self._fill_rooms()
            return
        me = self.me()

        def work():
            try:
                res = ("rooms", be, (be.people(self.page.inv), be.rooms(me)))
            except Exception as e:  # noqa: BLE001
                res = ("rooms-err", be, e)
            self._fetched.emit(res + ((then,) if then else ()))
        if isinstance(be, C.FileChat):
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def _fill_rooms(self):
        me = self.me()
        box = self.room_box
        box.blockSignals(True)
        box.clear()
        items = [(GENERAL, None)] + sorted(
            [(r["id"], r) for r in self.rooms.values()],
            key=lambda x: (x[1]["kind"] != "dm", C.room_title(x[1], me, self.people).lower()))
        for rid, r in items:
            n = self.unread_by.get(rid, 0)
            box.addItem(C.room_title(r, me, self.people) + (f"   ● {n}" if n else ""), rid)
        if self.room not in [rid for rid, _ in items]:
            self.room = GENERAL
        box.setCurrentIndex(max(0, box.findData(self.room)))
        box.blockSignals(False)
        other = sum(n for rid, n in self.unread_by.items() if rid != self.room)
        self.elsewhere.setText(f"<b style='color:{theme.INFO}'>● {other}</b>" if other else "")
        ok = self.backend is not None and getattr(self.backend, "rooms_ready", False)
        self.new_btn.setEnabled(ok)
        self.new_btn.setToolTip("Direct message one person, or start a group" if ok else
                                "Direct messages need the chat update on the team server")
        r = self.rooms.get(self.room)
        self.more_btn.setVisible(bool(r and r.get("kind") == "group"))
        if hasattr(self, "input"):
            self.input.setPlaceholderText(
                "Message the team…  (@name to mention)" if self.room == GENERAL else
                f"Message {C.room_title(r, me, self.people)[2:]}…")

    def switch_room(self, rid):
        self.room = rid or GENERAL
        from PySide6.QtCore import QSettings
        QSettings("Daedalus", "Daedalus").setValue("dims/chat_room", self.room)
        self.unread_by.pop(self.room, None)
        self._recount()
        self._fill_rooms()
        self._rendered = False
        self.render()

    def _recount(self):
        self.unread = sum(self.unread_by.values())
        self.unread_changed.emit(self.unread)

    def new_conversation(self):
        be = self.backend
        if be is None or not getattr(be, "rooms_ready", False):
            return
        d = NewChatDialog(self, [p for p in (be.people(self.page.inv) or []) if str(p["id"]) != str(self.me())])
        if not d.exec():
            return
        ids, name = d.result_people(), d.name.text().strip()
        try:
            room = C.new_room("dm" if len(ids) == 1 else "group", ids, name, me=self.me())
            room = be.save_room(room)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"couldn't start it — {e}")
            return
        self.rooms[room["id"]] = room
        for p in d.all_people:
            self.people.setdefault(p["id"], p["name"])
        self.switch_room(room["id"])
        self.input.setFocus()

    def _room_menu(self):
        r = self.rooms.get(self.room)
        if not r or r.get("kind") != "group":
            return
        m = QMenu(self)
        m.addAction("Rename…", self._rename_room)
        m.addAction("Add people…", self._add_people)
        m.addAction("Members: " + ", ".join(self.people.get(x, str(x)[:8]) for x in r["members"])).setEnabled(False)
        m.addSeparator()
        m.addAction("Leave group", self._leave_room)
        m.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def _save_room(self, r):
        try:
            self.backend.save_room(r)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"not saved — {e}")
            return False
        self.rooms[r["id"]] = r
        self._fill_rooms()
        return True

    def _rename_room(self):
        from PySide6.QtWidgets import QInputDialog
        r = dict(self.rooms[self.room])
        name, ok = QInputDialog.getText(self, "Rename group", "Name:", text=r.get("name") or "")
        if ok and name.strip():
            r["name"] = name.strip()[:80]
            self._save_room(r)

    def _add_people(self):
        r = dict(self.rooms[self.room])
        have = {str(x) for x in r["members"]}
        d = NewChatDialog(self, [p for p in self.backend.people(self.page.inv) if str(p["id"]) not in have],
                          adding=True)
        if d.exec() and d.result_people():
            r["members"] = list(r["members"]) + d.result_people()
            self._save_room(r)

    def _leave_room(self):
        r = dict(self.rooms[self.room])
        r["members"] = [x for x in r["members"] if str(x) != str(self.me())]
        if self._save_room(r) is not False:
            self.rooms.pop(r["id"], None)
            self.switch_room(GENERAL)

    def _store_key(self):
        st = self.page.store
        return id(st), str(getattr(st, "path", ""))

    def _since(self) -> int:
        return max((m["at"] for m in self.msgs), default=0) - 1

    def poll(self):
        if self._store_key() != getattr(self, "_key", None):
            return self.rebind()
        be = self.backend
        if be is None:
            self.status.setText("not available for this inventory")
            return
        if isinstance(be, C.ServerChat) and not be.ready:
            self.status.setText("needs the chat update on the server")
            return
        if self._busy:
            return
        self._polls += 1
        if self._polls % 8 == 0:
            self.load_rooms()
        since = self._since()
        if isinstance(be, C.FileChat):                    # local disk: quick, no thread
            try:
                self._on_fetched(("ok", be, be.fetch(since)))
            except OSError as e:
                self._on_fetched(("err", be, e))
            return
        self._busy = True

        def work():
            try:
                self._fetched.emit(("ok", be, be.fetch(since)))
            except Exception as e:  # noqa: BLE001
                self._fetched.emit(("err", be, e))
        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, res):
        kind, be, payload = res[:3]
        if kind in ("rooms", "rooms-err"):
            if be is not self.backend:
                return
            if kind == "rooms":
                people, rooms = payload
                self.people = {p["id"]: p["name"] for p in people}
                self.rooms = {r["id"]: r for r in rooms}
            self._fill_rooms()
            self.render()
            return
        self._busy = False
        if be is not self.backend:
            return
        if kind == "err":
            self.status.setText("can't load messages — hover for why")
            self.status.setToolTip(str(payload))
            return
        self.status.setToolTip("")
        known = {m["id"] for m in self.msgs}
        new = [m for m in payload if m["id"] not in known]
        if not new:
            return
        n = len(self.msgs) + len(new)
        self.status.setText(f"{n} message{'s' if n != 1 else ''}")
        first = not self.msgs
        self.msgs.extend(new)
        self.msgs.sort(key=lambda m: m["at"])
        me = self.page.inv.userName or ""
        if any(m.get("channel", GENERAL) not in self.rooms and m.get("channel", GENERAL) != GENERAL for m in new):
            self.load_rooms()                                   # added to a new DM / group
        if not first:
            looking = self.isVisible() and self.window().isActiveWindow()
            for m in new:
                ch = m.get("channel") or GENERAL
                if m.get("by") != me and not (looking and ch == self.room):
                    self.unread_by[ch] = self.unread_by.get(ch, 0) + 1
            self._recount()
            self._fill_rooms()
        self.render()

    # ------------------------------------------------------------ view
    def render(self):
        inv = self.page.inv
        me = inv.userName or ""
        out, last_day = [], None
        here = [m for m in self.msgs if (m.get("channel") or GENERAL) == self.room]
        for m in here[-400:]:
            t = dt.datetime.fromtimestamp(m["at"] / 1000)
            if t.date() != last_day:
                last_day = t.date()
                out.append(f"<p align='center' style='color:{theme.MUTED}'>— {t.strftime('%a %b %d')} —</p>")
            body = []
            for part in C.parts_of(m["body"]):
                if part[0] == "text":
                    body.append(self._mentions(html.escape(part[1]).replace("\n", "<br>"), me))
                else:
                    _k, kind, id_, lab = part
                    col = colour(C.quality(inv, kind, id_))
                    body.append(f"<a href='dims:{kind}:{html.escape(id_)}' style='color:{col}; "
                                f"text-decoration:none; font-weight:600'>[{html.escape(lab)}]</a>")
            who_col = theme.ACCENT_TEXT if m.get("by") == me else theme.INFO
            out.append(f"<p><span style='color:{theme.MUTED}'>{t.strftime('%H:%M')}</span> "
                       f"<b style='color:{who_col}'>{html.escape(m.get('by') or '?')}</b>: {''.join(body)}</p>")
        if not out:
            out.append(f"<p style='color:{theme.MUTED}'>No messages yet. Say hi — or Shift-click a part to ask "
                       "about it.</p>" if self.room == GENERAL else
                       f"<p style='color:{theme.MUTED}'>Start the conversation."
                       + ("" if getattr(self.backend, "private", True) else
                          " (In a shared file anyone with the file can read this.)") + "</p>")
        sb = self.view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        self.view.setHtml("".join(out))
        if at_end or not getattr(self, "_rendered", False):
            sb.setValue(sb.maximum())
        self._rendered = True

    @staticmethod
    def _mentions(text: str, me: str) -> str:
        import re
        first = (me or "").split()[0].lower() if me else ""

        def sub(mm):
            mine = first and mm.group(1).lower() == first
            col = theme.WARN if mine else theme.INFO
            return f"<b style='color:{col}'>@{mm.group(1)}</b>"
        return re.sub(r"@(\w+)", sub, text)

    def showEvent(self, e):
        super().showEvent(e)
        self.mark_read()
        self.render()

    def mark_read(self):
        """Seen the open room (others keep their count)."""
        if self.unread_by.pop(self.room, None) or self.unread != sum(self.unread_by.values()):
            self._recount()
            self._fill_rooms()

    # ------------------------------------------------------------ links
    def insert_link(self, kind: str, id_: str) -> bool:
        tok = C.link_for(self.page.inv, kind, id_)
        if tok is None:
            return False
        lab = C.LINK_RE.fullmatch(tok).group(3)
        shown = f"[{lab}]"
        self.links[shown] = tok
        cur = self.input.text()
        sep = "" if not cur or cur.endswith(" ") else " "
        self.input.setText(cur + sep + shown + " ")
        self.input.setFocus()
        self.input.end(False)
        return True

    def insert_quote(self, text: str) -> bool:
        """Something with no record behind it: drop its text in as a quote."""
        text = " ".join((text or "").split())[:160]
        if not text:
            return False
        cur = self.input.text()
        sep = "" if not cur or cur.endswith(" ") else " "
        self.input.setText(cur + sep + f"“{text}” ")
        self.input.setFocus()
        self.input.end(False)
        return True

    def _encode(self, text: str) -> str:
        for shown, tok in sorted(self.links.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(shown, tok)
        return text

    def _parse(self, url: QUrl):
        s = url.toString()
        if not s.startswith("dims:"):
            return None
        kind, _, id_ = s[5:].partition(":")
        return kind, id_

    def _clicked(self, url: QUrl):
        x = self._parse(url)
        if x:
            self.open_link.emit(*x)

    def _hover(self, url):
        x = self._parse(url) if isinstance(url, QUrl) else self._parse(QUrl(url))
        if not x:
            QToolTip.hideText()
            return
        QToolTip.showText(QCursor.pos() + QPoint(14, 8), C.tooltip(self.page.inv, *x), self.view)

    def _menu(self, pos):
        menu = self.view.createStandardContextMenu()
        me = self.page.inv.userName or ""
        mine = [m for m in self.msgs if m.get("by") == me and (m.get("channel") or GENERAL) == self.room][-5:]
        if mine and self.backend is not None:
            sub = QMenu("Delete my message", menu)
            for m in reversed(mine):
                sub.addAction(C.plain(m["body"])[:60], lambda mm=m: self.delete(mm))
            menu.addMenu(sub)
        menu.exec(self.view.mapToGlobal(pos))

    # ------------------------------------------------------------ send / delete
    def send(self):
        text = self.input.text().strip()
        if not text or self.backend is None:
            return
        try:
            msg = C.new_message(self.page.inv.userName or "unknown", self._encode(text), self.room)
            got = self.backend.post(msg)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"not sent — {e}")
            return
        self.input.clear()
        self.links.clear()
        if got["id"] not in {m["id"] for m in self.msgs}:
            self.msgs.append(got)
        self.render()

    def delete(self, m):
        try:
            self.backend.delete(m["id"])
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"not deleted — {e}")
            return
        self.msgs = [x for x in self.msgs if x["id"] != m["id"]]
        self.render()


class NewChatDialog(QDialog):
    """Pick one person (direct message) or several (group, with a name)."""

    def __init__(self, parent, people, adding=False):
        super().__init__(parent)
        self.all_people = people
        self.setWindowTitle("Add people" if adding else "New conversation")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Pick people to add:" if adding else
                           "Pick one person for a direct message, or several for a group:"))
        self.list = QListWidget()
        for p in people:
            it = QListWidgetItem(p["name"])
            it.setData(Qt.UserRole, p["id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.list.addItem(it)
        if not people:
            v.addWidget(QLabel("No one else yet — teammates appear once they've signed in to DIMS."))
        v.addWidget(self.list, 1)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Group name, e.g. Shop floor, RFP 1308 build")
        self.name.setVisible(not adding)
        v.addWidget(self.name)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)
        v.addWidget(self.bb)
        self.adding = adding
        self.list.itemChanged.connect(lambda _i: self._sync())
        self._sync()

    def result_people(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def _sync(self):
        n = len(self.result_people())
        ok = self.bb.button(QDialogButtonBox.Ok)
        if self.adding:
            ok.setText("Add")
        else:
            ok.setText("Message" if n == 1 else "Start group")
            self.name.setEnabled(n > 1)
        ok.setEnabled(n >= 1)

    def _ok(self):
        if not self.adding and len(self.result_people()) > 1 and not self.name.text().strip():
            self.name.setFocus()
            self.name.setPlaceholderText("Name the group first")
            return
        self.accept()
