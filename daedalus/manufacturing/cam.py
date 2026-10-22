"""2.5-axis CAM: facing, outside profile, pockets (from floor faces, islands kept), drilling (G81/G83, G84 tap),
feeds and speeds from the part material, posts for Fanuc, Haas, LinuxCNC and GRBL, cycle-time estimate.

Machine frame: the part as placed in the model, +Z is the spindle axis (machining from the top). The work
offset (G54) is at the top of the stock, at its min-X/min-Y corner or its centre.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

POSTS = {
    "fanuc": "Fanuc 0i / 30i (also most Haas, Okuma OSP-G, Mitsubishi)",
    "haas": "Haas NGC",
    "linuxcnc": "LinuxCNC / Mach3 / Mach4",
    "grbl": "GRBL (hobby routers — canned cycles expanded)",
    "siemens": "Siemens 840D / 828D (ISO-dialect-free: ; comments, G71, D offsets, drilling expanded)",
}
OP_TYPES = {
    "facing": "Face the stock top down to the part top",
    "profile": "Outside contour of the part, down to the bottom (+ breakthrough)",
    "pocket": "Clear pockets down to floor faces (auto-detected or picked), islands kept",
    "drill": "Drill vertical holes (peck G83; tap G84 for threaded holes)",
}
CUT_SPEED = {  # m/min for carbide end mills by material category
    "Aluminum": 300, "Copper": 180, "Magnesium": 400, "Titanium": 50, "Stainless": 70, "Steel": 110,
    "Cast iron": 90, "Nickel": 30, "Plastic": 250, "Composite": 150, "Wood": 500, "Ceramic": 20,
}
CYCLES = {
    "auto": "Auto — G83 peck when deeper than 3×Ø, else G81; G84 for taps; G82 for spot drills",
    "G81": "Drill — feed in, rapid out",
    "G82": "Spot / counterbore — feed in, dwell, rapid out",
    "G83": "Deep-hole peck — full retract after every peck (Q)",
    "G73": "High-speed peck — short chip-break retract (Q)",
    "G84": "Right-hand rigid tap — feed in, reverse, feed out",
    "G74": "Left-hand rigid tap",
    "G85": "Bore / ream — feed in, feed out",
    "G86": "Bore — feed in, spindle stop, rapid out",
    "G89": "Bore with dwell — feed in, dwell, feed out",
    "G76": "Fine bore — orient spindle, shift off the wall (Q), rapid out",
}
CYCLE_PARAMS = ("cycle", "peck", "dwell", "retract", "r_clear", "shift", "breakthrough", "diameter_tolerance",
                "spot_depth", "skip_through")
CHIP_BREAK = 0.5     # G73 retract (mm)
RAPID = 5000.0       # mm/min, for time estimates
TOOL_CHANGE_S = 8.0


@dataclass
class Tool:
    number: int
    kind: str          # flat | face | drill | tap | spot | ball
    diameter: float
    flutes: int = 3
    length: float = 50.0
    name: str = ""
    rpm: float = 0.0   # 0 = from material
    feed: float = 0.0  # mm/min, 0 = from material
    pitch: float = 0.0  # taps

    def label(self):
        return self.name or f"Ø{self.diameter:g} {self.kind}"


@dataclass
class OpResult:
    name: str
    type: str
    tool: Tool
    rpm: float
    feed: float
    plunge: float
    moves: list = field(default_factory=list)     # (kind, x, y, z) kind: rapid | feed | plunge
    cycles: list = field(default_factory=list)    # drilling: dicts
    notes: list = field(default_factory=list)
    time_s: float = 0.0
    length: float = 0.0


DEFAULT_TOOLS = [
    {"number": 1, "kind": "face", "diameter": 50.0, "flutes": 5, "name": "Ø50 face mill"},
    {"number": 2, "kind": "flat", "diameter": 10.0, "flutes": 3, "name": "Ø10 end mill"},
    {"number": 3, "kind": "flat", "diameter": 6.0, "flutes": 3, "name": "Ø6 end mill"},
    {"number": 4, "kind": "flat", "diameter": 3.0, "flutes": 2, "name": "Ø3 end mill"},
]


def default_setup():
    return {"object": None, "margin": [2.0, 2.0, 1.0], "origin": "corner", "post": "fanuc", "units": "mm",
            "max_rpm": 12000, "safe_z": 5.0, "clearance_z": 25.0, "coolant": True, "program": 1000,
            "orient": "+Z", "tools": [dict(t) for t in DEFAULT_TOOLS], "ops": []}


# which way the spindle looks at the part: the named model direction is turned to face +Z (machine up)
ORIENT = {"+Z": None, "-Z": ((1, 0, 0), 180.0), "+Y": ((1, 0, 0), 90.0), "-Y": ((1, 0, 0), -90.0),
          "+X": ((0, 1, 0), -90.0), "-X": ((0, 1, 0), 90.0)}


def orient_body(body, orient="+Z"):
    """The part turned so the face looking along `orient` (model axes) points up at the spindle."""
    rot = ORIENT.get(orient or "+Z")
    if not rot:
        return body
    from build123d import Axis
    c = body.bounding_box().center()
    return body.rotate(Axis(c, rot[0]), rot[1])


# ------------------------------------------------------------- geometry ----

def _wire_pts(w, tol=0.05):
    L = w.length
    n = max(8, min(2000, int(L / max(tol * 4, 0.2))))
    try:
        pts = [w.position_at(i / n) for i in range(n)]
    except Exception:  # noqa: BLE001
        pts = [v for e in w.edges() for v in (e.position_at(0),)]
    return [(p.X, p.Y) for p in pts]


def face_polygon(face):
    from shapely.geometry import Polygon
    outer = _wire_pts(face.outer_wire())
    holes = [_wire_pts(w) for w in face.inner_wires()]
    p = Polygon(outer, holes)
    return p if p.is_valid else p.buffer(0)


def section_polygon(body, z):
    """Union of the part's cross-section at height z (shapely)."""
    from build123d import Face, Plane
    from shapely.ops import unary_union
    bb = body.bounding_box()
    big = Face.make_rect(bb.size.X * 3 + 10, bb.size.Y * 3 + 10,
                         Plane((bb.center().X, bb.center().Y, z)))
    polys = []
    try:
        for f in (body & big).faces():
            polys.append(face_polygon(f))
    except Exception:  # noqa: BLE001
        pass
    return unary_union(polys) if polys else None


