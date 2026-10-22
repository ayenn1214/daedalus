"""Engineering material library. Qt-free.

Typical handbook values (room temperature, isotropic). Wood, plywood, MDF and composites are
anisotropic in reality: values are along-grain / in-plane and should be treated as estimates.
Rubber and other elastomers are non-linear; linear FEA results for them are indicative only.

Units: density g/cm³ · E GPa · yield/UTS/fatigue MPa · k W/(m·K) · cp J/(kg·K) · α µm/(m·K)
fatigue: fully reversed strength at `fatigue_cycles` (an endurance limit when endurance=True).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Material:
    name: str
    category: str
    density: float                 # g/cm³
    E: float                       # GPa
    nu: float
    yield_: float                  # MPa (brittle materials: design tensile strength)
    uts: float                     # MPa
    k: float                       # W/(m·K)
    cp: float                      # J/(kg·K)
    alpha: float                   # µm/(m·K)
    fatigue: float                 # MPa, fully reversed, at fatigue_cycles
    fatigue_cycles: float = 1e6
    endurance: bool = False        # True: no failure below `fatigue` (steels, Ti)
    ductile: bool = True
    note: str = ""
    custom: bool = False
    cost_per_kg: float = 0.0       # raw material, for BOM costing (0 = category default)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        keys = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in keys})

    def price(self) -> float:
        """Raw-material price per kg (rough market figure, any currency)."""
        if self.cost_per_kg:
            return self.cost_per_kg
        special = {"PEEK": 110.0, "PTFE (Teflon)": 25.0, "Titanium Ti-6Al-4V (annealed)": 45.0,
                   "Inconel 718 (aged)": 55.0, "Stainless 17-4 PH (H1025)": 12.0, "Aluminum 7075-T6": 9.0,
                   "Tool steel A2 (hardened)": 9.0}
        if self.name in special:
            return special[self.name]
        return {"Steel": 2.0, "Stainless steel": 6.5, "Cast iron": 2.5, "Aluminum": 6.0, "Copper alloy": 12.0,
                "Titanium": 35.0, "Magnesium": 8.0, "Nickel alloy": 50.0, "Zinc": 4.0, "Plastic": 6.0,
                "Elastomer": 8.0, "Composite": 30.0, "Wood": 2.5, "Ceramic & glass": 5.0}.get(self.category, 5.0)

    def summary(self) -> str:
        return (f"{self.density:g} g/cm³ · E {self.E:g} GPa · ν {self.nu:g} · Sy {self.yield_:g} MPa · "
                f"UTS {self.uts:g} MPa")


def _m(name, cat, rho, E, nu, sy, su, k, cp, a, sf, n=1e6, endurance=False, ductile=True, note=""):
    return Material(name, cat, rho, E, nu, sy, su, k, cp, a, sf, n, endurance, ductile, note)


_ANISO = "Anisotropic: along-grain / in-plane values used as isotropic — estimate only."

LIBRARY: list[Material] = [
    # ---- carbon & alloy steel
    _m("Steel AISI 1018 (CD)", "Steel", 7.87, 205, 0.29, 370, 440, 51.9, 486, 12.0, 220, endurance=True),
    _m("Steel AISI 1020 (HR)", "Steel", 7.87, 200, 0.29, 210, 380, 51.9, 486, 11.7, 190, endurance=True),
    _m("Steel AISI 1045 (CD)", "Steel", 7.85, 205, 0.29, 530, 625, 49.8, 486, 11.2, 310, endurance=True),
    _m("Steel AISI 4140 (annealed)", "Steel", 7.85, 205, 0.29, 415, 655, 42.6, 473, 12.2, 330, endurance=True),
    _m("Steel AISI 4140 (Q&T)", "Steel", 7.85, 205, 0.29, 655, 1020, 42.6, 473, 12.2, 510, endurance=True),
    _m("Steel AISI 4340 (Q&T)", "Steel", 7.85, 205, 0.29, 860, 1280, 44.5, 475, 12.3, 640, endurance=True),
    _m("Steel ASTM A36", "Steel", 7.85, 200, 0.26, 250, 400, 51.9, 486, 11.7, 200, endurance=True),
    _m("Steel ASTM A572 Gr 50", "Steel", 7.85, 200, 0.29, 345, 450, 51.9, 486, 11.7, 225, endurance=True),
    _m("Steel ASTM A500 Gr B (HSS tube)", "Steel", 7.85, 200, 0.29, 315, 400, 51.9, 486, 11.7, 200, endurance=True),
    _m("Steel ASTM A513 (DOM tube)", "Steel", 7.87, 200, 0.29, 480, 550, 51.9, 486, 11.7, 275, endurance=True),
    _m("Spring steel AISI 1095 (hardened)", "Steel", 7.85, 205, 0.29, 1150, 1380, 49.8, 486, 11.0, 650, endurance=True),
    _m("Tool steel A2 (hardened)", "Steel", 7.86, 203, 0.29, 1500, 1800, 26.0, 460, 10.7, 700, endurance=True,
       ductile=False),
    # ---- stainless
    _m("Stainless 304 (annealed)", "Stainless steel", 8.00, 193, 0.29, 215, 505, 16.2, 500, 17.3, 240,
       endurance=True),
    _m("Stainless 316 (annealed)", "Stainless steel", 8.00, 193, 0.30, 205, 515, 16.3, 500, 15.9, 240,
       endurance=True),
    _m("Stainless 410 (annealed)", "Stainless steel", 7.80, 200, 0.28, 275, 485, 24.9, 460, 9.9, 240,
       endurance=True),
    _m("Stainless 17-4 PH (H1025)", "Stainless steel", 7.80, 197, 0.27, 1000, 1070, 18.3, 460, 10.8, 500,
       endurance=True),
    # ---- cast iron
    _m("Gray cast iron ASTM A48 Class 40", "Cast iron", 7.20, 110, 0.26, 276, 276, 53.0, 490, 10.8, 128,
       endurance=True, ductile=False, note="Brittle: strength checks use UTS."),
    _m("Ductile iron 65-45-12", "Cast iron", 7.10, 169, 0.29, 310, 448, 36.0, 506, 11.2, 195, endurance=True),
    # ---- aluminium
    _m("Aluminum 1100-H14", "Aluminum", 2.71, 69.0, 0.33, 117, 124, 218, 904, 23.6, 48, 5e8),
    _m("Aluminum 2024-T3", "Aluminum", 2.78, 73.1, 0.33, 345, 483, 121, 875, 23.2, 138, 5e8),
    _m("Aluminum 3003-H14", "Aluminum", 2.73, 68.9, 0.33, 145, 150, 159, 893, 23.2, 62, 5e8),
    _m("Aluminum 5052-H32", "Aluminum", 2.68, 70.3, 0.33, 193, 228, 138, 880, 23.8, 117, 5e8),
    _m("Aluminum 6061-T6", "Aluminum", 2.70, 68.9, 0.33, 276, 310, 167, 896, 23.6, 96.5, 5e8),
    _m("Aluminum 6063-T5", "Aluminum", 2.70, 68.9, 0.33, 145, 186, 209, 900, 23.4, 68.9, 5e8),
    _m("Aluminum 7075-T6", "Aluminum", 2.81, 71.7, 0.33, 503, 572, 130, 960, 23.6, 159, 5e8),
    _m("Aluminum A356-T6 (cast)", "Aluminum", 2.67, 72.4, 0.33, 186, 262, 151, 963, 21.4, 90, 5e8),
    # ---- copper alloys
    _m("Copper C110 (annealed)", "Copper alloy", 8.89, 115, 0.31, 69, 220, 388, 385, 17.0, 76, 1e8),
    _m("Brass C360 (free-machining)", "Copper alloy", 8.50, 97, 0.31, 310, 385, 115, 380, 20.5, 138, 1e8),
    _m("Brass C260 (cartridge, half hard)", "Copper alloy", 8.53, 110, 0.375, 360, 420, 120, 375, 20.0, 145, 1e8),
    _m("Bronze C932 (bearing bronze)", "Copper alloy", 8.93, 100, 0.34, 124, 241, 58, 376, 18.0, 110, 1e8),
    _m("Phosphor bronze C510 (H04)", "Copper alloy", 8.86, 110, 0.34, 515, 560, 84, 380, 17.8, 205, 1e8),
    # ---- titanium, magnesium, nickel, zinc
    _m("Titanium Ti-6Al-4V (annealed)", "Titanium", 4.43, 113.8, 0.342, 880, 950, 6.7, 526, 8.6, 510,
       1e7, endurance=True),
    _m("Titanium Grade 2 (CP)", "Titanium", 4.51, 105, 0.37, 275, 345, 16.4, 523, 8.6, 200, 1e7, endurance=True),
    _m("Magnesium AZ31B", "Magnesium", 1.77, 45, 0.35, 200, 260, 96, 1000, 26.0, 96, 5e8),
    _m("Inconel 718 (aged)", "Nickel alloy", 8.19, 200, 0.29, 1100, 1375, 11.4, 435, 13.0, 600, 1e7,
       endurance=True),
    _m("Zinc Zamak 3 (die cast)", "Zinc", 6.60, 96, 0.27, 208, 268, 113, 419, 27.4, 47.6, 5e8),
    # ---- plastics
    _m("ABS", "Plastic", 1.04, 2.3, 0.35, 40, 44, 0.17, 1400, 90, 12, 1e7),
    _m("PLA (3D printed)", "Plastic", 1.24, 3.5, 0.36, 55, 60, 0.13, 1800, 68, 15, 1e6,
       note="Printed parts are weaker across layers (often 50–70 %)."),
    _m("PETG", "Plastic", 1.27, 2.1, 0.38, 50, 53, 0.20, 1200, 60, 14, 1e6),
    _m("Nylon 6/6 (dry)", "Plastic", 1.14, 2.9, 0.39, 80, 85, 0.25, 1670, 80, 25, 1e7),
    _m("Nylon 6 (cast, MC901)", "Plastic", 1.15, 3.3, 0.39, 75, 85, 0.24, 1700, 90, 25, 1e7),
    _m("Polycarbonate (PC)", "Plastic", 1.20, 2.35, 0.37, 62, 66, 0.20, 1200, 68, 14, 1e7),
    _m("Acetal POM (Delrin)", "Plastic", 1.42, 3.1, 0.35, 69, 69, 0.31, 1470, 110, 31, 1e7),
    _m("HDPE", "Plastic", 0.95, 1.07, 0.42, 26, 31, 0.48, 1900, 120, 10, 1e7),
    _m("UHMW-PE", "Plastic", 0.93, 0.69, 0.46, 21, 40, 0.41, 1840, 200, 8, 1e7),
    _m("Polypropylene (PP)", "Plastic", 0.905, 1.5, 0.42, 33, 35, 0.22, 1920, 100, 14, 1e7),
    _m("PTFE (Teflon)", "Plastic", 2.17, 0.5, 0.46, 10, 25, 0.25, 1000, 135, 6, 1e7),
    _m("PEEK", "Plastic", 1.30, 3.6, 0.40, 100, 100, 0.25, 1340, 47, 38, 1e7),
    _m("PVC (rigid)", "Plastic", 1.40, 3.0, 0.38, 45, 50, 0.16, 1000, 60, 17, 1e7),
    _m("Acrylic PMMA", "Plastic", 1.19, 3.1, 0.37, 65, 72, 0.19, 1470, 70, 28, 1e7, ductile=False),
    _m("Neoprene rubber (60A)", "Elastomer", 1.23, 0.0055, 0.49, 15, 15, 0.19, 2100, 200, 5, 1e6,
       note="Elastomer: linear FEA is indicative only."),
    # ---- composites
    _m("G-10 / FR-4 (in-plane)", "Composite", 1.85, 18.6, 0.12, 310, 310, 0.29, 1150, 9.9, 100, 1e7,
       ductile=False, note=_ANISO),
    _m("Carbon fibre (CFRP, quasi-isotropic)", "Composite", 1.55, 55, 0.30, 570, 600, 5.0, 1000, 2.0, 280,
       1e7, ductile=False, note=_ANISO),
    # ---- wood (along grain)
    _m("Hard maple", "Wood", 0.71, 12.6, 0.30, 54, 109, 0.16, 1700, 4.0, 33, 1e7, ductile=False, note=_ANISO),
    _m("Red oak", "Wood", 0.70, 12.5, 0.30, 47, 99, 0.17, 1700, 4.9, 30, 1e7, ductile=False, note=_ANISO),
    _m("White oak", "Wood", 0.75, 12.3, 0.30, 51, 105, 0.17, 1700, 4.9, 31, 1e7, ductile=False, note=_ANISO),
    _m("Douglas fir", "Wood", 0.53, 13.4, 0.30, 50, 85, 0.12, 1700, 4.0, 26, 1e7, ductile=False, note=_ANISO),
    _m("Pine / SPF lumber", "Wood", 0.42, 9.5, 0.30, 33, 65, 0.12, 1700, 4.0, 20, 1e7, ductile=False,
       note=_ANISO),
    _m("Baltic birch plywood", "Wood", 0.68, 9.5, 0.30, 35, 60, 0.15, 1700, 6.0, 18, 1e7, ductile=False,
       note=_ANISO),
    _m("MDF", "Wood", 0.75, 3.5, 0.25, 18, 30, 0.13, 1700, 10.0, 9, 1e7, ductile=False, note=_ANISO),
    # ---- other
    _m("Glass (soda-lime)", "Ceramic & glass", 2.50, 70, 0.22, 40, 40, 1.0, 840, 9.0, 20, 1e6, ductile=False,
       note="Brittle: design strength, not UTS of flawless glass."),
    _m("Alumina 96%", "Ceramic & glass", 3.72, 300, 0.21, 260, 260, 25, 880, 8.2, 130, 1e6, ductile=False),
]

BY_NAME: dict[str, Material] = {m.name: m for m in LIBRARY}

# names used by files from earlier versions
ALIASES = {
    "Steel 1018": "Steel AISI 1018 (CD)", "Stainless 304": "Stainless 304 (annealed)",
    "Aluminum 6061": "Aluminum 6061-T6", "Brass": "Brass C360 (free-machining)", "ABS": "ABS",
    "PLA": "PLA (3D printed)", "Hard maple": "Hard maple", "Baltic birch ply": "Baltic birch plywood",
}
DEFAULT = "Steel AISI 1018 (CD)"


def categories() -> list[str]:
    out = []
    for m in LIBRARY:
        if m.category not in out:
            out.append(m.category)
    return out


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def get(name: str, custom: dict | None = None) -> Material:
    """Look up a material (library, then the document's custom ones). Raises KeyError."""
    name = canonical(name)
    if custom and name in custom:
        c = custom[name]
        return c if isinstance(c, Material) else Material.from_dict(c)
    if name in BY_NAME:
        return BY_NAME[name]
    raise KeyError(name)


def search(text: str = "", category: str | None = None, custom: dict | None = None) -> list[Material]:
    pool = list(LIBRARY) + [get(n, custom) for n in (custom or {})]
    t = text.lower().strip()
    return [m for m in pool if (not category or m.category == category)
            and (not t or t in m.name.lower() or t in m.category.lower())]
