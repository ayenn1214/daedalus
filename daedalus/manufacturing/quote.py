"""Shop quoting (from SHOPQUOTE): CNC machining and sheet / laser cutting, driven by the exact B-rep.

What changed from the browser tool: volume, area, cut length, pierce count and thickness come from the
solid (not a bounding-box guess); material is bought as stock, not as the finished part; setup is spread
over the batch so quantity breaks are real; the CAM cycle time replaces the rate model when the part has
CAM operations.

Rates are rough shop defaults, all editable (and saved in ~/.daedalus/shop.json).
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

# removal rate (cm³/min), finishing rate (cm²/min) and laser reference speed (mm/min at 1 mm) by category
CNC_RATES = {
    "Aluminum": (15, 2500), "Steel": (6, 1200), "Stainless steel": (4, 800), "Cast iron": (7, 1300),
    "Copper alloy": (10, 1800), "Titanium": (2, 400), "Magnesium": (20, 3000), "Nickel alloy": (1.5, 300),
    "Zinc": (12, 2000), "Plastic": (20, 3500), "Elastomer": (8, 1500), "Composite": (12, 2000), "Wood": (40, 6000),
    "Ceramic & glass": (0.5, 100),
}
SHEET_SPEED = {
    "Steel": 3000, "Stainless steel": 2200, "Aluminum": 4000, "Copper alloy": 2500, "Titanium": 1800,
    "Plastic": 3500, "Wood": 6000, "Composite": 2500, "Zinc": 3000, "Magnesium": 3500, "Nickel alloy": 1500,
}

CNC_DEFAULTS = {"setup_min": 30.0, "setups": 1, "tool_changes": 3, "tool_change_min": 2.0, "complexity": 1.0,
                "rate": 75.0, "margin_pct": 20.0, "stock_margin": 2.0, "use_cam": True}
SHEET_DEFAULTS = {"setup_min": 15.0, "pierce_s": 2.0, "rate": 90.0, "margin_pct": 20.0, "nest_util": 0.80,
                  "kerf": 0.2}
QTY_BREAKS = (1, 5, 10, 25, 100)


def _shop_path() -> Path:
    return Path(os.environ.get("DAEDALUS_HOME") or Path.home() / ".daedalus") / "shop.json"


# ------------------------------------------------------------------ geometry
def part_geometry(body) -> dict:
    """Exact numbers for quoting one solid (mm, mm², mm³)."""
    bb = body.bounding_box()
    dims = [bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z]
    g = {"x": dims[0], "y": dims[1], "z": dims[2], "volume": float(body.volume), "area": float(body.area),
         "faces": len(body.faces()), "edges": len(body.edges())}
    try:
        from .cam import vertical_holes
        g["holes"] = [{"x": h["x"], "y": h["y"], "d": 2 * h["r"], "depth": h["z1"] - h["z0"]}
                      for h in vertical_holes(body)]
    except Exception:  # noqa: BLE001
        g["holes"] = []
    g.update(sheet_geometry(body))
    return g


def sheet_geometry(body) -> dict:
    """Flat-pattern facts for a plate-like part: thickness, cut length (all contours of the largest flat
    face, holes included), pierces (one per closed contour), flat blank size."""
    planes = [f for f in body.faces() if f.geom_type.name == "PLANE"]
    if not planes:
        return {"sheet": False}
    top = max(planes, key=lambda f: f.area)
    n = top.normal_at(top.center())
    nv = (n.X, n.Y, n.Z)
    proj = [v.X * nv[0] + v.Y * nv[1] + v.Z * nv[2] for v in body.vertices()]
    thick = max(proj) - min(proj) if proj else 0.0
    wires = top.wires()
    cut = sum(w.length for w in wires)
    # blank: the face's extent in its own plane
    import numpy as np
    pts = np.array([[v.X, v.Y, v.Z] for v in top.vertices()] or [[0, 0, 0]])
    ax = np.array(nv)
    u = np.cross(ax, [0, 0, 1] if abs(ax[2]) < 0.9 else [1, 0, 0])
    u /= np.linalg.norm(u) or 1
    w = np.cross(ax, u)
    pu, pw = pts @ u, pts @ w
    try:                                               # curved outlines: sample the edges too
        samp = np.array([[p.X, p.Y, p.Z] for e in top.edges() for p in (e.position_at(t) for t in
                                                                           (0, .25, .5, .75, 1))])
        pu, pw = np.concatenate([pu, samp @ u]), np.concatenate([pw, samp @ w])
    except Exception:  # noqa: BLE001
        pass
    bw, bh = float(pu.max() - pu.min()), float(pw.max() - pw.min())
    sheet_like = thick > 0 and thick <= 0.25 * min(bw, bh) + 1e-9 and \
        abs(top.area * thick - body.volume) / max(body.volume, 1e-9) < 0.35
    return {"sheet": bool(sheet_like), "thickness": thick, "cut_length": cut, "pierces": len(wires),
            "flat_area": float(top.area), "blank": (bw, bh)}


# ------------------------------------------------------------------ calculations
def calc_cnc(geom: dict, material, params: dict | None = None, qty: int = 1, cycle_min: float | None = None):
    p = {**CNC_DEFAULTS, **(params or {})}
    qty = max(1, int(qty))
    mrr, fin = CNC_RATES.get(getattr(material, "category", ""), (8, 1500))
    m = p["stock_margin"]
    stock_mm3 = (geom["x"] + 2 * m) * (geom["y"] + 2 * m) * (geom["z"] + m)
    removed_cm3 = max(stock_mm3 - geom["volume"], 0) / 1000
    if cycle_min and p.get("use_cam", True):
        rough, finish, source = cycle_min, 0.0, "CAM cycle time"
    else:
        rough = removed_cm3 / mrr if mrr else 0.0
        finish = (geom["area"] / 100) / fin * p["complexity"] if fin else 0.0
        source = "rate model"
    tool = p["tool_changes"] * p["tool_change_min"]
    setup = p["setup_min"] * p["setups"]
    per_part = rough + finish + tool
    batch_min = setup + per_part * qty
    density = getattr(material, "density", 7.85)                 # g/cm³
    stock_kg = stock_mm3 / 1000 * density / 1000
    part_kg = geom["volume"] / 1000 * density / 1000
    price = material.price() if hasattr(material, "price") else 5.0
    mat_cost = stock_kg * price * qty
    mach_cost = batch_min / 60 * p["rate"]
    sub = mach_cost + mat_cost
    margin = sub * p["margin_pct"] / 100
    total = sub + margin
    return {"process": "cnc", "qty": qty, "material": getattr(material, "name", "?"), "time_source": source,
            "rough_min": rough, "finish_min": finish, "tool_min": tool, "setup_min": setup,
            "per_part_min": per_part, "batch_min": batch_min, "batch_hr": batch_min / 60,
            "stock_mm": [geom["x"] + 2 * m, geom["y"] + 2 * m, geom["z"] + m], "stock_kg": stock_kg,
            "part_kg": part_kg, "machine_cost": mach_cost, "material_cost": mat_cost, "subtotal": sub,
            "margin": margin, "total": total, "unit": total / qty, "rate": p["rate"], "removed_cm3": removed_cm3}


def calc_sheet(geom: dict, material, params: dict | None = None, qty: int = 1):
    p = {**SHEET_DEFAULTS, **(params or {})}
    qty = max(1, int(qty))
    t = float(p.get("thickness") or geom.get("thickness") or 3.0)
    cut_len = float(p.get("cut_length") or geom.get("cut_length") or 0.0)
    pierces = int(p.get("pierces") or geom.get("pierces") or 1)
    ref = SHEET_SPEED.get(getattr(material, "category", ""), 2500)
    speed = ref / max(t, 0.5)                                    # mm/min
    cut_min = cut_len / speed if speed else 0.0
    pierce_min = pierces * p["pierce_s"] / 60
    per_part = cut_min + pierce_min
    batch_min = p["setup_min"] + per_part * qty
    bw, bh = geom.get("blank") or (geom["x"], geom["y"])
    blank_mm2 = (bw + p["kerf"] * 2) * (bh + p["kerf"] * 2) / max(p["nest_util"], 0.05)
    density = getattr(material, "density", 7.85)
    sheet_kg = blank_mm2 * t / 1000 * density / 1000
    part_kg = float(geom.get("flat_area") or bw * bh) * t / 1000 * density / 1000
    price = material.price() if hasattr(material, "price") else 2.0
    mat_cost = sheet_kg * price * qty
    mach_cost = batch_min / 60 * p["rate"]
    sub = mach_cost + mat_cost
    margin = sub * p["margin_pct"] / 100
    total = sub + margin
    return {"process": "sheet", "qty": qty, "material": getattr(material, "name", "?"), "thickness": t,
            "cut_length": cut_len, "pierces": pierces, "cut_speed": speed, "cut_min": cut_min,
            "pierce_min": pierce_min, "setup_min": p["setup_min"], "per_part_min": per_part,
            "batch_min": batch_min, "batch_hr": batch_min / 60, "sheet_kg": sheet_kg, "part_kg": part_kg,
            "machine_cost": mach_cost, "material_cost": mat_cost, "subtotal": sub, "margin": margin,
            "total": total, "unit": total / qty, "rate": p["rate"]}


def quote(geom, material, process="cnc", params=None, qty=1, cycle_min=None):
    if process == "sheet":
        return calc_sheet(geom, material, params, qty)
    return calc_cnc(geom, material, params, qty, cycle_min)


def qty_breaks(geom, material, process="cnc", params=None, cycle_min=None, breaks=QTY_BREAKS):
    return [(q, quote(geom, material, process, params, q, cycle_min)["unit"]) for q in breaks]


def cam_cycle_min(doc, name) -> float | None:
    """CAM cycle time of `name` if the document's CAM setup machines that object."""
    try:
        cam_setup = doc.cam or {}
        if not cam_setup.get("ops") or (cam_setup.get("object") or doc.objects[0].name) != name:
            return None
        from .cam import plan
        return plan(doc)["time_s"] / 60
    except Exception:  # noqa: BLE001
        return None


