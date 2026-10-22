"""Copilot tools for SolidWorks Link mode (BOM, properties, library, fasteners, inventory, commands)."""
from __future__ import annotations

import json

from .backend import SWError

SW_SYSTEM = """You are the Daedalus copilot working alongside a CAD program — SolidWorks or Fusion (sw_status
says which; the sw_* tools work on whichever is active). The CAD program does the 3D modelling; you handle
everything around it through tools:
- sw_status / sw_bom / sw_get_properties: what is open, the parts list, custom properties (PartNo,
  Description, Material, HolocronId, DaedalusStdId…).
- sw_set_properties: fill or fix custom properties (part numbers, descriptions, vendor, cost). Only change
  what the user asked for.
- Fasteners and standard parts: fastener_sizes lists kinds/sizes; sw_insert_fastener puts an ISO/ASME screw,
  nut, washer or bearing into the active assembly; std_library_search + sw_insert_standard_part insert the
  user's own standard parts. With mate=true they mate to the face/hole the user selected in SolidWorks —
  tell the user to select the hole first if nothing is selected.
- sw_save_part_to_library saves the active SolidWorks part as a Daedalus standard part (only when asked).
- Inventory (the shop's Holocron system): inventory_stock_check (BOM vs stock for N builds, shortfalls,
  locations), send_bom_to_inventory (BOM → project, writes HolocronId back to SolidWorks), inventory_search.
- sw_command: views (Isometric, Front, Top, Right…), fit, rebuild, save, export (step, stl, pdf, dxf),
  macro (path to .swp), command (SolidWorks command id).
- send_to_other_cad moves the active document to the other connected CAD program (SolidWorks ⇄ Fusion) as
  STEP with its properties — into its open assembly if there is one.
- In Fusion, part numbers/descriptions are component properties, other properties are attributes, mates are
  joints, macros don't apply and command ids are strings.
- open_in_daedalus sends the active model to Daedalus for simulation (FEA, frequency, buckling, thermal),
  CAM, physics or 2D drawings.
Don't try to model geometry — say that's done in SolidWorks. Units: mm, kg. Reply in 1–3 short sentences
with the key numbers."""


def _t(name, desc, props=None, req=()):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": list(req)}}


SW_TOOLS = [
    _t("sw_status", "Connection, SolidWorks version and the active document (type, configuration, properties, "
                    "material, mass)."),
    _t("sw_bom", "Parts list of the active assembly: part number, description, qty, material, mass, cost, "
                 "file, HolocronId / DaedalusStdId."),
    _t("sw_get_properties", "Custom properties of the active document or of an open file.",
       {"path": {"type": "string"}}),
    _t("sw_set_properties", "Set custom properties on the active document or an open file.",
       {"properties": {"type": "object"}, "path": {"type": "string"}}, ["properties"]),
    _t("fastener_sizes", "Fastener kinds, sizes and standard lengths available to insert.",
       {"kind": {"type": "string"}}),
    _t("sw_insert_fastener", "Insert a library fastener/bearing into the active SolidWorks assembly.",
       {"kind": {"type": "string", "enum": ["socket head cap screw", "hex bolt", "flat head screw", "hex nut",
                                            "washer", "ball bearing"]},
        "size": {"type": "string"}, "length": {"type": "number", "description": "mm (screws/bolts)"},
        "mate": {"type": "boolean"}}, ["kind", "size"]),
    _t("std_library_search", "Search the Daedalus standard-parts library.", {"query": {"type": "string"}}),
    _t("sw_insert_standard_part", "Insert a standard part (id from std_library_search) into the SolidWorks "
                                  "assembly.", {"id": {"type": "string"}, "mate": {"type": "boolean"}}, ["id"]),
    _t("sw_save_part_to_library", "Save the active SolidWorks part as a standard part.",
       {"name": {"type": "string"}, "part_number": {"type": "string"}, "category": {"type": "string"},
        "kind": {"type": "string", "enum": ["purchased", "manufactured"]}, "description": {"type": "string"},
        "vendor": {"type": "string"}}),
    _t("inventory_stock_check", "Active assembly's BOM against inventory stock for N builds.",
       {"units": {"type": "number"}}),
    _t("send_bom_to_inventory", "Send the active assembly's BOM to an inventory project (creates/links parts, "
                                "allocates quantities, writes HolocronId back to SolidWorks).",
       {"project": {"type": "string"}, "units": {"type": "number"}}, ["project"]),
    _t("inventory_search", "Search inventory parts (name, part number, category, size).",
       {"query": {"type": "string"}, "low_stock_only": {"type": "boolean"}}),
    _t("sw_command", "Run a SolidWorks action on the active document.",
       {"action": {"type": "string", "enum": ["view", "fit", "rebuild", "save", "export", "macro", "command"]},
        "arg": {"type": "string", "description": "view name / export format / macro path / command id"}},
       ["action"]),
    _t("send_to_other_cad", "Send the active document to the other connected CAD program (SolidWorks ⇄ "
                            "Fusion), inserted into its open assembly when there is one.",
       {"as_component": {"type": "boolean"}}),
    _t("open_in_daedalus", "Send the active SolidWorks model to Daedalus (geometry, mates, properties) for "
                           "simulation, CAM, physics or drawings."),
]


