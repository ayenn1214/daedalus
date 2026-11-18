"""Project Profile: weight / budget roll-ups, vendors, options, assemblies, production, files, 3D preview page."""
import os
import struct

from daedalus.inventory import configs as C
from daedalus.inventory import model as M
from daedalus.inventory import project_profile as PP


def sample():
    inv = M.Inventory()
    inv.vendors = [{"id": "v1", "name": "Fastenal", "leadTimeMinDays": 3, "leadTimeMaxDays": 7},
                   {"id": "v2", "name": "Protech", "leadTimeMinDays": 10, "leadTimeMaxDays": 21}]
    pid = inv.find_or_create_project("RFP 1308")
    plate = M.new_part(id="plate", name="PLATE", type="manufactured", weightLb=10, unitCost=20,
                       finish={"process": "Zinc", "vendor": "AuraCNC"})
    tube = M.new_part(id="tube", name="TUBE", type="manufactured", weightLb=5,
                      suppliers=[{"name": "Protech", "price": 8, "currency": "CAD"}])
    weld = M.new_part(id="weld", name="BASE WELDMENT", partNumber="C20-0040", type="manufactured", isWeldment=True,
                      components=[{"partId": "plate", "qty": 2}, {"partId": "tube", "qty": 4}], currentStock=0)
    bolt = M.new_part(id="bolt", name="1/2 HHCS", type="fastener", weightLb=0.1, unitCost=0.5,
                      suppliers=[{"name": "Fastenal", "price": 0.5, "currency": "CAD"}])
    caster = M.new_part(id="caster", name="CASTER", type="purchased")         # no weight, no price
    beam = M.new_part(id="beam", name="BEAM", type="manufactured", weightLb=40, unitCost=100)
    inv.parts = [plate, tube, weld, bolt, caster, beam]
    inv.set_allocation(weld, pid, 1)
    inv.set_allocation(bolt, pid, 10)
    inv.set_allocation(caster, pid, 4)
    pr = inv.project(pid)
    g = C.new_group("Add-ons", C.ANY)
    ch = C.new_choice("4th beam")
    ch["lines"] = [{"partId": "beam", "qty": 1}]
    g["choices"] = [ch]
    pr["options"] = [g]
    inv.partOrders = [{"id": "o1", "partId": "tube", "projectId": pid, "status": "Ordered", "vendorName": "Protech"}]
    inv.productionOrders = [{"id": "p1", "projectId": pid, "qty": 2, "status": "In Fab", "dueDate": "2026-12-01"},
                            {"id": "p2", "projectId": pid, "qty": 1, "status": "Complete",
                             "completedDate": M.today()}]
    return inv, pid


def test_rollups_and_coverage():
    inv, pid = sample()
    d = PP.profile(inv, pid)
    # weldment weight comes from its parts: 2×10 + 4×5 = 40; + bolts 10×0.1; caster unknown
    assert abs(d["weight"] - 41) < 1e-6
    assert d["lines"] == 3 and d["weightKnown"] == 2
    # cost: weldment 2×20 + 4×8 = 72, bolts 5; caster unpriced
    assert abs(d["cost"] - 77) < 1e-6 and d["costKnown"] == 2
    t = d["types"]
    assert t["MFG parts"]["lines"] == 3 and abs(t["MFG parts"]["weight"] - 40) < 1e-6
    assert t["Purchased parts"]["lines"] == 1 and t["Purchased parts"]["priced"] == 0
    assert t["Fasteners"]["qty"] == 10


def test_vendors_lead_options_assemblies_production():
    inv, pid = sample()
    d = PP.profile(inv, pid)
    names = {v["name"]: v for v in d["vendors"]}
    assert names["Protech"]["openPOs"] == 1 and "MFG parts" in names["Protech"]["kinds"]
    assert "Finishing" in names["AuraCNC"]["kinds"] and "Fastenal" in names
    assert d["lead"]["days"] == 21 and d["lead"]["vendor"] == "Protech"
    o = d["options"][0]
    assert o["name"] == "Add-ons" and o["choices"][0]["weight"] == 40 and o["choices"][0]["cost"] == 100
    a = d["assemblies"]
    assert len(a) == 1 and a[0]["part"]["id"] == "weld" and a[0]["kids"] == 2 and a[0]["short"] == 2
    assert a[0]["weight"] == (40, True) and a[0]["file"]["status"] == "no-file"
    assert d["production"]["inProduction"] == 2 and d["production"]["builtThisYear"] == 1
    assert d["files"]["mfg"] == 3 and d["files"]["linked"] == 0


def test_fmt_size():
    assert PP.fmt_size(None) == ""
    s = PP.fmt_size({"x": 254, "y": 2540, "z": 25.4})
    assert s.startswith("2,540 × 254 × 25 mm") and "(100.0 × 10.0 × 1.0 in)" in s


def _stl(path, tris):
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for p in t:
                f.write(struct.pack("<3f", *p))
            f.write(b"\0\0")


def _box(dx, dy, dz):
    v = [(x, y, z) for x in (0, dx) for y in (0, dy) for z in (0, dz)]
    q = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    out = []
    for a, b, c, d in q:
        out += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return out


