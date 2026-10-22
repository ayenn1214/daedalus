"""Claude tool layer.

Tool schemas are generated from the feature registry, so every modeling
operation the GUI can do, Claude can do — through the same Document API.
"""
from __future__ import annotations

import json

from ..core.document import MATERIALS, Document
from ..core.features import FEATURE_TYPES, FeatureError


def _feature_tool(cls) -> dict:
    props = {p.name: p.json_schema() for p in cls.PARAMS}
    props["name"] = {"type": "string", "description": "Optional custom feature name"}
    props["layer"] = {"type": "string", "description": "Layer to put it on (default: the active layer)"}
    props["object"] = {"type": "string", "description": "Object (body) to build it in (default: the active object)"}
    return {
        "name": f"add_{cls.TYPE}",
        "description": f"Add a {cls.LABEL} feature at the end of the history. {cls.DESCRIPTION}",
        "input_schema": {"type": "object", "properties": props},
    }


FACE_DESC = ("Faces of the study's object: list of face specs — face index, {\"face\": \"top\"|\"bottom\"|\"left\"|"
             "\"right\"|\"front\"|\"back\"}, {\"face_near\": [x,y,z], \"normal\": [nx,ny,nz]}, "
             "{\"axis_near\": [x,y,z]} (closest cylinder, e.g. a hole)")

