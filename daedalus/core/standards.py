"""Standard hardware data: metric (ISO) and inch (UNC) fasteners, deep-groove ball bearings,
hole sizes for the hole wizard, and simplified 3D models for the parts library. Qt-free.

Thread forms aren't modelled (cosmetic threads carry the spec); geometry is nominal.
All dimensions mm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

IN = 25.4


@dataclass(frozen=True)
class Fastener:
    size: str
    d: float                 # nominal (major) diameter
    pitch: float             # mm (metric) — TPI kept in the name for inch
    tap: float               # tap drill
    clear_close: float
    clear_normal: float
    clear_loose: float
    shcs_head: float         # socket head cap screw head Ø
    shcs_k: float            # SHCS head height
    socket: float            # hex key size
    cbore_d: float           # counterbore Ø for SHCS
    cbore_depth: float
    hex_af: float            # hex bolt/nut across flats
    hex_k: float             # hex bolt head height
    nut_m: float             # nut height
    washer_id: float
    washer_od: float
    washer_t: float
    csk_head: float          # flat-head (countersunk) head Ø
    system: str = "ISO"


def _iso(size, p, tap, cc, cn, cl, dk, k, s, cbd, cbh, af, hk, m, wi, wo, wt, csk):
    d = float(size[1:])
    return Fastener(size, d, p, tap, cc, cn, cl, dk, k, s, cbd, cbh, af, hk, m, wi, wo, wt, csk, "ISO")


METRIC = {f.size: f for f in [
    _iso("M3", 0.5, 2.5, 3.2, 3.4, 3.6, 5.5, 3, 2.5, 6.5, 3.4, 5.5, 2.0, 2.4, 3.2, 7, 0.5, 6.72),
    _iso("M4", 0.7, 3.3, 4.3, 4.5, 4.8, 7, 4, 3, 8, 4.4, 7, 2.8, 3.2, 4.3, 9, 0.8, 8.96),
    _iso("M5", 0.8, 4.2, 5.3, 5.5, 5.8, 8.5, 5, 4, 10, 5.4, 8, 3.5, 4.7, 5.3, 10, 1.0, 11.2),
    _iso("M6", 1.0, 5.0, 6.4, 6.6, 7.0, 10, 6, 5, 11, 6.5, 10, 4.0, 5.2, 6.4, 12, 1.6, 13.44),
    _iso("M8", 1.25, 6.8, 8.4, 9.0, 10.0, 13, 8, 6, 15, 8.6, 13, 5.3, 6.8, 8.4, 16, 1.6, 17.92),
    _iso("M10", 1.5, 8.5, 10.5, 11.0, 12.0, 16, 10, 8, 18, 10.8, 16, 6.4, 8.4, 10.5, 20, 2.0, 22.4),
    _iso("M12", 1.75, 10.2, 13.0, 13.5, 14.5, 18, 12, 10, 20, 13.0, 18, 7.5, 10.8, 13, 24, 2.5, 26.88),
    _iso("M16", 2.0, 14.0, 17.0, 17.5, 18.5, 24, 16, 14, 26, 17.5, 24, 10.0, 14.8, 17, 30, 3.0, 33.6),
    _iso("M20", 2.5, 17.5, 21.0, 22.0, 24.0, 30, 20, 17, 33, 21.5, 30, 12.5, 18.0, 21, 37, 3.0, 40.32),
    _iso("M24", 3.0, 21.0, 25.0, 26.0, 28.0, 36, 24, 19, 40, 25.5, 36, 15.0, 21.5, 25, 44, 4.0, 48.0),
]}


def _unc(size, d, tpi, tap, cc, cf, dk, k, s, cbd, cbh, af, hk, m, wi, wo, wt, csk):
    i = IN
    return Fastener(size, d * i, i / tpi, tap * i, cc * i, cf * i, cf * i + 0.4, dk * i, k * i, s * i, cbd * i,
                    cbh * i, af * i, hk * i, m * i, wi * i, wo * i, wt * i, csk * i, "UNC")


INCH = {f.size: f for f in [
    _unc("#4-40", .112, 40, .089, .116, .1285, .183, .112, 3 / 32, .219, .125, .25, .08, .094, .125, .25, .022, .225),
    _unc("#6-32", .138, 32, .1065, .144, .1495, .226, .138, 7 / 64, .281, .151, .3125, .1, .109, .156, .312, .035, .279),
    _unc("#8-32", .164, 32, .136, .1695, .177, .27, .164, 9 / 64, .312, .177, .344, .11, .125, .188, .375, .049, .332),
    _unc("#10-24", .19, 24, .1495, .196, .201, .312, .19, 5 / 32, .375, .203, .375, .12, .13, .219, .5, .049, .385),
    _unc("1/4-20", .25, 20, .201, .257, .266, .375, .25, 3 / 16, .438, .265, .4375, .163, .219, .281, .625, .065, .507),
    _unc("5/16-18", .3125, 18, .257, .323, .332, .469, .3125, 1 / 4, .531, .328, .5, .211, .266, .344, .688, .065, .635),
    _unc("3/8-16", .375, 16, .3125, .386, .397, .5625, .375, 5 / 16, .625, .39, .5625, .243, .328, .406, .812, .065, .762),
    _unc("1/2-13", .5, 13, .4219, .5156, .5312, .75, .5, 3 / 8, .8125, .515, .75, .323, .438, .531, 1.062, .095, 1.016),
    _unc("5/8-11", .625, 11, .5312, .6406, .6562, .9375, .625, 1 / 2, 1.0, .64, .9375, .403, .547, .656, 1.312, .095, 1.27),
    _unc("3/4-10", .75, 10, .6562, .7656, .7812, 1.125, .75, 5 / 8, 1.25, .765, 1.125, .483, .641, .812, 1.469, .134, 1.524),
]}

ALL = {**METRIC, **INCH}

METRIC_LENGTHS = [6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 80, 90, 100, 110, 120]
INCH_LENGTHS = [x / 8 for x in range(2, 9)] + [x / 4 for x in range(5, 13)] + [3.5, 4.0]   # inches

# deep-groove ball bearings: designation -> (bore d, outer D, width B)
BEARINGS = {
    "625": (5, 16, 5), "626": (6, 19, 6), "608": (8, 22, 7), "6800": (10, 19, 5), "6801": (12, 21, 5),
    "6802": (15, 24, 5), "6000": (10, 26, 8), "6001": (12, 28, 8), "6002": (15, 32, 9), "6003": (17, 35, 10),
    "6004": (20, 42, 12), "6005": (25, 47, 12), "6006": (30, 55, 13), "6200": (10, 30, 9), "6201": (12, 32, 10),
    "6202": (15, 35, 11), "6203": (17, 40, 12), "6204": (20, 47, 14), "6205": (25, 52, 15), "6206": (30, 62, 16),
}

KINDS = {
    "socket head cap screw": "ISO 4762 / ASME B18.3", "hex bolt": "ISO 4017 / ASME B18.2.1",
    "flat head screw": "ISO 10642 / ASME B18.3", "hex nut": "ISO 4032 / ASME B18.2.2",
    "washer": "ISO 7089 / SAE", "ball bearing": "deep groove, 2RS",
}


def fastener(size: str) -> Fastener:
    if size not in ALL:
        raise KeyError(f"Unknown fastener size '{size}'. Sizes: {list(ALL)}")
    return ALL[size]


def hole_size(size: str, fit: str = "normal") -> float:
    """fit: tap | close | normal | loose."""
    f = fastener(size)
    return {"tap": f.tap, "close": f.clear_close, "normal": f.clear_normal, "loose": f.clear_loose}[fit]


def designation(kind: str, size: str = "", length: float = 0.0) -> str:
    if kind == "ball bearing":
        return f"Ball bearing {size}-2RS"
    f = fastener(size)
    std = "ISO" if f.system == "ISO" else "ASME"
    names = {"socket head cap screw": "SHCS", "hex bolt": "Hex bolt", "flat head screw": "FHCS",
             "hex nut": "Hex nut", "washer": "Washer"}
    ln = ""
    if length:
        ln = f" x {length:g}" if f.system == "ISO" else f" x {length / IN:g}\""
    return f"{names[kind]} {size}{ln} ({std})"


def unit_cost(kind: str, size: str, length: float = 0.0) -> float:
    """Rough catalogue price, zinc-plated steel (for BOM costing)."""
    if kind == "ball bearing":
        d, D, B = BEARINGS[size]
        return round(2.0 + 0.004 * D * D * B / 10, 2)
    f = fastener(size)
    base = {"socket head cap screw": 0.08, "hex bolt": 0.06, "flat head screw": 0.09, "hex nut": 0.02,
            "washer": 0.01}[kind]
    return round(base * (f.d / 6) ** 2 * (1 + (length or f.d) / 40) + 0.03, 2)


# ------------------------------------------------------------------ models ---

def model(kind: str, size: str, length: float = 0.0):
    """Simplified solid. Screws/bolts: underside of the head at z=0, shank along −Z.
    Nuts/washers: bottom face at z=0, axis +Z. Bearings: centred, axis Z, faces at ±B/2."""
    from build123d import Align, Cone, Cylinder, Pos, RegularPolygon, extrude
    if kind == "ball bearing":
        d, D, B = BEARINGS[size]
        ring = Cylinder(D / 2, B) - Cylinder(d / 2, B)
        groove = Pos(0, 0, B / 2) * (Cylinder((D + d) / 4 + (D - d) / 8, 0.4) - Cylinder((D + d) / 4 - (D - d) / 8, 0.4))
        return ring - groove - Pos(0, 0, -B) * groove
    f = fastener(size)
    up = (Align.CENTER, Align.CENTER, Align.MIN)
    down = (Align.CENTER, Align.CENTER, Align.MAX)
    if kind in ("socket head cap screw", "hex bolt", "flat head screw"):
        if length <= 0:
            raise ValueError("Screw length needed")
        shank = Cylinder(f.d / 2, length, align=down)
        if kind == "socket head cap screw":
            head = Cylinder(f.shcs_head / 2, f.shcs_k, align=up)
            sock = Pos(0, 0, f.shcs_k) * extrude(RegularPolygon(f.socket / math.sqrt(3), 6), -f.shcs_k * 0.55)
            return head + shank - sock
        if kind == "hex bolt":
            head = extrude(RegularPolygon(f.hex_af / math.sqrt(3), 6), f.hex_k)
            return head + shank
        h = (f.csk_head - f.d) / 2                       # 90° countersunk head: top flush at z=0
        head = Pos(0, 0, -h) * Cone(f.d / 2, f.csk_head / 2, h, align=up)
        body = Pos(0, 0, -h) * Cylinder(f.d / 2, max(length - h, f.d), align=down)
        sock = extrude(RegularPolygon(f.socket / math.sqrt(3), 6), -h * 0.6)
        return head + body - sock
    if kind == "hex nut":
        nut = extrude(RegularPolygon(f.hex_af / math.sqrt(3), 6), f.nut_m)
        return nut - Cylinder(f.d / 2, f.nut_m, align=up)
    if kind == "washer":
        return Cylinder(f.washer_od / 2, f.washer_t, align=up) - Cylinder(f.washer_id / 2, f.washer_t, align=up)
    raise ValueError(f"Unknown part kind '{kind}'")
