"""Purchase orders from the buy list — a standard PO layout for any company: your logo and company block top left,
'Purchase Order' with Date / P.O. No. top right, Vendor and Ship To boxes, MPN · Description · Qty · Rate · Amount,
Total, and your footer notes.

Your company's details (name, address, phone, email, website, logo, ship-to, footer notes, PO numbering, file
name) are set once in PO settings and kept for the team in the inventory (syncMeta["po"]). 'Fill from a PO PDF…'
can read them from a PO your accounting system already printed.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import re

from . import model as M

DEFAULTS = {"companyName": "", "address": "", "phone": "", "email": "", "website": "", "logo": "",
            "shipTo": "", "footer": "", "poPrefix": "PO-", "nextPo": 1001, "fileTag": "", "poTag": "PO",
            "currency": "CAD"}


def company_lines(st: dict) -> list[str]:
    return [x for x in [st.get("companyName", "")] + (st.get("address") or "").splitlines()
            + [st.get("phone", ""), st.get("email", ""), st.get("website", "")] if x and x.strip()]


def ship_to(st: dict) -> str:
    """The Ship To block: as set, else the company name and address."""
    if (st.get("shipTo") or "").strip():
        return st["shipTo"].strip()
    return "\n".join(x for x in [st.get("companyName", "")] + (st.get("address") or "").splitlines() if x.strip())


def next_number(st: dict) -> str:
    n = st.get("nextPo")
    return f"{st.get('poPrefix') or ''}{int(M.num(n)):04d}" if n not in (None, "") else (st.get("poPrefix") or "")


def used_number(inv, number: str):
    """After a PO is issued: move the counter past it when it follows the prefix + number pattern."""
    st = settings(inv)
    pre = st.get("poPrefix") or ""
    m = re.fullmatch(re.escape(pre) + r"(\d+)", number or "")
    if m and int(m.group(1)) >= int(M.num(st.get("nextPo"))):
        st["nextPo"] = int(m.group(1)) + 1
        save_settings(inv, st)


# ------------------------------------------------------------------ settings
def settings(inv) -> dict:
    meta = inv.syncMeta if isinstance(inv.syncMeta, dict) else {}
    return {**DEFAULTS, **(meta.get("po") or {})}


def save_settings(inv, s: dict):
    if not isinstance(inv.syncMeta, dict):
        inv.syncMeta = {}
    inv.syncMeta["po"] = {k: s.get(k, DEFAULTS[k]) for k in DEFAULTS}


# ------------------------------------------------------------------ read an existing PO (the template)
def _words(path):
    from pypdf import PdfReader
    r = PdfReader(str(path))
    page = r.pages[0]
    out = []

    def v(text, cm, tm, _fd, _fs):
        t = text.strip()
        if t:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            if abs(x) > 0.5 or abs(y) > 0.5:          # pypdf sometimes can't place a run — skip it
                out.append((x, y, t))
    page.extract_text(visitor_text=v)
    logo = ""
    try:
        imgs = list(page.images)
        if imgs:
            im = imgs[0].image.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            logo = base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        pass
    h = float(page.mediabox.height)
    return out, logo, h


def read_template(path) -> dict:
    """PO settings read from a PO PDF: company name / address / phone / email / website, ship-to, footer, logo —
    plus that PO's vendor block, date and number."""
    words, logo, h = _words(path)

    def find(t):
        return next(((x, y) for x, y, s in words if s.lower() == t.lower()), None)
    vend, ship, total, table = find("Vendor"), find("Ship To"), find("Total"), find("Description")
    po_hdr, date_hdr = find("P.O. No."), find("Date")
    if date_hdr is None and po_hdr is not None:
        date_hdr = (po_hdr[0] - 60, po_hdr[1])
    top = vend[1] if vend else h * 0.82
    mid = ship[0] - 15 if ship else 300
    company = [s for x, y, s in sorted(words, key=lambda w: -w[1])
               if y > top + 15 and x < mid and s.lower() not in ("purchase order",)]
    tab_y = table[1] if table else top - 120

    def block(x0, x1):
        return [s for x, y, s in sorted(words, key=lambda w: -w[1]) if tab_y + 8 < y < top - 4 and x0 <= x < x1]
    vendor_lines = block(0, mid)
    ship_lines = block(mid, 9999)
    foot = [s for x, y, s in sorted(words, key=lambda w: -w[1]) if total and y < total[1] - 10]

    def under(hdr):
        if not hdr:
            return ""
        c = [s for x, y, s in words if abs(x - hdr[0]) < 25 and hdr[1] - 40 < y < hdr[1] - 5]
        return c[0] if c else ""
    tag = ""
    fname = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    m = re.search(r"_([A-Za-z]{2,6}PO)_", fname)
    if m:
        tag = m.group(1)
    name = re.search(r"^\d{6}_(.+?)_([A-Za-z]+)_[A-Za-z]{2,6}PO_", fname)
    phone = next((s for s in company if re.fullmatch(r"[+()\d][\d ()+.\-]{6,}", s)), "")
    email = next((s for s in company if "@" in s and " " not in s), "")
    web = next((s for s in company if re.match(r"(https?://|www\.)", s, re.I)), "")
    rest = [s for s in company if s not in (phone, email, web)]
    return {"companyName": rest[0] if rest else "", "address": "\n".join(rest[1:]), "phone": phone, "email": email,
            "website": web, "shipTo": "\n".join(ship_lines), "footer": "\n".join(foot), "logo": logo,
            "fileTag": name.group(2) if name else "", "poTag": tag or "PO",
            "vendor": vendor_lines, "date": under(date_hdr), "poNumber": under(po_hdr)}


