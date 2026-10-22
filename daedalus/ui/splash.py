"""Start-up splash: drawing + a small editor so the user can restyle it.

Only PySide is imported here — the splash is shown before the CAD kernel loads. Settings live in QSettings
under ``splash/*``; a custom image (PNG/JPG/SVG) replaces the drawing entirely.
"""
from __future__ import annotations

import json

W, H = 760, 368

DEFAULTS = {
    "bg": "#000000",            # outside the disc
    "disc": "#0A3A36",          # the disc
    "ink": "#E4EFEC",           # the symbol
    "status": "#6F8D89",        # loading text
    "glyph": "δ",
    "font": "Cambria",
    "italic": True,
    "bold": False,
    "size": 150,                # symbol height, % of the splash height (oversized: cropped by the disc)
    "dx": 3,                    # symbol offset, % of width / height
    "dy": 14,
    "rotate": 0,                # degrees
    "disc_r": 71,               # disc radius, % of height
    "disc_x": 50,               # disc centre, % of width / height
    "disc_y": 41,
    "clip": True,               # symbol only inside the disc
    "image": "",                # custom splash image (overrides the drawing)
}
FONT_FALLBACK = ["Cambria Math", "Cambria", "Georgia", "Times New Roman", "DejaVu Serif", "serif"]


