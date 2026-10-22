import json
import math

import numpy as np
import pytest
from build123d import Box, Cylinder, Pos, Rot, Sphere

from daedalus.ai.tools import ToolExecutor
from daedalus.core import materials as M
from daedalus.core.document import Document
from daedalus.core.features import FeatureError
from daedalus.sim import cfd, fatigue, fea

STEEL = M.get("Steel AISI 1018 (CD)")
BAR = Box(200, 20, 10)           # faces 0 -x, 1 +x, 2 -y, 3 +y, 4 -z, 5 +z
E = STEEL.E * 1000
I = 20 * 10 ** 3 / 12


def cant(load=100.0, **kw):
    return {"name": "S", "fixtures": [{"type": "fixed", "faces": [0]}],
            "loads": [{"type": "force", "faces": [1], "value": load, "direction": [0, 0, -1]}], "mesh_size": 4, **kw}


def test_cantilever_deflection_stress_reaction():
    r = fea.static(BAR, STEEL, cant())
    s = r.summary
    assert s["max_displacement_mm"] == pytest.approx(100 * 200 ** 3 / (3 * E * I), rel=0.02)
    assert s["max_von_mises_MPa"] == pytest.approx(100 * 200 * 5 / I, rel=0.06)
    assert s["reactions"][0]["Fz"] == pytest.approx(100, rel=1e-6)
    assert s["min_factor_of_safety"] == pytest.approx(STEEL.yield_ / s["max_von_mises_MPa"], rel=1e-3)


def test_pressure_torque_gravity_loads_balance():
    for ld, expect in (({"type": "pressure", "faces": [5], "value": 0.1}, (0, 0, 0.1 * 4000)),
                       ({"type": "gravity", "value": 9.81, "direction": [0, 0, -1]},
                        (0, 0, BAR.volume * STEEL.density * 1e-9 * 9810))):
        r = fea.static(BAR, STEEL, {"name": "S", "fixtures": [{"type": "fixed", "faces": [0]}], "loads": [ld],
                                     "mesh_size": 6})
        R = r.summary["reactions"][0]
        assert R["Fz"] == pytest.approx(expect[2], rel=1e-3)
    shaft = Cylinder(10, 100)            # torque on the top face, bottom fixed: τ = T r / J
    r = fea.static(shaft, STEEL, {"name": "T", "fixtures": [{"type": "fixed", "faces": [2]}],
                                  "loads": [{"type": "torque", "faces": [1], "value": 50.0}], "mesh_size": 3})
    tau = 50e3 * 10 / (math.pi * 10 ** 4 / 2)
    assert r.summary["max_von_mises_MPa"] == pytest.approx(math.sqrt(3) * tau, rel=0.06)


def test_modal_and_buckling_match_beam_theory():
    rho = STEEL.density * 1e-9
    fr = fea.frequency(BAR, STEEL, {"name": "F", "fixtures": [{"type": "fixed", "faces": [0]}], "modes": 2,
                                    "mesh_size": 4})
    f1 = (1.875 ** 2 / (2 * math.pi)) * math.sqrt(E * I / (rho * 200 * 200 ** 4))
    assert fr.summary["frequencies_Hz"][0] == pytest.approx(f1, rel=0.02)
    bk = fea.buckling(BAR, STEEL, {"name": "B", "fixtures": [{"type": "fixed", "faces": [0]}],
                                   "loads": [{"type": "force", "faces": [1], "value": 1000, "direction": [-1, 0, 0]}],
                                   "modes": 1, "mesh_size": 4})
    assert bk.summary["load_factors"][0] == pytest.approx(math.pi ** 2 * E * I / (4 * 200 ** 2) / 1000, rel=0.02)


def test_thermal_conduction_and_thermal_stress():
    th = fea.thermal(BAR, STEEL, {"name": "T", "thermal": [{"type": "temperature", "faces": [0], "value": 100},
                                                          {"type": "temperature", "faces": [1], "value": 20}],
                                  "mesh_size": 5})
    assert th.summary["max_heat_flux_W_m2"] == pytest.approx(STEEL.k * 80 / 0.2, rel=1e-3)
    sm = fea.mesh_solid(BAR, 5)
    rollers = [{"type": "roller", "faces": [f]} for f in (0, 1, 2, 4)]
    ts = fea.static(BAR, STEEL, {"name": "TS", "fixtures": rollers, "loads": [], "mesh_size": 5}, sm=sm,
                    dT_nodal=np.full(sm.n_nodes, 100.0))
    assert ts.summary["max_von_mises_MPa"] == pytest.approx(E * STEEL.alpha * 1e-6 * 100, rel=1e-3)
    tr = fea.thermal(BAR, STEEL, {"name": "T", "thermal": [{"type": "temperature", "faces": [0], "value": 200}],
                                  "transient": {"duration": 100, "steps": 10, "initial": 20}, "mesh_size": 8})
    avg = tr.curves["average temperature"][1]
    assert avg[0] < 40 and avg[-1] > avg[0] + 20 and all(b >= a - 1e-9 for a, b in zip(avg, avg[1:]))