def quote_object(doc, name=None, process=None, params=None, qty=1):
    """Quote one object of a Daedalus document. process None → sheet if the part is plate-like."""
    name = name or doc.active_object
    body = doc.result.bodies.get(name)
    if body is None:
        raise ValueError(f"{name} has no solid to quote")
    g = part_geometry(body)
    process = process or ("sheet" if g.get("sheet") else "cnc")
    mat = doc.material_of(name)
    r = quote(g, mat, process, params, qty, cam_cycle_min(doc, name) if process == "cnc" else None)
    r.update(object=name, geometry={k: v for k, v in g.items() if k != "holes"}, holes=len(g.get("holes", [])))
    return r


# ------------------------------------------------------------------ shop: machines, bookings, history
class Shop:
    """Machines, bookings and quote history, kept in ~/.daedalus/shop.json (shared by every window)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else _shop_path()
        self.data = {"machines": [], "bookings": [], "history": [], "params": {}}
        try:
            self.data.update(json.loads(self.path.read_text()))
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    @property
    def machines(self):
        return self.data["machines"]

    @property
    def bookings(self):
        return self.data["bookings"]

    @property
    def history(self):
        return self.data["history"]

    def add_machine(self, name, kind="cnc", rate=75.0, daily_hours=8.0):
        if not name.strip():
            raise ValueError("Machine name is empty")
        m = {"id": "m" + uuid.uuid4().hex[:8], "name": name.strip(), "type": kind, "rate": float(rate),
             "daily_hours": float(daily_hours)}
        self.machines.append(m)
        self.save()
        return m

    def remove_machine(self, mid):
        self.data["machines"] = [m for m in self.machines if m["id"] != mid]
        self.data["bookings"] = [b for b in self.bookings if b["machine"] != mid]
        self.save()

    def book(self, machine_id, job, hours, start: str | None = None, total=None, process="cnc"):
        if not any(m["id"] == machine_id for m in self.machines):
            raise ValueError("Unknown machine")
        b = {"id": "b" + uuid.uuid4().hex[:8], "machine": machine_id, "job": job, "hours": max(float(hours), 0.1),
             "start": start or date.today().isoformat(), "total": total, "process": process}
        self.bookings.append(b)
        self.save()
        return b

    def remove_booking(self, bid):
        self.data["bookings"] = [b for b in self.bookings if b["id"] != bid]
        self.save()

    def log(self, q: dict, job=""):
        self.history.append({"time": time.strftime("%Y-%m-%d %H:%M"), "job": job or q.get("object", ""),
                             "process": q["process"], "material": q["material"], "qty": q["qty"],
                             "hours": round(q["batch_hr"], 3), "unit": round(q["unit"], 2),
                             "total": round(q["total"], 2)})
        del self.history[:-500]
        self.save()

    def allocation(self, machine) -> dict:
        """{iso date: hours used}: each booking fills its machine's free daily hours from its start date."""
        used: dict = {}
        cap = float(machine["daily_hours"])
        for b in sorted((b for b in self.bookings if b["machine"] == machine["id"]), key=lambda b: b["start"]):
            left = b["hours"]
            d = date.fromisoformat(b["start"])
            guard = 0
            while left > 1e-3 and guard < 3650:
                guard += 1
                k = d.isoformat()
                free = max(cap - used.get(k, 0.0), 0.0)
                if free > 0:
                    take = min(free, left)
                    used[k] = used.get(k, 0.0) + take
                    left -= take
                if left > 1e-3:
                    d += timedelta(days=1)
        return used

    def finish_date(self, booking) -> str:
        m = next((m for m in self.machines if m["id"] == booking["machine"]), None)
        if m is None:
            return booking["start"]
        # replay allocation up to and including this booking
        used: dict = {}
        cap = float(m["daily_hours"])
        last = booking["start"]
        for b in sorted((b for b in self.bookings if b["machine"] == m["id"]), key=lambda b: b["start"]):
            left, d = b["hours"], date.fromisoformat(b["start"])
            while left > 1e-3:
                k = d.isoformat()
                free = max(cap - used.get(k, 0.0), 0.0)
                take = min(free, left)
                if take > 0:
                    used[k] = used.get(k, 0.0) + take
                    left -= take
                    if b is booking:
                        last = k
                if left > 1e-3:
                    d += timedelta(days=1)
            if b is booking:
                break
        return last

    def next_free(self, machine, hours, after: str | None = None) -> str:
        """First start date from which `hours` fit without waiting (greedy, same rule as allocation)."""
        used = self.allocation(machine)
        d = date.fromisoformat(after) if after else date.today()
        for _ in range(3650):
            if used.get(d.isoformat(), 0.0) < float(machine["daily_hours"]) - 1e-6:
                return d.isoformat()
            d += timedelta(days=1)
        return d.isoformat()


