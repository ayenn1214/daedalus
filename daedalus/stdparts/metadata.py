"""Read part information carried inside CAD files, to pre-fill standard parts.

    STEP   PRODUCT id / name / description of the root product, PRODUCT_DEFINITION description,
           user-defined attributes (SolidWorks / Inventor / Creo custom properties exported as
           DESCRIPTIVE_REPRESENTATION_ITEM: Part Number, Description, Material, Vendor, …),
           MATERIAL_DESIGNATION, header author / organisation / originating system
    IGES   global section: product id, author, organisation, native system
    STL    ASCII "solid <name>"
    SLDPRT custom properties + material, written by the SolidWorks bridge (needs SolidWorks once)
    name   vendor file-name conventions, e.g. McMaster "91251A542_Socket Head Screw.STEP"

Everything returned is a suggestion the user confirms in the Save dialog.
"""
from __future__ import annotations

import json
import re
from difflib import get_close_matches
from pathlib import Path

# property name (normalised) → standard-part field
_ALIASES = {
    "part_number": ["partnumber", "partno", "pn", "number", "itemnumber", "drawingnumber", "documentnumber",
                    "articlenumber", "partnum", "partid", "sku"],
    "description": ["description", "desc", "title", "partdescription", "partname", "longdescription"],
    "material": ["material", "swmaterial", "materialname", "materialspec", "materialdesignation"],
    "vendor": ["vendor", "manufacturer", "supplier", "mfg", "mfr", "make", "brand", "manufacturername"],
    "vendor_part_number": ["vendorpartnumber", "manufacturerpartnumber", "mpn", "mfgpartnumber", "mfgpn",
                           "mfrpartnumber", "supplierpartnumber", "catalognumber", "catalogue", "ordernumber",
                           "mcmasterpartnumber", "vendorpn", "orderingcode"],
    "unit_cost": ["cost", "price", "unitcost", "unitprice"],
    "category": ["category", "type", "parttype", "family", "group", "class"],
    "revision": ["revision", "rev", "version"],
}
_LOOKUP = {a: f for f, al in _ALIASES.items() for a in al}
_GENERIC = {"", "part", "part1", "body", "solid", "unnamed", "noname", "default", "open cascade step translator",
            "none", "unknown", "model", "assembly", "assem1", "product", "shape", "compound", "comp", "solid1",
            "body1", "part body", "partbody", "nauo", "component", "filename.iges", "filename.step"}
VENDORS = {"mcmaster": "McMaster-Carr", "misumi": "MISUMI", "fastenal": "Fastenal", "grainger": "Grainger",
           "skf": "SKF", "igus": "igus", "boschrexroth": "Bosch Rexroth", "rexroth": "Bosch Rexroth",
           "8020": "80/20", "festo": "Festo", "smc": "SMC", "thk": "THK", "hiwin": "HIWIN", "nsk": "NSK",
           "timken": "Timken", "parker": "Parker", "bimba": "Bimba", "automationdirect": "AutomationDirect",
           "digikey": "Digi-Key", "mouser": "Mouser", "bossard": "Bossard", "würth": "Würth", "wurth": "Würth",
           "norelem": "norelem", "kipp": "KIPP", "ganter": "GANTER", "item": "item", "boschrexrothag": "Bosch Rexroth",
           "stepperonline": "StepperOnline", "oriental": "Oriental Motor", "lenze": "Lenze", "sew": "SEW-Eurodrive"}


def _norm(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (k or "").lower().replace("sw-", "sw"))


def _clean(v) -> str:
    v = str(v or "").strip()
    low = v.lower()
    if low in _GENERIC or v.startswith("$PRP") or "processor" in low or "translator" in low:
        return ""
    if re.fullmatch(r"[=>\s]*\[?[\d:]+\]?", v) or re.fullmatch(r"(solid|body|shell|part|comp)[\s_-]*\d*", low):
        return ""                                   # OCC label refs (=>[0:1:1:2]) and auto names (Body3)
    return v


