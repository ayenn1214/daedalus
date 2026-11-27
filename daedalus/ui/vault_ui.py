"""File ▸ Versions… — check in, compare, restore, review and release the open part (core/vault.py)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import vault as V
from . import theme

STATE_COLOR = {"WIP": None, "In review": "#d9a400", "Released": "#3fb950", "Obsolete": "#8b949e"}


class CheckInDialog(QDialog):
    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Check in")
        self.resize(520, 360)
        v = QVBoxLayout(self)
        last = V.latest(doc.path) if doc.path else None
        txt = V.describe(V.compare(doc.path, last["v"], doc=doc)) if last else "First version of this part."
        v.addWidget(QLabel(f"<b>Changes since version {last['v']}</b>" if last else "<b>New history</b>"))
        ch = QPlainTextEdit(txt)
        ch.setReadOnly(True)
        v.addWidget(ch, 1)
        v.addWidget(QLabel("What changed and why"))
        self.msg = QLineEdit()
        self.msg.setPlaceholderText("e.g. Thicker web for the 400 kg load case")
        v.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = bb.addButton("Check in", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        bb.accepted.connect(lambda: self.accept() if self.msg.text().strip() else self.msg.setFocus())
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class VersionsDialog(QDialog):
    COLS = ["Version", "Rev", "State", "Date", "By", "Message", "Mass kg"]

    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self.window_ = window
        self.doc = window.doc
        self.setWindowTitle(f"Versions — {Path(self.doc.path).name if self.doc.path else 'unsaved part'}")
        self.resize(1000, 640)
        v = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        sp = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.show_detail)
        sp.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        sp.addWidget(self.detail)
        sp.setSizes([360, 220])
        v.addWidget(sp, 1)
        r1 = QHBoxLayout()
        self.b_check = self._b(r1, "Check in…", self.check_in, True, "Save and record this as the next version")
        self._b(r1, "Compare", self.compare, tip="Two selected versions, or one against the working file")
        self._b(r1, "Restore", self.restore, tip="Put the selected version into the working file (undoable)")
        self._b(r1, "Open copy", self.open_copy, tip="Open the selected version read-only in a new window")
        r1.addStretch(1)
        v.addLayout(r1)
        r2 = QHBoxLayout()
        self._b(r2, "Submit for review", self.submit)
        self._b(r2, "Approve", lambda: self.review(True))
        self._b(r2, "Reject…", lambda: self.review(False))
        self._b(r2, "Release…", self.release, tip="Next revision letter, release package, revision rows")
        self._b(r2, "Make obsolete", self.obsolete)
        self._b(r2, "Release folder", self.open_folder)
        r2.addStretch(1)
        self.req = QCheckBox("Require approval before release")
        self.req.toggled.connect(self._req)
        r2.addWidget(self.req)
        v.addLayout(r2)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.refresh()

    def _b(self, row, text, fn, primary=False, tip=""):
        b = QPushButton(text)
        if primary:
            b.setObjectName("primary")
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    # ------------------------------------------------------------------ view
    def refresh(self, select=None):
        if not self.doc.path:
            self.head.setText("Save the part first — its history is kept beside the file (Name.history).")
            self.table.setRowCount(0)
            return
        idx = V.load_index(self.doc.path)
        st = V.status(self.doc)
        rel = f"Released: <b>Rev {st['released']}</b>" if st.get("released") else "Not released yet"
        mod = f" · <span style='color:{theme.ERROR}'>working file changed since version {st['latest']}</span>" \
            if st.get("tracked") and st.get("modified") else ""
        self.head.setText(f"{rel} · {len(idx['versions'])} version(s) in {V.history_dir(self.doc.path).name}{mod}")
        self.req.blockSignals(True)
        self.req.setChecked(bool(idx.get("require_approval")))
        self.req.blockSignals(False)
        self.table.setRowCount(0)
        for e in reversed(idx["versions"]):
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [str(e["v"]), e.get("rev") or "", e["state"], e["date"].replace("T", " ")[:16], e["author"],
                    e["message"], "" if e.get("mass_kg") is None else f"{e['mass_kg']:g}"]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(t)
                it.setData(Qt.UserRole, e["v"])
                if c == 2 and STATE_COLOR.get(e["state"]):
                    from PySide6.QtGui import QColor
                    it.setForeground(QColor(STATE_COLOR[e["state"]]))
                self.table.setItem(r, c, it)
            if select == e["v"]:
                self.table.selectRow(r)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)

    def selected(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def show_detail(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        e = next(x for x in V.versions(self.doc.path) if x["v"] == sel[0])
        L = [f"Version {e['v']} · {e['state']}" + (f" · Rev {e['rev']}" if e.get("rev") else ""),
             f"{e['date'].replace('T', ' ')} · {e['author']}", e["message"],
             f"{e.get('features')} features · {e.get('objects')} object(s) · {e.get('drawings')} drawing(s) · "
             f"mass {e.get('mass_kg')} kg", ""]
        for a in e.get("approvals", []):
            L.append(f"{'Approved' if a['approved'] else 'Rejected'} by {a['by']} {a['date'][:10]}"
                     + (f" — {a['note']}" if a.get("note") else ""))
        for h in e.get("history", []):
            L.append(f"{h['date'][:16].replace('T', ' ')}  {h['what']} ({h['by']})" + (f" — {h['note']}" if h.get("note")
                                                                                     else ""))
        if e.get("package"):
            L += ["", "Release package:"] + [f"  {Path(p).name}" for p in e["package"]]
        if e.get("package_error"):
            L.append(f"Package problem: {e['package_error']}")
        self.detail.setPlainText("\n".join(L))

    def _err(self, e):
        QMessageBox.warning(self, "Versions", str(e))

    # ------------------------------------------------------------------ actions
    def check_in(self):
        if not self.doc.path:
            if not self.window_.save_doc():
                return
        if V.versions(self.doc.path) and not V.changed_since(self.doc):
            return self._err("No changes since the last version.")
        d = CheckInDialog(self.doc, self)
        if not d.exec():
            return
        try:
            e = V.check_in(self.doc, d.msg.text())
        except V.VaultError as ex:
            return self._err(ex)
        self.window_._update_title() if hasattr(self.window_, "_update_title") else None
        self.refresh(e["v"])

    def compare(self):
        sel = self.selected()
        try:
            if len(sel) == 2:
                d = V.compare(self.doc.path, min(sel), max(sel))
                head = f"Version {min(sel)} → {max(sel)}"
            elif len(sel) == 1:
                d = V.compare(self.doc.path, sel[0], doc=self.doc)
                head = f"Version {sel[0]} → working file"
            else:
                return self._err("Select one version (compared with the working file) or two.")
        except V.VaultError as ex:
            return self._err(ex)
        self.detail.setPlainText(head + "\n\n" + V.describe(d))

    def restore(self):
        sel = self.selected()
        if len(sel) != 1:
            return self._err("Select one version.")
        if QMessageBox.question(self, "Restore", f"Put version {sel[0]} into the working file? (Edit ▸ Undo takes it "
                                "back; check in afterwards to record it.)") != QMessageBox.Yes:
            return
        try:
            V.restore(self.doc, sel[0])
        except V.VaultError as ex:
            return self._err(ex)
        self.refresh()

    def open_copy(self):
        sel = self.selected()
        if len(sel) != 1:
            return self._err("Select one version.")
        doc = V.open_version(self.doc.path, sel[0])
        opener = getattr(self.window_, "open_document_copy", None)
        if opener:
            opener(doc, f"{Path(self.doc.path).stem} v{sel[0]} (read-only)")
        else:
            self._err("This window can't open a second part.")

    def submit(self):
        self._lifecycle(lambda v: V.submit(self.doc.path, v))

    def review(self, ok):
        note = ""
        if not ok:
            note, go = QInputDialog.getText(self, "Reject", "What needs to change?")
            if not go:
                return
        self._lifecycle(lambda v: V.review(self.doc.path, v, ok, note=note))

    def release(self):
        sel = self.selected()
        if len(sel) != 1:
            return self._err("Select one version.")
        letter = V.next_letter(self.doc.path)
        note, go = QInputDialog.getText(self, "Release", f"Release version {sel[0]} as Rev {letter}.\n"
                                        "Release note (goes in the drawings' revision table):")
        if not go:
            return
        try:
            e = V.release(self.doc.path, sel[0], note=note, doc=self.doc)
        except V.VaultError as ex:
            return self._err(ex)
        self._sync_dims(e)
        self.refresh(sel[0])
        msg = f"Released as Rev {e['rev']}."
        if e.get("package"):
            msg += f"\n\nRelease package ({len(e['package'])} files):\n{V.release_dir(self.doc.path, e['rev'])}"
        QMessageBox.information(self, "Released", msg)

    def _sync_dims(self, e):
        """The DIMS part with this file's name / part number gets the new revision."""
        try:
            from ..inventory.store import get_store
            store = get_store()
            stem = Path(self.doc.path).stem.lower()
            p = next((p for p in store.inv.parts if p.get("type") == "manufactured" and
                      stem in ((p.get("partNumber") or "").lower(), (p.get("name") or "").lower())), None)
            if p is not None:
                p["revision"] = e["rev"]
                store.save()
        except Exception:  # noqa: BLE001
            pass

    def obsolete(self):
        self._lifecycle(lambda v: V.obsolete(self.doc.path, v))

    def _lifecycle(self, fn):
        sel = self.selected()
        if len(sel) != 1:
            return self._err("Select one version.")
        try:
            fn(sel[0])
        except (V.VaultError, StopIteration) as ex:
            return self._err(ex)
        self.refresh(sel[0])

    def _req(self, on):
        if self.doc.path:
            V.set_require_approval(self.doc.path, on)

    def open_folder(self):
        if not self.doc.path:
            return
        rel = next((e for e in reversed(V.versions(self.doc.path)) if e.get("rev")), None)
        p = V.release_dir(self.doc.path, rel["rev"]) if rel else V.history_dir(self.doc.path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.exists() else Path(self.doc.path).parent)))
