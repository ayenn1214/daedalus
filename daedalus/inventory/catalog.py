"""Supplier catalogs: look a vendor part number up and bring back its description, specifications, price tiers,
status (discontinued → suggested replacement) and CAD — into DIMS (new part, or refreshed prices on the parts that
use that vendor) and into the standard parts library.

McMaster-Carr: the Product Information API (api.mcmaster.com) for approved customers — McMaster issues a client
certificate (.pfx + its password) and you sign in with your mcmaster.com account; contact eCommerce@mcmaster.com.
Products are 'subscribed' on first lookup (the API needs it; subscriptions are limited per day).
Other vendors without an API (Misumi, Fastenal, Grainger, Digi-Key, Mouser): a search link to open.

Qt-free; credentials come from settings (kept on this PC, never exported)."""
from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MCMASTER = "https://api.mcmaster.com/v1"
SEARCH = {
    "McMaster-Carr": "https://www.mcmaster.com/{q}/",
    "Misumi": "https://us.misumi-ec.com/vona2/result/?Keyword={q}",
    "Fastenal": "https://www.fastenal.com/product?query={q}",
    "Grainger": "https://www.grainger.ca/en/search?text={q}",
    "Digi-Key": "https://www.digikey.ca/en/products/result?keywords={q}",
    "Mouser": "https://www.mouser.ca/c/?q={q}",
    "Uline": "https://www.uline.ca/BL_35/Search?keywords={q}",
}


class CatalogError(RuntimeError):
    pass


def search_url(vendor: str, q: str) -> str:
    t = SEARCH.get(vendor) or "https://www.google.com/search?q={q}"
    return t.format(q=urllib.parse.quote(q.strip()))


# ------------------------------------------------------------------ settings
_OVERRIDE: dict = {}


def setting(key, default=""):
    if key in _OVERRIDE:
        return _OVERRIDE[key]
    try:
        from PySide6.QtCore import QSettings
        v = QSettings("Daedalus", "Daedalus").value(key, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def configured() -> bool:
    return all(setting(k, "") for k in ("mcmaster/cert", "mcmaster/user", "mcmaster/password"))


# ------------------------------------------------------------------ McMaster-Carr
def _ssl_context(cert: str, cert_password: str) -> ssl.SSLContext:
    """A TLS context presenting McMaster's client certificate (.pfx / .p12, or a PEM with its key)."""
    ctx = ssl.create_default_context()
    if not cert:
        return ctx
    p = Path(cert)
    if not p.exists():
        raise CatalogError(f"Client certificate not found: {cert}")
    if p.suffix.lower() in (".pfx", ".p12"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        try:
            key, crt, extra = pkcs12.load_key_and_certificates(p.read_bytes(),
                                                                (cert_password or "").encode() or None)
        except ValueError as e:
            raise CatalogError(f"Couldn't open the client certificate — is its password right? ({e})") from None
        pw = (cert_password or "daedalus").encode()
        fd, pem = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(pw)))
                fh.write(crt.public_bytes(serialization.Encoding.PEM))
                for c in extra or []:
                    fh.write(c.public_bytes(serialization.Encoding.PEM))
            ctx.load_cert_chain(pem, password=pw)
        finally:
            os.remove(pem)
    else:
        ctx.load_cert_chain(str(p), password=cert_password or None)
    return ctx