# ------------------------------------------------------------------ STEP (ISO 10303-21)

_STMT = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\(((?:'(?:[^']|'')*'|[^';])*)\)\s*;", re.S)
_WANTED = {"PRODUCT", "PRODUCT_DEFINITION_FORMATION", "PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE",
           "PRODUCT_DEFINITION", "NEXT_ASSEMBLY_USAGE_OCCURRENCE", "PROPERTY_DEFINITION",
           "PROPERTY_DEFINITION_REPRESENTATION", "REPRESENTATION", "DESCRIPTIVE_REPRESENTATION_ITEM",
           "MATERIAL_DESIGNATION", "PRODUCT_RELATED_PRODUCT_CATEGORY", "PRODUCT_CATEGORY"}


def _decode(s: str) -> str:
    s = s.replace("''", "'")

    def x2(m):
        h = m.group(1)
        return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h) - 3, 4))
    s = re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", x2, s)
    s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("latin-1"), s)
    s = re.sub(r"\\S\\(.)", lambda m: chr(ord(m.group(1)) + 128), s)
    return s


def _args(text: str):
    """Parse a Part-21 parameter list into Python values: str, ('#', n), list, None, numbers/enums as str."""
    i, n = 0, len(text)

    def value():
        nonlocal i
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            return None
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            v = _decode(text[i + 1:j])
            i = j + 1
            return v
        if c == "(":
            i += 1
            out = []
            while i < n:
                while i < n and text[i] in " \r\n\t,":
                    i += 1
                if i < n and text[i] == ")":
                    i += 1
                    return out
                out.append(value())
            return out
        if c == "#":
            m = re.match(r"#(\d+)", text[i:])
            i += m.end()
            return ("#", int(m.group(1)))
        if c in "$*":
            i += 1
            return None
        m = re.match(r"[A-Z0-9_]+\s*\(", text[i:])           # typed parameter, e.g. LENGTH_MEASURE(1.)
        if m:
            i += m.end() - 1
            return value()
        m = re.match(r"[^,()]+", text[i:])
        i += m.end() if m else 1
        return m.group(0).strip() if m else None

    out = []
    while i < n:
        while i < n and text[i] in " \r\n\t,":
            i += 1
        if i < n:
            out.append(value())
    return out


def _ref(v):
    return v[1] if isinstance(v, tuple) and v and v[0] == "#" else None