def floor_faces(body, ztop):
    """Up-facing planar faces below the top: pocket floors and steps."""
    out = []
    for i, f in enumerate(body.faces()):
        if f.geom_type.name != "PLANE":
            continue
        n = f.normal_at()
        if n.Z < 0.999:
            continue
        z = f.center().Z
        if z < ztop - 1e-3:
            out.append((i, f, z))
    return out


def vertical_holes(body):
    """Concave cylinders with a vertical axis: [{x, y, r, z0, z1, face}]."""
    holes = []
    for i, f in enumerate(body.faces()):
        if f.geom_type.name != "CYLINDER":
            continue
        try:
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            s = BRepAdaptor_Surface(f.wrapped).Cylinder()
            ax = s.Axis()
            d = ax.Direction()
            if abs(abs(d.Z()) - 1) > 1e-6:
                continue
            loc = ax.Location()
            r = s.Radius()
            c = f.center()
            # concave = surface normal points toward the axis
            n = f.normal_at(c)
            radial = np.array([c.X - loc.X(), c.Y - loc.Y()])
            if np.linalg.norm(radial) < 1e-9 or np.dot(radial, [n.X, n.Y]) > 0:
                continue
            bb = f.bounding_box()
            holes.append({"x": loc.X(), "y": loc.Y(), "r": r, "z0": bb.min.Z, "z1": bb.max.Z, "face": i})
        except Exception:  # noqa: BLE001
            continue
    # merge split cylinder halves / stacked faces of the same hole
    merged = []
    for h in sorted(holes, key=lambda h: (round(h["x"], 3), round(h["y"], 3), round(h["r"], 3), h["z0"])):
        m = next((g for g in merged if abs(g["x"] - h["x"]) < 1e-3 and abs(g["y"] - h["y"]) < 1e-3
                  and abs(g["r"] - h["r"]) < 1e-3 and h["z0"] <= g["z1"] + 1e-3 and h["z1"] >= g["z0"] - 1e-3), None)
        if m:
            m["z0"], m["z1"] = min(m["z0"], h["z0"]), max(m["z1"], h["z1"])
        else:
            merged.append(dict(h))
    return merged


# --------------------------------------------------------- feeds / speeds --

def speeds(tool: Tool, material, max_rpm):
    cat = getattr(material, "category", "Steel")
    vc = CUT_SPEED.get(cat, 110)
    D = tool.diameter
    if tool.kind in ("drill", "spot"):
        vc *= 0.55
    if tool.kind == "tap":
        vc *= 0.15
    rpm = tool.rpm or min(max_rpm, vc * 1000 / (math.pi * D))
    if tool.kind in ("drill", "spot"):
        f_rev = min(0.3, max(0.03, 0.012 * D * (1.5 if cat in ("Aluminum", "Plastic", "Wood") else 1.0)))
        feed = tool.feed or rpm * f_rev
        plunge = feed
    elif tool.kind == "tap":
        feed = rpm * (tool.pitch or 1.0)
        plunge = feed
    else:
        fz = min(0.12, max(0.01, 0.005 * D * (1.6 if cat in ("Aluminum", "Plastic", "Wood") else 1.0)))
        feed = tool.feed or rpm * tool.flutes * fz
        plunge = feed / 3
    return round(rpm), round(feed, 1), round(plunge, 1)


