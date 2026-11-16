"""Icarus: hard kill, history repair, unique tool names, DIMS orders / production / calculators."""
import collections
import json
import os
import threading
import types

from daedalus.inventory import model as M


def test_tool_names_unique():
    from daedalus.ai.dims_tools import DIMS_TOOLS
    from daedalus.ai.tools import TOOLS
    from daedalus.sw.tools import SW_TOOLS
    for tools in (TOOLS, SW_TOOLS, DIMS_TOOLS):
        dup = [k for k, v in collections.Counter(t["name"] for t in tools).items() if v > 1]
        assert not dup, dup
    names = {t["name"] for t in DIMS_TOOLS}
    assert {"dims_order", "dims_production_po", "design_calc", "convert_units"} <= names


def _fake_copilot(responses, on_create=None):
    from daedalus.ai.copilot import Copilot
    cp = Copilot.__new__(Copilot)
    cp.model, cp.max_steps, cp.messages, cp._stop, cp._killed = "m", 10, [], False, False
    cp.tools, cp.system, cp._api_key = [], "", "x"
    it = iter(responses)

    def create(**kw):
        if on_create:
            on_create(cp)
        return next(it)
    cp.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create), close=lambda: None)
    return cp


def _block(kind, **kw):
    b = types.SimpleNamespace(type=kind, **kw)
    b.model_dump = lambda: {"type": kind, **kw}
    return b


def test_repair_answers_dangling_tool_use():
    from daedalus.ai.copilot import Copilot
    cp = Copilot.__new__(Copilot)
    cp.messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}},
                                                     {"type": "tool_use", "id": "t2", "name": "y", "input": {}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "z", "input": {}}]}]
    cp.repair()
    ids = [b["tool_use_id"] for m in cp.messages if m["role"] == "user" for b in m["content"]
           if b.get("type") == "tool_result"]
    assert sorted(ids) == ["t1", "t2", "t3"] and cp.messages[-1]["role"] == "user"


def test_kill_mid_request_runs_no_tools():
    ran = []
    resp = types.SimpleNamespace(stop_reason="tool_use", content=[_block("tool_use", id="t1", name="x", input={})])
    cp = _fake_copilot([resp], on_create=lambda c: setattr(c, "_killed", True))
    cp.call_tool = lambda n, a: ran.append(n) or ("ok", False)
    cp.ask("go", on_text=lambda t: None)
    assert ran == [] and not any(m["role"] == "assistant" for m in cp.messages)


def test_stop_between_steps_keeps_history_valid():
    calls = []
    r1 = types.SimpleNamespace(stop_reason="tool_use", content=[_block("tool_use", id="t1", name="x", input={})])
    cp = _fake_copilot([r1])
    cp.call_tool = lambda n, a: (calls.append(n), cp.stop(), ("ok", False))[2]
    cp.ask("go", on_text=lambda t: None)
    assert calls == ["x"] and cp.messages[-1]["role"] == "user"
    assert cp.messages[-1]["content"][0]["tool_use_id"] == "t1"


def test_panel_kill_frees_the_chat(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from daedalus.ai.dims_tools import DIMS_TOOLS, DimsToolExecutor
    from daedalus.ui import copilot_panel as CP
    gate = threading.Event()

    class Slow:
        messages, _stop = [], False

        def ask(self, *a, **k):
            gate.wait(5)
            return ""

        def stop(self):
            self._stop = True

        def kill(self):
            self._stop = True
            gate.set()

        def repair(self):
            pass
    ex = DimsToolExecutor(lambda: None)
    panel = CP.CopilotPanel(None, executor=ex, tools=DIMS_TOOLS, system="x")
    panel.copilot = Slow()
    panel._start("hello", [])
    assert panel.busy() and panel.kill_btn.isEnabled()
    panel.escape()                                         # Esc: stop after this step
    assert panel.busy() and panel.copilot._stop
    panel.escape()                                         # Esc again: kill
    assert not panel.busy() and panel.send_btn.isEnabled() and not panel.kill_btn.isEnabled()
    for _ in range(50):
        app.processEvents()
        if not panel._orphans:
            break
        gate.wait(0.02)
    assert panel.worker is None


def test_dims_orders_production_and_calcs():
    from daedalus.ai.dims_tools import DimsToolExecutor

    class St:
        def __init__(self):
            self.inv = M.Inventory()

        def save(self):
            pass
    st = St()
    inv = st.inv
    inv.userName = "Ankur"
    pid = inv.find_or_create_project("RLC")
    nut = M.new_part(id="nut", name="Nut", type="fastener", suppliers=[{"name": "Acme", "price": 0.1}, {}, {}])
    inv.parts = [nut]
    inv.set_allocation(nut, pid, 4)
    ex = DimsToolExecutor(lambda: st)
    run = lambda n, **a: json.loads(ex.run(n, a)[0])           # noqa: E731
    o = run("dims_order", action="create", po="PO-9", name="Shop", lines=[{"project": "RLC", "qty": 2}])["order"]
    assert o["status"] == "Draft"
    got = run("dims_order", action="confirm", order="PO-9")
    prod = got["production"][0]
    assert prod["qty"] == 2 and inv.project_qty(pid) == 2
    run("dims_order", action="change", order="Shop", lines=[{"project": "RLC", "qty": 3}])
    assert inv.project_qty(pid) == 3 and inv.clientOrders[0]["rev"] == 1
    s = run("dims_production_po", id=prod["id"], action="fab", state="yes", po="F-1")
    assert s["fab"]["state"] == "yes" and s["pos"]["Fasteners"]["Acme"]["lines"][0]["qty"] == 12
    s = run("dims_production_po", id=prod["id"], action="po", group="Fasteners", vendor="Acme", state="na")
    assert s["done"] == "2/2"
    s = run("dims_production_po", id=prod["id"], action="finish", days=7)
    assert s["finishBy"] == M.add_days(M.today(), 7)
    lst = run("design_calc")
    assert any(c.get("id") == "parallel_key" for c in lst)
    key = run("design_calc", calc="parallel_key", inputs={"d": 30, "T": 200})
    assert key
    conv = run("convert_units", value="1 ft", to="mm")
    assert conv["result"].startswith("304.8")
