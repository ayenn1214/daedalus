"""Hollow structural sections (HSS): standard size tables, corner-radius rules, section properties, profiles.

Standards
  AISC   — ASTM A500 / A1085, US designations (HSS4x4x1/4, HSS6.625x0.280). Properties use the design wall
           t = 0.93·t_nom (A500) as in the AISC Manual; nominal weight uses t_nom. Outside corner radius 2t.
  CSA    — CAN/CSA G40.20/G40.21 (CISC Handbook), metric designations (HSS 102x102x6.4); t = nominal,
           outside corner radius 2t.
  EN10219 — cold-formed SHS/RHS/CHS (Europe/UK/Australia-style metric); ro = 2t (t ≤ 6), 2.5t (≤ 10), 3t.
  EN10210 — hot-finished SHS/RHS/CHS; ro = 1.5t, ri = 1.0t.
  custom — any B × H × t, round OD × t, with any corner radius rule.

Sizes are the common catalogue ranges; check mill availability before ordering. All lengths are mm.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from fractions import Fraction

IN = 25.4
STEEL_DENSITY = 7850.0                     # kg/m³ (AISC weights use 490 lb/ft³ = 7849 kg/m³)
STANDARDS = ["AISC", "CSA", "EN10219", "EN10210", "custom"]
STANDARD_LABELS = {"AISC": "AISC / ASTM A500 (US, inch)", "CSA": "CSA G40.20/21 (Canada, metric)",
                   "EN10219": "EN 10219 cold-formed (metric)", "EN10210": "EN 10210 hot-finished (metric)",
                   "custom": "Custom / non-standard"}
SHAPES = ["square", "rect", "round"]
CORNER_RULES = ["standard", "2t", "1.5t", "2.5t", "3t", "custom", "sharp"]


@dataclass
class HSS:
    standard: str
    shape: str                     # square | rect | round
    designation: str
    B: float                       # width (mm) — OD for round
    H: float                       # depth (mm) — = B for square / round
    t_nom: float                   # nominal wall (mm)
    t: float                       # design wall (mm)
    ro: float                      # outside corner radius (mm); 0 round
    ri: float                      # inside corner radius (mm)

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------------ size tables ---
F = Fraction
_FR = [F(1, 8), F(3, 16), F(1, 4), F(5, 16), F(3, 8), F(1, 2), F(5, 8), F(3, 4)]


def _walls(lo, hi):
    return [f for f in _FR if F(lo) <= f <= F(hi)]


# (B in, lo, hi wall) for square; (H, B, lo, hi) rect
_AISC_SQ = [(2, "1/8", "1/4"), ("2-1/4", "1/8", "1/4"), ("2-1/2", "1/8", "5/16"), (3, "1/8", "3/8"),
            ("3-1/2", "1/8", "3/8"), (4, "1/8", "1/2"), ("4-1/2", "1/8", "1/2"), (5, "1/8", "1/2"),
            ("5-1/2", "1/8", "3/8"), (6, "1/8", "5/8"), (7, "1/8", "5/8"), (8, "1/8", "5/8"), (9, "1/8", "5/8"),
            (10, "3/16", "5/8"), (12, "3/16", "5/8"), (14, "5/16", "5/8"), (16, "5/16", "5/8"),
            (18, "3/8", "5/8"), (20, "3/8", "3/4")]
_AISC_RECT = [(3, 1, "1/8", "3/16"), (3, "1-1/2", "1/8", "1/4"), (3, 2, "1/8", "1/4"),
              ("3-1/2", "1-1/2", "1/8", "1/4"), ("3-1/2", 2, "1/8", "1/4"), ("3-1/2", "2-1/2", "1/8", "5/16"),
              (4, 1, "1/8", "3/16"), (4, "1-1/2", "1/8", "1/4"), (4, 2, "1/8", "3/8"), (4, "2-1/2", "1/8", "5/16"),
              (4, 3, "1/8", "3/8"), (5, 2, "1/8", "5/16"), (5, "2-1/2", "1/8", "1/4"), (5, 3, "1/8", "1/2"),
              (5, 4, "1/8", "1/2"), (6, 2, "1/8", "3/8"), (6, 3, "1/8", "1/2"), (6, 4, "1/8", "1/2"),
              (6, 5, "1/8", "1/2"), (7, 2, "1/8", "1/4"), (7, 3, "1/8", "1/2"), (7, 4, "1/8", "1/2"),
              (7, 5, "1/8", "1/2"), (8, 2, "1/8", "3/8"), (8, 3, "1/8", "1/2"), (8, 4, "1/8", "5/8"),
              (8, 6, "3/16", "5/8"), (9, 3, "1/8", "1/2"), (9, 5, "1/8", "5/8"), (9, 7, "3/16", "5/8"),
              (10, 2, "1/8", "3/8"), (10, 3, "1/8", "3/8"), (10, 4, "1/8", "5/8"), (10, 5, "3/16", "3/8"),
              (10, 6, "3/16", "5/8"), (10, 8, "3/16", "5/8"), (12, 2, "3/16", "1/4"), (12, 3, "3/16", "5/16"),
              (12, 4, "3/16", "5/8"), (12, 6, "3/16", "5/8"), (12, 8, "3/16", "5/8"), (12, 10, "1/4", "1/2"),
              (14, 4, "5/16", "5/8"), (14, 6, "5/16", "5/8"), (14, 10, "5/16", "5/8"), (16, 4, "5/16", "5/8"),
              (16, 8, "5/16", "5/8"), (16, 12, "5/16", "5/8"), (18, 6, "3/8", "5/8"), (20, 4, "3/8", "1/2"),
              (20, 8, "3/8", "5/8"), (20, 12, "3/8", "5/8")]
_AISC_ROUND = {1.660: [0.140], 1.900: [0.120, 0.145, 0.188], 2.375: [0.125, 0.154, 0.188, 0.218, 0.250],
               2.500: [0.125, 0.188, 0.250], 2.875: [0.125, 0.188, 0.203, 0.250, 0.276],
               3.000: [0.134, 0.188, 0.250], 3.500: [0.125, 0.188, 0.216, 0.250, 0.300, 0.313],
               4.000: [0.125, 0.188, 0.220, 0.226, 0.237, 0.250, 0.313, 0.318, 0.337],
               4.500: [0.125, 0.188, 0.237, 0.337, 0.375],
               5.000: [0.125, 0.188, 0.250, 0.258, 0.312, 0.375, 0.500],
               5.563: [0.134, 0.188, 0.258, 0.375, 0.500], 6.000: [0.125, 0.188, 0.250, 0.280, 0.312, 0.375, 0.500],
               6.625: [0.125, 0.188, 0.250, 0.280, 0.312, 0.432, 0.500],
               7.000: [0.125, 0.188, 0.250, 0.312, 0.375, 0.500], 7.500: [0.188, 0.250, 0.312, 0.375, 0.500],
               8.625: [0.188, 0.250, 0.322, 0.375, 0.500, 0.625],
               10.000: [0.188, 0.250, 0.312, 0.375, 0.500, 0.625], 10.750: [0.250, 0.365, 0.500],
               12.750: [0.250, 0.375, 0.500], 14.000: [0.250, 0.312, 0.375, 0.500, 0.625],
               16.000: [0.250, 0.312, 0.375, 0.438, 0.500, 0.625], 18.000: [0.375, 0.500],
               20.000: [0.375, 0.500]}

_CSA_W = [2.5, 3.2, 3.8, 4.8, 6.4, 8.0, 9.5, 11, 13, 16]
_CSA_SQ = {25: (2.5, 3.2), 32: (2.5, 3.2), 38: (2.5, 4.8), 51: (3.2, 6.4), 64: (3.2, 6.4), 76: (3.2, 8.0),
           89: (3.2, 9.5), 102: (3.2, 13), 127: (4.8, 13), 152: (4.8, 13), 178: (4.8, 13), 203: (4.8, 13),
           254: (6.4, 13), 305: (6.4, 13), 356: (8.0, 16), 406: (9.5, 16)}
_CSA_RECT = [(51, 25), (76, 51), (89, 64), (102, 51), (102, 76), (127, 64), (127, 76), (152, 76), (152, 102),
             (178, 102), (178, 127), (203, 102), (203, 152), (254, 152), (254, 203), (305, 203), (356, 254),
             (406, 203), (406, 305)]
_CSA_ROUND = {26.7: (2.5, 3.2), 33.4: (2.5, 4.8), 42.2: (2.5, 4.8), 48.3: (2.5, 4.8), 60.3: (3.2, 6.4),
              73.0: (3.2, 6.4), 88.9: (3.2, 8.0), 102: (3.2, 8.0), 114: (3.2, 9.5), 127: (3.8, 9.5),
              141: (3.8, 9.5), 152: (3.8, 11), 168: (4.8, 13), 178: (4.8, 13), 194: (4.8, 13), 219: (4.8, 13),
              245: (6.4, 13), 273: (6.4, 13), 324: (6.4, 13), 356: (8.0, 16), 406: (8.0, 16)}

_EN_W = [2.0, 2.5, 3.0, 3.2, 3.6, 4.0, 5.0, 6.0, 6.3, 8.0, 10.0, 12.0, 12.5, 14.2, 16.0]
_EN_SQ = [20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 120, 140, 150, 160, 180, 200, 250, 260, 300, 350, 400]
_EN_RECT = [(40, 20), (40, 30), (50, 25), (50, 30), (60, 30), (60, 40), (70, 50), (80, 40), (80, 60), (90, 50),
            (100, 40), (100, 50), (100, 60), (100, 80), (120, 60), (120, 80), (140, 80), (150, 100), (160, 80),
            (180, 100), (200, 100), (200, 120), (250, 150), (300, 100), (300, 200), (400, 200), (450, 250),
            (500, 300)]
_EN_ROUND = [21.3, 26.9, 33.7, 42.4, 48.3, 60.3, 76.1, 88.9, 101.6, 114.3, 139.7, 168.3, 193.7, 219.1, 244.5, 273.0,
             323.9, 355.6, 406.4, 457.0, 508.0]
_EN_RW = [2.0, 2.3, 2.6, 2.9, 3.2, 3.6, 4.0, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0]


def _in(x) -> float:
    """'3-1/2' | 4 | Fraction -> inches (float)."""
    if isinstance(x, (int, float, Fraction)):
        return float(x)
    m = re.fullmatch(r"(\d+)(?:-(\d+)/(\d+))?", str(x))
    return int(m.group(1)) + (int(m.group(2)) / int(m.group(3)) if m.group(2) else 0)


def _fr_txt(f: Fraction | float | str) -> str:
    """Inches as AISC writes them: 3-1/2, 1/4, 4."""
    if isinstance(f, str):
        return f
    f = Fraction(f).limit_denominator(64)
    w, r = divmod(f.numerator, f.denominator)
    if not r:
        return str(w)
    return f"{w}-{r}/{f.denominator}" if w else f"{r}/{f.denominator}"


def corner_radii(standard: str, t: float, rule: str = "standard", custom: float = 0.0) -> tuple[float, float]:
    """(outside, inside) corner radius for a wall t."""
    if rule == "sharp":
        return 0.0, 0.0
    if rule == "custom":
        ro = max(0.0, float(custom))
        return ro, max(0.0, ro - t)
    if rule in ("2t", "1.5t", "2.5t", "3t"):
        ro = float(rule[:-1]) * t
        return ro, max(0.0, ro - t)
    if standard == "EN10210":
        return 1.5 * t, 1.0 * t
    if standard == "EN10219":
        k = 2.0 if t <= 6 else 2.5 if t <= 10 else 3.0
        return k * t, (k - 1) * t
    return 2.0 * t, 1.0 * t                         # AISC, CSA, custom 'standard'


def _mk(standard, shape, des, B, H, t_nom, t_des):
    ro, ri = (0.0, 0.0) if shape == "round" else corner_radii(standard, t_des)
    return HSS(standard, shape, des, round(B, 4), round(H, 4), round(t_nom, 4), round(t_des, 4), round(ro, 4),
               round(ri, 4))


def _build_tables():
    T = {s: [] for s in STANDARDS if s != "custom"}
    for b, lo, hi in _AISC_SQ:
        for w in _walls(F(lo), F(hi)):
            if 2 * float(w) >= _in(b) / 2.2:
                continue
            T["AISC"].append(_mk("AISC", "square", f"HSS{_fr_txt(b)}x{_fr_txt(b)}x{_fr_txt(w)}", _in(b) * IN,
                                 _in(b) * IN, float(w) * IN, 0.93 * float(w) * IN))
    for h, b, lo, hi in _AISC_RECT:
        for w in _walls(F(lo), F(hi)):
            if 2 * float(w) >= _in(b) / 2.2:
                continue
            T["AISC"].append(_mk("AISC", "rect", f"HSS{_fr_txt(h)}x{_fr_txt(b)}x{_fr_txt(w)}", _in(b) * IN,
                                 _in(h) * IN, float(w) * IN, 0.93 * float(w) * IN))
    for od, ws in _AISC_ROUND.items():
        for w in ws:
            T["AISC"].append(_mk("AISC", "round", f"HSS{od:.3f}x{w:.3f}", od * IN, od * IN, w * IN, 0.93 * w * IN))
    for b, (lo, hi) in _CSA_SQ.items():
        for w in _CSA_W:
            if lo <= w <= hi:
                T["CSA"].append(_mk("CSA", "square", f"HSS {b}x{b}x{w:g}", b, b, w, w))
    for h, b in _CSA_RECT:
        lo, hi = _CSA_SQ.get(b, (3.2, 13))
        for w in _CSA_W:
            if lo <= w <= hi and 2 * w < b / 2.2:
                T["CSA"].append(_mk("CSA", "rect", f"HSS {h}x{b}x{w:g}", b, h, w, w))
    for od, (lo, hi) in _CSA_ROUND.items():
        for w in _CSA_W:
            if lo <= w <= hi:
                T["CSA"].append(_mk("CSA", "round", f"HSS {od:g}x{w:g}", od, od, w, w))
    for std in ("EN10219", "EN10210"):
        for b in _EN_SQ:
            for w in _EN_W:
                if b / 40 <= w <= b / 6 + 1e-9 and (std == "EN10219" or b >= 40):
                    T[std].append(_mk(std, "square", f"SHS {b}x{b}x{w:g}", b, b, w, w))
        for h, b in _EN_RECT:
            for w in _EN_W:
                if h / 45 <= w <= b / 5 + 1e-9 and (std == "EN10219" or b >= 30):
                    T[std].append(_mk(std, "rect", f"RHS {h}x{b}x{w:g}", b, h, w, w))
        for od in _EN_ROUND:
            for w in _EN_RW:
                if od / 50 <= w <= od / 6 + 1e-9:
                    T[std].append(_mk(std, "round", f"CHS {od:g}x{w:g}", od, od, w, w))
    return T


TABLES = _build_tables()
_BY_DES: dict = {}


def _key(des: str) -> str:
    return re.sub(r"\s+", "", des.upper()).replace("×", "X").replace("(CUSTOM)", "")


def sizes(standard: str, shape: str | None = None, query: str = "") -> list[HSS]:
    rows = TABLES.get(standard, [])
    q = _key(query)
    return [h for h in rows if (shape is None or h.shape == shape) and (not q or q in _key(h.designation))]


def lookup(standard: str, designation: str) -> HSS:
    key = _key(designation)
    bare = key[3:] if key[:3] in ("HSS", "SHS", "RHS", "CHS") else key
    for k in (key, "HSS" + bare, "SHS" + bare, "RHS" + bare, "CHS" + bare):
        h = _BY_DES.get((standard, k))
        if h is not None:
            return h
    close = [h.designation for h in sizes(standard, None, bare.split("X")[0])][:10]
    raise KeyError(f"No {standard} section '{designation}'" + (f". Close: {close}" if close else ""))


_BY_DES.update({(s, _key(h.designation)): h for s, rows in TABLES.items() for h in rows})


def custom(shape: str, B: float, H: float | None, t: float, corner_rule: str = "2t", corner_radius: float = 0.0,
           label: str | None = None) -> HSS:
    """Non-standard section: any size and wall, corner radius by rule (2t, 1.5t … custom, sharp)."""
    if shape not in SHAPES:
        raise ValueError(f"shape: {SHAPES}")
    H = B if shape in ("square", "round") or not H else H
    if t <= 0 or B <= 0 or H <= 0:
        raise ValueError("Sizes and wall must be > 0")
    if shape == "round":
        if t >= B / 2:
            raise ValueError("Wall too thick for the OD")
        return HSS("custom", "round", label or f"HSS {B:g}x{t:g} (custom)", B, B, t, t, 0.0, 0.0)
    if 2 * t >= min(B, H):
        raise ValueError("Wall too thick for the tube size")
    ro, ri = corner_radii("custom", t, "2t" if corner_rule == "standard" else corner_rule, corner_radius)
    if ro > min(B, H) / 2:
        raise ValueError("Corner radius larger than half the tube width")
    return HSS("custom", shape, label or f"HSS {H:g}x{B:g}x{t:g} (custom)", B, H, t, t, ro, ri)


def resolve(standard: str, designation: str = "", shape: str = "square", B: float = 0, H: float = 0,
            t: float = 0, corner_rule: str = "standard", corner_radius: float = 0.0) -> HSS:
    """A standard section by designation, optionally with a different corner rule; or a custom one."""
    if standard != "custom" and designation:
        h = lookup(standard, designation)
        if corner_rule not in ("standard", "") and h.shape != "round":
            ro, ri = corner_radii(standard, h.t, corner_rule, corner_radius)
            h = HSS(**{**h.to_dict(), "ro": ro, "ri": ri})
        return h
    return custom(shape, B, H, t, corner_rule, corner_radius)


# --------------------------------------------------------------- geometry ---

def _rounded_rect(B, H, r, n=10):
    """Outline of a B×H rectangle with corner radius r, CCW, centred."""
    if r <= 1e-9:
        return [(B / 2, -H / 2), (B / 2, H / 2), (-B / 2, H / 2), (-B / 2, -H / 2)]
    pts = []
    for cx, cy, a0 in ((B / 2 - r, -H / 2 + r, -90), (B / 2 - r, H / 2 - r, 0), (-B / 2 + r, H / 2 - r, 90),
                       (-B / 2 + r, -H / 2 + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _poly_props(P):
    """(A, Sx_y (∫y dA), Sx_x, Ixx, Iyy) about the origin for a closed polygon (CCW positive)."""
    A = Qx = Qy = Ixx = Iyy = 0.0
    for i in range(len(P)):
        x0, y0 = P[i]
        x1, y1 = P[(i + 1) % len(P)]
        c = x0 * y1 - x1 * y0
        A += c
        Qx += (y0 + y1) * c
        Qy += (x0 + x1) * c
        Ixx += (y0 * y0 + y0 * y1 + y1 * y1) * c
        Iyy += (x0 * x0 + x0 * x1 + x1 * x1) * c
    return A / 2, Qx / 6, Qy / 6, Ixx / 12, Iyy / 12


def _clip_half(P, axis):
    """Part of a convex polygon with coordinate[axis] ≥ 0."""
    out = []
    for i in range(len(P)):
        a, b = P[i], P[(i + 1) % len(P)]
        ia, ib = a[axis] >= 0, b[axis] >= 0
        if ia:
            out.append(a)
        if ia != ib:
            s = a[axis] / (a[axis] - b[axis])
            out.append((a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s))
    return out


def properties(h: HSS, wall: float | None = None) -> dict:
    """Section properties (mm, mm², mm⁴, mm³, kg/m) — x axis = strong axis (about the H depth)."""
    t = h.t if wall is None else wall
    B, H = h.B, h.H
    if h.shape == "round":
        D, d = B, B - 2 * t
        A = math.pi / 4 * (D ** 2 - d ** 2)
        I = math.pi / 64 * (D ** 4 - d ** 4)
        S = I / (D / 2)
        Z = (D ** 3 - d ** 3) / 6
        J = 2 * I
        out = {"A": A, "Ix": I, "Iy": I, "Sx": S, "Sy": S, "Zx": Z, "Zy": Z, "rx": math.sqrt(I / A),
               "ry": math.sqrt(I / A), "J": J, "C": 2 * S, "D/t": D / t}
    else:
        ro = h.ro
        ri = max(0.0, ro - t)
        O = _rounded_rect(B, H, ro)
        I_ = _rounded_rect(B - 2 * t, H - 2 * t, ri)
        Ao, _, _, Ixo, Iyo = _poly_props(O)
        Ai, _, _, Ixi, Iyi = _poly_props(I_)
        A, Ix, Iy = Ao - Ai, Ixo - Ixi, Iyo - Iyi
        Zx = 2 * (_poly_props(_clip_half(O, 1))[1] - _poly_props(_clip_half(I_, 1))[1])
        Zy = 2 * (_poly_props(_clip_half(O, 0))[2] - _poly_props(_clip_half(I_, 0))[2])
        # torsion (AISC Manual, closed section with rounded midline): J = 4 Ap² t / p ; C = 2 t Ap − 4.5(4−π)t³
        rm = max(ro - t / 2, 0.0)
        Ap = (B - t) * (H - t) - (4 - math.pi) * rm ** 2
        p = 2 * (B - t) + 2 * (H - t) - (8 - 2 * math.pi) * rm
        J = 4 * Ap ** 2 * t / p
        C = 2 * (B - t) * (H - t) * t - 4.5 * (4 - math.pi) * t ** 3
        out = {"A": A, "Ix": Ix, "Iy": Iy, "Sx": Ix / (H / 2), "Sy": Iy / (B / 2), "Zx": Zx, "Zy": Zy,
               "rx": math.sqrt(Ix / A), "ry": math.sqrt(Iy / A), "J": J, "C": C,
               "b/t": (B - 3 * t) / t, "h/t": (H - 3 * t) / t}
    # nominal mass uses the nominal wall (AISC practice); for metric standards t_nom = t
    if wall is None and h.t_nom != h.t:
        A_nom = properties(HSS(**{**h.to_dict(), "t": h.t_nom,
                                  "ro": h.ro * h.t_nom / h.t if h.t else h.ro}), wall=h.t_nom)["A"]
    else:
        A_nom = out["A"]
    out["mass_per_m"] = A_nom * 1e-6 * STEEL_DENSITY            # kg/m
    out["area_nominal"] = A_nom
    out["surface_per_m"] = (perimeter(h) * 1000)                # mm² of painted outside surface per m
    return out


def perimeter(h: HSS) -> float:
    if h.shape == "round":
        return math.pi * h.B
    return 2 * (h.B + h.H) - (8 - 2 * math.pi) * h.ro


PROP_QUANTITY = {"A": "area", "area_nominal": "area", "Ix": "area_moment", "Iy": "area_moment", "J": "area_moment",
                 "Sx": "section_modulus", "Sy": "section_modulus", "Zx": "section_modulus",
                 "Zy": "section_modulus", "C": "section_modulus", "rx": "length", "ry": "length",
                 "mass_per_m": "linear_density", "surface_per_m": None, "b/t": None, "h/t": None, "D/t": None}
PROP_LABEL = {"A": "Area (design)", "area_nominal": "Area (nominal)", "Ix": "Ix (strong)", "Iy": "Iy (weak)",
              "Sx": "Sx elastic", "Sy": "Sy elastic", "Zx": "Zx plastic", "Zy": "Zy plastic", "rx": "rx",
              "ry": "ry", "J": "J torsion", "C": "C torsion modulus", "mass_per_m": "Weight / length",
              "surface_per_m": "Paint area / m (mm²)", "b/t": "b/t", "h/t": "h/t", "D/t": "D/t"}


def profile(h: HSS):
    """build123d Face/Sketch of the section, centred, width B along local X and depth H along local Y."""
    from build123d import Circle, Rectangle, RectangleRounded
    if h.shape == "round":
        return Circle(h.B / 2) - Circle(h.B / 2 - h.t)
    ro, ri = h.ro, max(0.0, h.ro - h.t)
    outer = RectangleRounded(h.B, h.H, ro) if ro > 1e-6 else Rectangle(h.B, h.H)
    inner = RectangleRounded(h.B - 2 * h.t, h.H - 2 * h.t, ri) if ri > 1e-6 else \
        Rectangle(h.B - 2 * h.t, h.H - 2 * h.t)
    return outer - inner


def describe(h: HSS) -> str:
    if h.shape == "round":
        return f"{h.designation}: OD {h.B:.2f} × t {h.t:.2f} mm"
    return f"{h.designation}: {h.H:.2f} × {h.B:.2f} × t {h.t:.2f} mm, ro {h.ro:.2f}"
