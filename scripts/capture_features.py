"""Still images of every Daedalus feature screen. Usage (Linux headless):
xvfb-run -a -s "-screen 0 1920x1080x24" python scripts/capture_features.py OUTDIR"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
OUT = sys.argv[1] if len(sys.argv) > 1 else "shots"
os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp()

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])
from daedalus.ui.theme import QSS  # noqa: E402

from daedalus.ui import theme as _theme; _theme.apply(app)
QSettings("Daedalus", "Daedalus").setValue("inventory/path", os.path.join(TMP, "inv.json"))
QInputDialog.getItem = staticmethod(lambda *a, **k: (a[3][0], True))
QInputDialog.getDouble = staticmethod(lambda *a, **k: (a[3], True))
QDialog.exec = lambda self: QDialog.Accepted          # auto-accept modal prompts with their defaults
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARN", a[1:3]) or QMessageBox.Ok)

from daedalus.ui.main_window import MainWindow  # noqa: E402

w = MainWindow()
w.resize(1680, 980)
w.show()
w.view.vtk.Initialize()


def pump(n=12):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.03)


def shot(name, fit=True, view="Isometric"):
    """Window + live GL viewport composited (grab() can't read the GL surface)."""
    pump()
    if w.stack.currentIndex() == 0:
        if view:
            w.view.set_view(view)
        if fit:
            w.view.fit()
        pump()
        w.view.render()
        vp = os.path.join(TMP, "vp.png")
        w.view.screenshot(vp)
        img = w.grab().toImage()
        p = QPainter(img)
        p.drawImage(w.view.mapTo(w, w.view.rect().topLeft()), QImage(vp))
        p.end()
    else:
        img = w.grab().toImage()
    img.save(os.path.join(OUT, name + ".png"))
    print("saved", name, flush=True)


def grab(widget, name):
    widget.show()
    pump()
    widget.grab().save(os.path.join(OUT, name + ".png"))
    print("saved", name, flush=True)


def reset():
    """New empty document in the same window."""
    from daedalus.core.document import Document
    w._swap_doc(Document())
    w.stack.setCurrentIndex(0)
    pump()
    return w.doc


# ------------------------------------------------------------------ toolbelt tabs
d = w.doc
d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 80, "h": 50}]})
d.add("extrude", {"sketch": "Sketch1", "depth": 20})
d.add("sketch", {"plane": "top", "entities": [{"type": "rect", "x": -10, "y": 0, "w": 40, "h": 30}]})
d.add("extrude", {"sketch": "Sketch2", "depth": 8, "operation": "cut"})
d.add("hole", {"face": "top", "positions": [[30, 15], [30, -15]], "fastener": "M6", "fit": "tap", "depth": 15})
d.add("fillet", {"radius": 4, "edges": "vertical&linear"})
pump()
tb = w.toolbelt
for i in range(tb.count()):
    tb.setCurrentIndex(i)
    pump(4)
    tb.grab().save(os.path.join(OUT, f"00_toolbelt_{i + 1}_{tb.tabText(i).lower()}.png"))
tb.setCurrentIndex(0)

# ------------------------------------------------------------------ printability checks
for k in ("draft", "thickness", "overhang"):
    w.check(k)
    shot(f"20_check_{k}")
w.clear_check()

# ------------------------------------------------------------------ measure
asm = w.asm
asm.set_mode("measure")
pump()
r = w.view.ren


def disp(p):
    r.SetWorldPoint(*p, 1.0)
    r.WorldToDisplay()
    x, y, _ = r.GetDisplayPoint()
    return x, y


w.view.set_view("Isometric")
w.view.fit()
pump()
for pt in ([-40, -25, 20], [40, 25, 20]):
    asm._measure_click(*disp(pt))
