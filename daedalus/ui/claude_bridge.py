"""Claude Desktop link, inside Daedalus: a tool server on 127.0.0.1 for `python -m daedalus.mcp`, and the
Help ▸ Connect to Claude Desktop… window. Tool calls run on the GUI thread, exactly like Icarus's."""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from .. import __version__
from .. import claude_link as L

GROUP_S = 45.0          # calls this close together are one request: one undo step in Daedalus


class ClaudeBridge(QObject):
    request = Signal(str, object)
    activity = Signal(str)                              # 'Claude Desktop: add_extrude' — for the status bar

    def __init__(self, executor, tools: list[dict], guide: str, kind: str, doc=None, parent=None):
        super().__init__(parent)
        self.executor, self.tools, self.guide, self.kind, self.doc = executor, tools, guide, kind, doc
        self.token = secrets.token_urlsafe(24)
        self.result = ("", False)
        self.calls = 0
        self._last = 0.0
        self._lock = threading.Lock()                   # one tool at a time, like Icarus
        self.request.connect(self._run, Qt.BlockingQueuedConnection)
        self.server = None

    # ---------------------------------------------------------------- GUI thread
    @Slot(str, object)
    def _run(self, name, args):
        try:
            now = time.time()
            if self.doc is not None and now - self._last > GROUP_S and not name.startswith(("get_", "list_", "dims_find",
                                                                                          "dims_get", "dims_list")):
                try:
                    self.doc.checkpoint()               # a new request from Claude: its own undo step
                except Exception:  # noqa: BLE001
                    pass
            self._last = now
            self.result = self.executor.run(name, args)
            self.calls += 1
            self.activity.emit(f"Claude Desktop: {name}")
        except Exception as e:  # noqa: BLE001
            self.result = (f"{e.__class__.__name__}: {e}", True)

    def info(self) -> dict:
        d = {"tools": len(self.tools), "calls": self.calls}
        if self.doc is not None:
            try:
                d["objects"] = [o.name for o in self.doc.objects]
                d["features"] = len(self.doc.features)
            except Exception:  # noqa: BLE001
                pass
        return d

    # ---------------------------------------------------------------- server thread
    def start(self) -> int:
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _ok(self):
                if self.headers.get("X-Daedalus-Token") != bridge.token:
                    self._send(403, {"error": "bad token"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if not self._ok():
                    return
                if self.path == "/tools":
                    return self._send(200, bridge.tools)
                if self.path == "/guide":
                    return self._send(200, bridge.guide)
                if self.path == "/info":
                    return self._send(200, bridge.info())
                self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if not self._ok():
                    return
                if self.path != "/call":
                    return self._send(404, {"error": "not found"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                name = str(body.get("name") or "")
                if name not in {t["name"] for t in bridge.tools}:
                    return self._send(200, {"text": f"Unknown tool {name}", "isError": True})
                with bridge._lock:
                    bridge.request.emit(name, body.get("arguments") or {})   # runs on the GUI thread
                    text, err = bridge.result
                self._send(200, {"text": text, "isError": bool(err)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="claude-link").start()
        port = self.server.server_address[1]
        L.write_link(port, self.token, self.kind, self.tools, self.guide, __version__)
        return port

    def stop(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
        L.clear_link()


def start_bridge(window, executor, tools, guide, kind, doc=None):
    """Start the link for a window (unless turned off in settings); stops when the app quits."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    if not QSettings("Daedalus", "Daedalus").value("claude/link", True, type=bool):
        return None
    try:
        b = ClaudeBridge(executor, tools, guide, kind, doc, parent=window)
        b.start()
    except OSError:
        return None
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(b.stop)
    if hasattr(window, "statusBar"):
        b.activity.connect(lambda t: window.statusBar().showMessage(t, 4000))
    return b


class ClaudeDesktopDialog(QDialog):
    """Help ▸ Connect to Claude Desktop…"""

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle("Claude Desktop")
        self.resize(680, 720)
        v = QVBoxLayout(self)
        intro = QLabel("<b>Use Daedalus from Claude Desktop.</b> Claude gets Icarus's tools — 3D modelling and DIMS "
                       "inventory — and works in this window: parts build live here (each request is one undo "
                       "step), inventory changes save under your account. It runs on your Claude plan, no Ollama "
                       "needed.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.state, 1)
        how = QLabel("After connecting, <b>quit and reopen Claude Desktop</b> (File ▸ Exit, or right-click its tray "
                     "icon ▸ Quit). Then ask it, e.g. <i>“In Daedalus, model a 200×100×12 plate with four M8 "
                     "holes”</i> or <i>“What's short for the RFP 1308 build?”</i>. If Daedalus is closed, Claude can "
                     "open it.")
        how.setWordWrap(True)
        how.setObjectName("muted")
        v.addWidget(how)
        row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self.do_connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.do_disconnect)
        row.addWidget(self.connect_btn)
        row.addWidget(self.disconnect_btn)
        row.addStretch(1)
        pick = QPushButton("Choose settings file…")
        pick.setToolTip("If Claude Desktop keeps its settings somewhere else: in Claude Desktop, Settings ▸ Developer ▸ "
                        "Edit Config shows the file — pick that claude_desktop_config.json here")
        pick.clicked.connect(self.choose_file)
        copy = QPushButton("Copy settings")
        copy.setToolTip("Copy the Daedalus entry, to paste into Claude Desktop ▸ Settings ▸ Developer ▸ Edit Config")
        copy.clicked.connect(self.copy_snippet)
        row.addWidget(pick)
        row.addWidget(copy)
        v.addLayout(row)
        # ---- by hand: the settings file and what's in it, editable
        cap = QLabel("<b>Edit by hand</b> — Claude Desktop's settings file and its contents. Claude Desktop ▸ Settings ▸ "
                     "Developer ▸ Edit Config shows where its file really is.")
        cap.setWordWrap(True)
        v.addWidget(cap)
        prow = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("…\\claude_desktop_config.json")
        self.path.editingFinished.connect(self.load_file)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_file)
        prow.addWidget(QLabel("Settings file"))
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        v.addLayout(prow)
        self.editor = QPlainTextEdit()
        f = self.editor.font()
        f.setFamily("Consolas")
        f.setStyleHint(f.StyleHint.Monospace)
        self.editor.setFont(f)
        self.editor.setMinimumHeight(220)
        self.editor.textChanged.connect(self._check_json)
        v.addWidget(self.editor, 2)
        erow = QHBoxLayout()
        ins = QPushButton("Insert Daedalus entry")
        ins.setToolTip("Adds (or updates) \"daedalus\" under \"mcpServers\" in the text above — the rest is kept")
        ins.clicked.connect(self.insert_entry)
        self.json_ok = QLabel()
        self.json_ok.setObjectName("muted")
        self.save_btn = QPushButton("Save file")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save_file)
        erow.addWidget(ins)
        erow.addWidget(self.json_ok, 1)
        erow.addWidget(self.save_btn)
        v.addLayout(erow)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        v.addWidget(bb)
        self.refresh()
        paths = L.status()["paths"]
        self.path.setText(paths[0] if paths else "")
        self.load_file()

    # ---------------------------------------------------------------- by hand
    def load_file(self):
        from pathlib import Path
        p = Path(self.path.text().strip().strip('"'))
        try:
            text = p.read_text(encoding="utf-8") if p.is_file() else ""
        except OSError as e:
            text = ""
            self.json_ok.setText(f"Can't read it: {e}")
        self.editor.setPlainText(text or "{\n  \"mcpServers\": {}\n}\n")
        self._check_json()

    def _check_json(self):
        import json as _j
        try:
            d = _j.loads(self.editor.toPlainText() or "{}")
            ok = isinstance(d, dict)
            has = ok and L.SERVER_NAME in (d.get("mcpServers") or {})
            self.json_ok.setText(("✓ valid JSON" if ok else "✗ must be a { … } object") +
                                 (" · Daedalus entry present" if has else " · no Daedalus entry yet"))
            self.save_btn.setEnabled(ok)
        except ValueError as e:
            self.json_ok.setText(f"✗ not valid JSON: {e}")
            self.save_btn.setEnabled(False)

    def insert_entry(self):
        import json as _j
        try:
            d = _j.loads(self.editor.toPlainText() or "{}")
        except ValueError as e:
            return QMessageBox.warning(self, "Claude Desktop", f"Fix the JSON first:\n{e}")
        if not isinstance(d, dict):
            d = {}
        d.setdefault("mcpServers", {})[L.SERVER_NAME] = L.server_entry()
        self.editor.setPlainText(_j.dumps(d, indent=2) + "\n")

    def save_file(self):
        import json as _j
        import os as _os
        import shutil as _sh
        from pathlib import Path

        from PySide6.QtCore import QSettings
        p = Path(self.path.text().strip().strip('"'))
        if not p.name:
            return QMessageBox.warning(self, "Claude Desktop", "Enter the settings file's path")
        try:
            _j.loads(self.editor.toPlainText())
        except ValueError as e:
            return QMessageBox.warning(self, "Claude Desktop", f"Not saved — the JSON isn't valid:\n{e}")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                _sh.copy2(p, p.with_name(p.name + ".before-daedalus"))
            tmp = p.with_suffix(".tmp")
            tmp.write_text(self.editor.toPlainText(), encoding="utf-8")
            _os.replace(tmp, p)
        except OSError as e:
            return QMessageBox.warning(self, "Claude Desktop", f"Couldn't save:\n{e}")
        QSettings("Daedalus", "Daedalus").setValue("claude/config_path", str(p))   # Connect uses it from now on
        self.refresh()
        QMessageBox.information(self, "Claude Desktop", f"Saved {p}.\n\nQuit and reopen Claude Desktop to load it.")

    def refresh(self):
        st = L.status()
        link = L.read_link()
        lines = [("✓ " if link else "✗ ") + (f"Daedalus link is on (this window, {self.bridge.kind == '3d' and '3D + DIMS' or 'DIMS'} "
                                              f"tools, {len(self.bridge.tools)} of them)" if self.bridge and link else
                                              "Daedalus link is off (Help ▸ Connect to Claude Desktop turns it on at the "
                                              "next start)")]
        if not st["installed"]:
            lines.append("✗ Claude Desktop isn't installed on this computer (claude.ai/download) — connecting writes "
                         "its settings anyway, ready for when it is.")
        lines.append(("✓ Connected — Claude Desktop starts Daedalus's tools" if st["connected"] else
                      "✗ Not connected yet") + f"<br><span style='color:gray'>{'<br>'.join(st['paths'])}</span>")
        if self.bridge and self.bridge.calls:
            lines.append(f"Claude has run {self.bridge.calls} tool call(s) in this session.")
        self.state.setText("<br><br>".join(lines))
        self.connect_btn.setText("Connect again" if st["connected"] else "Connect")
        self.disconnect_btn.setEnabled(st["connected"])

    def choose_file(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QFileDialog
        paths = L.status()["paths"]
        start = paths[0] if paths else ""
        fn, _ = QFileDialog.getOpenFileName(self, "Claude Desktop's settings (claude_desktop_config.json)", start,
                                            "Claude Desktop settings (claude_desktop_config.json);;JSON (*.json)")
        if fn:
            QSettings("Daedalus", "Daedalus").setValue("claude/config_path", fn)
            self.refresh()
            if hasattr(self, "path"):
                self.path.setText(fn)
                self.load_file()

    def copy_snippet(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(L.snippet())
        QMessageBox.information(self, "Claude Desktop", "Copied. In Claude Desktop: Settings ▸ Developer ▸ Edit Config "
                                "opens its settings file — paste the \"daedalus\" entry inside \"mcpServers\" (or the "
                                "whole thing if the file is empty), save, then quit and reopen Claude Desktop.")

    def do_connect(self):
        try:
            done = L.connect()
        except (OSError, ValueError) as e:
            return QMessageBox.warning(self, "Claude Desktop", f"Couldn't update Claude Desktop's settings:\n{e}")
        from PySide6.QtCore import QSettings
        QSettings("Daedalus", "Daedalus").setValue("claude/link", True)
        self.refresh()
        if hasattr(self, "path") and done:
            self.path.setText(done[0])
            self.load_file()
        QMessageBox.information(self, "Claude Desktop", "Connected:\n" + "\n".join(done) +
                                "\n\nQuit and reopen Claude Desktop to load Daedalus's tools.")

    def do_disconnect(self):
        try:
            L.disconnect()
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Claude Desktop", str(e))
        self.refresh()
