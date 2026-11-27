"""Copilot panel: chat with a local Ollama model (default) or Claude, which model through tool calls.

Threading: the agent loop runs on a QThread. Tool calls are marshalled to
the GUI thread with a BlockingQueuedConnection so the Document is only ever
touched from one thread. One prompt = one undo step.
"""
from __future__ import annotations

import html
import json
import os

import time

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..ai import ollama
from ..ai.copilot import DEFAULT_MODEL, Copilot
from . import theme

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
PROVIDERS = [("ollama", "Ollama (local)"), ("claude", "Claude (API)"), ("claude_code", "Claude (your plan)")]
VISION_HINT = "gemma3, qwen2.5vl, llama3.2-vision or llava"
EXAMPLES = [
    "200×100×12 mounting plate, 4× M8 clearance holes 15 mm from the edges, R5 corners",
    "Flanged bushing: Ø40 flange 6 thick, Ø25 body 30 long, Ø12 bore through",
    "Open-top box 120×80×50, 3 mm walls, R4 vertical edges",
    "Make it aluminum and add a 1 mm chamfer on the top edges",
]


class _Bridge(QObject):
    """Lives on the GUI thread; executes tool calls there."""
    request = Signal(str, object)

    def __init__(self, executor):
        super().__init__()
        self.executor = executor
        self.result = ("", False)

    @Slot(str, object)
    def run(self, name, args):
        self.result = self.executor.run(name, args)


class _ModelsSignal(QObject):
    ready = Signal(int, list, str, bool)


class _Worker(QThread):
    text = Signal(str)
    tool = Signal(str, str, str, bool)
    failed = Signal(str)
    progress = Signal(str)                            # what the model is doing now ('!…' = a tip for the chat)

    def __init__(self, copilot, prompt, state, bridge, images=None, documents=None):
        super().__init__()
        self.copilot, self.prompt, self.state, self.bridge = copilot, prompt, state, bridge
        self.images = images or []
        self.documents = documents or []
        self.killed = False

    def _call(self, name, args):
        if self.killed:                               # hard-stopped: never touch the model / inventory again
            return "Not run — stopped by the user.", True
        self.bridge.request.emit(name, args)          # blocks until GUI thread ran it
        return self.bridge.result

    def run(self):
        self.copilot.call_tool = self._call
        try:
            extra = {"on_status": self.progress.emit} if getattr(self.copilot, "supports_status", False) else {}
            self.copilot.ask(
                self.prompt, self.state,
                on_text=self.text.emit,
                on_tool=lambda n, a, r, e: self.tool.emit(n, json.dumps(a), r, e),
                images=self.images, documents=self.documents, **extra,
            )
        except Exception as e:  # noqa: BLE001
            if not self.killed:
                self.failed.emit(f"{e.__class__.__name__}: {e}")
            try:
                self.copilot.repair()                 # leave a history the next prompt can use
            except Exception:  # noqa: BLE001
                pass