def read_step(path) -> dict:
    text = Path(path).read_bytes().decode("latin-1")      # Part 21 is 8-bit; \X2\ escapes decoded below
    out: dict = {"_found": []}
    head = text[:4000]
    m = re.search(r"FILE_NAME\s*\(((?:'(?:[^']|'')*'|[^;])*)\)\s*;", head, re.S)
    if m:
        a = _args(m.group(1))
        author = a[2] if len(a) > 2 and isinstance(a[2], list) else []
        org = a[3] if len(a) > 3 and isinstance(a[3], list) else []
        out["_author"] = ", ".join(x for x in author if x)
        out["_organization"] = ", ".join(x for x in org if x)
        out["_system"] = (a[5] if len(a) > 5 and isinstance(a[5], str) else "") or \
                         (a[4] if len(a) > 4 and isinstance(a[4], str) else "")
    ents: dict[int, tuple[str, list]] = {}
    data_at = text.find("DATA;")
    for m in _STMT.finditer(text, data_at if data_at >= 0 else 0):
        t = m.group(2)
        if t in _WANTED:
            try:
                ents[int(m.group(1))] = (t, _args(m.group(3)))
            except Exception:  # noqa: BLE001
                continue
    prods = {k: a for k, (t, a) in ents.items() if t == "PRODUCT"}
    pdf_prod = {k: _ref(a[2]) for k, (t, a) in ents.items() if t.startswith("PRODUCT_DEFINITION_FORMATION")
                and len(a) > 2}
    pd = {k: a for k, (t, a) in ents.items() if t == "PRODUCT_DEFINITION"}
    children = {_ref(a[4]) for t, a in ents.values() if t == "NEXT_ASSEMBLY_USAGE_OCCURRENCE" and len(a) > 4}
    roots = [k for k in pd if k not in children] or list(pd)
    root_pd = roots[0] if roots else None
    root_prod = pdf_prod.get(_ref(pd[root_pd][2])) if root_pd and len(pd[root_pd]) > 2 else None
    if root_prod is None and prods:
        root_prod = next(iter(prods))
    out["_products"] = len(prods)
    if root_prod in prods:
        a = prods[root_prod]
        pid, name, desc = (_clean(a[i]) if len(a) > i and isinstance(a[i], str) else "" for i in range(3))
        if pid:
            out["part_number"] = pid
        if name and name != pid:
            out["name"] = name
        elif pid:
            out["name"] = pid
        if desc and desc not in (pid, name):
            out["description"] = desc
        if pid or name or desc:
            out["_found"].append("STEP product data")
    if root_pd in pd and len(pd[root_pd]) > 1:
        d = _clean(pd[root_pd][1])
        if d and d not in (out.get("part_number"), out.get("name"), "design", "part definition", "None") \
                and "description" not in out:
            out["description"] = d
    # user-defined attributes: PROPERTY_DEFINITION → PDR → REPRESENTATION → DESCRIPTIVE_REPRESENTATION_ITEMs
    prop_def = {k: a for k, (t, a) in ents.items() if t == "PROPERTY_DEFINITION"}
    rep_items = {k: a for k, (t, a) in ents.items() if t == "REPRESENTATION"}
    dri = {k: a for k, (t, a) in ents.items() if t == "DESCRIPTIVE_REPRESENTATION_ITEM"}
    attrs: dict[str, str] = {}
    multi = len(pd) > 1
    for t, a in ents.values():
        if t != "PROPERTY_DEFINITION_REPRESENTATION" or len(a) < 2:
            continue
        p = prop_def.get(_ref(a[0]))
        r = rep_items.get(_ref(a[1]))
        if p is None or r is None:
            continue
        if multi and root_pd and len(p) > 2 and _ref(p[2]) not in (root_pd, None):
            continue
        pname = p[0] if isinstance(p[0], str) else ""
        for it in (r[1] if len(r) > 1 and isinstance(r[1], list) else []):
            d = dri.get(_ref(it))
            if d and len(d) > 1 and isinstance(d[0], str) and isinstance(d[1], str):
                key = d[0] if _norm(pname) in ("userdefinedattributes", "userdefinedattribute", "", "general") \
                    or _norm(d[0]) in _LOOKUP else pname
                attrs[key] = d[1]
    if not attrs and not multi:                     # loose: items not wired to a definition
        for d in dri.values():
            if len(d) > 1 and isinstance(d[0], str) and isinstance(d[1], str) and _norm(d[0]) in _LOOKUP:
                attrs[d[0]] = d[1]
    for t, a in ents.values():
        if t == "MATERIAL_DESIGNATION" and a and isinstance(a[0], str) and _clean(a[0]):
            attrs.setdefault("Material", a[0])
    if attrs:
        out["_found"].append(f"{len(attrs)} file attribute{'s' if len(attrs) > 1 else ''}")
        out["_attributes"] = attrs
        _apply_props(out, attrs, override=True)
    return out


# ------------------------------------------------------------------ IGES

def read_iges(path) -> dict:
    lines = Path(path).read_text(errors="replace").splitlines()
    g = "".join(ln[:72] for ln in lines if len(ln) >= 73 and ln[72] == "G")
    if not g:
        return {"_found": []}
    pd, rd = ",", ";"
    if g.startswith("1H"):
        pd = g[2]
        if g[4:6] == "1H":
            rd = g[6]
    fields, i, n = [], 0, len(g)
    while i < n and len(fields) < 26:
        m = re.match(r"\s*(\d+)H", g[i:])
        if m:
            k = int(m.group(1))
            s = i + m.end()
            fields.append(g[s:s + k])
            i = s + k
        else:
            j = i
            while j < n and g[j] not in (pd, rd):
                j += 1
            fields.append(g[i:j].strip())
            i = j
        if i < n and g[i] == rd:
            break
        i += 1
    get = (lambda k: _clean(fields[k - 1]) if len(fields) >= k else "")
    out = {"_found": ["IGES global section"]}
    pid = get(3) or get(12)
    if pid and pid not in (get(5), get(6)) and not pid.lower().endswith((".igs", ".iges")):
        out["part_number"] = out["name"] = pid
    out["_author"], out["_organization"], out["_system"] = get(21), get(22), get(5)
    return out