class SWToolExecutor:
    def __init__(self, link, on_open_daedalus=None, peers=None):
        self.link = link
        self.on_open_daedalus = on_open_daedalus
        self.peers = peers or (lambda: [])

    def model_state(self):
        try:
            st = self.link.status()
        except Exception as e:  # noqa: BLE001
            return {"connected": False, "error": str(e)}
        doc = st.get("document") or {}
        return {"connected": st.get("connected"), "document": {k: doc.get(k) for k in ("title", "kind",
                                                                                      "configuration")}}

    def run(self, name, args):
        try:
            return json.dumps(self._run(name, args or {}), default=str), False
        except (SWError, ValueError, KeyError, FileNotFoundError) as e:
            return f"{e.__class__.__name__}: {e}", True
        except Exception as e:  # noqa: BLE001
            return f"{e.__class__.__name__}: {e}", True

    def _run(self, name, a):
        L = self.link
        if name == "sw_status":
            return L.status()
        if name == "sw_bom":
            return [{k: r.get(k) for k in ("item", "part", "description", "qty", "material", "unit_mass_kg",
                                           "unit_cost", "file", "inventory_id")} for r in L.bom()]
        if name == "sw_get_properties":
            return L.properties(a.get("path"))
        if name == "sw_set_properties":
            L.set_properties(a["properties"], a.get("path"))
            return {"ok": True}
        if name == "fastener_sizes":
            from ..core import standards as S
            kinds = [a["kind"]] if a.get("kind") else list(S.KINDS)
            out = {}
            for k in kinds:
                if k == "ball bearing":
                    out[k] = {"sizes": list(S.BEARINGS)}
                else:
                    out[k] = {"metric": list(S.METRIC), "inch": list(S.INCH),
                              "lengths_mm": S.METRIC_LENGTHS, "lengths_in": S.INCH_LENGTHS}
            return out
        if name == "sw_insert_fastener":
            from ..core import standards as S
            L_mm = float(a.get("length") or 0)
            if a["kind"] in ("socket head cap screw", "hex bolt", "flat head screw") and L_mm <= 0:
                raise ValueError("length (mm) is required for screws and bolts")
            if a["size"] in S.INCH and L_mm and L_mm < 10:
                L_mm *= S.IN                                      # given in inches
            return L.insert_fastener(a["kind"], a["size"], L_mm, mate=a.get("mate", True))
        if name == "std_library_search":
            return [{k: p.get(k) for k in ("id", "name", "part_number", "category", "kind", "vendor")}
                    for p in L.library.parts(a.get("query", ""))[:30]]
        if name == "sw_insert_standard_part":
            return L.insert_std(a["id"], mate=a.get("mate", True))
        if name == "sw_save_part_to_library":
            info = {k: a[k] for k in ("name", "part_number", "category", "kind", "description", "vendor") if a.get(k)}
            s = L.save_active_to_std(info)
            return {k: s.get(k) for k in ("id", "name", "part_number", "version")}
        if name == "inventory_stock_check":
            return L.stock_report(float(a.get("units") or 1))
        if name == "send_bom_to_inventory":
            rep = L.send_bom(a["project"], a.get("units"))
            return {k: rep.get(k) for k in ("project", "added", "linked", "units", "written_back", "write_failed")}
        if name == "inventory_search":
            inv = L.store.inv
            q = (a.get("query") or "").lower()
            out = []
            for p in inv.parts:
                text = " ".join(str(p.get(k) or "") for k in ("name", "partNumber", "category", "size")).lower()
                if q and not all(t in text for t in q.split()):
                    continue
                if a.get("low_stock_only") and inv.level(p) == "green":
                    continue
                out.append({"id": p["id"], "name": p.get("name"), "partNumber": p.get("partNumber"),
                            "stock": p.get("currentStock"), "reorderPoint": p.get("reorderPoint"),
                            "location": inv.location_label_for(p.get("locationId"))})
            return out[:40]
        if name == "sw_command":
            res = L.command(a["action"], a.get("arg"))
            return {"ok": True, **({"file": res} if isinstance(res, str) else {})}
        if name == "send_to_other_cad":
            others = self.peers()
            if not others:
                raise ValueError("Only one CAD program is connected — connect the other in the CAD selector")
            return L.transfer_to(others[0], as_component=a.get("as_component", True))
        if name == "open_in_daedalus":
            path = L.to_daedalus()
            if self.on_open_daedalus:
                self.on_open_daedalus(path)
            return {"ok": True, "opened": path}
        raise ValueError(f"Unknown tool {name}")