TOOLS: list[dict] = [
    {
        "name": "get_model",
        "description": "Return the feature history (params, errors, suppression), material and mass properties.",
        "input_schema": {"type": "object", "properties": {}},
    },
    *[_feature_tool(c) for c in FEATURE_TYPES.values() if not c.INTERNAL],
    {
        "name": "edit_feature",
        "description": "Change parameters of an existing feature. Only pass the keys that change.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "params": {"type": "object"}},
            "required": ["name", "params"],
        },
    },
    {
        "name": "add_layer",
        "description": "Create a layer (e.g. 'Frame', 'Hardware', 'Cut-outs'). Optionally make it the active layer.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "color": {"type": "string", "description": "#rrggbb"},
            "active": {"type": "boolean"}}, "required": ["name"]},
    },
    {
        "name": "set_layer",
        "description": ("Change a layer: colour, visible (display only), locked (blocks edits), in_model "
                        "(false leaves its features out of the rebuild), new_name, active."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "color": {"type": "string"}, "visible": {"type": "boolean"},
            "locked": {"type": "boolean"}, "in_model": {"type": "boolean"}, "new_name": {"type": "string"},
            "active": {"type": "boolean"}}, "required": ["name"]},
    },
    {
        "name": "move_to_layer",
        "description": "Move features onto a layer.",
        "input_schema": {"type": "object", "properties": {
            "features": {"type": "array", "items": {"type": "string"}}, "layer": {"type": "string"}},
            "required": ["features", "layer"]},
    },
    {
        "name": "add_object",
        "description": ("Create a new object (separate body) and make it active, so following add_* calls "
                        "build into it. Model each part at its own origin, then place/mate it."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "color": {"type": "string"},
            "translate": {"type": "array", "items": {"type": "number"}}}, "required": ["name"]},
    },
    {
        "name": "set_object",
        "description": "Change an object: fixed (ground), visible, color, new_name, active.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "fixed": {"type": "boolean"}, "visible": {"type": "boolean"},
            "color": {"type": "string"}, "new_name": {"type": "string"}, "active": {"type": "boolean"}},
            "required": ["name"]},
    },
    {
        "name": "place_object",
        "description": "Move an object rigidly: translate (mm) then rotate angle (deg) about a world axis through its origin.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "translate": {"type": "array", "items": {"type": "number"}},
            "axis": {"type": "array", "items": {"type": "number"}}, "angle": {"type": "number"}},
            "required": ["name"]},
    },
    {
        "name": "import_search",
        "description": ("Find parts to import from the rest of Daedalus: other projects (recent + project folders), "
                        "the standard-parts library, inventory parts with CAD, the ISO/ASME fastener & bearing "
                        "library. Returns [{source, id, name, detail}] for import_part."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string",
                                                   "enum": ["project", "std", "inventory", "fastener"]}}},
            "required": []},
    },
    {
        "name": "import_part",
        "description": ("Import a part found with import_search (or source 'file' + a path) as a new object, linked "
                        "to its source. connect_to mates it to a face: flat → sits on it, hole/shaft → concentric; "
                        "fasteners go into the hole with nut/washer. length (mm) for screws/bolts."),
        "input_schema": {"type": "object", "properties": {
            "source": {"type": "string", "enum": ["project", "std", "inventory", "fastener", "file"]},
            "id": {"type": "string"}, "link": {"type": "boolean"}, "length": {"type": "number"},
            "nut": {"type": "boolean"}, "washer": {"type": "boolean"},
            "connect_to": {"type": "object", "properties": {
                "object": {"type": "string"}, "face_index": {"type": "integer"},
                "point": {"type": "array", "items": {"type": "number"}}}}},
            "required": ["source", "id"]},
    },
    {
        "name": "orient_object",
        "description": ("Rotate / set down an object. action: 'rotate' (axis + angle deg about its bounding-box "
                        "centre; on_floor keeps its lowest point where it was), 'drop' (lower onto the floor z=0), "
                        "'face_down' (turn face_index to point straight down, then drop), 'set' (absolute X→Y→Z "
                        "angles in degrees, centre kept)."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["rotate", "drop", "face_down", "set"]},
            "axis": {"type": "array", "items": {"type": "number"}}, "angle": {"type": "number"},
            "on_floor": {"type": "boolean"}, "face_index": {"type": "integer"},
            "angles": {"type": "array", "items": {"type": "number"}}},
            "required": ["name", "action"]},
    },
    {
        "name": "add_mate",
        "description": ("Mate two objects. v2.8 types: tangent (cylinder axis + face, or two cylinders), "
                        "limit_distance / limit_angle (value..value2), width (+extra a2,b2), symmetric (+extra c), "
                        "path (ref {path: '<sketch/curve>'} or {edge_near:[x,y,z]} on the path part), screw (axes, "
                        "value = lead mm/rev), cam (cam part ref {cam_face: i}), slot (slotted part ref {slot_face: i "
                        "of a round end}). Types: coincident (faces flush & facing, axes collinear, point on face), "
                        "distance (value mm), parallel, perpendicular, angle (value deg), concentric/hinge "
                        "(shared axis), lock. A hinge = concentric + a coincident face mate. Rejected if it "
                        "over-defines the assembly."),
        "input_schema": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["coincident", "distance", "parallel", "perpendicular", "angle",
                                                "concentric", "hinge", "lock", "gear", "rack", "tangent",
                                                "limit_distance", "limit_angle", "width", "symmetric", "path",
                                                "screw", "cam", "slot"]},
            "value2": {"type": "number", "description": "limit mates: maximum (value = minimum)"},
            "extra": {"type": "object", "description": "width: {a2:{obj,ref}, b2:{obj,ref}} (the opposite faces) · "
                      "symmetric: {c:{obj,ref}} (symmetry plane) · path: {percent} · slot: {along: free|center|"
                      "percent, percent}"},
            "a": {"type": "object", "properties": {"object": {"type": "string"}, "ref": {"type": "object", "description": "Reference on that object, in its own coordinates. One of: "
                       "{face: top|bottom|front|back|left|right} (extreme planar face) · "
                       "{face_near: [x,y,z], normal: [nx,ny,nz]} · {axis: <feature name>, near: [x,y,z]} "
                       "(cylinder made by that feature) · {axis_near: [x,y,z]} · {point: [x,y,z]} · {face_index: i}"}},
                  "required": ["object", "ref"]},
            "b": {"type": "object", "properties": {"object": {"type": "string"}, "ref": {"type": "object", "description": "Reference on that object, in its own coordinates. One of: "
                       "{face: top|bottom|front|back|left|right} (extreme planar face) · "
                       "{face_near: [x,y,z], normal: [nx,ny,nz]} · {axis: <feature name>, near: [x,y,z]} "
                       "(cylinder made by that feature) · {axis_near: [x,y,z]} · {point: [x,y,z]} · {face_index: i}"}},
                  "required": ["object", "ref"]},
            "value": {"type": "number"}, "flip": {"type": "boolean"}}, "required": ["type", "a", "b"]},
    },
    {
        "name": "set_mate",
        "description": "Change a mate's value, flip or suppressed.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "value": {"type": "number"}, "flip": {"type": "boolean"},
            "suppressed": {"type": "boolean"}}, "required": ["name"]},
    },
    {
        "name": "delete_mate",
        "description": "Delete a mate.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "drive_mate",
        "description": ("Motion check: sweep a distance/angle mate from → to in steps, solving each step. Reports "
                        "whether every step satisfied the mates and any interference at the end."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "from": {"type": "number"}, "to": {"type": "number"},
            "steps": {"type": "integer"}}, "required": ["name", "from", "to"]},
    },
    {
        "name": "set_physics",
        "description": ("Physics simulation settings: gravity [x,y,z] m/s² (default [0,0,-9.81]; [0,0,0] for none), "
                        "duration s, friction coefficient, collisions 'decompose' (exact concave shapes, slower "
                        "first time) | 'hull' (convex hulls, fast) | 'off', timestep s."),
        "input_schema": {"type": "object", "properties": {
            "gravity": {"type": "array", "items": {"type": "number"}}, "duration": {"type": "number"},
            "friction": {"type": "number"}, "collisions": {"type": "string", "enum": ["decompose", "hull", "off"]},
            "timestep": {"type": "number"}}},
    },
    {
        "name": "set_drive",
        "description": ("Put a motor or spring on a mate for physics. On a hinge/concentric/angle mate (rotation): "
                        "speed deg/s, torque N·m, position deg (from the start), spring N·m/deg. On a distance mate "
                        "(sliding): speed mm/s, force N (kind 'torque'), position mm, spring N/mm. Distance/angle "
                        "mates with a drive are released so the drive moves them. kind 'none' removes it. "
                        "max limits motor torque/force; damping applies to springs."),
        "input_schema": {"type": "object", "properties": {
            "mate": {"type": "string"},
            "kind": {"type": "string", "enum": ["speed", "torque", "position", "spring", "none"]},
            "value": {"type": "number"}, "damping": {"type": "number"}, "max": {"type": "number"}},
            "required": ["mate", "kind"]},
    },
    {
        "name": "run_simulation",
        "description": ("Run the rigid-body physics from the current positions (gravity, contacts between parts, "
                        "mates as joints, drives). Returns joints used, how far each object moved, max speeds, "
                        "contacts, drive travel and peak torque/force, end poses, notes. The user sees it play back. "
                        "keep_end=true leaves the objects where the simulation ended."),
        "input_schema": {"type": "object", "properties": {"keep_end": {"type": "boolean"}}},
    },
    {
        "name": "add_study",
        "description": ("Create a simulation study on an object (default: active object; its material is used). "
                        "Types: static (stress, displacement, factor of safety, reactions), frequency (natural "
                        "frequencies, mode shapes), buckling (load factors), thermal (steady/transient), fatigue "
                        "(S-N life from a static study: options source, loading reversed|zero|ratio, cycles, mean, "
                        "surface, reliability), harmonic (forced vibration: fmin, fmax, damping), flow (CFD: mode "
                        "external|internal, fluid air|water|oil (SAE 30)|glycerin, velocity m/s, direction +X…-Z, "
                        "resolution cells across body), assembly (several parts: options objects 'A,B', contact "
                        "bonded|free; fixtures/loads take 'object'; add_bolt for bolts), drop (drop test: height mm, "
                        "steps), topology (SIMP: volume_fraction, iterations; uses its fixtures/loads; then "
                        "topology_to_body). Options can be passed in 'options'. mesh_size mm (default auto)."),
        "input_schema": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["static", "frequency", "buckling", "thermal", "fatigue", "harmonic",
                                                "flow", "assembly", "drop", "topology"]},
            "name": {"type": "string"}, "object": {"type": "string"}, "mesh_size": {"type": "number"},
            "options": {"type": "object"}}, "required": ["type"]},
    },
    {
        "name": "set_study",
        "description": "Change a study's options, mesh_size, object or name (new_name).",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "options": {"type": "object"},
                                                          "mesh_size": {"type": "number"}, "object": {"type": "string"},
                                                          "new_name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "add_fixture",
        "description": ("Hold faces in a static/frequency/buckling/harmonic study. type: fixed | roller (slides in "
                        "its plane; symmetry) | pin (hole rotates about its axis; slide=true also lets it slide) | "
                        "elastic (stiffness N/mm³, normal_only). " + FACE_DESC),
        "input_schema": {"type": "object", "properties": {
            "study": {"type": "string"}, "type": {"type": "string", "enum": ["fixed", "roller", "pin", "elastic"]},
            "faces": {"type": "array"}, "slide": {"type": "boolean"}, "stiffness": {"type": "number"},
            "object": {"type": "string", "description": "Assembly studies: the part the faces are on"},
            "normal_only": {"type": "boolean"}}, "required": ["study", "type", "faces"]},
    },
    {
        "name": "add_load",
        "description": ("Load in a static/buckling/harmonic study. force: value N, direction [x,y,z] | '+X'…'-Z' | "
                        "'normal' (into a flat face); pressure: value MPa into the faces; torque: value N·m about "
                        "the face's axis; bearing: value N, direction (on a hole); gravity: value m/s² (9.81), "
                        "direction (no faces); centrifugal: value rpm, direction = axis, point = a point on the axis "
                        "(no faces). " + FACE_DESC),
        "input_schema": {"type": "object", "properties": {
            "study": {"type": "string"},
            "type": {"type": "string", "enum": ["force", "pressure", "torque", "bearing", "gravity", "centrifugal"]},
            "faces": {"type": "array"}, "value": {"type": "number"}, "direction": {},
            "object": {"type": "string", "description": "Assembly studies: the part the faces are on"},
            "point": {"type": "array", "items": {"type": "number"}}}, "required": ["study", "type"]},
    },
    {
        "name": "add_thermal_condition",
        "description": ("Thermal study boundary condition. temperature: value °C; convection: h W/(m²K), ambient °C; "
                        "heat_flux: value W/m²; heat_power: value W on the faces; radiation: emissivity, ambient °C; "
                        "heat_volume: value W generated in the whole part (no faces). " + FACE_DESC),
        "input_schema": {"type": "object", "properties": {
            "study": {"type": "string"},
            "type": {"type": "string", "enum": ["temperature", "convection", "heat_flux", "heat_power", "radiation",
                                                "heat_volume"]},
            "faces": {"type": "array"}, "value": {"type": "number"}, "h": {"type": "number"},
            "ambient": {"type": "number"}, "emissivity": {"type": "number"}}, "required": ["study", "type"]},
    },
    {
        "name": "remove_study_item",
        "description": "Remove a fixture/load/thermal condition by index (kind: fixtures | loads | thermal).",
        "input_schema": {"type": "object", "properties": {"study": {"type": "string"}, "kind": {"type": "string"},
                                                          "index": {"type": "integer"}},
                         "required": ["study", "kind", "index"]},
    },
    {
        "name": "run_study",
        "description": ("Solve a study and return its results summary (max stress, displacement, factor of safety, "
                        "reactions / frequencies / load factors / temperatures / fatigue life & damage / flow "
                        "forces, Cd, pressure drop). The user sees the result plot. Can take seconds to minutes."),
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "import_file",
        "description": ("Import a file from the user's computer (absolute path). STEP/IGES/BREP/STL/OBJ/3MF → one "
                        "object per part (names, colours, placement kept; meshes become solids you can cut and "
                        "sketch on); DXF → a new editable sketch in the active object (on 'plane'); SolidWorks "
                        ".sldprt/.sldasm → via SolidWorks if installed (with mates). Returns objects/features/mates "
                        "created and notes."),
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "plane": {"type": "string", "description": "DXF sketch plane (default XY)"}},
            "required": ["path"]},
    },
    {
        "name": "export_file",
        "description": ("Write the model to a file (absolute path; format from the extension): .step (assembly "
                        "with names/colours/placement), .iges, .brep, .stl, .obj, .3mf, .glb/.gltf. For .dxf pass "
                        "'sketch' (a 2D sketch name)."),
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "sketch": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "check_interference",
        "description": "Overlap volume between every pair of objects at the current position.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_dimension",
        "description": ("Change one driving dimension of a constrained sketch (by its index in "
                        "geometry.constraints). The solver re-solves and the part rebuilds."),
        "input_schema": {
            "type": "object",
            "properties": {"sketch": {"type": "string"}, "index": {"type": "integer"},
                           "value": {"type": "number"}},
            "required": ["sketch", "index", "value"],
        },
    },
    {
        "name": "delete_feature",
        "description": "Delete a feature. Dependent features will fail until fixed.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "suppress_feature",
        "description": "Suppress or unsuppress a feature.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "suppressed": {"type": "boolean"}},
            "required": ["name", "suppressed"],
        },
    },
    {
        "name": "set_material",
        "description": ("Set a material (affects mass, physics, FEA). Without 'object' it is the document "
                        "material used by every object that has none of its own; with 'object' it is that "
                        "object's material ('' resets it). Use list_materials for exact names, e.g. "
                        "'Aluminum 6061-T6', 'Steel ASTM A36', 'Stainless 304 (annealed)', 'Nylon 6/6 (dry)'."),
        "input_schema": {"type": "object",
                         "properties": {"material": {"type": "string"}, "object": {"type": "string"}},
                         "required": ["material"]},
    },
    {
        "name": "list_materials",
        "description": ("Material library with key properties (density, E, Poisson, yield, UTS, fatigue, thermal). "
                        "Filter by search text and/or category (Steel, Stainless steel, Cast iron, Aluminum, "
                        "Copper alloy, Titanium, Magnesium, Nickel alloy, Zinc, Plastic, Elastomer, Composite, "
                        "Wood, Ceramic & glass, Custom)."),
        "input_schema": {"type": "object", "properties": {"search": {"type": "string"},
                                                          "category": {"type": "string"}}},
    },
    {
        "name": "add_custom_material",
        "description": ("Define a custom material (saved in the file), starting from 'based_on' and overriding any "
                        "of: density g/cm³, E GPa, nu, yield_ MPa, uts MPa, k W/mK, cp J/kgK, alpha µm/mK, "
                        "fatigue MPa, fatigue_cycles, ductile."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "based_on": {"type": "string"}, "category": {"type": "string"},
            "density": {"type": "number"}, "E": {"type": "number"}, "nu": {"type": "number"},
            "yield_": {"type": "number"}, "uts": {"type": "number"}, "k": {"type": "number"},
            "cp": {"type": "number"}, "alpha": {"type": "number"}, "fatigue": {"type": "number"},
            "fatigue_cycles": {"type": "number"}, "ductile": {"type": "boolean"}}, "required": ["name"]},
    },
]


