"""DIMS team chat panel. Shift-click a part, project or order anywhere in DIMS to drop a link into the message —
the link shows as [label] in its rarity colour, hovering shows the record as it is now, clicking opens it."""
from __future__ import annotations

import datetime as dt
import html
import threading

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QTextBrowser, QToolTip, QVBoxLayout, QWidget,
)

from ..inventory import chat as C
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
    _fetched = Signal(object)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
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
        v.addLayout(head)
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
        self.msgs, self.unread = [], 0
        self.render()
        self.poll()

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
        kind, be, payload = res
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
        if not first and not (self.isVisible() and self.window().isActiveWindow()):
            self.unread += sum(1 for m in new if m.get("by") != me)
            self.unread_changed.emit(self.unread)
        self.render()

    # ------------------------------------------------------------ view
    def render(self):
        inv = self.page.inv
        me = inv.userName or ""
        out, last_day = [], None
        for m in self.msgs[-400:]:
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
                       "about it.</p>")
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
        if self.unread:
            self.unread = 0
            self.unread_changed.emit(0)

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
        mine = [m for m in self.msgs if m.get("by") == me][-5:]
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
            msg = C.new_message(self.page.inv.userName or "unknown", self._encode(text))
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
