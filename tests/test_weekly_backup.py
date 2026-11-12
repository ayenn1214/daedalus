"""Weekly Excel backup: due from Friday until someone downloads it; one download clears it for everyone."""
import datetime as dt
import os

import openpyxl

from daedalus.inventory import backup as BK
from daedalus.inventory import model as M
from daedalus.inventory.store import InventoryStore


def ms(d):
    return int(d.timestamp() * 1000)


def test_due_rules():
    fri = dt.datetime(2026, 11, 13, 9, 0)          # a Friday
    assert fri.weekday() == 4
    assert BK.week_start(fri) == dt.datetime(2026, 11, 13)
    assert BK.week_start(dt.datetime(2026, 11, 17, 10)) == dt.datetime(2026, 11, 13)      # Tuesday → last Friday
    assert BK.week_start(dt.datetime(2026, 11, 12, 23)) == dt.datetime(2026, 11, 6)       # Thursday → previous
    assert BK.is_due({}, fri)                                                              # never backed up
    done_thu = {"weeklyBackupAt": ms(dt.datetime(2026, 11, 12, 16))}
    assert BK.is_due(done_thu, fri)                                                        # Friday: new week due
    assert not BK.is_due(done_thu, dt.datetime(2026, 11, 12, 18))                          # still last week's
    done_fri = {"weeklyBackupAt": ms(dt.datetime(2026, 11, 13, 8, 30))}
    assert not BK.is_due(done_fri, fri) and not BK.is_due(done_fri, dt.datetime(2026, 11, 19))
    assert BK.is_due(done_fri, dt.datetime(2026, 11, 20, 0, 1))                            # next Friday
    assert BK.is_due({"weeklyBackupAt": ms(dt.datetime(2026, 11, 5))}, dt.datetime(2026, 11, 16))  # missed: stays


def test_backup_workbook_and_shared_clear(tmp_path):
    f = tmp_path / "shared.json"
    a, b = InventoryStore(f), InventoryStore(f)
    a.inv.userName, b.inv.userName = "Ankur", "Mike"
    a.inv.parts.append(M.new_part(name="M8 bolt", currentStock=12, suppliers=[{"name": "Chris"}, {}, {}]))
    a.inv.vendors.append({"id": "v1", "name": "Fastenal", "category": "Fastener"})
    a.save()
    b.check_external()
    assert BK.is_due(a.inv.syncMeta) and BK.is_due(b.inv.syncMeta)
    out = BK.write_backup(a.inv, tmp_path / BK.default_name())
    wb = openpyxl.load_workbook(out)
    assert {"Inventory", "Activity Log", "Vendors", "Parts (all fields)", "Transactions"} <= set(wb.sheetnames)
    parts = wb["Parts (all fields)"]
    head = [c.value for c in parts[1]]
    assert "suppliers" in head and parts.cell(2, head.index("name") + 1).value == "M8 bolt"
    BK.mark_done(a.inv, out)
    a.save()
    import time
    time.sleep(0.02)
    os.utime(f)
    assert b.check_external() and not BK.is_due(b.inv.syncMeta)          # cleared for the other user
    assert BK.last_backup(b.inv.syncMeta)["by"] == "Ankur"


def test_banner_and_download(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog
    QApplication.instance() or QApplication([])
    from daedalus.ui.inventory_ui import InventoryPage
    st = InventoryStore(tmp_path / "inv.json")
    st.inv.parts.append(M.new_part(name="Pin"))
    st.save()
    page = InventoryPage(store=st, lite=True)
    seen = []
    page.backup_due.connect(seen.append)
    page._update_backup()
    assert page._backup_on is True and not page.backup_bar.isHidden()
    target = tmp_path / "bk.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    page.download_backup()
    assert target.exists() and page.backup_bar.isHidden() and seen == [False]
    assert not BK.is_due(InventoryStore(tmp_path / "inv.json").inv.syncMeta)
