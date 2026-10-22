"""Stress-life (S-N) fatigue on top of a static result. Qt-free.

S-N curve (Basquin, log-log line):  S = a · N^b through (1e3 cycles, f·UTS) and
(Nₑ, Sₑ) where Sₑ = material fatigue strength × Marin factors (surface, size, reliability, load).
Steels/Ti with an endurance limit: no damage below Sₑ (infinite life).
Mean stress: Goodman (default), Gerber, Soderberg, ASME-elliptic or none.
Loading: fully reversed (R = −1), zero-based (R = 0) or any ratio R = σmin/σmax; amplitude scale.
Stress: signed von Mises (sign of the larger-magnitude principal stress).
"""
from __future__ import annotations

import math

import numpy as np

from .fea import FieldResult, SimError

SURFACE = {  # Shigley: ka = a · Sut^b  (Sut MPa)
    "polished": None, "ground": (1.58, -0.085), "machined": (4.51, -0.265), "cold-drawn": (4.51, -0.265),
    "hot-rolled": (57.7, -0.718), "as-forged": (272.0, -0.995), "as-cast": (272.0, -0.995),
    "3D printed": None,
}
RELIABILITY = {0.5: 1.0, 0.9: 0.897, 0.95: 0.868, 0.99: 0.814, 0.999: 0.753, 0.9999: 0.702}
MEAN = ("goodman", "gerber", "soderberg", "asme", "none")


def sn_curve(material, ka=1.0, kb=1.0, kc=1.0, kd=1.0, ke=1.0):
    """(a, b, Se, Ne, endurance): S = a·N^b for 1e3 ≤ N ≤ Ne."""
    su = material.uts
    f = 0.9 if su < 500 else max(0.77, 0.9 - (su - 500) * 0.13 / 900)   # Shigley fig. 6-18, roughly
    S1e3 = f * su
    Se = material.fatigue * ka * kb * kc * kd * ke
    Ne = float(material.fatigue_cycles)
    Se = min(Se, S1e3 * 0.999)
    b = math.log10(Se / S1e3) / math.log10(Ne / 1e3)
    a = S1e3 / (1e3 ** b)
    return a, b, Se, Ne, material.endurance


def marin(material, surface="machined", size_mm=None, reliability=0.9, loading="bending"):
    k = {}
    sv = SURFACE.get(surface)
    k["ka"] = 1.0 if sv is None else min(1.0, sv[0] * material.uts ** sv[1])
    if surface == "3D printed":
        k["ka"] = 0.6
    if size_mm is None or loading == "axial":
        k["kb"] = 1.0
    elif size_mm <= 51:
        k["kb"] = 1.24 * max(size_mm, 2.79) ** -0.107 if size_mm >= 2.79 else 1.0
    else:
        k["kb"] = 1.51 * size_mm ** -0.157
    k["kc"] = {"bending": 1.0, "axial": 0.85, "torsion": 0.59}.get(loading, 1.0)
    rel = min(RELIABILITY, key=lambda r: abs(r - reliability))
    k["ke"] = RELIABILITY[rel]
    return k


def equivalent_alternating(sa, sm, su, sy, method):
    """Fully reversed amplitude equivalent to (σa, σm)."""
    sm_pos = np.maximum(sm, 0.0)                    # compressive mean doesn't hurt (conservative)
    if method == "none":
        return sa
    if method == "goodman":
        r = 1 - sm_pos / su
    elif method == "gerber":
        r = 1 - (sm_pos / su) ** 2
    elif method == "soderberg":
        r = 1 - sm_pos / sy
    elif method == "asme":
        return sa / np.sqrt(np.maximum(1e-9, 1 - (sm_pos / sy) ** 2))
    else:
        raise SimError(f"Mean-stress method: one of {MEAN}")
    return np.where(r > 1e-6, sa / np.maximum(r, 1e-6), np.inf)


