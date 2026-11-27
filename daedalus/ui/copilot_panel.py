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
PROVIDERS = [("ollama", "Ollama (local)"), ("openai", "OpenAI-compatible (GPT, LM Studio…)"),
             ("claude", "Claude (API)"), ("claude_code", "Claude (your plan)"), ("codex", "ChatGPT (your plan)"),
             ("gemini_cli", "Gemini (Google account)")]
# agent engines: module, class, install hint, sign-in hint
AGENTS = {
    "claude_code": ("claude_code", "ClaudeCodeCopilot", "Install Claude Code (claude.com/claude-code)",
                    "run  claude  once and sign in with your Claude account"),
    "codex": ("codex_cli", "CodexCopilot", "Install Codex (npm i -g @openai/codex, or from the ChatGPT desktop app)",
              "run  codex login  and sign in with your ChatGPT account"),
    "gemini_cli": ("gemini_cli", "GeminiCopilot", "Install Gemini CLI (npm i -g @google/gemini-cli)",
                   "run  gemini  once and choose Sign in with Google"),
}


def agent_module(kind):
    import importlib
    return importlib.import_module(f"daedalus.ai.{AGENTS[kind][0]}")
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
        self.provider.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.provider.setMinimumContentsLength(13)
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
        tail = (f'<p style="color:{theme.MUTED};margin:10px 0 0 0">Runs on your own account through '
                f'{html.escape(getattr(agent_module(self.kind()), AGENTS[self.kind()][1]).NAME)}, with the same '
                f'tools as Claude Desktop. First time: {html.escape(AGENTS[self.kind()][3])}.</p>'
                if self.kind() in AGENTS else
                f'<p style="color:{theme.MUTED};margin:10px 0 0 0">Runs on {html.escape(self._oa()[1])} '
                f'({html.escape(self._oa_url())}). The gear button sets the server, key and tools.</p>'
                if self.kind() == "openai" else
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
        k = self.kind()
        if k in AGENTS:
            return dict(PROVIDERS)[k]
        if k == "openai":
            return m or self._oa()[1]
        return (m or "Ollama") if k == "ollama" else "Claude"

    # OpenAI-compatible: the chosen server
    def _oa(self):
        from ..ai import openai_compat as OA
        return OA.PRESET.get(self.settings.value("openai/preset", "openai"), OA.PRESET["openai"])

    def _oa_url(self) -> str:
        p = self._oa()
        return (self.settings.value(f"openai/{p[0]}/url") or p[2]).rstrip("/")

    def _oa_key(self) -> str | None:
        p = self._oa()
        env = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY",
               "mistral": "MISTRAL_API_KEY", "gemini": "GEMINI_API_KEY", "xai": "XAI_API_KEY",
               "deepseek": "DEEPSEEK_API_KEY", "together": "TOGETHER_API_KEY"}.get(p[0])
        return self.settings.value(f"openai/{p[0]}/key") or (os.environ.get(env) if env else None) or None

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
        elif kind in AGENTS:
            M = agent_module(kind)
            cls = getattr(M, AGENTS[kind][1])
            from . import icons
            self.cfg_btn.setIcon(icons.icon("gear-six"))
            self.cfg_btn.setToolTip(f"{cls.NAME}: where it is, version, sign-in")
            self.tag.setText(f"AI copilot · {cls.LABEL}")
            for key_, label in M.MODELS:
                self.model.addItem(label, key_)
            saved = self.settings.value(f"{kind}/model", "")
            i = self.model.findData(saved)
            if i < 0 and saved:
                self.model.addItem(saved, saved)
                i = self.model.count() - 1
            self.model.setCurrentIndex(max(0, i))
            if self.model.lineEdit() is not None:
                self.model.lineEdit().setCursorPosition(0)
            self.model.setToolTip(f"Runs on {cls.LABEL.split('(')[-1].rstrip(')')} through {cls.NAME} — no API "
                                  "key. Same tools Claude Desktop uses. Type any model name the agent knows.")
        elif kind == "openai":
            from . import icons
            p = self._oa()
            self.cfg_btn.setIcon(icons.icon("gear-six"))
            self.cfg_btn.setToolTip("Server (OpenAI, LM Studio, Jan, OpenRouter…), API key, tools")
            self.tag.setText(f"AI copilot · {p[1]}")
            saved = self.settings.value(f"openai/{p[0]}/model") or p[4]
            if saved:
                self.model.addItem(saved)
                self.model.setCurrentText(saved)
            self.model.setToolTip(f"Models on {self._oa_url() or 'the server'} (type any name)")
            self._list_models_async(saved, warn=False, kind="openai")
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

    def _list_models_async(self, saved, warn=False, kind="ollama"):
        import threading
        host = self.host()
        if kind == "openai":
            from ..ai import openai_compat as OA
            url, key = self._oa_url(), self._oa_key()
            fetch = lambda: OA.list_models(url, key, timeout=6)  # noqa: E731
        else:
            fetch = lambda: ollama.list_models(host, timeout=3)  # noqa: E731
        self._models_kind = kind
        if not hasattr(self, "_models_sig"):
            self._models_sig = _ModelsSignal(self)
            self._models_sig.ready.connect(self._models_listed)
        sig = self._models_sig
        self._models_req = req = getattr(self, "_models_req", 0) + 1

        def run():
            try:
                got = fetch()
            except Exception:  # noqa: BLE001
                got = []
            try:
                sig.ready.emit(req, got, saved, warn)
            except RuntimeError:                               # panel gone
                pass
        threading.Thread(target=run, daemon=True).start()

    def _models_listed(self, req, got, saved, warn):
        if req != getattr(self, "_models_req", 0) or self.kind() != getattr(self, "_models_kind", "ollama"):
            return
        if got and self.kind() == "openai":
            pick = saved if saved and (saved in got or not got) else (saved or got[0])
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(([pick] if pick not in got else []) + got)
            self.model.setCurrentText(pick)
            self.model.blockSignals(False)
            if pick != saved:
                self._model_changed(pick)
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
        if self.kind() in AGENTS:
            i = self.model.findText(m)
            self.settings.setValue(f"{self.kind()}/model", (self.model.itemData(i) if i >= 0 else m.strip()) or "")
            return
        if self.kind() == "openai":
            self.settings.setValue(f"openai/{self._oa()[0]}/model", m.strip())
            return
        self.settings.setValue("ollama/model" if self.kind() == "ollama" else "model", m)

    def configure(self):
        if self.kind() == "ollama":
            OllamaSettings(self).exec()
            self._provider_changed(self.provider.currentIndex())
        elif self.kind() in AGENTS:
            AgentSettings(self, self.kind()).exec()
        elif self.kind() == "openai":
            if OpenAISettings(self).exec():
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
        if self.kind() in AGENTS:
            m = self.settings.value(f"{self.kind()}/model", "") or ""
            cli = key
            want = (self.kind(), m, cli)
            if self.copilot is None or getattr(self, "_cp_sig", None) != want:
                cls = getattr(agent_module(self.kind()), AGENTS[self.kind()][1])
                same = getattr(self, "_cp_sig", (None,))[0] == self.kind()
                sid = getattr(self.copilot, "session_id", None) if same else None   # a model change keeps the chat
                self.copilot = cls(cli, model=m, system=self.custom_system)
                self.copilot.session_id = sid
                self._cp_sig = want
            return
        if self.kind() == "openai":
            from ..ai import openai_compat as OA
            p = self._oa()
            want = (self.kind(), p[0], self._oa_url(), m, self.settings.value("openai/tools", ""),
                    self.settings.value("openai/tool_mode", "auto"), bool(key))
            if self.copilot is None or getattr(self, "_cp_sig", None) != want:
                old = self.copilot.messages if (self.copilot is not None and
                                                getattr(self, "_cp_sig", (None,))[0] == "openai") else []
                temp = self.settings.value("openai/temperature", "")
                self.copilot = OA.OpenAICopilot(
                    lambda *_: ("", True), self._oa_url(), key, m,
                    tool_set=self.settings.value("openai/tools", "") or ("core" if p[5] else "model"),
                    tool_mode=self.settings.value("openai/tool_mode", "auto"), tools=self.custom_tools,
                    system=self.custom_system, label=p[1],
                    temperature=float(temp) if temp not in ("", None) else (0.2 if p[5] else None))
                self.copilot.messages = old
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
                    tools=self.custom_tools, system=self.custom_system,
                    tool_mode=self.settings.value("ollama/tool_mode", "auto"),
                    think=self.settings.value("ollama/think", ""))
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
        elif self.kind() in AGENTS:
            M = agent_module(self.kind())
            cls = getattr(M, AGENTS[self.kind()][1])
            key = M.find_cli(self.settings.value(f"{self.kind()}/path") or None)
            if not key:
                self._append("Icarus", f"{cls.NAME} isn't installed on this PC. {AGENTS[self.kind()][2]}, then "
                                       f"{AGENTS[self.kind()][3]} — or set where it is with the gear button.",
                             theme.ERROR)
                return
            if self._link() is None:
                self._append("Icarus", f"The Claude link is off, so {cls.NAME} can't use this window's tools. Turn it "
                                       "on in Settings ▸ Claude Desktop, then restart Daedalus.", theme.ERROR)
                return
        elif self.kind() == "openai":
            p = self._oa()
            key = self._oa_key()
            if not self._oa_url():
                self._append("Icarus", "Set the server address with the gear button.", theme.ERROR)
                return
            if p[3] and not key:
                self._append("Icarus", f"{p[1]} needs an API key — set it with the gear button.", theme.ERROR)
                return
            if not self.model.currentText().strip():
                self._append("Icarus", f"Pick a model for {p[1]} (model box) — for a local server, load one in "
                                       "the app first.", theme.ERROR)
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
                if d["kind"] == "pdf" and self.kind() in ("claude", *AGENTS) and d["data"] and len(d["data"]) < 30_000_000:
                    pdfs.append(d["data"])                 # Claude reads the PDF itself (scans, drawings too)
                    d = {**d, "text": "(sent as a PDF document above)"}
                blocks.append(prompt_block(d))
            prompt = "\n\n".join(blocks) + "\n\n" + prompt
        self._append("You", html.escape(prompt) + (f'<br><span style="color:{theme.MUTED}">Attached: '
                                                    f'{html.escape(", ".join(names))}</span>' if names else ""))
        if self.doc is not None:
            self.doc.checkpoint()             # whole turn (incl. render check) = one undo step
        link = self._link() if self.kind() in AGENTS else None
        if link is not None:                  # Claude's calls arrive through the link: same undo step, Icarus label
            link.via = f"Icarus · {self.who()}"
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


