"""DIMS extras: part icons and wireframe thumbnails, the Analytics tab (trends, cost analysis, vendor price
cross-reference, production history, discontinued parts), the binning list (print / PDF / Excel) and the
manufactured-part revision check."""
from __future__ import annotations

import datetime as dt
import threading

from PySide6.QtCore import QDate, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
    QWidget,
)

from ..inventory import analytics as A
from ..inventory import model as M
from ..inventory import thumbs as T
from . import theme

# ------------------------------------------------------------------ icons
_ICONS: dict = {}


def type_icon(kind: str, size: int = 32) -> QIcon:
    """Line icon for a part type: fastener (bolt), purchased (box), manufactured (bracket)."""
    key = (kind, size, theme.MUTED)
    if key in _ICONS:
        return _ICONS[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    col = {"fastener": theme.WARN, "purchased": theme.INFO, "manufactured": theme.ACCENT_LINE}.get(kind, theme.MUTED)
    qp.setPen(QPen(QColor(col), max(1.2, size / 18), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    s = size / 32
    path = QPainterPath()
    if kind == "fastener":                       # hex head + threaded shank, side view
        path.addRect(QRectF(9 * s, 4 * s, 14 * s, 6 * s))
        path.moveTo(16 * s - 3 * s, 4 * s)
        path.lineTo(16 * s - 3 * s, 10 * s)
        path.moveTo(16 * s + 3 * s, 4 * s)
        path.lineTo(16 * s + 3 * s, 10 * s)
        path.addRect(QRectF(13 * s, 10 * s, 6 * s, 18 * s))
        for y in range(13, 27, 3):
            path.moveTo(13 * s, y * s)
            path.lineTo(19 * s, (y + 1.5) * s)
    elif kind == "purchased":                    # isometric box
        pts = [(16, 4), (28, 10), (28, 22), (16, 28), (4, 22), (4, 10)]
        path.moveTo(pts[0][0] * s, pts[0][1] * s)
        for x, y in pts[1:]:
            path.lineTo(x * s, y * s)
        path.closeSubpath()
        path.moveTo(4 * s, 10 * s)
        path.lineTo(16 * s, 16 * s)
        path.lineTo(28 * s, 10 * s)
        path.moveTo(16 * s, 16 * s)
        path.lineTo(16 * s, 28 * s)
    else:                                        # L bracket with two holes, isometric wireframe
        P = [(6, 12), (18, 6), (18, 18), (6, 24), (6, 12)]
        path.moveTo(P[0][0] * s, P[0][1] * s)
        for x, y in P[1:]:
            path.lineTo(x * s, y * s)
        Q = [(6, 24), (18, 30), (30, 24), (18, 18)]
        path.moveTo(Q[0][0] * s, Q[0][1] * s)
        for x, y in Q[1:]:
            path.lineTo(x * s, y * s)
        path.addEllipse(QPointF(12 * s, 15 * s), 2 * s, 2.4 * s)
        path.addEllipse(QPointF(18 * s, 24 * s), 2.4 * s, 1.6 * s)
    qp.drawPath(path)
    qp.end()
    ic = QIcon(pm)
    _ICONS[key] = ic
    return ic


class ThumbLoader(QObject):
    """Builds missing thumbnails on a background thread, one file at a time; `ready` fires per finished file."""
    ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: list[str] = []
        self._failed: set = set()
        self._busy = False
        self._lock = threading.Lock()

    def icon_for(self, p) -> QIcon:
        c = T.cached(p)
        if c is not None:
            return QIcon(str(c))
        src = T.source_for(p)
        if src and src not in self._failed:
            self.request(src)
        return type_icon(p.get("type") or "")

    def request(self, src):
        with self._lock:
            if src in self._queue:
                return
            self._queue.append(src)
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._busy = False
                    return
                src = self._queue.pop(0)
            try:
                out = T.make(src, color=theme.ACCENT_TEXT)
            except Exception:  # noqa: BLE001
                out = None
            if out is None:
                self._failed.add(src)
            else:
                self.ready.emit(src)


# ------------------------------------------------------------------ small widgets
def _table(headers, rows, stretch=0, money=(), pct=(), sortable=True):
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(False)
    for i, r in enumerate(rows):
        for j, h in enumerate(headers):
            v = r.get(h) if isinstance(r, dict) else r[j]
            if isinstance(v, QWidget):
                t.setCellWidget(i, j, v)
                continue
            if v is None or v == "":
                txt = "—" if v is None else ""
            elif h in money:
                txt = M.fmt_money(v)
            elif h in pct:
                txt = f"{v:+g}%" if isinstance(v, (int, float)) else str(v)
            elif isinstance(v, float):
                txt = f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"
            else:
                txt = str(v)
            it = QTableWidgetItem(txt)
            if isinstance(v, (int, float)):
                it.setData(Qt.UserRole, float(v))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, j, it)
    hdr = t.horizontalHeader()
    for c in range(len(headers)):
        hdr.setSectionResizeMode(c, QHeaderView.Stretch if c == stretch else QHeaderView.ResizeToContents)
    t.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)     # keep the given order until a header is clicked
    t.setSortingEnabled(sortable)
    t.setMinimumHeight(min(520, 34 + 30 * max(len(rows), 1)))
    return t


class BarChart(QWidget):
    """Monthly bars, one or two series (side by side), labelled under each bar."""

    def __init__(self, labels, series, money=False, height=170, parent=None):
        super().__init__(parent)
        self.labels, self.series, self.money = labels, series, money     # series: [(name, values, colour)]
        self.setMinimumHeight(height)

    def paintEvent(self, _e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        top, bot, left = 18, 28, 8
        vals = [v for _n, vs, _c in self.series for v in vs]
        mx = max(vals + [0]) or 1
        n = max(len(self.labels), 1)
        slot = (w - left * 2) / n
        k = len(self.series)
        bw = max(2.0, slot * 0.7 / max(k, 1))
        qp.setPen(QColor(theme.BORDER))
        qp.drawLine(left, h - bot, w - left, h - bot)
        f = qp.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        qp.setFont(f)
        for i, lab in enumerate(self.labels):
            x0 = left + i * slot + slot * 0.15
            for j, (_n, vs, col) in enumerate(self.series):
                v = vs[i] if i < len(vs) else 0
                bh = (h - top - bot) * (v / mx)
                qp.fillRect(QRectF(x0 + j * bw, h - bot - bh, bw - 1, bh), QColor(col))
            qp.setPen(QColor(theme.MUTED))
            short = dt.date(int(lab[:4]), int(lab[5:7]), 1).strftime("%b") if len(lab) == 7 and lab[4] == "-" else lab
            qp.drawText(QRectF(left + i * slot, h - bot + 2, slot, bot - 2), Qt.AlignHCenter | Qt.AlignTop, short)
        fm = qp.fontMetrics()
        x = w - left
        for n, _v, col in reversed(self.series):          # legend, right-aligned: ■ name
            tw = fm.horizontalAdvance(n)
            x -= tw
            qp.setPen(QColor(theme.MUTED))
            qp.drawText(QRectF(x, 0, tw, top), Qt.AlignVCenter, n)
            x -= 12
            qp.fillRect(QRectF(x, top / 2 - 4, 8, 8), QColor(col))
            x -= 14
        qp.setPen(QColor(theme.MUTED))
        qp.drawText(QRectF(left, 0, max(0, x - left), top), Qt.AlignLeft | Qt.AlignVCenter,
                    "max " + (M.fmt_money(mx) if self.money else f"{mx:g}"))
        qp.end()


def _hrow(*widgets, stretch_first=True):
    h = QHBoxLayout()
    for i, w in enumerate(widgets):
        if isinstance(w, str):
            w = QLabel(w)
        h.addWidget(w, 1 if (i == 0 and stretch_first) else 0)
    return h


def _muted(text):
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def _h(text):
    lab = QLabel(f"<b>{text}</b>")
    lab.setStyleSheet("padding-top:8px")
    return lab


def _save_xlsx(parent, name, sheets):
    path, _ = QFileDialog.getSaveFileName(parent, "Export to Excel", name, "Excel (*.xlsx)")
    if not path:
        return None
    clean = [(n, [{k: v for k, v in r.items() if k not in ("id", "projectId", "series")} for r in rows])
             for n, rows in sheets]
    M.write_xlsx(path, clean)
    return path


# ------------------------------------------------------------------ analytics tab
class AnalyticsView(QWidget):
    """Trends · Cost analysis · Vendor prices · Production history · Discontinued — sub-tabs."""
    edit_part = Signal(str)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.inv = page.inv
        st = page.analytics_state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for name, fn in (("Trends", self.trends), ("Cost Analysis", self.costs), ("Vendor Prices", self.vendors),
                         ("Production History", self.production), ("Discontinued", self.discontinued)):
            self.tabs.addTab(fn(), name)
        self.tabs.setCurrentIndex(st.get("tab", 0))
        self.tabs.currentChanged.connect(lambda i: st.__setitem__("tab", i))
        v.addWidget(self.tabs)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        return w, lay

    # ---- trends
    def trends(self):
        st = self.page.analytics_state
        w, v = self._page()
        months = st.get("months", 12)
        ms, rows = A.trend_rows(self.inv, months)
        flow = A.usage_by_month(self.inv, ms)
        span = QSpinBox()
        span.setRange(3, 36)
        span.setValue(months)
        span.setSuffix(" months")
        span.valueChanged.connect(lambda n: (st.__setitem__("months", n), QTimer.singleShot(400, self.page.refresh_current)))
        exp = QPushButton("Export to Excel")
        exp.clicked.connect(lambda: _save_xlsx(self, "DIMS-trends.xlsx", [("Trends", [
            {**{k: r[k] for k in r if k not in ("series",)}, **dict(zip(ms, r["series"]))} for r in rows])]))
        v.addLayout(_hrow(_muted("Stock used and received per month. Months left = stock ÷ average monthly use over the "
                                 "last 6 months; suggested reorder point = average daily use × the cheapest vendor's "
                                 "lead time × 1.5."), span, exp))
        v.addWidget(BarChart(ms, [("Used", flow["used"], theme.ERROR), ("Received", flow["received"], theme.OK)]))
        if not rows:
            v.addWidget(_muted("No usage recorded in this period yet — Use / Receive on the Parts tab records it."))
        else:
            hdr = ["Part", "Type", "Used (period)", "Avg / Month", "Trend %/mo", "On Hand", "Months Left", "Runs Out",
                   "Reorder Point", "Suggested Reorder Pt"]
            t = _table(hdr, rows, 0, pct=("Trend %/mo",))
            for i, r in enumerate(rows):
                ml = r["Months Left"]
                if ml is not None and ml < 1:
                    t.item(i, 6).setForeground(QColor(theme.ERROR))
                elif ml is not None and ml < 2:
                    t.item(i, 6).setForeground(QColor(theme.WARN))
                t.item(i, 0).setData(Qt.UserRole + 1, r["id"])
            t.cellDoubleClicked.connect(lambda r, _c: self._open_row(t, r))
            v.addWidget(t, 1)
        return w

    def _open_row(self, t, r):
        pid = t.item(r, 0).data(Qt.UserRole + 1)
        if pid:
            self.edit_part.emit(pid)

    # ---- costs
    def costs(self):
        w, v = self._page()
        c = A.cost_summary(self.inv, self.page.analytics_state.get("months", 12))
        exp = QPushButton("Export to Excel")
        sheets = [("Monthly", [{"Month": m, "Used Qty": c["flow"]["used"][i], "Used Cost": c["flow"]["used_cost"][i],
                                "Received Qty": c["flow"]["received"][i],
                                "Received Cost": c["flow"]["received_cost"][i]} for i, m in enumerate(c["months"])]),
                  ("By Type", [{"Type": k, "Value": x} for k, x in c["by_type"]]),
                  ("By Category", [{"Category": k, "Value": x} for k, x in c["by_category"]]),
                  ("By Vendor", [{"Vendor": k, "Value": x} for k, x in c["by_vendor"]]),
                  ("By Rack", [{"Rack": k, "Value": x} for k, x in c["by_rack"]]),
                  ("Top Parts", c["top_parts"]), ("Projects", c["projects"])]
        exp.clicked.connect(lambda: _save_xlsx(self, "DIMS-cost-analysis.xlsx", sheets))
        v.addLayout(_hrow(QLabel(f"<b>Stock value {M.fmt_money(c['stock_value'])}</b> CAD"
                                 + (f"  ·  <span style='color:{theme.WARN}'>{c['parts_without_cost']} part(s) have no "
                                    f"unit cost</span>" if c["parts_without_cost"] else "")), exp))
        v.addWidget(_muted("Value of stock used vs received each month, at today's unit costs."))
        v.addWidget(BarChart(c["months"], [("Used", c["flow"]["used_cost"], theme.ERROR),
                                           ("Received", c["flow"]["received_cost"], theme.OK)], money=True))
        grid = QHBoxLayout()
        for title, items in (("By type", c["by_type"]), ("By vendor", c["by_vendor"][:10]),
                             ("By category", c["by_category"][:10]), ("By rack", c["by_rack"])):
            col = QVBoxLayout()
            col.addWidget(_h(title))
            col.addWidget(_table(["Group", "Value"], [{"Group": k, "Value": x} for k, x in items], 0, money=("Value",)))
            grid.addLayout(col)
        v.addLayout(grid)
        v.addWidget(_h("Project cost (from the parts allocated to each project)"))
        v.addWidget(_table(["Project", "Parts", "Cost / Unit", "Units", "Total", "Cost to Cover Shortages"],
                           c["projects"], 0, money=("Cost / Unit", "Total", "Cost to Cover Shortages")))
        v.addWidget(_h("Most valuable stock"))
        v.addWidget(_table(["Part", "On Hand", "Unit Cost", "Value"], c["top_parts"], 0, money=("Unit Cost", "Value")))
        return w

    # ---- vendor cross-reference
    def vendors(self):
        w, v = self._page()
        parts, vrows = A.vendor_xref(self.inv)
        exp = QPushButton("Export to Excel")
        exp.clicked.connect(lambda: _save_xlsx(self, "DIMS-vendor-prices.xlsx", [("Parts", parts),
                                                                                  ("Vendors", vrows)]))
        total = sum(r["Savings / yr"] for r in parts)
        v.addLayout(_hrow(_muted("Parts priced by two or more suppliers (Edit part ▸ Suppliers), in CAD. Savings / yr = "
                                 "(current unit cost − cheapest quote) × the last 12 months' usage."
                                 + (f"  Possible savings: {M.fmt_money(total)} / yr." if total else "")), exp))
        if not parts:
            v.addWidget(_muted("No part has prices from two suppliers yet."))
            return w
        v.addWidget(_h("By vendor"))
        v.addWidget(_table(["Vendor", "Parts Quoted", "Cheapest On", "Avg Premium %", "Extra vs Cheapest / yr",
                            "Lead Time"], vrows, 0, money=("Extra vs Cheapest / yr",), pct=("Avg Premium %",)))
        v.addWidget(_h("By part"))
        t = _table(["Part", "Part Number", "Quotes", "Cheapest", "Cheapest Price", "Current Cost", "Spread %",
                    "Used / 12 mo", "Savings / yr"], parts, 2, money=("Cheapest Price", "Current Cost", "Savings / yr"),
                   pct=("Spread %",))
        for i, r in enumerate(parts):
            t.item(i, 0).setData(Qt.UserRole + 1, r["id"])
        t.cellDoubleClicked.connect(lambda r, _c: self._open_row(t, r))
        v.addWidget(t, 1)
        return w

    # ---- production history
    def production(self):
        st = self.page.analytics_state
        w, v = self._page()
        start = st.get("prod_start") or (dt.date.today() - dt.timedelta(days=365)).isoformat()
        end = st.get("prod_end") or dt.date.today().isoformat()
        d0, d1 = QDateEdit(QDate.fromString(start, Qt.ISODate)), QDateEdit(QDate.fromString(end, Qt.ISODate))
        for d in (d0, d1):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        pull = QPushButton("Pull")
        pull.setToolTip("Recalculate for these dates" + (" — completion dates come from the team server's audit trail"
                                                          if hasattr(self.page.store, "status_history") else ""))
        pull.clicked.connect(lambda: (st.update(prod_start=d0.date().toString(Qt.ISODate),
                                                prod_end=d1.date().toString(Qt.ISODate), prod_audit=None),
                                      self.page.refresh_current()))
        audit = st.get("prod_audit")
        if audit is None and hasattr(self.page.store, "status_history"):
            try:
                audit = self.page.store.status_history("production-orders", "status", "Complete")
            except Exception:  # noqa: BLE001
                audit = {}
            st["prod_audit"] = audit
        h = A.production_history(self.inv, start, end, audit or None)
        exp = QPushButton("Export to Excel")
        exp.clicked.connect(lambda: _save_xlsx(self, f"DIMS-production-{start}-to-{end}.xlsx", [
            ("Orders", h["orders"]), ("By Project", h["projects"]),
            ("By Month", [{"Month": m, "Units": u} for m, u in h["monthly"]]), ("Parts Used", h["parts"])]))
        v.addLayout(_hrow(_muted("Completed production orders. Completed = when the order was marked Complete "
                                 "(team server audit trail), else its finishing end date."),
                          QLabel("From"), d0, QLabel("to"), d1, pull, exp))
        if not h["orders"]:
            v.addWidget(_muted("No completed production orders in this period."))
            return w
        units = sum(r["Qty"] for r in h["orders"])
        late = [r for r in h["orders"] if r["Days Late"] is not None and r["Days Late"] > 0]
        v.addWidget(QLabel(f"<b>{len(h['orders'])} orders · {units:g} units built</b>"
                           + (f" · <span style='color:{theme.WARN}'>{len(late)} finished late</span>" if late else "")))
        if h["monthly"]:
            v.addWidget(BarChart([m for m, _u in h["monthly"]], [("Units", [u for _m, u in h["monthly"]],
                                                                   theme.ACCENT_LINE)]))
        v.addWidget(_h("By project"))
        v.addWidget(_table(["Project", "Orders", "Units", "On Time %", "Avg Order → Done Days"], h["projects"], 0))
        v.addWidget(_h("Orders"))
        v.addWidget(_table(["Project", "Qty", "Ordered", "Due", "Completed", "Days Late", "Fab Days", "Finishing Days",
                            "Order → Done Days"], h["orders"], 0))
        v.addWidget(_h("Parts used by these builds (project quantities × units)"))
        v.addWidget(_table(["Part", "Part Number", "Qty Used", "Cost"], h["parts"], 0, money=("Cost",)))
        return w

    # ---- discontinued
    def discontinued(self):
        w, v = self._page()
        rows = A.discontinued_rows(self.inv)
        exp = QPushButton("Export to Excel")
        exp.clicked.connect(lambda: _save_xlsx(self, "DIMS-discontinued.xlsx", [("Discontinued", rows)]))
        v.addLayout(_hrow(_muted("Parts marked discontinued (Edit part). They're never put on the reorder list; "
                                 "projects that still need them are listed so you can switch to the replacement."), exp))
        if not rows:
            v.addWidget(_muted("No discontinued parts."))
            return w
        t = _table(["Part", "Part Number", "Discontinued", "On Hand", "Needed by Projects", "Projects", "Replacement",
                    "Note"], rows, 0)
        for i, r in enumerate(rows):
            if r["Needed by Projects"]:
                t.item(i, 4).setForeground(QColor(theme.ERROR))
        v.addWidget(t, 1)
        return w


# ------------------------------------------------------------------ binning list
class BinningDialog(QDialog):
    def __init__(self, inv: M.Inventory, parent=None):
        super().__init__(parent)
        self.inv = inv
        self.setWindowTitle("Binning list")
        self.resize(900, 720)
        v = QVBoxLayout(self)
        self.rack = QComboBox()
        self.rack.addItem("All racks", None)
        for r in sorted({l.get("rack") for l in inv.locations if l.get("rack")}):
            self.rack.addItem(r, r)
        self.empty = QCheckBox("Include empty bins")
        self.empty.setChecked(True)
        self.low = QCheckBox("Only bins at / below reorder point")
        v.addLayout(_hrow(QLabel("Rack"), self.rack, self.empty, self.low, stretch_first=False))
        self.view = QTextBrowser()
        v.addWidget(self.view, 1)
        bb = QDialogButtonBox()
        for text, fn in (("Print…", self.print_), ("Save PDF…", self.pdf), ("Excel…", self.excel)):
            b = bb.addButton(text, QDialogButtonBox.ActionRole)
            b.clicked.connect(fn)
        bb.addButton(QDialogButtonBox.Close).clicked.connect(self.reject)
        v.addWidget(bb)
        for wdg in (self.rack, ):
            wdg.currentIndexChanged.connect(self.fill)
        for wdg in (self.empty, self.low):
            wdg.toggled.connect(self.fill)
        self.fill()

    def rows(self):
        return A.binning_rows(self.inv, self.rack.currentData(), self.empty.isChecked(), self.low.isChecked())

    def html(self):
        sub = f"{self.rack.currentText()} · printed {dt.datetime.now():%Y-%m-%d %H:%M}" + \
            (f" by {self.inv.userName}" if self.inv.userName else "")
        return A.binning_html(self.rows(), "DIMS binning list", sub)

    def fill(self, *_a):
        self.view.setHtml(self.html())

    def _doc(self):
        d = QTextDocument()
        d.setHtml(self.html())
        return d

    def print_(self):
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        pr = QPrinter(QPrinter.HighResolution)
        if QPrintDialog(pr, self).exec():
            self._doc().print_(pr)

    def pdf(self):
        from PySide6.QtPrintSupport import QPrinter
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF", f"DIMS-binning-list-{dt.date.today()}.pdf",
                                              "PDF (*.pdf)")
        if not path:
            return
        pr = QPrinter(QPrinter.HighResolution)
        pr.setOutputFormat(QPrinter.PdfFormat)
        pr.setOutputFileName(path)
        self._doc().print_(pr)

    def excel(self):
        _save_xlsx(self, f"DIMS-binning-list-{dt.date.today()}.xlsx", [("Binning list", self.rows())])


# ------------------------------------------------------------------ revision check
STATUS = {"ok": ("Up to date", theme.OK), "changed": ("File changed", theme.WARN),
          "newer": ("Newer revision", theme.ERROR), "mismatch": ("Rev mismatch", theme.ERROR),
          "missing": ("File missing", theme.ERROR), "unchecked": ("Not checked in", theme.MUTED),
          "no-file": ("No file", theme.MUTED)}


class VersionCheckDialog(QDialog):
    """Manufactured parts vs. their model / drawing files: newer revisions in the folder, files changed since
    they were checked in, revision letters that don't match."""

    def __init__(self, inv: M.Inventory, commit, parent=None):
        super().__init__(parent)
        self.inv, self.commit = inv, commit
        self.setWindowTitle("MFG revision check")
        self.resize(1050, 560)
        v = QVBoxLayout(self)
        v.addWidget(_muted("Each manufactured part's CAD model and drawing (Edit part) are compared with the revision "
                           "the inventory has: a higher 'Rev' file next to it, a file saved since it was checked in, or "
                           "a file name whose Rev doesn't match. Check in = record the current files as the part's "
                           "revision."))
        self.only = QCheckBox("Only parts that need attention")
        self.only.setChecked(True)
        self.only.toggled.connect(self.fill)
        v.addWidget(self.only)
        self.t = QTableWidget(0, 6)
        self.t.setHorizontalHeaderLabels(["Part", "Part Number", "Rev", "Status", "Detail", ""])
        self.t.verticalHeader().hide()
        self.t.setEditTriggers(QTableWidget.NoEditTriggers)
        h = self.t.horizontalHeader()
        for c in range(6):
            h.setSectionResizeMode(c, QHeaderView.Stretch if c == 4 else QHeaderView.ResizeToContents)
        v.addWidget(self.t, 1)
        bb = QDialogButtonBox()
        allb = bb.addButton("Check in all unchecked", QDialogButtonBox.ActionRole)
        allb.clicked.connect(self.check_in_unchecked)
        exp = bb.addButton("Excel…", QDialogButtonBox.ActionRole)
        exp.clicked.connect(lambda: _save_xlsx(self, "DIMS-revision-check.xlsx",
                                               [("Revision check", A.version_report(self.inv))]))
        bb.addButton(QDialogButtonBox.Close).clicked.connect(self.reject)
        v.addWidget(bb)
        self.fill()

    def fill(self, *_a):
        rows = A.version_report(self.inv)
        if self.only.isChecked():
            rows = [r for r in rows if r["Status"] not in ("ok", "no-file")]
        self.t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            lab, col = STATUS.get(r["Status"], (r["Status"], theme.MUTED))
            for j, val in enumerate((r["Part"], r["Part Number"], r["Revision"] or "—", lab, r["Detail"])):
                it = QTableWidgetItem(val)
                if j == 3:
                    it.setForeground(QColor(col))
                self.t.setItem(i, j, it)
            p = self.inv.part(r["id"])
            box = QWidget()
            hb = QHBoxLayout(box)
            hb.setContentsMargins(0, 0, 0, 0)
            if r["Status"] == "newer":
                b = QPushButton("Use newest")
                b.setToolTip("Point the part at the newest revision files and check them in")
                b.clicked.connect(lambda _=False, pp=p: self._accept(pp, newest=True))
                hb.addWidget(b)
            if r["Status"] in ("changed", "unchecked", "mismatch", "newer"):
                b = QPushButton("Check in")
                b.clicked.connect(lambda _=False, pp=p: self._accept(pp))
                hb.addWidget(b)
            for k, lab2 in (("cadFile", "Model"), ("drawingUrl", "Drawing")):
                if p.get(k):
                    b = QPushButton(lab2)
                    b.clicked.connect(lambda _=False, u=p[k]: __import__("daedalus.ui.inventory_ui", fromlist=["x"])
                                      ._open_link(u))
                    hb.addWidget(b)
            self.t.setCellWidget(i, 5, box)
        self.t.resizeRowsToContents()

    def _accept(self, p, newest=False):
        from PySide6.QtWidgets import QInputDialog
        found = [A.rev_from_name(str(p.get(k) or "")) for k in ("cadFile", "drawingUrl")]
        guess = max([f for f in found if f] or [p.get("revision") or ""], key=A.rev_key)
        rev, ok = QInputDialog.getText(self, "Check in", f"Revision for {p['name']}:", text=guess)
        if not ok:
            return
        A.accept_version(p, rev.strip() or None, newest=newest)
        p["updatedAt"] = M.now_ms()
        self.commit(f"{p['name']} checked in at Rev {p['revision'] or '—'}")
        self.fill()

    def check_in_unchecked(self):
        n = 0
        for p in self.inv.parts:
            if p.get("type") == "manufactured" and A.version_status(p)["status"] == "unchecked":
                A.accept_version(p)
                p["updatedAt"] = M.now_ms()
                n += 1
        if n:
            self.commit(f"Checked in {n} part(s)")
        else:
            QMessageBox.information(self, "Check in", "Nothing waiting to be checked in.")
        self.fill()
