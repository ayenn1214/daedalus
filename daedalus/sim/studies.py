"""Simulation studies stored in the document: definition, face references, running. Qt-free.

A study is a plain dict (saved in the .daedalus file):
  {"name", "type", "object", "mesh_size", "fixtures": [...], "loads": [...], "thermal": [...], "options": {...}}
Fixtures/loads/thermal conditions name faces by geometric reference (the same kind mates use), so they
follow the part through design changes. Directions are given in world coordinates.
"""
from __future__ import annotations

import copy
import hashlib
import json

import numpy as np

from ..core import assembly as asm
from . import fea
from .fea import FIXTURES, LOADS, THERMAL_BCS, SimError
from .mesh import MeshError as MeshError_

TYPES = {
    "static": "Static stress", "frequency": "Frequency (modal)", "buckling": "Buckling",
    "thermal": "Thermal", "fatigue": "Fatigue", "harmonic": "Harmonic response", "flow": "Flow (CFD)",
    "assembly": "Assembly static (bonded / bolted)", "drop": "Drop test", "topology": "Topology optimisation",
}
OPTIONS = {   # type -> {option: (default, help)}
    "static": {"temperature_from": ("", "Thermal study whose temperatures load this one (thermal stress)"),
               "uniform_dT": (0.0, "Uniform temperature change °C (thermal stress)")},
    "frequency": {"modes": (6, "Number of modes")},
    "buckling": {"modes": (4, "Number of buckling modes")},
    "thermal": {"transient": (False, "Transient (time-dependent) instead of steady"),
                "duration": (60.0, "Transient: duration s"), "steps": (60, "Transient: time steps"),
                "initial": (20.0, "Transient: initial temperature °C")},
    "fatigue": {"source": ("", "Static study giving the stress"), "loading": ("reversed", "reversed | zero | ratio"),
                "R": (-1.0, "Stress ratio σmin/σmax (loading = ratio)"), "cycles": (1e6, "Target life, cycles"),
                "mean": ("goodman", "goodman | gerber | soderberg | asme | none"),
                "surface": ("machined", "polished | ground | machined | cold-drawn | hot-rolled | as-forged | "
                            "as-cast | 3D printed"),
                "reliability": (0.9, "0.5 … 0.9999"), "size_mm": (0.0, "Section size mm (0 = ignore)"),
                "load_type": ("bending", "bending | axial | torsion"), "scale": (1.0, "Load scale factor")},
    "harmonic": {"modes": (12, "Modes in the superposition"), "fmin": (1.0, "Start frequency Hz"),
                 "fmax": (0.0, "End frequency Hz (0 = auto)"), "damping": (0.02, "Damping ratio"),
                 "points": (300, "Frequency points")},
    "flow": {"mode": ("external", "external | internal"), "fluid": ("air", "air | water | oil (SAE 30) | glycerin"),
             "velocity": (5.0, "Free-stream / inlet velocity m/s"), "direction": ("+X", "+X -X +Y -Y +Z -Z"),
             "resolution": (20, "Cells across the body / passage"), "flow_throughs": (2.0, "Run length"),
             "max_cells": (3e6, "Cell limit"), "objects": ("", "Objects in the flow (comma list, blank = all)")},
    "assembly": {"objects": ("", "Objects in the study (comma list, blank = all visible)"),
                 "contact": ("bonded", "bonded (touching faces glued) | free (joined only by bolts)")},
    "drop": {"height": (1000.0, "Drop height mm (impact speed √(2gh))"), "steps": (200, "Time steps"),
             "duration": (0.0, "Simulated time s (0 = auto from the contact time)")},
    "topology": {"volume_fraction": (0.4, "Material to keep (0.1 … 0.9)"), "iterations": (40, "Max iterations"),
                 "penalty": (3.0, "SIMP penalty"), "filter_radius": (0.0, "Filter radius mm (0 = 1.6 × element)")},
}


def new_study(doc, stype, name=None, obj=None, **options):
    if stype not in TYPES:
        raise SimError(f"Study type: one of {list(TYPES)}")
    base = {"static": "Static", "frequency": "Frequency", "buckling": "Buckling", "thermal": "Thermal",
            "fatigue": "Fatigue", "harmonic": "Harmonic", "flow": "Flow", "assembly": "Assembly",
            "drop": "Drop", "topology": "Topology"}[stype]
    if not name:
        n = 1
        while any(s["name"] == f"{base}{n}" for s in doc.studies):
            n += 1
        name = f"{base}{n}"
    if any(s["name"] == name for s in doc.studies):
        raise SimError(f"Study name '{name}' already used")
    obj = obj or doc.active_object
    doc.obj(obj)
    opts = {k: v[0] for k, v in OPTIONS.get(stype, {}).items()}
    for k, v in options.items():
        if k not in opts and k != "mesh_size":
            raise SimError(f"{TYPES[stype]} has no option '{k}'. Options: {list(opts)}")
    opts.update({k: v for k, v in options.items() if k != "mesh_size"})
    if stype == "fatigue" and not opts.get("source"):
        src = [s["name"] for s in doc.studies if s["type"] == "static" and s["object"] == obj]
        opts["source"] = src[-1] if src else ""
    st = {"name": name, "type": stype, "object": obj, "mesh_size": options.get("mesh_size"),
          "fixtures": [], "loads": [], "thermal": [], "options": opts}
    if stype == "assembly":
        st["connectors"] = []
    return st


