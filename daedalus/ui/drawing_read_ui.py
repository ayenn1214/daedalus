"""DIMS ▸ Parts ▸ Read drawings…: pull part number, description, revision, weight, material, finish, BOM and
revision history out of drawing PDFs, match each drawing to a part already in DIMS by part number, and choose
what goes into the part profile before anything is written."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..inventory import drawing_pdf as D
from ..inventory import model as M
from . import theme


def newest_only(paths) -> list[str]:
    """Several revisions of one drawing in a folder ('C20-0040 Rev A.pdf', '… Rev B.pdf') → the highest one."""
    from ..inventory import analytics as A
    best: dict = {}
    for p in paths:
        f = Path(p)
        key = (str(f.parent).lower(), A._base(f.name))
        r = A.rev_from_name(f.name) or ""
        if key not in best or A.rev_key(r) > A.rev_key(best[key][0]):
            best[key] = (r, p)
    keep = {v[1] for v in best.values()}
    return [p for p in paths if p in keep]


class PickDialog(QDialog):
    """Files or a whole folder (with subfolders), newest revision of each drawing only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Read drawings")
        self.s = QSettings("Daedalus", "Daedalus")
        self.paths: list[str] = []
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Read the title block, BOM and revisions of drawing PDFs into DIMS."))
        row = QHBoxLayout()
        fb = QPushButton("Pick PDF files…")
        fb.clicked.connect(self.files)
        db = QPushButton("Read a whole folder…")
        db.clicked.connect(self.folder)
        db.setDefault(True)
        row.addWidget(fb)
        row.addWidget(db)
        v.addLayout(row)
        self.sub = QCheckBox("Include subfolders")
        self.sub.setChecked(self.s.value("dims/drawing_sub", True, type=bool))
        self.newest = QCheckBox("Newest revision of each drawing only (skip older 'Rev' copies)")
        self.newest.setChecked(self.s.value("dims/drawing_newest", True, type=bool))
        v.addWidget(self.sub)
        v.addWidget(self.newest)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _start(self):
        return self.s.value("dims/drawing_dir") or str(Path.home())

    def _done(self, paths):
        self.s.setValue("dims/drawing_sub", self.sub.isChecked())
        self.s.setValue("dims/drawing_newest", self.newest.isChecked())
        if self.newest.isChecked():
            paths = newest_only(paths)
        self.paths = paths
        if paths:
            self.s.setValue("dims/drawing_dir", str(Path(paths[0]).parent))
            self.accept()

    def files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Drawing PDFs", self._start(), "PDF drawings (*.pdf)")
        if paths:
            self._done(paths)

    def folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder of drawing PDFs", self._start())
        if not d:
            return
        it = Path(d).rglob("*") if self.sub.isChecked() else Path(d).glob("*")
        paths = sorted(str(p) for p in it if p.suffix.lower() == ".pdf" and p.is_file())
        if not paths:
            QMessageBox.information(self, "Read drawings", "No PDFs in that folder.")
            return
        self.s.setValue("dims/drawing_dir", d)
        self._done(paths)