NEW_TOOLS = [
    # ---- production
    {"name": "insert_part",
     "description": ("Insert a standard library part as a new object: kind 'socket head cap screw' | 'hex bolt' | "
                     "'flat head screw' | 'hex nut' | 'washer' | 'ball bearing'; size M3…M24, #4-40…3/4-10, or a "
                     "bearing (625…6206); length mm for screws. With target_object + hole (face spec of a hole/bore "
                     "on it) it is placed in that hole and mated; nut/washer add them on the same axis. near = "
                     "[x,y,z] world point choosing the hole end the head sits at."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "size": {"type": "string"}, "length": {"type": "number"},
         "target_object": {"type": "string"}, "hole": {}, "near": {"type": "array", "items": {"type": "number"}},
         "nut": {"type": "boolean"}, "washer": {"type": "boolean"}}, "required": ["kind", "size"]}},
    {"name": "get_bom",
     "description": "Bill of materials: item, part no., description, material, qty, unit/total mass and cost.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "export_bom",
     "description": "Write the BOM (+ cut list, flat patterns) to .xlsx (Excel) or .csv (absolute path).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "insert_part_file",
     "description": ("Insert another .daedalus / STEP / IGES / STL file as one object. link=true re-reads it when the "
                     "file changes (update_links)."),
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "link": {"type": "boolean"}},
                      "required": ["path"]}},
    {"name": "update_links", "description": "Reload linked part files that changed on disk.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "explode",
     "description": "Exploded view on/off (factor scales the spread; on recomputes the offsets).",
     "input_schema": {"type": "object", "properties": {"on": {"type": "boolean"}, "factor": {"type": "number"}},
                      "required": ["on"]}},
    # ---- drawings
    {"name": "new_drawing",
     "description": ("2D drawing of the model: sheet size (A4…A0, Letter (A), Tabloid (B), ANSI C/D/E), standard "
                     "ISO (first angle) | ANSI (third angle), units mm|in, views from front/back/top/bottom/right/"
                     "left/iso/dimetric/trimetric laid out at an automatic scale with overall dimensions, centre "
                     "marks and hole/thread callouts; title {title, number, author, company, revision, material…}; "
                     "objects (default all visible); flat = sheet-metal feature for a flat-pattern drawing; bom "
                     "adds balloons + parts list."),
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "size": {"type": "string"}, "standard": {"type": "string"},
         "units": {"type": "string"}, "views": {"type": "array", "items": {"type": "string"}},
         "objects": {"type": "array", "items": {"type": "string"}}, "title": {"type": "object"},
         "landscape": {"type": "boolean"}, "flat": {"type": "string"}, "bom": {"type": "boolean"}}}},
    {"name": "add_drawing_view",
     "description": ("Add a view to a drawing. kind auxiliary (line along an inclined edge in the parent view: looks "
                     "square onto it), breaks [{axis,a,b}] (broken view). kind projected|iso (dir), section (line [[x1,y1],[x2,y2]] across the "
                     "parent view, in that view's model mm about its centre; flip reverses), detail (center [x,y] "
                     "+ radius in the parent view, scale default 2× parent), flat (feature). parent = view id, "
                     "label or dir. x,y sheet mm (default: free space)."),
     "input_schema": {"type": "object", "properties": {
         "drawing": {"type": "string"}, "kind": {"type": "string"}, "dir": {"type": "string"},
         "parent": {"type": "string"}, "line": {"type": "array"}, "flip": {"type": "boolean"},
         "center": {"type": "array"}, "radius": {"type": "number"}, "scale": {"type": "number"},
         "x": {"type": "number"}, "y": {"type": "number"}, "label": {"type": "string"},
         "exploded": {"type": "boolean"}, "feature": {"type": "string"}, "breaks": {"type": "array"}}}},
    {"name": "set_drawing",
     "description": ("Drawing options {standard, units, decimals, text_height, dim_text_height, arrow, hidden_lines, "
                     "center_marks, auto_dims, hole_callouts, bom, balloons, hatch_spacing, font, dxf_version}, title "
                     "block fields, sheet size, landscape, scale ('1:2'), rename."),
     "input_schema": {"type": "object", "properties": {
         "drawing": {"type": "string"}, "options": {"type": "object"}, "title": {"type": "object"},
         "size": {"type": "string"}, "landscape": {"type": "boolean"}, "scale": {"type": "string"},
         "rename": {"type": "string"}}}},
    {"name": "add_drawing_item",
     "description": ("Add 2D geometry or annotation to a drawing sheet, coordinates in sheet mm (origin bottom-left): "
                     "{t:'line',a,b} {t:'poly',pts,closed} {t:'circle',c,r} {t:'arc',c,r,a0,a1} {t:'text',p,text,h} "
                     "{t:'dim',kind:'h'|'v'|'aligned'|'dia'|'rad'|'angle'|'leader',p1,p2,at,c,r,text} "
                     "{t:'balloon',p,at,text}; optional layer, style visible|thin|hidden|center. GD&T: "
                     "{t:'gdt',p (frame lower-left),symbol (position, flatness, perpendicularity, parallelism, "
                     "profile_surface, circular_runout, …),tol,dia,mod 'M'|'L',datums ['A','B|M'],leader [x,y]} · "
                     "{t:'datum',p,at,name} · {t:'finish',p,ra,method any|removal|no_removal} · "
                     "{t:'dim',kind:'ordinate',p1 origin,p2,at,axis x|y}."),
     "input_schema": {"type": "object", "properties": {"drawing": {"type": "string"}, "item": {"type": "object"}},
                      "required": ["item"]}},
    {"name": "export_drawing",
     "description": ("Save a drawing: .pdf (all sheets, vector), .dxf (layers, linetypes, real dimensions; version "
                     "from options), .dwg (needs the ODA File Converter), .svg, .png. DWF isn't supported (closed "
                     "format) — offer PDF instead."),
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "drawing": {"type": "string"},
                                                       "sheet": {"type": "string"}}, "required": ["path"]}},
    {"name": "open_2d",
     "description": "Open a DXF / DWG / SVG / HPGL file as an editable 2D drawing (layers, text, dimensions kept).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "set_export_options",
     "description": ("3D export settings saved with the file: step_schema AP214|AP242|AP203, units mm|in, "
                     "mesh_tolerance mm, angular_tolerance rad, stl_ascii, per_object, visible_only, auto_exchange "
                     "(formats also written on every save, e.g. ['step','iges']). STEP is the most versatile "
                     "exact format; IGES for legacy systems."),
     "input_schema": {"type": "object", "properties": {
         "step_schema": {"type": "string"}, "units": {"type": "string"}, "mesh_tolerance": {"type": "number"},
         "angular_tolerance": {"type": "number"}, "stl_ascii": {"type": "boolean"}, "per_object": {"type": "boolean"},
         "visible_only": {"type": "boolean"}, "auto_exchange": {"type": "array", "items": {"type": "string"}}}}},
    # ---- manufacturing
    {"name": "cam_setup",
     "description": ("CAM setup: object, margin [x,y,z] stock allowance mm, origin corner|center (G54 at stock "
                     "top), post fanuc|haas|linuxcnc|grbl, units mm|in, max_rpm, safe_z, clearance_z, coolant."),
     "input_schema": {"type": "object", "properties": {
         "object": {"type": "string"}, "margin": {"type": "array"}, "origin": {"type": "string"},
         "post": {"type": "string"}, "units": {"type": "string"}, "max_rpm": {"type": "number"},
         "safe_z": {"type": "number"}, "clearance_z": {"type": "number"}, "coolant": {"type": "boolean"}}}},
    {"name": "cam_add_tool",
     "description": "Add a cutter: kind flat|face|drill|tap|spot|ball, diameter mm, flutes, pitch (taps), rpm/feed overrides.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "diameter": {"type": "number"}, "flutes": {"type": "integer"},
         "number": {"type": "integer"}, "pitch": {"type": "number"}, "rpm": {"type": "number"},
         "feed": {"type": "number"}}, "required": ["kind", "diameter"]}},
    {"name": "cam_add_operation",
     "description": ("Add a machining operation: facing | pocket (all floors, or faces [indices]) | profile (outside "
                     "contour) | drill (all vertical holes, one op per size, adds drills and taps). tool = T number "
                     "(default: chosen). Optional stepdown, stepover (fraction of Ø), stock_to_leave, peck."),
     "input_schema": {"type": "object", "properties": {
         "type": {"type": "string"}, "tool": {"type": "integer"}, "faces": {"type": "array"},
         "stepdown": {"type": "number"}, "stepover": {"type": "number"}, "stock_to_leave": {"type": "number"},
         "peck": {"type": "number"}}, "required": ["type"]}},
    {"name": "auto_cam",
     "description": "Analyse a part and generate the whole CAM program (facing, pockets, spot, drill, tap, "
                    "profile) with tools picked from the lists given. Ask the user for controller, max rpm, stock "
                    "allowance and available end mills / drills if not known. Returns operations, cycle time and "
                    "warnings (unreachable features → second setup).",
     "input_schema": {"type": "object", "properties": {
         "object": {"type": "string"}, "orient": {"type": "string", "enum": ["auto", "+Z", "-Z", "+X", "-X",
                                                                               "+Y", "-Y"]},
         "post": {"type": "string", "enum": ["fanuc", "haas", "linuxcnc", "grbl", "siemens"]},
         "max_rpm": {"type": "number"}, "margin_xy": {"type": "number"}, "margin_z": {"type": "number"},
         "endmills": {"type": "string", "description": "e.g. '16,12,8,6,3'"},
         "drills": {"type": "string", "description": "e.g. '3.3,5,6.6,8.5,10.2'"},
         "face_mill": {"type": "number"}, "spot": {"type": "boolean"}, "profile": {"type": "boolean"},
         "cycle": {"type": "string"}, "path": {"type": "string", "description": "save G-code here (optional)"}}}},
    {"name": "cam_edit_operation",
     "description": "Change a CAM operation (0-based index). Drilling canned cycle: cycle auto|G81 drill|G82 "
                    "spot+dwell|G83 deep peck|G73 chip-break peck|G84 tap|G74 left tap|G85 ream|G86 bore|"
                    "G89 bore+dwell|G76 fine bore; peck (Q mm), dwell (s), retract initial(G98)|r(G99), r_clear, "
                    "shift (G76), breakthrough.",
     "input_schema": {"type": "object", "properties": {
         "index": {"type": "integer"},
         "cycle": {"type": "string", "enum": ["auto", "G81", "G82", "G83", "G73", "G84", "G74", "G85", "G86",
                                              "G89", "G76"]},
         "peck": {"type": "number"}, "dwell": {"type": "number"},
         "retract": {"type": "string", "enum": ["initial", "r"]}, "r_clear": {"type": "number"},
         "shift": {"type": "number"}, "breakthrough": {"type": "number"}, "tool": {"type": "integer"},
         "suppressed": {"type": "boolean"}}, "required": ["index"]}},
    {"name": "cam_remove_operation", "description": "Remove CAM operation by index (0-based).",
     "input_schema": {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]}},
    {"name": "quote_part",
     "description": "Shop quote for one object: CNC machining or sheet/laser (auto from shape), exact volume / "
                    "area / cut length, material from the model, CAM cycle time when available, quantity breaks.",
     "input_schema": {"type": "object", "properties": {
         "object": {"type": "string"}, "process": {"type": "string", "enum": ["cnc", "sheet"]},
         "qty": {"type": "integer"}, "rate": {"type": "number", "description": "$/h"},
         "margin_pct": {"type": "number"}, "setup_min": {"type": "number"}}}},
    {"name": "cam_generate",
     "description": "Compute toolpaths: per-operation tool, rpm, feed, time and notes; total cycle time. Shown in 3D.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "export_gcode",
     "description": "Write G-code (absolute path, .nc/.tap/.ngc) with the setup's post (or post override).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "post": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "check_manufacturability",
     "description": ("draft: mould/cast draft and undercuts against direction (pull); thickness: thin walls below "
                     "min_wall; overhang: 3D-print support need with direction = build direction (also returns the "
                     "best axis orientation). Colours the part for the user."),
     "input_schema": {"type": "object", "properties": {
         "check": {"type": "string", "enum": ["draft", "thickness", "overhang"]}, "object": {"type": "string"},
         "direction": {"type": "array", "items": {"type": "number"}}, "min_draft": {"type": "number"},
         "min_wall": {"type": "number"}, "max_overhang": {"type": "number"}}, "required": ["check"]}},
    # ---- simulation follow-ups
    {"name": "add_bolt",
     "description": ("Bolt connector in an assembly study between a hole face on a_object and one on b_object "
                     "(face specs, e.g. {\"axis_near\": [x,y,z]}); preload N, grade 8.8|10.9|12.9|A2-70, "
                     "diameter/grip mm (default from the hole)."),
     "input_schema": {"type": "object", "properties": {
         "study": {"type": "string"}, "a_object": {"type": "string"}, "a_face": {}, "b_object": {"type": "string"},
         "b_face": {}, "preload": {"type": "number"}, "grade": {"type": "string"}, "diameter": {"type": "number"},
         "grip": {"type": "number"}}, "required": ["study", "a_object", "a_face", "b_object", "b_face"]}},
    {"name": "design_study",
     "description": ("Parameter sweep: set feature.param to each value, rebuild, measure mass and (if 'study') run "
                     "it; returns a table and min/max per metric. The model is restored afterwards. metrics from "
                     "mass_kg, volume_mm3, max_von_mises_MPa, max_displacement_mm, min_factor_of_safety, "
                     "first_frequency_Hz, first_buckling_factor, max_temperature_C, drag_N, pressure_drop_Pa."),
     "input_schema": {"type": "object", "properties": {
         "feature": {"type": "string"}, "param": {"type": "string"}, "values": {"type": "array"},
         "study": {"type": "string"}, "metrics": {"type": "array", "items": {"type": "string"}}},
         "required": ["feature", "param", "values"]}},
    {"name": "add_flange",
     "description": ("Add an edge flange to a sheet-metal feature on the outline edge nearest 'point' (world xyz, "
                     "e.g. the centre of the thin side face). length, angle (deg), down; optional start_offset, "
                     "end_offset, hem none|closed|open, hem_length, hem_gap, length_ref outer|inner|tangent, "
                     "position material_inside|material_outside|bend_outside|bend_centerline."),
     "input_schema": {"type": "object", "properties": {
         "feature": {"type": "string"}, "point": {"type": "array", "items": {"type": "number"}},
         "length": {"type": "number"}, "angle": {"type": "number"}, "down": {"type": "boolean"},
         "start_offset": {"type": "number"}, "end_offset": {"type": "number"}, "hem": {"type": "string"},
         "hem_length": {"type": "number"}, "hem_gap": {"type": "number"}, "length_ref": {"type": "string"},
         "position": {"type": "string"}}, "required": ["feature", "point"]}},
    {"name": "set_sheet_metal_state",
     "description": "Fold or flatten sheet-metal parts: state folded | flat (feature optional, default all).",
     "input_schema": {"type": "object", "properties": {"state": {"type": "string"}, "feature": {"type": "string"}},
                      "required": ["state"]}},
    {"name": "topology_to_body",
     "description": "Turn a solved topology study into a new object (smoothed solid) for redesign/export.",
     "input_schema": {"type": "object", "properties": {"study": {"type": "string"}, "name": {"type": "string"}},
                      "required": ["study"]}},
]