def face_refs(doc, obj, faces):
    """faces: list of face indices / doc.ref_on specs → stored geometric refs."""
    out = []
    for f in faces:
        r = doc.ref_on(obj, f)
        if r.get("face") is None:
            raise SimError(f"{f} doesn't name a face")
        out.append(r)
    return out


def resolve_faces(body, refs):
    out = []
    for r in refs:
        rr = asm.resolve_ref(body, r)
        if rr.get("face") is not None:
            out.append(int(rr["face"]))
    return sorted(set(out))


def _local_dir(vec, pose):
    R = asm.rotmat(pose[3:6])
    return list(R.T @ np.asarray(vec, float))


def _local_point(pt, pose):
    M = asm.pose_matrix(pose)
    return list((np.linalg.inv(M) @ np.append(np.asarray(pt, float), 1.0))[:3])


def solver_study(doc, st):
    """Study dict with face refs resolved to face indices and world directions made local."""
    body = doc.result.bodies.get(st["object"])
    if body is None:
        raise SimError(f"{st['object']} has no solid body")
    pose = doc.obj(st["object"]).pose
    out = {"name": st["name"], "mesh_size": st.get("mesh_size"), **st.get("options", {})}
    for key in ("fixtures", "loads", "thermal"):
        items = []
        for it in st.get(key, []):
            if it.get("suppressed"):
                continue
            d = {k: v for k, v in it.items() if k != "refs"}
            if "refs" in it:
                d["faces"] = resolve_faces(body, it["refs"])
                if not d["faces"]:
                    raise SimError(f"{it.get('name', it['type'])}: its faces no longer exist")
            if isinstance(d.get("direction"), (list, tuple)):
                d["direction"] = _local_dir(d["direction"], pose)
            if d.get("point") is not None:
                d["point"] = _local_point(d["point"], pose)
            items.append(d)
        out[key] = items
    if out.get("transient"):
        out["transient"] = {"duration": out.get("duration", 60.0), "steps": int(out.get("steps", 60)),
                            "initial": out.get("initial", 20.0)}
    else:
        out["transient"] = None
    return body, out


def fingerprint(doc, st) -> str:
    body = doc.result.bodies.get(st["object"])
    sig = json.dumps(st, sort_keys=True, default=str)
    if body is not None:
        bb = body.bounding_box()
        sig += f"|{body.volume:.6g}|{body.area:.6g}|{bb.min}|{bb.max}|{doc.material_of(st['object']).to_dict()}"
    return hashlib.sha1(sig.encode()).hexdigest()