class McMaster:
    """The McMaster-Carr Product Information API."""
    name = "McMaster-Carr"

    def __init__(self, cert: str | None = None, cert_password: str | None = None, user: str | None = None,
                 password: str | None = None, base: str = MCMASTER, ctx: ssl.SSLContext | None = None):
        self.cert = cert if cert is not None else setting("mcmaster/cert", "")
        self.cert_password = cert_password if cert_password is not None else setting("mcmaster/cert_password", "")
        self.user = user if user is not None else setting("mcmaster/user", "")
        self.password = password if password is not None else setting("mcmaster/password", "")
        self.base = base.rstrip("/")
        self._ctx = ctx
        self.token, self.token_exp = "", 0.0

    def ctx(self):
        if self._ctx is None:
            self._ctx = _ssl_context(self.cert, self.cert_password)
        return self._ctx

    def _call(self, method, path, body=None, raw=False, auth=True, retry=True):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.login()}"
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, context=self.ctx(), timeout=60) as r:
                data = r.read()
                return data if raw else (json.loads(data.decode() or "null") if data else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            try:
                j = json.loads(txt) or {}
            except ValueError:
                j = {}
            code = j.get("ErrorMessage") or j.get("ErrorCode") or ""
            if e.code == 403 and "EXPIRED" in str(code) and retry and auth:
                self.token = ""
                return self._call(method, path, body, raw, auth, retry=False)
            err = CatalogError(f"McMaster-Carr {e.code}: {j.get('ErrorDescription') or code or txt[:200]}")
            err.status, err.code = e.code, str(code)
            raise err from None
        except urllib.error.URLError as e:
            raise CatalogError(f"Can't reach McMaster-Carr's API ({e.reason}) — is the client certificate set?") \
                from None

    def login(self) -> str:
        if self.token and time.time() < self.token_exp:
            return self.token
        if not (self.user and self.password):
            raise CatalogError("Set your McMaster-Carr sign-in in Settings ▸ Suppliers")
        r = self._call("POST", "/login", {"UserName": self.user, "Password": self.password}, auth=False)
        self.token = r["AuthToken"]
        self.token_exp = time.time() + 23 * 3600
        return self.token

    def logout(self):
        if self.token:
            try:
                self._call("POST", "/logout", retry=False)
            except CatalogError:
                pass
        self.token = ""

    def product(self, pn: str) -> dict:
        pn = pn.strip()
        try:
            raw = self._call("GET", f"/products/{urllib.parse.quote(pn)}")
        except CatalogError as e:
            if "NOT_SUBSCRIBED" not in getattr(e, "code", ""):
                raise
            raw = self._call("PUT", "/products", {"URL": f"https://mcmaster.com/{pn}"})
        try:
            price = self._call("GET", f"/products/{urllib.parse.quote(pn)}/price") or []
        except CatalogError:
            price = []
        return normalize_mcmaster(raw, price)

    def changes(self, since: str) -> list[str]:
        """Subscribed part numbers changed since 'MM/dd/yyyy' (prices aren't reported here)."""
        r = self._call("GET", f"/changes?start={urllib.parse.quote(since)}") or {}
        return list(r.get("PartNumbers") or [])

    def file(self, link: str) -> bytes:
        """A CAD / image / datasheet link from product()['links'] (e.g. '/v1/cad/…')."""
        path = link[len("/v1"):] if link.startswith("/v1") else link
        return self._call("GET", path, raw=True)


def normalize_mcmaster(raw: dict, price: list) -> dict:
    links = {lk.get("Key"): lk.get("Value") for lk in raw.get("Links") or []}
    specs = {s.get("Attribute"): ", ".join(str(v) for v in (s.get("Values") or [])) for s in raw.get("Specifications")
             or []}
    tiers = [{"min": float(t.get("MinimumQuantity") or 1), "price": float(t.get("Amount") or 0),
              "unit": t.get("UnitOfMeasure") or "Each"} for t in price or []]
    pn = raw.get("PartNumber") or ""
    step = next((v for k, v in links.items() if k and "STEP" in k.upper() and "3-D" in k.upper()), None) or \
        next((v for k, v in links.items() if k and "STEP" in k.upper()), None)
    return {"vendor": "McMaster-Carr", "partNumber": pn, "family": raw.get("FamilyDescription") or "",
            "description": raw.get("DetailDescription") or raw.get("FamilyDescription") or pn, "specs": specs,
            "tiers": sorted(tiers, key=lambda t: t["min"]), "currency": "USD",
            "active": str(raw.get("ProductStatus") or "Active").lower() != "inactive",
            "suggested": links.get("SuggestedProduct") or "",
            "differences": raw.get("SuggestedProductDifferences") or [], "step": step,
            "dwg": next((v for k, v in links.items() if k and "DWG" in k.upper()), None),
            "image": links.get("Image"), "datasheet": next((v for k, v in links.items() if k and "atasheet" in k),
                                                           None),
            "url": f"https://www.mcmaster.com/{pn}/"}


def unit_price(info: dict, qty: float = 1) -> float | None:
    """The price per unit at qty from the tiers."""
    best = None
    for t in info.get("tiers") or []:
        if qty >= t["min"]:
            best = t["price"]
    if best is None and info.get("tiers"):
        best = info["tiers"][0]["price"]
    return best


FASTENER_WORDS = re.compile(r"\b(screw|bolt|nut|washer|rivet|pin|stud|insert|anchor)s?\b", re.I)


def part_type(info: dict) -> str:
    return "fastener" if FASTENER_WORDS.search(f"{info.get('family')} {info.get('description')}") else "purchased"


# ------------------------------------------------------------------ into DIMS / the library
def supplier_entry(info: dict, qty: float = 1) -> dict:
    return {"name": info["vendor"], "price": unit_price(info, qty), "currency": info.get("currency") or "USD",
            "partNumber": info["partNumber"], "website": info.get("url") or "", "priceTiers": info.get("tiers") or [],
            "checked": time.strftime("%Y-%m-%d")}


def apply_to_part(p: dict, info: dict, qty: float | None = None) -> dict:
    """Set / refresh this vendor's supplier entry on a DIMS part."""
    from . import model as M
    q = qty if qty is not None else max(1.0, M.num(p.get("reorderQty")) or 1.0)
    sups = p.setdefault("suppliers", [])
    s = next((s for s in sups if (s.get("name") or "").lower() == info["vendor"].lower() and
              (not s.get("partNumber") or s["partNumber"] == info["partNumber"])), None)
    new = supplier_entry(info, q)
    if s is None:
        slot = next((i for i, x in enumerate(sups) if not (x or {}).get("name")), None)   # the empty A–C slots
        if slot is None:
            sups.append(new)
        else:
            sups[slot] = new
        s = new
    else:
        s.update(new)
    if not info.get("active"):
        s["discontinued"] = True
        if info.get("suggested"):
            s["suggested"] = info["suggested"]
    p["updatedAt"] = M.now_ms()
    return s


def new_part(inv, info: dict, **kw) -> dict:
    from . import model as M
    p = M.new_part(name=info["description"][:120], type=kw.pop("type", part_type(info)), currentStock=0,
                   reorderPoint=0, reorderQty=max(1, int((info.get("tiers") or [{"min": 1}])[0]["min"])),
                   notes="; ".join(f"{k}: {v}" for k, v in list((info.get("specs") or {}).items())[:12]), **kw)
    apply_to_part(p, info)
    inv.ensure_vendor(info["vendor"])
    inv.parts.append(p)
    return p


def refresh_prices(inv, provider, progress=None) -> dict:
    """Every part with this vendor's part number: fresh price tiers, discontinued flags."""
    targets = [(p, s) for p in inv.parts for s in (p.get("suppliers") or [])
               if (s.get("name") or "").lower() == provider.name.lower() and s.get("partNumber")]
    out = {"updated": 0, "discontinued": [], "failed": {}}
    for i, (p, s) in enumerate(targets):
        if progress:
            progress(i, len(targets), p.get("name", ""))
        try:
            info = provider.product(s["partNumber"])
        except CatalogError as e:
            out["failed"][s["partNumber"]] = str(e)
            continue
        apply_to_part(p, info)
        out["updated"] += 1
        if not info["active"]:
            out["discontinued"].append({"part": p["name"], "partNumber": s["partNumber"],
                                        "suggested": info.get("suggested")})
    return out


def to_library(provider, info: dict, lib=None) -> dict:
    """Download the 3-D STEP into the standard parts library with the catalog data."""
    if not info.get("step"):
        raise CatalogError(f"No 3-D STEP for {info['partNumber']}")
    data = provider.file(info["step"])
    d = Path(tempfile.mkdtemp())
    path = d / f"{info['partNumber']}.step"
    path.write_bytes(data)
    if lib is None:
        from ..stdparts import get_library
        lib = get_library()
    try:
        return lib.add_file(path, {"name": f"{info['partNumber']} {info['description']}"[:120],
                                   "part_number": info["partNumber"], "vendor": info["vendor"],
                                   "vendor_part_number": info["partNumber"], "description": info["description"],
                                   "kind": "fastener" if part_type(info) == "fastener" else "purchased",
                                   "unit_cost": unit_price(info), "currency": info.get("currency"),
                                   "url": info.get("url"), "category": info.get("family") or "Purchased"})
    finally:
        try:
            path.unlink()
            d.rmdir()
        except OSError:
            pass