def _stepdown(tool, material, given):
    if given:
        return float(given)
    soft = getattr(material, "category", "") in ("Aluminum", "Plastic", "Wood", "Magnesium", "Copper")
    return round(tool.diameter * (0.5 if soft else 0.25), 3)


# ------------------------------------------------------------ toolpaths ----

def _levels(top, bottom, step):
    if top <= bottom + 1e-9:
        return []
    n = max(1, math.ceil((top - bottom) / step - 1e-9))
    return [top - (top - bottom) * (i + 1) / n for i in range(n)]


def _ring_coords(ring, climb=True, outside=True):
    pts = list(ring.coords)
    ccw = ring.is_ccw
    # climb milling with an M3 spindle: outside contours clockwise, inside contours counter-clockwise
    want_ccw = (not outside) if climb else outside
    if ccw != want_ccw:
        pts = pts[::-1]
    return pts


class _Path:
    def __init__(self, safe, clear):
        self.m = []
        self.safe, self.clear = safe, clear
        self.pos = None

    def rapid(self, x, y, z):
        self.m.append(("rapid", x, y, z))
        self.pos = (x, y, z)

    def feed(self, x, y, z, kind="feed"):
        self.m.append((kind, x, y, z))
        self.pos = (x, y, z)

    def loop(self, pts, z, start_z):
        x0, y0 = pts[0]
        if self.pos and self.pos[2] < self.safe - 1e-9:
            self.rapid(self.pos[0], self.pos[1], self.safe)
        self.rapid(x0, y0, self.safe)
        self.rapid(x0, y0, start_z)
        self.feed(x0, y0, z, "plunge")
        for x, y in pts[1:]:
            self.feed(x, y, z)
        self.feed(x0, y0, z)

    def retract(self):
        if self.pos:
            self.rapid(self.pos[0], self.pos[1], self.safe)


def op_facing(op, tool, body, stock, setup, material):
    R = tool.diameter / 2
    x0, y0, x1, y1, ztop_stock = stock["x0"], stock["y0"], stock["x1"], stock["y1"], stock["z1"]
    zpart = body.bounding_box().max.Z + float(op.get("stock_to_leave", 0.0))
    step = tool.diameter * float(op.get("stepover", 0.7))
    sd = _stepdown(tool, material, op.get("stepdown"))
    P = _Path(ztop_stock + setup["safe_z"], ztop_stock + setup["clearance_z"])
    ys = np.arange(y0 + R * 0.6, y1 - R * 0.6 + step * 0.999, step) if y1 - y0 > 1.2 * R else [(y0 + y1) / 2]
    for z in _levels(ztop_stock, zpart, sd) or ([zpart] if ztop_stock > zpart + 1e-9 else []):
        P.rapid(x0 - R - 2, ys[0], P.safe)
        P.rapid(x0 - R - 2, ys[0], z + 1.0)
        P.feed(x0 - R - 2, ys[0], z, "plunge")
        for i, y in enumerate(ys):
            xa, xb = (x0 - R - 2, x1 + R + 2) if i % 2 == 0 else (x1 + R + 2, x0 - R - 2)
            P.feed(xa, y, z)
            P.feed(xb, y, z)
        P.retract()
    return P.m, []


def op_profile(op, tool, body, stock, setup, material):
    from shapely.geometry import Polygon
    bb = body.bounding_box()
    R = tool.diameter / 2
    zb = bb.min.Z
    region = section_polygon(body, zb + min(0.05, (bb.max.Z - zb) / 4))
    notes = []
    if region is None or region.is_empty:
        return [], ["No cross-section at the part bottom"]
    # the outline is the union of sections at a few heights (catches overhangs above the bottom)
    from shapely.ops import unary_union
    hs = [zb + (bb.max.Z - zb) * t for t in (0.02, 0.5, 0.98)]
    region = unary_union([region] + [s for s in (section_polygon(body, h) for h in hs) if s is not None])
    outline = unary_union([Polygon(g.exterior) for g in getattr(region, "geoms", [region])])
    path = outline.buffer(R + float(op.get("stock_to_leave", 0.0)), quad_segs=16)
    top = min(stock["z1"], bb.max.Z if op.get("from_part_top", False) else stock["z1"])
    bottom = zb - float(op.get("breakthrough", 0.5))
    sd = _stepdown(tool, material, op.get("stepdown"))
    P = _Path(stock["z1"] + setup["safe_z"], stock["z1"] + setup["clearance_z"])
    tabs = int(op.get("tabs", 0))
    for poly in getattr(path, "geoms", [path]):
        pts = _ring_coords(poly.exterior, op.get("climb", True), outside=True)[:-1]
        last = None
        for z in _levels(top, bottom, sd):
            if last is None:
                P.loop(pts, z, top + 1.0)
            else:
                P.feed(pts[0][0], pts[0][1], z, "plunge")
                for x, y in pts[1:]:
                    P.feed(x, y, z)
                P.feed(pts[0][0], pts[0][1], z)
            last = z
        P.retract()
    if tabs:
        notes.append("Tabs are not generated — hold the part with a fixture plate or leave an onion skin")
    return P.m, notes