def run(doc, name, progress=None, cancel=None):
    st = doc.study(name)
    typ = st["type"]
    if progress:
        progress(f"{name}: preparing…")
    if typ == "flow":
        from . import cfd
        opts = dict(st.get("options", {}))
        names = [n.strip() for n in str(opts.get("objects", "")).split(",") if n.strip()]
        placed = doc.placed_bodies()
        bodies = {n: b for n, b in placed.items() if (not names or n in names) and doc.obj(n).visible}
        try:
            res = cfd.run(bodies, {"name": name, **opts}, progress=progress, cancel=cancel)
        except cfd.FlowError as e:
            raise SimError(str(e)) from e
        res.fingerprint = fingerprint(doc, st)
        return res
    if typ == "assembly":
        from .advanced import assembly_static
        res = assembly_static(doc, st, progress)
        res.fingerprint = fingerprint(doc, st)
        res.object = None
        res.hide_objects = list(res.summary.get("parts", []))
        return res
    if typ == "drop":
        from .advanced import drop_test
        body = doc.placed_bodies().get(st["object"])
        if body is None:
            raise SimError(f"{st['object']} has no solid body")
        try:
            res = drop_test(body, doc.material_of(st["object"]), {"name": name, "mesh_size": st.get("mesh_size"),
                                                                   **st.get("options", {})}, progress, cancel)
        except MeshError_ as e:
            raise SimError(str(e)) from e
        res.fingerprint = fingerprint(doc, st)
        res.object = None
        res.hide_objects = [st["object"]]
        return res
    material = doc.material_of(st["object"])
    body, s = solver_study(doc, st)
    if typ == "topology":
        from .advanced import topology
        try:
            res = topology(body, material, s, st.get("options", {}), progress, cancel)
        except MeshError_ as e:
            raise SimError(str(e)) from e
        res.fingerprint = fingerprint(doc, st)
        res.object = st["object"]
        return res
    from .mesh import MeshError, mesh_solid
    try:
        if progress:
            progress(f"{name}: meshing…")
        sm = mesh_solid(body, s.get("mesh_size"))
        if progress:
            progress(f"{name}: solving ({sm.n_elems:,} elements)…")
        if typ == "static":
            dT = None
            src = s.get("temperature_from")
            if src:
                th_st = doc.study(src)
                if th_st["object"] != st["object"]:
                    raise SimError("The thermal study must be on the same object")
                _, ts = solver_study(doc, th_st)
                th = fea.thermal(body, material, ts, sm)
                dT = th.extra["T_nodal"] - 20.0
            if s.get("uniform_dT"):
                dT = (dT if dT is not None else np.zeros(sm.n_nodes)) + float(s["uniform_dT"])
            res = fea.static(body, material, s, sm, dT_nodal=dT)
        elif typ == "frequency":
            res = fea.frequency(body, material, s, sm)
        elif typ == "buckling":
            res = fea.buckling(body, material, s, sm)
        elif typ == "thermal":
            res = fea.thermal(body, material, s, sm)
        elif typ == "harmonic":
            if not s.get("fmax"):
                s["fmax"] = None
                s.pop("fmax")
            res = fea.harmonic(body, material, s, sm)
        elif typ == "fatigue":
            from . import fatigue
            src = s.get("source")
            if not src:
                raise SimError("Pick the static study this fatigue study uses (option 'source')")
            src_st = doc.study(src)
            if src_st["type"] != "static":
                raise SimError(f"{src} isn't a static study")
            _, ss = solver_study(doc, src_st)
            stat = fea.static(body, material, ss, sm)
            fs = dict(s)
            if not fs.get("size_mm"):
                fs["size_mm"] = None
            res = fatigue.run(stat, material, fs)
        else:
            raise SimError(f"Unknown study type {typ}")
    except MeshError as e:
        raise SimError(str(e)) from e
    res.fingerprint = fingerprint(doc, st)
    res.object = st["object"]
    return res


def summary_text(res) -> str:
    s = res.summary
    k = res.kind
    if k == "static":
        return (f"Max von Mises {s['max_von_mises_MPa']:.4g} MPa · max displacement {s['max_displacement_mm']:.4g} mm · "
                f"min FoS {s['min_factor_of_safety']:.3g}")
    if k == "frequency":
        return "Natural frequencies: " + ", ".join(f"{f:.4g}" for f in s["frequencies_Hz"]) + " Hz"
    if k == "buckling":
        return "Buckling load factors: " + (", ".join(f"{f:.4g}" for f in s["load_factors"]) or "none")
    if k == "thermal":
        return f"Temperature {s['min_temperature_C']:.4g} … {s['max_temperature_C']:.4g} °C"
    if k == "fatigue":
        life = s["min_life_cycles"]
        return (f"Min life {life if isinstance(life, str) else f'{life:.3g}'} cycles · max damage "
                f"{s['max_damage']:.3g} · fatigue FoS {s['min_fatigue_FoS']:.3g}")
    if k == "harmonic":
        return (f"Peak at {s['peak_frequency_Hz']:.4g} Hz: {s['peak_displacement_mm']:.4g} mm, "
                f"{s['peak_stress_MPa']:.4g} MPa")
    if k == "drop":
        return (f"Impact {s['impact_velocity_m_s']:.3g} m/s · peak {s['peak_deceleration_g']:.4g} g · "
                f"max von Mises {s['max_von_mises_MPa']:.4g} MPa · FoS {s['min_factor_of_safety']:.3g}")
    if k == "topology":
        return (f"Kept {100 * s['volume_fraction_kept']:.0f} % · compliance {s['compliance_start']:.4g} → "
                f"{s['compliance_end']:.4g} N·mm")
    if k == "flow":
        parts = [f"Re {s['reynolds']:.3g}"]
        if s.get("pressure_drop_Pa") is not None:
            parts.append(f"Δp {s['pressure_drop_Pa']:.4g} Pa, Q {s['flow_rate_L_min']:.4g} L/min")
        for n, o in (s.get("objects") or {}).items():
            parts.append(f"{n}: drag {o['drag_N']:.4g} N" + (f", Cd {o['Cd']:.3g}" if "Cd" in o else ""))
        return " · ".join(parts)
    return ""


def brief(res) -> dict:
    """JSON-friendly summary for Claude."""
    out = {"kind": res.kind, **copy.deepcopy(res.summary), "notes": list(res.notes)}
    return json.loads(json.dumps(out, default=lambda o: float(o) if np.isscalar(o) else str(o)))
