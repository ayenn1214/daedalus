"""G-code reader for back-plotting any program (from SHOPQUOTE's simulator, extended).

Understands G0/G1/G2/G3 (I/J/K centre or R radius, XY plane), G17, G20/G21 / G70/G71 (converted to mm),
G90/G91, G4 dwell, every drilling / tapping / boring canned cycle (G73 G74 G76 G81 G82 G83 G84 G85 G86 G89 with
Q pecks, P dwell and G98/G99 retract), Siemens MCALL CYCLE81–89, G80, F feed, () and ; comments, N numbers.
Gives the tool path as segments plus distance/time statistics.
"""
from __future__ import annotations

import math
import re

WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
CANNED = (73, 74, 76, 81, 82, 83, 84, 85, 86, 89)
SIEMENS = re.compile(r"MCALL\s+CYCLE(8[1-9])\s*\(([^)]*)\)", re.I)
CHIP_BREAK = 0.5


def _arc_center_r(p0, p1, r, cw):
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    d = math.hypot(dx, dy) or 1e-9
    h = math.sqrt(max(r * r - d * d / 4, 0.0))
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    ux, uy = -dy / d, dx / d
    c1, c2 = (mx + ux * h, my + uy * h), (mx - ux * h, my - uy * h)

    def sweep(c):
        a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
        a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
        dl = a1 - a0
        if cw:
            while dl > 0:
                dl -= 2 * math.pi
        else:
            while dl < 0:
                dl += 2 * math.pi
        return abs(dl)
    s1, s2 = sweep(c1), sweep(c2)
    if r >= 0:
        return c1 if s1 <= s2 else c2
    return c1 if s1 > s2 else c2


def _arc_points(p0, p1, c, cw, per_circle=72):
    r0 = math.hypot(p0[0] - c[0], p0[1] - c[1])
    r1 = math.hypot(p1[0] - c[0], p1[1] - c[1])
    a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
    a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
    dl = a1 - a0
    if cw:
        while dl > 0:
            dl -= 2 * math.pi
        if abs(dl) < 1e-9:
            dl = -2 * math.pi
    else:
        while dl < 0:
            dl += 2 * math.pi
        if abs(dl) < 1e-9:
            dl = 2 * math.pi
    n = max(2, math.ceil(abs(dl) / (2 * math.pi) * per_circle))
    out = []
    for i in range(1, n + 1):
        t = i / n
        a = a0 + dl * t
        r = r0 + (r1 - r0) * t
        out.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a), p0[2] + (p1[2] - p0[2]) * t))
    return out