def text(q: dict) -> str:
    """Plain-text quote (clipboard / e-mail)."""
    rows = [("Part", q.get("object", "")), ("Process", "CNC machining" if q["process"] == "cnc" else
                                             "Sheet / laser"), ("Material", q["material"]), ("Quantity", q["qty"])]
    if q["process"] == "cnc":
        rows += [("Stock", " × ".join(f"{v:.1f}" for v in q["stock_mm"]) + " mm"),
                 ("Time per part", f"{q['per_part_min']:.1f} min ({q['time_source']})"),
                 ("Setup", f"{q['setup_min']:.0f} min")]
    else:
        rows += [("Thickness", f"{q['thickness']:.2f} mm"), ("Cut length", f"{q['cut_length']:.0f} mm"),
                 ("Pierces", q["pierces"]), ("Time per part", f"{q['per_part_min']:.2f} min")]
    rows += [("Batch machine time", f"{q['batch_hr']:.2f} h"), ("Machine", f"${q['machine_cost']:.2f}"),
             ("Material", f"${q['material_cost']:.2f}"), ("Margin", f"${q['margin']:.2f}"),
             ("TOTAL", f"${q['total']:.2f}"), ("Per part", f"${q['unit']:.2f}")]
    w = max(len(str(k)) for k, _ in rows)
    return "\n".join(f"{str(k).ljust(w)}  {v}" for k, v in rows)


def _round(v, n=2):
    return round(v, n) if isinstance(v, float) and math.isfinite(v) else v