# ------------------------------------------------------------------ a PO
def lines_from_parts(inv, items, vendor) -> list[dict]:
    """items: [(part, qty)] → PO lines for one vendor (MPN = the vendor's part number)."""
    out = []
    for p, q in items:
        s = next((s for s in inv.suppliers(p) if (s.get("name") or "").lower() == (vendor or "").lower()), None) \
            or inv.best_supplier(p) or {}
        price = M.num(s.get("price")) if s.get("price") not in (None, "") else 0.0
        desc = [x for x in (p.get("partNumber"), p.get("name")) if x]
        out.append({"partId": p["id"], "mpn": s.get("partNumber") or "", "description": "\n".join(desc),
                    "qty": M.num(q), "rate": price, "currency": s.get("currency") or "CAD"})
    return out


def vendor_block(v: dict | None, name: str = "") -> str:
    """Vendor box text from the vendor record: name, contact, street, city / province / postal code, country."""
    if not v:
        return name
    if (v.get("poBlock") or "").strip():                 # the vendor box as last saved from a PO
        return v["poBlock"].strip()
    city = " ".join(x for x in (", ".join(x for x in (v.get("city"), v.get("province")) if x), v.get("postalCode"))
                    if x)
    lines = [v.get("legalName") or v.get("name"), v.get("contactName"), v.get("address"), v.get("address2"), city,
             v.get("country")]
    if not any((v.get(k) or "").strip() for k in ("address", "city", "country")):
        lines += [v.get("phone"), v.get("email")]
    return "\n".join(x.strip() for x in lines if x and str(x).strip())


def total(lines) -> float:
    return round(sum(M.num(ln["qty"]) * M.num(ln["rate"]) for ln in lines), 2)


def file_name(st: dict, vendor: str, po: str, job: str = "", date: str | None = None) -> str:
    """'261117_3_Amigos_Rangate_RNPO_LL114513AA_Vacuum_System.pdf' — date, vendor, company, PO tag, PO no., job."""
    d = dt.date.fromisoformat(date) if date else dt.date.today()

    def clean(s):
        return re.sub(r"[^A-Za-z0-9.\-]+", "_", (s or "").strip()).strip("_")
    bits = [d.strftime("%y%m%d"), clean(vendor), clean(st.get("fileTag") or st.get("companyName", "").split(" ")[0]),
            clean(st.get("poTag")) + ("_" + clean(po) if po else ""), clean(job)]
    return "_".join(b for b in bits if b) + ".pdf"


def _money(v):
    return f"{v:,.2f}"