class CopilotPanel(QWidget):
    def __init__(self, doc, parent=None, executor=None, tools=None, system=None, examples=None, welcome=None):
        """doc=None + executor/tools/system: a copilot for another host (SolidWorks Link)."""
        super().__init__(parent)
        self.doc = doc
        if executor is None:
            from ..ai.tools import ToolExecutor
            executor = ToolExecutor(doc)
        self.executor = executor
        self.custom_tools, self.custom_system = tools, system
        self.examples = examples or EXAMPLES
        self.welcome = welcome
        self.bridge = _Bridge(self.executor)
        self.bridge.request.connect(self.bridge.run, Qt.BlockingQueuedConnection)
        self.settings = QSettings("Daedalus", "Daedalus")
        self.copilot: Copilot | None = None
        self.worker: _Worker | None = None
        self._orphans: list = []                    # killed workers still unwinding in the background
        self._turn_checkpointed = False
        self._attached: list[tuple[str, bytes]] = []
        self._docs: list[dict] = []                 # spreadsheets / PDFs (ai.attachments.describe)
        self.setAcceptDrops(True)
        self.render_provider = None          # () -> [png bytes], set by the main window
        self._verify_rounds = 0

        from PySide6.QtCore import QSize

        from . import icons
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        mark = QLabel()
        try:
            from .splash import logo_pixmap
            mark.setPixmap(logo_pixmap(26, self.devicePixelRatioF() or 1.0))
        except Exception:  # noqa: BLE001
            pass
        name = QLabel("Icarus")
        name.setStyleSheet("font-size: 12pt; font-weight: 600;")
        self.tag = QLabel("AI copilot")
        self.tag.setObjectName("muted")
        head.addWidget(mark)
        head.addWidget(name)
        head.addWidget(self.tag, 1)
        lay.addLayout(head)
        top = QHBoxLayout()
        self.provider = QComboBox()
        for key, label in PROVIDERS:
            self.provider.addItem(label, key)
        self.provider.setCurrentIndex(max(0, self.provider.findData(self.settings.value("ai/provider", "ollama"))))
        self.provider.setToolTip("Which model runs Icarus")
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setMinimumWidth(60)
        self.model.currentTextChanged.connect(self._model_changed)
        self.cfg_btn = QPushButton("")
        self.cfg_btn.setIcon(icons.icon("key"))
        self.cfg_btn.clicked.connect(self.configure)
        new_btn = QPushButton("")
        new_btn.setToolTip("Start a new chat")
        new_btn.setIcon(icons.icon("new chat"))
        new_btn.clicked.connect(self.new_chat)
        top.addWidget(self.provider)
        top.addWidget(self.model, 1)
        top.addWidget(self.cfg_btn)
        top.addWidget(new_btn)
        lay.addLayout(top)

        self.log = QTextBrowser()
        self.log.setObjectName("chat")
        self.log.setStyleSheet(f"QTextBrowser#chat {{ background: {theme.BG}; border: none; padding: 2px; }}")
        self.log.setOpenLinks(False)
        self.log.anchorClicked.connect(self._example_clicked)
        lay.addWidget(self.log, 1)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Describe a part or a change…  Ctrl+Enter to send")
        self.input.setFixedHeight(84)
        self.input.installEventFilter(self)
        lay.addWidget(self.input)

        att = QHBoxLayout()
        self.attach_btn = QPushButton("Attach" if icons.available() else "📎 Attach")
        self.attach_btn.setIcon(icons.icon("attach"))
        self.attach_btn.setObjectName("ghost")
        self.attach_btn.setToolTip("Attach a picture (needs a vision model), an Excel / CSV sheet or a PDF — or drop "
                                   "files onto this panel")
        self.attach_btn.clicked.connect(self.attach_image)
        self.attach_lbl = QLabel("")
        self.attach_lbl.setObjectName("muted")
        self.verify_cb = QCheckBox("Check result against image")
        self.verify_cb.setToolTip("After building from an image, send renders of the model back so it can fix differences")
        self.verify_cb.setChecked(True)
        self.clear_att_btn = QPushButton("" if icons.available() else "✕")
        self.clear_att_btn.setIcon(icons.icon("close"))
        self.clear_att_btn.setObjectName("ghost")
        self.clear_att_btn.setFixedWidth(28)
        self.clear_att_btn.setToolTip("Remove attachments")
        self.clear_att_btn.clicked.connect(self.clear_attachments)
        self.clear_att_btn.hide()
        att.addWidget(self.attach_btn)
        att.addWidget(self.attach_lbl, 1)
        att.addWidget(self.clear_att_btn)
        att.addWidget(self.verify_cb)
        self.verify_cb.setVisible(doc is not None)        # only a 3D model can be rendered back for checking
        lay.addLayout(att)

        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setIcon(icons.icon("stop"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.kill_btn = QPushButton("Kill")
        self.kill_btn.setToolTip("Hard stop now: abandon the request in progress, run no more tools, and get the chat "
                                 "back (Esc twice)")
        self.kill_btn.setStyleSheet(f"QPushButton:enabled {{ color: {theme.ERROR}; font-weight: 600; }}")
        self.kill_btn.setEnabled(False)
        self.kill_btn.clicked.connect(self.kill)
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.setIcon(icons.icon("send", theme.ON_ACCENT, theme.ON_ACCENT))
        self.send_btn.setIconSize(QSize(14, 14))
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.status, 1)
        row.addWidget(self.stop_btn)
        row.addWidget(self.kill_btn)
        row.addWidget(self.send_btn)
        lay.addLayout(row)
        self._provider_changed(self.provider.currentIndex(), first=True)
        self._welcome()

    # ---------------------------------------------------------- helpers
    def _example_clicked(self, url):
        """Example prompt → input box, verbatim (looked up by index: URLs mangle '×', 'Ø', spaces)."""
        ref = url.toString()
        if ref.startswith("setup:"):
            from .setup_dialog import SetupDialog
            SetupDialog(self.window()).exec()
            self._provider_changed(self.provider.currentIndex())
            return
        if ref.startswith("ex:"):
            try:
                text = self.examples[int(ref[3:])]
            except (ValueError, IndexError):
                text = ref[3:]
            self.input.setPlainText(text)
            self.input.setFocus()
            c = self.input.textCursor()
            c.movePosition(c.MoveOperation.End)
            self.input.setTextCursor(c)

    def _welcome(self):
        ex = "".join(
            f'<table width="100%" cellpadding="8" cellspacing="0" style="margin:0 0 6px 0">'
            f'<tr><td bgcolor="{theme.PANEL_2}"><a href="ex:{i}" style="color:{theme.TEXT};'
            f'text-decoration:none">{html.escape(e)}</a></td></tr></table>' for i, e in enumerate(self.examples))
        title, sub = self.welcome or ("What should we build?",
                                      "Icarus ({who}) edits the feature history directly — each prompt is one "
                                      "undo step. Try one:")
        tail = (f'<p style="color:{theme.MUTED};margin:10px 0 0 0">Runs on your Claude subscription through Claude '
                f'Code, with the same tools as Claude Desktop. First time: run  claude  in a terminal and sign '
                f'in.</p>' if self.kind() == "claude_code" else
                f'<p style="color:{theme.MUTED};margin:10px 0 0 0">No answer? <a href="setup:" '
                f'style="color:{theme.ACCENT_TEXT}">Setup check</a> installs Ollama and the model.</p>')
        self.log.setHtml(
            f'<p style="font-size:11pt;font-weight:600;margin:4px 0 2px 0">{html.escape(title)}</p>'
            f'<p style="color:{theme.MUTED};margin:0 0 10px 0">{html.escape(sub.format(who=self.who()))}</p>{ex}'
            + tail)

    def _append(self, who, body, color=None):
        color = color or theme.TEXT
        mine = who == "You"
        bg = theme.RAISED if mine else theme.BG
        name = theme.MUTED if mine else (theme.ERROR if who == "Error" else theme.ACCENT_LINE)
        self.log.append(
            f'<table width="100%" cellpadding="{8 if mine else 2}" cellspacing="0" style="margin:8px 0 0 0">'
            f'<tr><td bgcolor="{bg}"><span style="color:{name};font-size:8.5pt;font-weight:600">{who}</span><br>'
            f'<span style="color:{color}">{body}</span></td></tr></table>')
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.input and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and ev.modifiers() & Qt.ControlModifier:
            self.send()
            return True
        if obj is self.input and ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Escape and self.busy():
            self.escape()
            return True
        return super().eventFilter(obj, ev)

    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def escape(self):
        """Esc: stop after this step; Esc again: kill."""
        if not self.busy():
            return
        if self.copilot is not None and getattr(self.copilot, "_stop", False):
            self.kill()
        else:
            self.stop()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.busy():
            self.escape()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------- provider
    def kind(self) -> str:
        return self.provider.currentData() or "ollama"

    def host(self) -> str:
        # the saved Server setting wins; OLLAMA_HOST is often the server's *bind* address ("0.0.0.0")
        return ollama.normalize_host(self.settings.value("ollama/host") or os.environ.get("OLLAMA_HOST")
                                     or ollama.DEFAULT_HOST)

    def who(self) -> str:
        m = self.model.currentText().strip()
        if self.kind() == "claude_code":
            return "Claude (your plan)"
        return (m or "Ollama") if self.kind() == "ollama" else "Claude"

    def _provider_changed(self, _i, first=False):
        kind = self.kind()
        self.settings.setValue("ai/provider", kind)
        self.model.blockSignals(True)
        self.model.clear()
        if kind == "ollama":
            self.cfg_btn.setToolTip("Ollama server, context size, tools")
            from . import icons
            self.cfg_btn.setIcon(icons.icon("gear-six"))
            saved = self.settings.value("ollama/model", ollama.DEFAULT_MODEL)
            self.model.addItems([saved])
            self.model.setCurrentText(saved)
            if self.model.lineEdit() is not None:
                self.model.lineEdit().setCursorPosition(0)
            self.model.setToolTip("Installed Ollama models (ollama pull <name> to add more)")
            self.tag.setText("AI copilot · local")
            self._list_models_async(saved, warn=not first)      # never blocks the window on a slow server
        elif kind == "claude_code":
            from ..ai import claude_code as CC
            from . import icons
            self.cfg_btn.setIcon(icons.icon("gear-six"))
            self.cfg_btn.setToolTip("Claude Code: where it is, sign-in check")
            self.tag.setText("AI copilot · Claude (your plan)")
            for key_, label in CC.MODELS:
                self.model.addItem(label, key_)
            self.model.setCurrentIndex(max(0, self.model.findData(self.settings.value("claude_code/model", ""))))
            if self.model.lineEdit() is not None:
                self.model.lineEdit().setCursorPosition(0)
            self.model.setToolTip("Runs on your Claude subscription through Claude Code — no API key. "
                                  "Same tools Claude Desktop uses.")
        else:
            from . import icons
            self.cfg_btn.setIcon(icons.icon("key"))
            self.cfg_btn.setToolTip("Anthropic API key (stored on this PC)")
            self.tag.setText("AI copilot · Claude API")
            self.model.addItems(MODELS)
            self.model.setCurrentText(self.settings.value("model", DEFAULT_MODEL))
            self.model.setToolTip("")
        self.model.blockSignals(False)
        if getattr(self, "_cp_sig", (kind,))[0] != kind:   # message formats differ: new chat
            self.copilot = None
        if not first:
            self._welcome()

    def _list_models_async(self, saved, warn=False):
        import threading
        host = self.host()
        if not hasattr(self, "_models_sig"):
            self._models_sig = _ModelsSignal(self)
            self._models_sig.ready.connect(self._models_listed)
        sig = self._models_sig
        self._models_req = req = getattr(self, "_models_req", 0) + 1

        def run():
            try:
                got = ollama.list_models(host, timeout=3)
            except Exception:  # noqa: BLE001
                got = []
            try:
                sig.ready.emit(req, got, saved, warn)
            except RuntimeError:                               # panel gone
                pass
        threading.Thread(target=run, daemon=True).start()

    def _models_listed(self, req, got, saved, warn):
        if req != getattr(self, "_models_req", 0) or self.kind() != "ollama":
            return
        if got:
            pick = saved if saved in got else (ollama.DEFAULT_MODEL if ollama.DEFAULT_MODEL in got else got[0])
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(got)
            self.model.setCurrentText(pick)
            self.model.blockSignals(False)
            if self.model.lineEdit() is not None:
                self.model.lineEdit().setCursorPosition(0)
            if pick != saved:
                self._model_changed(pick)
        elif warn:
            self._append("Icarus", f"Ollama isn't answering at {html.escape(self.host())}. Start it "
                                   "(ollama serve), set the address with the gear button, or "
                                   f'<a href="setup:" style="color:{theme.ACCENT_TEXT}">run the setup check</a> '
                                   "to install Ollama and the model.", theme.ERROR)

    def _model_changed(self, m):
        if not m:
            return
        le = self.model.lineEdit()
        if le is not None and not le.hasFocus():
            le.setCursorPosition(0)                         # show the model's name from its start
        if self.kind() == "claude_code":
            self.settings.setValue("claude_code/model", self.model.currentData() or "")
            return
        self.settings.setValue("ollama/model" if self.kind() == "ollama" else "model", m)

    def configure(self):
        if self.kind() == "ollama":
            OllamaSettings(self).exec()
            self._provider_changed(self.provider.currentIndex())
        elif self.kind() == "claude_code":
            ClaudeCodeSettings(self).exec()
        else:
            self.set_key()

    def api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or self.settings.value("api_key") or None

    def set_key(self):
        key, ok = QInputDialog.getText(self, "Anthropic API key",
                                       "Key (stored locally in app settings):", QLineEdit.Password)
        if ok and key.strip():
            self.settings.setValue("api_key", key.strip())
            self.copilot = None

    def new_chat(self):
        if self.copilot:
            self.copilot.reset()
        self._welcome()

    # ----------------------------------------------------------- images
    def attach_image(self):
        from ..ai.attachments import FILTER
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach", "", FILTER)
        for p in paths:
            self.attach_path(p)

    def attach_path(self, path: str) -> bool:
        """Picture → sent as an image; Excel / CSV / PDF → read into the prompt (PDFs also go to Claude whole)."""
        from ..ai import attachments as AT
        try:
            d = AT.describe(path)
        except Exception as e:  # noqa: BLE001
            self._append("Icarus", html.escape(f"Couldn't read {os.path.basename(path)}: {e}"), theme.ERROR)
            return False
        if d["kind"] == "image":
            self.attach_bytes(d["name"], d["data"])
        else:
            self._docs.append(d)
            self._show_attached()
        return True

    def attach_bytes(self, name: str, data: bytes):
        self._attached.append((name, data))
        self._show_attached()

    def _show_attached(self):
        names = [n for n, _ in self._attached] + [d["name"] for d in self._docs]
        self.attach_lbl.setText("Attached: " + " · ".join(names) if names else "")
        self.clear_att_btn.setVisible(bool(names))

    def clear_attachments(self):
        self._attached.clear()
        self._docs.clear()
        self._show_attached()

    def dragEnterEvent(self, e):
        from ..ai.attachments import kind_of
        if e.mimeData().hasUrls() and any(kind_of(u.toLocalFile()) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile():
                self.attach_path(u.toLocalFile())
        e.acceptProposedAction()

    def ask_with_image(self, prompt: str, png: bytes, label="doodle.png"):
        """Entry point for the Doodle page: prompt + image, then render check."""
        self.clear_attachments()
        self.attach_bytes(label, png)
        self.input.setPlainText(prompt)
        self.send()

    # ------------------------------------------------------------- run
    def _ensure_copilot(self, key=None):
        m = self.model.currentText().strip()
        if self.kind() == "claude_code":
            m = self.model.currentData() or ""
            cli = key
            want = (self.kind(), m, cli)
            if self.copilot is None or getattr(self, "_cp_sig", None) != want:
                from ..ai.claude_code import ClaudeCodeCopilot
                sid = getattr(self.copilot, "session_id", None)      # changing model keeps the conversation
                self.copilot = ClaudeCodeCopilot(cli, model=m, system=self.custom_system)
                self.copilot.session_id = sid
                self._cp_sig = want
            return
        want = (self.kind(), m)
        if self.copilot is None or getattr(self, "_cp_sig", None) != want:
            old = self.copilot.messages if self.copilot else []
            if self.kind() == "ollama":
                self.copilot = ollama.OllamaCopilot(
                    call_tool=lambda *_: ("", True), host=self.host(), model=m,
                    num_ctx=int(self.settings.value("ollama/num_ctx", ollama.DEFAULT_CTX)),
                    tool_set=self.settings.value("ollama/tools", "core"),
                    tools=self.custom_tools, system=self.custom_system)
                old = old if old and isinstance(old[0].get("content"), str) else []
            else:
                self.copilot = Copilot(call_tool=lambda *_: ("", True), api_key=key, model=m,
                                       tools=self.custom_tools, system=self.custom_system)
                old = old if old and not isinstance(old[0].get("content"), str) else []
            self.copilot.messages = old
            self._cp_sig = want

    def _start(self, prompt, images, show=True, documents=None):
        self.worker = _Worker(self.copilot, prompt, self.executor.model_state(), self.bridge, images, documents)
        who = f"Icarus · {self.who()}"
        self.worker.text.connect(lambda t: self._append(who, html.escape(t).replace("\n", "<br>")))
        self.worker.tool.connect(self._on_tool)
        self.worker.failed.connect(lambda e: self._append("Error", html.escape(e), theme.ERROR))
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._done)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.kill_btn.setEnabled(True)
        self._phase, self._t0 = "Working…", time.monotonic()
        if not hasattr(self, "_tick"):
            self._tick = QTimer(self)
            self._tick.setInterval(1000)
            self._tick.timeout.connect(self._show_phase)
        self._tick.start()
        self._show_phase()
        self.worker.start()

    def _on_progress(self, text):
        if text.startswith("!"):                      # a tip, e.g. the model doesn't fit on the graphics card
            self._append("Icarus", html.escape(text[1:]), theme.WARN)
            return
        self._phase = text
        self._show_phase()

    def _show_phase(self):
        """'Waiting for qwen2.5:14b … · 42 s' — so a slow local model doesn't look like a frozen one."""
        if self.worker is None or not self.worker.isRunning():
            if hasattr(self, "_tick"):
                self._tick.stop()
            return
        secs = int(time.monotonic() - getattr(self, "_t0", time.monotonic()))
        self.status.setText(f"{self._phase} · {secs} s" if secs >= 2 else self._phase)

    def send(self):
        prompt = self.input.toPlainText().strip()
        if not prompt or (self.worker and self.worker.isRunning()):
            return
        key = None
        if self.kind() == "claude":
            key = self.api_key()
            if not key:
                self._append("Icarus", "Set an Anthropic API key first (key button or "
                                       "ANTHROPIC_API_KEY env var), or switch to Ollama.", theme.ERROR)
                return
        elif self.kind() == "claude_code":
            from ..ai import claude_code as CC
            key = CC.find_cli(self.settings.value("claude_code/path") or None)
            if not key:
                self._append("Icarus", "Claude Code isn't installed on this PC. Install it (claude.com/claude-code), "
                                       "run  claude  once and sign in with your Claude account — or set where it "
                                       "is with the gear button.", theme.ERROR)
                return
            if self._link() is None:
                self._append("Icarus", "The Claude link is off, so Claude can't use this window's tools. Turn it on "
                                       "in Settings ▸ Claude Desktop, then restart Daedalus.", theme.ERROR)
                return
        elif not self.model.currentText().strip():
            self._append("Icarus", "Pick an Ollama model (ollama pull qwen2.5:14b).", theme.ERROR)
            return
        if self.kind() == "ollama" and self._attached:
            caps = ollama.capabilities(self.host(), self.model.currentText().strip())
            if caps and "vision" not in caps:
                self._append("Icarus", f"{html.escape(self.model.currentText())} can't see images — pick a "
                                         f"vision model ({VISION_HINT}).", theme.ERROR)
                return
        self._ensure_copilot(key)
        self.input.clear()
        images = [d for _, d in self._attached]
        names = [n for n, _ in self._attached] + [d["name"] for d in self._docs]
        docs = list(self._docs)
        self.clear_attachments()
        pdfs = []
        if docs:
            from ..ai.attachments import prompt_block
            att = getattr(self.executor, "attachments", None)
            if isinstance(att, dict):
                att.update({d["name"]: d["path"] for d in docs})
            blocks = []
            for d in docs:
                if d["kind"] == "pdf" and self.kind() in ("claude", "claude_code") and d["data"] and len(d["data"]) < 30_000_000:
                    pdfs.append(d["data"])                 # Claude reads the PDF itself (scans, drawings too)
                    d = {**d, "text": "(sent as a PDF document above)"}
                blocks.append(prompt_block(d))
            prompt = "\n\n".join(blocks) + "\n\n" + prompt
        self._append("You", html.escape(prompt) + (f'<br><span style="color:{theme.MUTED}">Attached: '
                                                    f'{html.escape(", ".join(names))}</span>' if names else ""))
        if self.doc is not None:
            self.doc.checkpoint()             # whole turn (incl. render check) = one undo step
        link = self._link() if self.kind() == "claude_code" else None
        if link is not None:                  # Claude's calls arrive through the link: same undo step, Icarus label
            link.via = "Icarus · Claude"
        self._verify_rounds = 1 if images and self.verify_cb.isChecked() and self.render_provider else 0
        self.status.setText("")
        self._start(prompt, images, documents=pdfs)

    def _on_tool(self, name, args, result, err):
        try:
            a = json.loads(args)
            label = a.get("name") or a.get("sketch") or a.get("feature") or ""
        except ValueError:
            label = ""
        mark, col = ("✗", theme.ERROR) if err else ("✓", theme.OK)
        tip = html.escape(result[:300])
        self.log.append(f'<p style="margin:1px 0 1px 10px;color:{theme.MUTED}" title="{tip}">'
                        f'<span style="color:{col}">{mark}</span> {html.escape(name)} '
                        f'{html.escape(str(label))}</p>')

    def stop(self):
        if self.copilot:
            self.copilot.stop()
        self.status.setText("Stopping after current step… (Kill to stop now)")

    def kill(self):
        """Hard stop: the chat is usable again immediately. The request in flight is abandoned (Claude's connection
        is closed; a local model's answer is ignored when it arrives) and no further tool runs."""
        w = self.worker
        self._verify_rounds = 0
        if w is not None and w.isRunning():
            w.killed = True
            for sig in (w.text, w.tool, w.failed, w.finished, w.progress):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            self._orphans.append(w)                   # keep the QThread alive until it unwinds
            w.finished.connect(lambda w=w: self._orphans.remove(w) if w in self._orphans else None)
        if self.copilot is not None:
            try:
                kill = getattr(self.copilot, "kill", None)
                (kill or self.copilot.stop)()
            except Exception:  # noqa: BLE001
                pass
        self.worker = None
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.kill_btn.setEnabled(False)
        self._release_link()
        self.status.setText("Killed")
        self._append("Icarus", "Stopped. Anything already done stays (Ctrl+Z to undo a model change).", theme.MUTED)

    def _link(self):
        """The window's Claude link tool server (what Claude Desktop and 'Claude (your plan)' both use)."""
        w = self.window()
        b = getattr(w, "claude_bridge", None) if w is not None else None
        return b if b is not None and getattr(b, "server", None) is not None else None

    def _release_link(self):
        b = self._link()
        if b is not None and b.via != "Claude Desktop":
            b.via = "Claude Desktop"
            b._last = time.time()             # Claude Desktop's next request gets its own undo step

    def _done(self):
        if hasattr(self, "_tick"):
            self._tick.stop()
        self._release_link()
        stopped = self.copilot is not None and self.copilot._stop
        if self._verify_rounds > 0 and not stopped and self.doc is not None and self.doc.result.body is not None:
            self._verify_rounds -= 1
            try:
                renders = self.render_provider()
            except Exception as e:  # noqa: BLE001
                renders = []
                self._append("Icarus", f"Couldn't render for the check: {html.escape(str(e))}", theme.ERROR)
            if renders:
                self._append("Icarus", "Checking the result against the drawing…", theme.MUTED)
                self._start("Here are renders of the model you just built (isometric, then top view). "
                            "Compare them with the original drawing and fix any differences with tools. "
                            "If it already matches, say so in one sentence.", renders)
                return
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.kill_btn.setEnabled(False)
        if self.doc is None:
            self.status.setText("Done")
            return
        r = self.doc.result
        self.status.setText(f"{len(r.errors)} errors" if r.errors else "Done")


class ClaudeCodeSettings(QDialog):
    """Where the claude command is, and a check that it runs and is signed in."""

    def __init__(self, panel):
        super().__init__(panel)
        from ..ai import claude_code as CC
        self.panel, self.CC = panel, CC
        self.setWindowTitle("Claude (your plan)")
        v = QVBoxLayout(self)
        about = QLabel("Icarus sends your prompt to Claude through Claude Code, signed in with your Claude account "
                       "(Pro / Max / Team plan, no API key). Claude uses the same Daedalus tools as Claude Desktop, "
                       "and Claude Desktop keeps working as before.")
        about.setWordWrap(True)
        v.addWidget(about)
        form = QFormLayout()
        self.path = QLineEdit(panel.settings.value("claude_code/path") or "")
        self.path.setPlaceholderText(CC.find_cli() or "claude (not found — install Claude Code)")
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        form.addRow("Claude Code", row)
        v.addLayout(form)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        tip = QLabel("First time: install Claude Code, open a terminal, run  claude  and sign in. "
                     "Turn the Claude link on in Settings ▸ Claude Desktop.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        v.addWidget(tip)
        btns = QHBoxLayout()
        test = QPushButton("Test")
        test.clicked.connect(self.test)
        btns.addWidget(test)
        btns.addStretch(1)
        ok = QPushButton("Save")
        ok.setObjectName("primary")
        ok.clicked.connect(self.save)
        btns.addWidget(ok)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        v.addLayout(btns)
        self.setMinimumWidth(560)
        self.resize(560, 320)

    def browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Claude Code", "", "claude (claude claude.exe claude.cmd);;All (*)")
        if p:
            self.path.setText(p)

    def test(self):
        cli = self.CC.find_cli(self.path.text().strip() or None)
        if not cli:
            self.info.setText("Claude Code wasn't found. Install it from claude.com/claude-code or browse to it.")
            return
        try:
            ver = self.CC.version(cli)
        except Exception as e:  # noqa: BLE001
            self.info.setText(f"{cli} didn't run: {e}")
            return
        link = "on" if self.panel._link() is not None else "OFF — turn it on in Settings ▸ Claude Desktop"
        self.info.setText(f"Found {cli}\nVersion {ver}\nClaude link: {link}\n"
                          "Sign-in is checked on your first prompt.")
        self.adjustSize()

    def save(self):
        self.panel.settings.setValue("claude_code/path", self.path.text().strip())
        self.panel.copilot = None if self.panel.kind() == "claude_code" else self.panel.copilot
        self.accept()


class OllamaSettings(QDialog):
    """Server address, context size, tool set, connection test."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        st = panel.settings
        self.setWindowTitle("Ollama")
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.host = QLineEdit(st.value("ollama/host") or ollama.DEFAULT_HOST)
        self.host.setPlaceholderText(ollama.DEFAULT_HOST)
        form.addRow("Server", self.host)
        self.ctx = QComboBox()
        for n in (8192, 16384, 32768, 65536, 131072):
            self.ctx.addItem(f"{n // 1024}k tokens", n)
        cur = int(st.value("ollama/num_ctx", ollama.DEFAULT_CTX))
        self.ctx.setCurrentIndex(max(0, self.ctx.findData(cur)))
        self.ctx.setToolTip("Context window. The instructions + tools need ~12k (core) or ~25k (all); "
                            "bigger uses more VRAM")
        form.addRow("Context", self.ctx)
        self.tools = QComboBox()
        self.tools.addItem(f"Core ({len(ollama.CORE_TOOLS)} tools: model, assemble, import, BOM)", "core")
        self.tools.addItem("All tools (simulation, drawings, CAM, inventory…)", "all")
        self.tools.setCurrentIndex(0 if st.value("ollama/tools", "core") == "core" else 1)
        form.addRow("Tools", self.tools)
        v.addLayout(form)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        tip = QLabel("Good local models for tool use: qwen2.5 (14b+), qwen3 (14b/32b), llama3.1/3.3, mistral-small. "
                     f"For drawings/photos use a vision model: {VISION_HINT}. Install with  ollama pull <name>.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        v.addWidget(tip)
        row = QHBoxLayout()
        test = QPushButton("Test connection")
        test.clicked.connect(self.test)
        row.addWidget(test)
        row.addStretch(1)
        ok = QPushButton("Save")
        ok.setObjectName("primary")
        ok.clicked.connect(self.save)
        row.addWidget(ok)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        v.addLayout(row)
        self.resize(520, 260)

    def test(self):
        host = ollama.normalize_host(self.host.text())
        got = ollama.list_models(host, timeout=3.0)
        if not got:
            self.info.setText(f"No answer from {host}. Is Ollama running (ollama serve)?")
            return
        lines = []
        for m in got[:12]:
            caps = ollama.capabilities(host, m, timeout=3.0)
            tags = ", ".join(c for c in ("tools", "vision", "thinking") if c in caps)
            lines.append(f"{m}" + (f"  ({tags})" if tags else ""))
        self.info.setText(f"Connected · {len(got)} models:\n" + "\n".join(lines))

    def save(self):
        st = self.panel.settings
        st.setValue("ollama/host", ollama.normalize_host(self.host.text()))
        st.setValue("ollama/num_ctx", int(self.ctx.currentData()))
        st.setValue("ollama/tools", self.tools.currentData())
        self.panel.copilot = None
        self.accept()