pump()
w.statusBar().showMessage(getattr(asm, "last_measure", None) and
                          f"Distance {asm.last_measure['distance']:.3f} mm · ΔX {asm.last_measure['dx']:.3f} · "
                          f"ΔY {asm.last_measure['dy']:.3f} · ΔZ {asm.last_measure['dz']:.3f}" or "")
shot("21_measure", fit=False, view=None)
asm.set_mode(None)

# ------------------------------------------------------------------ CAM
w.show_cam()
for t in ("facing", "pocket", "profile", "drill"):
    w.cam_panel.add_op(t)
pump()
w.cam_panel.generate()
shot("30_cam_toolpaths")

# ------------------------------------------------------------------ export options, snaps, library, design study
from daedalus.ui.design_study_ui import DesignStudyDialog  # noqa: E402
from daedalus.ui.export_ui import ExportOptionsDialog  # noqa: E402
from daedalus.ui.production_ui import BomDialog, LibraryDialog  # noqa: E402
from daedalus.ui.snap_settings import SnapSettingsDialog  # noqa: E402

grab(ExportOptionsDialog(d, w), "40_export_options")
grab(SnapSettingsDialog(w), "41_snap_settings")
ds = DesignStudyDialog(d, w)
ds.feat.setCurrentText("Extrude1")
pump(2)
ds.param.setCurrentText("depth")
ds.lo.setValue(10)
ds.hi.setValue(40)
ds.n.setValue(7)
ds.run()
pump()
grab(ds, "42_design_study")

# ------------------------------------------------------------------ drawings
w.new_drawing()
pump()
shot("50_drawing_sheet")
from daedalus.ui.drawing_ui import OptionsDialog, TitleDialog  # noqa: E402

pg = w.drawing_page
grab(OptionsDialog(pg.current_drawing(), pg.current_sheet(), w), "51_drawing_options")
grab(TitleDialog(pg.current_sheet(), w), "52_drawing_title_block")
w.stack.setCurrentIndex(0)

# ------------------------------------------------------------------ sheet metal
d = reset()
d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 120, "h": 80},
                              {"type": "circle", "x": 0, "y": 0, "r": 10, "mode": "subtract"},
                              {"type": "rect", "x": 35, "y": 20, "w": 20, "h": 12, "mode": "subtract"}]})
d.add("sheet_metal", {"sketch": "Sketch1", "thickness": 2, "bend_radius": 2,
                      "flanges": [{"edge": "+X", "length": 30}, {"edge": "-X", "length": 30},
                                  {"edge": "+Y", "length": 20, "hem": "closed"}]})
w.history.refresh(select="SheetMetal1")
w.inspector.show_feature("SheetMetal1")
shot("10_sheet_metal_folded")
w.toggle_flat()
shot("11_sheet_metal_flat", view="Top")
flat_dxf = os.path.join(TMP, "flat.dxf")
d.export_flat_dxf("SheetMetal1", flat_dxf)
w.toggle_flat()

# 2D DXF editing
w.open_2d(flat_dxf)
pump()
shot("53_dxf_2d_edit")
w.stack.setCurrentIndex(0)

# ------------------------------------------------------------------ library + BOM + inventory
d = reset()
d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 160, "h": 100}]})
d.add("extrude", {"sketch": "Sketch1", "depth": 12})
d.add("hole", {"face": "top", "positions": [[-60, -35], [60, -35], [60, 35], [-60, 35]], "fastener": "M8",
               "fit": "normal"})
for x, y in ((-60, -35), (60, -35), (60, 35), (-60, 35)):
    d.insert_part("socket head cap screw", "M8", 25, target={"obj": "Object1", "face": {"axis_near": [x, y, 12]}},
                  washer=True)
d.insert_part("ball bearing", list(__import__("daedalus.core.standards", fromlist=["BEARINGS"]).BEARINGS)[3], 0)
pump()
shot("60_library_parts_assembly")

# seed a realistic inventory
from daedalus.inventory import cad as IC  # noqa: E402
from daedalus.inventory import model as M  # noqa: E402