def write_pdf(path, st: dict, po: dict):
    """po: {"number", "date" (ISO), "vendor" (block text), "shipTo" (block text), "lines", "currency"}."""
    from reportlab.lib.colors import Color, black
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    W, H = letter
    grey = Color(0.9, 0.9, 0.9)
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle(f"Purchase Order {po.get('number') or ''}".strip())
    c.setAuthor(st.get("companyName") or "Daedalus")
    lines = po.get("lines") or []
    cur = po.get("currency") or st.get("currency") or "CAD"
    d = po.get("date") or M.today()
    try:
        dtxt = dt.date.fromisoformat(d).strftime("%m/%d/%y")
    except ValueError:
        dtxt = d
    L, R = 36, W - 36
    cols = [L, 103.5, 326, 408, 491, R]                     # MPN | Description | Qty | Rate | Amount
    t_top, t_bot = H - 247.5, H - 697.5
    row_h, band = 10.5, 7

    # paginate: each line takes its description lines + a shaded gap
    def height(ln):
        return max(1, len(_wrap(ln["description"], 46))) * row_h + band + 4
    pages, cur_pg, used = [], [], 0.0
    avail = (t_top - 21) - t_bot - 6
    for ln in lines:
        hgt = height(ln)
        if cur_pg and used + hgt > avail:
            pages.append(cur_pg)
            cur_pg, used = [], 0.0
        cur_pg.append(ln)
        used += hgt
    pages.append(cur_pg)

    for n, chunk in enumerate(pages):
        last = n == len(pages) - 1
        # logo + company
        y0 = H - 36
        if st.get("logo"):
            try:
                img = ImageReader(io.BytesIO(base64.b64decode(st["logo"])))
                iw, ih = img.getSize()
                w = 82.0
                c.drawImage(img, L + 2, y0 - w * ih / iw, w, w * ih / iw, mask="auto")
                y0 -= w * ih / iw + 4
            except Exception:  # noqa: BLE001
                pass
        c.setFont("Times-Roman", 9)
        for i, s in enumerate(company_lines(st)[:7]):
            c.drawString(L + 3, y0 - 9 - i * 11.5, s)
        c.setFont("Helvetica-Bold", 19)
        c.drawRightString(R - 4, H - 58, "Purchase Order")
        # Date / P.O. No.
        bx0, bmid, bx1, by1, by0 = 441, 501, R, H - 67, H - 113
        c.setLineWidth(0.8)
        c.rect(bx0, by0, bx1 - bx0, by1 - by0)
        c.line(bx0, by1 - 22, bx1, by1 - 22)
        c.line(bmid, by0, bmid, by1)
        c.setFont("Helvetica", 8.5)
        c.drawCentredString((bx0 + bmid) / 2, by1 - 15, "Date")
        c.drawCentredString((bmid + bx1) / 2, by1 - 15, "P.O. No.")
        c.setFont("Times-Roman", 8.5)
        c.drawCentredString((bx0 + bmid) / 2, by1 - 39, dtxt)
        c.drawCentredString((bmid + bx1) / 2, by1 - 39, po.get("number") or "")
        # Vendor / Ship To
        for (x0, x1, title, text, font) in ((L, 283, "Vendor", po.get("vendor") or "", "Helvetica"),
                                            (328, R, "Ship To", po.get("shipTo") or ship_to(st), "Times-Roman")):
            top, bot = H - 130, H - 239
            c.rect(x0, bot, x1 - x0, top - bot)
            c.line(x0, top - 22, x1, top - 22)
            c.setFont("Helvetica", 8.5)
            c.drawString(x0 + 12, top - 15, title)
            c.setFont(font, 8.5)
            for i, s in enumerate(text.splitlines()[:9]):
                c.drawString(x0 + 3, top - 31 - i * 10.5, s)
        # table
        c.setFillColor(grey)
        c.rect(L, t_top - 21, R - L, 21, stroke=0, fill=1)
        c.setFillColor(black)
        c.rect(L, t_bot, R - L, t_top - t_bot)
        c.line(L, t_top - 21, R, t_top - 21)
        for x in cols[1:-1]:
            c.line(x, t_bot, x, t_top)
        c.setFont("Helvetica", 8.5)
        for x0, x1, head in zip(cols, cols[1:], ("MPN", "Description", "Qty", "Rate", "Amount")):
            c.drawCentredString((x0 + x1) / 2, t_top - 14, head)
        y = t_top - 21 - 11
        c.setFont("Times-Roman", 8.5)
        for k, ln in enumerate(chunk):
            desc = _wrap(ln["description"], 46)
            c.drawString(cols[0] + 3, y, (ln.get("mpn") or "")[:14])
            for i, s in enumerate(desc):
                c.drawString(cols[1] + 3, y - i * row_h, s)
            q = M.num(ln["qty"])
            c.drawRightString(cols[3] - 4, y, f"{q:g}")
            c.drawRightString(cols[4] - 4, y, _money(M.num(ln["rate"])))
            c.drawRightString(cols[5] - 4, y, _money(q * M.num(ln["rate"])))
            y -= len(desc) * row_h
            if k < len(chunk) - 1:                            # shaded gap between lines
                c.setFillColor(grey)
                c.rect(L + 0.5, y + 3, R - L - 1, band, stroke=0, fill=1)
                c.setFillColor(black)
            y -= band + 4
        # total
        c.rect(376, H - 731, R - 376, 34)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(386, H - 719, "Total")
        c.setFont("Times-Roman", 8.5)
        c.drawRightString(R - 6, H - 717, f"{cur} {_money(total(lines))}" if last else "continued…")
        # footer
        foot = [x for x in (st.get("footer") or "").splitlines() if x.strip()]
        if foot:
            c.roundRect(L, H - 774, R - L, 41, 6)
            c.setFont("Times-Roman", 8.5)
            for i, s in enumerate(foot[:3]):
                c.drawString(L + 6, H - 744 - i * 11, s)
        if len(pages) > 1:
            c.setFont("Helvetica", 7.5)
            c.drawRightString(R, 14, f"Page {n + 1} of {len(pages)}")
        c.showPage()
    c.save()
    return path


def _wrap(text, width) -> list[str]:
    out = []
    for para in (text or "").splitlines() or [""]:
        words, cur = para.split(), ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        out.append(cur)
    return out or [""]


def log_orders(inv, po: dict, vendor: str, project_id=None) -> list:
    """Open part orders (status Ordered) for the PO's lines that are parts, with the PO number."""
    rng = inv.lead_time_range(vendor)
    exp = ""
    if rng:
        days = int(M.num(rng[1] if isinstance(rng, (list, tuple)) else rng))
        exp = (dt.date.fromisoformat(po.get("date") or M.today()) + dt.timedelta(days=days)).isoformat()
    out = []
    for ln in po.get("lines") or []:
        if ln.get("partId") and inv.part(ln["partId"]) and M.num(ln["qty"]) > 0:
            out.append(inv.place_order(ln["partId"], M.num(ln["qty"]), po.get("date") or M.today(), exp, vendor,
                                       project_id, po.get("number") or ""))
    return out
