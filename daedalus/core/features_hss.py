"""HSS member feature: a straight hollow structural section from a standard table (AISC, CSA, EN 10219/10210) or
custom dimensions, with mitred / square ends and optional cap plates. Goes into the cut list with its weight."""
from __future__ import annotations

import math

from build123d import Align, Box, Plane, Vector, extrude

from . import hss as H
from .features import BuildContext, Feature, FeatureError, Param, as_part

AXES = {"Z": ((0, 1, 0), (0, 0, 1)), "X": ((0, 1, 0), (1, 0, 0)), "Y": ((-1, 0, 0), (0, 1, 0))}


class HSSMember(Feature):
    TYPE = "hss"
    LABEL = "HSS member"
    DESCRIPTION = ("Straight hollow structural section (square / rectangular / round tube). standard AISC | CSA | "
                   "EN10219 | EN10210 picks the size by designation (HSS4x4x1/4, HSS 102x102x6.4, RHS 200x100x8, "
                   "HSS6.625x0.280); standard custom uses shape, width, height, thickness and the corner rule — "
                   "any non-standard size. The member starts at (x, y, z) and runs along the axis; mitre angles "
                   "cut the ends; it is listed in the cut list with its weight.")
    PARAMS = [
        Param("standard", "choice", "AISC", "Size table, or custom", H.STANDARDS),
        Param("designation", "text", "HSS4x4x1/4", "Standard designation (ignored for custom)"),
        Param("shape", "choice", "square", "Custom shape", H.SHAPES),
        Param("width", "float", 101.6, "Custom width B / OD (mm)", minimum=0.1),
        Param("height", "float", 101.6, "Custom depth H (rect) (mm)", minimum=0.1),
        Param("thickness", "float", 6.35, "Custom wall t (mm)", minimum=0.01),
        Param("corner_rule", "choice", "standard", "Outside corner radius: the standard's rule, a multiple of t, "
              "custom radius or sharp", H.CORNER_RULES),
        Param("corner_radius", "float", 0.0, "Outside corner radius when corner_rule = custom (mm)", minimum=0.0),
        Param("length", "float", 1000.0, "Member length along its centreline (mm)", minimum=0.1),
        Param("axis", "choice", "Z", "Direction of the member", ["X", "Y", "Z", "-X", "-Y", "-Z"]),
        Param("x", "float", 0.0, "Start point X (mm)"),
        Param("y", "float", 0.0, "Start point Y (mm)"),
        Param("z", "float", 0.0, "Start point Z (mm)"),
        Param("rotation", "float", 0.0, "Rotate the section about the member axis (deg)"),
        Param("start_miter", "float", 0.0, "Start end cut angle, tilting about the section width axis (deg, 0 = "
              "square)"),
        Param("end_miter", "float", 0.0, "End cut angle (deg, 0 = square)"),
        Param("end_caps", "bool", False, "Weld cap plates on both ends"),
        Param("cap_thickness", "float", 6.0, "Cap plate thickness (mm)", minimum=0.1),
        Param("operation", "choice", "join", "join | cut", ["join", "cut"]),
    ]

    def section(self) -> H.HSS:
        p = self.params
        try:
            return H.resolve(p["standard"], p.get("designation", ""), p["shape"], p["width"], p["height"],
                             p["thickness"], p["corner_rule"], p["corner_radius"])
        except (KeyError, ValueError) as e:
            raise FeatureError(str(e.args[0] if e.args else e)) from e

    def summary(self):
        try:
            h = self.section()
        except FeatureError:
            return "HSS ?"
        from . import units as U
        return f"{h.designation} × {U.fmt(self.params['length'])}"

    def plane(self) -> Plane:
        p = self.params
        ax = p["axis"].lstrip("-")
        x_dir, z_dir = AXES[ax]
        if p["axis"].startswith("-"):
            z_dir = tuple(-c for c in z_dir)
            x_dir = tuple(-c for c in x_dir)
        pl = Plane(origin=(p["x"], p["y"], p["z"]), x_dir=x_dir, z_dir=z_dir)
        return pl.rotated((0, 0, p["rotation"])) if p["rotation"] else pl

    def build(self, ctx: BuildContext) -> None:
        p = self.params
        h = self.section()
        L = p["length"]
        a0, a1 = math.radians(p["start_miter"]), math.radians(p["end_miter"])
        for a in (a0, a1):
            if abs(a) >= math.radians(80):
                raise FeatureError("Mitre angles must be under 80°")
        half = h.H / 2
        e0, e1 = half * abs(math.tan(a0)) + 1, half * abs(math.tan(a1)) + 1
        if e0 + e1 - 2 >= L:
            raise FeatureError("Member too short for those mitre angles")
        pl = self.plane()
        face = H.profile(h)
        loc = Plane(origin=(0, 0, -e0 if a0 else 0))
        tool = extrude(loc * face, L + (e0 if a0 else 0) + (e1 if a1 else 0))
        S = 4 * (max(h.B, h.H) + L)
        cuts = []                                     # outward cutting planes at the two ends
        for z0, a, out in ((0.0, a0, -1.0), (L, a1, 1.0)):
            n = Vector(0, -out * math.sin(a), out * math.cos(a))      # Rx(a)·(0, 0, ±1)
            cuts.append(Plane(origin=(0, 0, z0), z_dir=n))
        for (z0, a, _o), cp in zip(((0.0, a0, -1.0), (L, a1, 1.0)), cuts):
            if a:
                tool = tool - cp * Box(S, S, S, align=(Align.CENTER, Align.CENTER, Align.MIN))
        part = tool
        if p["end_caps"]:
            from build123d import Face
            f0 = face.faces()[0] if hasattr(face, "faces") else face
            prism = extrude(Plane(origin=(0, 0, -S)) * Face(f0.outer_wire()), 2 * S + L)
            for cp in cuts:
                part = part + (prism & (cp * Box(S, S, p["cap_thickness"], align=(Align.CENTER, Align.CENTER, Align.MIN))))
        part = part.moved(pl.location)
        ctx.apply(as_part(part), p["operation"], self.name)
        props = H.properties(h)
        lp = L + half * (abs(math.tan(a0)) + abs(math.tan(a1)))
        members = [{"length": round(L, 2), "long_point": round(lp, 2), "straight": True, "section": h.designation,
                    "kg_per_m": props["mass_per_m"], "joints": 0}]
        if p["end_caps"]:
            members += [{"length": p["cap_thickness"], "long_point": p["cap_thickness"], "straight": True,
                         "item": "end cap", "section": f"End cap t{p['cap_thickness']:g}"}] * 2
        ctx.cutlists[self.name] = members


NEW_TYPES = [HSSMember]
