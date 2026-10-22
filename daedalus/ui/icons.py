"""One icon style across the app: Phosphor line icons (via qtawesome, MIT/OFL). If qtawesome isn't
installed, callers fall back to their text labels."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QIcon

from . import theme

# toolbelt / button label → Phosphor icon
TOOL = {
    # model
    "Doodle": "pen-nib", "Sketch": "pencil", "Extrude": "arrow-fat-lines-up", "Cut": "scissors",
    "Revolve": "circle-half-tilt", "Sweep": "path", "3D Sketch": "line-segments", "Frame": "frame-corners",
    "Hole": "target", "Fillet": "arrow-arc-right", "Chamfer": "triangle", "Shell": "selection",
    "Linear": "dots-three", "Circular": "circles-three", "Mirror": "arrows-left-right",
    # features
    "Plane": "square", "Axis": "arrows-down-up", "Loft": "stack-simple", "Rib": "git-branch", "Draft": "corners-in",
    "Thread": "nut", "Sheet metal": "note-blank", "Edge flange": "arrow-elbow-up-right",
    "Fold/Unfold": "arrows-in-line-vertical", "Flat DXF": "file-arrow-down", "Gusset": "triangle",
    # assembly
    "Object": "cube", "Move": "arrows-out-cardinal", "Rotate": "arrows-counter-clockwise",
    "Turntable": "disc", "Orient": "arrow-bend-double-up-right", "Drop": "align-bottom", "Face down": "arrow-line-down", "Mate": "link", "Drive": "play", "Interference": "intersect",
    "Explode": "arrows-out", "Library": "nut", "Std parts": "books", "Save std": "archive-tray",
    "Insert file": "file-plus", "Import": "download-simple", "Links": "arrows-clockwise", "BOM": "list-numbers", "Simulate": "atom",
    # simulation
    "Static": "barbell", "Frequency": "activity", "Buckling": "lightning", "Thermal": "thermometer",
    "Fatigue": "repeat", "Harmonic": "vibrate", "Flow": "wind", "Assembly": "squares-four",
    "Drop": "arrow-fat-line-down", "Topology": "tree-structure", "Design study": "sliders",
    "Material": "paint-bucket", "Run": "play-circle",
    # evaluate
    "Mass": "scales", "Measure": "ruler", "Cut list": "list-bullets", "Fit": "corners-out",
    # manufacture
    "CAM": "gear-six", "Facing": "rows", "Pocket": "square", "Profile": "path", "Drill": "circle-dashed",
    "G-code": "terminal", "Thickness": "arrows-in-line-horizontal", "Overhang": "sun-horizon", "Clear": "x-circle",
    # drawing
    "Drawing": "compass", "Sheets": "files", "Open 2D": "folder-open", "Flat dwg": "file-dotted",
    "Export opts": "sliders-horizontal",
    # inventory
    "Inventory": "package", "Send BOM": "paper-plane-right", "Stock check": "check-square-offset",
    "Reorder": "shopping-cart", "Shortages": "warning",
    # copilot
    "Ask": "sparkle", "Image": "image",
    # view tools (hub "View" group, view wheel)
    "Front": "square", "Back": "square", "Left": "arrow-fat-line-left", "Right": "arrow-fat-line-right",
    "Top": "arrow-fat-line-up", "Bottom": "arrow-fat-line-down", "Iso": "cube", "Normal to": "crosshair",
    "Previous": "arrow-u-up-left", "Shaded+E": "cube", "Shaded": "circle-half", "Uncoloured": "circle",
    "Hidden line": "polygon", "Wireframe": "grid-four", "Translucent": "drop", "Section": "scissors",
    "Isolate": "target", "Quote": "currency-dollar", "Backplot": "play", "Auto CAM": "lightning",
    # chrome
    "home": "house", "snaps": "magnet", "views": "cube", "layer": "stack", "material": "paint-bucket", "display": "eye",
    "search": "magnifying-glass", "close": "x", "add": "plus", "delete": "trash", "more": "dots-three-vertical",
    "open": "arrow-square-out", "undo": "arrow-counter-clockwise", "redo": "arrow-clockwise", "save": "floppy-disk",
    "export": "export", "send": "paper-plane-right", "stop": "stop-circle", "attach": "image",
    "new chat": "chats-circle", "key": "key", "back": "caret-left", "help": "question",
    "eye": "eye", "lock": "lock-simple", "anchor": "anchor-simple", "power": "power", "file": "file-text",
    "folder": "folder-open", "photo": "image", "doodle": "pen-nib",
}

VIEW = {"Isometric": "cube", "Front": "square", "Top": "square", "Right": "square", "Back": "square",
        "Bottom": "square", "Left": "square"}


_FONT: list = []        # [family, charmap] once loaded


def _phosphor():
    """Load the Phosphor font + charmap straight from the installed qtawesome package files, without importing
    qtawesome (its qtpy import promotes every Qt enum — ~0.2 s at start-up)."""
    if _FONT:
        return _FONT[0]
    try:
        import importlib.util
        import json
        from pathlib import Path

        from PySide6.QtGui import QFontDatabase
        spec = importlib.util.find_spec("qtawesome")
        if spec is None or not spec.submodule_search_locations:
            raise ImportError
        fonts = Path(list(spec.submodule_search_locations)[0]) / "fonts"
        ttf = sorted(fonts.glob("phosphor-*.ttf"))[-1]
        cmap = json.loads(sorted(fonts.glob("phosphor-charmap-*.json"))[-1].read_text())
        fid = QFontDatabase.addApplicationFont(str(ttf))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if not fams:
            raise ImportError
        _FONT.append((fams[0], {k: int(v, 16) if isinstance(v, str) else int(v) for k, v in cmap.items()}))
    except Exception:  # noqa: BLE001
        _FONT.append(None)
    return _FONT[0]


@lru_cache(maxsize=1)
def available() -> bool:
    if _phosphor() is not None:
        return True
    try:
        import qtawesome  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _glyph_icon(family, code, color, active, disabled):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QIconEngine, QPainter, QPixmap

    class _Engine(QIconEngine):
        def paint(self, painter, rect, mode, state):
            col = {QIcon.Mode.Active: active, QIcon.Mode.Selected: active, QIcon.Mode.Disabled: disabled}.get(
                mode, color)
            f = QFont(family)
            f.setPixelSize(max(1, round(rect.height())))
            painter.save()
            painter.setFont(f)
            painter.setPen(QColor(col))
            painter.drawText(QRect(rect), int(Qt.AlignCenter), chr(code))
            painter.restore()

        def pixmap(self, size, mode, state):
            pm = QPixmap(size)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            self.paint(p, QRect(0, 0, size.width(), size.height()), mode, state)
            p.end()
            return pm

        def clone(self):
            return _Engine()

    return QIcon(_Engine())


def header_icons(tree, cols: dict):
    """Replace header glyph text with icons: {column: key}."""
    if not available():
        return
    hi = tree.headerItem()
    for c, key in cols.items():
        hi.setText(c, "")
        hi.setIcon(c, icon(key, theme.MUTED))
        hi.setToolTip(c, {"eye": "Visible", "lock": "Locked", "anchor": "Fixed (ground)",
                          "power": "In model"}.get(key, key))


@lru_cache(maxsize=512)
def icon(key: str, color: str | None = None, active: str | None = None) -> QIcon:
    """Icon for a tool label or chrome key; empty QIcon when unknown or qtawesome is missing."""
    name = TOOL.get(key, key)
    if not available() or not name:
        return QIcon()
    font = _phosphor()
    if font is not None:
        code = font[1].get(name)
        if code is None:
            return QIcon()
        return _glyph_icon(font[0], code, color or theme.ICON, active or theme.ACCENT_LINE, theme.BORDER)
    import qtawesome as qta
    try:
        return qta.icon(f"ph.{name}", color=color or theme.ICON, color_active=active or theme.ACCENT_LINE,
                        color_disabled=theme.BORDER)
    except Exception:  # noqa: BLE001
        return QIcon()