def load(settings=None) -> dict:
    cfg = dict(DEFAULTS)
    try:
        if settings is None:
            from PySide6.QtCore import QSettings
            settings = QSettings("Daedalus", "Daedalus")
        raw = settings.value("splash/config", "")
        if raw:
            cfg.update({k: v for k, v in json.loads(raw).items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save(cfg: dict, settings=None):
    if settings is None:
        from PySide6.QtCore import QSettings
        settings = QSettings("Daedalus", "Daedalus")
    diff = {k: v for k, v in cfg.items() if k in DEFAULTS and v != DEFAULTS[k]}
    settings.setValue("splash/config", json.dumps(diff))


def render(cfg: dict | None = None, dpr: float = 1.0, size=(W, H)):
    """The splash as a QPixmap."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap, QTransform
    cfg = {**DEFAULTS, **(cfg or {})}
    w, h = size
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(cfg["bg"]))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    img_path = cfg.get("image") or ""
    if img_path:
        img = QImage(img_path)
        if not img.isNull():
            sc = img.scaled(int(w * dpr), int(h * dpr), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx, sy = (sc.width() - w * dpr) / 2, (sc.height() - h * dpr) / 2
            p.drawImage(QRectF(0, 0, w, h), sc, QRectF(sx, sy, w * dpr, h * dpr))
            p.end()
            return pm
    frame = QPainterPath()
    frame.addRect(QRectF(0, 0, w, h))
    cx, cy, r = w * cfg["disc_x"] / 100, h * cfg["disc_y"] / 100, h * cfg["disc_r"] / 100
    disc = QPainterPath()
    disc.addEllipse(QPointF(cx, cy), r, r)
    disc = disc.intersected(frame)
    p.fillPath(disc, QColor(cfg["disc"]))
    text = str(cfg.get("glyph") or "")
    if text:
        f = QFont()
        f.setFamilies([cfg.get("font") or FONT_FALLBACK[0], *FONT_FALLBACK])
        f.setItalic(bool(cfg.get("italic")))
        f.setBold(bool(cfg.get("bold")))
        f.setPixelSize(max(4, int(h * float(cfg["size"]) / 100)))
        glyph = QPainterPath()
        glyph.addText(0, 0, f, text)
        gb = glyph.boundingRect()
        glyph.translate(-gb.center().x(), -gb.center().y())
        t = QTransform()
        t.translate(cx + w * float(cfg["dx"]) / 100, cy + h * float(cfg["dy"]) / 100)
        t.rotate(float(cfg.get("rotate") or 0))
        glyph = t.map(glyph)
        p.save()
        p.setClipPath(disc if cfg.get("clip", True) else frame)
        p.fillPath(glyph, QColor(cfg["ink"]))
        p.restore()
    p.end()
    return pm


def logo_pixmap(size: int = 64, dpr: float = 1.0, ring: str | None = None):
    """The Daedalus mark as a square, transparent QPixmap: the teal disc with the oversized δ cropped by it —
    the splash symbol, same proportions. ring: optional outline colour."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QTransform
    c = DEFAULTS
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r = size / 2 - (1.0 if ring else 0.5)
    cx = cy = size / 2
    disc = QPainterPath()
    disc.addEllipse(QPointF(cx, cy), r, r)
    p.fillPath(disc, QColor(c["disc"]))
    k = r / float(c["disc_r"])                       # splash units (% of height) → px
    f = QFont()
    f.setFamilies([c["font"], *FONT_FALLBACK])
    f.setItalic(bool(c["italic"]))
    f.setPixelSize(max(4, int(float(c["size"]) * k)))
    g = QPainterPath()
    g.addText(0, 0, f, c["glyph"])
    gb = g.boundingRect()
    g.translate(-gb.center().x(), -gb.center().y())
    t = QTransform()
    t.translate(cx + float(c["dx"]) * W / H * k, cy + float(c["dy"]) * k)
    g = t.map(g)
    p.save()
    p.setClipPath(disc)
    p.fillPath(g, QColor(c["ink"]))
    p.restore()
    if ring:
        p.setPen(QPen(QColor(ring), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    p.end()
    return pm


def logo_icon():
    """Window / taskbar icon in the sizes Windows asks for."""
    from PySide6.QtGui import QIcon
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(logo_pixmap(s))
    return ic


class Splash:
    """The QSplashScreen shown at start-up."""

    def __init__(self, version=""):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication, QSplashScreen
        self.cfg = load()
        scr = QApplication.primaryScreen()
        self.w = QSplashScreen(render(self.cfg, scr.devicePixelRatio() if scr else 1.0))
        self.w.show()
        self.version = version
        self._col = QColor(self.cfg["status"])

    def showMessage(self, msg):
        from PySide6.QtCore import Qt
        self.w.showMessage(msg + "   ", Qt.AlignBottom | Qt.AlignRight, self._col)

    def finish(self, w):
        self.w.finish(w)


def editor(parent=None):
    """Dialog to restyle the splash with a live preview."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import (
        QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QFontComboBox, QFormLayout,
        QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget,
    )

    dlg = QDialog(parent)
    dlg.setWindowTitle("Splash screen")
    cfg = load()
    lay = QVBoxLayout(dlg)
    preview = QLabel()
    preview.setAlignment(Qt.AlignCenter)
    preview.setMinimumSize(W // 2 + 20, H // 2 + 20)
    lay.addWidget(preview)
    form = QFormLayout()
    lay.addLayout(form)
    widgets = {}

    def refresh():
        pm = render(cfg, 1.0, (W, H))
        preview.setPixmap(pm.scaled(int(W * 0.66), int(H * 0.66), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def slider(key, lo, hi, label):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(cfg[key]))
        val = QLabel(str(int(cfg[key])))
        val.setFixedWidth(36)
        s.valueChanged.connect(lambda v: (cfg.__setitem__(key, v), val.setText(str(v)), refresh()))
        h.addWidget(s, 1)
        h.addWidget(val)
        form.addRow(label, row)
        widgets[key] = s

    def color(key, label):
        b = QPushButton()
        b.setFixedWidth(110)

        def paint():
            c = QColor(cfg[key])
            b.setText(c.name().upper())
            b.setStyleSheet(f"background:{c.name()}; color:{'#000' if c.lightness() > 140 else '#fff'};")

        def pick():
            c = QColorDialog.getColor(QColor(cfg[key]), dlg, label)
            if c.isValid():
                cfg[key] = c.name()
                paint()
                refresh()
        b.clicked.connect(pick)
        paint()
        form.addRow(label, b)
        widgets[key] = (b, paint)

    glyph = QLineEdit(cfg["glyph"])
    glyph.setMaxLength(8)
    glyph.setPlaceholderText("δ")
    glyph.textChanged.connect(lambda t: (cfg.__setitem__("glyph", t), refresh()))
    form.addRow("Symbol", glyph)
    font = QFontComboBox()
    font.setCurrentFont(QFont(cfg["font"]))
    font.currentFontChanged.connect(lambda f: (cfg.__setitem__("font", f.family()), refresh()))
    style = QWidget()
    sh = QHBoxLayout(style)
    sh.setContentsMargins(0, 0, 0, 0)
    sh.addWidget(font, 1)
    for key, text in (("italic", "Italic"), ("bold", "Bold"), ("clip", "Inside disc only")):
        cb = QCheckBox(text)
        cb.setChecked(bool(cfg[key]))
        cb.toggled.connect(lambda on, k=key: (cfg.__setitem__(k, on), refresh()))
        sh.addWidget(cb)
        widgets[key] = cb
    form.addRow("Typeface", style)
    slider("size", 20, 260, "Symbol size")
    slider("dx", -60, 60, "Symbol ← →")
    slider("dy", -60, 60, "Symbol ↑ ↓")
    slider("rotate", -45, 45, "Symbol tilt")
    slider("disc_r", 10, 120, "Disc size")
    slider("disc_x", 0, 100, "Disc ← →")
    slider("disc_y", 0, 100, "Disc ↑ ↓")
    color("disc", "Disc colour")
    color("bg", "Background")
    color("ink", "Symbol colour")
    color("status", "Loading text")
    img_row = QWidget()
    ih = QHBoxLayout(img_row)
    ih.setContentsMargins(0, 0, 0, 0)
    img = QLineEdit(cfg["image"])
    img.setPlaceholderText("none — use the drawing above")
    img.textChanged.connect(lambda t: (cfg.__setitem__("image", t.strip()), refresh()))
    browse = QPushButton("Browse…")
    browse.clicked.connect(lambda: (lambda pth: img.setText(pth) if pth else None)(
        QFileDialog.getOpenFileName(dlg, "Splash image", "", "Images (*.png *.jpg *.jpeg *.bmp *.svg)")[0]))
    ih.addWidget(img, 1)
    ih.addWidget(browse)
    form.addRow("Own image", img_row)
    hint = QLabel(f"Images are cropped to fill {W}×{H}. Changes show at the next start.")
    hint.setObjectName("hint")
    lay.addWidget(hint)
    bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)

    def reset():
        cfg.clear()
        cfg.update(DEFAULTS)
        glyph.setText(cfg["glyph"])
        font.setCurrentFont(QFont(cfg["font"]))
        img.setText("")
        for k, wdg in widgets.items():
            if isinstance(wdg, QSlider):
                wdg.setValue(int(cfg[k]))
            elif isinstance(wdg, QCheckBox):
                wdg.setChecked(bool(cfg[k]))
            else:
                wdg[1]()
        refresh()
    bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(reset)
    bb.accepted.connect(lambda: (save(cfg), dlg.accept()))
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    refresh()
    dlg.cfg = cfg
    return dlg