page = w.inventory_page
inv = page.inv
inv.userName = "Ankur"
inv.vendors += [
    {"id": M.uid(), "name": "McMaster-Carr", "category": "Fastener", "contactName": "Sales", "phone": "",
     "email": "", "website": "https://www.mcmaster.com", "leadTimeMinDays": 3, "leadTimeMaxDays": 5, "notes": ""},
    {"id": M.uid(), "name": "Fastenal", "category": "Fastener", "contactName": "Burnaby branch", "phone": "",
     "email": "", "website": "", "leadTimeMinDays": 1, "leadTimeMaxDays": 3, "notes": ""},
    {"id": M.uid(), "name": "SKF Canada", "category": "Other", "contactName": "", "phone": "", "email": "",
     "website": "", "leadTimeMinDays": 7, "leadTimeMaxDays": 14, "notes": ""}]
locs = []
for rack, shelf, size, pos in (("A", 1, "small", "03"), ("A", 1, "small", "04"), ("A", 2, "large", "01"),
                               ("B", 1, "large", "02")):
    loc = {"id": M.uid(), "rack": rack, "shelf": shelf, "binSize": size, "binPosition": pos,
           "name": f"{rack}{shelf}-{pos}", "sections": False}
    inv.locations.append(loc)
    locs.append(loc["id"])
r1 = IC.send_bom(inv, d, "Mounting plate", 12)
for p in inv.parts:                                   # some stock on the plate's parts
    p["currentStock"] = {"fastener": 30, "purchased": 8}.get(p["type"], 4)
    p["reorderPoint"] = {"fastener": 25, "purchased": 4}.get(p["type"], 2)
    p["locationId"] = locs[0] if p["type"] == "fastener" else locs[2]
lift = inv.find_or_create_project("Lift cart LC-200", totalQtyNeeded=6)
press = inv.find_or_create_project("Frame press FP-1", totalQtyNeeded=2)


def fast(size, length, stock, rp, cost, loc, vendor, head="Socket Head", cat="Screw"):
    p = M.new_part(type="fastener", category=cat, headStyle=head, threadStandard="Metric Coarse", size=size,
                   length=length, name=M.fastener_name(size, "Metric Coarse", head, "", cat, length),
                   currentStock=stock, reorderPoint=rp, reorderQty=rp * 3, unitCost=cost, unitCostCurrency="CAD",
                   locationId=loc, unit="ea")
    p["suppliers"][0] = {"name": vendor, "price": cost, "currency": "CAD", "partNumber": "", "website": ""}
    inv.parts.append(p)
    return p


s1 = fast("M6", "16", 140, 50, 0.14, locs[0], "Fastenal")
s2 = fast("M10", "40", 18, 25, 0.62, locs[1], "McMaster-Carr", head="Hex", cat="Bolt")
s3 = fast("M10", "", 60, 40, 0.09, locs[1], "Fastenal", head="Nylon Insert", cat="Nut")
caster = M.new_part(type="purchased", name="Swivel caster 100 mm w/ brake", partNumber="CST-100B", currentStock=10,
                    reorderPoint=12, reorderQty=24, unitCost=21.5, unitCostCurrency="USD", locationId=locs[2])
caster["suppliers"][0] = {"name": "McMaster-Carr", "price": 21.5, "currency": "USD", "partNumber": "2834T45",
                          "website": "https://www.mcmaster.com"}
tube = M.new_part(type="manufactured", name="Upright 50x50x3 SHS x 900", partNumber="LC-200-03", currentStock=8,
                  reorderPoint=4, unitCost=38.0, locationId=locs[3])
rail = M.new_part(type="manufactured", name="Base rail 50x50x3 SHS x 1200", partNumber="LC-200-04", currentStock=5,
                  reorderPoint=4, unitCost=46.0, locationId=locs[3])
