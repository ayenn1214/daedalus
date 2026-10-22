"""Units: measurement types (quantities), SI and US customary units, construction formats, unit systems.

Everything inside Daedalus is stored in base units (mm, deg, kg, N, MPa, s, °C …). Units only change what you
see and what you can type: every field accepts '1/2"', '3 ft 4-1/2 in', "5'-6 1/4\"", '25.4 mm + 2', '2 m'.
"""
from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ quantities ---
# quantity -> (label, base unit, {unit: factor to base}) ; value_base = value_unit * factor
IN, FT, LBM, LBF = 25.4, 304.8, 0.45359237, 4.4482216152605
G0 = 9806.65                                    # mm/s²

Q: dict[str, tuple[str, str, dict[str, float]]] = {
    "length": ("Length", "mm", {
        "µm": 1e-3, "mm": 1.0, "cm": 10.0, "m": 1000.0, "km": 1e6,
        "mil": IN / 1000, "in": IN, "ft": FT, "yd": 3 * FT, "mi": 5280 * FT}),
    "angle": ("Angle", "deg", {
        "deg": 1.0, "rad": 180 / math.pi, "grad": 0.9, "arcmin": 1 / 60, "arcsec": 1 / 3600, "rev": 360.0}),
    "slope": ("Slope / pitch", "deg", {          # non-linear units handled in _SPECIAL
        "deg": 1.0, "%": None, "in/ft": None, "1:n": None, "rise:run": None}),
    "area": ("Area", "mm²", {
        "mm²": 1.0, "cm²": 100.0, "m²": 1e6, "ha": 1e10, "km²": 1e12,
        "in²": IN ** 2, "ft²": FT ** 2, "yd²": (3 * FT) ** 2, "acre": 43560 * FT ** 2, "square": 100 * FT ** 2}),
    "volume": ("Volume", "mm³", {
        "mm³": 1.0, "cm³": 1e3, "mL": 1e3, "L": 1e6, "m³": 1e9,
        "in³": IN ** 3, "ft³": FT ** 3, "yd³": (3 * FT) ** 3, "gal": 231 * IN ** 3, "qt": 231 / 4 * IN ** 3,
        "fl oz": 231 / 128 * IN ** 3, "bf": 144 * IN ** 3}),          # bf = board foot
    "mass": ("Mass", "kg", {
        "mg": 1e-6, "g": 1e-3, "kg": 1.0, "t": 1000.0,
        "oz": LBM / 16, "lb": LBM, "slug": 14.593902937, "ton": 2000 * LBM, "long ton": 2240 * LBM}),
    "density": ("Density", "g/cm³", {
        "g/cm³": 1.0, "kg/m³": 1e-3, "kg/L": 1.0, "lb/in³": LBM * 1000 / (IN / 10) ** 3,
        "lb/ft³": LBM * 1000 / (FT / 10) ** 3}),
    "linear_density": ("Mass per length", "kg/m", {
        "kg/m": 1.0, "g/mm": 1.0, "lb/ft": LBM / 0.3048, "lb/in": LBM / 0.0254}),
    "force": ("Force", "N", {
        "dyn": 1e-5, "N": 1.0, "kN": 1e3, "MN": 1e6, "kgf": 9.80665,
        "ozf": LBF / 16, "lbf": LBF, "kip": 1000 * LBF, "tonf": 2000 * LBF}),
    "torque": ("Moment / torque", "N·m", {
        "N·mm": 1e-3, "N·m": 1.0, "kN·m": 1e3, "kgf·m": 9.80665,
        "ozf·in": LBF / 16 * 0.0254, "lbf·in": LBF * 0.0254, "lbf·ft": LBF * 0.3048, "kip·ft": 1000 * LBF * 0.3048,
        "kip·in": 1000 * LBF * 0.0254}),
    "pressure": ("Pressure / stress", "MPa", {
        "Pa": 1e-6, "kPa": 1e-3, "MPa": 1.0, "GPa": 1e3, "N/mm²": 1.0, "bar": 0.1, "atm": 0.101325,
        "psi": LBF / IN ** 2, "ksi": 1000 * LBF / IN ** 2, "psf": LBF / FT ** 2, "ksf": 1000 * LBF / FT ** 2}),
    "linear_load": ("Line load", "N/mm", {
        "N/mm": 1.0, "N/m": 1e-3, "kN/m": 1.0, "lbf/in": LBF / IN, "plf": LBF / FT, "klf": 1000 * LBF / FT}),
    "area_load": ("Area load", "kPa", {
        "Pa": 1e-3, "kPa": 1.0, "kN/m²": 1.0, "psf": LBF / FT ** 2 * 1e3, "ksf": LBF / FT ** 2 * 1e6,
        "psi": LBF / IN ** 2 * 1e3}),
    "stiffness": ("Stiffness", "N/mm", {
        "N/mm": 1.0, "N/m": 1e-3, "kN/m": 1.0, "kN/mm": 1e3, "lbf/in": LBF / IN, "lbf/ft": LBF / FT}),
    "time": ("Time", "s", {"ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "day": 86400.0}),
    "frequency": ("Frequency", "Hz", {"Hz": 1.0, "kHz": 1e3, "rpm": 1 / 60, "cpm": 1 / 60}),
    "angular_velocity": ("Angular velocity", "rpm", {
        "rpm": 1.0, "rad/s": 60 / (2 * math.pi), "deg/s": 1 / 6}),
    "velocity": ("Velocity", "mm/s", {
        "mm/s": 1.0, "m/s": 1e3, "m/min": 1e3 / 60, "km/h": 1e6 / 3600,
        "in/s": IN, "in/min": IN / 60, "ft/s": FT, "ft/min": FT / 60, "mph": 5280 * FT / 3600}),
    "acceleration": ("Acceleration", "mm/s²", {
        "mm/s²": 1.0, "m/s²": 1e3, "in/s²": IN, "ft/s²": FT, "g": G0}),
    "energy": ("Energy", "J", {
        "mJ": 1e-3, "J": 1.0, "kJ": 1e3, "MJ": 1e6, "Wh": 3600.0, "kWh": 3.6e6, "cal": 4.184, "kcal": 4184.0,
        "in·lbf": LBF * 0.0254, "ft·lbf": LBF * 0.3048, "Btu": 1055.05585}),
    "power": ("Power", "W", {
        "mW": 1e-3, "W": 1.0, "kW": 1e3, "MW": 1e6, "hp": 745.69987, "PS": 735.49875, "Btu/h": 0.29307107,
        "ft·lbf/s": LBF * 0.3048}),
    "temperature": ("Temperature", "°C", {"°C": None, "K": None, "°F": None, "°R": None}),
    "temp_delta": ("Temperature difference", "K", {"K": 1.0, "°C": 1.0, "°F": 5 / 9, "°R": 5 / 9}),
    "area_moment": ("Second moment of area", "mm⁴", {
        "mm⁴": 1.0, "cm⁴": 1e4, "m⁴": 1e12, "in⁴": IN ** 4, "ft⁴": FT ** 4}),
    "section_modulus": ("Section modulus", "mm³", {
        "mm³": 1.0, "cm³": 1e3, "m³": 1e9, "in³": IN ** 3}),
    "mass_moment": ("Mass moment of inertia", "kg·mm²", {
        "g·mm²": 1e-3, "kg·mm²": 1.0, "kg·m²": 1e6, "lb·in²": LBM * IN ** 2, "lb·ft²": LBM * FT ** 2,
        "slug·ft²": 14.593902937 * FT ** 2}),
    "flow": ("Volume flow", "L/min", {
        "mL/s": 0.06, "L/s": 60.0, "L/min": 1.0, "m³/h": 1000 / 60, "gpm": 3.785411784, "cfm": 28.316846592}),
    "thermal_conductivity": ("Thermal conductivity", "W/(m·K)", {
        "W/(m·K)": 1.0, "Btu/(h·ft·°F)": 1.730734666}),
    "energy_density": ("Specific energy", "J/kg", {"J/kg": 1.0, "kJ/kg": 1e3, "Btu/lb": 2326.0}),
    "electric_current": ("Current", "A", {"mA": 1e-3, "A": 1.0}),
    "voltage": ("Voltage", "V", {"mV": 1e-3, "V": 1.0, "kV": 1e3}),
    "ratio": ("Ratio", "", {"": 1.0, "%": 0.01}),
}

ALIASES = {
    "um": "µm", "micron": "µm", "millimeter": "mm", "millimetre": "mm", "centimeter": "cm", "meter": "m",
    "metre": "m", "meters": "m", "kilometer": "km", "inch": "in", "inches": "in", "\"": "in", "''": "in",
    "foot": "ft", "feet": "ft", "'": "ft", "yard": "yd", "yards": "yd", "mile": "mi", "thou": "mil",
    "°": "deg", "degree": "deg", "degrees": "deg", "radian": "rad", "radians": "rad",
    "mm2": "mm²", "cm2": "cm²", "m2": "m²", "in2": "in²", "ft2": "ft²", "sqft": "ft²", "sq ft": "ft²",
    "sf": "ft²", "sqin": "in²", "yd2": "yd²", "sy": "yd²",
    "mm3": "mm³", "cm3": "cm³", "cc": "cm³", "m3": "m³", "in3": "in³", "ft3": "ft³", "yd3": "yd³", "cy": "yd³",
    "l": "L", "ml": "mL", "liter": "L", "litre": "L", "gallon": "gal", "board feet": "bf", "fbm": "bf",
    "lbs": "lb", "lbm": "lb", "pound": "lb", "pounds": "lb", "tonne": "t", "kilogram": "kg",
    "g/cc": "g/cm³", "g/cm3": "g/cm³", "kg/m3": "kg/m³", "lb/in3": "lb/in³", "lb/ft3": "lb/ft³", "pcf": "lb/ft³",
    "kips": "kip", "k": "kip", "lb-f": "lbf", "newton": "N", "kn": "kN",
    "nm": "N·m", "n·m": "N·m", "n.m": "N·m", "n-m": "N·m", "nmm": "N·mm", "n·mm": "N·mm", "n-mm": "N·mm",
    "kn·m": "kN·m", "knm": "kN·m", "kn-m": "kN·m",
    "lbf-in": "lbf·in", "in-lb": "lbf·in", "in·lb": "lbf·in", "in-lbf": "lbf·in", "lb-in": "lbf·in",
    "lbf-ft": "lbf·ft", "ft-lb": "lbf·ft", "ft·lb": "lbf·ft", "ft-lbf": "lbf·ft", "lb-ft": "lbf·ft",
    "kip-ft": "kip·ft", "k-ft": "kip·ft", "kip-in": "kip·in",
    "n/mm2": "N/mm²", "mpa": "MPa", "kpa": "kPa", "gpa": "GPa", "pa": "Pa",
    "lb/ft": "plf", "lbf/ft": "plf", "k/ft": "klf", "kip/ft": "klf", "kn/m": "kN/m", "n/m": "N/m",
    "lb/in": "lbf/in", "n/mm": "N/mm",
    "lb/ft2": "psf", "lbf/ft²": "psf", "lb/sqft": "psf", "lb/in2": "psi", "kn/m2": "kN/m²",
    "deg c": "°C", "degc": "°C", "c": "°C", "deg f": "°F", "degf": "°F", "f": "°F",
    "mm4": "mm⁴", "cm4": "cm⁴", "in4": "in⁴", "kgmm2": "kg·mm²", "kg·m2": "kg·m²", "lb·in2": "lb·in²",
    "rev/min": "rpm", "r/min": "rpm", "hz": "Hz", "mph": "mph", "fpm": "ft/min", "ipm": "in/min",
    "sec": "s", "seconds": "s", "hr": "h", "hours": "h", "minutes": "min",
    "percent": "%", "pct": "%",
}

# ---------------------------------------------------------- special (affine / slope) ---


def _temp_to_base(v, u):
    return {"°C": v, "K": v - 273.15, "°F": (v - 32) * 5 / 9, "°R": (v - 491.67) * 5 / 9}[u]


def _temp_from_base(v, u):
    return {"°C": v, "K": v + 273.15, "°F": v * 9 / 5 + 32, "°R": (v + 273.15) * 9 / 5}[u]


def _slope_to_base(v, u):                      # base: degrees
    if u == "deg":
        return v
    if u == "%":
        return math.degrees(math.atan(v / 100))
    if u in ("in/ft", "rise:run"):              # rise per 12 (roof pitch "4/12" = 4 in/ft)
        return math.degrees(math.atan(v / 12))
    if u == "1:n":                              # 1 vertical : n horizontal
        return 90.0 if v == 0 else math.degrees(math.atan(1 / v))
    raise UnitError(u)


def _slope_from_base(v, u):
    t = math.tan(math.radians(v))
    if u == "deg":
        return v
    if u == "%":
        return t * 100
    if u in ("in/ft", "rise:run"):
        return t * 12
    if u == "1:n":
        return math.inf if abs(t) < 1e-15 else 1 / t
    raise UnitError(u)


class UnitError(ValueError):
    pass


def quantities() -> dict[str, str]:
    return {k: v[0] for k, v in Q.items()}


def units_of(quantity: str) -> list[str]:
    return list(Q[quantity][2])


def base_unit(quantity: str) -> str:
    return Q[quantity][1]


def canon(unit: str, quantity: str | None = None) -> str:
    """Canonical spelling of a unit ('inches' -> 'in', 'n-m' -> 'N·m')."""
    u = unit.strip()
    pools = [Q[quantity][2]] if quantity else [x[2] for x in Q.values()]
    for pool in pools:
        if u in pool:
            return u
    low = u.lower()
    if quantity == "linear_density" and low in ("lb/ft", "plf"):
        return "lb/ft"
    if quantity == "slope" and low in ("in/ft", "x/12", "/12", "pitch"):
        return "in/ft"
    a = ALIASES.get(u) or ALIASES.get(low)
    if a:
        if quantity is None or a in Q[quantity][2]:
            return a
        if quantity == "linear_load" and a == "lbf/in":
            return a
    for pool in pools:
        for k in pool:
            if k.lower() == low or k.replace("·", "-").lower() == low or k.replace("·", "").lower() == low:
                return k
    raise UnitError(f"Unknown unit '{unit}'" + (f" for {Q[quantity][0].lower()}" if quantity else ""))


def quantity_of_unit(unit: str) -> str:
    for q, (_l, _b, pool) in Q.items():
        if unit in pool and q not in ("slope", "temp_delta", "ratio", "kgmm"):
            return q
    raise UnitError(f"Unknown unit '{unit}'")


def to_base(value: float, unit: str, quantity: str) -> float:
    unit = canon(unit, quantity)
    if quantity == "temperature":
        return _temp_to_base(value, unit)
    if quantity == "slope":
        return _slope_to_base(value, unit)
    return value * Q[quantity][2][unit]


def from_base(value: float, unit: str, quantity: str) -> float:
    unit = canon(unit, quantity)
    if quantity == "temperature":
        return _temp_from_base(value, unit)
    if quantity == "slope":
        return _slope_from_base(value, unit)
    return value / Q[quantity][2][unit]


def convert(value: float, src: str, dst: str, quantity: str | None = None) -> float:
    q = quantity or quantity_of_unit(canon(src))
    return from_base(to_base(value, src, q), dst, q)


# ------------------------------------------------------------------- systems ---

@dataclass
class UnitSystem:
    name: str
    label: str
    units: dict = field(default_factory=dict)          # quantity -> unit (missing: base unit)
    decimals: dict = field(default_factory=dict)       # 'length' | 'angle' | 'default' -> places
    length_format: str = "decimal"                     # decimal | fractional | ft-in (architectural)
    denominator: int = 16                              # fractional / ft-in rounding (2..128)
    note: str = ""

    def unit(self, quantity: str) -> str:
        return self.units.get(quantity) or Q[quantity][1]

    def places(self, quantity: str) -> int:
        if quantity in self.decimals:
            return int(self.decimals[quantity])
        if quantity in ("length",):
            return int(self.decimals.get("length", 3))
        if quantity in ("angle", "slope"):
            return int(self.decimals.get("angle", 2))
        return int(self.decimals.get("default", 4))

    def to_dict(self):
        return {"name": self.name, "units": dict(self.units), "decimals": dict(self.decimals),
                "length_format": self.length_format, "denominator": self.denominator}


def _sys(name, label, units, decimals=None, length_format="decimal", denominator=16, note=""):
    return UnitSystem(name, label, units, decimals or {"length": 3, "angle": 2, "default": 4}, length_format,
                      denominator, note)


_SI_COMMON = {"angle": "deg", "slope": "deg", "force": "N", "time": "s", "frequency": "Hz", "energy": "J",
              "power": "W", "temperature": "°C", "temp_delta": "K", "angular_velocity": "rpm",
              "thermal_conductivity": "W/(m·K)", "flow": "L/min", "density": "kg/m³"}
_US_COMMON = {"angle": "deg", "slope": "deg", "force": "lbf", "time": "s", "frequency": "Hz", "energy": "ft·lbf",
              "power": "hp", "temperature": "°F", "temp_delta": "°F", "angular_velocity": "rpm", "mass": "lb",
              "thermal_conductivity": "Btu/(h·ft·°F)", "flow": "gpm", "linear_density": "lb/ft"}

SYSTEMS: dict[str, UnitSystem] = {s.name: s for s in (
    _sys("MMGS", "SI — mm · g · s (MMGS)", {**_SI_COMMON, "length": "mm", "area": "mm²", "volume": "mm³",
                                            "mass": "g", "density": "g/cm³", "torque": "N·mm", "pressure": "MPa",
                                            "velocity": "mm/s", "acceleration": "mm/s²", "linear_load": "N/mm",
                                            "area_load": "kPa", "stiffness": "N/mm", "area_moment": "mm⁴",
                                            "section_modulus": "mm³", "mass_moment": "g·mm²",
                                            "linear_density": "kg/m"}),
    _sys("SI_mm", "SI — mm · kg · N · MPa", {**_SI_COMMON, "length": "mm", "area": "mm²", "volume": "mm³",
                                             "mass": "kg", "torque": "N·m", "pressure": "MPa",
                                             "velocity": "mm/s", "acceleration": "m/s²", "linear_load": "N/mm",
                                             "area_load": "kPa", "stiffness": "N/mm", "area_moment": "mm⁴",
                                             "section_modulus": "mm³", "mass_moment": "kg·mm²",
                                             "linear_density": "kg/m"}),
    _sys("MKS", "SI — m · kg · N · Pa (MKS)", {**_SI_COMMON, "length": "m", "area": "m²", "volume": "m³",
                                               "mass": "kg", "torque": "N·m", "pressure": "Pa", "velocity": "m/s",
                                               "acceleration": "m/s²", "linear_load": "N/m", "area_load": "Pa",
                                               "stiffness": "N/m", "area_moment": "m⁴", "section_modulus": "m³",
                                               "mass_moment": "kg·m²", "linear_density": "kg/m"},
         {"length": 4, "angle": 2, "default": 4}),
    _sys("CGS", "CGS — cm · g · s", {**_SI_COMMON, "length": "cm", "area": "cm²", "volume": "cm³", "mass": "g",
                                     "density": "g/cm³", "force": "dyn", "torque": "N·mm", "pressure": "kPa",
                                     "velocity": "m/s", "acceleration": "m/s²", "linear_load": "N/mm",
                                     "area_load": "kPa", "stiffness": "N/mm", "area_moment": "cm⁴",
                                     "section_modulus": "cm³", "mass_moment": "g·mm²", "linear_density": "kg/m"},
         {"length": 3, "angle": 2, "default": 4}),
    _sys("IPS", "US customary — in · lb · s (IPS)", {**_US_COMMON, "length": "in", "area": "in²",
                                                    "volume": "in³", "density": "lb/in³", "torque": "lbf·in",
                                                    "pressure": "psi", "velocity": "in/s", "acceleration": "in/s²",
                                                    "linear_load": "lbf/in", "area_load": "psi",
                                                    "stiffness": "lbf/in", "area_moment": "in⁴",
                                                    "section_modulus": "in³", "mass_moment": "lb·in²",
                                                    "energy": "in·lbf"},
         {"length": 4, "angle": 2, "default": 4}),
    _sys("IPS_frac", "US customary — fractional inches", {**_US_COMMON, "length": "in", "area": "in²",
                                                          "volume": "in³", "density": "lb/in³",
                                                          "torque": "lbf·in", "pressure": "psi", "velocity": "in/s",
                                                          "acceleration": "in/s²", "linear_load": "lbf/in",
                                                          "area_load": "psi", "stiffness": "lbf/in",
                                                          "area_moment": "in⁴", "section_modulus": "in³",
                                                          "mass_moment": "lb·in²", "energy": "in·lbf"},
         {"length": 4, "angle": 2, "default": 4}, "fractional", 64),
    _sys("FPS", "US customary — ft · lb · s (FPS)", {**_US_COMMON, "length": "ft", "area": "ft²",
                                                    "volume": "ft³", "density": "lb/ft³", "torque": "lbf·ft",
                                                    "pressure": "psi", "velocity": "ft/s", "acceleration": "ft/s²",
                                                    "linear_load": "plf", "area_load": "psf", "stiffness": "lbf/in",
                                                    "area_moment": "in⁴", "section_modulus": "in³",
                                                    "mass_moment": "lb·ft²"},
         {"length": 4, "angle": 2, "default": 4}),
    _sys("CONST_US", "Construction — ft-in-fraction (architectural)",
         {**_US_COMMON, "length": "ft", "slope": "in/ft", "area": "ft²", "volume": "yd³", "mass": "lb",
          "density": "lb/ft³", "force": "kip", "torque": "kip·ft", "pressure": "ksi", "velocity": "ft/min",
          "acceleration": "ft/s²", "linear_load": "plf", "area_load": "psf", "stiffness": "lbf/in", "area_moment": "in⁴", "section_modulus": "in³", "mass_moment": "lb·ft²",
          "linear_density": "lb/ft"},
         {"length": 4, "angle": 2, "default": 3}, "ft-in", 16,
         "Lengths read 5'-6 1/4\"; slopes as rise per 12; loads in plf / psf / kips; concrete in yd³."),
    _sys("CONST_US_ENG", "Construction — decimal feet (civil / survey)",
         {**_US_COMMON, "length": "ft", "slope": "%", "area": "ft²", "volume": "yd³", "density": "lb/ft³",
          "force": "kip", "torque": "kip·ft", "pressure": "ksi", "velocity": "ft/s", "acceleration": "ft/s²",
          "linear_load": "klf", "area_load": "ksf", "stiffness": "lbf/in", "area_moment": "in⁴",
          "section_modulus": "in³", "mass_moment": "lb·ft²", "linear_density": "lb/ft"},
         {"length": 2, "angle": 2, "default": 3}, "decimal", 16, "Stations / grades: decimal feet, slope in %."),
    _sys("CONST_SI", "Construction — metric (mm · kN · MPa)",
         {**_SI_COMMON, "length": "mm", "slope": "%", "area": "m²", "volume": "m³", "mass": "kg",
          "density": "kg/m³", "force": "kN", "torque": "kN·m", "pressure": "MPa", "velocity": "m/s",
          "acceleration": "m/s²", "linear_load": "kN/m", "area_load": "kPa", "stiffness": "kN/m",
          "area_moment": "mm⁴", "section_modulus": "mm³", "mass_moment": "kg·m²", "linear_density": "kg/m"},
         {"length": 0, "angle": 2, "default": 3}, note="Drawings in mm, areas m², concrete m³, loads kN, kN/m, kPa."),
)}
DEFAULT = "SI_mm"

_active = SYSTEMS[DEFAULT]


def system_from(d: dict | str | None) -> UnitSystem:
    """A UnitSystem from a stored dict ({name, units overrides, decimals, length_format, denominator})."""
    if isinstance(d, UnitSystem):
        return d
    if not d:
        return SYSTEMS[DEFAULT]
    if isinstance(d, str):
        d = {"name": d}
    base = SYSTEMS.get(d.get("name") or DEFAULT, SYSTEMS[DEFAULT])
    s = UnitSystem(base.name, base.label, dict(base.units), dict(base.decimals), base.length_format,
                   base.denominator, base.note)
    for q, u in (d.get("units") or {}).items():
        if q in Q:
            try:
                s.units[q] = canon(u, q)
            except UnitError:
                pass
    s.decimals.update({k: int(v) for k, v in (d.get("decimals") or {}).items()})
    s.length_format = d.get("length_format", s.length_format)
    s.denominator = int(d.get("denominator", s.denominator) or 16)
    if d.get("units") and any(base.units.get(q) != s.units.get(q) for q in d["units"]):
        s.label = base.label + " (custom)"
    return s


def active() -> UnitSystem:
    return _active


def set_active(system) -> UnitSystem:
    global _active
    _active = system_from(system)
    return _active


# ------------------------------------------------------------------- format ---

def _frac(x: float, den: int) -> tuple[int, int, int]:
    """x ≥ 0 -> (whole, num, den) rounded to 1/den, reduced."""
    n = round(x * den)
    whole, num = divmod(n, den)
    d = den
    while num and num % 2 == 0 and d % 2 == 0:
        num //= 2
        d //= 2
    return int(whole), int(num), int(d)


def fmt_fraction_in(inches: float, den: int = 16, unit_mark='"') -> str:
    s = "-" if inches < 0 else ""
    w, n, d = _frac(abs(inches), den)
    if n == 0:
        return f"{s}{w}{unit_mark}"
    return f"{s}{w} {n}/{d}{unit_mark}" if w else f"{s}{n}/{d}{unit_mark}"


def fmt_ft_in(mm: float, den: int = 16) -> str:
    """Architectural: 5'-6 1/4\" (always feet-dash-inches; 0'-3/8\" for small values)."""
    s = "-" if mm < 0 else ""
    total = abs(mm) / IN
    n_all = round(total * den)
    ft, rem = divmod(n_all, 12 * den)
    w, n = divmod(rem, den)
    d = den
    while n and n % 2 == 0 and d % 2 == 0:
        n //= 2
        d //= 2
    inch = f"{w}" if not n else (f"{w} {n}/{d}" if w else f"{n}/{d}")
    return f"{s}{ft}'-{inch}\""


def fmt(value: float | None, quantity: str = "length", system: UnitSystem | None = None, unit: str | None = None,
        places: int | None = None, show_unit: bool = True) -> str:
    """Base value → display text in the system's unit (or a given unit)."""
    if value is None:
        return "—"
    s = system or _active
    u = canon(unit, quantity) if unit else s.unit(quantity)
    if quantity == "length" and unit is None and s.length_format == "ft-in":
        return fmt_ft_in(value, s.denominator)
    if quantity == "length" and unit is None and s.length_format == "fractional" and u == "in":
        return fmt_fraction_in(value / IN, s.denominator, '"' if show_unit else "")
    v = from_base(value, u, quantity)
    if abs(v) < 1e-9 and quantity != "temperature":
        v = 0.0
    if quantity == "slope" and u in ("in/ft", "rise:run"):
        return f"{_trim(v, 2)}/12" if show_unit else _trim(v, 2)
    if quantity == "slope" and u == "1:n":
        return "1:∞" if math.isinf(v) else f"1:{_trim(v, 2)}"
    p = s.places(quantity) if places is None else places
    if v and abs(v) < 1 and places is None and not (quantity == "length" and p == 0):
        p = min(max(p, -math.floor(math.log10(abs(v))) + 2), 10)   # small values keep 3 significant digits
    txt = _trim(v, p)
    if not show_unit or not u:
        return txt
    return f"{txt}{'' if u in ('°C', '°F', '%') else ' '}{'°' if u == 'deg' else u}".replace(" °", "°")


def _trim(v, p):
    t = f"{v:,.{p}f}" if abs(v) >= 1e5 else f"{v:.{p}f}"
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def label(quantity: str, system: UnitSystem | None = None) -> str:
    """Unit suffix for a field ('mm', '°', 'in', 'ft-in')."""
    s = system or _active
    if quantity == "length" and s.length_format == "ft-in":
        return "ft-in"
    u = s.unit(quantity)
    return "°" if u == "deg" else u


# --------------------------------------------------------------------- parse ---

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUN = {"sqrt": math.sqrt, "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
        "tan": lambda d: math.tan(math.radians(d)), "pi": math.pi, "abs": abs, "round": round, "min": min,
        "max": max}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _FUN and not callable(_FUN[node.id]):
        return _FUN[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and callable(_FUN.get(node.func.id)):
        return float(_FUN[node.func.id](*[_eval(a) for a in node.args]))
    raise UnitError("Not a number or expression")


def _unit_tokens(quantity):
    toks = {}
    pools = [quantity] if quantity else list(Q)
    for q in pools:
        for u in Q[q][2]:
            if u:
                toks[u] = (q, u)
    for a, u in ALIASES.items():
        for q in pools:
            if u in Q[q][2] and a not in ("c", "f", "k", "'", '"', "''"):
                toks.setdefault(a, (q, u))
    return sorted(toks.items(), key=lambda kv: -len(kv[0]))


_TOK_CACHE: dict = {}


def parse(text: str | float, quantity: str = "length", default_unit: str | None = None,
          system: UnitSystem | None = None) -> float:
    """Typed text → base value. Bare numbers are in default_unit (or the active system's unit).

    Accepts: '12.5', '1/2"', '3 ft 4-1/2 in', "5'-6 1/4\"", "5' 6\"", '25.4mm + 2', '2*3 in', '45°', '4/12'
    (slope), '3%' (slope), '1:12', '72°F', '(10 + 2) mm'.
    """
    if isinstance(text, (int, float)):
        v = float(text)
        u = default_unit or (system or _active).unit(quantity)
        return to_base(v, u, quantity)
    s = (system or _active)
    raw = str(text).strip().replace("−", "-").replace("×", "*").replace("÷", "/")
    raw = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", raw)
    if not raw:
        raise UnitError("Empty value")
    du = canon(default_unit, quantity) if default_unit else s.unit(quantity)
    if quantity == "temperature":
        m = re.fullmatch(r"\s*(-?[\d.]+(?:[eE][-+]?\d+)?)\s*(°?\s*[CFKR]|K|deg\s*[CF])?\s*", raw, re.I)
        if not m:
            raise UnitError("Temperature: a number with °C, °F, K or °R")
        u = du
        if m.group(2):
            t = m.group(2).replace(" ", "").replace("°", "").upper().replace("DEG", "")
            u = {"C": "°C", "F": "°F", "K": "K", "R": "°R"}[t]
        return to_base(float(m.group(1)), u, quantity)
    if quantity == "slope":
        m = re.fullmatch(r"\s*([\d.]+)\s*(?:/|in\s*/|:)\s*12\s*", raw)
        if m:
            return _slope_to_base(float(m.group(1)), "in/ft")
        m = re.fullmatch(r"\s*1\s*:\s*([\d.]+)\s*", raw)
        if m:
            return _slope_to_base(float(m.group(1)), "1:n")
        m = re.fullmatch(r"\s*(-?[\d.]+)\s*%\s*", raw)
        if m:
            return _slope_to_base(float(m.group(1)), "%")
        m = re.fullmatch(r"\s*(-?[\d.]+)\s*(?:°|deg)\s*", raw)
        if m:
            return float(m.group(1))
        return _slope_to_base(float(_eval(ast.parse(raw, mode="eval"))), du)
    t = raw
    if quantity in ("length", None):
        # architectural: 5'-6 1/4"  /  5' 6"  /  5'6-1/2"
        t = re.sub(r"(\d+(?:\.\d+)?)\s*'\s*-?\s*(?=\d)", r"\1ft + ", t)
        t = t.replace("''", '"')
        t = re.sub(r"(\d)\s*'", r"\1ft", t)
        t = re.sub(r'(\d)\s*"', r"\1in", t)
    # mixed numbers: 1 1/2, 1-1/2 → (1+1/2)
    t = re.sub(r"(?<![\d./])(\d+)(?:\s+|-)(\d+)\s*/\s*(\d+)(?![\d.])", r"(\1+\2/\3)", t)
    key = quantity
    if key not in _TOK_CACHE:
        _TOK_CACHE[key] = _unit_tokens(quantity)
    toks = _TOK_CACHE[key]
    base_q = quantity
    fac_d = to_base(1.0, du, quantity) if quantity not in ("temperature", "slope") else 1.0
    i, last_unit = 0, False
    # replace each unit token with *(factor relative to default unit)
    lower = t
    res = ""
    while i < len(lower):
        ch = lower[i]
        if ch in "eE" and i and (lower[i - 1].isdigit() or lower[i - 1] == ".") and \
                re.match(r"[eE][-+]?\d", lower[i:]):
            m = re.match(r"[eE][-+]?\d+", lower[i:])
            res += m.group(0)
            i += len(m.group(0))
            continue
        if ch.isalpha() or ch in "°µ\"'%²³⁴":
            hit = None
            for tok, (q, u) in toks:
                if lower[i:i + len(tok)].lower() == tok.lower() and \
                        (i + len(tok) >= len(lower) or not (lower[i + len(tok)].isalpha() and tok[-1].isalpha())):
                    hit = (tok, q, u)
                    break
            if hit is None:
                m = re.match(r"[A-Za-z_]+", lower[i:])
                name = m.group(0) if m else ch
                if name in _FUN:
                    res += name
                    i += len(name)
                    continue
                raise UnitError(f"Unknown unit or name '{name}'")
            tok, q, u = hit
            if base_q and q != base_q:
                raise UnitError(f"'{tok}' is a {Q[q][0].lower()} unit, not {Q[base_q][0].lower()}")
            f = Q[q][2][u] / fac_d
            res = res.rstrip() + f"*{f!r}"
            i += len(tok)
            last_unit = True
            continue
        if last_unit and (ch.isdigit() or ch == "(") and res and not res.rstrip().endswith(("+", "-", "*", "/")):
            res += "+"                                 # '3 ft 4 in' → 3ft + 4in
        last_unit = False if not ch.isspace() else last_unit
        res += ch
        i += 1
    try:
        v = _eval(ast.parse(res.strip(), mode="eval"))
    except SyntaxError as e:
        raise UnitError(f"Can't read '{text}'") from e
    return float(v) * fac_d


def try_parse(text, quantity="length", default_unit=None, system=None):
    try:
        return parse(text, quantity, default_unit, system)
    except (UnitError, ZeroDivisionError, OverflowError, ValueError, TypeError):
        return None


# --------------------------------------------------------- feature parameters ---

_UNITLESS = ("factor", "ratio", "scale", "k_factor", "kfactor", "rho", "weight", "fraction", "tension",
             "friction", "coefficient", "percent", "turns", "revolutions", "count", "mu", "efficiency")


def param_quantity(p) -> str | None:
    """Quantity of a feature parameter (float), inferred from its name and help when not declared."""
    q = getattr(p, "quantity", None)
    if q:
        return None if q == "none" else q
    if getattr(p, "kind", "float") != "float":
        return None
    n = p.name.lower()
    h = (p.help or "").lower()
    if re.search(r"\((?:deg|degrees)\b", h) or "°" in h or "angle" in n or n in ("draft", "rotation", "twist", "taper", "sweep_angle",
                                                          "spin") or "(degrees)" in h:
        return "angle"
    if any(w in n for w in _UNITLESS) or "(%)" in h or "ratio" in h and "(mm)" not in h:
        return None
    if "(mm²)" in h:
        return "area"
    if "(n)" in h:
        return "force"
    if "(mpa)" in h:
        return "pressure"
    if "(kg)" in h:
        return "mass"
    return "length"