def test_profile_page_and_model(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    from PySide6.QtWidgets import QApplication, QLabel, QTableWidget
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import project_profile_ui as PU
    from daedalus.ui.inventory_ui import InventoryPage
    inv, pid = sample()
    stl = tmp_path / "rfp.stl"
    _stl(stl, _box(2400, 1300, 2100))
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.set_project_model(pid, str(stl))
    assert page.inv.project(pid)["modelFile"]
    page.show_project_profile(pid)
    for _ in range(5):
        app.processEvents()
    w = page.pages["projects"].widget()
    assert w._profile["project"]["id"] == pid
    text = " ".join(l.text() for l in w.findChildren(QLabel))
    assert "RFP 1308" in text and "Customizable options" not in text  # captions are upper-case
    assert "CUSTOMIZABLE OPTIONS" in text and "41 lb" in text and "Protech" in text
    tables = w.findChildren(QTableWidget)
    assert any(t.horizontalHeaderItem(0).text() == "Assembly" for t in tables)
    # the model loads on a thread
    import time
    card = w._model
    for _ in range(100):
        app.processEvents()
        if len(card.view.segs):
            break
        time.sleep(0.05)
    assert len(card.view.segs) == 12                                          # a box's 12 feature edges
    assert page.inv.project(pid)["modelSize"] == {"x": 2400.0, "y": 1300.0, "z": 2100.0}
    assert card.size.text().startswith("2,400 × 2,100 × 1,300 mm")
    for n in PU.VIEWS:
        card.view.set_view(n)
        assert card.view.projected().shape == (12, 2, 2)
    card.view.grab()                                                          # paints
    # back to the list
    page.show_project_profile(None)
    app.processEvents()
    app.processEvents()
    assert not hasattr(page.pages["projects"].widget(), "_profile")


def test_production_preset():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui.inventory_dialogs import OrderDialog
    inv, pid = sample()
    d = OrderDialog(inv, {"projectId": pid})
    assert d.windowTitle() == "Add Production Order" and d.values()["projectId"] == pid
    assert d.values()["orderDate"] == M.today()


def test_model_lines_visible_on_dark_viewport(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui import project_profile_ui as PU
    from daedalus.ui import theme
    monkeypatch.setattr(theme, "VIEW_BG_TOP", (0.02, 0.02, 0.02))
    monkeypatch.setattr(theme, "VIEW_BG_BOTTOM", (0.0, 0.0, 0.0))
    assert PU.ModelView._dark() and PU.ModelView().line_color().lightnessF() > 0.8
    monkeypatch.setattr(theme, "VIEW_BG_TOP", (0.98, 0.98, 0.99))
    monkeypatch.setattr(theme, "VIEW_BG_BOTTOM", (0.85, 0.88, 0.91))
    assert not PU.ModelView._dark() and PU.ModelView().line_color().lightnessF() < 0.3


def test_part_profile_shows_model(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    import time
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui import project_profile_ui as PU
    from daedalus.ui.inventory_ui import InventoryPage
    inv, pid = sample()
    stl = tmp_path / "plate.stl"
    _stl(stl, _box(300, 200, 10))
    inv.part("plate")["cadFile"] = str(stl)
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.parts_sel = "plate"                           # the open part survives page rebuilds
    page.goto("parts")
    app.processEvents()
    page._browser.show_part("plate")
    for _ in range(100):                               # the page may rebuild the profile once; follow it
        app.processEvents()
        br = page._browser
        panel = br.model_panel
        if panel is not None and len(panel.view.segs):
            break
        time.sleep(0.05)
    assert len(panel.view.segs) == 12 and panel.size.text().startswith("300 × 200 × 10 mm")
    # the panel follows the 3D model field
    br.editor.cad.setText("")
    for _ in range(30):
        app.processEvents()
        time.sleep(0.03)
    assert len(panel.view.segs) == 0 and "No 3D model" in panel.view.msg
    # SolidWorks file: a STEP / STL with the same name next to it is shown instead
    sw = tmp_path / "plate.SLDPRT"
    sw.write_bytes(b"x")
    assert PU.step_for(str(sw)) == str(stl)
    br.show_part("bolt")                                                  # fastener without a model: no panel
    assert br.model_panel is None


def test_model_orientation():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import numpy as np
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from daedalus.ui import project_profile_ui as PU
    saved = []
    panel = PU.ModelPanel("", on_orient=saved.append)
    seg = np.array([[[0, 0, 0], [0, 100, 0]]], float)                 # a line along Y (drawn Y-up)
    panel.view.set_segments(seg)
    panel.view.set_view("Front")                                       # front: x across, z up
    h0 = np.ptp(panel.view.projected()[..., 1])
    panel.turn_model("X", 90)                                          # Y up → Z up
    assert saved[-1] is not None and len(saved[-1]) == 9
    h1 = np.ptp(panel.view.projected()[..., 1])
    assert h0 < 1e-9 and abs(h1 - 100) < 1e-6                          # now stands up in the front view
    panel.set_model_orient(None)
    assert saved[-1] is None and np.allclose(panel.view.orient, np.eye(3))
    assert PU.orient_value(PU.turn("Z", 90)) == [0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    # a saved orientation comes back
    p2 = PU.ModelPanel("", orient=PU.orient_value(PU.turn("X", 90)))
    assert np.allclose(p2.view.orient, PU.turn("X", 90))
