"""Copilot tools for v2.8: equations, configurations, subassemblies, component patterns, drawing annotations.
(New feature types — curves, surfaces, sheet-metal tools — get add_* tools automatically.)"""
from __future__ import annotations

import json

S = {"type": "string"}
N = {"type": "number"}

TOOLS28 = [
    {"name": "set_variable", "description": "Create / change a global variable (expression allowed, trig in degrees).",
     "input_schema": {"type": "object", "properties": {"name": S, "expr": S, "delete": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "add_equation",
     "description": ("Drive a parameter by an expression of variables and other parameters: target 'Extrude1.depth' "
                     "or a sketch dimension 'Sketch1.D2' (constraint index). expr e.g. 'W*2', 'Hole1.diameter + 1'. "
                     "delete=true removes the equation on target; suppressed toggles it."),
     "input_schema": {"type": "object", "properties": {"target": S, "expr": S, "delete": {"type": "boolean"},
                                                       "suppressed": {"type": "boolean"}}, "required": ["target"]}},
    {"name": "configuration",
     "description": ("Configurations (part/assembly variants). action add (name, copy_from?) | set (name + params "
                     "{'Feature.param': value|null}, suppress {'Feature': bool|null}, variables {name: expr|null}, "
                     "material, objects {obj: {suppressed: bool}}, description, new_name) | activate (name) | delete "
                     "(name) | table (returns the design table)."),
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["add", "set", "activate", "delete", "table"]}, "name": S,
         "copy_from": S, "params": {"type": "object"}, "suppress": {"type": "object"}, "variables": {"type": "object"},
         "material": S, "objects": {"type": "object"}, "description": S, "new_name": S},
         "required": ["action"]}},
    {"name": "subassembly",
     "description": ("Subassemblies: action make (objects, name?, rigid=true, parent?) | set (name, rigid?, new_name?, "
                     "parent?) | dissolve (name) | move (objects, name or '' for top level) | tree (structure) | save "
                     "(name, path)."),
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["make", "set", "dissolve", "move", "tree", "save"]}, "name": S,
         "objects": {"type": "array", "items": S}, "rigid": {"type": "boolean"}, "parent": S, "new_name": S,
         "path": S}, "required": ["action"]}},
    {"name": "component_pattern",
     "description": ("Pattern / mirror components (instances share the seed's geometry and follow it). action add: "
                     "kind linear (dir1 'X'|[x,y,z], spacing1, count1, dir2, spacing2, count2) | circular (axis "
                     "'Z' or {p,d}, count, angle) | mirror (plane 'YZ' or {p,n}); seeds [objects]. action set (name + "
                     "params) | delete (name)."),
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["add", "set", "delete"]}, "kind": S, "name": S,
         "seeds": {"type": "array", "items": S}, "dir1": {}, "spacing1": N, "count1": {"type": "integer"},
         "dir2": {}, "spacing2": N, "count2": {"type": "integer"}, "axis": {}, "count": {"type": "integer"},
         "angle": N, "plane": {}}, "required": ["action"]}},
    {"name": "set_drawing_view",
     "description": ("Change a drawing view: breaks [{axis:'x'|'y', a, b}] in view mm (broken view, [] removes), "
                     "label, scale, x, y, hidden."),
     "input_schema": {"type": "object", "properties": {
         "drawing": S, "view": S, "breaks": {"type": "array"}, "label": S, "scale": N, "x": N, "y": N,
         "hidden": {"type": "boolean"}}, "required": ["view"]}},
    {"name": "add_revision", "description": "Add a revision-table row (letter auto-increments; title block follows).",
     "input_schema": {"type": "object", "properties": {"drawing": S, "description": S, "rev": S, "approved": S,
                                                       "zone": S}, "required": ["description"]}},
    {"name": "add_ordinate_dims",
     "description": "Ordinate dimensions on a sheet (sheet mm): origin [x,y], points [[x,y],...], axis x|y.",
     "input_schema": {"type": "object", "properties": {"drawing": S, "origin": {"type": "array"},
                                                       "points": {"type": "array"}, "axis": S, "offset": N},
                      "required": ["origin", "points"]}},
]
TOOLS28 += [
    {"name": "design_calc",
     "description": ("Machine-design calculators (design-data-handbook procedures): call with no 'calc' to list them "
                     "with their inputs; otherwise calc id + inputs → results. Units mm, N, MPa, N·m, kW, rpm, deg. "
                     "Ids include shaft_asme, parallel_key, spur_gear, helical_gear, worm_gear, fatigue, "
                     "principal_stress, column_buckling, hertz_contact, flange_coupling, plate_clutch, band_brake, "
                     "block_brake, thick_cylinder, section_properties, factor_of_safety. use_material=true fills "
                     "E/ν/Sy/Su from the active object's material."),
     "input_schema": {"type": "object", "properties": {"calc": S, "inputs": {"type": "object"},
                                                       "use_material": {"type": "boolean"}, "object": S}}},
    {"name": "search_reference",
     "description": ("Search the user's reference library (their own handbooks, e.g. the Design Data Handbook) for a "
                     "topic; returns pages with snippets. page=N with doc returns that page's full text. Cite the "
                     "document and page when you use it."),
     "input_schema": {"type": "object", "properties": {"query": S, "doc": S, "page": {"type": "integer"},
                                                       "limit": {"type": "integer"}}}},
]
TOOLS28 += [
    {"name": "set_units",
     "description": ("Document unit system (display + typed input only; geometry stays in mm/deg and every tool "
                     "still takes mm). name: MMGS | SI_mm | MKS | CGS | IPS | IPS_frac | FPS | CONST_US (ft-in-"
                     "fraction architectural) | CONST_US_ENG (decimal feet) | CONST_SI (construction metric). "
                     "units {quantity: unit} overrides (quantities: length, angle, slope, area, volume, mass, "
                     "density, linear_density, force, torque, pressure, linear_load, area_load, stiffness, time, "
                     "frequency, angular_velocity, velocity, acceleration, energy, power, temperature, temp_delta, "
                     "area_moment, section_modulus, mass_moment, flow …). decimals {length|angle|default: n}, "
                     "length_format decimal|fractional|ft-in, denominator 2…128. No args: report the current "
                     "system."),
     "input_schema": {"type": "object", "properties": {"name": S, "units": {"type": "object"},
                                                       "decimals": {"type": "object"}, "length_format": S,
                                                       "denominator": {"type": "integer"}}}},
    {"name": "convert_units",
     "description": ("Convert a value between units, or read a typed value: value 5' 6-1/4 in, 3 kip, 50 psf, "
                     "'4/12' (slope), '72°F' + quantity (length default) → base and the requested / every unit."),
     "input_schema": {"type": "object", "properties": {"value": S, "quantity": S, "to": S}, "required": ["value"]}},
    {"name": "measure",
     "description": ("Measure entities: refs [{obj?, kind vertex|edge|face|body|point, index (into the body's "
                     "vertices/edges/faces), p [x,y,z] for point}]. One ref: coordinates / length, radius, arc / "
                     "area, perimeter, surface type, tilt / volume, mass, weight, COM. Two: min / centre / max "
                     "distance, ΔX/ΔY/ΔZ, normal distance, angle, slope. More: totals, path length. Results in the "
                     "document's units and raw base values (mm, deg, kg, N)."),
     "input_schema": {"type": "object", "properties": {"refs": {"type": "array", "items": {"type": "object"}}},
                      "required": ["refs"]}},
    {"name": "hss_sizes",
     "description": ("HSS tables: standard AISC | CSA | EN10219 | EN10210, shape square|rect|round, query filter "
                     "('4x4', '102x102', '6.625') → designations with size, wall and weight; designation → full "
                     "section properties (A, I, S, Z, r, J, weight). Then add_hss (straight member) or add_frame "
                     "with standard + designation (members along a sketch)."),
     "input_schema": {"type": "object", "properties": {"standard": S, "shape": S, "query": S, "designation": S,
                                                       "limit": {"type": "integer"}}}},
]
NAMES = {t["name"] for t in TOOLS28}


