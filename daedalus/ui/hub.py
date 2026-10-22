"""Tool hub: a draggable delta (δ) floating over the viewport.

Click the δ to fan out a ring of categories (Model, Features, Assembly, …). Click a category to open its toolbar:
toolbars are pinned to the hub, follow it when it is dragged, and stay open until closed with their ×
(or by clicking the category again). Up to three are open at once; opening a fourth replaces the oldest.
The ribbon toolbelt is unchanged; the hub reuses the same actions.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QRegion, QTransform
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QMenu, QToolButton, QToolTip, QVBoxLayout, QWidget

from . import icons, theme

CAT_ICON = {"Model": "cube", "Features": "stack-simple", "Assembly": "squares-four", "Simulation": "atom",
            "Evaluate": "ruler", "Manufacture": "gear-six", "Drawing": "compass", "Inventory": "package",
            "Icarus": "sparkle", "View": "eye"}

D_R = 30          # hub disk radius
RING_R = 72       # ring radius (centre to category centre)
CAT_R = 19        # category button radius
PAD = 6
MAX_BARS = 3

_FLAGS = Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus


def d_path(size: float) -> QPainterPath:
    """Hub glyph: a small italic delta (δ), centred on (0, 0) and `size` tall."""
    from PySide6.QtGui import QFont
    f = QFont()
    f.setFamilies(["Cambria", "Georgia", "Segoe UI", "DejaVu Serif", "Times New Roman"])
    f.setPixelSize(100)
    f.setBold(True)
    f.setItalic(True)
    p = QPainterPath()
    p.addText(0, 0, f, "δ")
    r = p.boundingRect()
    k = size / max(r.height(), 1e-6)
    t = QTransform()
    t.scale(k, k)
    t.translate(-r.center().x(), -r.center().y())
    return t.map(p)


def _c(hexstr, a=255):
    c = QColor(hexstr)
    c.setAlpha(a)
    return c


def _ease_out_back(x: float) -> float:
    c1 = 1.45
    c3 = c1 + 1
    x = min(max(x, 0.0), 1.0)
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _label_font():
    from PySide6.QtGui import QFont
    f = QFont()
    f.setPointSizeF(8.5)
    f.setWeight(QFont.DemiBold)
    return f


# ------------------------------------------------------------------ the δ + ring
class HubDisk(QWidget):
    LABEL_W = 92                  # room either side for the hover label
    LABEL_H = 30                  # room above/below for the hover label
    STAGGER = 0.035               # per-category delay in the fan-out

    def __init__(self, ctl, names):
        super().__init__(ctl.parent_widget, ctl.flags)
        self.ctl = ctl
        self.names = names
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        r = RING_R + CAT_R + PAD
        self.setFixedSize(2 * (r + self.LABEL_W), 2 * (r + self.LABEL_H))
        self._t = 0.0                 # ring fan-out 0..1
        self.expanded = False
        self._hover = None            # "D" | category name | None
        self._down = None             # item under a held mouse button
        self._press = None
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.valueChanged.connect(self._tick)
        self._anim.finished.connect(self._update_mask)
        self._anim.finished.connect(lambda: self.ctl.reposition())
        self._update_mask()

    # geometry
    def centre(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    def _ti(self, i) -> float:
        """Per-category progress (staggered) 0..1."""
        span = 1 - self.STAGGER * (len(self.names) - 1)
        return min(max((self._t - self.STAGGER * i) / span, 0.0), 1.0)

    def cat_pos(self, i, t=None) -> QPointF:
        t = _ease_out_back(self._ti(i)) if t is None else t
        a = math.radians(-90 + i * 360 / len(self.names))
        c = self.centre()
        return QPointF(c.x() + RING_R * t * math.cos(a), c.y() + RING_R * t * math.sin(a))

    def label_rect(self, i) -> QRectF:
        from PySide6.QtGui import QFontMetricsF
        fm = QFontMetricsF(_label_font())
        w, h = fm.horizontalAdvance(self.names[i]) + 18, fm.height() + 8
        a = math.radians(-90 + i * 360 / len(self.names))
        q = self.cat_pos(i, 1.0)
        ca, sa = math.cos(a), math.sin(a)
        gap = CAT_R + 6
        if abs(ca) > 0.35:            # left / right of the button
            x = q.x() + gap if ca > 0 else q.x() - gap - w
            return QRectF(x, q.y() - h / 2, w, h)
        y = q.y() + gap if sa > 0 else q.y() - gap - h
        return QRectF(q.x() - w / 2, y, w, h)

    def item_at(self, p: QPointF):
        c = self.centre()
        if math.hypot(p.x() - c.x(), p.y() - c.y()) <= D_R + 1:
            return "D"
        if self._t > 0.6:
            for i, n in enumerate(self.names):
                q = self.cat_pos(i)
                if math.hypot(p.x() - q.x(), p.y() - q.y()) <= CAT_R + 2:
                    return n
        return None

    def set_expanded(self, on: bool, animate=True):
        self.expanded = on
        self._anim.stop()
        if animate:
            self._anim.setStartValue(self._t)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.setDuration(240 if on else 150)
            self._update_mask(full=True)
            self._anim.start()
        else:
            self._t = 1.0 if on else 0.0
            self._update_mask()
            self.update()
            self.ctl.reposition()

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def _update_mask(self, full=False):
        """Clicks outside the drawn shapes fall through to the viewport."""
        if full:
            self.setMask(QRegion(self.rect()))
            return
        c = self.centre()
        g = 5                                          # room for the soft shadow
        reg = QRegion(int(c.x() - D_R - g), int(c.y() - D_R - g), 2 * (D_R + g), 2 * (D_R + g), QRegion.Ellipse)
        if self._t > 0.01:
            for i, n in enumerate(self.names):
                q = self.cat_pos(i)
                reg = reg.united(QRegion(int(q.x() - CAT_R - g), int(q.y() - CAT_R - g), 2 * (CAT_R + g),
                                         2 * (CAT_R + g), QRegion.Ellipse))
                if n == self._hover:
                    reg = reg.united(QRegion(self.label_rect(i).adjusted(-2, -2, 2, 3).toAlignedRect()))
        self.setMask(reg)

    # painting
    @staticmethod
    def _shadow(p, centre, r):
        p.setPen(Qt.NoPen)
        for k, a in ((4, 18), (2.5, 34), (1.2, 52)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawEllipse(centre + QPointF(0, 1.5), r + k, r + k)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        c = self.centre()
        if self._t > 0.01:
            # faint guide ring tying the categories to the hub
            p.setOpacity(min(1.0, self._t) * 0.55)
            p.setPen(QPen(_c(theme.BORDER), 1, Qt.DotLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, RING_R * min(1.0, self._t), RING_R * min(1.0, self._t))
            sz = 18
            for i, n in enumerate(self.names):
                ti = self._ti(i)
                if ti <= 0.0:
                    continue
                q = self.cat_pos(i)
                is_open = n in self.ctl.bars
                hov = self._hover == n
                r = CAT_R * (0.9 if self._down == n else 1.0)
                p.setOpacity(min(1.0, ti * 1.6))
                self._shadow(p, q, r)
                if is_open:
                    pen, fill, icol = QPen(_c(theme.ACCENT_LINE), 2), _c(theme.ACCENT_SOFT, 250), theme.ACCENT_LINE
                    if hov:
                        fill = _c(theme.ACCENT_SOFT).lighter(130)
                elif hov:
                    pen, fill, icol = QPen(_c(theme.HOVER_BORDER), 1.4), _c(theme.RAISED, 250), theme.TEXT
                else:
                    pen, fill, icol = QPen(_c(theme.BORDER), 1.2), _c(theme.PANEL, 245), theme.ICON
                p.setPen(pen)
                p.setBrush(fill)
                p.drawEllipse(q, r, r)
                ic = icons.icon(CAT_ICON.get(n, "dot"), icol)
                if not ic.isNull():
                    p.drawPixmap(QRectF(q.x() - sz / 2, q.y() - sz / 2, sz, sz).toRect(), ic.pixmap(sz * 2, sz * 2))
                else:
                    p.setPen(_c(icol))
                    p.drawText(QRectF(q.x() - r, q.y() - r, 2 * r, 2 * r), Qt.AlignCenter, n[:2])
                if hov and self._t >= 0.99:
                    lr = self.label_rect(i)
                    p.setOpacity(1.0)
                    p.setPen(QPen(_c(theme.BORDER), 1))
                    p.setBrush(_c(theme.PANEL, 250))
                    p.drawRoundedRect(lr, lr.height() / 2, lr.height() / 2)
                    p.setFont(_label_font())
                    p.setPen(_c(theme.ACCENT_LINE if is_open else theme.TEXT))
                    p.drawText(lr, Qt.AlignCenter, n)
            p.setOpacity(1.0)
        hov = self._hover == "D"
        r = D_R * (0.94 if self._down == "D" else 1.0)
        self._shadow(p, c, r)
        p.setPen(QPen(_c(theme.ACCENT_LINE), 2.0 if self.expanded else 1.5))
        p.setBrush(_c(theme.RAISED if hov else theme.PANEL, 250))
        p.drawEllipse(c, r, r)
        glyph = d_path(r * 1.15)
        glyph.translate(c)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(theme.ACCENT_TEXT if hov else theme.ACCENT_LINE))
        p.drawPath(glyph)

    # mouse
    def mousePressEvent(self, e):
        it = self.item_at(e.position())
        if e.button() == Qt.RightButton and it == "D":
            self._menu(e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        self._press = (it, e.globalPosition().toPoint(), self.pos(), False)
        self._down = it
        self.update()

    def mouseMoveEvent(self, e):
        gp = e.globalPosition().toPoint()
        if self._press and self._press[0] == "D":
            it, g0, w0, dragging = self._press
            if dragging or (gp - g0).manhattanLength() > 4:
                if not dragging:
                    self._down = None
                    QToolTip.hideText()
                self._press = (it, g0, w0, True)
                self.ctl.move_hub(w0 + (gp - g0))
                self.setCursor(Qt.ClosedHandCursor)
            return
        h = self.item_at(e.position())
        if h != self._hover:
            self._hover = h
            self._update_mask()
            self.update()
            if h == "D":
                QToolTip.showText(gp, "Click: categories · Drag: move · Right-click: options", self)
            else:
                QToolTip.hideText()
        self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        self._down = None
        self.update()
        if not self._press:
            return
        it, _g0, _w0, dragging = self._press
        self._press = None
        self.setCursor(Qt.OpenHandCursor if self._hover == "D" else Qt.PointingHandCursor)
        if dragging:
            self.ctl.save()
            return
        rel = self.item_at(e.position())
        if rel != it:
            return
        if it == "D":
            self.set_expanded(not self.expanded)
            self.ctl.save()
        elif it:
            self.ctl.toggle(it)

    def leaveEvent(self, _e):
        if self._hover or self._down:
            self._hover = self._down = None
            self._update_mask()
            self.update()

    def _menu(self, gp):
        m = QMenu(self)
        m.addAction("Collapse categories" if self.expanded else "Expand categories",
                    lambda: (self.set_expanded(not self.expanded), self.ctl.save()))
        m.addAction("Close all toolbars", self.ctl.close_all).setEnabled(bool(self.ctl.bars))
        m.addSeparator()
        main = self.ctl.main
        if hasattr(main, "set_claude_visible"):
            on = main.copilot_dock.isVisible()
            m.addAction("Hide Icarus" if on else "Show Icarus", lambda: main.set_claude_visible(not on))
        m.addAction("Reset position", self.ctl.reset_position)
        m.addAction("Hide hub  (Ctrl+Shift+H brings it back)", lambda: self.ctl.set_enabled(False))
        m.exec(gp)


# ------------------------------------------------------------------ pinned toolbar
SH = 8            # soft-shadow margin around a toolbar


class HubBar(QWidget):
    BTN_H = QSize(76, 54)         # horizontal: icon over label
    BTN_V = QSize(144, 30)        # vertical: icon beside label
    PER_ROW = 8
    PER_COL = 10

    def __init__(self, ctl, name, buttons, vertical=False):
        super().__init__(ctl.parent_widget, ctl.flags)
        from PySide6.QtGui import QFontMetrics
        self.ctl = ctl
        self.name = name
        self.vertical = vertical
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("hubbar")
        v = QVBoxLayout(self)
        v.setContentsMargins(SH + 8, SH + 6, SH + 6, SH + 8)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 0, 0)
        head.setSpacing(6)
        ic = icons.icon(CAT_ICON.get(name, "dot"), theme.ACCENT_LINE)
        if not ic.isNull():
            glyph = QLabel()
            glyph.setPixmap(ic.pixmap(28, 28).scaled(14, 14, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            head.addWidget(glyph)
        ttl = QLabel(name)
        ttl.setObjectName("hubtitle")
        head.addWidget(ttl)
        head.addStretch(1)
        x = QToolButton()
        x.setObjectName("hubclose")
        x.setAutoRaise(True)
        x.setToolTip(f"Close {name} toolbar")
        cic = icons.icon("close", theme.MUTED, theme.TEXT)
        if cic.isNull():
            x.setText("×")
        else:
            x.setIcon(cic)
            x.setIconSize(QSize(11, 11))
        x.setFixedSize(20, 20)
        x.clicked.connect(lambda: ctl.close(name))
        head.addWidget(x)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(2)
        tools = [b for b in buttons if b[0] is not None]
        size = self.BTN_V if vertical else self.BTN_H
        for k, (label, glyph_txt, tip, fn) in enumerate(tools):
            b = QToolButton()
            b.setAutoRaise(True)
            b.setObjectName("hubtool")
            b.setFixedSize(size)
            fm = QFontMetrics(b.font())
            room = size.width() - (34 if vertical else 6)
            text = fm.elidedText(label, Qt.ElideRight, room)
            tic = icons.icon(label)
            if not tic.isNull():
                b.setIcon(tic)
                b.setIconSize(QSize(18, 18) if vertical else QSize(20, 20))
                b.setText(text)
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if vertical else Qt.ToolButtonTextUnderIcon)
            else:
                b.setText(f"{glyph_txt}  {text}" if vertical else f"{glyph_txt}\n{text}")
            b.setToolTip(f"<b>{label}</b><br>{tip}")
            b.clicked.connect(fn)
            if vertical:
                grid.addWidget(b, k % self.PER_COL, k // self.PER_COL)
            else:
                grid.addWidget(b, k // self.PER_ROW, k % self.PER_ROW)
        v.addLayout(grid)
        self.setStyleSheet(
            f"QLabel#hubtitle {{ color:{theme.TEXT}; font-size:9pt; font-weight:600; }}"
            f"QToolButton#hubclose {{ border:none; border-radius:10px; }}"
            f"QToolButton#hubclose:hover {{ background:{theme.RAISED}; }}"
            f"QToolButton#hubtool {{ color:{theme.ICON}; font-size:8pt; padding:3px 4px; border:none;"
            f" border-radius:7px; {'text-align:left;' if vertical else ''} }}"
            f"QToolButton#hubtool:hover {{ color:{theme.TEXT}; background:{theme.RAISED}; }}"
            f"QToolButton#hubtool:pressed {{ color:{theme.ACCENT_LINE}; background:{theme.ACCENT_SOFT}; }}")
        self.adjustSize()
        self._fade = None

    def showEvent(self, e):
        super().showEvent(e)
        a = QVariantAnimation(self)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(140)
        a.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
        self.setWindowOpacity(0.0)
        a.start()
        self._fade = a

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(SH, SH, -SH, -SH)
        p.setPen(Qt.NoPen)
        for k in range(SH, 0, -1):                       # soft drop shadow
            p.setBrush(QColor(0, 0, 0, int(5 + 22 * (1 - k / SH) ** 2)))
            p.drawRoundedRect(body.adjusted(-k, -k + 2, k, k + 2), 10 + k, 10 + k)
        p.setPen(QPen(_c(theme.BORDER), 1))
        p.setBrush(_c(theme.PANEL, 250))
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)


# ------------------------------------------------------------------ controller
class HubController(QObject):
    """Owns the hub disk and its toolbars; keeps them glued to the viewport."""

    def __init__(self, main, view, groups, settings, floating=False, area_fn=None, key="hub", ring=None):
        """floating: not tied to a Daedalus window — stays on top of other apps (SolidWorks Link); area_fn()
        gives the global rectangle to keep it in (default: the viewport, or the screen when floating)."""
        super().__init__(main)
        self.main, self.view, self.settings = main, view, settings
        self.floating, self.area_fn, self.key = floating, area_fn, key
        self.parent_widget = None if floating else main
        self.flags = _FLAGS | (Qt.WindowStaysOnTopHint if floating else Qt.WindowType(0))
        self.groups = dict(groups)
        self.bars: dict[str, HubBar] = {}
        self.order: list[str] = []           # slot order, oldest first
        self.enabled = settings.value(f"{key}/enabled", True, type=bool)
        self.disk = HubDisk(self, ring or [n for n, _ in groups if n != "Icarus"])
        off = settings.value(f"{key}/offset")
        self.offset = QPoint(int(off[0]), int(off[1])) if isinstance(off, (list, tuple)) and len(off) == 2 else None
        self.disk.set_expanded(settings.value(f"{key}/expanded", False, type=bool), animate=False)
        for n in settings.value(f"{key}/open", [], type=list) or []:
            if n in self.groups and n in self.disk.names:
                self._open(n, save=False)
        if main is not None:
            main.installEventFilter(self)
        if view is not None:
            view.installEventFilter(self)
        stack = getattr(main, "stack", None)
        if stack is not None:
            stack.currentChanged.connect(lambda _i: self.sync())

    # placement
    def _area(self) -> QRect:
        """Viewport rectangle in global coordinates (limited to the screen it is on)."""
        if self.area_fn is not None:
            return self.area_fn()
        if self.view is None:
            from PySide6.QtGui import QGuiApplication
            return QGuiApplication.primaryScreen().availableGeometry()
        r = QRect(self.view.mapToGlobal(QPoint(0, 0)), self.view.size())
        scr = self.view.screen()
        return r.intersected(scr.availableGeometry()) if scr is not None else r

    def _default_offset(self):
        if self.floating:                                  # right-hand side of the host window
            a = self._area()
            return QPoint(max(120, a.width() - 150), max(120, a.height() // 2))
        return QPoint(110, 110)

    def move_hub(self, top_left_global: QPoint):
        a = self._area()
        w, h = self.disk.width(), self.disk.height()
        # keep the D itself inside the viewport (the ring may overhang)
        cx = min(max(top_left_global.x() + w // 2, a.left() + D_R + 4), a.right() - D_R - 4)
        cy = min(max(top_left_global.y() + h // 2, a.top() + D_R + 4), a.bottom() - D_R - 4)
        self.offset = QPoint(cx - a.left(), cy - a.top())
        self.reposition()

    def reposition(self):
        if not self.disk.isVisible():
            return
        a = self._area()
        off = self.offset or self._default_offset()
        cx = min(max(a.left() + off.x(), a.left() + D_R + 4), a.right() - D_R - 4)
        cy = min(max(a.top() + off.y(), a.top() + D_R + 4), a.bottom() - D_R - 4)
        self.disk.move(cx - self.disk.width() // 2, cy - self.disk.height() // 2)
        self._layout_bars(QPoint(cx, cy), a)

    def _layout_bars(self, c: QPoint, a: QRect):
        """Slot 0 above-right, slot 1 below-right, slot 2 vertical on the left; each flips to the other side
        when it would leave the area, and the two horizontal bars never take the same side."""
        ro = (RING_R + CAT_R + 4 if self.disk.expanded else D_R + 6) - SH
        taken = None                                       # side used by slot 0: "above" | "below"
        placed: list[QRect] = []
        for slot, name in enumerate(self.order):
            bar = self.bars[name]
            bar.adjustSize()
            w, h = bar.width(), bar.height()
            if slot in (0, 1):
                x = c.x() + 4 - SH
                if x + w > a.right():
                    x = c.x() - 4 - w + SH
                above, below = c.y() - ro - h, c.y() + ro
                fits_above, fits_below = above >= a.top(), below + h <= a.bottom()
                want = "above" if slot == 0 else "below"
                if slot == 1 and taken == "below":
                    want = "above"
                if want == "above" and not fits_above and (fits_below and taken != "below"):
                    want = "below"
                elif want == "below" and not fits_below and (fits_above and taken != "above"):
                    want = "above"
                y = above if want == "above" else below
                if slot == 0:
                    taken = want
            else:
                x, y = c.x() - ro - w, c.y() - 30
                if x < a.left():
                    x = c.x() + ro
            x, y = self._clear_of(QRect(x, y, w, h), placed, c, ro, a)
            placed.append(QRect(x, y, w, h).adjusted(SH, SH, -SH, -SH))
            bar.move(x, y)

    @staticmethod
    def _clear_of(r: QRect, placed, c: QPoint, ro: int, a: QRect):
        """Clamp into the area and step clear of bars already placed (left/right of the ring, then further
        out above/below)."""
        def clamp(x, y):
            return (min(max(x, a.left() + 4), max(a.left() + 4, a.right() - r.width() - 4)),
                    min(max(y, a.top() + 4), max(a.top() + 4, a.bottom() - r.height() - 4)))

        def free(x, y):
            body = QRect(x, y, r.width(), r.height()).adjusted(SH, SH, -SH, -SH)
            return not any(body.intersects(p) for p in placed)
        w, h = r.width(), r.height()
        cands = [(r.x(), r.y()), (c.x() - ro - w, c.y() - h // 2), (c.x() + ro, c.y() - h // 2)]
        if placed:
            lo = max(p.bottom() for p in placed)
            hi = min(p.top() for p in placed)
            cands += [(r.x(), lo + 4 - SH), (r.x(), hi - h - 4 + SH)]
        for x, y in cands:
            x, y = clamp(x, y)
            if free(x, y):
                return x, y
        return clamp(r.x(), r.y())

    # categories
    def toggle(self, name):
        if name in self.bars:
            self.close(name)
        else:
            self._open(name)

    def _open(self, name, save=True):
        if name in self.bars:
            return
        if len(self.order) >= MAX_BARS:
            self.close(self.order[0], save=False)
        slot = len(self.order)
        bar = HubBar(self, name, self.groups[name], vertical=(slot == 2))
        self.bars[name] = bar
        self.order.append(name)
        self._rebuild_orientation()
        if self._should_show():
            for b in self.bars.values():
                b.show()
            self.disk.raise_()
            self.reposition()
        self.disk.update()
        if save:
            self.save()

    def _rebuild_orientation(self):
        """Slot 2 is vertical; rebuild bars whose orientation no longer matches their slot."""
        for slot, n in enumerate(self.order):
            want_v = slot == 2
            if self.bars[n].vertical != want_v:
                old = self.bars[n]
                self.bars[n] = HubBar(self, n, self.groups[n], vertical=want_v)
                old.deleteLater()

    def close(self, name, save=True):
        bar = self.bars.pop(name, None)
        if bar is None:
            return
        bar.hide()
        bar.deleteLater()
        self.order.remove(name)
        self._rebuild_orientation()
        if self._should_show():
            for b in self.bars.values():
                b.show()
        self.reposition()
        self.disk.update()
        if save:
            self.save()

    def close_all(self):
        for n in list(self.order):
            self.close(n, save=False)
        self.save()

    def reset_position(self):
        self.offset = None
        self.reposition()
        self.save()

    # visibility
    def _should_show(self):
        if self.floating:
            return self.enabled
        return (self.enabled and self.main.isVisible() and not self.main.isMinimized()
                and self.view.isVisible() and self.view.width() > 120)

    def sync(self):
        on = self._should_show()
        for w in [self.disk, *self.bars.values()]:
            w.setVisible(on)
        if on:
            for b in self.bars.values():
                b.raise_()
            self.disk.raise_()
            self.reposition()

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self.save()
        self.sync()
        act = getattr(self.main, "hub_action", None)
        if act is not None and act.isChecked() != self.enabled:
            act.setChecked(self.enabled)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Move, QEvent.Resize):
            self.reposition()
        elif t in (QEvent.Show, QEvent.Hide, QEvent.WindowStateChange):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.sync)
        return False

    def save(self):
        s = self.settings
        s.setValue(f"{self.key}/enabled", self.enabled)
        s.setValue(f"{self.key}/expanded", self.disk.expanded)
        s.setValue(f"{self.key}/open", list(self.order))
        if self.offset is not None:
            s.setValue(f"{self.key}/offset", [self.offset.x(), self.offset.y()])
        else:
            s.remove(f"{self.key}/offset")