# ------------------------------------------------------------------ others

def read_stl(path) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(256)
    if head[:5].lower() == b"solid" and b"facet" in open(path, "rb").read(2048):
        name = _clean(head[5:].split(b"\n")[0].decode("latin-1", "replace"))
        if name:
            return {"name": name, "_found": ["STL solid name"]}
    return {"_found": []}


def read_solidworks(path) -> dict:
    """Custom properties + material from the bridge's <name>.daedalus-props.json (SolidWorks converts once)."""
    from ..io import swbridge
    p = Path(path)
    props = swbridge.read_props(p)
    if props is None and swbridge.available()[0]:
        try:
            swbridge.convert_cached(p)
            props = swbridge.read_props(p)
        except Exception:  # noqa: BLE001
            props = None
    if props is None:
        return {"_found": [], "_note": "SolidWorks properties are read when SolidWorks converts the file "
                                       "(Windows with SolidWorks, or the batch command on a SolidWorks PC)."}
    out = {"_found": ["SolidWorks custom properties"], "_attributes": props.get("properties", {})}
    _apply_props(out, props.get("properties", {}), override=True)
    if props.get("material"):
        out["material"] = props["material"]
    if props.get("configuration"):
        out["_configuration"] = props["configuration"]
    return out


def from_filename(path) -> dict:
    """Vendor naming conventions in the file name."""
    stem = Path(path).stem
    out: dict = {}
    m = re.match(r"^(\d{3,6}[A-Z]\d{1,6})[_ -]+(.+)$", stem)               # McMaster: 91251A542_Socket Head ...
    if m:
        out.update(vendor="McMaster-Carr", vendor_part_number=m.group(1), part_number=m.group(1),
                   name=m.group(2).replace("_", " ").strip(), _found=["McMaster file name"])
        return out
    if re.match(r"^\d{3,6}[A-Z]\d{1,6}$", stem):
        return {"vendor": "McMaster-Carr", "vendor_part_number": stem, "part_number": stem,
                "_found": ["McMaster part number"]}
    m = re.match(r"^([A-Z]{2,8}\d*[A-Z]*)-([\w.]+(?:-[\w.]+)*)$", stem)    # MISUMI-style configured codes
    if m and any(c.isdigit() for c in stem):
        out.update(part_number=stem, _found=["part code in file name"])
    return out


# ------------------------------------------------------------------ public

def _apply_props(out: dict, props: dict, override=False):
    for k, v in props.items():
        f = _LOOKUP.get(_norm(k))
        v = _clean(v)
        if not f or not v:
            continue
        if f == "unit_cost":
            try:
                v = float(re.sub(r"[^\d.]", "", v))
            except ValueError:
                continue
        if f == "revision":
            out["revision"] = v
            continue
        if override or f not in out:
            out[f] = v


def guess_vendor(*texts) -> str:
    blob = _norm(" ".join(t for t in texts if t))
    for key, name in VENDORS.items():
        if _norm(key) and _norm(key) in blob:
            return name
    return ""