def pick_paths(parent) -> list[str]:
    d = PickDialog(parent)
    return d.paths if d.exec() else []


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
                bom = bool(prop["bom"]) and not has_comps
                state = ("unmatched" if not prop["partNumber"] else "new" if not exists
                         else "update" if keys or bom or any(not f["same"] for f in prop["fields"]) else "same")
                self.items.append({"path": p, "data": d, "prop": prop, "keys": keys, "bom": bom, "missing": False,
                                   "create": True, "state": state,
                                   "use": state in ("new", "update") and (bool(keys) or bom or state == "new")})
            except Exception as e:  # noqa: BLE001
                self.items.append({"path": p, "error": str(e), "use": False, "state": "error"})
        prog.setValue(len(paths))
        # the same part number twice: the newest file wins, the others are shown but not applied
        import os
        seen = {}
        for it in self.items:
            if "error" in it or not it["prop"]["partNumber"]:
                continue
            k = M._pn(it["prop"]["partNumber"])
            try:
                t = os.path.getmtime(it["path"])
            except OSError:
                t = 0
            if k in seen:
                other, ot = seen[k]
                loser = it if t <= ot else other
                loser["state"], loser["use"] = "duplicate", False
                if loser is other:
                    seen[k] = (it, t)
            else:
                seen[k] = (it, t)
        self.batch_pns = {M._pn(it["prop"]["partNumber"]) for it in self.items
                          if "error" not in it and it["prop"]["partNumber"] and it["state"] != "duplicate"}
        # new parts: inside another drawing's BOM → counted through it; otherwise suggest a project
        parents = {}
        for it in self.items:
            if "error" in it:
                continue
            for b in it["prop"]["bom"]:
                if b.get("partNumber"):
                    parents.setdefault(M._pn(b["partNumber"]), it["prop"]["partNumber"])
        for p_ in self.inv.parts:                               # already a component of a part in DIMS?
            for c in p_.get("components") or []:
                ch = self.inv.part(c.get("partId"))
                if ch and ch.get("partNumber"):
                    parents.setdefault(M._pn(ch["partNumber"]), p_.get("partNumber") or p_["name"])
        for it in self.items:
            if "error" in it or it["state"] != "new":
                continue
            par = parents.get(M._pn(it["prop"]["partNumber"]))
            it["parent"] = par
            it["project"] = None if par else D.project_from_path(self.inv, it["path"])
            it["newProject"] = ""
            it["projQty"] = 1

        v = QVBoxLayout(self)
        intro = QLabel("Each drawing is matched to DIMS by its part number. Tick what to write into the part profile — "
                       "values already in DIMS are only ticked when the part has none. Nothing is saved until "
                       "<b>Apply</b>.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        from PySide6.QtWidgets import QComboBox
        bar = QHBoxLayout()
        self.summary = QLabel()
        bar.addWidget(self.summary, 1)
        self.filter = QComboBox()
        for k, lab in (("all", "All drawings"), ("todo", "Ticked to apply"), ("update", "Updates to existing parts"),
                       ("new", "New parts"), ("same", "Up to date"), ("problem", "Couldn't read / no part number")):
            self.filter.addItem(lab, k)
        self.filter.currentIndexChanged.connect(lambda _i: self._apply_filter())
        bar.addWidget(self.filter)
        for lab, fn, tip in (("Tick blanks everywhere", self.tick_blanks,
                              "On every drawing: tick each field that's blank in DIMS (nothing gets overwritten)"),
                             ("Select all", lambda: self._select(True), "Apply every drawing that has something to add"),
                             ("Select none", lambda: self._select(False), "")):
            b = QPushButton(lab)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        v.addLayout(bar)
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
                status = {"new": "new part", "update": "in DIMS · updates", "same": "in DIMS · up to date",
                          "duplicate": "older copy — skipped"}.get(it["state"], "in DIMS")
                txt = f"{pr['partNumber'] or Path(it['path']).stem}   ·  {status}"
                tip = d.get("description") or ""
                col = {"new": theme.INFO, "same": theme.MUTED, "duplicate": theme.MUTED}.get(it["state"], theme.TEXT)
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
        self._apply_filter()
        self._count()

    def _count(self):
        n = sum(1 for it in self.items if it.get("use"))
        self.apply_btn.setText(f"Apply ({n})")
        self.apply_btn.setEnabled(n > 0)
        c = {}
        for it in self.items:
            c[it["state"]] = c.get(it["state"], 0) + 1
        parts = [f"<b>{len(self.items)}</b> drawing{'s' if len(self.items) != 1 else ''}"]
        for k, lab in (("update", "with updates"), ("new", "new parts"), ("same", "up to date"),
                       ("unmatched", "no part number"), ("error", "couldn't read")):
            if c.get(k):
                parts.append(f"{c[k]} {lab}")
        self.summary.setText(" · ".join(parts))

    def _apply_filter(self):
        k = self.filter.currentData()
        for r, it in enumerate(self.items):
            show = (k == "all" or (k == "todo" and it.get("use")) or it["state"] == k
                    or (k == "problem" and it["state"] in ("error", "unmatched")))
            self.list.setRowHidden(r, not show)

    def _select(self, on):
        for r, it in enumerate(self.items):
            if "error" in it or not it["prop"]["partNumber"] or self.list.isRowHidden(r):
                continue
            it["use"] = on and (it["state"] != "same" or bool(it["keys"]))
            li = self.list.item(r)
            self.list.blockSignals(True)
            li.setCheckState(Qt.Checked if it["use"] else Qt.Unchecked)
            self.list.blockSignals(False)
        self._count()

    def tick_blanks(self):
        for it in self.items:
            if "error" in it:
                continue
            exists = it["prop"]["part"] is not None
            add = {f["key"] for f in it["prop"]["fields"]
                   if not f["same"] and (not exists or str(f["current"] or "").strip() in ("", "-", "—"))}
            it["keys"] |= add
            if add:
                it["use"] = True
        self._fill_list()
        self.show_item(self.list.currentRow())

    def _use_changed(self, li):
        r = self.list.row(li)
        self.items[r]["use"] = li.checkState() == Qt.Checked
        self._count()

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
            self.dv.addLayout(self._project_row(it))
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
                in_batch = not hit and M._pn(b.get("partNumber")) in self.batch_pns
                st_ = QTableWidgetItem(f"✓ {hit['name']}" if hit else "in this batch" if in_batch else "not in DIMS")
                if in_batch:
                    st_.setForeground(QBrush(QColor(theme.INFO)))
                elif not hit:
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
    def _project_row(self, it):
        from PySide6.QtWidgets import QComboBox, QInputDialog, QSpinBox
        row = QHBoxLayout()
        if it.get("parent"):
            lab = QLabel(f"Goes into <b>{it['parent']}</b> — no project needed, it's counted through that "
                         "weldment / assembly.")
            lab.setWordWrap(True)
            row.addWidget(lab, 1)
            return row
        row.addWidget(QLabel("Project:"))
        combo = QComboBox()
        combo.addItem("— Not in a project —", None)
        for pr in [p for p in self.inv.projects if not p.get("archived")]:
            combo.addItem(pr["name"], pr["id"])
        combo.addItem("+ New project…", "__new__")
        if it.get("newProject"):
            combo.insertItem(combo.count() - 1, f"{it['newProject']} (new)", "new:" + it["newProject"])
        cur = ("new:" + it["newProject"]) if it.get("newProject") else it.get("project")
        combo.setCurrentIndex(max(0, combo.findData(cur)))
        qty = QSpinBox()
        qty.setRange(1, 100000)
        qty.setValue(int(it.get("projQty") or 1))
        qty.setSuffix(" per unit")
        qty.valueChanged.connect(lambda v: it.__setitem__("projQty", v))

        def chosen(_i):
            d = combo.currentData()
            if d == "__new__":
                name, ok = QInputDialog.getText(self, "New project", "Project name:",
                                                text=D.folder_project_name(it["path"]))
                name = name.strip()
                if ok and name:
                    ex = next((p for p in self.inv.projects if p["name"].lower() == name.lower()), None)
                    it["project"], it["newProject"] = (ex["id"], "") if ex else (None, name)
                QTimer.singleShot(0, lambda: self.show_item(self.list.currentRow()))
                return
            if isinstance(d, str) and d.startswith("new:"):
                it["project"], it["newProject"] = None, d[4:]
            else:
                it["project"], it["newProject"] = d, ""
            qty.setEnabled(bool(d))
        combo.currentIndexChanged.connect(chosen)
        qty.setEnabled(bool(cur))
        row.addWidget(combo, 1)
        row.addWidget(qty)
        if it.get("project") and not it.get("newProject"):
            hint = QLabel("from the folder name")
            hint.setStyleSheet(f"color:{theme.MUTED}")
            row.addWidget(hint)
        return row

    def apply(self):
        by = self.inv.userName or ""
        done, made = [], []
        for it in sorted(self.items, key=lambda x: bool("error" not in x and x["prop"]["bom"])):
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
                if pr["part"] is None and (it.get("project") or it.get("newProject")):
                    pid = it.get("project") or self.inv.find_or_create_project(it["newProject"])
                    self.inv.set_allocation(res["part"], pid, it.get("projQty") or 1)
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
