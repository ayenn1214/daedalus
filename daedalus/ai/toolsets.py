"""Which of Icarus's tools a model gets, and 'prompted tools' for models that can't call tools natively.

Tool sets: core (the 32 modelling tools a small local model handles), model (every modelling / simulation / drawing /
CAM tool, no inventory: 127 — under the 128-tool limit of OpenAI-style APIs), dims (inventory) and all.

Prompted tools: the tool list goes into the instructions and the model answers with a JSON object
{"tool": name, "arguments": {...}}, which Icarus runs; the result comes back as the next user message. Lets any chat
model (gemma, deepseek-r1, phi, llava…) drive Daedalus."""
from __future__ import annotations

import json

DIMS_PREFIX = ("dims_", "inventory_", "send_bom_to_inventory")
API_TOOL_LIMIT = 128
SETS = [("core", "Core (modelling basics)"), ("model", "Modelling + simulation + drawings + CAM"),
        ("dims", "Inventory (DIMS)"), ("all", "All tools")]


def _all():
    from .tools import TOOLS
    return TOOLS


def select(tool_set: str = "core", tools: list[dict] | None = None, limit: int | None = None) -> list[dict]:
    """The tool definitions (Anthropic-style: name / description / input_schema) for a set."""
    if tools is not None:                              # a host with its own tools (CAD Link)
        return tools[:limit] if limit else tools
    from .ollama import CORE_TOOLS
    allt = _all()
    if tool_set == "core":
        out = [t for t in allt if t["name"] in CORE_TOOLS]
    elif tool_set == "model":
        out = [t for t in allt if not t["name"].startswith(DIMS_PREFIX)]
    elif tool_set == "dims":
        out = [t for t in allt if t["name"].startswith(DIMS_PREFIX) or t["name"] in ("get_model", "get_bom")]
    else:
        out = list(allt)
    if limit and len(out) > limit:                      # keep modelling first, inventory last
        out = sorted(out, key=lambda t: t["name"].startswith(DIMS_PREFIX))[:limit]
    return out


def openai_schemas(defs: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in defs]


PROMPTED = """
How to use tools (this model calls them through text)
- To use a tool, reply with ONLY a JSON object and nothing else:
  {"tool": "<tool name>", "arguments": { ... }}
  Several in a row: a JSON list of such objects. Arguments must match the tool's schema.
- The result comes back in the next message, starting "TOOL RESULT". Read it; on an error fix the arguments and retry.
- When the work is done, answer in plain sentences with no JSON.

Tools:
"""


def catalogue(defs: list[dict]) -> str:
    lines = []
    for t in defs:
        desc = " ".join((t.get("description") or "").split())
        if len(desc) > 400:
            desc = desc[:400].rsplit(" ", 1)[0] + "…"
        schema = t.get("input_schema") or {}
        lines.append(f"- {t['name']}: {desc}\n  arguments: {json.dumps(schema.get('properties') or {}, separators=(',', ':'))}"
                     + (f"  required: {schema['required']}" if schema.get("required") else ""))
    return PROMPTED + "\n".join(lines)


def parse_calls(text: str, known: set[str]) -> list[tuple[str, dict]]:
    """Tool calls written as JSON in a model's text: {"tool"|"name": …, "arguments"|"args"|"parameters"|"input": …}
    (also OpenAI's {"function": {...}} shape), alone, in a list or inside ```json fences."""
    dec = json.JSONDecoder()
    calls, i = [], 0
    while True:
        i = min([j for j in (text.find("{", i), text.find("[", i)) if j >= 0], default=-1)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        i = end
        for o in obj if isinstance(obj, list) else [obj]:
            if not isinstance(o, dict):
                continue
            o = o.get("function", o) if isinstance(o.get("function"), dict) else o
            name = o.get("tool") or o.get("name") or o.get("tool_name")
            if name not in known:
                continue
            args = next((o[k] for k in ("arguments", "args", "parameters", "input") if k in o), {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            calls.append((name, args if isinstance(args, dict) else {}))
    return calls
