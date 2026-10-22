"""Daedalus visual identity: graphite panels, copper accent. Deliberately not a clone of any commercial CAD look.

apply(app) sets the Fusion style (identical on Windows / Linux / macOS), a matching dark palette, the
stylesheet below and a proxy style that drops stock icons from dialog buttons.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

def _mode() -> str:
    m = os.environ.get("DAEDALUS_THEME", "")
    if not m:
        try:
            from PySide6.QtCore import QSettings
            m = QSettings("Daedalus", "Daedalus").value("ui/theme", "graphite") or "graphite"
        except Exception:  # noqa: BLE001
            m = "graphite"
    return m if m in ("graphite", "black") else "graphite"


MODE = _mode()
THEMES = {"graphite": "Graphite", "black": "Black"}

if MODE == "black":
    # pure black surfaces; every colour (accent, status, selection, plots, parts) kept
    ACCENT = "#0A3A36"        # deep teal — fills (primary buttons, checked boxes, hub disc)
    ACCENT_HOVER = "#0F4F49"  # fill hover
    ACCENT_SOFT = "#06201E"   # checked / selected backgrounds
    ACCENT_LINE = "#2A9184"   # same hue, light enough for borders, underlines, icons on black
    ACCENT_TEXT = "#7FD0C4"   # same hue, readable as text
    ON_ACCENT = "#E4EFEC"
    BG = "#000000"
    PANEL = "#0b0b0b"
    PANEL_2 = "#151515"
    RAISED = "#1f1f1f"
    INPUT = "#070707"
    BORDER = "#2e2e2e"
    BORDER_SOFT = "#1b1b1b"
    TEXT = "#e6e8eb"
    MUTED = "#8f969e"
    DIM = "#5c636b"
    ICON = "#c3c9d0"
    ERROR = "#e5534b"
    OK = "#57ab5a"
    WARN = "#d4a72c"
    INFO = "#7db3e8"
    PURPLE = "#b591e8"
    ROW_ALT = "#0f0f0f"
    SCROLL = "#333333"
    SCROLL_HOVER = "#4d4d4d"
    HOVER_BORDER = "#444444"
    VIEW_BG_TOP = (0.10, 0.10, 0.11)
    VIEW_BG_BOTTOM = (0.0, 0.0, 0.0)
    BODY_COLOR = (0.62, 0.64, 0.67)
    EDGE_COLOR = (0.04, 0.04, 0.05)
    SKETCH_COLOR = (0.10, 0.55, 0.50)
    SELECT_COLOR = (0.16, 0.66, 0.60)
    VIEW_TEXT = (0.85, 0.86, 0.88)
    VIEW_HILITE = (0.31, 0.76, 0.97)
    VIEW_SNAP = (1.0, 0.82, 0.4)
    VIEW_GHOST = (0.45, 0.47, 0.50)
    VIEW_LINES = (0.85, 0.86, 0.88)
    CAM_RAPID = (0.9, 0.6, 0.2)
    SK_BG, SK_GRID, SK_GRID_MAJOR, SK_AXIS = "#000000", "#111111", "#1c1c1c", "#3a3a3a"
    SK_DEFINED, SK_CONSTR, SK_SEL, SK_HOVER = "#e6e8eb", "#7d8590", "#4fc3f7", "#9be3ff"
    SK_DIM, SK_UNDER, SK_BADGE, SK_BADGE_BG = "#9ecbff", "#5a6470", "#c8ccd2", "#1a1a1a"
    SK_GUIDE, SK_RUBBER = "#ffd166", (79, 195, 247, 30)
    SK_FREE, SK_BAD = ACCENT_LINE, ERROR
    # drawing canvas: white paper painted, then lightness-inverted with hues kept (exports stay black on white)
    DRAW_BG, DRAW_TEXT, DRAW_SEL, DRAW_SNAP, DRAW_INVERT = "#f4f4f4", "#e6e8eb", "#1f7ae0", "#e07a1f", True
    DOODLE_PAPER, DOODLE_GRID, DOODLE_INK, DOODLE_HINT = "#050505", "#171717", "#ececec", "#6e6e6e"
    THUMB_BG = ((0.10, 0.10, 0.11), (0.02, 0.02, 0.02))
    THUMB_BODY, THUMB_EDGE = (0.66, 0.68, 0.72), (0.03, 0.03, 0.04)
else:
    ACCENT = "#0A3A36"        # deep teal — fills (primary buttons, checked boxes, hub disc)
    ACCENT_HOVER = "#0F4F49"  # fill hover
    ACCENT_SOFT = "#0C2F2C"   # checked / selected backgrounds
    ACCENT_LINE = "#2E9A8D"   # same hue, light enough for borders, underlines, icons on graphite
    ACCENT_TEXT = "#86D6CA"   # same hue, readable as text
    ON_ACCENT = "#E4EFEC"
    BG = "#1b1e22"
    PANEL = "#23272c"
    PANEL_2 = "#2b3036"
    RAISED = "#323840"
    INPUT = "#1f2327"
    BORDER = "#3a4048"
    BORDER_SOFT = "#2f343a"
    TEXT = "#e6e8eb"
    MUTED = "#959ea8"
    DIM = "#6b737d"
    ICON = "#c3c9d0"
    ERROR = "#e5534b"
    OK = "#57ab5a"
    WARN = "#d4a72c"
    INFO = "#7db3e8"
    PURPLE = "#b591e8"
    ROW_ALT = "#262a30"
    SCROLL = "#434a53"
    SCROLL_HOVER = "#59616b"
    HOVER_BORDER = "#4a525c"
    VIEW_BG_TOP = (0.82, 0.85, 0.88)
    VIEW_BG_BOTTOM = (0.55, 0.60, 0.66)
    BODY_COLOR = (0.70, 0.72, 0.75)
    EDGE_COLOR = (0.10, 0.11, 0.13)
    SKETCH_COLOR = (0.10, 0.55, 0.50)
    SELECT_COLOR = (0.16, 0.66, 0.60)
    VIEW_TEXT = (0.15, 0.15, 0.15)
    VIEW_HILITE = (0.31, 0.76, 0.97)
    VIEW_SNAP = (1.0, 0.82, 0.4)
    VIEW_GHOST = (0.35, 0.38, 0.42)
    VIEW_LINES = (0.15, 0.16, 0.2)
    CAM_RAPID = (0.9, 0.6, 0.2)
    SK_BG, SK_GRID, SK_GRID_MAJOR, SK_AXIS = "#1a1d21", "#23272d", "#2c3138", "#3d4550"
    SK_DEFINED, SK_CONSTR, SK_SEL, SK_HOVER = "#e6e8eb", "#7d8590", "#4fc3f7", "#9be3ff"
    SK_DIM, SK_UNDER, SK_BADGE, SK_BADGE_BG = "#9ecbff", "#5a6470", "#c8ccd2", "#2a2f36"
    SK_GUIDE, SK_RUBBER = "#ffd166", (79, 195, 247, 30)
    SK_FREE, SK_BAD = ACCENT_LINE, ERROR
    DRAW_BG, DRAW_TEXT, DRAW_SEL, DRAW_SNAP, DRAW_INVERT = "#5b6470", "#e8ecf1", "#1f7ae0", "#e07a1f", False
    DOODLE_PAPER, DOODLE_GRID, DOODLE_INK, DOODLE_HINT = "#f3f0e8", "#e4dfd2", "#2b2b2b", "#9a9384"
    THUMB_BG = ((0.92, 0.93, 0.95), (0.74, 0.77, 0.81))
    THUMB_BODY, THUMB_EDGE = (0.72, 0.75, 0.80), (0.12, 0.13, 0.15)

FONT = "'Segoe UI Variable Text', 'Segoe UI', 'Inter', 'Helvetica Neue', 'Arial'"


def set_mode(mode: str):
    """Remember the theme for the next start (colours are read once, at start-up)."""
    from PySide6.QtCore import QSettings
    QSettings("Daedalus", "Daedalus").setValue("ui/theme", mode)


def _assets() -> Path:
    """Small SVGs the stylesheet references (arrows, check marks). Written once per user."""
    d = Path(tempfile.gettempdir()) / f"daedalus-ui-v3-{MODE}"
    files = {
        "down.svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                    f'<path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="{MUTED}" stroke-width="1.5" '
                    f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "up.svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                  f'<path d="M2 6.5 5 3.5 8 6.5" fill="none" stroke="{MUTED}" stroke-width="1.5" '
                  f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "check.svg": '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                     f'<path d="M2.5 6.2 5 8.6 9.6 3.6" fill="none" stroke="{ON_ACCENT}" stroke-width="2" '
                     'stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "dot.svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                   f'<circle cx="6" cy="6" r="3" fill="{ON_ACCENT}"/></svg>',
        "right.svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                     f'<path d="M3.5 2 6.5 5 3.5 8" fill="none" stroke="{MUTED}" stroke-width="1.5" '
                     f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "open.svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                    f'<path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="{MUTED}" stroke-width="1.5" '
                    f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    }
    try:
        d.mkdir(parents=True, exist_ok=True)
        for n, svg in files.items():
            f = d / n
            if not f.exists() or f.read_text() != svg:
                f.write_text(svg)
    except OSError:
        pass
    return d


_A = _assets().as_posix()

QSS = f"""
* {{ outline: none; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}

/* ---------- chrome */
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {PANEL}; padding: 7px 10px; border-bottom: 1px solid {BORDER_SOFT};
    font-weight: 600; color: {MUTED}; }}
QMenuBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER_SOFT}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {RAISED}; }}
QMenu {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px; }}
QMenu::item {{ padding: 6px 28px 6px 14px; border-radius: 4px; background: transparent; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QMenu::item:disabled {{ color: {BORDER}; }}
QMenu::separator {{ height: 1px; background: {BORDER_SOFT}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
QMenu::indicator:checked {{ image: url({_A}/check.svg); background: {ACCENT}; border-radius: 3px; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {BORDER_SOFT}; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 8px; background: transparent; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px 8px;
    border-radius: 4px; }}
QSplitter::handle {{ background: {BORDER_SOFT}; }}
QSplitter::handle:hover {{ background: {ACCENT_LINE}; }}
QMainWindow::separator {{ background: {BORDER_SOFT}; width: 1px; height: 1px; }}
QMainWindow::separator:hover {{ background: {ACCENT_LINE}; }}

/* ---------- tabs */
QTabWidget::pane {{ border: none; background: {PANEL}; top: -1px; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT_LINE}; font-weight: 600; }}
QTabBar::tab:disabled {{ color: {BORDER}; }}

/* ---------- buttons */
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 14px;
    min-height: 18px; color: {TEXT}; }}
QPushButton:hover {{ background: {RAISED}; border-color: {HOVER_BORDER}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; border-color: {ACCENT_LINE}; }}
QPushButton:disabled {{ color: {DIM}; border-color: {BORDER_SOFT}; background: {PANEL}; }}
QPushButton:focus {{ border-color: {ACCENT_LINE}; }}
QPushButton#primary, QPushButton[default="true"] {{ background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid {ACCENT_LINE};
    font-weight: 600; }}
