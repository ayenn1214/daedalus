"""Requests for quote: from what's short or below its reorder point (or any parts), one email per vendor asking for
price and lead time — generated, not sent. Copy it, open it as a draft in your mail program (.eml or mailto), and
attach the line list (CSV / Excel). Quotes that come back go into the vendor prices with enter_quote().

Template, numbering and the RFQ log are kept for the team in the inventory (syncMeta["rfq"]); company details come
from PO settings."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import urllib.parse
from email.message import EmailMessage
from email.utils import formatdate

from . import model as M
from . import purchase_order as P

NO_VENDOR = "No vendor set"
SUBJECT = "Request for quote {rfq} — {company}"
BODY = """Hi {contact},

Could you please quote the items below — unit price, lead time and any minimum order or price breaks?
{due_line}
{lines}

Please reference {rfq} in your reply. A spreadsheet of the same lines is attached.

Thank you,
{sender}
{company}"""
DEFAULTS = {"subject": SUBJECT, "body": BODY, "prefix": "RFQ-", "next": 1, "dueDays": 7, "sender": "",
            "attach": "csv", "log": []}


# ------------------------------------------------------------------ settings
def settings(inv) -> dict:
    meta = inv.syncMeta if isinstance(inv.syncMeta, dict) else {}
    return {**DEFAULTS, **(meta.get("rfq") or {})}


def save_settings(inv, s: dict):
    if not isinstance(inv.syncMeta, dict):
        inv.syncMeta = {}
    inv.syncMeta["rfq"] = {k: s.get(k, DEFAULTS[k]) for k in DEFAULTS}


def next_number(st: dict) -> str:
    return f"{st.get('prefix') or ''}{int(M.num(st.get('next') or 1)):04d}"


def used_number(inv, number: str):
    st = settings(inv)
    m = re.fullmatch(re.escape(st.get("prefix") or "") + r"(\d+)", number or "")
    if m and int(m.group(1)) >= int(M.num(st.get("next") or 1)):
        st["next"] = int(m.group(1)) + 1
        save_settings(inv, st)


# ------------------------------------------------------------------ what to ask for
def group(inv, items, every_supplier: bool = True) -> dict:
    """items: [(part, qty)] → {vendor: [(part, qty)]}. every_supplier: each part goes to every vendor that sells it
    (to compare quotes); otherwise only to its cheapest. Parts with no supplier go under NO_VENDOR."""
    out: dict = {}
    for p, q in items:
        sups = inv.suppliers(p)
        if not every_supplier:
            best = inv.best_supplier(p)
            sups = [best] if best else sups[:1]
        names = list(dict.fromkeys(s["name"] for s in sups if s)) or [NO_VENDOR]
        for n in names:
            out.setdefault(n, []).append((p, q))
    return out


def from_reorder(inv, project="all", vendor="all", why="all") -> list:
    """[(part, suggested qty)] for the Reorder list (why: all | short | low)."""
    short = {r["part"]["id"]: r for r in inv.shortages()}
    parts = inv.reorder_list(project, vendor)
    parts += [r["part"] for r in short.values() if r["part"] not in parts]
    if why == "short":
        parts = [p for p in parts if p["id"] in short]
    elif why == "low":
        parts = [p for p in parts if M.num(p.get("currentStock")) <= M.num(p.get("reorderPoint"))]
    need = (lambda p: inv.needed_for_project(p, project)) if project != "all" else inv.total_needed
    return [(p, inv.suggested_qty(p, need(p))) for p in parts]


def lines(inv, items, vendor) -> list[dict]:
    """Lines for one vendor: their part number, our part number, description, spec, qty, unit, last price."""
    out = []
    for p, q in items:
        s = next((s for s in inv.suppliers(p) if (s.get("name") or "").lower() == (vendor or "").lower()), None) or {}
        out.append({"partId": p["id"], "vendorPn": s.get("partNumber") or "", "ourPn": p.get("partNumber") or "",
                    "description": p.get("name") or "", "spec": inv.spec_line(p) if p.get("type") == "fastener" else "",
                    "qty": M.num(q), "unit": p.get("unit") or "ea",
                    "lastPrice": M.num(s.get("price")) if s.get("price") not in (None, "") else None,
                    "currency": s.get("currency") or "CAD", "website": s.get("website") or ""})
    return out


# ------------------------------------------------------------------ the email
def lines_text(ls: list[dict], prices: bool = False) -> str:
    """A plain-text table that survives any mail program (monospace not assumed: one item per line)."""
    out = []
    for i, ln in enumerate(ls, 1):
        q = f"{ln['qty']:g}"
        bits = [f"{i}. {q} {ln['unit']}", ln["description"]]
        refs = [f"your #{ln['vendorPn']}" if ln["vendorPn"] else "", f"our #{ln['ourPn']}" if ln["ourPn"] else ""]
        if any(refs):
            bits.append("(" + ", ".join(r for r in refs if r) + ")")
        if ln.get("spec") and ln["spec"] not in ln["description"]:
            bits.append("— " + ln["spec"])
        if prices and ln.get("lastPrice") is not None:
            bits.append(f"— last {M.fmt_money(ln['lastPrice'])} {ln['currency']}")
        out.append(" ".join(b for b in bits if b))
    return "\n".join(out)


def csv_text(ls: list[dict], rfq: str = "") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["RFQ", "Line", "Your Part #", "Our Part #", "Description", "Spec", "Qty", "Unit", "Unit Price",
                "Currency", "Lead Time (days)", "MOQ", "Notes"])
    for i, ln in enumerate(ls, 1):
        w.writerow([rfq, i, ln["vendorPn"], ln["ourPn"], ln["description"], ln.get("spec", ""), f"{ln['qty']:g}",
                    ln["unit"], "", ln["currency"], "", "", ""])
    return buf.getvalue()


def xlsx_bytes(ls: list[dict], rfq: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = rfq or "RFQ"
    rows = list(csv.reader(io.StringIO(csv_text(ls, rfq))))
    for r in rows:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, wdt in zip("ABCDEFGHIJKLM", (10, 6, 16, 14, 40, 30, 8, 7, 11, 9, 15, 7, 24)):
        ws.column_dimensions[col].width = wdt
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def compose(inv, vendor: str, items, rfq: str | None = None, due: str | None = None, st: dict | None = None,
            prices: bool = False) -> dict:
    """{'to', 'cc', 'subject', 'body', 'lines', 'rfq', 'due', 'vendor', 'csv'} for one vendor."""
    st = st or settings(inv)
    po = P.settings(inv)
    v = next((x for x in inv.vendors if (x.get("name") or "").lower() == vendor.lower()), None) or {}
    rfq = rfq or next_number(st)
    if due is None and M.num(st.get("dueDays")):
        due = (dt.date.today() + dt.timedelta(days=int(M.num(st["dueDays"])))).isoformat()
    ls = lines(inv, items, vendor)
    contact = (v.get("contactName") or "").split()[0] if (v.get("contactName") or "").strip() else \
        (vendor if vendor != NO_VENDOR else "there")
    vals = _Safe(vendor=vendor, contact=contact, company=po.get("companyName") or "", rfq=rfq,
                 date=M.fmt_date(M.today()), due=M.fmt_date(due) if due else "",
                 due_line=f"We'd appreciate your quote by {M.fmt_date(due)}.\n" if due else "",
                 lines=lines_text(ls, prices), sender=st.get("sender") or inv.userName or "",
                 count=str(len(ls)), phone=po.get("phone") or "", email=po.get("email") or "")
    tpl = st.get("body") or BODY
    if not vals["sender"]:
        tpl = tpl.replace("{sender}\n", "")                # no name set: no empty line above the company
    body = tpl.format_map(vals)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return {"vendor": vendor, "to": v.get("email") or "", "cc": "", "subject": (st.get("subject") or SUBJECT)
            .format_map(vals).strip(), "body": body, "lines": ls, "rfq": rfq, "due": due or "",
            "csv": csv_text(ls, rfq)}


def mailto(mail: dict, limit: int = 1900) -> str | None:
    """A mailto: link, or None when it would be too long for the mail program (use the .eml draft then)."""
    q = urllib.parse.urlencode({"subject": mail["subject"], "body": mail["body"]}, quote_via=urllib.parse.quote)
    url = f"mailto:{urllib.parse.quote(mail.get('to') or '', safe='@,')}?{q}"
    return url if len(url) <= limit else None


def eml_bytes(mail: dict, attach: str = "csv", sender: str = "") -> bytes:
    """An unsent draft (.eml): Outlook, Apple Mail and Thunderbird open it ready to send, with the line list
    attached."""
    msg = EmailMessage()
    msg["To"] = mail.get("to") or ""
    if mail.get("cc"):
        msg["Cc"] = mail["cc"]
    if sender:
        msg["From"] = sender
    msg["Subject"] = mail["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["X-Unsent"] = "1"                               # Outlook: open as a draft, not a received message
    msg.set_content(mail["body"])
    name = re.sub(r"[^\w.-]+", "_", f"{mail['rfq']} {mail['vendor']}").strip("_")
    if attach == "csv":
        msg.add_attachment(mail["csv"].encode("utf-8"), maintype="text", subtype="csv", filename=f"{name}.csv")
    elif attach == "xlsx":
        msg.add_attachment(xlsx_bytes(mail["lines"], mail["rfq"]), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=f"{name}.xlsx")
    return bytes(msg)


# ------------------------------------------------------------------ log and quotes back
def log(inv, mails: list[dict]) -> dict:
    """Record the RFQ (one entry per vendor) and move the number on."""
    st = settings(inv)
    for m in mails:
        st["log"] = (st.get("log") or []) + [{
            "id": M.uid(), "rfq": m["rfq"], "vendor": m["vendor"], "date": M.today(), "due": m.get("due") or "",
            "status": "Requested", "lines": [{"partId": ln["partId"], "qty": ln["qty"]} for ln in m["lines"]]}]
    st["log"] = st["log"][-500:]
    save_settings(inv, st)
    if mails:
        used_number(inv, mails[0]["rfq"])
        try:
            from .. import m365
            m365.notify("rfq", f"Quotes requested: {mails[0]['rfq']}", "", {
                m["vendor"]: f"{len(m['lines'])} line(s)" for m in mails}, key=mails[0]["rfq"])
        except Exception:  # noqa: BLE001
            pass
    return st


def open_requests(inv) -> list[dict]:
    return [e for e in settings(inv).get("log") or [] if e.get("status") == "Requested"]


def enter_quote(inv, entry_id: str, prices: dict, lead_days=None, currency: str | None = None) -> int:
    """A vendor's reply: prices {partId: unit price} go into that vendor's supplier entry on each part (added if
    missing); lead time onto the vendor. Returns how many prices were set."""
    st = settings(inv)
    e = next((x for x in st.get("log") or [] if x.get("id") == entry_id), None)
    if e is None:
        raise KeyError(entry_id)
    vendor, n = e["vendor"], 0
    for pid, price in prices.items():
        p = inv.part(pid)
        if p is None or price in (None, ""):
            continue
        sups = p.setdefault("suppliers", [])
        s = next((s for s in sups if (s.get("name") or "").lower() == vendor.lower()), None)
        if s is None:
            s = {"name": vendor, "price": None, "currency": currency or "CAD", "partNumber": "", "website": ""}
            slot = next((i for i, x in enumerate(sups) if not (x or {}).get("name")), None)  # empty A–C slot
            if slot is None:
                sups.append(s)
            else:
                sups[slot] = s
        s["price"] = M.num(price)
        if currency:
            s["currency"] = currency
        s["quoted"] = M.today()
        s["quoteRef"] = e["rfq"]
        n += 1
    if lead_days not in (None, ""):
        v = inv.ensure_vendor(vendor)
        v["leadTimeMinDays"] = v.get("leadTimeMinDays") if v.get("leadTimeMinDays") not in (None, "") else int(M.num(lead_days))
        v["leadTimeMaxDays"] = int(M.num(lead_days))
    e["status"], e["quoted"] = "Quoted", M.today()
    save_settings(inv, st)
    return n


def read_quote_csv(text: str, entry: dict, inv) -> dict:
    """Prices from the vendor's filled-in copy of the attached sheet: {partId: price} matched by line number."""
    rows = list(csv.DictReader(io.StringIO(text)))
    out = {}
    for r in rows:
        try:
            i = int(M.num(r.get("Line"))) - 1
        except (TypeError, ValueError):
            continue
        price = (r.get("Unit Price") or "").replace("$", "").replace(",", "").strip()
        if 0 <= i < len(entry["lines"]) and price:
            try:
                out[entry["lines"][i]["partId"]] = float(price)
            except ValueError:
                pass
    return out
