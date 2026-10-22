"""Radial view wheel (Space in the 3D view): flick toward a view to snap the camera there.

Tap Space → the wheel stays open, click a view. Hold Space → move toward a view and release.
Centre = zoom to fit. Chips under the wheel switch the display mode. Keys 1–8 pick a sector, Esc closes.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import icons, theme

# (key, label, icon) clockwise from north
SECTORS = [
    ("view:Top", "Top", "arrow-fat-line-up"),
    ("view:Isometric", "Iso", "cube"),
    ("view:Right", "Right", "arrow-fat-line-right"),
    ("view:Back", "Back", "arrow-u-up-left"),
    ("view:Bottom", "Bottom", "arrow-fat-line-down"),
    ("view:Front", "Front", "square"),
    ("view:Left", "Left", "arrow-fat-line-left"),
    ("normal", "Normal to", "crosshair"),
]
MODES = [("Shaded + edges", "Shaded+E"), ("Shaded", "Shaded"), ("Uncoloured", "Uncoloured"),
         ("Hidden line", "Hidden"), ("Wireframe", "Wire"), ("Translucent", "Glass")]

R_OUT, R_IN = 118, 34
CHIP_H, CHIP_GAP = 24, 4


class ViewWheel(QWidget):
    chosen = Signal(str)        # "view:<name>", "fit", "normal", "mode:<display mode>", "previous"

    def __init__(self, parent=None, current_mode="", can_normal=False):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.current_mode = current_mode
        self.can_normal = can_normal
        self.hot: str | None = None
        self._t = 0.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._held = True
        self.chip_w = 74
        w = max(2 * R_OUT + 24, len(MODES) * (self.chip_w + CHIP_GAP) + 16)
        self.resize(w, 2 * R_OUT + 24 + CHIP_H + 14)
        self.c = QPointF(self.width() / 2, R_OUT + 12)
        self._anim = QTimer(self, interval=12)
        self._anim.timeout.connect(self._tick)
        self._anim.start()

    # ----------------------------------------------------------- open
    def popup_at(self, gp=None):
        gp = gp or QCursor.pos()
        self.move(int(gp.x() - self.c.x()), int(gp.y() - self.c.y()))
        self.show()
        self.grabKeyboard()

    def _tick(self):
        self._t = min(1.0, self._t + 0.12)
        self.update()
        if self._t >= 1.0:
            self._anim.stop()

    # ------------------------------------------------------- geometry
    def _chip_rects(self):
        n = len(MODES)
        total = n * self.chip_w + (n - 1) * CHIP_GAP
        x = (self.width() - total) / 2
        y = self.c.y() + R_OUT + 12
        return [(k, QRectF(x + i * (self.chip_w + CHIP_GAP), y, self.chip_w, CHIP_H)) for i, (k, _l) in
                enumerate(MODES)]

    def _hit(self, p: QPointF):
        for k, r in self._chip_rects():
            if r.contains(p):
                return "mode:" + k
        d = p - self.c
        dist = math.hypot(d.x(), d.y())
        if dist < R_IN:
            return "fit"
        if dist > R_OUT + 60:
            return None
        ang = (math.degrees(math.atan2(d.x(), -d.y())) + 360 + 22.5) % 360       # 0 = north, clockwise
        key = SECTORS[int(ang // 45)][0]
        if key == "normal" and not self.can_normal:
            return None
        return key

    # --------------------------------------------------------- paint
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        e = 1 - (1 - self._t) ** 3
        r_out = R_IN + (R_OUT - R_IN) * e
        p.setOpacity(0.25 + 0.75 * e)
        # shadow + disk
        for i in range(6, 0, -1):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 10))
            p.drawEllipse(self.c + QPointF(0, 3), r_out + i, r_out + i)
        ring = QPainterPath()
        ring.addEllipse(self.c, r_out, r_out)
        hole = QPainterPath()
        hole.addEllipse(self.c, R_IN, R_IN)
        p.fillPath(ring.subtracted(hole), QColor(theme.PANEL))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(self.c, r_out, r_out)
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        for i, (key, label, ic) in enumerate(SECTORS):
            a0 = 90 - (i * 45) - 22.5                                         # Qt angles: CCW from east
            enabled = key != "normal" or self.can_normal
            if key == self.hot:
                sec = QPainterPath()
                sec.moveTo(self.c)
                sec.arcTo(QRectF(self.c.x() - r_out, self.c.y() - r_out, 2 * r_out, 2 * r_out), a0 + 45, -45)
                sec.closeSubpath()
                p.fillPath(sec.subtracted(hole), QColor(theme.ACCENT_SOFT))
                arc = QRectF(self.c.x() - r_out + 2, self.c.y() - r_out + 2, 2 * r_out - 4, 2 * r_out - 4)
                p.setPen(QPen(QColor(theme.ACCENT_LINE), 3, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(arc, int((a0 + 3) * 16), int((45 - 6) * 16))
            # divider
            rad = math.radians(i * 45 + 22.5)
            p.setPen(QPen(QColor(theme.BORDER_SOFT), 1))
            p.drawLine(self.c + QPointF(math.sin(rad) * R_IN, -math.cos(rad) * R_IN),
                       self.c + QPointF(math.sin(rad) * r_out, -math.cos(rad) * r_out))
            # label + icon
            mid = math.radians(i * 45)
            rr = (R_IN + r_out) / 2 + 4
            cpos = self.c + QPointF(math.sin(mid) * rr, -math.cos(mid) * rr)
            col = QColor(theme.TEXT if enabled else theme.DIM)
            if key == self.hot:
                col = QColor(theme.ACCENT_LINE)
            icon = icons.icon(ic, col.name(), col.name())
            if not icon.isNull():
                p.drawPixmap(int(cpos.x() - 9), int(cpos.y() - 17), icon.pixmap(18, 18))
            p.setFont(f)
            p.setPen(col)
            p.drawText(QRectF(cpos.x() - 40, cpos.y() + 2, 80, 16), Qt.AlignCenter, label)
            p.setPen(QColor(theme.DIM))
            p.drawText(QRectF(cpos.x() - 40, cpos.y() + 15, 80, 13), Qt.AlignCenter, f"{i + 1}")
        # centre: fit
        p.setPen(QPen(QColor(theme.ACCENT_LINE if self.hot == "fit" else theme.BORDER), 1.4))
        p.setBrush(QColor(theme.ACCENT_SOFT if self.hot == "fit" else theme.RAISED))
        p.drawEllipse(self.c, R_IN - 4, R_IN - 4)
        ic = icons.icon("frame-corners", theme.TEXT)
        if not ic.isNull():
            p.drawPixmap(int(self.c.x() - 9), int(self.c.y() - 15), ic.pixmap(18, 18))
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(self.c.x() - 30, self.c.y() + 2, 60, 16), Qt.AlignCenter, "Fit")
        # display-mode chips
        f2 = QFont(self.font())
        f2.setPointSizeF(8)
        p.setFont(f2)
        for k, r in self._chip_rects():
            on = k == self.current_mode
            hot = self.hot == "mode:" + k
            path = QPainterPath()
            path.addRoundedRect(r, CHIP_H / 2, CHIP_H / 2)
            p.fillPath(path, QColor(theme.ACCENT_SOFT if (on or hot) else theme.PANEL))
            p.setPen(QPen(QColor(theme.ACCENT_LINE if (on or hot) else theme.BORDER), 1))
            p.drawPath(path)
            p.setPen(QColor(theme.TEXT if (on or hot) else theme.MUTED))
            p.drawText(r, Qt.AlignCenter, dict(MODES)[k])

    # -------------------------------------------------------- events
    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        if h != self.hot:
            self.hot = h
            self.update()

    def mousePressEvent(self, e):
        h = self._hit(e.position())
        if e.button() == Qt.LeftButton and h:
            self._pick(h)
        elif not self.rect().contains(e.position().toPoint()) or e.button() != Qt.LeftButton or h is None:
            self.close()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.close()
        elif Qt.Key_1 <= k <= Qt.Key_8:
            key = SECTORS[k - Qt.Key_1][0]
            if key != "normal" or self.can_normal:
                self._pick(key)
        elif k in (Qt.Key_F, Qt.Key_0):
            self._pick("fit")
        elif k == Qt.Key_Backspace:
            self._pick("previous")
        elif k in (Qt.Key_Return, Qt.Key_Enter) and self.hot:
            self._pick(self.hot)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            if self._clock.elapsed() > 280 and self.hot:          # held and flicked: release picks
                self._pick(self.hot)

    def _pick(self, key):
        self.releaseKeyboard()
        self.close()
        self.chosen.emit(key)

    def closeEvent(self, e):
        try:
            self.releaseKeyboard()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(e)