def op_pocket(op, tool, body, stock, setup, material):
    R = tool.diameter / 2
    bb = body.bounding_box()
    zt = bb.max.Z
    floors = floor_faces(body, zt)
    pick = op.get("faces", "auto")
    notes = []
    if pick != "auto":
        idx = set(int(i) for i in pick)
        floors = [f for f in floors if f[0] in idx]
        if not floors:
            return [], ["Picked faces aren't up-facing planar floors"]
    holes = vertical_holes(body)
    so = tool.diameter * float(op.get("stepover", 0.45))
    sd = _stepdown(tool, material, op.get("stepdown"))
    leave = float(op.get("stock_to_leave", 0.0))
    P = _Path(stock["z1"] + setup["safe_z"], stock["z1"] + setup["clearance_z"])
    done = 0
    for i, f, zf in sorted(floors, key=lambda t: -t[2]):
        poly = face_polygon(f)
        # skip blind-hole bottoms (left to drilling)
        if any(abs(math.pi * h["r"] ** 2 - poly.area) < 0.02 * poly.area and abs(h["z0"] - zf) < 1e-3 for h in holes):
            continue
        # the region above the floor up to the part top must be open (pocket walls are vertical): use
        # the cross-section just above the floor to find walls
        above = section_polygon(body, zf + 0.02)
        region = poly if above is None else poly.difference(above)
        # top of this pocket: highest material around it, at most the stock top
        ztop = stock["z1"]
        if region.is_empty or region.area < 1e-6:
            continue
        rings = []
        cur = region.buffer(-(R + leave), quad_segs=12)
        if cur.is_empty:
            notes.append(f"Face {i}: Ø{tool.diameter:g} tool doesn't fit — use a smaller tool")
            continue
        while not cur.is_empty:
            for g in getattr(cur, "geoms", [cur]):
                if g.geom_type == "Polygon":
                    rings.append(("out", g.exterior))
                    rings += [("in", r) for r in g.interiors]
            cur = cur.buffer(-so, quad_segs=12)
        rings.reverse()                     # inside out: start in the middle, finish on the wall
        for z in _levels(ztop, zf + leave, sd):
            for kind, ring in rings:
                pts = _ring_coords(ring, op.get("climb", True), outside=(kind == "in"))[:-1]
                if len(pts) < 2:
                    continue
                P.loop(pts, z, z + sd + 0.5)
            P.retract()
        done += 1
    if not done and not notes:
        notes.append("No pocket floors found")
    return P.m, notes


def op_drill(op, tool, body, stock, setup, material, threads=()):
    holes = vertical_holes(body)
    bb = body.bounding_box()
    D = tool.diameter
    tol = float(op.get("diameter_tolerance", 0.05))
    sel = [h for h in holes if abs(2 * h["r"] - D) <= tol + 1e-9 or
           (tool.kind == "tap" and _thread_at(threads, h))]
    if op.get("diameter"):
        sel = [h for h in holes if abs(2 * h["r"] - float(op["diameter"])) <= tol + 1e-9]
    elif tool.kind == "spot":                                   # spot every hole
        sel = list(holes)
    try:                                                        # only holes that open towards the spindle
        from build123d import Vector
        sel = [h for h in sel if not body.is_inside(Vector(h["x"], h["y"], h["z1"] + 0.05))]
    except Exception:  # noqa: BLE001
        pass
    if op.get("skip_through"):
        sel = [h for h in sel if not (h["z0"] <= bb.min.Z + 1e-3 and h["z1"] >= bb.max.Z - 1e-3)]
    cycles = []
    for h in sel:
        through = h["z0"] <= bb.min.Z + 1e-3
        if tool.kind == "spot":                                 # 90° spot: chamfer just past the hole edge
            depth = float(op.get("spot_depth") or (h["r"] + 0.2))
            cycles.append({"x": h["x"], "y": h["y"], "z": h["z1"] - depth,
                           "r": h["z1"] + float(op.get("r_clear", 2.0)), "top": h["z1"]})
            continue
        if tool.kind == "tap":
            th = _thread_at(threads, h)
            depth_z = h["z1"] - float(th.get("depth") or (h["z1"] - h["z0"])) if th else h["z0"]
        else:
            depth_z = h["z0"] - (0.3 * D + float(op.get("breakthrough", 1.0)) if through else 0.0)
        cycles.append({"x": h["x"], "y": h["y"], "z": depth_z, "r": h["z1"] + float(op.get("r_clear", 2.0)),
                       "top": h["z1"]})
    cycles.sort(key=lambda c: (round(c["y"], 2), c["x"]))
    # nearest-neighbour ordering
    if cycles:
        order = [cycles.pop(0)]
        while cycles:
            p = order[-1]
            k = min(range(len(cycles)), key=lambda i: (cycles[i]["x"] - p["x"]) ** 2 + (cycles[i]["y"] - p["y"]) ** 2)
            order.append(cycles.pop(k))
        cycles = order
    kind = resolve_cycle(op.get("cycle", "auto"), tool, cycles)
    peck = float(op.get("peck", 0) or 0)
    if kind in ("G83", "G73") and peck <= 0:
        peck = D * (1.5 if kind == "G83" else 1.0)
    dwell = float(op.get("dwell", 0) or (0.3 if kind in ("G82", "G89") else 0.0))
    for c in cycles:
        c["cycle"] = kind
        c["q"] = peck if kind in ("G83", "G73") else 0.0
        c["p"] = dwell if kind in ("G82", "G89", "G84", "G74") else 0.0
        if kind == "G76":
            c["shift"] = float(op.get("shift", 0.2))
        c["ret"] = "r" if str(op.get("retract", "initial")).lower() in ("r", "g99") else "initial"
    notes = [] if cycles else [f"No Ø{D:g} vertical holes found"]
    if kind in ("G84", "G74") and tool.kind != "tap":
        notes.append(f"{kind} is a tapping cycle but T{tool.number} is a {tool.kind}")
    return [], cycles, notes


