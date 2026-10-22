"""Machine-design calculators (the classic design-data-handbook procedures). Qt-free.

Each calculator declares its inputs once; that spec drives the Design Data dialog, validation and the
Icarus / Claude tool. Units: mm, N, MPa (= N/mm²), N·m for torques and moments, kW, rpm, degrees.

The formulas are the standard textbook / code forms (Lamé, Lewis, Buckingham, ASME shaft code, Soderberg,
Goodman, Gerber, Rankine-Gordon, Hertz, band-brake e^μθ …), grouped like the Design Data Handbook chapters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


class CalcError(ValueError):
    pass


@dataclass
class In:
    name: str
    label: str
    default: Any
    unit: str = ""
    choices: list | None = None
    help: str = ""
    minimum: float | None = None


@dataclass
class Calc:
    id: str
    title: str
    chapter: str
    inputs: list
    fn: Callable
    help: str = ""
    material_inputs: dict = field(default_factory=dict)   # input -> Material attribute (prefill from model)

    def defaults(self) -> dict:
        return {i.name: i.default for i in self.inputs}

    def run(self, values: dict | None = None) -> dict:
        v = self.defaults()
        for k, x in (values or {}).items():
            spec = next((i for i in self.inputs if i.name == k), None)
            if spec is None:
                raise CalcError(f"{self.title}: no input '{k}'. Inputs: {[i.name for i in self.inputs]}")
            if spec.choices:
                if x not in spec.choices:
                    raise CalcError(f"{spec.label}: one of {spec.choices}")
            elif isinstance(spec.default, bool):
                x = x if isinstance(x, bool) else str(x).lower() in ("1", "true", "yes")
            elif isinstance(spec.default, (int, float)):
                try:
                    x = float(x)
                except (TypeError, ValueError) as e:
                    raise CalcError(f"{spec.label}: not a number ({x!r})") from e
                if spec.minimum is not None and x < spec.minimum:
                    raise CalcError(f"{spec.label} must be ≥ {spec.minimum:g}")
            v[k] = x
        try:
            out = self.fn(**v)
        except ZeroDivisionError as e:
            raise CalcError(f"{self.title}: a value is zero where it can't be") from e
        except ValueError as e:
            if isinstance(e, CalcError):
                raise
            raise CalcError(f"{self.title}: {e}") from e
        return out

    def spec(self) -> dict:
        return {"id": self.id, "title": self.title, "chapter": self.chapter, "help": self.help,
                "inputs": [{"name": i.name, "label": i.label, "unit": i.unit, "default": i.default,
                            **({"choices": i.choices} if i.choices else {})} for i in self.inputs]}


def R(value, unit="", note=""):
    """One result row."""
    return {"value": value, "unit": unit, "note": note}


REGISTRY: dict[str, Calc] = {}


def calc(id, title, chapter, inputs, help="", material=None):
    def deco(fn):
        REGISTRY[id] = Calc(id, title, chapter, inputs, fn, help, material or {})
        return fn
    return deco


def _ok(cond, msg):
    if not cond:
        raise CalcError(msg)


# =========================================================== 1 Stress analysis ===

@calc("principal_stress", "Principal stresses & Mohr's circle", "Stress analysis", [
    In("sx", "σx", 80.0, "MPa"), In("sy", "σy", -20.0, "MPa"), In("txy", "τxy", 30.0, "MPa"),
    In("sy_mat", "Yield strength Sy (for factors of safety)", 250.0, "MPa", minimum=0.0),
], "Plane stress → principal stresses, maximum shear, von Mises, and the factor of safety by each failure theory.",
    material={"sy_mat": "yield_"})
def principal_stress(sx, sy, txy, sy_mat):
    c = (sx + sy) / 2
    r = math.hypot((sx - sy) / 2, txy)
    s1, s2 = c + r, c - r
    theta = 0.5 * math.degrees(math.atan2(2 * txy, sx - sy))
    vm = math.sqrt(s1 ** 2 - s1 * s2 + s2 ** 2)
    tresca = max(abs(s1 - s2), abs(s1), abs(s2))
    out = {"σ1": R(s1, "MPa"), "σ2": R(s2, "MPa"), "τmax (in-plane)": R(r, "MPa"),
           "τmax (absolute)": R(max(r, abs(s1) / 2, abs(s2) / 2), "MPa"), "Principal angle θp": R(theta, "°"),
           "Mohr centre": R(c, "MPa"), "von Mises σe": R(vm, "MPa")}
    if sy_mat > 0:
        out["FoS — max normal (Rankine)"] = R(sy_mat / max(abs(s1), abs(s2), 1e-12))
        out["FoS — max shear (Guest/Tresca)"] = R(sy_mat / max(tresca, 1e-12))
        out["FoS — distortion energy (von Mises)"] = R(sy_mat / max(vm, 1e-12), "", "ductile materials")
        nu = 0.3
        ep = max(abs(s1 - nu * s2), abs(s2 - nu * s1))
        out["FoS — max strain (Saint-Venant, ν=0.3)"] = R(sy_mat / max(ep, 1e-12))
    return out


@calc("combined_shaft_stress", "Bending + torsion (+ axial) on a round bar", "Stress analysis", [
    In("d", "Diameter", 40.0, "mm", minimum=0.01), In("di", "Bore (0 = solid)", 0.0, "mm", minimum=0.0),
    In("M", "Bending moment", 500.0, "N·m"), In("T", "Torque", 800.0, "N·m"), In("F", "Axial force", 0.0, "N"),
    In("sy_mat", "Yield strength Sy", 350.0, "MPa", minimum=0.0),
], "Equivalent bending and twisting moments (Rankine / Guest), von Mises stress and factor of safety.",
    material={"sy_mat": "yield_"})
def combined_shaft_stress(d, di, M, T, F, sy_mat):
    _ok(di < d, "Bore must be smaller than the diameter")
    k = 1 - (di / d) ** 4
    Mn, Tn = M * 1e3, T * 1e3
    sb = 32 * Mn / (math.pi * d ** 3 * k)
    sa = F / (math.pi / 4 * (d ** 2 - di ** 2))
    t = 16 * Tn / (math.pi * d ** 3 * k)
    s = sb + sa
    Me = 0.5 * (M + math.hypot(M, T))
    Te = math.hypot(M, T)
    vm = math.sqrt(s ** 2 + 3 * t ** 2)
    return {"Bending stress": R(sb, "MPa"), "Axial stress": R(sa, "MPa"), "Shear stress (torsion)": R(t, "MPa"),
            "Equivalent bending moment Me": R(Me, "N·m", "max normal stress theory"),
            "Equivalent twisting moment Te": R(Te, "N·m", "max shear stress theory"),
            "Max principal stress": R(s / 2 + math.hypot(s / 2, t), "MPa"),
            "Max shear stress": R(math.hypot(s / 2, t), "MPa"), "von Mises": R(vm, "MPa"),
            "Factor of safety (von Mises)": R(sy_mat / vm if vm else float("inf"))}


@calc("hertz_contact", "Hertz contact stress", "Stress analysis", [
    In("kind", "Contact", "sphere-sphere", choices=["sphere-sphere", "sphere-flat", "cylinder-cylinder",
                                                    "cylinder-flat"]),
    In("F", "Load", 1000.0, "N", minimum=0.0), In("d1", "Diameter 1", 20.0, "mm", minimum=0.01),
    In("d2", "Diameter 2 (−ve for a concave seat)", 30.0, "mm"), In("L", "Contact length (cylinders)", 20.0, "mm",
                                                                    minimum=0.01),
    In("E1", "E1", 207.0, "GPa", minimum=0.1), In("nu1", "ν1", 0.3), In("E2", "E2", 207.0, "GPa", minimum=0.1),
    In("nu2", "ν2", 0.3),
], "Contact patch size and maximum pressure (Hertz).", material={"E1": "E", "nu1": "nu", "E2": "E", "nu2": "nu"})
def hertz_contact(kind, F, d1, d2, L, E1, nu1, E2, nu2):
    m = (1 - nu1 ** 2) / (E1 * 1e3) + (1 - nu2 ** 2) / (E2 * 1e3)
    flat = kind.endswith("flat")
    inv = 1 / d1 + (0 if flat else 1 / d2)
    _ok(inv > 0, "The bodies don't touch convexly (check the diameters)")
    if kind.startswith("sphere"):
        a = (3 * F * m / (8 * inv)) ** (1 / 3)
        pmax = 3 * F / (2 * math.pi * a ** 2)
        return {"Contact radius a": R(a, "mm"), "Max pressure pmax": R(pmax, "MPa"),
                "Max shear (≈0.31 pmax, at 0.48a deep)": R(0.31 * pmax, "MPa"),
                "Approach of centres": R(2 * a ** 2 * inv, "mm")}
    b = math.sqrt(2 * F * m / (math.pi * L * inv))
    pmax = 2 * F / (math.pi * b * L)
    return {"Half-width b": R(b, "mm"), "Max pressure pmax": R(pmax, "MPa"),
            "Max shear (≈0.30 pmax, at 0.78b deep)": R(0.30 * pmax, "MPa")}


@calc("column_buckling", "Column buckling (Euler / Rankine-Gordon / Johnson)", "Stress analysis", [
    In("section", "Section", "solid round", choices=["solid round", "hollow round", "rectangle"]),
    In("d", "Diameter / width b", 40.0, "mm", minimum=0.01), In("t", "Bore / depth h", 0.0, "mm", minimum=0.0),
    In("L", "Length", 1500.0, "mm", minimum=0.01),
    In("ends", "End conditions", "pinned-pinned", choices=["pinned-pinned", "fixed-free", "fixed-pinned",
                                                           "fixed-fixed"]),
    In("E", "E", 207.0, "GPa", minimum=0.1), In("sc", "Yield / crushing stress σc", 250.0, "MPa", minimum=0.1),
    In("P", "Applied load", 20000.0, "N", minimum=0.0),
], "Critical load by Euler (long), Johnson's parabola (intermediate) and Rankine-Gordon, with the slenderness check.",
    material={"E": "E", "sc": "yield_"})
def column_buckling(section, d, t, L, ends, E, sc, P):
    if section == "solid round":
        A, I = math.pi * d ** 2 / 4, math.pi * d ** 4 / 64
    elif section == "hollow round":
        _ok(t < d, "Bore must be smaller than the diameter")
        A, I = math.pi * (d ** 2 - t ** 2) / 4, math.pi * (d ** 4 - t ** 4) / 64
    else:
        _ok(t > 0, "Give the depth h")
        A, I = d * t, min(d * t ** 3, t * d ** 3) / 12
    K = {"pinned-pinned": 1.0, "fixed-free": 2.0, "fixed-pinned": 0.7, "fixed-fixed": 0.5}[ends]
    Le = K * L
    k = math.sqrt(I / A)
    lam = Le / k
    Em = E * 1e3
    Pe = math.pi ** 2 * Em * I / Le ** 2
    a = sc / (math.pi ** 2 * Em)
    Pr = sc * A / (1 + a * lam ** 2)
    lam_t = math.sqrt(2 * math.pi ** 2 * Em / sc)
    Pj = A * sc * (1 - sc * lam ** 2 / (4 * math.pi ** 2 * Em))
    Pcr = Pe if lam >= lam_t else Pj
    return {"Effective length": R(Le, "mm"), "Radius of gyration k": R(k, "mm"), "Slenderness Le/k": R(lam),
            "Transition slenderness": R(lam_t, "", "above: Euler governs"),
            "Euler load": R(Pe, "N"), "Johnson load": R(Pj, "N"), "Rankine-Gordon load": R(Pr, "N",
                                                                                      f"a = {a:.3e}"),
            "Critical load used": R(Pcr, "N", "Euler" if lam >= lam_t else "Johnson"),
            "Factor of safety": R(Pcr / P if P else float("inf"))}


@calc("impact_stress", "Impact stress (falling weight)", "Stress analysis", [
    In("W", "Falling weight", 500.0, "N", minimum=0.0), In("h", "Drop height", 50.0, "mm", minimum=0.0),
    In("A", "Bar section area", 400.0, "mm²", minimum=0.01), In("L", "Bar length", 1000.0, "mm", minimum=0.01),
    In("E", "E", 207.0, "GPa", minimum=0.1),
], "Stress and extension of a bar hit axially by a falling weight: σ = W/A (1 + √(1 + 2hAE/(WL))).",
    material={"E": "E"})
def impact_stress(W, h, A, L, E):
    Em = E * 1e3
    s_static = W / A
    s = s_static * (1 + math.sqrt(1 + 2 * h * A * Em / (W * L))) if W else 0.0
    return {"Static stress W/A": R(s_static, "MPa"), "Impact stress": R(s, "MPa"),
            "Impact factor": R(s / s_static if s_static else 0.0), "Max extension": R(s * L / Em, "mm"),
            "Strain energy": R(s ** 2 / (2 * Em) * A * L / 1e3, "J")}


# ========================================================= 2 Working stresses ===

FINISH = {  # Marin surface factor ka = a·Su^b (Su MPa)
    "ground": (1.58, -0.085), "machined / cold drawn": (4.51, -0.265), "hot rolled": (57.7, -0.718),
    "as forged": (272.0, -0.995),
}
RELIABILITY = {"50%": 1.0, "90%": 0.897, "95%": 0.868, "99%": 0.814, "99.9%": 0.753, "99.99%": 0.702}


@calc("fatigue", "Fatigue design (Soderberg / Goodman / Gerber / ASME)", "Working stresses", [
    In("smax", "σmax", 180.0, "MPa"), In("smin", "σmin", -60.0, "MPa"),
    In("su", "Ultimate strength Su", 540.0, "MPa", minimum=1.0), In("sy", "Yield strength Sy", 350.0, "MPa", minimum=1.0),
    In("se_prime", "Endurance limit Se' (0 = 0.5 Su)", 0.0, "MPa", minimum=0.0),
    In("finish", "Surface", "machined / cold drawn", choices=list(FINISH)),
    In("d", "Size (diameter)", 30.0, "mm", minimum=0.1),
    In("loading", "Loading", "bending", choices=["bending", "axial", "torsion"]),
    In("reliability", "Reliability", "99%", choices=list(RELIABILITY)),
    In("kt", "Stress concentration Kt", 1.0, minimum=1.0), In("q", "Notch sensitivity q", 0.8, minimum=0.0),
    In("temp_factor", "Temperature factor", 1.0, minimum=0.01),
], "Corrected endurance limit (Marin factors) and the factor of safety by each mean-stress criterion.",
    material={"su": "uts", "sy": "yield_"})
def fatigue(smax, smin, su, sy, se_prime, finish, d, loading, reliability, kt, q, temp_factor):
    sm, sa = (smax + smin) / 2, (smax - smin) / 2
    se0 = se_prime or (0.5 * su if su <= 1400 else 700.0)
    a, b = FINISH[finish]
    ka = min(a * su ** b, 1.0)
    if loading == "axial":
        kb = 1.0
    else:
        kb = 1.24 * d ** -0.107 if d <= 51 else 1.51 * d ** -0.157
        kb = min(kb, 1.0)
    kc = {"bending": 1.0, "axial": 0.85, "torsion": 0.59}[loading]
    kd = temp_factor
    ke = RELIABILITY[reliability]
    kf = 1 + q * (kt - 1)
    se = se0 * ka * kb * kc * kd * ke
    su_l = su * (0.67 if loading == "torsion" else 1.0)
    sy_l = sy * (0.577 if loading == "torsion" else 1.0)
    saf = kf * sa
    out = {"Mean stress σm": R(sm, "MPa"), "Alternating stress σa": R(sa, "MPa"),
           "Fatigue factor Kf": R(kf, "", "applied to σa"), "Surface factor ka": R(ka), "Size factor kb": R(kb),
           "Load factor kc": R(kc), "Reliability factor ke": R(ke), "Corrected endurance limit Se": R(se, "MPa")}
    if sm <= 0:
        n = se / saf if saf else float("inf")
        out["Factor of safety (σm ≤ 0: Se/σa)"] = R(n)
    else:
        n_sod = 1 / (saf / se + sm / sy_l)
        n_good = 1 / (saf / se + sm / su_l)
        n_ger = 0.5 * (su_l / sm) ** 2 * (saf / se) * (-1 + math.sqrt(1 + 4 * (sm * se / (su_l * saf)) ** 2)) \
            if saf else float("inf")
        n_asme = 1 / math.sqrt((saf / se) ** 2 + (sm / sy_l) ** 2)
        out["FoS — Soderberg"] = R(n_sod, "", "conservative")
        out["FoS — Goodman"] = R(n_good)
        out["FoS — Gerber"] = R(n_ger)
        out["FoS — ASME elliptic"] = R(n_asme)
    out["FoS — first-cycle yield"] = R(sy_l / max(abs(smax), abs(smin), 1e-12))
    return out


@calc("notch_sensitivity", "Notch sensitivity & fatigue factor", "Working stresses", [
    In("kt", "Theoretical Kt", 2.0, minimum=1.0), In("r", "Notch radius", 1.0, "mm", minimum=0.001),
    In("su", "Ultimate strength Su", 540.0, "MPa", minimum=1.0),
    In("loading", "Loading", "bending/axial", choices=["bending/axial", "torsion"]),
], "Neuber constant from Su (steels), q = 1/(1 + √a/√r), Kf = 1 + q(Kt − 1).", material={"su": "uts"})
def notch_sensitivity(kt, r, su, loading):
    s = su / 6.895                                   # ksi
    sa = s + (20 if loading == "torsion" else 0)
    root_a = (0.246 - 3.08e-3 * sa + 1.51e-5 * sa ** 2 - 2.67e-8 * sa ** 3) * math.sqrt(25.4)  # √mm
    q = 1 / (1 + root_a / math.sqrt(r))
    return {"Neuber constant √a": R(root_a, "√mm"), "Notch sensitivity q": R(q),
            "Fatigue concentration Kf": R(1 + q * (kt - 1))}


FOS_TABLE = {  # material: (steady, varying, shock)
    "Cast iron": ("5–6", "8–12", "16–20"), "Wrought iron": ("4", "7", "10–15"), "Steel": ("4", "8", "12–16"),
    "Soft metals and alloys": ("6", "9", "15"), "Leather": ("9", "12", "15"), "Timber": ("7", "10–15", "20"),
}


@calc("factor_of_safety", "Factor of safety (n = a × b × c)", "Working stresses", [
    In("material", "Material", "Steel", choices=list(FOS_TABLE)),
    In("load", "Load", "steady", choices=["steady", "varying", "shock"]),
    In("su", "Ultimate strength Su", 410.0, "MPa", minimum=1.0), In("sy", "Yield strength Sy", 250.0, "MPa", minimum=1.0),
    In("b", "Shock factor b", 1.0, minimum=1.0, help="1 steady · 2 suddenly applied · 1.25–1.5 light, 2–3 heavy shock"),
    In("c", "Margin c", 1.5, minimum=1.0, help="1.5–2 ductile uniform · 2.5–3 cast iron / brittle · 3–4 timber"),
], "Typical factors by material and load type, and n = a·b·c with a = Su/Sy (elastic ratio); design stress = Su/n.",
    material={"su": "uts", "sy": "yield_"})
def factor_of_safety(material, load, su, sy, b, c):
    i = ["steady", "varying", "shock"].index(load)
    a = su / sy
    n = a * b * c
    return {"Typical factor (on Su)": R(FOS_TABLE[material][i], "", f"{material}, {load} load"),
            "Elastic ratio a = Su/Sy": R(a), "n = a·b·c": R(n), "Design stress Su/n": R(su / n, "MPa")}


# ========================================================== 3 Design of shafts ===

ASME_K = {  # (Km, Kt)
    "stationary, gradual": (1.0, 1.0), "stationary, sudden": (1.75, 1.75),
    "rotating, gradual / steady": (1.5, 1.0), "rotating, sudden minor shock": (1.75, 1.25),
    "rotating, sudden heavy shock": (2.5, 2.25),
}


@calc("shaft_asme", "Shaft diameter (ASME code)", "Design of shafts", [
    In("power", "Power (0 = use torque)", 7.5, "kW", minimum=0.0), In("rpm", "Speed", 500.0, "rpm", minimum=0.0),
    In("T", "Torque (if power = 0)", 0.0, "N·m", minimum=0.0), In("M", "Bending moment", 300.0, "N·m", minimum=0.0),
    In("F", "Axial load", 0.0, "N", minimum=0.0),
    In("service", "Load", "rotating, gradual / steady", choices=list(ASME_K)),
    In("su", "Ultimate strength Su", 540.0, "MPa", minimum=1.0), In("sy", "Yield strength Sy", 350.0, "MPa", minimum=1.0),
    In("keyway", "Keyway", True), In("k", "Bore / outside ratio (hollow)", 0.0, minimum=0.0),
    In("L", "Length for twist check", 1000.0, "mm", minimum=0.0), In("G", "Shear modulus G", 80.0, "GPa", minimum=0.1),
], "d³ = 16/(π τ (1−k⁴)) · √((Km M)² + (Kt T)²); τ allowable = min(0.30 Sy, 0.18 Su), −25 % with a keyway. "
   "Includes the twist check (≤ 0.25°/m).", material={"su": "uts", "sy": "yield_"})
def shaft_asme(power, rpm, T, M, F, service, su, sy, keyway, k, L, G):
    _ok(k < 1, "Bore ratio must be below 1")
    if power:
        _ok(rpm > 0, "Give the speed")
        T = power * 1e3 * 60 / (2 * math.pi * rpm)
    Km, Kt = ASME_K[service]
    tau = min(0.30 * sy, 0.18 * su) * (0.75 if keyway else 1.0)
    Tn, Mn = T * 1e3, M * 1e3
    d = (16 / (math.pi * tau * (1 - k ** 4)) * math.hypot(Km * Mn, Kt * Tn)) ** (1 / 3)
    if F:
        for _ in range(30):                                 # add axial: Km M + α F d (1 + k²)/8, α = 1
            d_new = (16 / (math.pi * tau * (1 - k ** 4)) * math.hypot(Km * Mn + F * d * (1 + k ** 2) / 8,
                                                                      Kt * Tn)) ** (1 / 3)
            if abs(d_new - d) < 1e-6:
                break
            d = d_new
    std = _preferred_shaft(d)
    J = math.pi * std ** 4 * (1 - k ** 4) / 32
    twist = math.degrees(Tn * L / (G * 1e3 * J)) if L else 0.0
    return {"Torque T": R(T, "N·m"), "Km / Kt": R(f"{Km} / {Kt}"), "Allowable shear τ": R(tau, "MPa"),
            "Required diameter": R(d, "mm"), "Standard diameter": R(std, "mm", "next preferred size"),
            **({"Bore": R(k * std, "mm")} if k else {}),
            "Twist over L": R(twist, "°", "limit ≈ 0.25°/m" if twist / max(L / 1000, 1e-9) <= 0.25 else
                              "exceeds 0.25°/m — stiffen")}


SHAFT_SIZES = [6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32, 35, 38, 40, 42, 45, 48, 50, 55, 60, 65, 70, 75, 80,
               85, 90, 95, 100, 110, 120, 125, 130, 140, 150, 160, 170, 180, 190, 200, 220, 240, 250, 260, 280, 300]


def _preferred_shaft(d):
    return next((s for s in SHAFT_SIZES if s >= d - 1e-9), math.ceil(d / 10) * 10)


@calc("critical_speed", "Critical speed (Dunkerley / Rayleigh)", "Design of shafts", [
    In("d", "Shaft diameter", 40.0, "mm", minimum=0.1), In("L", "Bearing span", 800.0, "mm", minimum=0.1),
    In("masses", "Masses: kg@mm from left, comma separated", "20@300, 10@550"),
    In("E", "E", 207.0, "GPa", minimum=0.1), In("density", "Shaft density", 7.85, "g/cm³", minimum=0.0),
], "Simply supported shaft: first whirling speed from the point masses and the shaft's own mass.",
    material={"E": "E", "density": "density"})
def critical_speed(d, L, masses, E, density):
    I = math.pi * d ** 4 / 64
    EI = E * 1e3 * I                                     # N·mm²
    g = 9810.0                                           # mm/s²
    items = []
    for part in [p for p in str(masses).split(",") if p.strip()]:
        m, _, a = part.partition("@")
        items.append((float(m), float(a)))
    inv = 0.0
    ys = []
    for m, a in items:
        _ok(0 < a < L, "Mass positions must be inside the span")
        b = L - a
        y = m * 9.81 * a ** 2 * b ** 2 / (3 * EI * L)        # mm, W = m g in N
        ys.append((m, y))
        inv += y / g
    w_shaft = density * 1e-6 * math.pi * d ** 2 / 4 * 9.81       # N/mm (density g/cm³ → kg/mm³ ×1e-6)
    y_shaft = 5 * w_shaft * L ** 4 / (384 * EI)
    inv_total = inv + y_shaft / (1.27 * g)
    n_d = 60 / (2 * math.pi) / math.sqrt(inv_total) if inv_total else float("inf")
    num = sum(m * y for m, y in ys) * g
    den = sum(m * y * y for m, y in ys)
    n_r = 60 / (2 * math.pi) * math.sqrt(num / den) if den else float("inf")
    return {"Shaft self-weight deflection": R(y_shaft, "mm"), "Critical speed (Dunkerley)": R(n_d, "rpm", "lower bound"),
            "Critical speed (Rayleigh, masses only)": R(n_r, "rpm", "upper bound"),
            "Safe running speed (≤ 0.7 × Dunkerley)": R(0.7 * n_d, "rpm")}


@calc("rect_torsion", "Torsion of a rectangular bar", "Design of shafts", [
    In("b", "Long side b", 40.0, "mm", minimum=0.1), In("c", "Short side c", 20.0, "mm", minimum=0.1),
    In("T", "Torque", 200.0, "N·m"), In("L", "Length", 500.0, "mm", minimum=0.0),
    In("G", "Shear modulus G", 80.0, "GPa", minimum=0.1),
], "τmax = T/(α b c²) at the middle of the long side; θ = T L/(β b c³ G) (Saint-Venant coefficients).")
def rect_torsion(b, c, T, L, G):
    if c > b:
        b, c = c, b
    r = b / c
    tab = [(1.0, 0.208, 0.1406), (1.5, 0.231, 0.196), (2.0, 0.246, 0.229), (2.5, 0.258, 0.249), (3.0, 0.267, 0.263),
           (4.0, 0.282, 0.281), (5.0, 0.291, 0.291), (6.0, 0.299, 0.299), (10.0, 0.312, 0.312), (1e9, 1 / 3, 1 / 3)]
    for (r0, a0, b0), (r1, a1, b1) in zip(tab, tab[1:]):
        if r0 <= r <= r1:
            f = (r - r0) / (r1 - r0) if r1 < 1e8 else min((r - r0) / 40, 1)
            alpha, beta = a0 + f * (a1 - a0), b0 + f * (b1 - b0)
            break
    Tn = T * 1e3
    tau = Tn / (alpha * b * c ** 2)
    theta = Tn * L / (beta * b * c ** 3 * G * 1e3)
    return {"α": R(alpha), "β": R(beta), "Max shear stress": R(tau, "MPa"), "Twist": R(math.degrees(theta), "°")}


# ============================================================== 4 Keys and pins ===

KEYS = [  # shaft d over, up to, b, h, t1 (shaft depth), t2 (hub depth)   (ISO R773 / DIN 6885)
    (6, 8, 2, 2, 1.2, 1.0), (8, 10, 3, 3, 1.8, 1.4), (10, 12, 4, 4, 2.5, 1.8), (12, 17, 5, 5, 3.0, 2.3),
    (17, 22, 6, 6, 3.5, 2.8), (22, 30, 8, 7, 4.0, 3.3), (30, 38, 10, 8, 5.0, 3.3), (38, 44, 12, 8, 5.0, 3.3),
    (44, 50, 14, 9, 5.5, 3.8), (50, 58, 16, 10, 6.0, 4.3), (58, 65, 18, 11, 7.0, 4.4), (65, 75, 20, 12, 7.5, 4.9),
    (75, 85, 22, 14, 9.0, 5.4), (85, 95, 25, 14, 9.0, 5.4), (95, 110, 28, 16, 10.0, 6.4),
    (110, 130, 32, 18, 11.0, 7.4), (130, 150, 36, 20, 12.0, 8.4), (150, 170, 40, 22, 13.0, 9.4),
    (170, 200, 45, 25, 15.0, 10.4), (200, 230, 50, 28, 17.0, 11.4),
]
KEY_LENGTHS = [6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63, 70, 80, 90, 100, 110, 125, 140,
               160, 180, 200, 220, 250, 280, 320, 360, 400]


@calc("parallel_key", "Parallel (feather) key", "Keys, pins and cotters", [
    In("d", "Shaft diameter", 40.0, "mm", minimum=6.0), In("T", "Torque", 400.0, "N·m", minimum=0.0),
    In("tau", "Allowable shear in the key", 60.0, "MPa", minimum=1.0),
    In("sc", "Allowable crushing (hub / shaft / key, the weakest)", 100.0, "MPa", minimum=1.0),
], "Standard section from the shaft size; length from shear (F = T/(d/2) = τ b l) and crushing (F = σc (h − t1) l).")
def parallel_key(d, T, tau, sc):
    row = next((k for k in KEYS if k[0] < d <= k[1]), None)
    _ok(row is not None, "Shaft size outside the key table (6 … 230 mm)")
    _, _, b, h, t1, t2 = row
    F = T * 1e3 / (d / 2)
    l_shear = F / (tau * b)
    l_crush = F / (sc * (h - t1))
    l = max(l_shear, l_crush)
    std = next((x for x in KEY_LENGTHS if x >= l), KEY_LENGTHS[-1])
    return {"Key b × h": R(f"{b} × {h}", "mm"), "Shaft keyway depth t1": R(t1, "mm"),
            "Hub keyway depth t2": R(t2, "mm"), "Tangential force": R(F, "N"),
            "Length for shear": R(l_shear, "mm"), "Length for crushing": R(l_crush, "mm"),
            "Standard length": R(std, "mm", "hub ≥ this; ≈1.5 d typical" if std < 1.5 * d else "long — consider two keys")}


@calc("cotter_joint", "Socket & spigot cotter joint", "Keys, pins and cotters", [
    In("P", "Axial load", 50000.0, "N", minimum=0.0), In("st", "Allowable tension", 80.0, "MPa", minimum=1.0),
    In("tau", "Allowable shear", 60.0, "MPa", minimum=1.0), In("sc", "Allowable crushing", 120.0, "MPa", minimum=1.0),
], "Standard proportions checked for tension, shear and crushing: rod d, spigot d2, socket d1, collars, cotter b × t.")
def cotter_joint(P, st, tau, sc):
    d = math.sqrt(4 * P / (math.pi * st))
    d = math.ceil(d)
    t = max(round(0.31 * d), 1)
    # spigot: tension across the slot
    d2 = d
    while math.pi / 4 * d2 ** 2 - d2 * t < P / st:
        d2 += 1
    while P / (d2 * t) > sc:
        t += 1
    d1 = d2
    while math.pi / 4 * (d1 ** 2 - d2 ** 2) - (d1 - d2) * t < P / st:
        d1 += 1
    b = math.ceil(P / (2 * t * tau))
    a = math.ceil(P / (2 * d2 * tau))
    c = math.ceil(P / (2 * (d1 - d2) * tau)) if d1 > d2 else a
    d3 = d2 + 1
    while P / ((d3 - d2) * t) > sc:
        d3 += 1
    return {"Rod diameter d": R(d, "mm"), "Spigot diameter d2": R(d2, "mm"), "Socket outer d1": R(d1, "mm"),
            "Spigot collar d3": R(d3, "mm"), "Cotter thickness t": R(t, "mm"), "Cotter width b": R(b, "mm"),
            "Spigot end a": R(a, "mm"), "Socket end c": R(c, "mm"), "Taper": R("1 : 24 to 1 : 48")}


@calc("knuckle_joint", "Knuckle joint (pin)", "Keys, pins and cotters", [
    In("P", "Axial load", 30000.0, "N", minimum=0.0), In("tau", "Allowable shear (pin)", 60.0, "MPa", minimum=1.0),
    In("sb", "Allowable bending (pin)", 100.0, "MPa", minimum=1.0),
    In("sc", "Allowable bearing", 90.0, "MPa", minimum=1.0), In("st", "Allowable tension (rod)", 80.0, "MPa", minimum=1.0),
], "Pin in double shear, bending with the eye/fork proportions (eye t = 1.25 d, fork t1 = 0.75 d).")
def knuckle_joint(P, tau, sb, sc, st):
    D = math.ceil(math.sqrt(4 * P / (math.pi * st)))
    d = math.sqrt(2 * P / (math.pi * tau))
    d = max(d, D)
    while True:
        t, t1 = 1.25 * d, 0.75 * d
        M = P / 2 * (t / 3 + t1 / 2) - P / 2 * t / 4          # standard bending moment on the pin
        if 32 * M / (math.pi * d ** 3) <= sb and P / (d * t) <= sc:
            break
        d += 0.5
    d = math.ceil(d)
    return {"Rod diameter D": R(D, "mm"), "Pin diameter d": R(d, "mm"), "Eye thickness t": R(1.25 * d, "mm"),
            "Fork thickness t1": R(0.75 * d, "mm"), "Eye / fork outer diameter": R(2 * d, "mm"),
            "Pin head diameter": R(1.5 * d, "mm"), "Pin shear stress": R(2 * P / (math.pi * d ** 2), "MPa")}


# ===================================================================== 12 Gears ===

FORM = {"20° full depth": (0.154, 0.912), "14.5° full depth": (0.124, 0.684), "20° stub": (0.175, 0.841)}
MODULES = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16, 20, 25, 32, 40, 50]


@calc("spur_gear", "Spur gear (Lewis + Buckingham)", "Gears", [
    In("power", "Power", 10.0, "kW", minimum=0.0), In("rpm", "Pinion speed", 1000.0, "rpm", minimum=0.1),
    In("zp", "Pinion teeth", 20.0, minimum=8.0), In("ratio", "Ratio", 3.0, minimum=1.0),
    In("module", "Module (0 = choose)", 0.0, "mm", minimum=0.0),
    In("face_k", "Face width b = k·m", 10.0, minimum=4.0),
    In("form", "Tooth form", "20° full depth", choices=list(FORM)),
    In("s0", "Allowable static bending stress (weaker gear)", 140.0, "MPa", minimum=1.0),
    In("cs", "Service factor", 1.25, minimum=1.0),
    In("bhn", "Surface hardness (BHN)", 300.0, minimum=100.0), In("E", "E (both)", 207.0, "GPa", minimum=1.0),
    In("error", "Tooth error e", 0.025, "mm", minimum=0.0),
], "Lewis beam strength with Barth velocity factor; Buckingham dynamic load and wear (Q, K) check. Picks the "
   "smallest standard module that passes when module = 0.")
def spur_gear(power, rpm, zp, ratio, module, face_k, form, s0, cs, bhn, E, error):
    a, b_ = FORM[form]
    zg = round(zp * ratio)
    yp = a - b_ / zp
    T = power * 1e3 * 60 / (2 * math.pi * rpm)

    def check(m):
        dp = m * zp
        v = math.pi * dp * rpm / 60000
        Cv = 3.05 / (3.05 + v) if v <= 10 else (6.1 / (6.1 + v) if v <= 20 else 5.55 / (5.55 + math.sqrt(v)))
        Ft = T * 1e3 / (dp / 2)
        b = face_k * m
        Fs = s0 * b * math.pi * yp * m                       # Lewis beam strength σ b π m y
        Q = 2 * zg / (zp + zg)
        K = 0.16 * (bhn / 100) ** 2                          # load-stress factor, MPa
        Fw = dp * b * Q * K                                  # Buckingham wear strength
        C = {"14.5° full depth": 11000, "20° full depth": 11400, "20° stub": 11800}[form] * error * (E / 207)
        Fd = Ft + 21 * v * (b * C + Ft) / (21 * v + math.sqrt(b * C + Ft)) if v > 0 else Ft
        return dict(m=m, dp=dp, v=v, Cv=Cv, Ft=Ft, b=b, Fs=Fs, Fw=Fw, Fd=Fd)
    if module:
        r = check(module)
    else:
        r = None
        for m in MODULES:
            r = check(m)
            if r["Fs"] * r["Cv"] >= cs * r["Ft"] and r["Fw"] >= r["Fd"] and r["Fs"] >= r["Fd"]:
                break
    ok_b = r["Fs"] * r["Cv"] >= cs * r["Ft"]
    return {"Torque on pinion": R(T, "N·m"), "Gear teeth": R(zg), "Module": R(r["m"], "mm"),
            "Pitch diameters": R(f"{r['dp']:.1f} / {r['m'] * zg:.1f}", "mm"),
            "Centre distance": R(r["m"] * (zp + zg) / 2, "mm"), "Face width": R(r["b"], "mm"),
            "Pitch-line velocity": R(r["v"], "m/s"), "Lewis form factor y": R(yp),
            "Barth velocity factor Cv": R(r["Cv"]), "Tangential load Ft": R(r["Ft"], "N"),
            "Beam strength Fs": R(r["Fs"], "N", "OK with Cv and service factor" if ok_b else "too weak"),
            "Dynamic load Fd (Buckingham)": R(r["Fd"], "N"),
            "Wear strength Fw": R(r["Fw"], "N", "OK" if r["Fw"] >= r["Fd"] else "wear-limited: harden or widen"),
            "Bending FoS (Fs·Cv / (cs·Ft))": R(r["Fs"] * r["Cv"] / (cs * r["Ft"]))}


@calc("helical_gear", "Helical gear geometry & forces", "Gears", [
    In("mn", "Normal module", 3.0, "mm", minimum=0.1), In("z", "Teeth", 24.0, minimum=6.0),
    In("beta", "Helix angle β", 20.0, "°", minimum=0.0), In("an", "Normal pressure angle", 20.0, "°"),
    In("power", "Power", 10.0, "kW", minimum=0.0), In("rpm", "Speed", 1000.0, "rpm", minimum=0.1),
    In("herringbone", "Herringbone", False),
], "Transverse module, pitch diameter, virtual (formative) teeth, face-width limits, and the tangential, radial and "
   "axial (thrust) forces.")
def helical_gear(mn, z, beta, an, power, rpm, herringbone):
    bt = math.radians(beta)
    _ok(beta < 45, "Helix angle should be below 45°")
    m = mn / math.cos(bt)
    d = m * z
    zv = z / math.cos(bt) ** 3
    at = math.degrees(math.atan(math.tan(math.radians(an)) / math.cos(bt)))
    T = power * 1e3 * 60 / (2 * math.pi * rpm)
    Ft = T * 1e3 / (d / 2)
    Fr = Ft * math.tan(math.radians(at))
    Fa = 0.0 if herringbone else Ft * math.tan(bt)
    if beta > 0:
        bmin = (2.3 if herringbone else 1.15) * math.pi * mn / math.sin(bt)
        bmax = (30 if herringbone else 20) * mn / math.sin(bt)
    else:
        bmin, bmax = 8 * mn, 12 * mn
    return {"Transverse module": R(m, "mm"), "Pitch diameter": R(d, "mm"), "Virtual teeth zv": R(zv),
            "Transverse pressure angle": R(at, "°"), "Torque": R(T, "N·m"), "Tangential force": R(Ft, "N"),
            "Radial force": R(Fr, "N"), "Axial thrust": R(Fa, "N", "cancels in herringbone" if herringbone else ""),
            "Face width (min, AGMA)": R(bmin, "mm"), "Face width (max)": R(bmax, "mm"),
            "Typical face width": R(f"{(20 if herringbone else 12.5) * mn:.0f} – {(30 if herringbone else 20) * mn:.0f}",
                                    "mm")}


@calc("worm_gear", "Worm gear efficiency & forces", "Gears", [
    In("m", "Axial module", 5.0, "mm", minimum=0.1), In("starts", "Worm starts", 2.0, minimum=1.0),
    In("zg", "Wheel teeth", 40.0, minimum=10.0), In("q", "Diameter factor q = d/m", 10.0, minimum=5.0),
    In("an", "Normal pressure angle", 20.0, "°"), In("mu", "Friction coefficient", 0.05, minimum=0.0),
    In("power", "Input power", 3.0, "kW", minimum=0.0), In("rpm", "Worm speed", 1440.0, "rpm", minimum=0.1),
], "Lead angle, efficiency, self-locking check and the three force components on the worm.")
def worm_gear(m, starts, zg, q, an, mu, power, rpm):
    d1 = q * m
    d2 = zg * m
    lam = math.atan(starts / q)
    a = math.radians(an)
    eff = (math.cos(a) - mu * math.tan(lam)) / (math.cos(a) + mu / math.tan(lam))
    T1 = power * 1e3 * 60 / (2 * math.pi * rpm)
    Fw_t = T1 * 1e3 / (d1 / 2)                                     # worm tangential = wheel axial
    Fg_t = Fw_t * (math.cos(a) * math.cos(lam) - mu * math.sin(lam)) / (math.cos(a) * math.sin(lam) + mu *
                                                                        math.cos(lam))
    Fr = Fg_t * math.sin(a) / (math.cos(a) * math.cos(lam) - mu * math.sin(lam))
    return {"Ratio": R(zg / starts), "Worm / wheel pitch diameters": R(f"{d1:.1f} / {d2:.1f}", "mm"),
            "Centre distance": R((d1 + d2) / 2, "mm"), "Lead angle": R(math.degrees(lam), "°"),
            "Efficiency": R(eff * 100, "%"),
            "Self-locking": R("yes" if mu > math.tan(lam) * math.cos(a) else "no", "",
                              "μ > tan λ · cos αn"),
            "Worm tangential (= wheel axial)": R(Fw_t, "N"), "Wheel tangential (= worm axial)": R(Fg_t, "N"),
            "Separating (radial) force": R(Fr, "N"), "Output speed": R(rpm * starts / zg, "rpm"),
            "Output torque": R(T1 * eff * zg / starts, "N·m")}


# ========================================================= 13 Couplings, brakes ===

@calc("flange_coupling", "Rigid flange coupling", "Couplings, clutches and brakes", [
    In("d", "Shaft diameter", 50.0, "mm", minimum=5.0), In("T", "Torque", 900.0, "N·m", minimum=0.0),
    In("tau_b", "Allowable bolt shear", 40.0, "MPa", minimum=1.0), In("sc_b", "Allowable bolt crushing", 80.0, "MPa",
                                                                    minimum=1.0),
    In("tau_h", "Allowable shear in cast-iron hub / flange", 15.0, "MPa", minimum=1.0),
], "Standard proportions (hub 2d, bolt circle 3d, flange OD 4d, flange t = 0.5d), bolt count and size from shear, "
   "and hub / flange checks.")
def flange_coupling(d, T, tau_b, sc_b, tau_h):
    Tn = T * 1e3
    n = 3 if d <= 40 else (4 if d <= 100 else (6 if d <= 180 else 8))
    D, D1, D2, t, L = 2 * d, 3 * d, 4 * d, 0.5 * d, 1.5 * d
    db = math.sqrt(8 * Tn / (math.pi * n * tau_b * D1))
    M = [5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 42]
    dbs = next((x for x in M if x >= db), 48)
    crush = 2 * Tn / (n * dbs * t * D1)
    hub = 16 * Tn * D / (math.pi * (D ** 4 - d ** 4))
    fl = 2 * Tn / (math.pi * D ** 2 * t)
    return {"Hub diameter": R(D, "mm"), "Hub length": R(L, "mm"), "Bolt circle": R(D1, "mm"),
            "Flange OD": R(D2, "mm"), "Flange thickness": R(t, "mm"), "Number of bolts": R(n),
            "Bolt diameter (calc)": R(db, "mm"), "Bolt size": R(f"M{dbs}"),
            "Bolt crushing stress": R(crush, "MPa", "OK" if crush <= sc_b else "too high — more / bigger bolts"),
            "Hub shear stress": R(hub, "MPa", "OK" if hub <= tau_h else "too high"),
            "Flange shear at the hub": R(fl, "MPa", "OK" if fl <= tau_h else "too high")}


@calc("muff_coupling", "Muff (sleeve) coupling", "Couplings, clutches and brakes", [
    In("d", "Shaft diameter", 40.0, "mm", minimum=5.0), In("T", "Torque", 500.0, "N·m", minimum=0.0),
    In("tau_s", "Allowable shear in the sleeve", 15.0, "MPa", minimum=1.0),
], "Sleeve D = 2d + 13 mm, L = 3.5d, checked as a hollow shaft in torsion.")
def muff_coupling(d, T, tau_s):
    D, L = 2 * d + 13, 3.5 * d
    tau = 16 * T * 1e3 * D / (math.pi * (D ** 4 - d ** 4))
    return {"Sleeve OD": R(D, "mm"), "Sleeve length": R(L, "mm"),
            "Sleeve shear stress": R(tau, "MPa", "OK" if tau <= tau_s else "too high")}


@calc("plate_clutch", "Plate / disc clutch", "Couplings, clutches and brakes", [
    In("Do", "Outer friction diameter", 250.0, "mm", minimum=1.0), In("Di", "Inner friction diameter", 150.0, "mm",
                                                                      minimum=0.0),
    In("mu", "Friction coefficient", 0.3, minimum=0.01), In("pairs", "Friction surfaces (pairs)", 2.0, minimum=1.0),
    In("theory", "Theory", "uniform wear", choices=["uniform wear", "uniform pressure"]),
    In("F", "Axial spring force (0 = from torque)", 4000.0, "N", minimum=0.0),
    In("T", "Torque to carry (if F = 0)", 0.0, "N·m", minimum=0.0), In("pmax", "Allowable pressure", 0.25, "MPa",
                                                                      minimum=0.0),
], "Friction torque by uniform wear (worn clutches) or uniform pressure (new); force needed for a torque.")
def plate_clutch(Do, Di, mu, pairs, theory, F, T, pmax):
    _ok(Do > Di, "Outer diameter must exceed inner")
    ro, ri = Do / 2, Di / 2
    rm = (ro + ri) / 2 if theory == "uniform wear" else 2 / 3 * (ro ** 3 - ri ** 3) / (ro ** 2 - ri ** 2)
    if not F:
        _ok(T > 0, "Give the axial force or the torque")
        F = T * 1e3 / (mu * pairs * rm)
    Tq = mu * F * rm * pairs / 1e3
    p = F / (2 * math.pi * ri * (ro - ri)) if theory == "uniform wear" and ri > 0 else F / (math.pi * (ro ** 2 - ri ** 2))
    return {"Mean friction radius": R(rm, "mm"), "Axial force": R(F, "N"), "Torque capacity": R(Tq, "N·m"),
            "Max pressure": R(p, "MPa", "OK" if p <= pmax or not pmax else "over the allowable"),
            "Best Di/Do for max torque (uniform wear)": R(0.577)}


@calc("cone_clutch", "Cone clutch", "Couplings, clutches and brakes", [
    In("Dm", "Mean cone diameter", 200.0, "mm", minimum=1.0), In("b", "Face width", 50.0, "mm", minimum=0.1),
    In("alpha", "Semi-cone angle", 12.5, "°", minimum=1.0), In("mu", "Friction coefficient", 0.25, minimum=0.01),
    In("T", "Torque", 300.0, "N·m", minimum=0.0),
], "Axial engaging force and pressure for a torque (uniform wear).")
def cone_clutch(Dm, b, alpha, mu, T):
    a = math.radians(alpha)
    Fn = T * 1e3 / (mu * Dm / 2)
    Fa = Fn * math.sin(a)
    Fengage = Fn * (math.sin(a) + mu * math.cos(a))
    p = Fn / (math.pi * Dm * b)
    return {"Normal force": R(Fn, "N"), "Axial force to hold": R(Fa, "N"),
            "Axial force to engage": R(Fengage, "N"), "Pressure on lining": R(p, "MPa")}


@calc("band_brake", "Band brake", "Couplings, clutches and brakes", [
    In("D", "Drum diameter", 400.0, "mm", minimum=1.0), In("theta", "Angle of wrap", 270.0, "°", minimum=1.0),
    In("mu", "Friction coefficient", 0.25, minimum=0.01), In("T", "Braking torque", 400.0, "N·m", minimum=0.0),
    In("kind", "Type", "simple", choices=["simple", "differential", "band & block"]),
    In("a", "Lever: tight-side arm a", 50.0, "mm", minimum=0.0), In("b_arm", "Lever: slack-side arm b", 0.0, "mm",
                                                                   minimum=0.0),
    In("L", "Lever length to the effort", 500.0, "mm", minimum=1.0),
    In("t", "Band thickness", 3.0, "mm", minimum=0.1), In("st", "Allowable band tension stress", 70.0, "MPa", minimum=1.0),
], "T1/T2 = e^(μθ); band tensions, lever effort, and band width.")
def band_brake(D, theta, mu, T, kind, a, b_arm, L, t, st):
    r = math.exp(mu * math.radians(theta))
    dT = T * 1e3 / (D / 2)
    T2 = dT / (r - 1)
    T1 = T2 * r
    if kind == "differential":
        P = (T2 * b_arm - T1 * a) / L                    # both band ends on the lever
    else:
        P = T2 * a / L                                   # slack end on the lever at a, tight end at the pivot
    width = T1 / (st * t)
    return {"Tension ratio e^μθ": R(r), "Tight-side tension T1": R(T1, "N"), "Slack-side tension T2": R(T2, "N"),
            "Lever effort P": R(P, "N", "≤ 0: self-locking" if P <= 0 else ""),
            "Band width": R(width, "mm")}


@calc("block_brake", "Single block (shoe) brake", "Couplings, clutches and brakes", [
    In("D", "Drum diameter", 300.0, "mm", minimum=1.0), In("T", "Braking torque", 200.0, "N·m", minimum=0.0),
    In("mu", "Friction coefficient", 0.3, minimum=0.01), In("arc", "Shoe contact angle", 60.0, "°", minimum=1.0),
    In("x", "Pivot to shoe (along the lever)", 200.0, "mm", minimum=0.0),
    In("L", "Pivot to effort", 600.0, "mm", minimum=1.0),
    In("e", "Pivot offset from the friction line (+ = helps)", 30.0, "mm"),
    In("w", "Shoe width", 50.0, "mm", minimum=1.0),
], "Effort on the lever for a torque; long shoes use the equivalent friction coefficient μ' = 4μ sin(θ/2)/(θ + sin θ).")
def block_brake(D, T, mu, arc, x, L, e, w):
    th = math.radians(arc)
    mu_e = 4 * mu * math.sin(th / 2) / (th + math.sin(th)) if arc > 60 else mu
    Ff = T * 1e3 / (D / 2)
    Rn = Ff / mu_e
    P = (Rn * x - Ff * e) / L
    area = D * math.sin(th / 2) * w
    return {"Equivalent μ'": R(mu_e), "Friction force": R(Ff, "N"), "Normal force": R(Rn, "N"),
            "Lever effort": R(P, "N", "self-energising / self-locking" if P <= 0 else ""),
            "Bearing pressure": R(Rn / area, "MPa", "projected shoe area")}


@calc("brake_heat", "Brake energy & temperature rise", "Couplings, clutches and brakes", [
    In("m", "Moving mass", 1000.0, "kg", minimum=0.0), In("v1", "Speed before", 15.0, "m/s", minimum=0.0),
    In("v2", "Speed after", 0.0, "m/s", minimum=0.0), In("I", "Rotating inertia (0 = none)", 0.0, "kg·m²",
                                                        minimum=0.0),
    In("w1", "Angular speed before", 0.0, "rpm", minimum=0.0), In("mb", "Drum / disc mass", 15.0, "kg", minimum=0.01),
    In("c", "Specific heat", 460.0, "J/kg·K", minimum=1.0),
], "Energy absorbed and the temperature rise of the drum (one stop, no cooling).")
def brake_heat(m, v1, v2, I, w1, mb, c):
    E = 0.5 * m * (v1 ** 2 - v2 ** 2) + 0.5 * I * (w1 * 2 * math.pi / 60) ** 2
    return {"Energy absorbed": R(E / 1e3, "kJ"), "Temperature rise": R(E / (mb * c), "K")}


# =================================================================== Appendices ===

@calc("section_properties", "Section properties", "Appendix", [
    In("shape", "Section", "rectangle", choices=["rectangle", "hollow rectangle", "circle", "hollow circle",
                                                "I-section", "channel"]),
    In("b", "Width b / D", 50.0, "mm", minimum=0.01), In("h", "Depth h / d (bore)", 100.0, "mm", minimum=0.0),
    In("t", "Wall / web thickness", 5.0, "mm", minimum=0.0), In("tf", "Flange thickness", 8.0, "mm", minimum=0.0),
], "Area, centroid, second moments, section moduli and radii of gyration.")
def section_properties(shape, b, h, t, tf):
    if shape == "rectangle":
        A, Ix, Iy, cy, cx = b * h, b * h ** 3 / 12, h * b ** 3 / 12, h / 2, b / 2
    elif shape == "hollow rectangle":
        _ok(2 * t < min(b, h), "Wall too thick")
        bi, hi = b - 2 * t, h - 2 * t
        A, Ix, Iy, cy, cx = b * h - bi * hi, (b * h ** 3 - bi * hi ** 3) / 12, (h * b ** 3 - hi * bi ** 3) / 12, h / 2, b / 2
    elif shape in ("circle", "hollow circle"):
        D, d = b, (h if shape == "hollow circle" else 0.0)
        _ok(d < D, "Bore must be smaller than D")
        A = math.pi / 4 * (D ** 2 - d ** 2)
        Ix = Iy = math.pi / 64 * (D ** 4 - d ** 4)
        cy = cx = D / 2
    elif shape == "I-section":
        _ok(2 * tf < h and t < b, "Check the thicknesses")
        hw = h - 2 * tf
        A = 2 * b * tf + hw * t
        Ix = (b * h ** 3 - (b - t) * hw ** 3) / 12
        Iy = (2 * tf * b ** 3 + hw * t ** 3) / 12
        cy, cx = h / 2, b / 2
    else:                                                    # channel, web vertical on the left
        _ok(2 * tf < h and t < b, "Check the thicknesses")
        hw = h - 2 * tf
        A = 2 * b * tf + hw * t
        Ix = (b * h ** 3 - (b - t) * hw ** 3) / 12
        cx = (2 * b * tf * b / 2 + hw * t * t / 2) / A
        Iy = 2 * (tf * b ** 3 / 12 + b * tf * (b / 2 - cx) ** 2) + hw * t ** 3 / 12 + hw * t * (t / 2 - cx) ** 2
        cy = h / 2
    return {"Area": R(A, "mm²"), "Ixx": R(Ix, "mm⁴"), "Iyy": R(Iy, "mm⁴"),
            "Zxx = I/c": R(Ix / cy, "mm³"), "Zyy": R(Iy / max(cx, (b - cx) if shape == "channel" else cx), "mm³"),
            "kxx": R(math.sqrt(Ix / A), "mm"), "kyy": R(math.sqrt(Iy / A), "mm"),
            **({"Centroid from the web back": R(cx, "mm")} if shape == "channel" else {}),
            **({"Polar J": R(Ix + Iy, "mm⁴")} if "circle" in shape else {})}


@calc("thick_cylinder", "Pressure cylinders (thin / Lamé / Barlow / Clavarino)", "Appendix", [
    In("di", "Inner diameter", 100.0, "mm", minimum=0.1), In("p", "Internal pressure", 20.0, "MPa", minimum=0.0),
    In("s", "Allowable stress", 120.0, "MPa", minimum=1.0), In("nu", "Poisson's ratio", 0.3),
    In("closed", "Closed ends", True),
], "Wall thickness by thin-shell, Lamé (brittle), Clavarino (ductile, closed) and Barlow; hoop/radial stresses.",
    material={"nu": "nu"})
def thick_cylinder(di, p, s, nu, closed):
    ri = di / 2
    t_thin = p * di / (2 * s)
    _ok(p < s, "Pressure must be below the allowable stress for Lamé")
    t_lame = ri * (math.sqrt((s + p) / (s - p)) - 1)
    if closed:
        num, den = s + (1 - 2 * nu) * p, s - (1 + nu) * p
    else:
        num, den = s + (1 - nu) * p, s - (1 + nu) * p
    t_clav = ri * (math.sqrt(num / den) - 1) if den > 0 else float("inf")
    t_bar = p * di / (2 * s - p) if 2 * s > p else float("inf")
    ro = ri + t_lame
    hoop_i = p * (ro ** 2 + ri ** 2) / (ro ** 2 - ri ** 2)
    return {"Thin-shell t": R(t_thin, "mm", "valid for t < di/20"), "Lamé t": R(t_lame, "mm", "brittle"),
            "Clavarino t": R(t_clav, "mm", "ductile, " + ("closed" if closed else "open") + " ends"),
            "Barlow t": R(t_bar, "mm", "high-pressure tubes"),
            "Hoop stress at bore (Lamé t)": R(hoop_i, "MPa"), "Radial stress at bore": R(-p, "MPa"),
            "Axial stress (closed)": R(p * ri ** 2 / (ro ** 2 - ri ** 2) if closed else 0.0, "MPa")}


@calc("power_torque", "Power ⇄ torque ⇄ speed", "Appendix", [
    In("power", "Power (0 = solve)", 7.5, "kW", minimum=0.0), In("T", "Torque (0 = solve)", 0.0, "N·m", minimum=0.0),
    In("rpm", "Speed (0 = solve)", 1440.0, "rpm", minimum=0.0),
], "P = 2π N T / 60 — give any two.")
def power_torque(power, T, rpm):
    given = sum(1 for x in (power, T, rpm) if x)
    _ok(given >= 2, "Give two of power, torque and speed")
    if not T:
        T = power * 1e3 * 60 / (2 * math.pi * rpm)
    elif not power:
        power = 2 * math.pi * rpm * T / 60 / 1e3
    elif not rpm:
        rpm = power * 1e3 * 60 / (2 * math.pi * T)
    return {"Power": R(power, "kW"), "Power (hp)": R(power / 0.7457, "hp"), "Torque": R(T, "N·m"),
            "Torque (lbf·ft)": R(T / 1.3558, "lbf·ft"), "Speed": R(rpm, "rpm"),
            "Angular speed": R(rpm * 2 * math.pi / 60, "rad/s")}


# --------------------------------------------------------------------- helpers ---

def chapters() -> dict:
    out = {}
    for c in REGISTRY.values():
        out.setdefault(c.chapter, []).append(c)
    return out


def run(calc_id: str, values: dict | None = None) -> dict:
    if calc_id not in REGISTRY:
        raise CalcError(f"No calculator '{calc_id}'. Calculators: {sorted(REGISTRY)}")
    return REGISTRY[calc_id].run(values)


def prefill_from_material(calc_id: str, material) -> dict:
    """Input values taken from a Material (E in GPa, strengths in MPa, density g/cm³)."""
    c = REGISTRY[calc_id]
    out = {}
    for inp, attr in c.material_inputs.items():
        v = getattr(material, attr, None)
        if v:
            out[inp] = float(v)
    return out


def fmt_value(v) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        a = abs(v)
        if a and (a >= 1e6 or a < 1e-3):
            return f"{v:.4g}"
        return f"{v:.4g}" if a < 100 else f"{v:,.1f}"
    return str(v)


def report(calc_id: str, values: dict, result: dict) -> str:
    c = REGISTRY[calc_id]
    lines = [c.title, "=" * len(c.title), "", "Inputs:"]
    for i in c.inputs:
        lines.append(f"  {i.label}: {fmt_value(values.get(i.name, i.default))} {i.unit}".rstrip())
    lines += ["", "Results:"]
    for k, r in result.items():
        lines.append(f"  {k}: {fmt_value(r['value'])} {r['unit']}".rstrip() + (f"   ({r['note']})" if r["note"] else ""))
    return "\n".join(lines)
