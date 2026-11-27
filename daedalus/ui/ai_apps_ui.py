"""Help ▸ Connect AI apps… — give ChatGPT desktop / Codex, Gemini CLI, Claude Code, LM Studio, Cursor, Windsurf and
VS Code the Daedalus tools (the same link Claude Desktop uses)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from .. import ai_apps as A
from . import theme


class AIAppsDialog(QDialog):
    def __init__(self, bridge=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle("Connect AI apps")
        v = QVBoxLayout(self)
        intro = QLabel("These apps can use every Icarus tool in this Daedalus window, like Claude Desktop does. "
                       "Connect adds Daedalus to the app's settings and keeps everything else (the old file is saved "
                       "as <i>.before-daedalus</i>); restart the app afterwards.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        link = "on" if bridge is not None and getattr(bridge, "server", None) is not None else \
            "<span style='color:%s'>off — turn it on in Settings ▸ Claude Desktop</span>" % theme.ERROR
        self.link = QLabel(f"Daedalus link: {link}")
        v.addWidget(self.link)
        self.grid_host = QWidget()
        v.addWidget(self.grid_host)
        tip = QLabel("ChatGPT desktop and Codex share one settings file, so one Connect covers both. Gemini CLI only "
                     "loads servers in folders you've trusted. ChatGPT on the web can't reach programs on this "
                     "computer. Prefer to type in Daedalus? Pick ChatGPT / Gemini / Claude (your plan) as Icarus's "
                     "engine.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        v.addWidget(tip)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        self.refresh()
        self.resize(700, 600)

    def refresh(self):
        old = self.grid_host.layout()
        if old is not None:
            while old.count():
                w = old.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
            QWidget().setLayout(old)                    # drop the old layout
        g = QGridLayout(self.grid_host)
        g.setColumnStretch(1, 1)
        g.setVerticalSpacing(10)
        for r, st in enumerate(A.all_status()):
            name = QLabel(f"<b>{st['name']}</b><br><span style='color:{theme.MUTED}'>{st['what']}</span>")
            name.setToolTip(st["path"])
            name.setMinimumHeight(name.sizeHint().height() + 4)
            g.addWidget(name, r, 0, 1, 2)
            if st["connected"]:
                state, col = "Connected", theme.OK
            elif st["installed"]:
                state, col = "Not connected", theme.MUTED
            else:
                state, col = "Not found", theme.MUTED
            lab = QLabel(state)
            lab.setStyleSheet(f"color:{col}")
            g.addWidget(lab, r, 2, Qt.AlignRight)
            b = QPushButton("Disconnect" if st["connected"] else "Connect")
            if not st["connected"]:
                b.setObjectName("primary")
            b.clicked.connect(lambda _c=False, k=st["key"], on=st["connected"]: self.toggle(k, on))
            g.addWidget(b, r, 3)

    def toggle(self, key, on):
        try:
            where = A.disconnect(key) if on else A.connect(key)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Connect AI apps", f"{A.APP[key][1]}: {e}")
            return
        if not on:
            QMessageBox.information(self, "Connect AI apps", f"{A.APP[key][1]} can now use Daedalus.\n\n{where}\n\n"
                                    "Restart the app (or start a new chat) to load it.")
        self.refresh()