def resolve_cycle(cycle, tool, cycles=()):
    """The canned cycle an operation uses ('auto' picks by tool and depth)."""
    cycle = (cycle or "auto").upper() if cycle != "auto" else "auto"
    if cycle != "auto":
        if cycle not in CYCLES:
            raise ValueError(f"cycle must be one of {list(CYCLES)}")
        return cycle
    if tool.kind == "tap":
        return "G84"
    if tool.kind == "spot":
        return "G82"
    deep = any((c["top"] - c["z"]) > 3 * tool.diameter for c in cycles)
    return "G83" if deep else "G81"


def _thread_at(threads, h):
    for th in threads:
        o = th.get("axis", [[None]])[0]
        if o[0] is not None and abs(o[0] - h["x"]) < 1e-3 and abs(o[1] - h["y"]) < 1e-3:
            return th
    return None


# ------------------------------------------------------------- planner ----

def stock_box(body, setup):
    bb = body.bounding_box()
    mx, my, mz = (list(setup.get("margin", [2, 2, 1])) + [1, 1, 1])[:3]
    return {"x0": bb.min.X - mx, "y0": bb.min.Y - my, "z0": bb.min.Z, "x1": bb.max.X + mx, "y1": bb.max.Y + my,
            "z1": bb.max.Z + mz}


def plan(doc, setup=None):
    """Compute every operation. Returns {"ops": [OpResult], "stock": {...}, "origin": (x, y, z), ...}."""
    setup = {**default_setup(), **(setup if setup is not None else (doc.cam or {}))}
    name = setup.get("object") or doc.objects[0].name
    placed = doc.placed_bodies()
    if name not in placed:
        raise ValueError(f"'{name}' has no body to machine")
    body = orient_body(placed[name], setup.get("orient", "+Z"))
    material = doc.material_of(name)
    stock = stock_box(body, setup)
    tools = {int(t["number"]): Tool(**t) for t in setup["tools"]}
    threads = [dict(t) for t in doc.result.threads.values() if t.get("object", name) == name]
    results = []
    for k, op in enumerate(setup["ops"]):
        if op.get("suppressed"):
            continue
        t = tools.get(int(op.get("tool", 0) or 0))
        if t is None:
            raise ValueError(f"Operation {k + 1}: no tool T{op.get('tool')}")
        rpm, feed, plunge = speeds(t, material, setup["max_rpm"])
        res = OpResult(op.get("name") or f"{op['type'].title()} {k + 1}", op["type"], t, rpm, feed, plunge)
        if op["type"] == "facing":
            res.moves, res.notes = op_facing(op, t, body, stock, setup, material)
        elif op["type"] == "profile":
            res.moves, res.notes = op_profile(op, t, body, stock, setup, material)
        elif op["type"] == "pocket":
            res.moves, res.notes = op_pocket(op, t, body, stock, setup, material)
        elif op["type"] == "drill":
            res.moves, res.cycles, res.notes = op_drill(op, t, body, stock, setup, material, threads)
        else:
            raise ValueError(f"Unknown operation type {op['type']}")
        _estimate(res, stock["z1"] + setup["safe_z"])
        results.append(res)
    ox = stock["x0"] if setup["origin"] == "corner" else (stock["x0"] + stock["x1"]) / 2
    oy = stock["y0"] if setup["origin"] == "corner" else (stock["y0"] + stock["y1"]) / 2
    total = sum(r.time_s for r in results) + TOOL_CHANGE_S * len({r.tool.number for r in results})
    return {"ops": results, "stock": stock, "origin": (ox, oy, stock["z1"]), "setup": setup, "object": name,
            "material": material.name, "time_s": total}