def run(static_res: FieldResult, material, study) -> FieldResult:
    import time
    t0 = time.perf_counter()
    res = _run(static_res, material, study)
    res.ms = static_res.ms + (time.perf_counter() - t0) * 1000
    return res


def _run(static_res: FieldResult, material, study) -> FieldResult:
    """study keys: loading ('reversed' | 'zero' | 'ratio'), R, cycles, mean, surface, reliability,
    size_mm, load_type ('bending'|'axial'|'torsion'), scale."""
    if static_res.kind != "static":
        raise SimError("Fatigue needs a static study's result")
    sig = static_res.extra["sig_q"]
    model = static_res.extra["model"]
    from .fea import _principal, _von_mises
    vm_q = _von_mises(sig)
    s1, _, s3 = _principal(sig)
    signed = np.where(np.abs(s1) >= np.abs(s3), np.sign(s1), np.sign(s3)) * vm_q
    smax = model.project(signed) * float(study.get("scale", 1.0))
    loading = study.get("loading", "reversed")
    R = {"reversed": -1.0, "zero": 0.0}.get(loading, float(study.get("R", -1.0)))
    if R >= 1:
        raise SimError("Stress ratio R must be < 1")
    smin = R * smax
    sa = np.abs(smax - smin) / 2
    sm = (smax + smin) / 2
    k = marin(material, study.get("surface", "machined"), study.get("size_mm"), float(study.get("reliability", 0.9)),
              study.get("load_type", "bending"))
    a, b, Se, Ne, endurance = sn_curve(material, **k)
    method = study.get("mean", "goodman")
    sar = equivalent_alternating(sa, sm, material.uts, material.yield_, method)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        life = np.where(sar > 0, (sar / a) ** (1.0 / b), np.inf)
    life = np.where(sar >= 0.9 * material.uts, 1.0, life)
    if endurance:
        life = np.where(sar <= Se, np.inf, life)
    life = np.minimum(life, 1e12)                     # display cap ("infinite")
    target = float(study.get("cycles", 1e6))
    damage = target / np.maximum(life, 1.0)
    S_target = a * target ** b if (target < Ne or not endurance) else Se
    if endurance and target >= Ne:
        S_target = Se
    fos = S_target / np.maximum(sar, 1e-9)
    i = int(np.argmax(sar))                          # most critical point
    res = FieldResult(study["name"], "fatigue", static_res.p, static_res.tris)
    res.scalars = {"life (cycles)": life, "damage at target": damage, "fatigue factor of safety": np.minimum(fos, 1e3),
                   "equivalent alternating stress": sar}
    res.units = {"life (cycles)": "cycles", "damage at target": "", "fatigue factor of safety": "",
                 "equivalent alternating stress": "MPa"}
    Ns = np.logspace(3, max(math.log10(Ne) + 1, 7), 60)
    S = np.where((Ns > Ne) & endurance, Se, a * Ns ** b)
    res.curves = {"S-N curve": (list(map(float, Ns)), list(map(float, S)), "cycles", "MPa")}
    min_life = float(life[i])
    res.summary = {
        "material": material.name, "loading": loading, "R": R, "mean_stress_correction": method,
        "target_cycles": target, "min_life_cycles": "infinite" if min_life >= 1e12 else round(min_life, 0),
        "min_life_at": [round(float(c), 3) for c in static_res.p[i]],
        "max_damage": round(float(damage.max()), 5), "min_fatigue_FoS": round(float(fos.min()), 3),
        "corrected_endurance_MPa": round(Se, 2), "marin_factors": {k_: round(v, 3) for k_, v in k.items()},
        "passes": bool(damage.max() < 1.0),
    }
    if not res.summary["passes"]:
        res.notes.append(f"Fails before {target:.3g} cycles (damage {damage.max():.2f} > 1)")
    if not material.endurance:
        res.notes.append(f"{material.name} has no endurance limit: S-N extrapolated past {Ne:.0e} cycles")
    if material.category in ("Plastic", "Wood", "Composite", "Elastomer"):
        res.notes.append("Fatigue data for this material class are rough estimates")
    return res