frame = M.new_part(type="manufactured", name="LC-200 base frame (welded)", partNumber="LC-200-01", currentStock=2,
                   reorderPoint=2, isWeldment=True, components=[{"partId": tube["id"], "qty": 2},
                                                                {"partId": rail["id"], "qty": 2}])
inv.parts += [caster, tube, rail, frame]
for p, q in ((frame, 1), (caster, 4), (s2, 8), (s3, 8), (s1, 16)):
    inv.set_allocation(p, lift, q)
for p, q in ((s2, 12), (s3, 12), (tube, 4)):
    inv.set_allocation(p, press, q)
for pid, mode, q, note in ((s1["id"], "usage", 32, "LC-200 batch 3"), (caster["id"], "receiving", 8, "PO 4471"),
                           (s2["id"], "usage", 16, "FP-1 build"), (tube["id"], "receiving", 4, "Cut on saw")):
    inv.record(pid, mode, q, note)
today = __import__("datetime").date.today().isoformat()
inv.place_order(s2["id"], 100, today, M.add_days(today, 4))
inv.place_order(caster["id"], 24, today, M.add_days(today, 10))
inv.productionOrders += [
    {"id": M.uid(), "projectId": lift, "qty": 6, "orderDate": today, "dueDate": M.add_days(today, 28),
     "fabStart": M.add_days(today, 2), "fabEnd": M.add_days(today, 14), "finishStart": M.add_days(today, 15),
     "finishEnd": M.add_days(today, 22), "status": "In Fabrication"},
    {"id": M.uid(), "projectId": press, "qty": 2, "orderDate": today, "dueDate": M.add_days(today, 40),
     "fabStart": M.add_days(today, 12), "fabEnd": M.add_days(today, 26), "finishStart": M.add_days(today, 27),
     "finishEnd": M.add_days(today, 33), "status": "Scheduled"}]
inv.clients.append({"id": M.uid(), "name": "Northshore Cabinets"})
inv.clientOrders.append({"id": M.uid(), "clientId": inv.clients[0]["id"], "clientName": "Northshore Cabinets",
                         "projectLines": [{"projectId": lift, "qty": 2}], "orderDate": today,
                         "dueDate": M.add_days(today, 30), "status": "Confirmed", "notes": "Deliver to Burnaby"})
page.commit("Demo inventory loaded")
pump()

b = BomDialog(d, w, on_send=w.send_bom_to_inventory)
b.units.setValue(12)
b.resize(1300, 420)
grab(b, "61_bom_with_stock")
lib = LibraryDialog(d, None, w)
grab(lib, "62_library_dialog_stock")
from daedalus.ui import inventory_dialogs as D  # noqa: E402

grab(D.SendBomDialog(inv, "Mounting plate", w), "63_send_bom_dialog")

w.show_inventory()
for i, (key, label) in enumerate(page.TABS):
    page.goto(key)
    pump()
    page.toast.hide()
    w.grab().save(os.path.join(OUT, f"7{i:01d}_inventory_{key}.png" if i < 10 else f"80_inventory_{key}.png"))
    print("saved inventory", key, flush=True)
for dlg, name in ((D.PartDialog(inv, s2, w), "81_inv_part_fastener"), (D.PartDialog(inv, frame, w), "82_inv_part_weldment"),
                  (D.MoveDialog(s1, "usage", w), "83_inv_use_stock"),
                  (D.BatchDialog(inv, [s2, caster, s3], "order", w), "84_inv_batch_order"),
                  (D.VendorDialog(inv.vendors[0], w), "85_inv_vendor"),
                  (D.LocationDialog(None, w), "86_inv_location"),
                  (D.HistoryDialog(inv, s1["id"], w), "87_inv_history")):
    grab(dlg, name)
    dlg.close()
w.stack.setCurrentIndex(0)
QSettings("Daedalus", "Daedalus").remove("inventory/path")
print("DONE", flush=True)
os._exit(0)
