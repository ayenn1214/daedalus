"""Settings (File ▸ Settings…, Ctrl+,): every Daedalus setting in one window — a section list, a search box that
finds any setting by name, changes that apply as you make them (no Apply button), export / import of all settings
and reset per section. Works from the 3D workspace, the DIMS window and CAD Link: sections a window doesn't have
still save, and say where they apply."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme

SECRET = {"api_key", "update/token", "teams/webhook", "m365/refresh"}                      # never exported to a file
NOT_EXPORTED = SECRET | {"ui/geometry", "ui/dock_state", "dims/chat_geometry", "update/last_check",
                         "update/available", "migrated/from_strata", "home/tip"}


def settings() -> QSettings:
    return QSettings("Daedalus", "Daedalus")


# ------------------------------------------------------------------ what a setting is
@dataclass
class Opt:
    key: str
    label: str
    kind: str = "bool"                                     # bool · choice · int · text · password · folder · file
    default: object = None
    choices: list = field(default_factory=list)            # [(value, label)] for choice
    help: str = ""
    apply: Callable | None = None                          # apply(window, value) — live
    restart: bool = False                                  # applies after a restart
    lo: int = 0
    hi: int = 100000
    suffix: str = ""
    where: str = ""                                         # e.g. "3D workspace" when it only applies there

    def read(self, s):
        v = s.value(self.key, self.default)
        if self.kind == "bool":
            return v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
        if self.kind == "int":
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return int(self.default or 0)
        return "" if v is None else v


def _call(window, name, *a):
    f = getattr(window, name, None)
    if callable(f):
        f(*a)
        return True
    return False


def _view_attr(attr):
    def ap(window, v):
        view = getattr(window, "view", None)
        if view is not None and hasattr(view, attr):
            setattr(view, attr, v)
    return ap


def _inv_page(window):
    for a in ("inventory_page", "dims_page", "page"):
        p = getattr(window, a, None)
        if p is not None and hasattr(p, "store"):
            return p
    return None


def _copilot(window):
    return getattr(window, "copilot", None)


def _apply_provider(window, v):
    cp = _copilot(window)
    if cp is not None and hasattr(cp, "provider"):
        i = cp.provider.findData(v)
        if i >= 0 and cp.provider.currentIndex() != i:
            cp.provider.setCurrentIndex(i)


def _apply_model(window, v):
    cp = _copilot(window)
    if cp is not None and hasattr(cp, "model") and cp.kind() == "ollama":
        cp.model.setCurrentText(v)


def _apply_autosave(window, v):
    t = getattr(window, "_autosave_timer", None)
    if t is not None:
        t.start(max(15, int(v)) * 1000)


def _apply_user(window, v):
    pg = _inv_page(window)
    if pg is not None and v:
        pg.store.inv.userName = v
        if hasattr(pg, "who"):
            try:
                pg.who.setText(v)
            except RuntimeError:
                pass


def _themes():
    return list(theme.THEMES.items())


def _sizes():
    from .adaptive import SIZES
    return list(SIZES.items())


def _navs():
    try:
        from .viewport import NAV_SCHEMES
        return list(NAV_SCHEMES.items())
    except Exception:  # noqa: BLE001
        return [("solidworks", "SolidWorks")]


def _modes():
    try:
        from .viewport import DISPLAY_MODES
    except Exception:  # noqa: BLE001
        try:
            from .main_window import DISPLAY_MODES
        except Exception:  # noqa: BLE001
            return []
    return [(m, m) for m in DISPLAY_MODES]


# ------------------------------------------------------------------ the sections
def sections() -> list[dict]:
    """[{key, title, blurb, opts: [Opt], extra: builder(page, window, dialog) | None, keys: [extra keys]}]"""
    from ..ai import ollama
    from ..ai.copilot import DEFAULT_MODEL
    return [
        {"key": "general", "title": "General", "blurb": "Look, start-up and saving.",
         "opts": [
             Opt("ui/theme", "Theme", "choice", "graphite", _themes(), "Colours for the whole app.",
                 apply=lambda w, v: _call(w, "switch_theme", v), restart=True),
             Opt("ui/toolbar_size", "Toolbar size", "choice", "auto", _sizes(),
                 "Auto fits the ribbon to the window; Full / Compact / Icons fix the density.",
                 apply=lambda w, v: _call(w, "set_toolbar_size", v), where="3D workspace"),
             Opt("home/show", "Show the Home page at start", "bool", True, help="Recent files, examples and tips."),
             Opt("home/reopen_last", "Reopen the last file at start", "bool", False),
             Opt("user/name", "Your name", "text", "", help="Signs check-ins, approvals and releases (File ▸ "
                 "Versions). Blank = your computer login."),
             Opt("autosave/seconds", "Autosave every", "int", 120, lo=15, hi=3600, suffix=" s",
                 help="A recovery copy of the open model (in ~/.daedalus/autosave).", apply=_apply_autosave,
                 where="3D workspace"),
         ], "extra": _general_extra, "keys": ["startup/modules", "splash/config"]},
        {"key": "view", "title": "3D view", "blurb": "How the model looks and how the mouse moves it.",
         "opts": [
             Opt("view/quality", "Display quality", "choice", "normal",
                 [("fine", "Fine (smooth curves)"), ("normal", "Normal"), ("draft", "Draft (big assemblies)")],
                 apply=lambda w, v: _call(w, "set_display_quality", v)),
             Opt("view/antialias", "Anti-aliasing", "choice", "fxaa",
                 [("fxaa", "FXAA (fast, default)"), ("msaa", "MSAA ×4 (smoothest)"), ("off", "Off (fastest)")],
                 apply=lambda w, v: _call(w, "set_antialias", v)),
             Opt("view/mode", "Display mode", "choice", (_modes() or [("", "")])[0][0], _modes(),
                 apply=lambda w, v: _call(w, "set_display_mode", v)),
             Opt("view/nav", "Mouse", "choice", "solidworks", _navs(), "Which buttons rotate, pan and zoom.",
                 apply=lambda w, v: _call(w, "set_nav", v)),
             Opt("view/zoom_cursor", "Zoom toward the cursor", "bool", True, apply=_view_attr("zoom_to_cursor")),
             Opt("view/animate", "Animate view changes", "bool", True, apply=_view_attr("animate_views")),
             Opt("view/hover", "Highlight what's under the mouse", "bool", True, apply=_view_attr("hover_highlight")),
             Opt("view/grid", "Floor grid", "bool", True, apply=lambda w, v: _call(w, "set_grid", v)),
             Opt("view/simplify_moving", "Simplify big models while the view moves", "bool", True,
                 apply=_view_attr("simplify_moving")),
         ], "extra": _view_extra, "where": "3D workspace"},
        {"key": "sketch", "title": "Sketch", "blurb": "Sketching defaults — used from the next sketch you open.",
         "opts": [
             Opt("sketch/grid", "Snap grid", "choice", "0",
                 [("0", "Off"), ("0.5", "0.5 mm"), ("1", "1 mm"), ("2", "2 mm"), ("5", "5 mm"), ("10", "10 mm"),
                  ("25.4", "1 in"), ("12.7", "1/2 in"), ("3.175", "1/8 in")]),
             Opt("sketch/angle_step", "Angle snap", "choice", "15",
                 [("5", "5°"), ("15", "15°"), ("30", "30°"), ("45", "45°"), ("90", "90°")]),
             Opt("sketch/auto_relations", "Add relations automatically (horizontal, coincident …)", "bool", True),
             Opt("sketch/ask_plane", "Ask which plane when starting a sketch", "bool", True),
             Opt("sketch/rapid", "Rapid sketch (start a sketch by clicking a face)", "bool", False),
             Opt("import/ask_up", "Ask which way is up when importing a model", "bool", True),
         ], "extra": _sketch_extra, "where": "3D workspace"},
        {"key": "units", "title": "Units", "blurb": "Unit system, units per measurement, decimals and fractions.",
         "opts": [], "extra": _units_extra, "keys": ["units/system", "units/custom"]},
        {"key": "icarus", "title": "Icarus", "blurb": "The built-in copilot: which engine and model it uses.",
         "opts": [
             Opt("ai/provider", "Engine", "choice", "ollama",
                 [("ollama", "Ollama (local, free)"), ("openai", "OpenAI-compatible (GPT, LM Studio, Jan, OpenRouter…)"),
                  ("claude", "Claude (API key)"), ("claude_code", "Claude (your plan, via Claude Code)"),
                  ("codex", "ChatGPT (your plan, via Codex)"), ("gemini_cli", "Gemini (Google account, via Gemini CLI)")],
                 apply=_apply_provider),
             Opt("ollama/model", "Ollama model", "text", ollama.DEFAULT_MODEL,
                 help="Installed with  ollama pull <name>. A model that doesn't fit on the graphics card is slow.",
                 apply=_apply_model),
             Opt("ollama/host", "Ollama server", "text", "", help=f"Blank = this PC ({ollama.DEFAULT_HOST})."),
             Opt("ollama/num_ctx", "Context", "choice", str(ollama.DEFAULT_CTX),
                 [(str(n), f"{n // 1024}k tokens") for n in (8192, 16384, 32768, 65536)],
                 "Bigger uses more graphics memory; Icarus's instructions need about 12k."),
             Opt("ollama/tools", "Ollama tools", "choice", "core",
                 [("core", f"Core ({len(ollama.CORE_TOOLS)} tools)"), ("all", "All tools")]),
             Opt("model", "Claude model", "choice", DEFAULT_MODEL,
                 [(m, m) for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")]),
             Opt("api_key", "Claude API key", "password", "", help="Stored on this PC only. "
                 "ANTHROPIC_API_KEY, when set, wins."),
             Opt("claude_code/model", "Claude (your plan) model", "choice", "",
                 [("", "Default (your plan)"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku"),
                  ("fable", "Fable")]),
             Opt("claude_code/path", "Claude Code command", "text", "",
                 help="Blank = find  claude  automatically. Sign in once by running  claude  in a terminal. "
                      "Uses the Claude link (Claude Desktop section)."),
             Opt("ollama/tool_mode", "Ollama tool calls", "choice", "auto",
                 [("auto", "Automatic"), ("native", "Native"), ("prompted", "Described in the prompt (any model)")],
                 help="Models without tool support still work: the tools are listed in the prompt."),
             Opt("ollama/think", "Ollama thinking", "choice", "",
                 [("", "Model default"), ("off", "Off"), ("on", "On"), ("low", "Low"), ("medium", "Medium"),
                  ("high", "High")]),
             Opt("openai/preset", "OpenAI-compatible server", "choice", "openai",
                 [(p[0], p[1]) for p in __import__("daedalus.ai.openai_compat", fromlist=["x"]).PRESETS],
                 help="Address, key and model: Icarus's gear button."),
             Opt("openai/tools", "OpenAI-compatible tools", "choice", "",
                 [("", "Automatic"), ("core", "Core"), ("model", "Modelling"), ("dims", "Inventory"), ("all", "All")]),
             Opt("codex/model", "ChatGPT (your plan) model", "text", "", help="Blank = Codex's default."),
             Opt("codex/path", "Codex command", "text", "",
                 help="Blank = find  codex  automatically. Sign in once:  codex login ."),
             Opt("gemini_cli/model", "Gemini model", "text", "", help="Blank = Gemini CLI's default."),
             Opt("gemini_cli/path", "Gemini CLI command", "text", "",
                 help="Blank = find  gemini  automatically. Sign in once by running  gemini ."),
         ], "extra": _icarus_extra},
        {"key": "claude", "title": "Claude Desktop", "blurb": "Use Daedalus's tools from Claude Desktop.",
         "opts": [Opt("claude/link", "Let Claude Desktop use Daedalus while it's open", "bool", True, restart=True,
                      help="A local tool server on 127.0.0.1, protected by a random key.")],
         "extra": _claude_extra, "keys": ["claude/config_path"]},
        {"key": "ai_apps", "title": "AI apps", "blurb": "ChatGPT desktop / Codex, Gemini CLI, Claude Code, LM Studio, "
                                                        "Cursor, Windsurf and VS Code can use Daedalus's tools too.",
         "opts": [], "extra": _ai_apps_extra},
        {"key": "dims", "title": "DIMS", "blurb": "Inventory: who you are, where the data is, how it saves.",
         "opts": [
             Opt("inventory/user", "Your name", "text", "", help="On the activity log and orders.",
                 apply=_apply_user),
             Opt("dims/background_saves", "Save to the team server in the background", "bool", True,
                 help="The window never waits for the server; turn off to wait for each save."),
         ], "extra": _dims_extra, "keys": ["inventory/path", "tables/prefs"]},
        {"key": "company", "title": "Company profile", "blurb": "What prints on purchase orders — shared with the "
         "team.", "opts": [], "extra": _company_extra},
        {"key": "folders", "title": "Folders", "blurb": "Where Daedalus opens and saves files.",
         "opts": [
             Opt("inventory/export_dir", "Exports (Excel, CSV)", "folder", ""),
             Opt("inventory/po_dir", "Purchase orders", "folder", ""),
             Opt("inventory/backup_dir", "Inventory backups", "folder", ""),
             Opt("pdf/dir", "PDFs", "folder", ""),
             Opt("dims/drawing_dir", "Drawings to read", "folder", ""),
             Opt("stdparts/path", "Standard parts library", "folder", "", help="Blank = the built-in library."),
             Opt("swlink/macros", "SolidWorks macros", "folder", ""),
         ]},
        {"key": "cadlink", "title": "CAD Link", "blurb": "SolidWorks, Inventor, Solid Edge, Rhino and Fusion link.",
         "opts": [Opt("swlink/on_top", "Keep the CAD Link window on top", "bool", False, restart=True)],
         "where": "CAD Link"},
        {"key": "updates", "title": "Updates", "blurb": "Getting new versions from GitHub.",
         "opts": [
             Opt("update/auto", "Check for updates at start (and every few hours)", "bool", True),
             Opt("update/channel", "Channel", "choice", "releases",
                 [("releases", "Releases (stable)"), ("main", "Every commit on main (testing)")]),
             Opt("update/repo", "Repository", "text", "", help="Blank = ayenn1214/daedalus."),
             Opt("update/token", "GitHub token", "password", "", help="Only for a private repository."),
         ], "extra": _updates_extra, "keys": ["update/skip"]},
        {"key": "m365", "title": "Microsoft 365", "blurb": "Teams alerts, and a shared OneDrive / SharePoint workbook "
                                                         "for DIMS.",
         "opts": [Opt("teams/enabled", "Teams alerts", "bool", False),
                  Opt("teams/webhook", "Teams channel webhook", "password", "",
                      help="Teams ▸ the channel ▸ ⋯ ▸ Workflows ▸ “Post to a channel when a webhook request is "
                           "received” ▸ copy the address it gives you."),
                  Opt("m365/workbook", "Shared workbook link", "text", "",
                      help="OneDrive / SharePoint ▸ the workbook ▸ Share ▸ Copy link. Pull / Push are on DIMS ▸ "
                           "Import / Export."),
                  Opt("m365/client_id", "App (client) ID", "text", "",
                      help="Your organisation's Microsoft Entra app registration (public client, delegated "
                           "Files.ReadWrite.All). Ask IT, or register one at entra.microsoft.com ▸ App registrations "
                           "(allow public client flows)."),
                  Opt("m365/tenant", "Tenant", "text", "organizations",
                      help="organizations (any work account), or your tenant ID / domain.")],
         "extra": _m365_extra},
        {"key": "compute", "title": "Compute", "blurb": "Where flow and structural simulations run: every CPU core "
                                                       "or an NVIDIA graphics card (CUDA).",
         "opts": [Opt("compute/device", "Run simulations on", "choice", "auto",
                      [("auto", "Automatic (graphics card when available)"), ("cpu", "CPU only"),
                       ("gpu", "Graphics card (CUDA)")], apply=lambda v: _compute_apply()),
                  Opt("compute/threads", "CPU threads", "int", 0, lo=0, hi=256, help="0 = all cores. Fewer leaves the computer "
                      "responsive while a simulation runs.", apply=lambda v: _compute_apply())],
         "extra": _compute_extra},
        {"key": "advanced", "title": "Advanced", "blurb": "Reports, checks and all settings at once.",
         "opts": [Opt("bugreport/email", "Your email on bug reports", "text", "")],
         "extra": _advanced_extra},
    ]


# ------------------------------------------------------------------ extra parts of pages
def _btn(text, fn, primary=False, tip=""):
    b = QPushButton(text)
    if primary:
        b.setObjectName("primary")
    if tip:
        b.setToolTip(tip)
    b.clicked.connect(fn)
    return b


def _left(lay, *widgets):
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    row.addStretch(1)
    lay.addLayout(row)


def _note(text):
    l = QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(True)
    return l


def _embed(dlg, lay, on_saved=None):
    """Show an existing settings dialog inside the page. Cancel / Close go (there's nothing to close), OK reads
    Save, and saving keeps it on the page."""
    from PySide6.QtWidgets import QDialogButtonBox
    dlg.setWindowFlags(Qt.Widget)
    for bb in dlg.findChildren(QDialogButtonBox):
        for b in bb.buttons():
            if bb.buttonRole(b) == QDialogButtonBox.RejectRole:
                b.hide()
        ok = bb.button(QDialogButtonBox.Ok)
        if ok is not None:
            ok.setText("Save")
    box = QFrame()
    box.setObjectName("setcard")
    v = QVBoxLayout(box)
    v.setContentsMargins(10, 8, 10, 8)
    v.addWidget(dlg)
    lay.addWidget(box)

    def done(result):
        dlg.setResult(0)
        dlg.show()                                         # accept() hides a dialog: keep it on the page
        if result and on_saved:
            on_saved()
    dlg.finished.connect(done)
    dlg.show()
    return dlg


def _general_extra(page, lay, window, sd):
    from .. import modules as MOD
    s = settings()
    mods = MOD.load(s)
    lay.addWidget(_note("Startup modules: " + MOD.describe(mods) + " — what loads when Daedalus starts."))
    row = QHBoxLayout()
    row.addWidget(_btn("Startup modules…", lambda: _modules(window, sd)))
    row.addWidget(_btn("Splash screen…", lambda: __import__("daedalus.ui.splash", fromlist=["x"]).editor(sd).exec()))
    row.addStretch(1)
    lay.addLayout(row)


def _modules(window, sd):
    from .modules_ui import StartupModulesDialog
    StartupModulesDialog(settings(), sd, current=getattr(window, "mods", None)).exec()
    sd.rebuild("general")


def _view_extra(page, lay, window, sd):
    if hasattr(window, "show_graphics_info"):
        _left(lay, _btn("Graphics info…", window.show_graphics_info, tip="Graphics card, OpenGL and frame time"))


def _sketch_extra(page, lay, window, sd):
    try:
        from .snap_settings import SnapSettingsDialog
        lay.addWidget(_note("Snapping (endpoints, midpoints, centres, tangents …):"))
        _embed(SnapSettingsDialog(page), lay, sd.saved)
    except Exception as e:  # noqa: BLE001
        lay.addWidget(_note(f"Snap settings unavailable here ({e})."))


def _units_extra(page, lay, window, sd):
    doc = getattr(window, "doc", None)
    try:
        from .units_ui import UnitsDialog
        if doc is None or not hasattr(doc, "units"):
            from ..core.document import Document
            doc = Document()
            lay.addWidget(_note("No model is open: these become the default for new models."))
        _embed(UnitsDialog(doc, settings(), page), lay, sd.saved)
    except Exception as e:  # noqa: BLE001
        lay.addWidget(_note(f"Units are set in the 3D workspace ({e})."))


def _icarus_extra(page, lay, window, sd):
    from ..ai import ollama
    from .. import setup_check as SC
    out = QLabel("")
    out.setWordWrap(True)
    out.setObjectName("muted")

    def test():
        host = ollama.normalize_host(settings().value("ollama/host") or ollama.DEFAULT_HOST)
        got = ollama.list_models(host, timeout=3.0)
        want = settings().value("ollama/model") or ollama.DEFAULT_MODEL
        if not got:
            out.setText(f"✗ No answer from {host}. Start Ollama (Start menu ▸ Ollama), then test again.")
        else:
            have = want in got or f"{want}:latest" in got
            out.setText(f"✓ Ollama answers at {host} — {len(got)} model(s): {', '.join(got[:8])}"
                        + ("" if have else f"<br>✗ {want} isn't downloaded:  ollama pull {want}"))
    row = QHBoxLayout()
    row.addWidget(_btn("Test connection", test, True))
    try:
        rec = SC.recommended_model()
        vr = SC.vram_gb()
        row.addWidget(_note(f"Recommended for this PC: <b>{rec}</b>" +
                            (f" ({vr:.0f} GB graphics card)" if vr else " (no NVIDIA card found)")), 1)
    except Exception:  # noqa: BLE001
        pass
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(out)
    if hasattr(window, "show_setup_check"):
        _left(lay, _btn("Setup check (Python + Icarus)…", window.show_setup_check))


def _claude_extra(page, lay, window, sd):
    from .claude_bridge import ClaudeDesktopDialog
    _embed(ClaudeDesktopDialog(getattr(window, "claude_bridge", None), page), lay)


def _ai_apps_extra(page, lay, window, sd):
    from .ai_apps_ui import AIAppsDialog
    _embed(AIAppsDialog(getattr(window, "claude_bridge", None), page), lay)


def _dims_extra(page, lay, window, sd):
    pg = _inv_page(window)
    s = settings()
    st = getattr(pg, "store", None) if pg is not None else None
    if st is not None:
        if getattr(st, "kind", "file") == "supabase":
            who = getattr(st, "email", "") or "not signed in"
            lay.addWidget(_note(f"<b>Team server</b> · {who} · {getattr(st, 'role', '') or ''}<br>{getattr(st, 'url', '')}"))
        else:
            lay.addWidget(_note(f"<b>Inventory file</b> · {getattr(st, 'path', '')}"))
    else:
        lay.addWidget(_note("Inventory file: " + (s.value("inventory/path") or "the default") +
                            " — open DIMS to change the team server."))
    row = QHBoxLayout()
    if pg is not None:
        row.addWidget(_btn("Team server…", pg.team_connect, tip="Server address, sign in / out, roles"))
        if hasattr(pg, "choose_file"):
            row.addWidget(_btn("Inventory file…", pg.choose_file, tip="Use a different inventory file on this PC"))
    row.addStretch(1)
    lay.addLayout(row)
    try:
        from .table_prefs import TablePrefsDialog
        lay.addWidget(_note("<b>Tables</b> — row and column spacing, grid lines, text size:"))
        _embed(TablePrefsDialog((lambda: pg.refresh_current()) if pg is not None else (lambda: None), page, s),
               lay, sd.saved)
    except Exception as e:  # noqa: BLE001
        lay.addWidget(_note(f"Table settings unavailable ({e})."))


def _company_extra(page, lay, window, sd):
    pg = _inv_page(window)
    if pg is None:
        lay.addWidget(_note("Open DIMS (the inventory) to edit the company profile — it's kept with the team's "
                            "inventory."))
        return
    from ..inventory import purchase_order as P
    from .po_ui import POSettingsDialog
    d = POSettingsDialog(pg.inv, page)

    def saved():
        P.save_settings(pg.inv, d.value())
        pg.commit("Company profile saved")
        sd.saved()
    _embed(d, lay, saved)


def _updates_extra(page, lay, window, sd):
    from .. import __version__
    row = QHBoxLayout()
    row.addWidget(_note(f"This is Daedalus <b>{__version__}</b>."))
    row.addWidget(_btn("Check now", lambda: __import__("daedalus.ui.update_ui", fromlist=["x"]).check_now(window),
                       True))
    try:
        from .. import updater as U
        folders = U.rollback_folders()
        if folders:
            from . import update_ui as UU
            row.addWidget(_btn("Roll back…", lambda: UU._rollback(window, folders[0]),
                               tip=f"Go back to the version before the last update ({folders[0].name})"))
    except Exception:  # noqa: BLE001
        pass
    row.addStretch(1)
    lay.addLayout(row)


def _m365_extra(page, lay, window, sd):
    from .. import m365
    from PySide6.QtWidgets import QCheckBox, QMessageBox
    lay.addWidget(_note("<b>Alert on</b>"))
    on = str(m365.setting("teams/events", ",".join(k for k, _ in m365.EVENTS)) or "").split(",")
    boxes = {}
    for k, label in m365.EVENTS:
        cb = QCheckBox(label)
        cb.setChecked(k in on)
        cb.toggled.connect(lambda _c: m365.set_setting("teams/events", ",".join(x for x, b in boxes.items()
                                                                                if b.isChecked())))
        boxes[k] = cb
        lay.addWidget(cb)
    row = QHBoxLayout()

    def test():
        try:
            m365.post_teams(m365.setting("teams/webhook", ""), "Daedalus test alert", "Alerts from Daedalus will "
                            "appear here.", {"From": __import__("getpass").getuser()})
            QMessageBox.information(page, "Teams", "Sent — check the channel.")
        except m365.M365Error as e:
            QMessageBox.warning(page, "Teams", str(e))
    row.addWidget(_btn("Send test alert", test))
    acct = _note("Signed in to Microsoft 365." if m365.signed_in() else "Not signed in to Microsoft 365.")

    def sign_in():
        try:
            dc = m365.device_code_start()
        except m365.M365Error as e:
            return QMessageBox.warning(page, "Microsoft 365", str(e))
        from PySide6.QtGui import QDesktopServices, QGuiApplication
        from PySide6.QtCore import QUrl
        QGuiApplication.clipboard().setText(dc["user_code"])
        QDesktopServices.openUrl(QUrl(dc["verification_uri"]))
        box = QMessageBox(QMessageBox.Information, "Microsoft 365",
                          f"Enter this code on the Microsoft page that opened (it's copied):\n\n{dc['user_code']}\n\n"
                          "Daedalus finishes the sign-in when you've done that.", QMessageBox.Cancel, page)
        import threading
        result = {}
        def run():
            try:
                result["r"] = m365.device_code_poll(dc)
            except Exception as e:  # noqa: BLE001
                result["e"] = e
        threading.Thread(target=run, daemon=True).start()
        from PySide6.QtCore import QTimer
        t = QTimer(box)
        t.timeout.connect(lambda: box.accept() if result else None)
        t.start(500)
        box.exec()
        if "e" in result:
            QMessageBox.warning(page, "Microsoft 365", str(result["e"]))
        acct.setText("Signed in to Microsoft 365." if m365.signed_in() else "Not signed in to Microsoft 365.")
    row.addWidget(_btn("Sign in…", sign_in))
    row.addWidget(_btn("Sign out", lambda: (m365.sign_out(), acct.setText("Not signed in to Microsoft 365."))))
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(acct)
    lay.addWidget(_note("If the OneDrive app already syncs the workbook's folder to this PC, DIMS Import / Export on "
                        "that file works with no sign-in."))


def _compute_apply():
    from .. import compute
    compute.apply_threads()
    compute.info(refresh=True)


def _compute_extra(page, lay, window, sd):
    from .. import compute
    lab = _note(compute.summary().replace("\n", "<br>"))
    lay.addWidget(lab)
    row = QHBoxLayout()
    row.addWidget(_btn("Check again", lambda: lab.setText(compute.summary().replace("\n", "<br>")) if
                       compute.info(refresh=True) else None))
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(_note("Graphics card (NVIDIA, CUDA): <code>pip install numba-cuda[cu12]</code> for flow, "
                        "<code>pip install cupy-cuda12x</code> for structural solves. Faster CPU solves: "
                        "<code>pip install pypardiso</code>. Flow already uses every CPU core."))


def _advanced_extra(page, lay, window, sd):
    from .. import crash
    row = QHBoxLayout()
    row.addWidget(_btn("Crash reports folder", lambda: __import__("PySide6.QtGui", fromlist=["x"]).QDesktopServices
                       .openUrl(__import__("PySide6.QtCore", fromlist=["x"]).QUrl.fromLocalFile(str(crash.crash_dir())))))
    if hasattr(window, "show_setup_check"):
        row.addWidget(_btn("Setup check…", window.show_setup_check))
    if hasattr(window, "report_bug"):
        row.addWidget(_btn("Report a bug…", window.report_bug))
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(_note("<b>All settings</b> — move them to another PC, or start over. Passwords, API keys and "
                        "tokens are never written to the file."))
    row = QHBoxLayout()
    row.addWidget(_btn("Export settings…", sd.export_all))
    row.addWidget(_btn("Import settings…", sd.import_all))
    row.addWidget(_btn("Reset everything…", sd.reset_all))
    row.addStretch(1)
    lay.addLayout(row)


# ------------------------------------------------------------------ export / import / reset
def export_settings(path) -> int:
    s = settings()
    out = {}
    for k in s.allKeys():
        if k in NOT_EXPORTED or k.startswith(("ui/", "tables/state")) and k != "ui/theme" and k != "ui/toolbar_size":
            continue
        v = s.value(k)
        try:
            json.dumps(v)
        except TypeError:
            continue
        out[k] = v
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"daedalus_settings": 1, "settings": out}, f, indent=2, sort_keys=True)
    return len(out)


def import_settings(path) -> int:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    vals = d.get("settings") if isinstance(d, dict) else None
    if not isinstance(vals, dict):
        raise ValueError("Not a Daedalus settings file")
    s = settings()
    n = 0
    for k, v in vals.items():
        if k in SECRET:
            continue
        s.setValue(k, v)
        n += 1
    return n


def section_keys(sec) -> list[str]:
    return [o.key for o in sec["opts"]] + list(sec.get("keys", []))


def reset_section(sec):
    s = settings()
    for k in section_keys(sec):
        if k in SECRET:
            continue
        s.remove(k)


# ------------------------------------------------------------------ the window
class SettingsDialog(QDialog):
    def __init__(self, window=None, section: str | None = None, parent=None):
        from PySide6.QtWidgets import QWidget as _W
        super().__init__(parent or (window if isinstance(window, _W) else None))
        self.window_ = window
        self.s = settings()
        self.setWindowTitle("Settings")
        self.resize(980, 700)
        self.setStyleSheet(f"QFrame#setcard {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER}; "
                           f"border-radius: 8px; }}")
        self.secs = sections()
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        left = QWidget()
        left.setFixedWidth(230)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 8, 12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter)
        lv.addWidget(self.search)
        self.nav = QListWidget()
        self.nav.setObjectName("settingsnav")
        for sec in self.secs:
            it = QListWidgetItem(sec["title"])
            it.setData(Qt.UserRole, sec["key"])
            self.nav.addItem(it)
        self.nav.currentRowChanged.connect(self._show)
        lv.addWidget(self.nav, 1)
        self.toast = _note("")
        lv.addWidget(self.toast)
        root.addWidget(left)
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet(f"color: {theme.BORDER}")
        root.addWidget(line)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.pages, self.rows = {}, {}
        i = next((k for k, sec in enumerate(self.secs) if sec["key"] == section), 0)
        self.nav.setCurrentRow(i)
        self._toast_t = QTimer(self)
        self._toast_t.setSingleShot(True)
        self._toast_t.timeout.connect(lambda: self.toast.setText(""))

    # ---------------------------------------------------------------- pages (built when first shown)
    def _show(self, row):
        if row < 0:
            return
        sec = self.secs[row]
        if sec["key"] not in self.pages:
            self.pages[sec["key"]] = self._build(sec)
            self.stack.addWidget(self.pages[sec["key"]])
            self.filter(self.search.text())
        self.stack.setCurrentWidget(self.pages[sec["key"]])

    def rebuild(self, key):
        old = self.pages.pop(key, None)
        if old is not None:
            self.stack.removeWidget(old)
            old.deleteLater()
        self._show(self.nav.currentRow())

    def _build(self, sec):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel(f"<span style='font-size:15pt;font-weight:600'>{sec['title']}</span>")
        head.addWidget(t)
        head.addStretch(1)
        if section_keys(sec):
            head.addWidget(_btn("Reset section", lambda _=False, s_=sec: self.reset(s_),
                                tip="Back to the defaults for this section"))
        lay.addLayout(head)
        blurb = sec["blurb"]
        if sec.get("where") and not self._has(sec["where"]):
            blurb += f" <i>(Applies in the {sec['where']} — saved for when you use it.)</i>"
        lay.addWidget(_note(blurb))
        self.rows[sec["key"]] = []
        if sec["opts"]:
            card = QFrame()
            card.setObjectName("setcard")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(14, 8, 14, 8)
            cv.setSpacing(2)
            for k, o in enumerate(sec["opts"]):
                sep = None
                if k:
                    sep = QFrame()
                    sep.setFrameShape(QFrame.HLine)
                    sep.setStyleSheet(f"color: {theme.BORDER}")
                    cv.addWidget(sep)
                row = self._row(o)
                row.sep = sep
                cv.addWidget(row)
                self.rows[sec["key"]].append((row, o))
            lay.addWidget(card)
        if sec.get("extra"):
            try:
                sec["extra"](page, lay, self.window_, self)
            except Exception as e:  # noqa: BLE001
                lay.addWidget(_note(f"Couldn't show this part here: {e}"))
        lay.addStretch(1)
        sa.setWidget(page)
        return sa

    def _has(self, where):
        w = self.window_
        if where == "3D workspace":
            return w is not None and hasattr(w, "view")
        if where == "CAD Link":
            return w is not None and hasattr(w, "link")
        return True

    def _row(self, o: Opt):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 6, 0, 6)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        lab = QLabel(o.label + (" <span style='color:gray'>· restart</span>" if o.restart else ""))
        txt.addWidget(lab)
        if o.help:
            txt.addWidget(_note(o.help))
        h.addLayout(txt, 1)
        val = o.read(self.s)
        if o.kind == "bool":
            ed = QCheckBox()
            ed.setChecked(bool(val))
            ed.toggled.connect(lambda v, o=o: self.set(o, bool(v)))
        elif o.kind == "choice":
            ed = QComboBox()
            for v, l in o.choices:
                ed.addItem(l, v)
            i = ed.findData(str(val)) if ed.findData(val) < 0 else ed.findData(val)
            if i < 0 and val not in (None, ""):
                ed.addItem(str(val), val)
                i = ed.count() - 1
            ed.setCurrentIndex(max(0, i))
            ed.setMinimumWidth(220)
            ed.activated.connect(lambda _i, o=o, ed=ed: self.set(o, ed.currentData()))
        elif o.kind == "int":
            ed = QSpinBox()
            ed.setRange(o.lo, o.hi)
            ed.setValue(int(val))
            ed.setSuffix(o.suffix)
            ed.setMinimumWidth(120)
            ed.editingFinished.connect(lambda o=o, ed=ed: self.set(o, ed.value()))
        elif o.kind in ("folder", "file"):
            ed = QWidget()
            hl = QHBoxLayout(ed)
            hl.setContentsMargins(0, 0, 0, 0)
            le = QLineEdit(str(val or ""))
            le.setPlaceholderText("Not set")
            le.setMinimumWidth(320)
            le.editingFinished.connect(lambda o=o, le=le: self.set(o, le.text().strip()))

            def browse(_=False, o=o, le=le):
                p = QFileDialog.getExistingDirectory(self, o.label, le.text() or os.path.expanduser("~")) \
                    if o.kind == "folder" else QFileDialog.getOpenFileName(self, o.label, le.text())[0]
                if p:
                    le.setText(p)
                    self.set(o, p)
            hl.addWidget(le, 1)
            hl.addWidget(_btn("Browse…", browse))
            ed.line = le
        else:
            ed = QLineEdit(str(val or ""))
            ed.setMinimumWidth(260)
            if o.kind == "password":
                ed.setEchoMode(QLineEdit.Password)
                ed.setPlaceholderText("Not set")
            elif o.key in ("ollama/host", "update/repo"):
                ed.setPlaceholderText("Default")
            ed.editingFinished.connect(lambda o=o, ed=ed: self.set(o, ed.text().strip()))
        w.editor = ed
        h.addWidget(ed, 0, Qt.AlignRight | Qt.AlignVCenter)
        return w

    # ---------------------------------------------------------------- changes
    def set(self, o: Opt, v):
        if o.read(self.s) == v and self.s.contains(o.key):
            return
        if v in ("", None) and o.kind in ("text", "password", "folder", "file"):
            self.s.remove(o.key)
        else:
            self.s.setValue(o.key, v)
        if o.apply is not None and self.window_ is not None:
            try:
                o.apply(self.window_, v)
            except Exception as e:  # noqa: BLE001
                self.say(f"Saved — applies next start ({e})")
                return
        self.say("Saved" + (" — applies after a restart" if o.restart and o.key != "ui/theme" else ""))

    def saved(self):
        self.say("Saved")

    def say(self, text):
        self.toast.setText(f"✓ {text}" if text else "")
        self._toast_t.start(3500)

    def reset(self, sec):
        if QMessageBox.question(self, "Settings", f"Put “{sec['title']}” back to the defaults?") != QMessageBox.Yes:
            return
        reset_section(sec)
        self.rebuild(sec["key"])
        self.say(f"{sec['title']} reset — some changes apply after a restart")

    def reset_all(self):
        if QMessageBox.question(self, "Settings", "Put every setting back to the default? Your inventory, models "
                                "and team server sign-in aren't touched; the API key and GitHub token are kept.") \
                != QMessageBox.Yes:
            return
        for sec in self.secs:
            reset_section(sec)
        for k in list(self.pages):
            self.rebuild(k)
        self.say("Everything reset — restart Daedalus to see all of it")

    def export_all(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export settings", os.path.expanduser("~/Daedalus settings.json"),
                                           "Settings (*.json)")
        if p:
            n = export_settings(p)
            self.say(f"Exported {n} settings")

    def import_all(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import settings", os.path.expanduser("~"), "Settings (*.json)")
        if not p:
            return
        try:
            n = import_settings(p)
        except (OSError, ValueError) as e:
            return QMessageBox.warning(self, "Settings", f"Couldn't import: {e}")
        for k in list(self.pages):
            self.rebuild(k)
        self.say(f"Imported {n} settings — restart to apply them all")

    # ---------------------------------------------------------------- search
    def filter(self, text):
        """Hide sections without a match; inside a section, show only the matching settings (all of them when the
        match is the section's own name or one of its buttons)."""
        words = [w for w in (text or "").lower().split() if w]

        def has(t):
            t = t.lower()
            return all(w in t for w in words)
        first = None
        for i, sec in enumerate(self.secs):
            own = f"{sec['title']} {sec['blurb']} {_EXTRA_WORDS.get(sec['key'], '')}"
            opt_hit = {o.key: has(f"{o.label} {o.help} {o.key}") for o in sec["opts"]}
            hit = not words or has(own) or any(opt_hit.values()) or \
                has(own + " " + " ".join(f"{o.label} {o.help} {o.key}" for o in sec["opts"]))
            self.nav.item(i).setHidden(not hit)
            if hit and first is None:
                first = i
            show_all = not words or not any(opt_hit.values())
            for row, o in self.rows.get(sec["key"], []):
                on = show_all or opt_hit[o.key]
                row.setVisible(on)
                if row.sep is not None:
                    row.sep.setVisible(on)
        cur = self.nav.currentItem()
        if first is not None and (cur is None or cur.isHidden()):
            self.nav.setCurrentRow(first)


_EXTRA_WORDS = {
    "general": "startup modules splash screen",
    "view": "graphics info opengl",
    "sketch": "snap snapping endpoint midpoint tangent",
    "units": "metric imperial inch mm feet decimals fractions construction",
    "icarus": "ollama claude test connection setup check recommended model ai copilot",
    "claude": "claude desktop mcp connect disconnect config",
    "dims": "inventory team server supabase sign in table spacing grid lines rows columns",
    "company": "purchase order po logo address ship to footer numbering",
    "folders": "folder directory path",
    "updates": "update check now roll back channel github",
    "advanced": "export import reset crash bug report",
}


def open_settings(window, section=None):
    d = SettingsDialog(window, section)
    d.exec()
    return d