def parse(text: str) -> dict:
    """{'segments': [(kind, p0, p1, feed, line)], 'unit', 'bounds', 'lines', 'warnings'} in mm."""
    scale = 1.0
    absolute = True
    modal = None
    cycle = None
    retract_initial = True
    feed = 0.0
    pos = [0.0, 0.0, 0.0]
    segs = []
    warn = []
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    lines = text.splitlines()

    def add(kind, a, b, n):
        segs.append((kind, tuple(a), tuple(b), feed, n))
        for i in range(3):
            lo[i] = min(lo[i], b[i])
            hi[i] = max(hi[i], b[i])

    dwell_total = 0.0
    fanuc_ms = True                                           # P without a decimal point = milliseconds

    def drill(xy, cyc):
        """Draw one canned cycle at xy from the current height."""
        nonlocal pos, dwell_total
        code, z, r, q, p = cyc["code"], cyc["z"], cyc["r"], cyc.get("q") or 0.0, cyc.get("p") or 0.0
        start_z = pos[2]
        add("rapid", pos, (xy[0], xy[1], pos[2]), n)
        add("rapid", (xy[0], xy[1], pos[2]), (xy[0], xy[1], r), n)
        top = r
        if code in (83, 73) and q > 0:
            zc = top
            while zc > z + 1e-9:
                znew = max(z, zc - q)
                add("feed", (xy[0], xy[1], zc + (0 if zc == top else 0)), (xy[0], xy[1], znew), n)
                zc = znew
                if zc > z + 1e-9:
                    up = r if code == 83 else zc + CHIP_BREAK
                    add("rapid", (xy[0], xy[1], zc), (xy[0], xy[1], up), n)
                    add("rapid", (xy[0], xy[1], up), (xy[0], xy[1], zc + (CHIP_BREAK if code == 83 else 0)), n)
                    if code == 83:
                        add("feed", (xy[0], xy[1], zc + CHIP_BREAK), (xy[0], xy[1], zc), n)
        else:
            add("feed", (xy[0], xy[1], r), (xy[0], xy[1], z), n)
        dwell_total += p
        back = cyc["init"] if cyc["g98"] else r
        if code in (84, 74, 85, 89):
            add("feed", (xy[0], xy[1], z), (xy[0], xy[1], r), n)
            if back != r:
                add("rapid", (xy[0], xy[1], r), (xy[0], xy[1], back), n)
        else:
            add("rapid", (xy[0], xy[1], z), (xy[0], xy[1], back), n)
        pos = [xy[0], xy[1], back if back is not None else start_z]

    for n, raw in enumerate(lines):
        sm = SIEMENS.search(raw)
        if sm:                                               # MCALL CYCLE8x(RTP, RFP, SDIS, DP, …)
            a_ = [x.strip() for x in sm.group(2).split(",")]

            def num(i, default=0.0):
                try:
                    return float(a_[i]) * scale if a_[i] else default
                except (IndexError, ValueError):
                    return default
            code = int(sm.group(1))
            rtp, rfp, sdis, dp = num(0), num(1), num(2), num(3)
            cyc = {"code": {81: 81, 82: 82, 83: 83, 84: 84, 85: 85, 86: 86, 87: 86, 88: 86, 89: 89}[code],
                   "z": dp, "r": rfp + sdis, "init": rtp, "g98": True, "q": 0.0, "p": 0.0}
            if code == 83:
                first = num(5, rfp)
                cyc["q"] = max(rfp - first, 0.0) or (rfp - dp)
                try:
                    cyc["code"] = 73 if int(float(a_[11] or 1)) == 0 else 83
                except (IndexError, ValueError):
                    pass
            if code in (82, 89):
                cyc["p"] = num(5) / scale if len(a_) > 5 else 0.0
            if code == 84:
                try:
                    if float(a_[8] or 0) < 0:
                        cyc["code"] = 74
                except (IndexError, ValueError):
                    pass
            cycle = cyc
            modal = None
            continue
        if re.match(r"^\s*(N\d+\s*)?MCALL\s*$", raw, re.I):
            cycle = None
            continue
        line = re.sub(r"\([^)]*\)", "", raw)
        line = line.split(";", 1)[0].strip()
        if not line or line == "%":
            continue
        g, prm, raw_p = [], {}, None
        for letter, val in WORD.findall(line):
            L = letter.upper()
            v = float(val)
            if L == "G":
                g.append(round(v, 1))
            elif L == "P":
                prm["P"] = v
                raw_p = val
            elif L not in "MNOTSD":
                prm[L] = v
        if 20 in g or 70 in g:
            scale = 25.4
        if 21 in g or 71 in g:
            scale = 1.0
        if 90 in g:
            absolute = True
        if 91 in g:
            absolute = False
        if "F" in prm:
            feed = prm["F"] * scale
        if 80 in g:
            cycle = None
        if 4 in g:                                           # G4 dwell (P / X seconds or ms)
            v = prm.get("P", prm.get("X", 0.0))
            dwell_total += (v / 1000 if raw_p and "." not in raw_p and fanuc_ms and v >= 10 else v)
            continue
        canned = [int(x) for x in g if int(x) in CANNED]
        if canned:
            cycle = {"code": canned[-1], "z": None, "r": None, "q": None, "p": 0.0, "init": pos[2],
                     "g98": 99 not in g}
            modal = None
        elif cycle is not None and (98 in g or 99 in g):
            cycle["g98"] = 98 in g
        motion = [x for x in g if x in (0, 1, 2, 3)]
        if motion:
            modal = int(motion[-1])
            cycle = None
        if any(x in g for x in (28, 53)):                # machine-home moves: skip, they'd distort the plot
            continue
        has = any(k in prm for k in "XYZ")
        tgt = list(pos)
        for i, k in enumerate("XYZ"):
            if k in prm:
                tgt[i] = prm[k] * scale if absolute else pos[i] + prm[k] * scale
        if cycle is not None:
            if "Z" in prm:
                cycle["z"] = prm["Z"] * scale if absolute else (cycle["r"] or pos[2]) + prm["Z"] * scale
            if "R" in prm:
                cycle["r"] = prm["R"] * scale if absolute else cycle["init"] + prm["R"] * scale
            if "Q" in prm and cycle["code"] in (73, 83):
                cycle["q"] = abs(prm["Q"]) * scale
            if "P" in prm:
                cycle["p"] = prm["P"] / 1000 if raw_p and "." not in raw_p else prm["P"]
            if not ("X" in prm or "Y" in prm or "Z" in prm):
                continue
            if cycle["z"] is None or cycle["r"] is None:
                warn.append(f"line {n + 1}: drilling cycle without Z/R")
                continue
            drill((tgt[0], tgt[1]), cycle)
            continue
        if modal is None or not has:
            continue
        if modal == 0:
            add("rapid", pos, tgt, n)
        elif modal == 1:
            add("feed", pos, tgt, n)
        else:
            cw = modal == 2
            c = None
            if "I" in prm or "J" in prm:
                c = (pos[0] + prm.get("I", 0.0) * scale, pos[1] + prm.get("J", 0.0) * scale)
            elif "R" in prm:
                c = _arc_center_r(pos, tgt, prm["R"] * scale, cw)
            if c is None:
                add("feed", pos, tgt, n)
            else:
                prev = tuple(pos)
                for p in _arc_points(pos, tgt, c, cw):
                    add("feed", prev, p, n)
                    prev = p
        pos = tgt
    bounds = (lo, hi) if segs else ([0, 0, 0], [0, 0, 0])
    return {"segments": segs, "unit": "in" if scale != 1.0 else "mm", "bounds": bounds, "lines": len(lines),
            "warnings": warn, "dwell_min": dwell_total / 60}


def stats(parsed: dict, rapid=5000.0) -> dict:
    rd = fd = rt = ft = 0.0
    for kind, a, b, f, _n in parsed["segments"]:
        d = math.dist(a, b)
        if kind == "rapid":
            rd += d
            rt += d / rapid
        else:
            fd += d
            ft += d / f if f > 0 else 0.0
    lo, hi = parsed["bounds"]
    dw = parsed.get("dwell_min", 0.0)
    return {"rapid_mm": rd, "feed_mm": fd, "rapid_min": rt, "feed_min": ft, "dwell_min": dw,
            "total_min": rt + ft + dw,
            "segments": len(parsed["segments"]), "extent_mm": [hi[i] - lo[i] for i in range(3)],
            "z_min": lo[2], "z_max": hi[2]}


def timeline(parsed: dict, rapid=5000.0):
    """Cumulative time (min) at the end of each segment, for playback."""
    t, out = 0.0, []
    for kind, a, b, f, _n in parsed["segments"]:
        d = math.dist(a, b)
        t += d / rapid if kind == "rapid" else (d / f if f > 0 else 0.0)
        out.append(t)
    return out