class AgentSettings(QDialog):
    """Where the agent's command is (claude / codex / gemini), and a check that it runs."""

    def __init__(self, panel, kind):
        super().__init__(panel)
        self.panel, self.kind = panel, kind
        self.M = agent_module(kind)
        cls = getattr(self.M, AGENTS[kind][1])
        self.cls = cls
        self.setWindowTitle(cls.LABEL)
        v = QVBoxLayout(self)
        about = QLabel(f"Icarus sends your prompt to {cls.NAME}, signed in with your own account — no API key. It "
                       "uses the same Daedalus tools as Claude Desktop, through the Claude link, and the desktop apps "
                       "keep working alongside.")
        about.setWordWrap(True)
        v.addWidget(about)
        form = QFormLayout()
        self.path = QLineEdit(panel.settings.value(f"{kind}/path") or "")
        self.path.setPlaceholderText(self.M.find_cli() or f"{cls.NAME} not found — install it")
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        form.addRow(cls.NAME, row)
        v.addLayout(form)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        tip = QLabel(f"First time: {AGENTS[kind][2]}, then {AGENTS[kind][3]}. Keep the Claude link on "
                     "(Settings ▸ Claude Desktop).")
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
        p, _ = QFileDialog.getOpenFileName(self, self.cls.NAME, "", "Programs (*);;All (*)")
        if p:
            self.path.setText(p)

    def test(self):
        cli = self.M.find_cli(self.path.text().strip() or None)
        if not cli:
            self.info.setText(f"{self.cls.NAME} wasn't found. {AGENTS[self.kind][2]}, or browse to it.")
            return
        try:
            ver = self.M.version(cli)
        except Exception as e:  # noqa: BLE001
            self.info.setText(f"{cli} didn't run: {e}")
            return
        link = "on" if self.panel._link() is not None else "OFF — turn it on in Settings ▸ Claude Desktop"
        self.info.setText(f"Found {cli}\nVersion {ver}\nClaude link: {link}\n"
                          "Sign-in is checked on your first prompt.")
        self.adjustSize()

    def save(self):
        self.panel.settings.setValue(f"{self.kind}/path", self.path.text().strip())
        if self.panel.kind() == self.kind:
            self.panel.copilot = None
        self.accept()


