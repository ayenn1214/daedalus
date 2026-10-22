"""Parts library (ISO/ANSI fasteners, bearings), bill of materials with costing, linked part files."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core import standards as S
from ..core.features import FeatureError

THREADED = ("socket head cap screw", "hex bolt", "flat head screw")


def app_inventory():
    """The app-wide inventory store (file from settings), or None if it can't be opened."""
    try:
        from PySide6.QtCore import QSettings

        from ..inventory.store import get_store
        return get_store(QSettings("Daedalus", "Daedalus").value("inventory/path") or None)
    except Exception:  # noqa: BLE001
        return None


class LibraryDialog(QDialog):
    """Pick a standard part; with a hole picked in the viewport it is placed in it and mated."""

    def __init__(self, doc, pick=None, parent=None):
        super().__init__(parent)
        self.doc, self.pick = doc, pick
        self.setWindowTitle("Parts library")
        f = QFormLayout(self)
        self.kind = QComboBox()
        for k, std in S.KINDS.items():
            self.kind.addItem(f"{k.title()}  ·  {std}", k)
        self.system = QComboBox()
        self.system.addItems(["Metric (ISO)", "Inch (ANSI)"])
        self.size = QComboBox()
        self.length = QComboBox()
        self.length.setEditable(True)
        self.nut = QCheckBox("Add nut")
        self.washer = QCheckBox("Add washer under the head")
        self.info = QLabel()
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.target = QLabel()
        self.target.setWordWrap(True)
        if pick and pick.get("type") == "cylinder" and pick.get("object"):
            self.target.setText(f"→ Into the picked hole on {pick['object']} (face {pick['index']}), mated "
                                f"concentric + seated on the face.")
        else:
            self.target.setText("No hole picked — the part is added at the origin. (Pick a hole face first to "
                                "place and mate it automatically.)")
        f.addRow("Part", self.kind)
        f.addRow("System", self.system)
        f.addRow("Size", self.size)
        f.addRow("Length", self.length)
        f.addRow(self.washer)
        f.addRow(self.nut)
        f.addRow(self.info)
        f.addRow(self.target)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Insert")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.kind.currentIndexChanged.connect(self._sizes)
        self.system.currentIndexChanged.connect(self._sizes)
        self.size.currentIndexChanged.connect(self._info)
        self.length.currentTextChanged.connect(self._info)
        self._sizes()

    def _sizes(self):
        k = self.kind.currentData()
        self.size.blockSignals(True)
        self.size.clear()
        if k == "ball bearing":
            self.system.setEnabled(False)
            self.size.addItems(list(S.BEARINGS))
        else:
            self.system.setEnabled(True)
            self.size.addItems(list(S.METRIC if self.system.currentIndex() == 0 else S.INCH))
            if self.system.currentIndex() == 0 and "M8" in S.METRIC:
                self.size.setCurrentText("M8")
        self.size.blockSignals(False)
        self.length.blockSignals(True)
        self.length.clear()
        on = k in THREADED
        self.length.setEnabled(on)
        if on:
            if self.system.currentIndex() == 0:
                self.length.addItems([f"{v:g}" for v in S.METRIC_LENGTHS])
                self.length.setCurrentText("25")
            else:
                self.length.addItems([f'{v:g}"' for v in S.INCH_LENGTHS])
                self.length.setCurrentText('1"')
        self.length.blockSignals(False)
        self.nut.setEnabled(k in ("socket head cap screw", "hex bolt"))
        self.washer.setEnabled(k in ("socket head cap screw", "hex bolt"))
        self._info()

    def _len(self):
        t = self.length.currentText().strip()
        if not t or not self.length.isEnabled():
            return 0.0
        try:
            return float(t.rstrip('"')) * (25.4 if t.endswith('"') or self.system.currentIndex() == 1 else 1.0)
        except ValueError:
            return 0.0

    def _info(self):
        k, sz = self.kind.currentData(), self.size.currentText()
        if not sz:
            return
        try:
            d = S.designation(k, sz, self._len())
            c = S.unit_cost(k, sz, self._len())
            extra = ""
            if k in THREADED or k in ("hex nut", "washer"):
                extra = f" · clearance hole Ø{S.hole_size(sz, 'normal'):g} · tap drill Ø{S.hole_size(sz, 'tap'):g}"
            st = app_inventory()
            hit = None
            if st is not None:
                from ..inventory.cad import stock_for_designation
                hit = stock_for_designation(st.inv, d)
            if hit:
                p, n, loc = hit
                extra += f"<br><b>Inventory:</b> {n:g} {p.get('unit') or 'ea'} in stock" + (f" · {loc}" if loc else "")
            elif st is not None:
                extra += "<br><b>Inventory:</b> not stocked"
            self.info.setText(f"{d} · ≈${c:.2f}{extra}")
        except Exception as e:  # noqa: BLE001
            self.info.setText(str(e))

    def insert(self):
        k, sz = self.kind.currentData(), self.size.currentText()
        target = None
        if self.pick and self.pick.get("type") == "cylinder" and self.pick.get("object"):
            target = {"obj": self.pick["object"], "face": {"face_index": self.pick["index"],
                                                           "pick": list(self.pick["local"])},
                      "near": list(self.pick["point"])}
        return self.doc.insert_part(k, sz, self._len(), target=target, nut=self.nut.isChecked() and
                                    self.nut.isEnabled(), washer=self.washer.isChecked() and self.washer.isEnabled())