def match_material(text: str, names: list[str]) -> str | None:
    """Material library name for a free-text material ('AISI 1020', '6061-T6 Aluminum', 'Delrin')."""
    t = (text or "").lower().strip()
    if not t:
        return None
    low = {n.lower(): n for n in names}
    if t in low:
        return low[t]
    syn = [(r"6061", "6061"), (r"6063", "6063"), (r"7075", "7075"), (r"5052", "5052"), (r"2024", "2024"),
           (r"\b304\b", "304"), (r"\b316\b", "316"), (r"\b17-?4", "17-4"), (r"\b1018\b", "1018"),
           (r"\b1020\b", "1020"), (r"\b1045\b", "1045"), (r"\b4140\b", "4140"), (r"\b4340\b", "4340"),
           (r"\ba36\b", "a36"), (r"delrin|acetal|\bpom\b", "acetal"), (r"nylon|\bpa ?6", "nylon"),
           (r"\babs\b", "abs"), (r"\bpla\b", "pla"), (r"\bpetg\b", "petg"), (r"polycarb|\bpc\b", "polycarbonate"),
           (r"uhmw", "uhmw"), (r"\bhdpe\b", "hdpe"), (r"brass", "brass"), (r"bronze", "bronze"),
           (r"copper", "copper"), (r"titanium|ti-?6al", "titanium"), (r"cast iron", "cast iron"),
           (r"stainless", "stainless"), (r"alumin", "aluminum"), (r"alloy steel", "alloy steel"),
           (r"steel", "steel"), (r"rubber|neoprene|epdm", "rubber"), (r"\bmdf\b", "mdf"), (r"plywood", "plywood"),
           (r"oak|maple|birch|pine|wood", "wood")]
    for pat, key in syn:
        if re.search(pat, t):
            hits = [n for n in names if key in n.lower()]
            if hits:
                # prefer one that also shares the family word (e.g. 'aluminum' for 6061)
                fam = [h for h in hits if any(w in h.lower() for w in re.findall(r"[a-z]{4,}", t))]
                return sorted(fam or hits, key=len)[0]
    m = get_close_matches(t, list(low), n=1, cutoff=0.6)
    return low[m[0]] if m else None


def read(path) -> dict:
    """Suggested standard-part fields for a file (keys with a leading _ are notes, not fields)."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in (".step", ".stp", ".p21"):
            meta = read_step(p)
        elif ext in (".iges", ".igs"):
            meta = read_iges(p)
        elif ext == ".stl":
            meta = read_stl(p)
        elif ext in (".sldprt", ".sldasm"):
            meta = read_solidworks(p)
        else:
            meta = {"_found": []}
    except Exception as e:  # noqa: BLE001  (never block adding a part)
        meta = {"_found": [], "_error": str(e)}
    fn = from_filename(p)
    for k, v in fn.items():
        if k == "_found":
            meta.setdefault("_found", []).extend(v)
        elif not meta.get(k) or (k == "name" and meta.get("name") == meta.get("part_number")):
            meta[k] = v
    if not meta.get("vendor"):
        v = guess_vendor(meta.get("_organization", ""), meta.get("_author", ""), p.stem,
                         meta.get("description", ""))
        if v:
            meta["vendor"] = v
            meta.setdefault("_found", []).append("vendor from file")
    own_vendor = any(_LOOKUP.get(_norm(k)) == "vendor" for k in (meta.get("_attributes") or {}))
    if meta.get("vendor") and meta.get("part_number") and not meta.get("vendor_part_number") and not own_vendor:
        meta["vendor_part_number"] = meta["part_number"]       # a vendor's own file carries the vendor's number
    if meta.get("revision"):
        meta["notes"] = (meta.get("notes", "") + f" Rev {meta['revision']}").strip()
    if "kind" not in meta and meta.get("vendor"):
        meta["kind"] = "purchased"
    meta.setdefault("name", p.stem)
    return meta


def suggestions(meta: dict) -> dict:
    """Only the standard-part fields."""
    from .library import FIELDS
    return {k: v for k, v in meta.items() if k in FIELDS and v not in (None, "")}


def write_props_note(meta: dict) -> str:
    f = meta.get("_found") or []
    return ("Read from file: " + ", ".join(dict.fromkeys(f))) if f else ""


__all__ = ["read", "suggestions", "match_material", "guess_vendor", "read_step", "read_iges", "json"]  # noqa
