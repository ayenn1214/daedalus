"""Doodle → constrained sketch.

Takes freehand strokes (any units, y up), recognises each as a circle or a
chain of lines and arcs, joins endpoints that were meant to meet, adds
horizontal/vertical relations where the hand was close, scales the result to
a target size and returns sketch2d geometry the solver tidies up.

No ML: least-squares circle fits, Ramer–Douglas–Peucker corners and simple
heuristics, so it is predictable and runs offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import sketch2d as sk

Pt = tuple[float, float]


@dataclass
class DoodleOptions:
    size_mm: float = 100.0          # longest side of the result
    hv_deg: float = 7.0             # lines within this of horizontal/vertical get H/V
    snap_px: float = 12.0           # endpoint merge distance in drawing units (screen pixels)
    noise_px: float = 2.5           # hand noise floor in drawing units
    round_mm: float = 1.0           # round coordinates to this (0 = off)
    centre: bool = True             # put the drawing's centre on the origin
    exact_size: bool = True         # rescale after straightening so the longest side is exactly size_mm


# ------------------------------------------------------------------ maths ---

def _len(poly):
    return sum(math.dist(a, b) for a, b in zip(poly, poly[1:]))


def _resample(poly, step):
    if len(poly) < 2:
        return list(poly)
    out = [poly[0]]
    acc = 0.0
    for a, b in zip(poly, poly[1:]):
        d = math.dist(a, b)
        while acc + d >= step and d > 0:
            t = (step - acc) / d
            a = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            out.append(a)
            d = math.dist(a, b)
            acc = 0.0
        acc += d
    if math.dist(out[-1], poly[-1]) > step * 0.3:
        out.append(poly[-1])
    return out


def fit_circle(pts):
    """Kåsa algebraic fit. Returns (cx, cy, r, rms residual)."""
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts) / n
    sy = sum(p[1] for p in pts) / n
    u = [p[0] - sx for p in pts]
    v = [p[1] - sy for p in pts]
    suu = sum(a * a for a in u)
    svv = sum(b * b for b in v)
    suv = sum(a * b for a, b in zip(u, v))
    suuu = sum(a ** 3 for a in u)
    svvv = sum(b ** 3 for b in v)
    suvv = sum(a * b * b for a, b in zip(u, v))
    svuu = sum(b * a * a for a, b in zip(u, v))
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    rhs1 = 0.5 * (suuu + suvv)
    rhs2 = 0.5 * (svvv + svuu)
    uc = (rhs1 * svv - rhs2 * suv) / det
    vc = (rhs2 * suu - rhs1 * suv) / det
    cx, cy = uc + sx, vc + sy
    r = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    rms = math.sqrt(sum((math.dist(p, (cx, cy)) - r) ** 2 for p in pts) / n)
    return cx, cy, r, rms


def _turning(pts, c):
    """Signed total angle swept by pts around centre c (radians)."""
    tot = 0.0
    for a, b in zip(pts, pts[1:]):
        a0 = math.atan2(a[1] - c[1], a[0] - c[0])
        a1 = math.atan2(b[1] - c[1], b[0] - c[0])
        d = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        tot += d
    return tot


def _rdp(pts, eps):
    if len(pts) < 3:
        return [0, len(pts) - 1]
    a, b = pts[0], pts[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        p = pts[i]
        d = abs(dy * (p[0] - a[0]) - dx * (p[1] - a[1])) / L if L > 1e-12 else math.dist(p, a)
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [0, len(pts) - 1]
    left = _rdp(pts[: idx + 1], eps)
    right = _rdp(pts[idx:], eps)
    return left[:-1] + [i + idx for i in right]


def _seg_dir(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _smooth(pts, m, closed, passes=2):
    """Moving average over ±m samples (endpoints pinned for open strokes)."""
    if m < 1:
        return pts
    n = len(pts)
    for _ in range(passes):
        out = []
        for i in range(n):
            if not closed and (i < m or i >= n - m):
                out.append(pts[i])
                continue
            xs = ys = 0.0
            for j in range(i - m, i + m + 1):
                q = pts[j % n]
                xs += q[0]
                ys += q[1]
            out.append((xs / (2 * m + 1), ys / (2 * m + 1)))
        pts = out
    return pts


def _turn_at(pts, i, k, closed):
    n = len(pts)

    def P(j):
        return pts[j % n] if closed else pts[max(0, min(n - 1, j))]

    p0, p1, p2 = P(i - k), P(i), P(i + k)
    if p0 == p1 or p1 == p2:
        return 0.0
    return _wrap(_seg_dir(p1, p2) - _seg_dir(p0, p1))


def _segment(pts, closed, k_len=None):
    """Split a uniformly resampled stroke into ("line" | "arc", points) pieces.

    Two scales: a fine turning-angle window finds sharp corners (cuts); inside each
    piece a coarse window separates straight runs from steadily turning (arc) runs."""
    if closed and math.dist(pts[0], pts[-1]) < 1e-9:
        pts = pts[:-1]
    n = len(pts)
    if n < 6:
        return [("line", pts + ([pts[0]] if closed else []))]
    h = _len(pts) / max(n - 1, 1)
    k_len = k_len or 6 * h
    kc = max(1, round(0.3 * k_len / h))           # corner scale
    kl = max(2, round(k_len / h))                 # curvature scale

    # --- corners: sharp turns at the fine scale, non-maximum suppressed
    ac = [_turn_at(pts, i, kc, closed) for i in range(n)]
    lo, hi = (0, n) if closed else (kc, n - kc)
    cand = [i for i in range(lo, hi) if abs(ac[i]) > math.radians(38)]
    corners = []
    for i in sorted(cand, key=lambda j: -abs(ac[j])):
        if all(min(abs(i - c), n - abs(i - c) if closed else abs(i - c)) > 2 * kc for c in corners):
            corners.append(i)
    corners.sort()

    # --- pieces between corners
    if closed:
        if not corners:
            return _split_piece(pts + [pts[0]], kl, closed_loop=True)
        spans = [(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))]
        pieces = []
        for a, b in spans:
            idx = list(range(a, b + 1)) if b > a else list(range(a, n)) + list(range(0, b + 1))
            pieces.append([pts[i] for i in idx])
    else:
        cuts = [0] + corners + [n - 1]
        pieces = [pts[a: b + 1] for a, b in zip(cuts, cuts[1:])]
    out = []
    for pc in pieces:
        out += _split_piece(pc, kl)
    return out


def _split_piece(pc, kl, closed_loop=False):
    """One corner-free piece → straight / arc runs, using the coarse turning window."""
    n = len(pc)
    if n < 6:
        return [("line", pc)]
    k = max(2, min(kl, (n - 1) // 3))
    a = [_turn_at(pc, i, k, closed_loop) for i in range(n)]
    tau = math.radians(7)
    curved = [abs(x) > tau and (closed_loop or k <= i < n - k) for i, x in enumerate(a)]
    # runs of curved samples, same turning direction
    runs, i = [], 0
    while i < n:
        if curved[i]:
            j = i
            while j + 1 < n and curved[j + 1] and (a[j + 1] > 0) == (a[i] > 0):
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    arcs = []
    for r0, r1 in runs:
        tot = sum(a[r0: r1 + 1]) / k
        if r1 - r0 + 1 >= 3 and abs(tot) > math.radians(18):
            # widen to the tangent points: the window smears each end by about k/2
            arcs.append((max(0, r0 - k // 2), min(n - 1, r1 + k // 2)))
    if closed_loop and not arcs:
        return [("arc", pc)]
    out, cur = [], 0
    for r0, r1 in arcs:
        if r0 - cur >= 2:
            out.append(("line", pc[cur: r0 + 1]))
        else:
            r0 = cur
        out.append(("arc", pc[r0: r1 + 1]))
        cur = r1
    if n - 1 - cur >= 1:
        if out and n - 1 - cur < 2:
            k_, run = out[-1]
            out[-1] = (k_, run + pc[cur + 1:])
        else:
            out.append(("line", pc[cur:]))
    return out or [("line", pc)]


def _cleanup(pieces, closed, diag, noise=0.0):
    """Merge near-collinear lines, absorb tiny pieces, and turn consistent
    bends made of several short lines into arcs."""
    def ang(run):
        return _seg_dir(run[0], run[-1])

    def join(a, b):
        return a + b[1:]

    def good_arc(run):
        fc = fit_circle(run)
        if not fc:
            return False
        cx, cy, r, rms = fc
        return rms < 0.05 * r and abs(_turning(run, (cx, cy))) > math.radians(25) and r < 4 * diag

    def straight(run):
        a, b = run[0], run[-1]
        L = math.dist(a, b)
        if L < 1e-9:
            return False
        dev = max(abs((b[1] - a[1]) * (q[0] - a[0]) - (b[0] - a[0]) * (q[1] - a[1])) / L for q in run)
        return dev < max(0.03 * L, 0.015 * diag, noise)

    # noise "arcs" that aren't really arcs are lines
    pieces = [["arc" if k == "arc" and good_arc(r) else "line", r] for k, r in pieces]

    changed = True
    while changed and len(pieces) > 1:
        changed = False
        n = len(pieces)
        rng = range(n) if closed else range(n - 1)
        for i in rng:
            j = (i + 1) % n
            if i == j:
                break
            (ka, ra), (kb, rb) = pieces[i], pieces[j]
            tiny_a = _len(ra) < max(0.04 * diag, 2 * noise)
            tiny_b = _len(rb) < max(0.04 * diag, 2 * noise)
            if ka == kb == "line" and straight(join(ra, rb)):
                merged = ["line", join(ra, rb)]
            elif tiny_a or tiny_b:
                keep = kb if tiny_a else ka
                merged = [keep, join(ra, rb)]
            elif ka == kb == "arc":
                if not good_arc(join(ra, rb)):
                    continue
                merged = ["arc", join(ra, rb)]
            else:
                continue
            if j == 0:                       # wrap-around merge for closed strokes
                pieces = [merged] + pieces[1:i]
            else:
                pieces = pieces[:i] + [merged] + pieces[j + 1:]
            changed = True
            break
    # tighten arc boundaries: trim end samples that belong to the neighbouring straight runs
    for idx, (kind, run) in enumerate(pieces):
        if kind != "arc" or len(run) < 12:
            continue
        lo, hi = 0, len(run)
        def score(a, b):
            fc = fit_circle(run[a:b])
            return fc[3] / fc[2] if fc and fc[2] > 0 else 1e9
        best = score(lo, hi)
        step = max(1, len(run) // 40)
        improved = True
        while improved and (hi - lo) > 0.5 * len(run):
            improved = False
            for a, b in ((lo + step, hi), (lo, hi - step)):
                sc = score(a, b)
                if sc < best * 0.97:
                    best, lo, hi, improved = sc, a, b, True
                    break
        if lo or hi < len(run):
            prev_i, next_i = idx - 1, idx + 1
            if lo and (prev_i >= 0 or closed) and pieces[prev_i % len(pieces)][0] == "line":
                pieces[prev_i % len(pieces)][1] = pieces[prev_i % len(pieces)][1] + run[1: lo + 1]
            else:
                lo = 0
            if hi < len(run) and (next_i < len(pieces) or closed) and pieces[next_i % len(pieces)][0] == "line":
                pieces[next_i % len(pieces)][1] = run[hi - 1: -1] + pieces[next_i % len(pieces)][1]
            else:
                hi = len(run)
            pieces[idx][1] = run[lo:hi]

    # runs of ≥3 lines bending the same way that sit on a circle → one arc
    out, i = [], 0
    while i < len(pieces):
        j = i
        while j + 1 < len(pieces) and pieces[j][0] == pieces[j + 1][0] == "line":
            t0 = _wrap(ang(pieces[j + 1][1]) - ang(pieces[j][1]))
            if abs(t0) > math.radians(50):
                break
            if j > i:
                tp = _wrap(ang(pieces[j][1]) - ang(pieces[j - 1][1]))
                if tp * t0 <= 0:
                    break
            j += 1
        if j - i >= 2:
            # an arc drawn as lines is several *short* pieces; a long straight edge ends the run
            lens = [_len(q[1]) for q in pieces[i: j + 1]]
            while j - i >= 2 and max(lens) > 0.45 * sum(lens):
                if lens[0] >= lens[-1]:
                    i += 1
                    out.append(pieces[i - 1])
                    lens = lens[1:]
                else:
                    j -= 1
                    lens = lens[:-1]
        if j - i >= 2:
            run = pieces[i][1]
            for q in pieces[i + 1: j + 1]:
                run = join(run, q[1])
            if good_arc(run):
                out.append(["arc", run])
                i = j + 1
                continue
        out.append(pieces[i])
        i += 1
    return out


def _has_long_straight(pts, L):
    """True if some stretch ≥ 22% of the stroke is straight (i.e. it's a polygon, not a wobbly circle)."""
    n = len(pts)
    span = max(3, int(n * 0.22))
    for i in range(0, n - span, max(1, span // 4)):
        a, b = pts[i], pts[i + span]
        d = math.dist(a, b)
        if d < 1e-9:
            continue
        dev = max(abs((b[1] - a[1]) * (q[0] - a[0]) - (b[0] - a[0]) * (q[1] - a[1])) / d for q in pts[i: i + span + 1])
        if dev < 0.03 * d and d > 0.18 * L:
            return True
    return False


# ----------------------------------------------------------- recognition ---

@dataclass
class Prim:
    kind: str                     # line | arc | circle
    a: Pt | None = None
    b: Pt | None = None
    c: Pt | None = None
    r: float = 0.0


def recognise_stroke(raw, scale_ref, noise=2.5):
    """One stroke → list of primitives (and whether it was closed)."""
    pts = [tuple(map(float, p)) for p in raw]
    if len(pts) < 2:
        return [], False
    L = _len(pts)
    if L < scale_ref * 0.01:
        return [], False
    n_raw = len(pts)
    if math.dist(pts[0], pts[-1]) > 0.3 * L and (L < 24 * noise or n_raw < 12):
        # short or sparse flick: straight line(s) only — too few samples to judge curvature
        idx = _rdp(pts, max(1.6 * noise, 0.06 * L))
        return [Prim("line", a=pts[i], b=pts[j]) for i, j in zip(idx, idx[1:])], False
    pts = _resample(pts, max(L / 200, 1e-6))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1e-9
    closed = math.dist(pts[0], pts[-1]) < max(0.12 * diag, 0.02 * L)
    h = L / 200
    smooth = _smooth(pts, max(1, round(0.012 * diag / h)), closed)

    # whole stroke a circle?
    fc = fit_circle(pts)
    if fc and closed:
        cx, cy, r, rms = fc
        cover = abs(_turning(pts, (cx, cy)))
        w, hgt = max(xs) - min(xs), max(ys) - min(ys)
        roundish = 0.7 < (w / hgt if hgt else 99) < 1.4
        if cover > math.radians(300) and (rms < 0.07 * r or (rms < 0.15 * r and roundish and
                                                           not _has_long_straight(pts, L))):
            return [Prim("circle", c=(cx, cy), r=r)], True

    work = smooth[:-1] + [smooth[0]] if closed else smooth
    pieces = [[kind, run] for kind, run in _segment(work, closed, k_len=0.08 * diag)]
    pieces = _cleanup(pieces, closed, diag, noise)
    prims: list[Prim] = []
    for kind, run in pieces:
        if kind == "arc":
            fc = fit_circle(run)
            if fc:
                cx, cy, r, rms = fc
                sweep = _turning(run, (cx, cy))
                if rms < 0.06 * r and abs(sweep) > math.radians(20) and r < 4 * diag:
                    a, b = run[0], run[-1]
                    if sweep < 0:
                        a, b = b, a           # store CCW a → b
                    prims.append(Prim("arc", a=a, b=b, c=(cx, cy), r=r))
                    continue
            prims.append(Prim("line", a=run[0], b=run[-1]))
        else:
            prims.append(Prim("line", a=run[0], b=run[-1]))
    return prims, closed


# --------------------------------------------------------------- assembly ---

def doodle_to_geometry(strokes, opts: DoodleOptions | None = None) -> dict:
    """Strokes (lists of (x, y), y up) → normalized sketch2d geometry."""
    opts = opts or DoodleOptions()
    allp = [tuple(p) for s in strokes for p in s]
    if len(allp) < 2:
        return sk.empty_geometry()
    xs, ys = [p[0] for p in allp], [p[1] for p in allp]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = opts.size_mm / span
    ox = (max(xs) + min(xs)) / 2 if opts.centre else min(xs)
    oy = (max(ys) + min(ys)) / 2 if opts.centre else min(ys)

    def T(p):
        return ((p[0] - ox) * scale, (p[1] - oy) * scale)

    prims: list[Prim] = []
    for s in strokes:
        ps, _closed = recognise_stroke(s, span, opts.noise_px)
        for p in ps:
            prims.append(Prim(p.kind, T(p.a) if p.a else None, T(p.b) if p.b else None,
                              T(p.c) if p.c else None, p.r * scale))

    # cluster endpoints (and the origin) that were meant to coincide
    tol = opts.snap_px * scale
    clusters: list[list[Pt]] = [[(0.0, 0.0)]] if opts.centre else []
    origin_cluster = 0 if opts.centre else -1

    def cid(p):
        for k, cl in enumerate(clusters):
            cx = sum(q[0] for q in cl) / len(cl)
            cy = sum(q[1] for q in cl) / len(cl)
            if math.dist(p, (cx, cy)) < tol and (k != origin_cluster or math.dist(p, (0, 0)) < tol * 0.4):
                cl.append(p)
                return k
        clusters.append([p])
        return len(clusters) - 1

    ends = []
    for p in prims:
        if p.kind in ("line", "arc"):
            ends.append((cid(p.a), cid(p.b)))
        else:
            ends.append((None, None))

    g = sk.empty_geometry()
    pid_of: dict[int, str] = {}

    def rnd(v):
        return round(v / opts.round_mm) * opts.round_mm if opts.round_mm else v

    def point(k):
        if k == origin_cluster:
            return sk.ORIGIN
        if k not in pid_of:
            cl = clusters[k]
            pid = sk.new_id(g, "p")
            g["points"][pid] = [rnd(sum(q[0] for q in cl) / len(cl)), rnd(sum(q[1] for q in cl) / len(cl))]
            pid_of[k] = pid
        return pid_of[k]

    for p, (ka, kb) in zip(prims, ends):
        if p.kind == "circle":
            c = sk.new_id(g, "p")
            g["points"][c] = [rnd(p.c[0]), rnd(p.c[1])]
            r = max(rnd(p.r), opts.round_mm or 0.1)
            g["curves"].append({"id": sk.new_id(g, "c"), "type": "circle", "c": c, "r": r})
            continue
        a, b = point(ka), point(kb)
        if a == b:
            continue
        if p.kind == "line":
            if any(c["type"] == "line" and {c["a"], c["b"]} == {a, b} for c in g["curves"]):
                continue
            lid = sk.new_id(g, "l")
            g["curves"].append({"id": lid, "type": "line", "a": a, "b": b})
        else:
            c = sk.new_id(g, "p")
            g["points"][c] = [p.c[0], p.c[1]]
            g["curves"].append({"id": sk.new_id(g, "a"), "type": "arc", "c": c, "a": a, "b": b})

    _close_gaps(g, 3 * opts.snap_px * scale)

    # horizontal / vertical where the hand was close; keep each only if the sketch still solves
    for c in g["curves"]:
        if c["type"] != "line":
            continue
        (ax, ay), (bx, by) = g["points"][c["a"]], g["points"][c["b"]]
        ang = abs(math.degrees(math.atan2(by - ay, bx - ax))) % 180
        rel = "horizontal" if min(ang, 180 - ang) < opts.hv_deg else \
            "vertical" if abs(ang - 90) < opts.hv_deg else None
        if rel:
            g["constraints"].append({"type": rel, "e": [c["id"]]})
            try:
                if not sk.solve(g).ok:
                    g["constraints"].pop()
            except sk.SketchError:
                g["constraints"].pop()
    try:
        res = sk.solve(g)
        if res.ok:
            g = res.geometry
    except sk.SketchError:
        pass
    # hit the requested size exactly after straightening, then re-round
    xs, ys = _extent(g)
    span2 = max(max(xs) - min(xs), max(ys) - min(ys))
    if span2 > 1e-9 and opts.exact_size:
        k = opts.size_mm / span2
        for pid, (x, y) in g["points"].items():
            g["points"][pid] = [rnd(x * k), rnd(y * k)]
        for c in g["curves"]:
            if c["type"] == "circle":
                c["r"] = max(rnd(c["r"] * k), opts.round_mm or 0.1)
        try:
            res = sk.solve(g)
            if res.ok:
                g = res.geometry
        except sk.SketchError:
            pass
    return sk.normalize(g)


def _extent(g):
    """x and y samples covering every curve (arc bulges and circles included)."""
    P = g["points"]
    xs, ys = [], []
    for c in g["curves"]:
        if c["type"] == "line":
            for k in ("a", "b"):
                xs.append(P[c[k]][0])
                ys.append(P[c[k]][1])
        elif c["type"] == "circle":
            cx, cy = P[c["c"]]
            xs += [cx - c["r"], cx + c["r"]]
            ys += [cy - c["r"], cy + c["r"]]
        else:
            cx, cy = P[c["c"]]
            ax, ay = P[c["a"]]
            bx, by = P[c["b"]]
            r = math.hypot(ax - cx, ay - cy)
            a0, a1 = math.atan2(ay - cy, ax - cx), math.atan2(by - cy, bx - cx)
            sw = (a1 - a0) % (2 * math.pi) or 2 * math.pi
            for i in range(17):
                t = a0 + sw * i / 16
                xs.append(cx + r * math.cos(t))
                ys.append(cy + r * math.sin(t))
    return xs or [0.0], ys or [0.0]


def _close_gaps(g, tol):
    """Pair up loose endpoints (used by only one curve) that are close: merge them."""
    while True:
        deg: dict[str, int] = {}
        for c in g["curves"]:
            if c["type"] in ("line", "arc"):
                for k in ("a", "b"):
                    deg[c[k]] = deg.get(c[k], 0) + 1
        loose = [p for p, d in deg.items() if d == 1]
        best = None
        for i, a in enumerate(loose):
            for b in loose[i + 1:]:
                d = math.dist(g["points"][a], g["points"][b])
                if d < tol and (best is None or d < best[0]):
                    best = (d, a, b)
        if not best:
            return
        _, a, b = best
        keep, drop = (a, b) if a != sk.ORIGIN else (a, b)
        if drop == sk.ORIGIN:
            keep, drop = drop, keep
        mx = (g["points"][a][0] + g["points"][b][0]) / 2
        my = (g["points"][a][1] + g["points"][b][1]) / 2
        if keep != sk.ORIGIN:
            g["points"][keep] = [mx, my]
        sk.merge_points(g, keep, drop)


def summarize(geom: dict) -> str:
    kinds = [c["type"] for c in geom["curves"]]
    parts = [f"{kinds.count(k)} {k}{'s' if kinds.count(k) != 1 else ''}" for k in ("line", "arc", "circle")
             if kinds.count(k)]
    return ", ".join(parts) or "nothing recognised"
