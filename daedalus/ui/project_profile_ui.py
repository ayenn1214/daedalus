"""Project Profile page (Projects tab ▸ Profile): the whole project at a glance — 3D preview of the linked model,
weight, overall size, approx budget, longest lead, options, vendors, breakdown by type, assemblies, production
and drawing revisions. Figures come from daedalus.inventory.project_profile."""
from __future__ import annotations

import math
import os
import threading

import numpy as np
from PySide6.QtCore import QLineF, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import theme
from ..inventory import model as M
from ..inventory import project_profile as PP

MODEL_FILTER = "3D models (*.step *.stp *.iges *.igs *.stl *.obj *.brep *.daedalus);;All files (*)"
VIEWS = {"Iso": (45.0, math.degrees(math.atan(1 / math.sqrt(2)))), "Front": (0.0, 0.0), "Top": (0.0, 90.0),
         "Right": (90.0, 0.0)}
_CACHE: dict = {}                      # (path, mtime, size) → (segments, bounds)


# ------------------------------------------------------------------ 3D preview
def _key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), int(st.st_mtime), st.st_size)


def load_model(path: str):
    """(segments m×2×3, bounds) for a model file, cached for the session (blocking)."""
    from ..inventory import thumbs as T
    k = _key(path)
    if k is None:
        raise FileNotFoundError(path)
    if k in _CACHE:
        return _CACHE[k]
    m = T.mesh(path)
    if m is None or len(m[1]) == 0:
        raise ValueError("Couldn't read a 3D shape from this file")
    v, f = m
    segs = T.feature_edges(v, f)
    out = (segs, T.bounds(v))
    _CACHE[k] = out
    return out


