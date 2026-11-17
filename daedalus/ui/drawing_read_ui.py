"""DIMS ▸ Parts ▸ Read drawings…: pull part number, description, revision, weight, material, finish, BOM and
revision history out of drawing PDFs, match each drawing to a part already in DIMS by part number, and choose
what goes into the part profile before anything is written."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import drawing_pdf as D
from . import theme


def pick_paths(parent) -> list[str]:
    s = QSettings("Daedalus", "Daedalus")
    start = s.value("dims/drawing_dir") or str(Path.home())
    paths, _ = QFileDialog.getOpenFileNames(parent, "Read drawing PDFs (pick one or many — or Cancel to pick a folder)",
                                            start, "PDF drawings (*.pdf)")
    if not paths:
        folder = QFileDialog.getExistingDirectory(parent, "…or read every PDF in a folder", start)
        if not folder:
            return []
        paths = sorted(str(p) for p in Path(folder).rglob("*.pdf"))
    if paths:
        s.setValue("dims/drawing_dir", str(Path(paths[0]).parent))
    return paths


def _fmt(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class ReadDrawingsDialog(QDialog):
    def __init__(self, inv, paths, commit, parent=None):
        super().__init__(parent)
        self.inv, self.commit_fn = inv, commit
        self.setWindowTitle("Read drawings into DIMS")
        self.resize(1180, 720)
        self.items: list[dict] = []                 # {path, data | error, prop, keys, bom, missing, create, use}
        prog = QProgressDialog("Reading drawings…", "Stop", 0, len(paths), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(400)
        for i, p in enumerate(paths):
            prog.setValue(i)
            prog.setLabelText(f"Reading {Path(p).name}…")
            QApplication.processEvents()
            if prog.wasCanceled():
                break
            try:
                d = D.parse(p)
                prop = D.proposals(inv, d)
                exists = prop["part"] is not None
                keys = {f["key"] for f in prop["fields"]
                        if not f["same"] and (not exists or str(f["current"] or "").strip() in ("", "-", "—"))}
                has_comps = bool(exists and prop["part"].get("components"))
                self.items.append({"path": p, "data": d, "prop": prop, "keys": keys,
                                   "bom": bool(prop["bom"]) and not has_comps, "missing": False,
                                   "create": True, "use": bool(prop["partNumber"])})
            except Exception as e:  # noqa: BLE001
                self.items.append({"path": p, "error": str(e), "use": False})
        prog.setValue(len(paths))

        v = QVBoxLayout(self)
        intro = QLabel("Each drawing is matched to DIMS by its part number. Tick what to write into the part profile — "
                       "values already in DIMS are only ticked when the part has none. Nothing is saved until "
                       "<b>Apply</b>.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        sp = QSplitter()
        self.list = QListWidget()
        self.list.setMinimumWidth(270)
        self.list.currentRowChanged.connect(self.show_item)
        self.list.itemChanged.connect(self._use_changed)
        sp.addWidget(self.list)
        self.detail = QWidget()
        self.dv = QVBoxLayout(self.detail)
        self.dv.setContentsMargins(8, 0, 0, 0)
        sp.addWidget(self.detail)
        sp.setStretchFactor(1, 1)
        v.addWidget(sp, 1)
        bb = QDialogButtonBox()
        self.apply_btn = bb.addButton("Apply", QDialogButtonBox.AcceptRole)
        self.apply_btn.setDefault(True)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill_list()
        if self.items:
            self.list.setCurrentRow(0)

    # ---------------------------------------------------------------- list
    def _fill_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for it in self.items:
            if "error" in it:
                txt, tip, col = f"✕  {Path(it['path']).name}", it["error"], theme.ERROR
            else:
                pr, d = it["prop"], it["data"]
                status = "in DIMS" if pr["part"] else "new part"
                txt = f"{pr['partNumber'] or Path(it['path']).stem}   ·  {status}"
                tip, col = d.get("description") or "", theme.TEXT if pr["part"] else theme.INFO
                if not pr["partNumber"]:
                    txt, col = f"?  {Path(it['path']).name}  ·  no part number found", theme.WARN
            li = QListWidgetItem(txt)
            li.setToolTip(f"{it['path']}\n{tip}")
            li.setForeground(QBrush(QColor(col)))
            if "error" not in it and it["prop"]["partNumber"]:
                li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
                li.setCheckState(Qt.Checked if it["use"] else Qt.Unchecked)
            self.list.addItem(li)
        self.list.blockSignals(False)
        n = sum(1 for it in self.items if it.get("use"))
        self.apply_btn.setText(f"Apply ({n})")
        self.apply_btn.setEnabled(n > 0)

    def _use_changed(self, li):
        r = self.list.row(li)
        self.items[r]["use"] = li.checkState() == Qt.Checked
        n = sum(1 for it in self.items if it.get("use"))
        self.apply_btn.setText(f"Apply ({n})")
        self.apply_btn.setEnabled(n > 0)

    # ---------------------------------------------------------------- detail
    def _clear(self):
        while self.dv.count():
            w = self.dv.takeAt(0)
            if w.widget():
                w.widget().hide()
                w.widget().deleteLater()
            elif w.layout():
                lay = w.layout()
                while lay.count():
                    x = lay.takeAt(0)
                    if x.widget():
                        x.widget().hide()
                        x.widget().deleteLater()

    def show_item(self, row):
        self._clear()
        if row < 0 or row >= len(self.items):
            return
        it = self.items[row]
        top = QHBoxLayout()
        name = QLabel(f"<b>{Path(it['path']).name}</b>")
        top.addWidget(name, 1)
        ob = QPushButton("Open PDF")
        ob.clicked.connect(lambda: __import__("daedalus.ui.pdf_ui", fromlist=["x"]).open_pdf(it["path"], self))
        top.addWidget(ob)
        self.dv.addLayout(top)
        if "error" in it:
            e = QLabel(it["error"])
            e.setWordWrap(True)
            e.setStyleSheet(f"color:{theme.ERROR}")
            self.dv.addWidget(e)
            self.dv.addStretch(1)
            return
        d, pr = it["data"], it["prop"]
        part = pr["part"]
        if part:
            st = QLabel(f"<b>{pr['partNumber']}</b> is already in DIMS: <b>{part['name']}</b>"
                        + (f" · Rev {part['revision']}" if part.get("revision") else "")
                        + " — tick what to add to its profile.")
        elif pr["partNumber"]:
            st = QLabel(f"<b>{pr['partNumber']}</b> isn't in DIMS yet.")
            cb = QCheckBox("Create it as a new MFG part")
            cb.setChecked(it["create"])
            cb.toggled.connect(lambda on: it.__setitem__("create", on))
        else:
            st = QLabel("No part number found on this drawing — it can't be matched.")
        st.setWordWrap(True)
        self.dv.addWidget(st)
        if not part and pr["partNumber"]:
            self.dv.addWidget(cb)
        if d.get("source") == "ocr":
            o = QLabel("Read by OCR (scanned drawing) — check the values.")
            o.setStyleSheet(f"color:{theme.WARN}")
            self.dv.addWidget(o)

        # fields
        t = QTableWidget(len(pr["fields"]), 4)
        t.setHorizontalHeaderLabels(["", "Field", "In DIMS now", "From the drawing"])
        t.verticalHeader().hide()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, f in enumerate(pr["fields"]):
            box = QCheckBox()
            box.setChecked(f["key"] in it["keys"])
            box.setEnabled(not f["same"])
            box.toggled.connect(lambda on, k=f["key"]: it["keys"].add(k) if on else it["keys"].discard(k))
            cw = QWidget()
            cl = QHBoxLayout(cw)
            cl.setContentsMargins(6, 0, 0, 0)
            cl.addWidget(box)
            t.setCellWidget(r, 0, cw)
            t.setItem(r, 1, QTableWidgetItem(f["label"]))
            cur = QTableWidgetItem(_fmt(f["current"]) if part else "(new part)")
            new = QTableWidgetItem(_fmt(Path(f["new"]).name if f["key"] == "drawingUrl" else f["new"]))
            if f["key"] == "drawingUrl":
                new.setToolTip(f["new"])
                cur.setToolTip(str(f["current"] or ""))
            if f["same"]:
                new.setForeground(QBrush(QColor(theme.MUTED)))
                new.setText(new.text() + "   (same)")
            elif part and f["current"] not in (None, ""):
                new.setForeground(QBrush(QColor(theme.WARN)))
                new.setToolTip("Different from DIMS — tick to replace")
            t.setItem(r, 2, cur)
            t.setItem(r, 3, new)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        t.setMaximumHeight(34 + 30 * max(1, len(pr["fields"])))
        self.dv.addWidget(t)

        # BOM
        if pr["bom"]:
            kind = "weldment" if pr["isWeldment"] else "assembly"
            have = len(part.get("components") or []) if part else 0
            bomcb = QCheckBox(f"Set these as the {kind}'s parts"
                              + (f" (replaces the {have} it has now)" if have else ""))
            bomcb.setChecked(it["bom"])
            bomcb.toggled.connect(lambda on: it.__setitem__("bom", on))
            self.dv.addWidget(bomcb)
            bt = QTableWidget(len(pr["bom"]), 5)
            bt.setHorizontalHeaderLabels(["Item", "Part number", "Description", "Qty", "In DIMS"])
            bt.verticalHeader().hide()
            bt.setEditTriggers(QTableWidget.NoEditTriggers)
            missing = 0
            for r, b in enumerate(pr["bom"]):
                for c, val in enumerate((b["item"], b["partNumber"], b["description"], _fmt(b["qty"]))):
                    bt.setItem(r, c, QTableWidgetItem(str(val)))
                hit = b.get("part")
                st_ = QTableWidgetItem(f"✓ {hit['name']}" if hit else "not in DIMS")
                if not hit:
                    missing += 1
                    st_.setForeground(QBrush(QColor(theme.WARN)))
                bt.setItem(r, 4, st_)
            bh = bt.horizontalHeader()
            for c in range(5):
                bh.setSectionResizeMode(c, QHeaderView.Stretch if c in (2, 4) else QHeaderView.ResizeToContents)
            bt.setMaximumHeight(40 + 31 * len(pr["bom"]))
            bt.setMinimumHeight(min(240, 40 + 31 * len(pr["bom"])))
            self.dv.addWidget(bt)
            if missing:
                mc = QCheckBox(f"Create the {missing} missing part{'s' if missing != 1 else ''} (MFG, from the BOM)")
                mc.setChecked(it["missing"])
                mc.toggled.connect(lambda on: it.__setitem__("missing", on))
                self.dv.addWidget(mc)

        # history and the rest (shown; revisions are kept on the part)
        extra = []
        if pr["revisions"]:
            extra.append("<b>Revisions</b> (kept in the part's history): " + "; ".join(
                f"{r['rev']} — {r['description']} ({r['date']}{', ' + r['by'] if r['by'] else ''})"
                for r in pr["revisions"]))
        info = [f"Drawn {d['drawnBy']}" if d.get("drawnBy") else "", f"date {d['drawnDate']}" if d.get("drawnDate") else "",
                f"scale {d['scale']}" if d.get("scale") else "", f"sheet {d['sheet']}" if d.get("sheet") else "",
                f"material {d['materialNote'].lower()}" if d.get("materialNote") else ""]
        if any(info):
            extra.append(" · ".join(x for x in info if x))
        if d.get("tolerances"):
            extra.append("Tolerances: " + ", ".join(f"{k} ±{v}" for k, v in d["tolerances"].items()))
        if d.get("notes"):
            extra.append("<b>Notes:</b> " + " · ".join(d["notes"]))
        if d.get("meta"):
            m = d["meta"]
            extra.append(f"<span style='color:{theme.MUTED}'>" + " · ".join(
                x for x in (m.get("creator"), m.get("author"), m.get("modified")) if x) + "</span>")
        if extra:
            lab = QLabel("<br>".join(extra))
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.dv.addWidget(lab)
        self.dv.addStretch(1)

    # ---------------------------------------------------------------- write
    def apply(self):
        by = self.inv.userName or ""
        done, made = [], []
        for it in self.items:
            if not it.get("use") or "error" in it:
                continue
            pr = it["prop"]
            if pr["part"] is None and not it["create"]:
                continue
            res = D.apply(self.inv, it["data"], pr, it["keys"], bom=it["bom"], create_missing=it["missing"],
                          create_part=it["create"], by=by)
            if res["part"] is not None:
                done.append(res["part"])
                made += res["created"]
        if not done:
            QMessageBox.information(self, "Read drawings", "Nothing to apply.")
            return
        msg = f"Read {len(done)} drawing{'s' if len(done) != 1 else ''} into DIMS" + \
              (f" · {len(made)} new part{'s' if len(made) != 1 else ''}" if made else "")
        self.commit_fn(msg)
        self.done_parts = done
        self.accept()


def read_drawings(page, paths=None):
    """From the DIMS page: pick PDFs (or a folder), review, apply."""
    paths = paths or pick_paths(page)
    if not paths:
        return
    dlg = ReadDrawingsDialog(page.inv, paths, lambda m: page.commit(m), page)
    dlg.exec()
