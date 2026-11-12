"""DIMS table layout: row / column spacing, grid lines, text size, remembered column widths."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_table_layout(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QHeaderView, QTableWidget, QTableWidgetItem, QToolButton
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import table_prefs as TP
    s = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)

    def make():
        t = QTableWidget(3, 3)
        t.setHorizontalHeaderLabels(["Name", "Phone", ""])
        for r in range(3):
            t.setItem(r, 0, QTableWidgetItem(f"Vendor {r}"))
            t.setCellWidget(r, 2, QToolButton(text="Edit"))
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        t.setMinimumHeight(34 + 32 * 3)
        t.resize(600, 300)
        t.show()
        return t
    a = make()
    TP.apply(a, None, s)
    app.processEvents()
    base_row, base_w = a.rowHeight(0), a.horizontalHeader().sectionSize(1)
    assert a.horizontalHeader().sectionResizeMode(1) == QHeaderView.Interactive          # drag to resize
    assert a.horizontalHeader().sectionResizeMode(0) == QHeaderView.Stretch
    TP.save({**TP.DEFAULTS, "row_pad": 12, "col_pad": 20, "grid": True}, s)
    b = make()
    TP.apply(b, None, s)
    app.processEvents()
    assert b.rowHeight(0) >= base_row + 16 and b.horizontalHeader().sectionSize(1) >= base_w + 28
    assert b.showGrid() and b.minimumHeight() >= sum(b.rowHeight(r) for r in range(3))
    assert b.cellWidget(0, 2).height() >= 20                                             # buttons not squashed
    b.horizontalHeader().resizeSection(1, 333)                                           # user drags a column
    c = make()
    TP.apply(c, None, s)
    app.processEvents()
    assert c.horizontalHeader().sectionSize(1) == 333                                    # remembered
    TP.reset_widths(s)
    d = make()
    TP.apply(d, None, s)
    app.processEvents()
    assert d.horizontalHeader().sectionSize(1) != 333
    dlg = TP.TablePrefsDialog(lambda: None, None, s)
    dlg.row.setValue(3)
    assert TP.prefs(s)["row_pad"] == 3
    dlg.defaults()
    assert TP.prefs(s) == TP.DEFAULTS


def test_spreadsheet_style_controls(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QHeaderView, QTableWidget, QTableWidgetItem
    app = QApplication.instance() or QApplication([])
    from daedalus.ui import table_prefs as TP
    s = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)

    def make():
        t = QTableWidget(3, 3)
        t.setHorizontalHeaderLabels(["Name", "Phone", "Notes"])
        for r in range(3):
            t.setItem(r, 0, QTableWidgetItem("A rather long vendor name " * (r + 1)))
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        t.resize(700, 300)
        t.show()
        return t
    TP.save({**TP.DEFAULTS, "row_numbers": True}, s)
    a = make()
    TP.apply(a, None, s)
    app.processEvents()
    assert a.verticalHeader().isVisible()                                  # Excel-style row numbers
    key = TP.K + "widths/" + TP._key(a)
    # right-click menus: run the chosen action without showing the menu
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getInt", lambda *a_, **k: (55, True))

    def pick(menu, text):
        next(act for act in menu.actions() if act.text() == text).trigger()
    pick(TP.col_menu(a, a.horizontalHeader(), key, s, 2), "Hide “Notes”")
    assert a.horizontalHeader().isSectionHidden(2) and TP.table_state(s, key)["hidden"] == [2]
    assert any(act.text() == "Unhide" for act in TP.col_menu(a, a.horizontalHeader(), key, s, 0).actions())
    pick(TP.row_menu(a, key, s, 0), "Row height…")
    assert all(a.rowHeight(r) == 55 for r in range(3)) and TP.table_state(s, key)["rowh"] == 55
    a.horizontalHeader().resizeSection(0, 50)
    TP.autofit(a, 0)
    assert a.horizontalHeader().sectionSize(0) > 200                         # fits the longest name
    b = make()                                                                # next time the page is built
    TP.apply(b, None, s)
    app.processEvents()
    assert b.horizontalHeader().isSectionHidden(2) and b.rowHeight(0) == 55
    s.setValue(key, {"0": 123})                                               # old saved format still read
    assert TP.table_state(s, key)["w"] == {"0": 123}
