"""Copilot panel: chat with a local Ollama model (default) or Claude, which model through tool calls.

Threading: the agent loop runs on a QThread. Tool calls are marshalled to
the GUI thread with a BlockingQueuedConnection so the Document is only ever
touched from one thread. One prompt = one undo step.
"""
from __future__ import annotations

import html
import json
import os

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..ai import ollama
from ..ai.copilot import DEFAULT_MODEL, Copilot
from ..ai.tools import ToolExecutor
from . import theme

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
PROVIDERS = [("ollama", "Ollama (local)"), ("claude", "Claude (API)")]
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

    def __init__(self, copilot, prompt, state, bridge, images=None):
        super().__init__()
        self.copilot, self.prompt, self.state, self.bridge = copilot, prompt, state, bridge
        self.images = images or []

    def _call(self, name, args):
        self.bridge.request.emit(name, args)          # blocks until GUI thread ran it
        return self.bridge.result

    def run(self):
        self.copilot.call_tool = self._call
        try:
            self.copilot.ask(
                self.prompt, self.state,
                on_text=self.text.emit,
                on_tool=lambda n, a, r, e: self.tool.emit(n, json.dumps(a), r, e),
                images=self.images,
            )
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e.__class__.__name__}: {e}")


class CopilotPanel(QWidget):
    def __init__(self, doc, parent=None, executor=None, tools=None, system=None, examples=None, welcome=None):
        """doc=None + executor/tools/system: a copilot for another host (SolidWorks Link)."""
        super().__init__(parent)
        self.doc = doc
        self.executor = executor or ToolExecutor(doc)
        self.custom_tools, self.custom_system = tools, system
        self.examples = examples or EXAMPLES
        self.welcome = welcome
        self.bridge = _Bridge(self.executor)
        self.bridge.request.connect(self.bridge.run, Qt.BlockingQueuedConnection)
        self.settings = QSettings("Daedalus", "Daedalus")
        self.copilot: Copilot | None = None
        self.worker: _Worker | None = None
        self._turn_checkpointed = False
        self._attached: list[tuple[str, bytes]] = []
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
        self.provider.setCurrentIndex(0 if self.settings.value("ai/provider", "ollama") == "ollama" else 1)
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
        self.attach_btn = QPushButton("Image" if icons.available() else "📎 Image")
        self.attach_btn.setIcon(icons.icon("attach"))
        self.attach_btn.setObjectName("ghost")
        self.attach_btn.setToolTip("Attach a photo / drawing / screenshot to model from (needs a vision model)")
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
        self.clear_att_btn.setToolTip("Remove attached images")
        self.clear_att_btn.clicked.connect(self.clear_attachments)
        self.clear_att_btn.hide()
        att.addWidget(self.attach_btn)
        att.addWidget(self.attach_lbl, 1)
        att.addWidget(self.clear_att_btn)
        att.addWidget(self.verify_cb)
        lay.addLayout(att)

        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setIcon(icons.icon("stop"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.setIcon(icons.icon("send", theme.ON_ACCENT, theme.ON_ACCENT))
        self.send_btn.setIconSize(QSize(14, 14))
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.status, 1)
        row.addWidget(self.stop_btn)
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
        self.log.setHtml(
            f'<p style="font-size:11pt;font-weight:600;margin:4px 0 2px 0">{html.escape(title)}</p>'
            f'<p style="color:{theme.MUTED};margin:0 0 10px 0">{html.escape(sub.format(who=self.who()))}</p>{ex}'
            f'<p style="color:{theme.MUTED};margin:10px 0 0 0">No answer? <a href="setup:" '
            f'style="color:{theme.ACCENT_TEXT}">Setup check</a> installs Ollama and the model.</p>')

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
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------- provider
    def kind(self) -> str:
        return self.provider.currentData() or "ollama"

    def host(self) -> str:
        # the saved Server setting wins; OLLAMA_HOST is often the server's *bind* address ("0.0.0.0")
        return ollama.normalize_host(self.settings.value("ollama/host") or os.environ.get("OLLAMA_HOST")
                                     or ollama.DEFAULT_HOST)

    def who(self) -> str:
        m = self.model.currentText().strip()
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
        self.settings.setValue("ollama/model" if self.kind() == "ollama" else "model", m)

    def configure(self):
        if self.kind() == "ollama":
            OllamaSettings(self).exec()
            self._provider_changed(self.provider.currentIndex())
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
        path, _ = QFileDialog.getOpenFileName(self, "Attach image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            with open(path, "rb") as f:
                self.attach_bytes(os.path.basename(path), f.read())

    def attach_bytes(self, name: str, data: bytes):
        self._attached.append((name, data))
        self.attach_lbl.setText("Attached: " + " · ".join(n for n, _ in self._attached))
        self.clear_att_btn.show()

    def clear_attachments(self):
        self._attached.clear()
        self.attach_lbl.setText("")
        self.clear_att_btn.hide()

    def ask_with_image(self, prompt: str, png: bytes, label="doodle.png"):
        """Entry point for the Doodle page: prompt + image, then render check."""
        self.clear_attachments()
        self.attach_bytes(label, png)
        self.input.setPlainText(prompt)
        self.send()

    # ------------------------------------------------------------- run
    def _ensure_copilot(self, key=None):
        m = self.model.currentText().strip()
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

    def _start(self, prompt, images, show=True):
        self.worker = _Worker(self.copilot, prompt, self.executor.model_state(), self.bridge, images)
        who = f"Icarus · {self.who()}"
        self.worker.text.connect(lambda t: self._append(who, html.escape(t).replace("\n", "<br>")))
        self.worker.tool.connect(self._on_tool)
        self.worker.failed.connect(lambda e: self._append("Error", html.escape(e), theme.ERROR))
        self.worker.finished.connect(self._done)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Working…")
        self.worker.start()

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
        names = [n for n, _ in self._attached]
        self.clear_attachments()
        self._append("You", html.escape(prompt) + (f'<br><span style="color:{theme.MUTED}">Attached: '
                                                    f'{html.escape(", ".join(names))}</span>' if names else ""))
        if self.doc is not None:
            self.doc.checkpoint()             # whole turn (incl. render check) = one undo step
        self._verify_rounds = 1 if images and self.verify_cb.isChecked() and self.render_provider else 0
        self.status.setText("")
        self._start(prompt, images)

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
        self.status.setText("Stopping after current step…")

    def _done(self):
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
        if self.doc is None:
            self.status.setText("Done")
            return
        r = self.doc.result
        self.status.setText(f"{len(r.errors)} errors" if r.errors else "Done")


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
