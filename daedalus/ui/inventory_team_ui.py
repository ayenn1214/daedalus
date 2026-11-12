"""DIMS team server (Supabase): connect + sign in. The anon key and URL are kept in this PC's settings, the
session as a refresh token — never the password."""
from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from ..inventory import backends as B
from ..inventory.supabase import AuthError, SyncError


class TeamServerDialog(QDialog):
    def __init__(self, settings, current_store=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.store = None
        self.setWindowTitle("Team server (Supabase)")
        self.resize(520, 0)
        v = QVBoxLayout(self)
        intro = QLabel("Use the Supabase project the original web app used (same data), or a new free one. "
                       "Each person signs in with their own account — add accounts in Supabase ▸ Authentication ▸ "
                       "Users. No web hosting is needed.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        cfg = B.team_config(settings)
        f = QFormLayout()
        self.url = QLineEdit(cfg["url"])
        self.url.setPlaceholderText("https://abcdefgh.supabase.co")
        self.key = QLineEdit(cfg["key"])
        self.key.setPlaceholderText("anon public key (Project Settings ▸ API)")
        self.email = QLineEdit(getattr(current_store, "email", "") or "")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        f.addRow("Project URL", self.url)
        f.addRow("Anon key", self.key)
        f.addRow("Email", self.email)
        f.addRow("Password", self.pw)
        v.addLayout(f)
        self.upload = QCheckBox("Send this PC's current inventory to the server (merged with what's there)")
        self.upload.setChecked(False)
        self.upload.setVisible(getattr(current_store, "kind", "file") == "file")
        v.addWidget(self.upload)
        sql = QPushButton("Copy DIMS setup SQL")
        sql.setToolTip("Creates the DIMS tables, stock rules, roles, audit trail and access rules — paste into "
                       "Supabase ▸ SQL Editor ▸ Run. Safe to run again; leaves the old app_storage table alone.")
        sql.clicked.connect(lambda: (QGuiApplication.clipboard().setText(B.setup_sql()),
                                     self.note.setText("Setup SQL copied — paste it into Supabase ▸ SQL Editor and "
                                                       "Run, then sign in here. An admin can then import the old "
                                                       "data from Inventory ▸ Import / Export.")))
        v.addWidget(sql)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Sign in && connect")
        bb.accepted.connect(self.connect_)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def connect_(self):
        url, key = self.url.text().strip(), self.key.text().strip()
        if not url.startswith("http") or not key:
            self.note.setText("Enter the project URL (https://….supabase.co) and the anon key.")
            return
        try:
            st = B.connect_team(self.settings, url, key, self.email.text().strip(), self.pw.text())
        except AuthError as e:
            self.note.setText(f"Sign-in failed: {e}")
            return
        except SyncError as e:
            self.note.setText(f"{e}\nIf this is a new project, run the setup SQL first.")
            return
        self.pw.clear()
        self.store = st
        self.accept()


class SignInDialog(QDialog):
    def __init__(self, store, parent=None, reason=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Sign in — DIMS team server")
        v = QVBoxLayout(self)
        if reason:
            lab = QLabel(reason)
            lab.setWordWrap(True)
            v.addWidget(lab)
        f = QFormLayout()
        self.email = QLineEdit(store.email)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        f.addRow("Email", self.email)
        f.addRow("Password", self.pw)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Sign in")
        bb.accepted.connect(self.go)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def go(self):
        try:
            self.store.sign_in(self.email.text().strip(), self.pw.text())
        except SyncError as e:
            QMessageBox.warning(self, "Sign in", str(e))
            return
        self.accept()


class PeopleDialog(QDialog):
    """Admins: who has access, and what each person may do."""

    ROLES = [("admin", "Admin — everything, incl. roles"), ("editor", "Editor — add / edit / delete"),
             ("floor", "Floor — record stock in / out only"), ("viewer", "Viewer — read only")]

    def __init__(self, store, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QComboBox, QHeaderView, QTableWidget, QTableWidgetItem
        self.store = store
        self.setWindowTitle("DIMS — people & roles")
        self.resize(560, 360)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Add people in Supabase ▸ Authentication ▸ Users; they appear here as editors."))
        try:
            people = store.people()
        except SyncError as e:
            people = []
            v.addWidget(QLabel(str(e)))
        t = QTableWidget(len(people), 3)
        t.setHorizontalHeaderLabels(["Email", "Name", "Role"])
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        for r, p in enumerate(people):
            t.setItem(r, 0, QTableWidgetItem(p.get("email") or ""))
            t.setItem(r, 1, QTableWidgetItem(p.get("display_name") or ""))
            cb = QComboBox()
            for key, lab in self.ROLES:
                cb.addItem(lab, key)
            cb.setCurrentIndex(max(0, cb.findData(p.get("role"))))
            cb.setEnabled(store.role == "admin")
            cb.activated.connect(lambda _i, uid=p["user_id"], c=cb: self.change(uid, c.currentData()))
            t.setCellWidget(r, 2, cb)
        v.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def change(self, uid, role):
        try:
            self.store.set_role(uid, role)
        except SyncError as e:
            QMessageBox.warning(self, "Roles", str(e))
