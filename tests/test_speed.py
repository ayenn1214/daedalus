"""Speed work: lookups cached while a page is built (same answers), Parts sub-tabs built when first opened, the
app-wide Shift-click filter only on while DIMS is on screen and off during rebuilds."""
import os

from daedalus.inventory import configs as C
from daedalus.inventory import model as M
from daedalus.inventory import mrp as R


def sample():
    inv = M.Inventory()
    pid = inv.find_or_create_project("Cart")
    pid2 = inv.find_or_create_project("Press")
    plate = M.new_part(id="plate", name="PLATE", type="manufactured", currentStock=3)
    weld = M.new_part(id="weld", name="FRAME", type="manufactured", isWeldment=True,
                      components=[{"partId": "plate", "qty": 2}])
    asm = M.new_part(id="asm", name="TOP", type="manufactured", isAssembly=True,
                     components=[{"partId": "weld", "qty": 1}, {"partId": "bolt", "qty": 4}])
    bolt = M.new_part(id="bolt", name="BOLT", type="fastener", currentStock=5, reorderPoint=10)
    beam = M.new_part(id="beam", name="BEAM", type="manufactured")
    inv.parts = [plate, weld, asm, bolt, beam]
    inv.set_allocation(asm, pid, 1)
    inv.set_allocation(bolt, pid, 8)
    inv.set_allocation(bolt, pid2, 2)
    g = C.new_group("Add-ons", C.ANY)
    ch = C.new_choice("Beam", True)
    ch["lines"] = [{"partId": "beam", "qty": 1}]
    g["choices"] = [ch]
    inv.project(pid)["options"] = [g]
    inv.productionOrders = [{"id": "o1", "projectId": pid, "qty": 3, "status": "In Fab",
                             "config": {C.QTY: {"asm": 1}}}]
    return inv, pid, pid2


def answers(inv, pid, pid2):
    return (
        [inv.part(k)["name"] for k in ("plate", "weld", "bolt")], inv.part("nope"),
        [p["id"] for p in inv.parts_for_project(pid)], [p["id"] for p in inv.parts_for_project(pid2)],
        sorted(inv.component_ids()), {p["id"]: inv.total_needed(p) for p in inv.parts},
        {p["id"]: inv.needed_for_project(p, pid) for p in inv.parts}, inv.project_qty(pid),
        R.parents(inv, "bolt"), R.where_used(inv, "plate")["projects"], [p["id"] for p in inv.low_stock()],
        {p["id"]: inv.project_chips(p) for p in inv.parts}, len(inv.duplicate_groups()),
        sorted(C.option_parts(inv.project(pid), inv)), C.option_projects(inv, inv.part("beam")),
    )


def test_cached_lookups_give_the_same_answers():
    inv, pid, pid2 = sample()
    plain = answers(inv, pid, pid2)
    with inv.cached():
        assert inv._memo is not None
        assert answers(inv, pid, pid2) == plain
        assert answers(inv, pid, pid2) == plain                   # second time from the cache
        with inv.cached():                                        # nested: same cache
            assert inv.memo("x", lambda: 1) == 1 and inv.memo("x", lambda: 2) == 1
    assert inv._memo is None and inv.memo("x", lambda: 2) == 2    # outside: always fresh
    inv.parts.append(M.new_part(id="late", name="LATE"))
    with inv.cached():
        inv.part("plate")
        inv.parts.append(M.new_part(id="later", name="LATER"))    # added mid-build: still found
        assert inv.part("later")["name"] == "LATER"
    assert answers(inv, pid, pid2)[0] == plain[0]


def test_parts_subtabs_built_when_opened_and_event_filter(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.inventory.store import InventoryStore
    from daedalus.ui.inventory_ui import InventoryPage
    inv, *_ = sample()
    st = InventoryStore(tmp_path / "inv.json")
    st.inv = inv
    st.save()
    page = InventoryPage(st, lite=True)
    page.show()
    app.processEvents()
    assert page._filtering
    page.goto("parts")
    app.processEvents()
    ev = page.pages["parts"].widget()
    names = [ev.tabs.tabText(i) for i in range(ev.tabs.count())]
    i_used = names.index("Where Used")
    assert getattr(ev.tabs.widget(i_used), "_lazy", False)        # not built yet
    ev.tabs.setCurrentIndex(i_used)
    app.processEvents()
    assert not getattr(ev.tabs.widget(i_used), "_lazy", False) and ev.tabs.currentIndex() == i_used
    assert page.mrp_state["eng_tab"] == i_used
    page.refresh_current()                                        # rebuilt on the tab you were on
    app.processEvents()
    ev = page.pages["parts"].widget()
    assert ev.tabs.currentIndex() == i_used and not getattr(ev.tabs.widget(i_used), "_lazy", False)
    assert page._filtering                                        # back on after the rebuild
    page.hide()
    assert not page._filtering
    page.show()
    assert page._filtering
    page.close()


def test_parts_inside_a_project_part_are_on_the_project():
    """Weldment pieces (any depth) of a part on a project belong to that project — not 'Unassigned' — while what
    the project needs still comes from the top level only."""
    inv, pid, pid2 = sample()
    sub = M.new_part(id="sub", name="SUB WELD", type="manufactured", isWeldment=True,
                     components=[{"partId": "gus", "qty": 2}])
    gus = M.new_part(id="gus", name="GUSSET", type="manufactured")
    loose = M.new_part(id="loose", name="LOOSE", type="purchased")
    inv.parts += [sub, gus, loose]
    inv.part("weld")["components"].append({"partId": "sub", "qty": 1})
    for k in ("weld", "plate", "sub", "gus", "bolt"):
        assert inv.in_project(inv.part(k), pid), k                 # asm (on Cart) → weld → plate / sub → gus
    assert not inv.in_project(inv.part("gus"), pid2) and inv.in_project(inv.part("bolt"), pid2)
    assert [p["id"] for p in inv.parts if inv.unassigned(p)] == ["beam", "loose"]   # beam: an option part
    assert inv.project_chips(inv.part("gus")) == ["Cart"] and inv.project_ids(inv.part("gus"), via_bom=False) == []
    assert inv.bom_projects()["gus"][pid] == ["asm"]
    need = inv.needed_for_project(inv.part("plate"), pid)
    assert [p["id"] for p in inv.parts_for_project(pid)] == ["asm", "bolt"]   # demand side unchanged
    with inv.cached():
        assert inv.needed_for_project(inv.part("plate"), pid) == need
        assert [p["id"] for p in inv.parts if inv.unassigned(p)] == ["beam", "loose"]   # beam: an option part


def test_shared_weldment_is_on_every_project_that_uses_it():
    """One weldment (and its sub-weldments) used by two projects: the same part is on both."""
    inv, pid, pid2 = sample()
    press = M.new_part(id="press", name="PRESS FRAME", type="manufactured", isWeldment=True,
                       components=[{"partId": "weld", "qty": 1}])          # the Cart's FRAME weldment again
    inv.parts.append(press)
    inv.set_allocation(press, pid2, 1)
    for k in ("weld", "plate"):
        assert set(inv.project_ids(inv.part(k))) == {pid, pid2}, k
        assert inv.project_chips(inv.part(k)) == ["Cart", "Press"]
    assert inv.bom_projects()["plate"] == {pid: ["asm"], pid2: ["press"]}
    assert len([p for p in inv.parts if p["name"] == "FRAME"]) == 1          # one part, not a copy per project