def _estimate(res: OpResult, safe):
    t = 0.0
    L = 0.0
    prev = None
    for kind, x, y, z in res.moves:
        if prev is not None:
            d = math.dist(prev, (x, y, z))
            if kind == "rapid":
                t += d / RAPID * 60
            else:
                L += d
                t += d / (res.plunge if kind == "plunge" else res.feed) * 60
        prev = (x, y, z)
    for c in res.cycles:
        t_c, l_c = cycle_time(c, res.feed, res.rpm, getattr(res.tool, "pitch", 0) or 0.0)
        t += t_c
        L += l_c
    res.time_s, res.length = t, L


def cycle_time(c, feed, rpm=0.0, pitch=0.0, rapid=RAPID):
    """(seconds, cut length mm) of one canned cycle from R down to Z and back."""
    depth = max(c["r"] - c["z"], 0.0)
    kind = c.get("cycle", "G83" if c.get("q") else "G81")
    q = c.get("q") or depth
    f = feed if feed > 0 else 100.0
    if kind in ("G84", "G74"):
        f = rpm * pitch if rpm and pitch else f
    t = 1.0                                                      # positioning / settle
    if kind == "G83":
        n = max(1, math.ceil(depth / q))
        t += depth / f * 60
        t += sum(2 * min(q * k, depth) for k in range(1, n)) / rapid * 60 + depth / rapid * 60
    elif kind == "G73":
        n = max(1, math.ceil(depth / q))
        t += depth / f * 60 + (n - 1) * 2 * CHIP_BREAK / rapid * 60 + depth / rapid * 60
    elif kind in ("G85", "G89", "G84", "G74"):
        t += 2 * depth / f * 60
    else:                                                        # G81 G82 G86 G76
        t += depth / f * 60 + depth / rapid * 60
    t += c.get("p", 0.0)
    return t, depth


# ---------------------------------------------------------------- posts ----

def gcode(result, post=None) -> str:
    setup = result["setup"]
    post = (post or setup.get("post", "fanuc")).lower()
    if post not in POSTS:
        raise ValueError(f"post must be one of {list(POSTS)}")
    inch = setup.get("units", "mm") == "in"
    sc = 1 / 25.4 if inch else 1.0
    nd = 4 if inch else 3
    ox, oy, oz = result["origin"]

    def f(v):
        s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    def X(x):
        return f((x - ox) * sc)

    def Y(y):
        return f((y - oy) * sc)

    def Z(z):
        return f((z - oz) * sc)

    def F(v):
        return f"{v * sc:.{1 if not inch else 2}f}"

    L = []
    pct = post in ("fanuc", "haas")
    if pct:
        L.append("%")
        L.append(f"O{int(setup.get('program', 1000)):04d} ({result['object'][:20].upper()})")
    L.append(f"(DAEDALUS · {POSTS[post].split(' (')[0]})")
    L.append(f"(MATERIAL {result['material'].upper()[:40]})")
    st = result["stock"]
    L.append(f"(STOCK {f((st['x1'] - st['x0']) * sc)} X {f((st['y1'] - st['y0']) * sc)} X "
             f"{f((st['z1'] - st['z0']) * sc)} {'IN' if inch else 'MM'} · G54 AT STOCK TOP "
             f"{'MIN CORNER' if setup['origin'] == 'corner' else 'CENTER'})")
    for r in result["ops"]:
        L.append(f"(T{r.tool.number} {r.tool.label().upper()} · {r.name.upper()})")
    if post == "siemens":
        L.append(("G70" if inch else "G71") + " G17 G40 G90 G94")
    else:
        L.append(("G20" if inch else "G21") + " G17 G40 G49 G80 G90" if post != "grbl" else
                 ("G20" if inch else "G21") + " G17 G90 G94")
    if post == "linuxcnc":
        L.append("G64 P0.01")
    safe = Z(st["z1"] + setup["safe_z"])
    clear = Z(st["z1"] + setup["clearance_z"])
    cur_tool = None
    for r in result["ops"]:
        L.append(f"({r.name.upper()} · {r.type.upper()} · S{r.rpm} F{F(r.feed)} · ~{r.time_s / 60:.1f} MIN)")
        if r.tool.number != cur_tool:
            if cur_tool is not None:
                L.append("M9")
                L.append("M5")
                if post in ("fanuc", "haas"):
                    L.append("G91 G28 Z0")
                    L.append("G90")
            if post == "grbl":
                L.append(f"M5\n(CHANGE TO T{r.tool.number} {r.tool.label().upper()})\nM0")
            elif post == "siemens":
                L.append(f"T{r.tool.number} D1")
                L.append("M6")
            else:
                L.append(f"T{r.tool.number} M6")
            cur_tool = r.tool.number
        left = bool(r.cycles) and r.cycles[0].get("cycle") == "G74"
        L.append(f"S{r.rpm} {'M4' if left else 'M3'}")
        if post in ("grbl", "siemens"):
            L.append(f"G54 G0 Z{clear}" if post == "siemens" else f"G0 Z{clear}")
        else:
            L.append(f"G54 G0 Z{clear}" if post != "linuxcnc" else f"G54 G0 Z{clear}")
            L.append(f"G43 H{r.tool.number} Z{clear}")
        if setup.get("coolant", True):
            L.append("M8")
        last = {"g": None, "x": None, "y": None, "z": clear, "f": None}
        for i, (kind, x, y, z) in enumerate(r.moves):
            g = "G0" if kind == "rapid" else "G1"
            xs, ys, zs = X(x), Y(y), Z(z)
            if i == 0 and kind == "rapid":            # position at clearance height first, then descend
                L.append(f"G0 X{xs} Y{ys}")
                last.update(g="G0", x=xs, y=ys)
            words = [g] if g != last["g"] else []
            if xs != last["x"]:
                words.append(f"X{xs}")
            if ys != last["y"]:
                words.append(f"Y{ys}")
            if zs != last["z"]:
                words.append(f"Z{zs}")
            fv = r.plunge if kind == "plunge" else r.feed
            if g == "G1" and fv != last["f"]:
                words.append(f"F{F(fv)}")
                last["f"] = fv
            if any(w[0] in "XYZ" for w in words):
                L.append(" ".join(words))
                last.update(g=g)
            last.update(x=xs, y=ys, z=zs)
        if r.cycles:
            L.extend(_cycle_block(r, post, X, Y, Z, F, f, sc, safe))
            L[:] = [s for s in L if s != ""]
        L.append(f"G0 Z{clear}")
    L.append("M9")
    L.append("M5")
    if post in ("fanuc", "haas"):
        L.append("G91 G28 Z0")
        L.append("G91 G28 X0 Y0" if post == "fanuc" else "G53 G0 Y0")
        L.append("G90")
    L.append("M30")
    if pct:
        L.append("%")
    if post in ("fanuc", "haas"):                  # sequence numbers N10, N20, … after the program number
        out, n = [], 10
        for s in L:
            if s.startswith(("%", "O", "(")):
                out.append(s)
            else:
                for line in s.split("\n"):
                    out.append(f"N{n} {line}")
                    n += 10
        L = out
    if post == "siemens":                         # Siemens: ; comments, no % / O-number
        import re as _re
        L = [_re.sub(r"^\((.*)\)$", r"; \1", s) for s in L]
    return "\n".join(_ascii(s) for s in L) + "\n"