class BomDialog(QDialog):
    COLS = [("item", "Item"), ("part", "Part no."), ("description", "Description"), ("material", "Material"),
            ("qty", "Qty"), ("unit_mass_kg", "Unit kg"), ("total_mass_kg", "Total kg"), ("unit_cost", "Unit $"),
            ("total_cost", "Total $")]

    INV_COLS = ["In stock", "Location", "Short"]

    def __init__(self, doc, parent=None, on_send=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("Bill of materials")
        self.resize(1100, 440)
        lay = QVBoxLayout(self)
        rows = doc.bom()
        self.store = app_inventory()
        t = QTableWidget(len(rows), len(self.COLS) + (len(self.INV_COLS) if self.store else 0))
        t.setHorizontalHeaderLabels([c for _, c in self.COLS] + (self.INV_COLS if self.store else []))
        self.table = t
        tot_cost = tot_mass = 0.0
        for i, r in enumerate(rows):
            for j, (k, _c) in enumerate(self.COLS):
                v = r.get(k, "")
                it = QTableWidgetItem(f"{v:.2f}" if isinstance(v, float) else str(v))
                if isinstance(v, (int, float)):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(i, j, it)
            tot_cost += float(r.get("total_cost", 0) or 0)
            tot_mass += float(r.get("total_mass_kg", 0) or 0)
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(t)
        lay.addWidget(QLabel(f"{sum(r['qty'] for r in rows)} parts · {tot_mass:.3f} kg · ≈${tot_cost:,.2f}  "
                             "(modelled parts: raw material × 1.15; purchased: catalogue estimate)"))
        h = QHBoxLayout()
        buttons = [("Export Excel…", lambda: self.export(".xlsx")), ("Export CSV…", lambda: self.export(".csv"))]
        if self.store is not None:
            from PySide6.QtWidgets import QSpinBox
            h.addWidget(QLabel("Builds:"))
            self.units = QSpinBox()
            self.units.setRange(1, 1_000_000)
            self.units.valueChanged.connect(self.fill_stock)
            h.addWidget(self.units)
            self.stock_lab = QLabel()
            h.addWidget(self.stock_lab)
            if on_send:
                buttons.insert(0, ("Send to Inventory…", lambda: (on_send(), self.fill_stock())))
            self.fill_stock()
        h.addStretch(1)
        for text, fn in buttons + [("Close", self.accept)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        lay.addLayout(h)

    def fill_stock(self):
        """Inventory columns: stock, bin, shortfall for the chosen number of builds."""
        from PySide6.QtGui import QColor

        from ..inventory.cad import stock_report
        from . import theme
        self.store.check_external()
        rep = stock_report(self.store.inv, self.doc, self.units.value())
        n0 = len(self.COLS)
        short = missing = 0
        for i, r in enumerate(rep):
            vals = ["—" if r["in_stock"] is None else f"{r['in_stock']:g}", r["location"] or "",
                    "not stocked" if r["status"] == "not in inventory" else (f"{r['short']:g}" if r["short"] else "")]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j != 1:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if j == 2 and v:
                    it.setForeground(QColor(theme.ERROR if r["status"] == "short" else theme.ACCENT_LINE))
                if r["on_order"]:
                    it.setToolTip(f"{r['on_order']:g} on order")
                self.table.setItem(i, n0 + j, it)
            short += r["status"] == "short"
            missing += r["status"] == "not in inventory"
        self.stock_lab.setText(f"  {short} short · {missing} not stocked · inventory: {self.store.path.name}")

    def export(self, ext):
        base = (self.doc.path.with_suffix("").name if self.doc.path else "bom") + "_BOM" + ext
        p, _ = QFileDialog.getSaveFileName(self, "Export BOM", base, "Excel (*.xlsx)" if ext == ".xlsx" else "CSV (*.csv)")
        if p:
            if not p.lower().endswith(ext):
                p += ext
            try:
                self.doc.export_bom(p)
            except FeatureError as e:
                return QMessageBox.warning(self, "BOM", str(e))
            QMessageBox.information(self, "BOM", f"Saved {Path(p).name}")