QPushButton#primary:hover, QPushButton[default="true"]:hover {{ background: {ACCENT_HOVER}; }}
QDialogButtonBox QPushButton:default {{ background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid {ACCENT_LINE};
    font-weight: 600; }}
QDialogButtonBox QPushButton:default:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: {BORDER}; border-color: {BORDER}; color: {MUTED}; }}
QPushButton#danger {{ color: {ERROR}; border-color: {BORDER}; }}
QPushButton#danger:hover {{ background: {RAISED}; border-color: {ERROR}; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {MUTED}; }}
QPushButton#ghost:hover {{ background: {PANEL_2}; color: {TEXT}; }}
QPushButton::menu-indicator {{ image: url({_A}/down.svg); subcontrol-position: right center; right: 6px; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 8px;
    color: {TEXT}; }}
QToolButton:hover {{ background: {PANEL_2}; border-color: {BORDER_SOFT}; }}
QToolButton:pressed, QToolButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT_LINE}; color: {ACCENT_TEXT}; }}
QToolButton:disabled {{ color: {DIM}; }}
QToolButton::menu-indicator {{ image: url({_A}/down.svg); subcontrol-position: right center; right: 4px; }}
QToolButton[popupMode="2"] {{ padding-right: 18px; }}
QToolButton#tool {{ padding: 4px 2px 3px 2px; min-width: 62px; font-size: 8.5pt; color: {ICON}; }}
QToolButton#tool:hover {{ color: {TEXT}; }}
QToolButton#view {{ padding: 3px 9px; color: {MUTED}; }}
QToolButton#view:hover {{ color: {TEXT}; }}
QWidget#toolbelt {{ background: {PANEL}; border-bottom: 1px solid {BORDER_SOFT}; }}
QWidget#toolbelt QTabWidget::pane {{ background: {PANEL}; }}
QWidget#toolbelt QWidget {{ background: {PANEL}; }}
QWidget#viewbar {{ background: {BG}; border-bottom: 1px solid {BORDER_SOFT}; }}
QLabel#sep {{ background: {BORDER_SOFT}; min-width: 1px; max-width: 1px; margin: 8px 6px; }}