NEW_TOOLS += [
    {"name": "inventory_stock_check",
     "description": "Check the design's BOM against the shop inventory: per row the matching inventory part, stock, "
                    "bin location, needed for `units` builds, shortfall and open orders.",
     "input_schema": {"type": "object", "properties": {"units": {"type": "number"}}}},
    {"name": "send_bom_to_inventory",
     "description": "Add the design's BOM to an inventory project (created if new): new parts get 0 stock, existing "
                    "ones are linked by part number / fastener spec / name; qty per assembly is allocated. "
                    "units = builds planned (project Total Qty Needed). weldments: frames become weldments.",
     "input_schema": {"type": "object", "properties": {"project": {"type": "string"}, "units": {"type": "number"},
                                                       "weldments": {"type": "boolean"}}, "required": ["project"]}},
    {"name": "inventory_search",
     "description": "Search inventory parts (name, part no., category, size); low_only = at/below reorder point. "
                    "Also returns cross-project shortages when shortages=true.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "low_only": {"type": "boolean"},
                                                       "shortages": {"type": "boolean"}}}},
    {"name": "std_library_search",
     "description": "Search the user's standard parts library (their saved parts for reuse across projects): "
                    "name, part number, category, kind, vendor, cost, size. Empty query lists everything.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"},
                                                       "kind": {"type": "string"}}}},
    {"name": "insert_standard_part",
     "description": "Insert a standard part (id, part number or name) as a new object. link=true (default) keeps "
                    "it linked to the library file; at = [x,y,z] mm. Mate it afterwards with add_mate.",
     "input_schema": {"type": "object", "properties": {"part": {"type": "string"}, "link": {"type": "boolean"},
                                                       "at": {"type": "array", "items": {"type": "number"}}},
                      "required": ["part"]}},
    {"name": "std_part_usage",
     "description": "Where a standard part (id, part number or name) is used: inventory projects (qty per unit, "
                    "units, needed — through weldments too) with active production orders, saved Daedalus files, "
                    "and inventory stock / bin / on order / shortfall. Use before changing or replacing a part.",
     "input_schema": {"type": "object", "properties": {"part": {"type": "string"}}, "required": ["part"]}},
    {"name": "add_standard_part_file",
     "description": "Add a STEP/IGES/STL/SolidWorks/.daedalus file (absolute path) to the standard parts library "
                    "(only when asked). Part number, name, description, material and vendor are read from the file "
                    "(STEP product data and custom properties, vendor file names); pass fields only to override. "
                    "Checks the library for similar parts: an identical one blocks the save (ok=false, similar=[…]) "
                    "— ask the user, then pass replace=<id> or allow_duplicate=true.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "name": {"type": "string"}, "part_number": {"type": "string"},
         "category": {"type": "string"}, "kind": {"type": "string"}, "description": {"type": "string"},
         "material": {"type": "string"}, "vendor": {"type": "string"}, "unit_cost": {"type": "number"},
         "replace": {"type": "string"}, "allow_duplicate": {"type": "boolean"}},
         "required": ["path"]}},
    {"name": "save_standard_part",
     "description": "Save an object as a standard part (only when the user asks). kind purchased|manufactured|"
                    "fastener|hardware|electrical|other. An existing part number is replaced with a new version. "
                    "Checks the library for similar parts: an identical one blocks the save (ok=false, similar=[…]) "
                    "— ask the user, then pass replace=<id> or allow_duplicate=true, or insert the existing part.",
     "input_schema": {"type": "object", "properties": {
         "object": {"type": "string"}, "name": {"type": "string"}, "part_number": {"type": "string"},
         "category": {"type": "string"}, "kind": {"type": "string"}, "description": {"type": "string"},
         "vendor": {"type": "string"}, "unit_cost": {"type": "number"}, "replace": {"type": "string"},
         "allow_duplicate": {"type": "boolean"}}, "required": ["object", "name"]}},
    {"name": "inventory_record",
     "description": "Record stock movement for an inventory part (id or exact name): mode usage|receiving, qty, note. "
                    "Only when the user asks.",
     "input_schema": {"type": "object", "properties": {"part": {"type": "string"}, "mode": {"type": "string"},
                                                       "qty": {"type": "number"}, "note": {"type": "string"}},
                      "required": ["part", "mode", "qty"]}},
]

