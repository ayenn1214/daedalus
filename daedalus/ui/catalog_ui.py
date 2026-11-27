"""DIMS ▸ Vendors ▸ Catalog lookup… — a vendor part number → description, specs, price tiers, CAD → new DIMS part,
refreshed prices, or a standard part with its 3-D model (inventory/catalog.py)."""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QProgressDialog, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..inventory import catalog as CAT
from ..inventory import model as M


class CatalogDialog(QDialog):
    def __init__(self, page, parent=None, provider=None):
        super().__init__(parent or page)
        self.page, self.inv = page, page.inv
        self.provider = provider
        self.info = None
        self.setWindowTitle("Catalog lookup")
        self.resize(760, 600)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.vendor = QComboBox()
        for name in CAT.SEARCH:
            self.vendor.addItem(name + ("" if name != "McMaster-Carr" else
                                        ("  · API" if (provider or CAT.configured()) else "  · website")), name)
        row.addWidget(self.vendor)
        self.pn = QLineEdit()
        self.pn.setPlaceholderText("vendor part number, e.g. 91290A396")
        self.pn.returnPressed.connect(self.lookup)
        row.addWidget(self.pn, 1)
        go = QPushButton("Look up")
        go.setObjectName("primary")
        go.clicked.connect(self.lookup)
        row.addWidget(go)
        v.addLayout(row)
        self.title = QLabel("")
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["", ""])
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.table, 1)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        r2 = QHBoxLayout()
        self.b_add = self._b(r2, "Add to DIMS", self.add_part, True)
        self.b_upd = self._b(r2, "Update a part…", self.update_part)
        self.b_lib = self._b(r2, "3-D model → standard parts", self.to_library)
        self._b(r2, "Open on website", self.open_web)
        r2.addStretch(1)
        v.addLayout(r2)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._enable(False)
        if not (provider or CAT.configured()):
            self.note.setText("McMaster-Carr's API needs an approved account (client certificate + sign-in, Settings ▸ "
                              "Suppliers). Without it, Look up opens the vendor's page.")

    def _b(self, row, text, fn, primary=False):
        b = QPushButton(text)
        if primary:
            b.setObjectName("primary")
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    def _enable(self, on):
        for b in (self.b_add, self.b_upd, self.b_lib):
            b.setEnabled(on)

    def _prov(self):
        if self.vendor.currentData() == "McMaster-Carr" and (self.provider or CAT.configured()):
            if self.provider is None:
                self.provider = CAT.McMaster()
            return self.provider
        return None

    def lookup(self):
        pn = self.pn.text().strip()
        if not pn:
            return
        prov = self._prov()
        if prov is None:
            self.open_web()
            return
        try:
            self.info = prov.product(pn)
        except CAT.CatalogError as e:
            self.info = None
            self._enable(False)
            self.title.setText("")
            self.table.setRowCount(0)
            return self.note.setText(str(e))
        i = self.info
        status = "" if i["active"] else " — <b>discontinued</b>" + (f", replaced by {i['suggested']}" if i["suggested"]
                                                                     else "")
        self.title.setText(f"<b>{i['partNumber']}</b> · {i['description']}<br>{i['family']}{status}")
        rows = [(f"{t['min']:g}+ {t['unit']}", f"{M.fmt_money(t['price'])} {i['currency']}") for t in i["tiers"]]
        rows += list(i["specs"].items())
        self.table.setRowCount(0)
        for k, val in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(k)))
            self.table.setItem(r, 1, QTableWidgetItem(str(val)))
        self.table.resizeColumnToContents(0)
        have = [p for p in self.inv.parts if any(s.get("partNumber") == i["partNumber"] for s in p.get("suppliers")
                                                  or [])]
        self.note.setText(("Already in DIMS: " + ", ".join(p["name"] for p in have[:3])) if have else
                          ("3-D model available." if i.get("step") else "No 3-D model."))
        self._enable(True)
        self.b_lib.setEnabled(bool(i.get("step")))

    def add_part(self):
        if not self.info:
            return
        p = CAT.new_part(self.inv, self.info)
        self.page.commit(f"Added {p['name']} from {self.info['vendor']}")
        self.note.setText(f"Added to DIMS as “{p['name']}” — set its stock and reorder point on the Parts tab.")

    def update_part(self):
        if not self.info:
            return
        names = sorted(p["name"] for p in self.inv.parts)
        name, ok = QInputDialog.getItem(self, "Update a part", f"Put {self.info['vendor']} {self.info['partNumber']} "
                                        "(price tiers, part number, link) on:", names, 0, True)
        if not ok:
            return
        p = next((p for p in self.inv.parts if p["name"] == name), None)
        if p is None:
            return
        s = CAT.apply_to_part(p, self.info)
        self.page.commit(f"{p['name']}: {self.info['vendor']} {M.fmt_money(s['price'])} {s['currency']}")

    def to_library(self):
        if not self.info:
            return
        try:
            sp = CAT.to_library(self._prov(), self.info)
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Catalog", str(e))
        self.note.setText(f"Added to the standard parts library: {sp.get('name')}")

    def open_web(self):
        QDesktopServices.openUrl(QUrl(CAT.search_url(self.vendor.currentData(), self.pn.text() or "")))


def refresh_prices_ui(page, provider=None):
    if not (provider or CAT.configured()):
        return QMessageBox.information(page, "Catalog", "Set up McMaster-Carr's API first (Settings ▸ Suppliers).")
    prov = provider or CAT.McMaster()
    dlg = QProgressDialog("Refreshing McMaster-Carr prices…", "Stop", 0, 0, page)
    dlg.setMinimumDuration(300)

    def prog(i, n, name):
        dlg.setMaximum(n)
        dlg.setValue(i)
        dlg.setLabelText(f"{name} ({i + 1} of {n})")
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        if dlg.wasCanceled():
            raise CAT.CatalogError("Stopped")
    try:
        r = CAT.refresh_prices(page.inv, prov, prog)
    except CAT.CatalogError as e:
        r = {"updated": 0, "discontinued": [], "failed": {"": str(e)}}
    dlg.close()
    msg = f"{r['updated']} price(s) refreshed"
    if r["discontinued"]:
        msg += "; discontinued: " + ", ".join(f"{d['part']} ({d['partNumber']}"
                                              + (f" → {d['suggested']})" if d["suggested"] else ")")
                                              for d in r["discontinued"][:5])
    if r["failed"]:
        msg += f"; {len(r['failed'])} failed"
    page.commit(msg)
    return r