class ModelView(QWidget):
    """Wireframe (feature edges) of the project's model: drag to turn, wheel to zoom, Iso / Front / Top / Right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segs = np.zeros((0, 2, 3))
        self.yaw, self.pitch = VIEWS["Iso"]
        self.zoom = 1.0
        self.msg = ""
        self._drag = None
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.OpenHandCursor)

    def set_segments(self, segs):
        self.segs = np.asarray(segs, float).reshape(-1, 2, 3)
        if len(self.segs):
            c = self.segs.reshape(-1, 3)
            self.segs = self.segs - (c.min(axis=0) + c.max(axis=0)) / 2
        self.msg = ""
        self.update()

    def set_message(self, text):
        self.msg = text
        self.update()

    def set_view(self, name):
        self.yaw, self.pitch = VIEWS[name]
        self.zoom = 1.0
        self.update()

    def projected(self) -> np.ndarray:
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
        rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
        q = self.segs.reshape(-1, 3) @ (rx @ rz).T
        return np.stack([q[:, 0], -q[:, 2]], axis=1).reshape(-1, 2, 2)

    @staticmethod
    def _dark() -> bool:
        """Is the view background dark? (black / sith / midnight themes use a near-black viewport)"""
        lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in (theme.VIEW_BG_TOP, theme.VIEW_BG_BOTTOM)]
        return sum(lum) / 2 < 0.45

    def line_color(self) -> QColor:
        """Edges that stand out on the viewport in every theme."""
        return QColor("#dfe6ee") if self._dark() else QColor.fromRgbF(*theme.EDGE_COLOR)

    def paintEvent(self, _e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.height())
        top, bot = theme.VIEW_BG_TOP, theme.VIEW_BG_BOTTOM
        g.setColorAt(0, QColor.fromRgbF(*top))
        g.setColorAt(1, QColor.fromRgbF(*bot))
        qp.fillRect(self.rect(), g)
        if len(self.segs):
            s2 = self.projected()
            pts = s2.reshape(-1, 2)
            lo, hi = pts.min(axis=0), pts.max(axis=0)
            span = float(max(hi - lo)) or 1.0
            sc = min(self.width(), self.height()) * 0.86 / span * self.zoom
            mid = (lo + hi) / 2
            cx, cy = self.width() / 2, self.height() / 2
            qp.setPen(QPen(self.line_color(), 1.2, Qt.SolidLine, Qt.RoundCap))
            qp.drawLines([QLineF(QPointF(cx + (a[0] - mid[0]) * sc, cy + (a[1] - mid[1]) * sc),
                                 QPointF(cx + (b[0] - mid[0]) * sc, cy + (b[1] - mid[1]) * sc)) for a, b in s2])
        if self.msg:
            qp.setPen(self.line_color())
            qp.drawText(self.rect().adjusted(16, 16, -16, -16), Qt.AlignCenter | Qt.TextWordWrap, self.msg)
        qp.end()

    def mousePressEvent(self, e):
        self._drag = e.position()
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        d = e.position() - self._drag
        self._drag = e.position()
        self.yaw = (self.yaw + d.x() * 0.5) % 360
        self.pitch = max(-90.0, min(90.0, self.pitch + d.y() * 0.5))
        self.update()

    def mouseReleaseEvent(self, _e):
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, e):
        self.zoom = max(0.2, min(8.0, self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15)))
        self.update()

    def mouseDoubleClickEvent(self, _e):
        self.zoom = 1.0
        self.update()


class ModelCard(QFrame):
    """The preview with its view buttons; loads the model on a worker thread."""

    def __init__(self, page, pr, size_label=None, parent=None):
        super().__init__(parent)
        self.page, self.pr, self.size_label = page, pr, size_label
        self.setObjectName("ppcard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        head = QHBoxLayout()
        cap = QLabel("3D MODEL")
        cap.setObjectName("h2")
        head.addWidget(cap)
        self.name = QLabel()
        self.name.setObjectName("muted")
        head.addWidget(self.name, 1)
        self.view_btns = {}
        for n in VIEWS:
            b = QPushButton(n)
            b.setObjectName("small")
            b.clicked.connect(lambda _=False, n=n: self.view.set_view(n))
            head.addWidget(b)
            self.view_btns[n] = b
        fit = QPushButton("Fit")
        fit.setObjectName("small")
        fit.clicked.connect(lambda: (setattr(self.view, "zoom", 1.0), self.view.update()))
        head.addWidget(fit)
        lay.addLayout(head)
        self.view = ModelView()
        lay.addWidget(self.view, 1)
        foot = QHBoxLayout()
        self.size = QLabel()
        self.size.setObjectName("muted")
        foot.addWidget(self.size, 1)
        self.link = QPushButton("Link 3D model…" if not pr.get("modelFile") else "Change model…")
        self.link.setObjectName("small")
        self.link.clicked.connect(self.choose)
        foot.addWidget(self.link)
        lay.addLayout(foot)
        self._result = None
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)
        self.start()

    def start(self):
        f = self.pr.get("modelFile") or ""
        self.size.setText(PP.fmt_size(self.pr.get("modelSize")) or "")
        for b in self.view_btns.values():
            b.setEnabled(bool(f))
        if not f:
            self.name.setText("")
            self.view.set_message("No 3D model linked.\nLink the project's top-level STEP (or STL) to see it here, "
                                  "with its overall size.")
            return
        path = M.local_path(f)
        self.name.setText(" · " + os.path.basename(path))
        self.name.setToolTip(path)
        if not os.path.exists(path):
            self.view.set_message(f"Model file not found on this PC:\n{path}")
            return
        k = _key(path)
        if k in _CACHE:
            return self._show(_CACHE[k])
        self.view.set_message("Loading model… (a big STEP can take a minute the first time)")
        self._result = None

        def work():
            try:
                self._result = ("ok", load_model(path))
            except Exception as e:  # noqa: BLE001
                self._result = ("err", str(e))
        threading.Thread(target=work, daemon=True).start()
        self._timer.start()

    def _poll(self):
        if self._result is None:
            return
        self._timer.stop()
        kind, val = self._result
        if kind == "ok":
            self._show(val)
        else:
            self.view.set_message(f"Couldn't show the model:\n{val}")

    def _show(self, res):
        segs, b = res
        self.view.set_segments(segs)
        if b and b != self.pr.get("modelSize"):
            self.pr["modelSize"] = b                   # kept with the project; saved with the next change
        txt = PP.fmt_size(b)
        self.size.setText(txt)
        if self.size_label is not None:
            try:
                self.size_label.setText(txt.split(" (")[0] if txt else "—")
            except RuntimeError:
                pass

    def choose(self):
        cur = M.local_path(self.pr.get("modelFile") or "")
        p, _ = QFileDialog.getOpenFileName(self, "Link the project's 3D model",
                                           os.path.dirname(cur) if cur else M.local_path(self.pr.get("folderPath") or ""),
                                           MODEL_FILTER)
        if p:
            self.page.set_project_model(self.pr["id"], p)


# ------------------------------------------------------------------ cards
def _card(title, body_widgets=(), border=None):
    c = QFrame()
    c.setObjectName("ppcard")
    c.setStyleSheet(f"#ppcard{{background:{theme.PANEL}; border:1px solid {border or theme.BORDER}; border-radius:8px;}}")
    v = QVBoxLayout(c)
    v.setContentsMargins(14, 10, 14, 12)
    v.setSpacing(3)
    cap = QLabel(title.upper())
    cap.setObjectName("h2")
    v.addWidget(cap)
    for w in body_widgets:
        v.addWidget(w)
    return c


def _big(text, color=None):
    n = QLabel(text)
    f = n.font()
    f.setPointSizeF(f.pointSizeF() + 8)
    f.setWeight(f.Weight.DemiBold)
    n.setFont(f)
    if color:
        n.setStyleSheet(f"color:{color}")
    return n


def _note(text):
    l = QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(True)
    return l


def _pct(k, n):
    return f"{k} of {n}" if n else "—"


# ------------------------------------------------------------------ the page
def build(page, pid: str) -> QWidget:
    from .inventory_ui import _muted, _num_item, _open_link, _section, _small
    inv = page.inv
    pr = inv.project(pid)
    d = PP.profile(inv, pid)
    w, v = page._page()

    # header
    hdr = QHBoxLayout()
    hdr.addWidget(_small("◂ Projects", lambda: page.show_project_profile(None), "Back to all projects"))
    t = QLabel(f"<span style='font-size:15pt; font-weight:600'>{pr['name']}</span>")
    t.setProperty("dimsLink", f"project:{pid}")
    hdr.addWidget(t)
    hdr.addStretch(1)
    if pr.get("folderPath"):
        hdr.addWidget(_small("Open folder", lambda: _open_link(pr["folderPath"]), pr["folderPath"], icon="folder"))
    for b in (_small("BOM / Export", lambda: page.project_bom(pid), "Parts list by type — copy or export to Excel"),
              _small("Customize…", lambda: QTimer.singleShot(0, lambda: page.customize_project(pid)),
                     "Options: pick-one choices and add-ons, each adding its own parts"),
              _small("Edit", lambda: page.edit_project(pid)),
              _small("+ Production order", lambda: page.add_production_for(pid), "Schedule a build of this project")):
        hdr.addWidget(b)
    v.addLayout(hdr)
    chips = [f"{d['lines']} top-level line{'s' if d['lines'] != 1 else ''}", f"{len(d['parts'])} parts in all"]
    if d["assemblies"]:
        chips.append(f"{len(d['assemblies'])} assemblies / weldments")
    if d["options"]:
        chips.append(f"{len(d['options'])} option group{'s' if len(d['options']) != 1 else ''}")
    if pr.get("notes"):
        chips.append(pr["notes"])
    v.addWidget(_muted("  ·  ".join(chips)))

    # model + KPIs
    top = QHBoxLayout()
    top.setSpacing(10)
    size_kpi = _big(PP.fmt_size(d["size"]).split(" (")[0] if d["size"] else "—")
    model = ModelCard(page, pr, size_kpi)
    model.setStyleSheet(f"#ppcard{{background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:8px;}}")
    model.setMinimumHeight(330)
    top.addWidget(model, 3)
    grid = QGridLayout()
    grid.setSpacing(10)
    n = d["lines"]
    grid.addWidget(_card("Total weight", [_big(f"{d['weight']:,.0f} lb" if d["weight"] else "—"),
                                          _note(f"{_pct(d['weightKnown'], n)} lines weighed"
                                                + (f" · {d['weight'] * 0.4536:,.0f} kg" if d["weight"] else ""))],
                         theme.WARN if n and d["weightKnown"] < n else None), 0, 0)
    grid.addWidget(_card("Overall size", [size_kpi, _note("from the linked 3D model (mm)" if pr.get("modelFile")
                                                          else "link a 3D model to measure it")]), 0, 1)
    grid.addWidget(_card("Approx budget", [_big(M.fmt_money(d["cost"]) if d["cost"] else "—"),
                                           _note(f"{_pct(d['costKnown'], n)} lines priced · materials & bought parts"
                                                 + (f" · finishing ≈ {M.fmt_money(d['finishing']['zinc'] + d['finishing']['powder'])}"
                                                    if d["finishing"]["zinc"] or d["finishing"]["powder"] else ""))],
                         theme.WARN if n and d["costKnown"] < n else None), 1, 0)
    ld = d["lead"]
    grid.addWidget(_card("Longest lead", [_big(f"{ld['days']} days" if ld["days"] else "—"),
                                          _note(f"{ld['part'].get('name')} · {ld['vendor']}" if ld["days"] and ld["part"]
                                                else "no lead times on the suppliers yet")]), 1, 1)
    gw = QWidget()
    gw.setLayout(grid)
    top.addWidget(gw, 2)
    v.addLayout(top)

    # options + vendors
    mid = QHBoxLayout()
    mid.setSpacing(10)
    ow = []
    if d["options"]:
        for g in d["options"]:
            ow.append(QLabel(f"<b>{g['name']}</b> <span style='color:{theme.MUTED}'>· "
                             f"{'pick one' if g['kind'] == 'one' else 'add-ons'}</span>"))
            for c in g["choices"]:
                bits = [f"{c['lines']} part{'s' if c['lines'] != 1 else ''}"]
                if c["weight"]:
                    bits.append(f"+{c['weight']:,.0f} lb")
                if c["cost"]:
                    bits.append(f"+{M.fmt_money(c['cost'])}")
                ow.append(_note(f"   {'● ' if c['default'] else '○ '}{c['name']} — {' · '.join(bits)}"))
    else:
        ow.append(_note("No options yet. Customize… adds pick-one choices (e.g. length) and add-ons "
                        "(e.g. an extra beam), each with its own parts."))
    oc = _card("Customizable options", ow)
    oc.layout().addStretch(1)
    oc.layout().addWidget(_small("Customize…", lambda: QTimer.singleShot(0, lambda: page.customize_project(pid))),
                          0, Qt.AlignLeft)
    mid.addWidget(oc, 1)
    vw = []
    for vd in d["vendors"][:10]:
        extra = f" · <span style='color:{theme.INFO}'>{vd['openPOs']} open PO{'s' if vd['openPOs'] != 1 else ''}</span>" \
            if vd["openPOs"] else ""
        vw.append(QLabel(f"<b>{vd['name']}</b> <span style='color:{theme.MUTED}'>· {vd['parts']} part"
                         f"{'s' if vd['parts'] != 1 else ''} · {', '.join(vd['kinds']) or 'orders'}</span>{extra}"))
    if len(d["vendors"]) > 10:
        vw.append(_note(f"+ {len(d['vendors']) - 10} more"))
    if not vw:
        vw.append(_note("No vendors linked — add suppliers on the part profiles, or a finisher on MFG parts."))
    vc = _card("Vendors on this project", vw)
    vc.layout().addStretch(1)
    mid.addWidget(vc, 1)
    v.addLayout(mid)

    # breakdown by type
    v.addWidget(_section("Breakdown by type"))
    rows = []
    for k, tt in d["types"].items():
        if not tt["lines"]:
            continue
        rows.append([k, _num_item(tt["lines"]), _num_item(round(tt["qty"], 2)),
                     _num_item(f"{tt['weight']:,.1f}" if tt["weight"] else "—"),
                     _num_item(M.fmt_money(tt["cost"]) if tt["cost"] else "—"),
                     _num_item(f"{tt['priced']} / {tt['lines']}")])
    fin = d["finishing"]
    if fin["zincParts"] or fin["powderParts"]:
        bits = []
        if fin["zincParts"]:
            bits.append(f"zinc {fin['zincParts']} parts · {fin['zincLb']:,.0f} lb")
        if fin["powderParts"]:
            bits.append(f"powder coat {fin['powderParts']} parts" + (f" · {fin['needQuote']} need a quote"
                                                                    if fin["needQuote"] else ""))
        rows.append(["Finishing", _num_item(fin["zincParts"] + fin["powderParts"]), "", "",
                     _num_item(M.fmt_money(fin["zinc"] + fin["powder"]) if fin["zinc"] or fin["powder"] else "—"),
                     "; ".join(bits)])
    v.addWidget(page.table(["Type", "Lines", "Qty / unit", "Weight (lb)", "Cost", "Priced"], rows, stretch=0)
                if rows else _muted("No parts on this project yet."))

    # assemblies
    if d["assemblies"]:
        v.addWidget(_section("Assemblies & weldments"))
        colors = {"ok": theme.OK, "changed": theme.WARN, "newer": theme.WARN, "mismatch": theme.ERROR,
                  "missing": theme.ERROR, "no-file": theme.MUTED}
        words = {"ok": "up to date", "changed": "changed since check-in", "newer": "newer rev on disk",
                 "mismatch": "rev mismatch", "missing": "file missing", "no-file": "no drawing"}
        rows = []
        for a in d["assemblies"]:
            p = a["part"]
            wt, ok = a["weight"]
            st = a["file"]["status"]
            rev = f"Rev {p.get('revision')} · " if p.get("revision") else ""
            name = QLabel(f"<a href='#' style='color:{theme.TEXT}; text-decoration:none'><b>{p.get('name')}</b></a>"
                          + (f" <span style='color:{theme.MUTED}'>{p.get('partNumber')}</span>"
                             if p.get("partNumber") else ""))
            name.linkActivated.connect(lambda _l, i=p["id"]: page.edit_part(i))
            name.setToolTip("Open the part profile")
            rows.append([name, _num_item(a["qty"]), _num_item(f"{wt:,.1f}" + ("" if ok else " *") if wt else "—"),
                         _num_item(a["kids"]),
                         _num_item(a["short"] or "", theme.ERROR if a["short"] else None),
                         _num_item(a["stock"]),
                         _num_item(rev + words.get(st, st), colors.get(st))])
        v.addWidget(page.table(["Assembly", "Qty / unit", "Weight (lb)", "Parts", "Short", "Built (stock)",
                                "Drawing"], rows, stretch=0))
        if any(not a["weight"][1] for a in d["assemblies"]):
            v.addWidget(_muted("* some of its parts have no weight yet"))

    # production
    v.addWidget(_section("Production"))
    po = d["production"]
    v.addWidget(_muted(f"{po['inProduction']:g} in production · {po['builtThisYear']:g} built this year · "
                       f"{len(po['all'])} order{'s' if len(po['all']) != 1 else ''} in all"))
    if po["open"]:
        rows = [[o.get("status") or "", _num_item(M.num(o.get("qty"), 1)), o.get("orderDate") or "",
                 o.get("dueDate") or "", _small("Open", lambda _=False, o=o: page.open_production(o))]
                for o in sorted(po["open"], key=lambda o: o.get("dueDate") or "9999")]
        v.addWidget(page.table(["Status", "Qty", "Ordered", "Due", ""], rows, stretch=0))

    # files & revisions
    v.addWidget(_section("Files & revisions"))
    fl = d["files"]
    v.addWidget(_muted(f"{fl['linked']} of {fl['mfg']} MFG parts have a drawing linked · "
                       f"{len(fl['stale'])} need a look"))
    if fl["stale"]:
        rows = []
        for p, s in fl["stale"][:30]:
            rows.append([p.get("name") or "", p.get("partNumber") or "", p.get("revision") or "",
                         s.get("detail") or s["status"],
                         _small("Open", lambda _=False, i=p["id"]: page.edit_part(i), "Open the part profile")])
        v.addWidget(page.table(["Part", "Part No.", "Rev", "What changed", ""], rows, stretch=3))
    v.addStretch(1)
    w._profile = d                       # for tests
    w._model = model
    return w