TOOLS += NEW_TOOLS

from .tools28 import NAMES as _NAMES28, TOOLS28, run28  # noqa: E402

TOOLS += TOOLS28


def _item(x: dict) -> dict:
    out = {k: v for k, v in x.items() if k != "refs"}
    if "refs" in x:
        out["faces"] = [r.get("face") for r in x["refs"]]
    return out


def _brief_status(st: dict) -> dict:
    fp = st.get("free_points")
    if fp is not None and len(fp) > 20:
        return {**st, "free_points": f"{len(fp)} points"}
    return st


class ToolExecutor:
    """Runs tool calls against a Document. No checkpoints: the caller groups
    a whole assistant turn into one undo step."""

    def __init__(self, doc: Document):
        self.doc = doc

    def model_state(self) -> dict:
        r = self.doc.result
        return {
            "units": "mm",
            "material": self.doc.material,
            "layers": [l.to_dict() for l in self.doc.layers],
            "active_layer": self.doc.active_layer,
            "objects": [{"name": o.name, "fixed": o.fixed, "visible": o.visible,
                         "material": self.doc.material_of(o.name).name,
                         "translate": [round(v, 3) for v in o.pose[:3]],
                         "rotation_vector_rad": [round(v, 5) for v in o.pose[3:]]} for o in self.doc.objects],
            "active_object": self.doc.active_object,
            "physics": self.doc.physics.to_dict(),
            "studies": [{**{k: v for k, v in st.items() if k not in ("fixtures", "loads", "thermal", "connectors")},
                         **({"bolts": [_item({k: v for k, v in b.items() if k not in ("a_refs", "b_refs")})
                                       for b in st["connectors"]]} if st.get("connectors") else {}),
                         "fixtures": [_item(x) for x in st.get("fixtures", [])],
                         "loads": [_item(x) for x in st.get("loads", [])],
                         "thermal": [_item(x) for x in st.get("thermal", [])],
                         "solved": st["name"] in self.doc.sim_results} for st in self.doc.studies],
            "mates": [{"name": m.name, "type": m.type, "objects": [m.a["obj"], m.b["obj"]], "value": m.value,
                       "flip": m.flip, "suppressed": m.suppressed,
                       "ok": not (r.assembly and m.name in r.assembly.failed)} for m in self.doc.mates],
            "assembly_dof": r.assembly.dof if r.assembly else None,
            "rollback_index": self.doc.rollback,
            "features": [
                {**f.public_dict(),
                 **({"error": r.errors[f.name]} if f.name in r.errors else {}),
                 **({"sketch_status": _brief_status(r.sketch_status[f.name])} if f.name in r.sketch_status else {}),
                 **({"sketch_note": r.sketch_issues[f.name]} if f.name in r.sketch_issues else {})}
                for f in self.doc.features
            ],
            "mass_properties": self.doc.mass_properties(),
            "drawings": [{"name": dr.name, "linked": dr.linked, "sheets": [
                {"name": sh.name, "size": sh.size, "scale": f"{sh.scale[0]:g}:{sh.scale[1]:g}",
                 "views": [{"id": v["id"], "kind": v.get("kind"), "dir": v.get("dir"), "label": v.get("label", "")}
                           for v in sh.views], "items": len(sh.items)} for sh in dr.sheets]}
                for dr in getattr(self.doc, "drawings", [])],
            **({"cam": {"setup": {k: v for k, v in self.doc.cam.items() if k not in ("tools", "ops")},
                        "tools": self.doc.cam.get("tools", []), "operations": self.doc.cam.get("ops", [])}}
               if getattr(self.doc, "cam", None) else {}),
            **({"export_options": self.doc.export_options} if getattr(self.doc, "export_options", None) else {}),
            **({"cut_list": self.doc.cut_list()} if self.doc.result.cutlists else {}),
            **({"variables": self.doc.variables} if self.doc.variables else {}),
            **({"equations": self.doc.equations} if self.doc.equations else {}),
            **({"configurations": self.doc.config_names(), "active_configuration": self.doc.active_config}
               if self.doc.configurations else {}),
            **({"assembly_tree": self.doc.tree()} if self.doc.subassemblies else {}),
            **({"component_patterns": [{k: p[k] for k in ("name", "kind", "seeds", "instances")}
                                       for p in self.doc.comp_patterns]} if self.doc.comp_patterns else {}),
            **({"surfaces": list(self.doc.result.surfaces)} if self.doc.result.surfaces else {}),
        }

    def _status(self, focus: str | None = None) -> str:
        r = self.doc.result
        out = {"ok": not r.errors}
        if focus and focus in r.cutlists:
            out["members"] = len(r.cutlists[focus])
        if focus:
            out["feature"] = focus
            if focus in r.errors:
                out["error"] = r.errors[focus]
            if focus in r.sketch_status:
                out["sketch"] = r.sketch_status[focus]
            if focus in r.sketch_issues:
                out["sketch_note"] = r.sketch_issues[focus]
        if r.errors:
            out["all_errors"] = r.errors
        mp = self.doc.mass_properties()
        if mp:
            out["bbox_mm"] = mp["bbox_mm"]
            out["bbox_min"] = mp["bbox_min"]
            out["volume_mm3"] = mp["volume_mm3"]
        return json.dumps(out)

    def _assembly(self, name, args):
        d = self.doc
        if name == "add_object":
            o = d.add_object(args["name"], args.get("color"), checkpoint=False,
                             pose=list(args.get("translate", [0, 0, 0])) + [0, 0, 0])
            return json.dumps({"ok": True, "object": o.name, "active_object": d.active_object}), False
        if name == "set_object":
            props = {k: args[k] for k in ("fixed", "visible", "color", "new_name", "active") if k in args}
            d.set_object(args["name"], checkpoint=False, **props)
            return json.dumps({"ok": True, "objects": [o.name for o in d.objects], "active_object": d.active_object}), False
        if name == "place_object":
            o = d.obj(args["name"])
            d.place_object(args["name"], translate=args.get("translate", [0, 0, 0]), axis=args.get("axis", [0, 0, 1]),
                           angle_deg=args.get("angle", 0.0), about=o.pose[:3], checkpoint=False)
            return json.dumps({"ok": True, "pose": [round(v, 4) for v in d.obj(args["name"]).pose]}), False
        if name == "import_search":
            from ..core import importer
            dirs = []
            try:
                from PySide6.QtCore import QSettings
                st = QSettings("Daedalus", "Daedalus")
                dirs = list(st.value("import/dirs", [], type=list) or []) + [st.value("last_dir") or ""]
                recent = list(st.value("recent_files", [], type=list) or [])
            except Exception:  # noqa: BLE001
                recent = []
            if getattr(d, "path", None):
                dirs.append(str(d.path.parent))
            got = importer.search(args.get("query", ""), args.get("sources"), dirs=[x for x in dirs if x],
                                  recent=recent, exclude=str(d.path) if getattr(d, "path", None) else None)
            return json.dumps([{"source": i["source"], "id": i["id"], "name": i["name"], "detail": i.get("detail", "")}
                               for i in got[:40]]), False
        if name == "import_part":
            from ..core import importer
            item = importer.resolve(args["source"], args["id"])
            tgt = None
            ct = args.get("connect_to")
            if ct and ct.get("object") is not None and ct.get("face_index") is not None:
                tgt = {"object": ct["object"], "index": int(ct["face_index"]), "point": ct.get("point")}
            r = importer.import_item(d, item, link=args.get("link", True), target=tgt, length=args.get("length"),
                                     nut=bool(args.get("nut")), washer=bool(args.get("washer")), checkpoint=False)
            return json.dumps({"ok": True, **{k: r[k] for k in ("objects", "mates", "connected", "message")}}), False
        if name == "orient_object":
            n, act = args["name"], args["action"]
            if act == "rotate":
                d.rotate_object(n, axis=args.get("axis", [0, 0, 1]), angle_deg=args.get("angle", 90.0),
                                on_floor=bool(args.get("on_floor")), checkpoint=False)
            elif act == "drop":
                d.drop_to_floor(n, checkpoint=False)
            elif act == "face_down":
                d.face_to_floor(n, int(args["face_index"]), checkpoint=False)
            elif act == "set":
                rx, ry, rz = (list(args.get("angles", [0, 0, 0])) + [0, 0, 0])[:3]
                d.set_orientation(n, rx, ry, rz, checkpoint=False)
            lo = d.object_world_points(n).min(0)
            return json.dumps({"ok": True, "pose": [round(v, 4) for v in d.obj(n).pose],
                               "angles": [round(v, 3) for v in d.orientation_angles(d.obj(n).pose)],
                               "lowest_z": round(float(lo[2]), 4)}), False
        if name == "add_mate":
            a, b = args["a"], args["b"]
            ext = {k: ({"obj": v.get("obj") or v.get("object"), "ref": v["ref"]} if isinstance(v, dict) and "ref" in v
                       else v) for k, v in (args.get("extra") or {}).items()}
            m = d.add_mate(args["type"], {"obj": a["object"], "ref": a["ref"]}, {"obj": b["object"], "ref": b["ref"]},
                           value=args.get("value", 0.0), flip=args.get("flip"), checkpoint=False,
                           value2=args.get("value2"), extra=ext or None)
            res = d.result.assembly
            return json.dumps({"ok": True, "mate": m.name, "assembly_dof": res.dof if res else None,
                               "poses": {o.name: [round(v, 3) for v in o.pose[:3]] for o in d.objects}}), False
        if name == "set_mate":
            props = {k: args[k] for k in ("value", "flip", "suppressed") if k in args}
            d.set_mate(args["name"], checkpoint=False, **props)
            res = d.result.assembly
            return json.dumps({"ok": bool(res is None or res.ok), "assembly_dof": res.dof if res else None}), False
        if name == "delete_mate":
            d.delete_mate(args["name"], checkpoint=False)
            return json.dumps({"ok": True}), False
        if name == "drive_mate":
            frames = d.drive(args["name"], args["from"], args["to"], int(args.get("steps", 12)))
            bad = [round(f["_value"], 3) for f in frames if not f["_ok"]]
            return json.dumps({"ok": not bad, "steps": len(frames), "failed_at": bad,
                               "interference_at_end": d.interference()}), bool(bad)
        if name == "check_interference":
            return json.dumps({"overlaps": [{"a": a, "b": b, "volume_mm3": v} for a, b, v in d.interference()]}), False
        return f"Unknown tool {name}", True

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (result_text, is_error)."""
        self.doc._suspend_cp = getattr(self.doc, "_suspend_cp", 0) + 1
        try:
            return self._run(name, args)
        finally:
            self.doc._suspend_cp -= 1

    def _new(self, name, args):
        d = self.doc
        J = lambda o: json.dumps(o, default=lambda x: float(x) if hasattr(x, "__float__") else str(x))  # noqa: E731
        if name == "insert_part":
            target = None
            if args.get("target_object"):
                target = {"obj": args["target_object"], "face": args.get("hole"), "near": args.get("near")}
            r = d.insert_part(args["kind"], args["size"], float(args.get("length", 0) or 0), target=target,
                              nut=bool(args.get("nut")), washer=bool(args.get("washer")), checkpoint=False)
            return J({"ok": True, **r, "assembly_dof": d.result.assembly.dof if d.result.assembly else None})
        if name == "get_bom":
            return J({"rows": d.bom()})
        if name == "export_bom":
            d.export_bom(args["path"])
            return J({"ok": True, "path": args["path"]})
        if name == "insert_part_file":
            n = d.insert_part_file(args["path"], bool(args.get("link", True)), checkpoint=False)
            return J({"ok": True, "object": n})
        if name == "update_links":
            return J({"updated": d.update_links()})
        if name == "explode":
            if args["on"]:
                off = d.auto_explode(float(args.get("factor", 1.0)), checkpoint=False)
                return J({"ok": True, "moved": list(off)})
            d.exploded = False
            d._emit()
            return J({"ok": True})
        if name == "new_drawing":
            kw = {k: args[k] for k in ("name", "size", "standard", "units", "objects", "title", "landscape", "flat",
                                       "bom") if k in args}
            if "views" in args:
                kw["views"] = tuple(args["views"])
            if kw.get("flat"):
                kw["views"] = ()
            dr = d.new_drawing(**kw)
            sh = dr.sheet()
            return J({"ok": True, "drawing": dr.name, "sheet": sh.size, "scale": f"{sh.scale[0]:g}:{sh.scale[1]:g}",
                      "views": [{"id": v["id"], "dir": v.get("dir"), "kind": v["kind"], "x": round(v["x"], 1),
                                 "y": round(v["y"], 1), "bbox_model_mm": [round(q, 2) for q in v.get("_bbox", ())]}
                                for v in sh.views]})
        if name == "add_drawing_view":
            kw = {k: args[k] for k in ("kind", "dir", "x", "y", "scale", "parent", "line", "flip", "center", "radius",
                                       "label", "exploded", "feature", "breaks") if k in args}
            v = d.add_drawing_view(args.get("drawing"), **kw)
            return J({"ok": True, "id": v["id"], "kind": v["kind"], "x": round(v["x"], 1), "y": round(v["y"], 1),
                      "scale": v["scale"], "error": v.get("_error")})
        if name == "set_drawing":
            kw = {k: args[k] for k in ("options", "title", "size", "landscape", "scale", "rename") if k in args}
            dr = d.set_drawing(args.get("drawing"), **kw)
            return J({"ok": True, "drawing": dr.name, "options": dr.options.to_dict()})
        if name == "add_drawing_item":
            d.add_drawing_item(args["item"], args.get("drawing"))
            return J({"ok": True})
        if name == "export_drawing":
            p = d.export_drawing(args["path"], args.get("drawing"), args.get("sheet"))
            return J({"ok": True, "path": p})
        if name == "open_2d":
            dr = d.open_2d(args["path"])
            sh = dr.sheet()
            from collections import Counter
            return J({"ok": True, "drawing": dr.name, "entities": dict(Counter(i["t"] for i in sh.items)),
                      "layers": list(sh.layers)})
        if name == "set_export_options":
            return J({"ok": True, "export_options": d.set_export_options(**args)})
        if name == "cam_setup":
            return J({"ok": True, "setup": d.cam_setup(**args)})
        if name == "cam_add_tool":
            return J({"ok": True, "tool": d.cam_add_tool(**args)})
        if name == "cam_add_operation":
            a = dict(args)
            t = a.pop("type")
            return J({"ok": True, "added": d.cam_add_op(t, a.pop("tool", None), **a)})
        if name == "auto_cam":
            from ..manufacturing import autocam as A
            a = dict(args)
            obj = a.pop("object", None) or d.active_object
            if a.get("orient", "auto") == "auto":
                a["orient"] = A.recommend(d.placed_bodies()[obj])["best"]["orient"]
            if "margin_xy" in a or "margin_z" in a:
                a["margin"] = [a.pop("margin_xy", 3.0)] * 2 + [a.pop("margin_z", 1.0)]
            path = a.pop("path", None)
            r = A.build(d, obj, a)
            out = {"orient": a["orient"], "cycle_time_min": r["summary"]["cycle_time_min"],
                   "stock_mm": r["summary"]["stock_mm"], "warnings": r["warnings"],
                   "operations": [{k: o[k] for k in ("name", "tool", "rpm", "feed_mm_min", "holes", "time_min")}
                                  for o in r["summary"]["operations"]],
                   "analysis": A.describe(r["analysis"])}
            if path:
                out["saved"] = d.export_gcode(path)
            return J(out)
        if name == "cam_edit_operation":
            a = dict(args)
            i = int(a.pop("index"))
            return J({"ok": True, "operation": d.cam_edit_op(i, **a)})
        if name == "cam_remove_operation":
            d.cam_remove_op(int(args["index"]))
            return J({"ok": True})
        if name == "quote_part":
            from ..manufacturing import quote as Q
            prm = {k: args[k] for k in ("rate", "margin_pct", "setup_min") if args.get(k) is not None}
            q = Q.quote_object(d, args.get("object"), args.get("process"), prm, int(args.get("qty") or 1))
            g = q.pop("geometry", {})
            mat = d.material_of(q["object"])
            brk = Q.qty_breaks(Q.part_geometry(d.result.bodies[q["object"]]), mat, q["process"], prm,
                               Q.cam_cycle_min(d, q["object"]) if q["process"] == "cnc" else None)
            out = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in q.items()}
            out["qty_breaks"] = [{"qty": n, "per_part": round(u, 2)} for n, u in brk]
            out["sheet_like"] = g.get("sheet")
            return J(out)
        if name == "cam_generate":
            s = d.cam_plan()
            d._emit()
            return J(s)
        if name == "export_gcode":
            return J(d.export_gcode(args["path"], args.get("post")))
        if name == "check_manufacturability":
            a = dict(args)
            s = d.check_manufacturability(a.pop("check"), **a)
            d._emit()
            return J(s)
        if name == "add_bolt":
            a = dict(args)
            it = d.add_bolt(a.pop("study"), a.pop("a_object"), a.pop("a_face"), a.pop("b_object"), a.pop("b_face"),
                            checkpoint=False, **a)
            return J({"ok": True, "bolt": it["name"]})
        if name == "design_study":
            r = d.design_study(args["feature"], args["param"], args["values"], args.get("study"), args.get("metrics"))
            return J(r)
        if name == "add_flange":
            a = dict(args)
            fl = d.add_flange(a.pop("feature"), a.pop("point"), a.pop("length", 20.0), a.pop("angle", 90.0),
                              a.pop("down", False), checkpoint=False, **a)
            fs = d.result.flats
            return J({"ok": True, "flange": fl, "errors": d.result.errors,
                      "flat_size": {k: v["size"] for k, v in fs.items()}})
        if name == "set_sheet_metal_state":
            n = d.set_sheet_metal_state(args["state"], args.get("feature"), checkpoint=False)
            return J({"ok": True, "features": n, "errors": d.result.errors})
        if name == "topology_to_body":
            return J({"ok": True, "object": d.topology_to_body(args["study"], args.get("name"))})
        if name in ("std_library_search", "insert_standard_part", "save_standard_part", "add_standard_part_file",
                    "std_part_usage"):
            from ..stdparts import get_library
            lib = getattr(self, "std_library", None) or get_library()
            if name == "std_library_search":
                keep = ("id", "name", "part_number", "category", "kind", "description", "vendor", "unit_cost",
                        "material", "size_mm", "mass_kg", "version")
                hits = lib.parts(args.get("query", ""), args.get("category"), args.get("kind"))
                return J({"library": str(lib.root), "count": len(hits),
                          "parts": [{k: h.get(k) for k in keep if h.get(k) is not None} for h in hits[:80]]})
            if name == "insert_standard_part":
                n = lib.insert(d, args["part"], link=args.get("link", True), at=args.get("at"), checkpoint=False)
                return J({"ok": True, "object": n, "part_no": d.obj(n).part_no,
                          "assembly_dof": d.result.assembly.dof if d.result.assembly else None})
            if name == "std_part_usage":
                from ..stdparts.usage import files_using, std_status
                info = lib.find(args["part"])
                st = getattr(self, "inventory_store", None)
                if st is None:
                    from ..inventory.store import get_store
                    try:
                        from PySide6.QtCore import QSettings
                        st = get_store(QSettings("Daedalus", "Daedalus").value("inventory/path") or None)
                    except Exception:  # noqa: BLE001
                        st = get_store()
                st.check_external()
                inv = std_status(st.inv, info)
                return J({"part": {"id": info["id"], "name": info["name"], "part_number": info.get("part_number"),
                                   "version": info.get("version")},
                          "in_production": [{"project": p["project"], "orders": [o for o in p["production"]
                                                                                 if o["active"]],
                                             "needed": p["needed"], "via": p.get("via")} for p in inv["in_production"]],
                          "projects": [{k: p[k] for k in ("project", "qty_per_unit", "units", "needed", "archived",
                                                          "via")} for p in inv["projects"]],
                          "files": files_using(lib, info["id"]), "inventory": inv["parts"],
                          "stock": inv["stock"], "needed": inv["needed"], "short": inv["short"],
                          "on_order": inv["on_order"]})
            def brief(ms):
                return [{"id": m["id"], "name": m["name"], "part_number": m.get("part_number"),
                         "level": m["match"]["level"], "score": m["match"]["score"],
                         "reasons": m["match"]["reasons"]} for m in ms]
            if name == "add_standard_part_file":
                found = lib.file_info(args["path"])
                info = {k: args[k] for k in ("name", "part_number", "category", "kind", "description", "material",
                                             "vendor", "unit_cost") if k in args}
                pn = info.get("part_number") or found.get("part_number")
                hit = lib.by_part_number(pn or "")
                rid = args.get("replace") or (hit["id"] if hit else None)
                sim = lib.similar_to_file(args["path"], {**{k: v for k, v in found.items() if not k.startswith("_")},
                                                         **info}, threshold=0.8)
                if sim and sim[0]["match"]["level"] == "identical" and not rid and not args.get("allow_duplicate"):
                    return J({"ok": False, "reason": "an identical part is already in the library",
                              "similar": brief(sim)})
                r = lib.add_file(args["path"], info, replace=rid)
                keep = ("id", "name", "part_number", "description", "material", "vendor", "vendor_part_number",
                        "kind", "size_mm", "mass_kg", "version")
                return J({"ok": True, "replaced": bool(rid), "read_from_file": found.get("_found", []),
                          "part": {k: r.get(k) for k in keep if r.get(k) is not None},
                          "similar": brief([m for m in sim if m["id"] != r["id"]])})
            info = {k: args[k] for k in ("name", "part_number", "category", "kind", "description", "vendor",
                                         "unit_cost") if k in args}
            hit = lib.by_part_number(args.get("part_number", ""))
            rid = args.get("replace") or (hit["id"] if hit else None)
            sim = lib.similar_to_object(d, args["object"], info, threshold=0.8)
            if sim and sim[0]["match"]["level"] == "identical" and not rid and not args.get("allow_duplicate"):
                return J({"ok": False, "reason": "an identical part is already in the library",
                          "similar": brief(sim)})
            r = lib.add_object(d, args["object"], info, replace=rid)
            return J({"ok": True, "id": r["id"], "version": r["version"], "replaced": bool(rid),
                      "similar": brief([m for m in sim if m["id"] != r["id"]])})
        if name.startswith("inventory_") or name == "send_bom_to_inventory":
            return self._inventory(name, args, J)
        return None

    def _inventory(self, name, args, J):
        from ..inventory import cad as IC
        from ..inventory.model import num
        from ..inventory.store import get_store
        try:
            from PySide6.QtCore import QSettings
            path = QSettings("Daedalus", "Daedalus").value("inventory/path") or None
        except Exception:  # noqa: BLE001
            path = None
        st = getattr(self, "inventory_store", None) or get_store(path)
        st.check_external()
        inv = st.inv
        if name == "inventory_stock_check":
            rep = IC.stock_report(inv, self.doc, float(args.get("units") or 1))
            return J({"inventory_file": str(st.path), "rows": rep,
                      "short": sum(r["status"] == "short" for r in rep),
                      "not_in_inventory": sum(r["status"] == "not in inventory" for r in rep)})
        if name == "send_bom_to_inventory":
            r = IC.send_bom(inv, self.doc, args["project"], args.get("units"), bool(args.get("weldments", True)))
            st.save()
            return J({"ok": True, **r})
        if name == "inventory_search":
            q = (args.get("query") or "").lower()
            parts = inv.low_stock() if args.get("low_only") else inv.parts
            keys = ("name", "partNumber", "qbPartNumber", "category", "size", "type")
            hits = [p for p in parts if not q or any(q in str(p.get(k) or "").lower() for k in keys)]
            out = {"count": len(hits), "parts": [
                {"id": p["id"], "name": p["name"], "type": p.get("type"), "partNumber": p.get("partNumber"),
                 "stock": num(p.get("currentStock")), "unit": p.get("unit"), "reorderPoint": num(p.get("reorderPoint")),
                 "location": inv.location_label_for(p.get("locationId")), "unitCost": num(p.get("unitCost")),
                 "projects": inv.project_chips(p)} for p in hits[:60]]}
            if args.get("shortages"):
                out["shortages"] = inv.shortages()
            return J(out)
        if name == "inventory_record":
            key = args["part"].strip().lower()
            p = inv.part(args["part"]) or next((x for x in inv.parts if x["name"].lower() == key), None)
            if p is None:
                raise ValueError(f"No inventory part '{args['part']}'")
            mode = "usage" if args["mode"].lower().startswith("u") else "receiving"
            tx = inv.record(p["id"], mode, float(args["qty"]), args.get("note") or "via Claude")
            st.save()
            return J({"ok": True, "transaction": tx, "part": p["name"], "stock": num(p.get("currentStock"))})
        return None

    def _run(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            if name in _NAMES28:
                out = run28(self.doc, name, args)
                if out is not None:
                    return out, False
            if name in {t["name"] for t in NEW_TOOLS}:
                out = self._new(name, args)
                if out is not None:
                    return out, False
            if name == "get_model":
                return json.dumps(self.model_state()), False
            if name in ("add_object", "set_object", "place_object", "orient_object", "import_search", "import_part",
                        "add_mate", "set_mate", "delete_mate",
                        "drive_mate", "check_interference"):
                return self._assembly(name, args)
            if name.startswith("add_") and name[4:] in FEATURE_TYPES:
                ftype = name[4:]
                args = dict(args)
                fname = args.pop("name", None)
                layer = args.pop("layer", None)
                obj = args.pop("object", None)
                f = self.doc.add(ftype, args, name=fname, checkpoint=False, layer=layer, obj=obj)
                st = self._status(f.name)
                return st, f.name in self.doc.result.errors
            if name == "set_physics":
                self.doc.set_physics(checkpoint=False, **{k: args[k] for k in
                                                         ("gravity", "duration", "friction", "collisions", "timestep")
                                                         if k in args})
                return json.dumps({"ok": True, "physics": {k: v for k, v in self.doc.physics.to_dict().items()
                                                           if k != "drives"}}), False
            if name == "set_drive":
                kind = None if args["kind"] == "none" else args["kind"]
                self.doc.set_drive(args["mate"], kind, args.get("value", 0.0), args.get("damping", 0.0),
                                   args.get("max", 0.0), checkpoint=False)
                return json.dumps({"ok": True, "drives": [d.to_dict() for d in self.doc.physics.drives]}), False
            if name == "run_simulation":
                r = self.doc.simulate()
                self.doc.last_sim = r
                self.doc._emit()
                if args.get("keep_end"):
                    self.doc.apply_frame(r.frames[-1], checkpoint=False)
                return json.dumps({"ok": r.ok, **r.summary, "notes": r.notes}), not r.ok
            if name == "add_study":
                kw = dict(args.get("options") or {})
                if args.get("mesh_size"):
                    kw["mesh_size"] = args["mesh_size"]
                st = self.doc.add_study(args["type"], args.get("name"), args.get("object"), checkpoint=False, **kw)
                return json.dumps({"ok": True, "study": st["name"], "object": st["object"],
                                   "material": self.doc.material_of(st["object"]).name,
                                   "options": st["options"]}), False
            if name == "set_study":
                props = dict(args.get("options") or {})
                for k in ("mesh_size", "object", "new_name"):
                    if k in args:
                        props[k] = args[k]
                self.doc.set_study(args["name"], checkpoint=False, **props)
                return json.dumps({"ok": True, "study": self.doc.study(args.get("new_name") or args["name"])},
                                  default=str), False
            if name in ("add_fixture", "add_load", "add_thermal_condition"):
                kind = {"add_fixture": "fixtures", "add_load": "loads", "add_thermal_condition": "thermal"}[name]
                params = {k: v for k, v in args.items() if k not in ("study", "type", "faces")}
                it = self.doc.add_study_item(args["study"], kind, args["type"], args.get("faces"), checkpoint=False,
                                             **params)
                return json.dumps({"ok": True, "added": it.get("name"), "faces": len(it.get("refs", []))}), False
            if name == "remove_study_item":
                self.doc.remove_study_item(args["study"], args["kind"], int(args["index"]), checkpoint=False)
                return json.dumps({"ok": True}), False
            if name == "run_study":
                from ..sim.studies import brief
                res = self.doc.run_study(args["name"])
                self.doc.sim_show = args["name"]
                self.doc._emit()
                return json.dumps(brief(res)), False
            if name == "import_file":
                s = self.doc.import_file(args["path"], checkpoint=False, plane=args.get("plane", "XY"))
                return json.dumps({"ok": True, **s, "errors": self.doc.result.errors}), False
            if name == "export_file":
                p = args["path"]
                if p.lower().endswith(".dxf"):
                    n = self.doc.export_sketch_dxf(args.get("sketch") or "", p)
                    return json.dumps({"ok": True, "path": p, "curves": n}), False
                self.doc.export(p)
                return json.dumps({"ok": True, "path": p}), False
            if name == "edit_feature":
                hidden = set(args.get("params", {})) & self.doc.get(args["name"]).HIDDEN
                if hidden:
                    return f"{', '.join(sorted(hidden))} can't be edited", True
                self.doc.edit(args["name"], args.get("params", {}), checkpoint=False)
                return self._status(args["name"]), args["name"] in self.doc.result.errors
            if name == "add_layer":
                self.doc.add_layer(args["name"], args.get("color"), checkpoint=False,
                                   activate=bool(args.get("active")))
                return json.dumps({"ok": True, "layers": [l.name for l in self.doc.layers],
                                   "active_layer": self.doc.active_layer}), False
            if name == "set_layer":
                props = {k: args[k] for k in ("color", "visible", "locked", "in_model", "new_name", "active")
                         if k in args}
                self.doc.set_layer(args["name"], checkpoint=False, **props)
                return json.dumps({"ok": True, "layer": self.doc.layer(props.get("new_name") or args["name"]).to_dict(),
                                   "active_layer": self.doc.active_layer}), False
            if name == "move_to_layer":
                self.doc.move_to_layer(list(args["features"]), args["layer"], checkpoint=False)
                return self._status(), False
            if name == "set_dimension":
                f = self.doc.get(args["sketch"])
                geom = json.loads(json.dumps(f.params.get("geometry") or {}))
                cons = geom.get("constraints", [])
                i = int(args["index"])
                if not 0 <= i < len(cons) or "value" not in cons[i]:
                    raise FeatureError(f"Constraint {i} of {f.name} is not a dimension")
                cons[i]["value"] = float(args["value"])
                self.doc.edit(f.name, {"geometry": geom}, checkpoint=False)
                return self._status(f.name), f.name in self.doc.result.errors
            if name == "delete_feature":
                broken = self.doc.delete(args["name"], checkpoint=False)
                return json.dumps({"deleted": args["name"], "dependents_now_broken": broken}), False
            if name == "suppress_feature":
                self.doc.get(args["name"]).suppressed = bool(args["suppressed"])
                self.doc.rebuild()
                return self._status(args["name"]), False
            if name == "set_material":
                obj = args.get("object")
                self.doc.set_material(args["material"], obj=obj or None, checkpoint=False)
                return json.dumps({"ok": True, "material": self.doc.material_of(obj).name,
                                   "mass": self.doc.mass_properties().get("mass_kg")}), False
            if name == "list_materials":
                from ..core import materials as matlib
                ms = matlib.search(args.get("search", ""), args.get("category") or None,
                                   self.doc.custom_materials)
                return json.dumps([{"name": m.name, "category": m.category, "density": m.density, "E_GPa": m.E,
                                    "nu": m.nu, "yield_MPa": m.yield_, "uts_MPa": m.uts,
                                    "fatigue_MPa": m.fatigue, "k": m.k, "alpha": m.alpha,
                                    **({"note": m.note} if m.note else {})} for m in ms][:60]), False
            if name == "add_custom_material":
                m = self.doc.add_custom_material(args, checkpoint=False)
                return json.dumps({"ok": True, "material": m.to_dict()}), False
            return f"Unknown tool {name}", True
        except (FeatureError, KeyError, TypeError, ValueError) as e:
            return f"Error: {e}", True