/* ---------- inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QTimeEdit {{
    background: {INPUT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px; min-height: 18px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QDateEdit:hover, QPlainTextEdit:hover {{
    border-color: {HOVER_BORDER}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateEdit:focus {{ border-color: {ACCENT_LINE}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {DIM};
    background: {PANEL}; border-color: {BORDER_SOFT}; }}
QLineEdit[readOnly="true"] {{ background: {PANEL}; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ border: none; width: 20px; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url({_A}/down.svg); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {ACCENT_TEXT}; outline: none; }}
QComboBox QAbstractItemView::item {{ min-height: 24px; padding: 0 8px; }}
QSpinBox, QDoubleSpinBox, QDateEdit {{ padding-right: 20px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateEdit::up-button {{ subcontrol-origin: border;
    subcontrol-position: top right; width: 18px; border: none; border-left: 1px solid {BORDER_SOFT};
    border-top-right-radius: 6px; background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateEdit::down-button {{ subcontrol-origin: border;
    subcontrol-position: bottom right; width: 18px; border: none; border-left: 1px solid {BORDER_SOFT};
    border-bottom-right-radius: 6px; background: transparent; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::down-button:hover, QDateEdit::up-button:hover, QDateEdit::down-button:hover {{ background: {RAISED}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateEdit::up-arrow {{ image: url({_A}/up.svg); width: 8px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateEdit::down-arrow {{ image: url({_A}/down.svg); width: 8px;
    height: 8px; }}
QDateEdit::drop-down {{ border: none; width: 20px; }}
QDateEdit::down-arrow:on {{ top: 1px; }}
QCalendarWidget QWidget {{ background: {PANEL_2}; alternate-background-color: {PANEL}; }}
QCalendarWidget QToolButton {{ color: {TEXT}; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; border: 1px solid {HOVER_BORDER};
    background: {INPUT}; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT_LINE}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_LINE}; image: url({_A}/check.svg); }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_LINE}; image: url({_A}/dot.svg); }}
QCheckBox::indicator:disabled {{ background: {PANEL}; border-color: {BORDER_SOFT}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {DIM}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_LINE}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QProgressBar {{ background: {INPUT}; border: 1px solid {BORDER}; border-radius: 6px; text-align: center;
    color: {TEXT}; min-height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT_LINE}; border-radius: 5px; }}
QGroupBox {{ border: 1px solid {BORDER_SOFT}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; font-weight: 600; }}

/* ---------- lists, trees, tables */
QTreeWidget, QTreeView, QTableWidget, QTableView, QListWidget, QListView, QTextBrowser {{
    background: {PANEL}; border: 1px solid {BORDER_SOFT}; border-radius: 6px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {ACCENT_TEXT};
    alternate-background-color: {ROW_ALT}; gridline-color: {BORDER_SOFT}; }}
QTreeView::item, QListView::item {{ padding: 4px 4px; min-height: 22px; border: none; }}
QTreeView::item:hover, QListView::item:hover {{ background: {PANEL_2}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{ background: {ACCENT_SOFT};
    color: {ACCENT_TEXT}; }}
QTableView::item {{ padding: 2px 6px; border: none; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:has-children:closed {{ image: url({_A}/right.svg); }}
QTreeView::branch:has-children:open {{ image: url({_A}/open.svg); }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {PANEL_2}; color: {MUTED}; border: none; border-right: 1px solid {BORDER_SOFT};
    border-bottom: 1px solid {BORDER_SOFT}; padding: 5px 8px; font-weight: 600; font-size: 8.5pt; }}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {PANEL_2}; border: none; }}

/* ---------- scroll */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SCROLL}; border-radius: 3px; min-height: 28px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 3px; min-width: 28px; margin: 2px 0; }}
QScrollBar::handle:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ---------- text roles */
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 11pt; font-weight: 600; color: {TEXT}; }}
QLabel#h2 {{ font-size: 8.5pt; font-weight: 700; color: {MUTED}; letter-spacing: 0.6px; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#error {{ color: {ERROR}; }}
QLabel#hint {{ color: {MUTED}; font-size: 8.5pt; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER_SOFT}; border-radius: 8px; }}
"""


def apply(app):
    """Fusion + dark palette + stylesheet + proxy style. Safe to call once at start-up."""
    from PySide6.QtGui import QColor, QFont, QPalette
    from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

    small = QStyle.PM_SmallIconSize

    class _Style(QProxyStyle):
        # No styleHint override: Qt calls it tens of thousands of times while building the window, and a
        # Python override there costs ~0.15 s at startup plus overhead on every repaint.
        def pixelMetric(self, m, option=None, widget=None, _small=small):
            if m is _small or m == _small:
                return 16
            return super().pixelMetric(m, option, widget)

    app.setStyle(_Style(QStyleFactory.create("Fusion")))
    pal = QPalette()
    for role, c in ((QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, INPUT),
                    (QPalette.AlternateBase, PANEL), (QPalette.ToolTipBase, PANEL_2), (QPalette.ToolTipText, TEXT),
                    (QPalette.Text, TEXT), (QPalette.Button, PANEL_2), (QPalette.ButtonText, TEXT),
                    (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, ON_ACCENT),
                    (QPalette.Link, ACCENT_TEXT), (QPalette.PlaceholderText, DIM), (QPalette.Mid, BORDER),
                    (QPalette.Dark, BG), (QPalette.Light, RAISED)):
        pal.setColor(role, QColor(c))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(DIM))
    app.setPalette(pal)
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"])
    f.setPointSizeF(9.5)
    app.setFont(f)
    app.setStyleSheet(QSS)


__all__ = ["apply", "QSS", "os"]