ClaudeCodeSettings = lambda panel: AgentSettings(panel, "claude_code")  # noqa: E731


class OpenAISettings(QDialog):
    """Which OpenAI-compatible server (OpenAI, LM Studio, Jan, OpenRouter…), its address and key, and the tools."""

    def __init__(self, panel):
        super().__init__(panel)
        from ..ai import openai_compat as OA
        from ..ai import toolsets as TS
        self.panel, self.OA = panel, OA
        st = panel.settings
        self.setWindowTitle("OpenAI-compatible server")
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.preset = QComboBox()
        for p in OA.PRESETS:
            self.preset.addItem(p[1] + ("  · this PC" if p[5] else ""), p[0])
        self.preset.setCurrentIndex(max(0, self.preset.findData(st.value("openai/preset", "openai"))))
        self.preset.currentIndexChanged.connect(self._preset)
        form.addRow("Server", self.preset)
        self.url = QLineEdit()
        form.addRow("Address", self.url)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        form.addRow("API key", self.key)
        self.tools = QComboBox()
        self.tools.addItem("Automatic (core on this PC, modelling on the cloud)", "")
        for k, label in TS.SETS:
            self.tools.addItem(label, k)
        self.tools.setCurrentIndex(max(0, self.tools.findData(st.value("openai/tools", ""))))
        form.addRow("Tools", self.tools)
        self.mode = QComboBox()
        for k, label in (("auto", "Automatic (native, or described in the prompt if the model can't)"),
                         ("native", "Native function calling"), ("prompted", "Described in the prompt (any model)")):
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(st.value("openai/tool_mode", "auto"))))
        form.addRow("Tool calls", self.mode)
        self.temp = QLineEdit(str(st.value("openai/temperature", "") or ""))
        self.temp.setPlaceholderText("model default (0.2 on this PC)")
        form.addRow("Temperature", self.temp)
        v.addLayout(form)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        tip = QLabel("LM Studio: Developer tab ▸ Start server. Jan: Settings ▸ Local API Server. llama.cpp: "
                     "llama-server --jinja. OpenAI / OpenRouter / Groq / Mistral / Gemini / xAI / DeepSeek: paste the "
                     "key from their website. Keys stay on this PC.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        v.addWidget(tip)
        btns = QHBoxLayout()
        test = QPushButton("Test connection")
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
        self._preset()
        self.setMinimumWidth(600)

    def _p(self):
        return self.OA.PRESET[self.preset.currentData()]

    def _preset(self, *_):
        p, st = self._p(), self.panel.settings
        self.url.setText(st.value(f"openai/{p[0]}/url") or p[2])
        self.url.setPlaceholderText(p[2] or "http://host:port/v1")
        self.key.setText(st.value(f"openai/{p[0]}/key") or "")
        self.key.setPlaceholderText("needed" if p[3] else "not needed for a server on this PC")
        self.info.setText("")

    def test(self):
        url = self.url.text().strip().rstrip("/")
        got = self.OA.list_models(url, self.key.text().strip() or None, timeout=8)
        if got:
            self.info.setText(f"Connected · {len(got)} models: " + ", ".join(got[:15]) + ("…" if len(got) > 15 else ""))
        else:
            self.info.setText(f"No model list from {url}. Is the server running and the key right? (Some servers "
                              "don't list models — type the model name in Icarus's model box.)")

    def save(self):
        st, p = self.panel.settings, self._p()
        st.setValue("openai/preset", p[0])
        url = self.url.text().strip().rstrip("/")
        st.setValue(f"openai/{p[0]}/url", "" if url == p[2] else url)
        st.setValue(f"openai/{p[0]}/key", self.key.text().strip())
        st.setValue("openai/tools", self.tools.currentData())
        st.setValue("openai/tool_mode", self.mode.currentData())
        t = self.temp.text().strip()
        try:
            st.setValue("openai/temperature", str(float(t)) if t else "")
        except ValueError:
            st.setValue("openai/temperature", "")
        self.panel.copilot = None
        self.accept()


class _PullSignal(QObject):
    progress = Signal(str, int, int)
    done = Signal(str, str)


class OllamaModels(QDialog):
    """Installed models (size, what they can do), recommended ones to download, delete."""

    def __init__(self, panel):
        super().__init__(panel)
        from PySide6.QtWidgets import QProgressBar, QTableWidget
        self.panel = panel
        self.host = panel.host()
        self.setWindowTitle("Ollama models")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>On this computer</b> · {html.escape(self.host)}"))
        self.have = QTableWidget(0, 4)
        self.have.setHorizontalHeaderLabels(["Model", "Size", "Can", "Context"])
        self.have.verticalHeader().setVisible(False)
        self.have.setSelectionBehavior(QTableWidget.SelectRows)
        self.have.setEditTriggers(QTableWidget.NoEditTriggers)
        self.have.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.have, 1)
        row = QHBoxLayout()
        use = QPushButton("Use in Icarus")
        use.setObjectName("primary")
        use.clicked.connect(self.use)
        row.addWidget(use)
        rm = QPushButton("Delete…")
        rm.clicked.connect(self.remove)
        row.addWidget(rm)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(QLabel("<b>Download</b> — good for Icarus (models without native tools get prompted tools)"))
        self.rec = QTableWidget(0, 3)
        self.rec.setHorizontalHeaderLabels(["Model", "Download", "Good for"])
        self.rec.verticalHeader().setVisible(False)
        self.rec.setSelectionBehavior(QTableWidget.SelectRows)
        self.rec.setEditTriggers(QTableWidget.NoEditTriggers)
        self.rec.horizontalHeader().setStretchLastSection(True)
        for name, why, gb, how in ollama.RECOMMENDED:
            r = self.rec.rowCount()
            self.rec.insertRow(r)
            from PySide6.QtWidgets import QTableWidgetItem
            self.rec.setItem(r, 0, QTableWidgetItem(name))
            self.rec.setItem(r, 1, QTableWidgetItem(f"{gb:.1f} GB" if gb else "cloud"))
            self.rec.setItem(r, 2, QTableWidgetItem(f"{why} · {how}"))
        self.rec.resizeColumnsToContents()
        v.addWidget(self.rec, 1)
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("or any model from ollama.com/library, e.g. qwen3:14b")
        self.rec.itemSelectionChanged.connect(
            lambda: self.name.setText(self.rec.item(self.rec.currentRow(), 0).text()) if self.rec.currentRow() >= 0
            else None)
        row.addWidget(self.name, 1)
        self.pull_btn = QPushButton("Download")
        self.pull_btn.clicked.connect(self.pull)
        row.addWidget(self.pull_btn)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        r2 = QHBoxLayout()
        r2.addStretch(1)
        r2.addWidget(close)
        v.addLayout(r2)
        self.sig = _PullSignal(self)
        self.sig.progress.connect(self._progress)
        self.sig.done.connect(self._pulled)
        self._cancel = False
        self.refresh()
        self.resize(760, 640)

    def refresh(self):
        from PySide6.QtWidgets import QTableWidgetItem
        self.have.setRowCount(0)
        got = ollama.installed(self.host)
        if not got:
            self.info.setText(f"Ollama isn't answering at {self.host}, or has no models yet.")
        for m in got:
            info = ollama.model_info(self.host, m["name"], timeout=4)
            r = self.have.rowCount()
            self.have.insertRow(r)
            caps = info["caps"]
            can = ", ".join(c for c in ("tools", "vision", "thinking") if c in caps) or "chat (prompted tools)"
            if "tools" not in caps and caps:
                can += " · prompted tools"
            size = "cloud" if m.get("remote") else f"{m['size'] / 1e9:.1f} GB"
            for c, t in enumerate((m["name"], f"{size} · {m['params'] or info['params']}", can,
                                   f"{info['context'] // 1024}k" if info["context"] else "")):
                self.have.setItem(r, c, QTableWidgetItem(t))
        self.have.resizeColumnsToContents()

    def _sel(self):
        r = self.have.currentRow()
        return self.have.item(r, 0).text() if r >= 0 else None

    def use(self):
        m = self._sel()
        if m:
            self.panel.settings.setValue("ollama/model", m)
            self.panel._provider_changed(self.panel.provider.currentIndex())
            self.accept()

    def remove(self):
        from PySide6.QtWidgets import QMessageBox
        m = self._sel()
        if not m or QMessageBox.question(self, "Delete model", f"Delete {m} from this computer? You can download it "
                                         "again later.") != QMessageBox.Yes:
            return
        try:
            ollama.delete(self.host, m)
        except Exception as e:  # noqa: BLE001
            self.info.setText(str(e))
        self.refresh()

    def pull(self):
        import threading
        name = self.name.text().strip()
        if not name:
            return
        if self.pull_btn.text() == "Cancel":
            self._cancel = True
            return
        self._cancel = False
        self.pull_btn.setText("Cancel")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        sig, host = self.sig, self.host

        def run():
            try:
                ollama.pull(host, name, progress=lambda s, d, t: sig.progress.emit(s, d, t), stop=lambda: self._cancel)
                sig.done.emit(name, "")
            except Exception as e:  # noqa: BLE001
                sig.done.emit(name, str(e))
        threading.Thread(target=run, daemon=True).start()

    def _progress(self, status, done, total):
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / total))
            self.info.setText(f"{status} · {done / 1e9:.2f} of {total / 1e9:.2f} GB")
        else:
            self.info.setText(status)

    def _pulled(self, name, err):
        self.pull_btn.setText("Download")
        self.bar.setVisible(False)
        self.info.setText(f"Couldn't download {name}: {err}" if err else f"{name} is ready — select it and click "
                                                                         "Use in Icarus.")
        self.refresh()


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
        self.tools.addItem("Modelling + simulation + drawings + CAM (~127 tools, needs 32k+)", "model")
        self.tools.addItem("Inventory (DIMS)", "dims")
        self.tools.addItem("All tools (simulation, drawings, CAM, inventory…)", "all")
        self.tools.setCurrentIndex(max(0, self.tools.findData(st.value("ollama/tools", "core"))))
        form.addRow("Tools", self.tools)
        self.mode = QComboBox()
        for k, label in (("auto", "Automatic — native if the model supports tools, else described in the prompt"),
                         ("native", "Native tool calling"), ("prompted", "Described in the prompt (any model)")):
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(st.value("ollama/tool_mode", "auto"))))
        self.mode.setToolTip("Models without tool support (gemma3, deepseek-r1, llava, phi…) still work: Icarus lists "
                             "the tools in the prompt and runs the JSON calls the model writes")
        form.addRow("Tool calls", self.mode)
        self.think = QComboBox()
        for k, label in (("", "Model default"), ("off", "Off (fastest)"), ("on", "On"), ("low", "Low (gpt-oss)"),
                         ("medium", "Medium (gpt-oss)"), ("high", "High (gpt-oss)")):
            self.think.addItem(label, k)
        self.think.setCurrentIndex(max(0, self.think.findData(st.value("ollama/think", ""))))
        self.think.setToolTip("For thinking models (qwen3, deepseek-r1, gpt-oss…): thinking is slower but plans "
                              "multi-step parts better")
        form.addRow("Thinking", self.think)
        v.addLayout(form)
        models = QPushButton("Models… (download, delete, what each can do)")
        models.clicked.connect(lambda: OllamaModels(self.panel).exec())
        v.addWidget(models)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        v.addWidget(self.info)
        tip = QLabel("Best for tool use: qwen3 (8b/14b/32b), qwen2.5 (14b+), gpt-oss:20b, llama3.1, mistral-small. "
                     f"Drawings/photos: a vision model ({VISION_HINT}). Any other model works with prompted tools. "
                     "No graphics card? Ollama's cloud models (…-cloud, after  ollama signin).")
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
        st.setValue("ollama/tool_mode", self.mode.currentData())
        st.setValue("ollama/think", self.think.currentData())
        self.panel.copilot = None
        self.accept()