def run28(doc, name, args):
    J = lambda o: json.dumps(o, default=lambda x: float(x) if hasattr(x, "__float__") else str(x))  # noqa: E731
    d = doc
    if name == "set_variable":
        if args.get("delete"):
            d.delete_variable(args["name"], checkpoint=False)
        else:
            d.set_variable(args["name"], args.get("expr", "0"), checkpoint=False)
        return J({"ok": True, "variables": d.variable_values(), "errors": d.result.errors.get("Equations")})
    if name == "add_equation":
        t = args["target"]
        if args.get("delete"):
            d.delete_equation(t, checkpoint=False)
        elif any(e["target"] == t for e in d.equations):
            d.set_equation(t, args.get("expr"), args.get("suppressed"), checkpoint=False)
        else:
            d.add_equation(t, args["expr"], checkpoint=False)
        return J({"ok": True, "values": d.eq_values, "errors": d.eq_errors})
    if name == "configuration":
        a = args["action"]
        if a == "add":
            d.add_configuration(args["name"], args.get("description", ""), args.get("copy_from"), checkpoint=False)
        elif a == "set":
            d.set_configuration(args["name"], checkpoint=False, **{k: args[k] for k in (
                "params", "suppress", "variables", "material", "objects", "description", "new_name") if k in args})
        elif a == "activate":
            d.activate_configuration(args["name"])
        elif a == "delete":
            d.delete_configuration(args["name"], checkpoint=False)
        cols, rows = d.config_table()
        return J({"ok": True, "active": d.active_config, "columns": cols, "rows": rows,
                  "errors": d.result.errors, "mass": d.mass_properties().get("mass_kg")})
    if name == "subassembly":
        a = args["action"]
        if a == "make":
            s = d.make_subassembly(args.get("name"), args["objects"], bool(args.get("rigid", True)),
                                   args.get("parent"), checkpoint=False)
            return J({"ok": True, "subassembly": s, "tree": d.tree()})
        if a == "set":
            kw = {k: args[k] for k in ("rigid", "new_name") if k in args}
            if "parent" in args:
                kw["parent"] = args["parent"] or None
            d.set_subassembly(args["name"], checkpoint=False, **kw)
        elif a == "dissolve":
            d.dissolve_subassembly(args["name"], checkpoint=False)
        elif a == "move":
            d.move_to_subassembly(args["objects"], args.get("name") or None, checkpoint=False)
        elif a == "save":
            return J({"ok": True, "path": d.save_subassembly(args["name"], args["path"])})
        return J({"ok": True, "tree": d.tree()})
    if name == "component_pattern":
        a = args["action"]
        keys = ("dir1", "spacing1", "count1", "dir2", "spacing2", "count2", "axis", "count", "angle", "plane")
        if a == "add":
            p = d.add_component_pattern(args["kind"], args["seeds"], checkpoint=False, name=args.get("name"),
                                        **{k: args[k] for k in keys if k in args})
            return J({"ok": True, "pattern": p["name"], "instances": p["instances"]})
        if a == "set":
            d.set_component_pattern(args["name"], checkpoint=False, **{k: args[k] for k in keys if k in args})
            return J({"ok": True, "instances": d.comp_pattern(args["name"])["instances"]})
        d.delete_component_pattern(args["name"], checkpoint=False)
        return J({"ok": True})
    if name == "set_drawing_view":
        v = d.set_drawing_view(args["view"], args.get("drawing"), breaks=args.get("breaks"), label=args.get("label"),
                               scale=args.get("scale"), x=args.get("x"), y=args.get("y"), hidden=args.get("hidden"))
        return J({"ok": True, "id": v["id"], "bbox": v.get("_bbox"), "break_shift": v.get("_break_shift")})
    if name == "add_revision":
        r = d.add_revision(args["description"], args.get("drawing"), rev=args.get("rev"),
                           approved=args.get("approved", ""), zone=args.get("zone", ""))
        return J({"ok": True, "revision": r})
    if name == "design_calc":
        from ..design import calcs as C
        if not args.get("calc"):
            return J([c.spec() for c in C.REGISTRY.values()])
        vals = dict(args.get("inputs") or {})
        if args.get("use_material"):
            m = d.material_of(args.get("object") or d.active_object)
            vals = {**C.prefill_from_material(args["calc"], m), **vals}
        try:
            res = C.run(args["calc"], vals)
        except C.CalcError as e:
            from ..core.features import FeatureError
            raise FeatureError(str(e)) from e
        return J({k: (f"{C.fmt_value(v['value'])} {v['unit']}".strip() + (f" ({v['note']})" if v["note"] else ""))
                  for k, v in res.items()})
    if name == "search_reference":
        from ..design import reference as REF
        if args.get("page") and args.get("doc"):
            return J({"doc": args["doc"], "page": args["page"], "text": REF.page_text(args["doc"], int(args["page"]))})
        if not REF.documents():
            return J({"hits": [], "note": "The reference library is empty — the user can add handbooks in Evaluate ▸ "
                                          "Design data ▸ Reference library."})
        return J({"hits": REF.search(args.get("query", ""), args.get("doc"), int(args.get("limit", 6)))})
    if name == "set_units":
        from ..core import units as U
        kw = {k: args[k] for k in ("name", "units", "decimals", "length_format", "denominator") if k in args}
        sysm = d.set_units(**kw) if kw else d.unit_system()
        return J({"system": sysm.name, "label": sysm.label, "length_format": sysm.length_format,
                  "units": {q: sysm.unit(q) for q in U.Q if q != "ratio"},
                  "example_length": U.fmt(1682.75, "length", sysm)})
    if name == "convert_units":
        from ..core import units as U
        q = args.get("quantity") or "length"
        try:
            base = U.parse(args["value"], q)
            out = {"quantity": q, "base": base, "base_unit": U.base_unit(q), "display": U.fmt(base, q)}
            if args.get("to"):
                out["result"] = U.fmt(base, q, unit=args["to"], places=6)
            else:
                out["all"] = {u: U.fmt(base, q, unit=u, places=6, show_unit=False) for u in U.units_of(q)}
                if q == "length":
                    out["ft_in"] = U.fmt_ft_in(base, 16)
        except (U.UnitError, ZeroDivisionError, ValueError) as e:
            from ..core.features import FeatureError
            raise FeatureError(str(e)) from e
        return J(out)
    if name == "measure":
        from ..core import measure as M
        res = M.measure(d, args["refs"])
        return J({"results": dict(M.format_rows(res)),
                  "base": {lab: v for lab, v, _q in res["rows"] if not isinstance(v, str)}})
    if name == "hss_sizes":
        from ..core import hss as H
        std = args.get("standard") or "AISC"
        if args.get("designation"):
            try:
                h = H.lookup(std, args["designation"])
            except KeyError as e:
                from ..core.features import FeatureError
                raise FeatureError(str(e.args[0])) from e
            from ..core import units as U
            pr = H.properties(h)
            return J({"section": h.to_dict(), "properties_base": pr,
                      "properties": {H.PROP_LABEL.get(k, k): (U.fmt(v, H.PROP_QUANTITY[k]) if H.PROP_QUANTITY.get(k)
                                                              else round(v, 3)) for k, v in pr.items()}})
        rows = H.sizes(std, args.get("shape"), args.get("query", ""))
        lim = int(args.get("limit", 40))
        return J({"standard": std, "count": len(rows), "sizes": [
            {"designation": h.designation, "B": h.B, "H": h.H, "t": h.t,
             "kg_per_m": round(H.properties(h)["mass_per_m"], 3)} for h in rows[:lim]]})
    if name == "add_ordinate_dims":
        items = d.add_ordinate_dims(args["origin"], args["points"], args.get("axis", "x"), args.get("drawing"),
                                    offset=float(args.get("offset", 10.0)))
        return J({"ok": True, "dims": len(items)})
    return None