def test_harmonic_peak_and_fatigue():
    h = fea.harmonic(BAR, STEEL, {**cant(10), "modes": 3, "fmin": 150, "fmax": 260, "points": 400,
                                  "damping": 0.02, "mesh_size": 5})
    st = fea.static(BAR, STEEL, cant(10, mesh_size=5))
    q = h.summary["peak_displacement_mm"] / st.summary["max_displacement_mm"]
    assert 20 < q < 27                                  # ≈ 1/(2ζ) = 25
    low = fatigue.run(fea.static(BAR, STEEL, cant(100)), STEEL, {"name": "Fa", "cycles": 1e6})
    assert low.summary["min_life_cycles"] == "infinite" and low.summary["passes"]
    high = fatigue.run(fea.static(BAR, STEEL, cant(400)), STEEL, {"name": "Fa", "cycles": 1e6})
    assert isinstance(high.summary["min_life_cycles"], float) and not high.summary["passes"]
    zero = fatigue.run(fea.static(BAR, STEEL, cant(300)), STEEL, {"name": "Fa", "loading": "zero", "cycles": 1e6})
    rev = fatigue.run(fea.static(BAR, STEEL, cant(300)), STEEL, {"name": "Fa", "loading": "reversed", "cycles": 1e6})
    assert zero.summary["min_fatigue_FoS"] > rev.summary["min_fatigue_FoS"]


def test_no_fixture_and_no_load_errors():
    with pytest.raises(fea.SimError):
        fea.static(BAR, STEEL, {"name": "S", "fixtures": [], "loads": [{"type": "pressure", "faces": [5], "value": 1}],
                                "mesh_size": 8})
    with pytest.raises(fea.SimError):
        fea.static(BAR, STEEL, {"name": "S", "fixtures": [{"type": "fixed", "faces": [0]}], "loads": [], "mesh_size": 8})


def test_pipe_flow_pressure_drop():
    pipe = Rot(0, 90, 0) * (Cylinder(10, 80) - Cylinder(7, 80))
    U = 20 * 1.002e-6 / 0.014
    r = cfd.run({"Pipe": pipe}, {"mode": "internal", "fluid": "water", "velocity": U, "resolution": 14,
                                 "flow_throughs": 3})
    s = r.summary
    D_eff = math.sqrt(4 * s["flow_rate_m3_s"] / U / math.pi)       # the voxel bore
    dp = 32 * 1.002e-3 * 0.08 * U / D_eff ** 2
    assert s["pressure_drop_Pa"] == pytest.approx(dp, rel=0.25)


def test_document_studies_and_claude_tools(tmp_path):
    d = Document()
    d.add("sketch", {"entities": [{"type": "rect", "x": 0, "y": 0, "w": 200, "h": 20}]})
    d.add("extrude", {"sketch": "Sketch1", "depth": 10})
    ex = ToolExecutor(d)
    out, err = ex.run("set_material", {"material": "Aluminum 6061-T6"})
    assert not err
    out, err = ex.run("add_study", {"type": "static", "mesh_size": 5})
    assert not err, out
    out, err = ex.run("add_fixture", {"study": "Static1", "type": "fixed", "faces": [{"face": "left"}]})
    assert not err, out
    out, err = ex.run("add_load", {"study": "Static1", "type": "force", "faces": [{"face": "right"}], "value": 50,
                                   "direction": "-Z"})
    assert not err, out
    out, err = ex.run("run_study", {"name": "Static1"})
    assert not err, out
    res = json.loads(out)
    E_al = 68.9e3
    assert res["max_displacement_mm"] == pytest.approx(50 * 200 ** 3 / (3 * E_al * I), rel=0.03)
    assert d.study_result_fresh("Static1")
    out, err = ex.run("add_study", {"type": "fatigue", "options": {"cycles": 1e7}})
    assert not err and json.loads(out)["options"]["source"] == "Static1"
    out, err = ex.run("run_study", {"name": "Fatigue1"})
    assert not err, out
    assert "studies" in ex.model_state()
    out, err = ex.run("add_load", {"study": "Static1", "type": "force", "value": 5})
    assert err                                             # needs faces
    d.set_material("Steel ASTM A36")
    assert not d.study_result_fresh("Static1")             # material change makes it stale
    d.save(tmp_path / "s.daedalus")
    e = Document.open(tmp_path / "s.daedalus")
    assert [s["name"] for s in e.studies] == ["Static1", "Fatigue1"]
    assert e.study("Static1")["fixtures"][0]["refs"]
    with pytest.raises(FeatureError):
        e.add_study("magic")


def test_material_library_and_custom():
    d = Document()
    assert len(M.LIBRARY) > 60 and "Aluminum" in M.categories()
    d.set_material("Steel 1018")                            # old name maps to the new one
    assert d.material == "Steel AISI 1018 (CD)"
    m = d.add_custom_material({"name": "My nylon", "based_on": "Nylon 6/6 (dry)", "E": 2.5})
    assert m.custom and m.E == 2.5 and m.density == 1.14
    d.add_object("Part2")
    d.set_material("My nylon", obj="Part2")
    assert d.material_of("Part2").name == "My nylon" and d.material_of("Object1").name == "Steel AISI 1018 (CD)"
    with pytest.raises(FeatureError):
        d.set_material("Unobtainium")