def _cycle_block(r, post, X, Y, Z, F, f, sc, safe):
    """Canned-cycle G-code for one drilling operation in the post's dialect."""
    out = []
    cyc = r.cycles[0].get("cycle", "G81")
    pitch = r.tool.pitch or 1.0
    tap_feed = r.rpm * pitch
    feed = tap_feed if cyc in ("G84", "G74") else r.feed

    def dwell_word(p):
        if not p:
            return ""
        if post == "fanuc":
            return f" P{int(round(p * 1000))}"                 # Fanuc: milliseconds, no decimal point
        return f" P{p:.3f}".rstrip("0").rstrip(".")             # Haas / LinuxCNC: seconds

    if post == "grbl":                                          # no canned cycles: write the motions out
        if cyc in ("G84", "G74"):
            out.append(f"(T{r.tool.number} {cyc} TAPPING NOT POSSIBLE ON GRBL — {len(r.cycles)} HOLES SKIPPED)")
            return out
        for c in r.cycles:
            back = safe if c.get("ret") != "r" else Z(c["r"])
            out.append(f"G0 X{X(c['x'])} Y{Y(c['y'])}")
            out.append(f"G0 Z{Z(c['r'])}")
            if cyc in ("G83", "G73"):
                zc, q = c["r"], c.get("q") or (c["r"] - c["z"])
                while zc > c["z"] + 1e-9:
                    zc = max(c["z"], zc - q)
                    out.append(f"G1 Z{Z(zc)} F{F(feed)}")
                    if zc > c["z"] + 1e-9:
                        out.append(f"G0 Z{Z(c['r']) if cyc == 'G83' else Z(zc + CHIP_BREAK)}")
                        if cyc == "G83":
                            out.append(f"G0 Z{Z(zc + CHIP_BREAK)}")
            else:
                out.append(f"G1 Z{Z(c['z'])} F{F(feed)}")
                if c.get("p"):
                    out.append(f"G4 P{c['p']:.2f}")
                if cyc in ("G85", "G89"):
                    out.append(f"G1 Z{Z(c['r'])} F{F(feed)}")
                elif cyc in ("G86", "G76"):
                    out.append("M5")
            out.append(f"G0 Z{back}")
            if cyc in ("G86", "G76"):
                out.append(f"M3 S{r.rpm}")
        return out

    if post == "siemens":                                       # 840D measuring-cycle style: MCALL CYCLE8x(...)
        c0 = r.cycles[0]
        rtp = f(float(safe) if c0.get("ret") != "r" else float(Z(c0["r"])))
        rfp = Z(c0["top"])
        sdis = f((c0["r"] - c0["top"]) * sc)
        dp = Z(c0["z"])
        if cyc == "G81":
            call = f"CYCLE81({rtp},{rfp},{sdis},{dp},)"
        elif cyc == "G82":
            call = f"CYCLE82({rtp},{rfp},{sdis},{dp},,{f(c0.get('p', 0))})"
        elif cyc in ("G83", "G73"):
            q = (c0.get("q") or 1.0) * sc
            call = (f"CYCLE83({rtp},{rfp},{sdis},{dp},,{f(float(rfp) - q)},,0,0,0,1,"
                    f"{1 if cyc == 'G83' else 0})")
        elif cyc in ("G84", "G74"):
            pit = pitch * sc * (-1 if cyc == "G74" else 1)
            call = f"CYCLE84({rtp},{rfp},{sdis},{dp},,{f(c0.get('p', 0))},3,,{f(pit)},0,{r.rpm},{r.rpm})"
        elif cyc == "G85":
            call = f"CYCLE85({rtp},{rfp},{sdis},{dp},,0,{F(feed)},{F(feed)})"
        elif cyc == "G89":
            call = f"CYCLE89({rtp},{rfp},{sdis},{dp},,{f(c0.get('p', 0))})"
        else:                                                   # G86 / G76 → CYCLE86 (G76 lifts off the wall)
            rpa = f(-(c0.get("shift", 0.0)) * sc) if cyc == "G76" else "0"
            call = f"CYCLE86({rtp},{rfp},{sdis},{dp},,0,3,{rpa},0,0,0)"
        out.append(f"G0 X{X(c0['x'])} Y{Y(c0['y'])}")
        out.append(f"G0 Z{safe}")
        out.append(f"F{F(feed)}")
        out.append(f"MCALL {call}")
        for c in r.cycles:
            out.append(f"X{X(c['x'])} Y{Y(c['y'])}")
        out.append("MCALL")
        return out

    # Fanuc / Haas / LinuxCNC
    code = cyc
    if post == "linuxcnc" and cyc == "G76":                    # LinuxCNC's G76 is thread cutting
        code = "G86"
        out.append("(FINE BORE AS G86 - LINUXCNC G76 IS THREADING)")
    c0 = r.cycles[0]
    words = f"X{X(c0['x'])} Y{Y(c0['y'])} Z{Z(c0['z'])} R{Z(c0['r'])}"
    if code in ("G83", "G73"):
        words += f" Q{f(c0['q'] * sc)}"
    if code in ("G82", "G89", "G84", "G74"):
        words += dwell_word(c0.get("p", 0))
    if code == "G76":
        words += f" Q{f(c0.get('shift', 0.2) * sc)}"
    if code in ("G84", "G74") and post == "fanuc":
        out.append(f"M29 S{r.rpm}")                            # rigid tapping
    out.append(f"G0 X{X(c0['x'])} Y{Y(c0['y'])}")
    out.append(f"G0 Z{safe}")
    out.append(f"{'G99' if c0.get('ret') == 'r' else 'G98'} {code} {words} F{F(feed)}")
    for c in r.cycles[1:]:
        extra = f" Z{Z(c['z'])} R{Z(c['r'])}" if abs(c["z"] - c0["z"]) > 1e-6 or \
            abs(c["r"] - c0["r"]) > 1e-6 else ""
        out.append(f"X{X(c['x'])} Y{Y(c['y'])}{extra}")
    out.append("G80")
    return out


def _ascii(line):
    """Controls want plain ASCII and no nested parentheses in comments."""
    line = line.replace("Ø", "D").replace("·", "-").replace("×", "x").replace("°", "DEG")
    if "(" in line:
        i = line.index("(")
        body = line[i + 1:].rstrip()
        body = body[:-1] if body.endswith(")") else body
        line = line[:i] + "(" + body.replace("(", "").replace(")", "") + ")"
    return line.encode("ascii", "replace").decode().replace("?", "")


def summary(result) -> dict:
    return {"object": result["object"], "material": result["material"],
            "stock_mm": [round(result["stock"]["x1"] - result["stock"]["x0"], 2),
                         round(result["stock"]["y1"] - result["stock"]["y0"], 2),
                         round(result["stock"]["z1"] - result["stock"]["z0"], 2)],
            "cycle_time_min": round(result["time_s"] / 60, 2),
            "operations": [{"name": r.name, "type": r.type, "tool": f"T{r.tool.number} {r.tool.label()}",
                            "rpm": r.rpm, "feed_mm_min": r.feed, "moves": len(r.moves), "holes": len(r.cycles),
                            "time_min": round(r.time_s / 60, 2), "notes": r.notes} for r in result["ops"]]}
