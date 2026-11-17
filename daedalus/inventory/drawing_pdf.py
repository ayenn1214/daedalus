"""Read a drawing PDF: title block, BOM table, revision table and notes.

Drawings published from CAD (SOLIDWORKS, Inventor, …) carry real text, so nothing is guessed: PDFium (QtPdf)
gives every character with its position, characters become words, words on one baseline become cells, and fields
are read next to their labels (PART NUMBER, DESCRIPTION, MATERIAL, FINISH, APPROX. WEIGHT, REV., DRAWN, DATE,
SCALE, SHEET …). Scanned drawings have no text: when Tesseract is installed the page is OCR'd into the same word
boxes (less exact — the result says so).

parse(path) → {"partNumber", "description", "material", "finish", "weightLb", "weightText", "rev", "drawnBy",
               "drawnDate", "scale", "sheet", "tolerances": {…}, "bom": [{item, partNumber, description, qty}],
               "revisions": [{rev, description, date, by}], "notes": [...], "meta": {...}, "pages": n,
               "source": "text" | "ocr", "file"}
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class DrawingError(Exception):
    pass


@dataclass
class Word:
    text: str
    x: float
    y: float          # top (points, y down)
    w: float
    h: float

    @property
    def r(self):
        return self.x + self.w

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


# ------------------------------------------------------------------ words from the page
def _qdoc(path):
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        raise DrawingError("Reading drawings needs the Daedalus window running")
    from PySide6.QtPdf import QPdfDocument
    d = QPdfDocument()
    err = d.load(str(path))
    if d.status() != QPdfDocument.Status.Ready or d.pageCount() < 1:
        raise DrawingError(f"{Path(path).name} isn't a readable PDF ({err})")
    return d


def words_from_text(d, page=0) -> list[Word]:
    """Words as PDFium orders them (its spaces and line breaks), each with the box around its characters."""
    txt = d.getAllText(page).text()
    out, cur = [], None
    for i, ch in enumerate(txt):
        if ch.isspace():
            cur = None
            continue
        r = d.getSelectionAtIndex(page, i, 1).boundingRectangle()
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        if w <= 0 and h <= 0:
            continue
        h = max(h, 1.0)
        if cur is not None and (abs((y + h / 2) - cur.cy) > max(cur.h, h) * 0.8 or x < cur.x - 1
                                or x - cur.r > max(1.8, 0.62 * max(cur.h, h))):
            cur = None                                    # the text jumped to another place on the sheet
        if cur is None:
            cur = Word(ch, x, y, w, h)
            out.append(cur)
            continue
        top, bot = min(cur.y, y), max(cur.y + cur.h, y + h)
        cur.text += ch
        cur.w = max(cur.r, x + w) - cur.x
        cur.y, cur.h = top, bot - top
    return out


def tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(p).exists():
            return p
    return None


def words_from_ocr(d, page=0, dpi=300) -> list[Word]:
    exe = tesseract()
    if not exe:
        raise DrawingError("This drawing is scanned (no text in the PDF). Install Tesseract OCR "
                           "(https://github.com/UB-Mannheim/tesseract/wiki on Windows) to read scans.")
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QImage, QPainter
    pt = d.pagePointSize(page)
    s = dpi / 72
    img = d.render(page, QSize(int(pt.width() * s), int(pt.height() * s)))
    bg = QImage(img.size(), QImage.Format_RGB32)
    bg.fill(QColor("white"))
    p = QPainter(bg)
    p.drawImage(0, 0, img)
    p.end()
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "page.png"
        bg.save(str(png))
        r = subprocess.run([exe, str(png), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True,
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = []
    for line in r.stdout.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or not f[11].strip():
            continue
        try:
            conf = float(f[10])
        except ValueError:
            conf = -1
        if conf < 30:
            continue
        x, y, w, h = (int(f[i]) / s for i in (6, 7, 8, 9))
        out.append(Word(f[11].strip(), x, y, w, h))
    return out


# ------------------------------------------------------------------ cells and rows
def cells(words: list[Word], gap: float = 4.5) -> list[Word]:
    """Words on one baseline with small gaps → one cell ('PART NUMBER', 'WLDMT, SHIP BRKT')."""
    ws = sorted(words, key=lambda w: (round(w.cy / 2), w.x))
    out: list[Word] = []
    for w in sorted(ws, key=lambda w: (w.cy, w.x)):
        hit = None
        for c in reversed(out[-40:]):
            if abs(c.cy - w.cy) <= max(c.h, w.h) * 0.45 and 0 <= w.x - c.r <= max(gap, 0.9 * max(c.h, w.h)):
                hit = c
                break
        if hit:
            hit.text += " " + w.text
            top, bot = min(hit.y, w.y), max(hit.y + hit.h, w.y + w.h)
            hit.w = max(hit.r, w.r) - hit.x
            hit.y, hit.h = top, bot - top
        else:
            out.append(Word(w.text, w.x, w.y, w.w, w.h))
    return out


def _row(cs, ref: Word, tol=None):
    tol = tol if tol is not None else max(ref.h, 4) * 0.7
    return sorted([c for c in cs if c is not ref and abs(c.cy - ref.cy) <= tol], key=lambda c: c.x)


LABELS = {
    "partNumber": ["PART NUMBER", "PART NO", "PART #", "DWG NO", "DWG. NO", "DRAWING NUMBER"],
    "description": ["DESCRIPTION", "TITLE"],
    "material": ["MATERIAL"],
    "finish": ["FINISH"],
    "weight": ["APPROX. WEIGHT", "APPROX WEIGHT", "WEIGHT"],
    "rev": ["REV.", "REV", "REVISION"],
    "drawnBy": ["DRAWN", "DRAWN BY"],
    "drawnDate": ["DATE"],
    "scale": ["SCALE"],
    "sheet": ["SHEET"],
}
NOT_A_VALUE = re.compile(r"^(PART NUMBER|DESCRIPTION|MATERIAL|FINISH|DRAWN|DATE|APV|CHK|CHECKED|ENG|QA|SCALE|SHEET|"
                         r"REV\.?|WEIGHT|APPROX\.? WEIGHT|THIRD ANGLE|PROJECTION|QTY\.?|ITEM NO\.?)$", re.I)


def _is_label(text):
    up = text.upper()
    return bool(NOT_A_VALUE.match(text)) or any(up == m or up.startswith(m + " ") for v in LABELS.values() for m in v)


def _label_value(cs: list[Word], names: list[str], prefer_left=True):
    """Value next to a label: the rest of the label's own cell, else the cells to its right on the row (joined
    while they sit close together)."""
    hits = []
    for c in cs:
        up = c.text.upper()
        for n in names:
            head = up == n or up.startswith(n + " ") or up.startswith(n + ":")
            tail = up.endswith(" " + n)
            if not (head or tail):
                continue
            rest = c.text[len(n):].strip(" :") if head else ""
            rest = re.sub(r"^\(UNLESS NOTED\)\s*", "", rest, flags=re.I)
            if rest and not NOT_A_VALUE.match(rest):
                hits.append((c, rest, 1))
                break
            right = [o for o in _row(cs, c) if o.x >= c.r - 1]
            val, last = [], c
            for o in right:
                gap = o.x - last.r
                if (not val and gap > 140) or (val and gap > max(9, 2 * o.h)) or _is_label(o.text):
                    break
                val.append(o.text)
                last = o
            if val:
                hits.append((c, " ".join(val), 0 if up == n else 1))
            break
    if not hits:
        return None, None
    hits.sort(key=lambda h: (h[2], h[0].x if prefer_left else -h[0].x, h[0].y))
    return hits[0][1], hits[0][0]


def _clusters(ws: list[Word]) -> list[list[Word]]:
    """Words → text lines (same baseline), top to bottom."""
    out: list[list[Word]] = []
    for w in sorted(ws, key=lambda w: w.cy):
        if out and abs(w.cy - sum(o.cy for o in out[-1]) / len(out[-1])) <= max(w.h, 3) * 0.6:
            out[-1].append(w)
        else:
            out.append([w])
    for line in out:
        line.sort(key=lambda w: w.x)
    return out


def _header_row(words: list[Word], first: Word, gap: float = 120, ends=()) -> list[Word]:
    """The header label and the header words right of it on its line, while they stay close together."""
    line = sorted([w for w in words if abs(w.cy - first.cy) <= first.h * 0.6 and w.x >= first.x - 1],
                  key=lambda w: w.x)
    row, last = [], None
    for w in line:
        if last is not None and w.x - last.r > gap:
            break
        row.append(w)
        last = w
        if any(w.text.upper().rstrip(".").startswith(e) for e in ends):
            break                                     # the table's last column header
    return row


def _table_rows(words: list[Word], head: list[Word], starts) -> list[list[Word]]:
    """Rows of a table under or over its header (SOLIDWORKS BOMs grow up from the title block, revision tables
    down from the top of the sheet): lines inside the header's width, contiguous, whose first word fits `starts`."""
    x0, x1 = min(w.x for w in head) - 6, max(w.r for w in head) + 10
    hy = sum(w.cy for w in head) / len(head)
    lines = _clusters([w for w in words if x0 <= w.x <= x1 and w not in head])
    above = [ln for ln in lines if ln[0].cy < hy][::-1]
    below = [ln for ln in lines if ln[0].cy > hy]
    best = []
    for side in (below, above):
        got, prev = [], hy
        step = None
        col2 = head[1].x if len(head) > 1 else x1
        for ln in side:
            ln = [w for w in ln if w.x < x1 - 8]
            if not ln or ln[0].x >= col2 - 2:
                continue                              # nothing in the first column: text beside the table
            cy = sum(w.cy for w in ln) / len(ln)
            gap = abs(cy - prev)
            if step is not None and gap > step * 1.8:
                break
            if not starts(ln[0].text):
                break
            step = step or gap
            got.append(ln)
            prev = cy
        if len(got) > len(best):
            best = got
    return best


AREAS: list = []                               # (x0, y0, x1, y1) of tables found by the last parse


def _area(head, rows):
    ws = list(head) + [w for ln in rows for w in ln]
    AREAS.append((min(w.x for w in ws) - 2, min(w.y for w in ws) - 2, max(w.r for w in ws) + 2,
                  max(w.y + w.h for w in ws) + 2))


def _bom(words: list[Word]) -> list[dict]:
    """BOM: header ITEM NO. | PART NUMBER | DESCRIPTION | QTY.; rows start with the item number."""
    for h in [w for w in words if w.text.upper().startswith("ITEM")]:
        head = _header_row(words, h, gap=200, ends=("QTY", "QUANTITY"))
        joined = " ".join(w.text.upper() for w in head)
        if "QTY" not in joined or ("PART" not in joined and "DESCRIPTION" not in joined):
            continue
        qty_x = next(w.x for w in head if "QTY" in w.text.upper())
        desc_x = next((w.x for w in head if w.text.upper().startswith("DESCRIPTION")), None)
        out = []
        rows = _table_rows(words, head, lambda t: t.rstrip(".").isdigit())
        if rows:
            _area(head, rows)
        for ln in rows:
            item = int(ln[0].text.rstrip("."))
            rest = ln[1:]
            qw = [w for w in rest if w.x >= qty_x - 8 and re.fullmatch(r"\d+(\.\d+)?", w.text)]
            mid = [w for w in rest if w not in qw]
            qty = float(qw[-1].text) if qw else 1.0
            pn = ""
            if mid and re.search(r"\d", mid[0].text) and (desc_x is None or mid[0].r < desc_x - 2):
                pn = mid.pop(0).text
            out.append({"item": item, "partNumber": pn, "description": " ".join(w.text for w in mid), "qty": qty})
        if out:
            return sorted(out, key=lambda r: r["item"])
    return []


DATE_RE = re.compile(r"\b(\d{1,2}[A-Z]{3}\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{6})\b", re.I)


def _revisions(words: list[Word]) -> list[dict]:
    for h in [w for w in words if w.text.upper() in ("REV.", "REV", "REVISION", "ZONE")]:
        head = _header_row(words, h, gap=330, ends=("BY", "APPROVED", "APPD", "APP'D", "APPR"))
        joined = " ".join(w.text.upper() for w in head)
        if "DESCRIPTION" not in joined or "DATE" not in joined:
            continue
        out = []
        rows = _table_rows(words, head, lambda t: bool(re.fullmatch(r"[A-Z]{1,2}|\d{1,2}|-", t)))
        if rows:
            _area(head, rows)
            pass
        for ln in rows:
            text = " ".join(w.text for w in ln[1:])
            m = list(DATE_RE.finditer(text))
            if m:
                desc, date, by = text[:m[-1].start()].strip(), m[-1].group(1), text[m[-1].end():].strip()
            else:
                desc, date, by = text, "", ""
            out.append({"rev": ln[0].text, "description": desc, "date": date, "by": by})
        if out:
            return out
    return []


BOILER = re.compile(r"PROPERTY|RIGHTS RESERVED|REPRODUCTION|CONSENT|DIMENSIONS IN|TOLERANCES|UNLESS OTHERWISE|"
                    r"PROJECTION|THIRD ANGLE|FIRST ANGLE|ANGULAR|FRACTIONAL|SCALE|SHEET|DRAWN|APPROX|ITEM NO|"
                    r"REVISIONS|PART NUMBER|DESCRIPTION|MATERIAL|FINISH|^DATE|^APV|^REV", re.I)


def _notes(cs: list[Word], skip: set, values: set, block=None) -> list[str]:
    """Free text: lines with words in them that aren't labels, values, tables, dimensions or boilerplate;
    in the title block only full sentences count. Lines stacked close together join into one note."""
    def inside(c, a):
        return a[0] <= c.cx <= a[2] and a[1] <= c.cy <= a[3]
    # disclaimer paragraphs: a long line naming property / reproduction / consent, and the lines wrapped with it
    lines = _clusters([Word(c.text, c.x, c.y, c.w, c.h) for c in cs])
    runs = []                                         # each line split where there's a wide gap
    for ln in lines:
        cur = [ln[0]]
        for w in ln[1:]:
            if w.x - cur[-1].r > 12:
                runs.append(cur)
                cur = [w]
            else:
                cur.append(w)
        runs.append(cur)
    para = [r for r in runs if len(" ".join(w.text for w in r)) > 30
            and re.search(r"PROPERTY|REPRODUC|CONSENT|PROHIBITED|CONFIDENTIAL", " ".join(w.text for w in r), re.I)]
    for r in list(runs):
        if r not in para and any(abs(r[0].x - p[0].x) < 5 and abs(r[0].cy - p[0].cy) < 2.6 * max(r[0].h, p[0].h)
                                 for p in para):
            para.append(r)
    zones = [(r[0].x - 1, min(w.y for w in r) - 1, r[-1].r + 1, max(w.y + w.h for w in r) + 1) for r in para]
    cand = []
    for c in cs:
        if any(z[0] <= c.cx <= z[2] and z[1] <= c.cy <= z[3] for z in zones):
            continue                                  # part of a disclaimer
        if id(c) in skip or not re.search(r"[A-Za-z]{3}", c.text) or BOILER.search(c.text):
            continue
        if re.fullmatch(r"[\d.,\s\"'°±()\[\]RØ/xX-]+", c.text) or c.text.strip().upper() in values:
            continue
        if any(inside(c, a) for a in AREAS):
            continue
        if block and inside(c, block) and len(c.text.split()) < 5:
            continue
        if c.text.strip().upper().startswith(("RANGATE", "©")) or "©" in c.text:
            continue
        cand.append(c)
    cand.sort(key=lambda c: (c.y, c.x))
    out, used = [], set()
    for i, c in enumerate(cand):
        if i in used:
            continue
        text, last = c.text, c
        for j in range(i + 1, len(cand)):
            d = cand[j]
            if j not in used and abs(d.x - c.x) < 4 and 0 < d.y - last.y < last.h * 2.2:
                text += " " + d.text
                used.add(j)
                last = d
        out.append(text.strip())
    return out


def _weight(text: str | None):
    if not text:
        return None, None
    m = re.search(r"([\d.]+)\s*(LBS?|LB\.|KG|G)\b", text, re.I)
    if not m:
        m2 = re.search(r"([\d.]+)", text)
        return (float(m2.group(1)) if m2 else None), text
    v, u = float(m.group(1)), m.group(2).upper().rstrip(".")
    lb = v if u.startswith("LB") else v * 2.20462 if u == "KG" else v / 453.592
    return round(lb, 4), text


def _tolerances(cs: list[Word]) -> dict:
    t = " ".join(c.text for c in cs)
    out = {}
    for k, pat in (("X.XXX", r"X\.XXX\"?\s*[±+-]?\s*([\d.]+)"), ("X.XX", r"X\.XX\"?\s*[±+-]?\s*([\d.]+)"),
                   ("X.X", r"X\.X\"?\s*[±+-]?\s*([\d.]+)"), ("angular", r"ANGULAR\s*[±+-]?\s*([\d.]+)"),
                   ("fractional", r"FRACTIONAL\s*[±+-]?\s*([\d/]+)")):
        m = re.search(pat, t, re.I)
        if m:
            out[k] = m.group(1)
    return out


def parse(path, page: int = 0, ocr: bool = True) -> dict:
    d = _qdoc(path)
    try:
        words, source = words_from_text(d, page), "text"
        if len([w for w in words if re.search(r"[A-Za-z]", w.text)]) < 5:
            if not ocr:
                raise DrawingError("No text in this PDF (scanned)")
            words, source = words_from_ocr(d, page), "ocr"
        meta = {}
        try:
            from pypdf import PdfReader
            m = PdfReader(str(path)).metadata or {}
            for k, key in (("title", "/Title"), ("author", "/Author"), ("creator", "/Creator")):
                if m.get(key):
                    meta[k] = str(m.get(key))
            for k, key in (("created", "creation_date"), ("modified", "modification_date")):
                v = getattr(m, key, None)
                if v:
                    meta[k] = v.strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            pass
        pages = d.pageCount()
    finally:
        d.close()
    cs = cells(words)
    AREAS.clear()
    res = {"file": str(path), "pages": pages, "source": source, "meta": meta}
    used, labs = set(), []
    for key in ("partNumber", "description", "material", "finish", "weight", "drawnBy", "scale"):
        v, lab = _label_value(cs, LABELS[key])
        res[key] = v
        if lab is not None:
            used.add(id(lab))
            labs.append(lab)
    # DATE: the left-most DATE with a value (drawn date; approval date is usually blank)
    dates = []
    for c in cs:
        if c.text.upper() == "DATE" or c.text.upper().startswith("DATE "):
            rest = c.text[4:].strip()
            vals = [rest] if rest else [o.text for o in _row(cs, c) if 0 <= o.x - c.r < 80]
            for v in vals[:1]:
                if DATE_RE.search(v):
                    dates.append((c.x, DATE_RE.search(v).group(1)))
    res["drawnDate"] = sorted(dates)[0][1] if dates else None
    # REV. in the title block: the label nearest the bottom-right with a short value
    revs = []
    for c in cs:
        up = c.text.upper()
        if up in ("REV.", "REV") or up.startswith("REV. ") or up.startswith("REV "):
            rest = c.text.split(None, 1)[1].strip() if " " in c.text else ""
            if rest and re.fullmatch(r"[A-Z0-9]{1,3}", rest):
                revs.append((c, rest))
                continue
            near = [o for o in _row(cs, c) if 0 <= o.x - c.r < 40 and re.fullmatch(r"[A-Z0-9]{1,3}", o.text)]
            if near:
                revs.append((c, near[0].text))
    revs.sort(key=lambda r: (r[0].y + r[0].x), reverse=True)
    res["rev"] = revs[0][1] if revs else None
    m = re.search(r"SHEET\s*(\d+)\s*OF\s*(\d+)", " ".join(c.text for c in cs), re.I)
    res["sheet"] = f"{m.group(1)} of {m.group(2)}" if m else None
    if res.get("scale"):
        sm = re.search(r"\d+\s*:\s*\d+", res["scale"])
        res["scale"] = sm.group(0).replace(" ", "") if sm else res["scale"]
    res["weightLb"], res["weightText"] = _weight(res.pop("weight", None))
    if res["weightLb"] is None:                                  # weight printed below / beside its label
        for c in cs:
            if "WEIGHT" in c.text.upper():
                for o in _row(cs, c, tol=max(c.h, 6)):
                    if o.x > c.x and re.search(r"[\d.]+\s*(LB|KG)", o.text, re.I):
                        res["weightLb"], res["weightText"] = _weight(o.text)
                        break
    for k in ("material", "finish"):
        if res.get(k) and res[k].upper() in ("AS SHOWN", "SEE BOM", "SEE NOTES", "-", "—", "N/A", "NONE"):
            res[k + "Note"], res[k] = res[k], None
    if res.get("partNumber"):
        res["partNumber"] = res["partNumber"].split()[0]
    res["bom"] = _bom(words)
    res["revisions"] = _revisions(words)
    res["tolerances"] = _tolerances(cs)
    block = None
    if len(labs) >= 3:                                            # title block: around its labels, to the corner
        block = (min(lb.x for lb in labs) - 60, min(lb.y for lb in labs) - 20, 10_000, 10_000)
    vals = {str(res.get(k) or "").upper() for k in ("description", "materialNote", "finishNote", "material",
                                                   "finish", "drawnBy", "drawnDate", "partNumber")} - {""}
    res["notes"] = _notes(cs, used, vals, block)
    if not res.get("partNumber") and meta.get("title") and re.search(r"\d", meta["title"]):
        res["partNumber"] = meta["title"].split()[0]
    return res


# ------------------------------------------------------------------ into DIMS
FIELDS = [("description", "Name / description"), ("revision", "Revision"), ("weightLb", "Weight (lb)"),
          ("material", "Material"), ("finish", "Finish"), ("drawingUrl", "Drawing")]


def proposals(inv, d: dict) -> dict:
    """What reading this drawing would change. {"part": existing or None, "fields": [(key, label, current, new)],
    "bom": [{row…, "part": match or None}], "isWeldment": bool, "revisions": [...]}"""
    from . import model as M
    from . import mrp as R
    pn = (d.get("partNumber") or "").strip()
    part = M.find_part(inv.parts, "", pn) if pn else None
    if part is not None and M._pn(part.get("partNumber")) != M._pn(pn):
        part = None
    cur = part or {}
    new = {"description": d.get("description"), "revision": d.get("rev"), "weightLb": d.get("weightLb"),
           "material": d.get("material"), "finish": d.get("finish"), "drawingUrl": M.portable_path(d.get("file"))}
    old = {"description": cur.get("name"), "revision": cur.get("revision"), "weightLb": cur.get("weightLb"),
           "material": R.mat_of(cur).get("grade") if cur else None,
           "finish": R.fin_of(cur).get("process") if cur else None,
           "drawingUrl": M.portable_path(cur.get("drawingUrl")) if cur.get("drawingUrl") else None}
    fields = []
    for k, lab in FIELDS:
        if new.get(k) in (None, ""):
            continue
        same = str(old.get(k) or "").strip().upper() == str(new[k]).strip().upper()
        try:
            same = same or (k == "weightLb" and abs(float(old.get(k) or 0) - float(new[k])) < 1e-6)
        except (TypeError, ValueError):
            pass
        fields.append({"key": k, "label": lab, "current": old.get(k), "new": new[k], "same": same})
    bom = []
    for r in d.get("bom") or []:
        hit = M.find_part(inv.parts, "", r["partNumber"]) if r.get("partNumber") else None
        if hit is not None and M._pn(hit.get("partNumber")) != M._pn(r["partNumber"]):
            hit = None
        bom.append({**r, "part": hit})
    desc = (d.get("description") or "").upper()
    pre = pn.split("-")[0].upper()
    weld = bool(bom) and (desc.startswith(("WLDMT", "WELDMENT", "WELDT")) or (len(pre) == 3 and pre[1] == "2"))
    assy = bool(bom) and not weld
    return {"part": part, "partNumber": pn, "fields": fields, "bom": bom, "isWeldment": weld, "isAssembly": assy,
            "revisions": d.get("revisions") or []}


def apply(inv, d: dict, prop: dict, keys: set, bom: bool = True, create_missing: bool = False,
          create_part: bool = True, by: str = "") -> dict:
    """Write the chosen fields into the part (creating it when new). Returns {"part", "created": [...]}"""
    from . import model as M
    from . import mrp as R
    part = prop["part"]
    created = []
    if part is None:
        if not create_part:
            return {"part": None, "created": []}
        part = M.new_part(name=d.get("description") or prop["partNumber"], partNumber=prop["partNumber"],
                          type="manufactured", createdBy=by or "drawing")
        inv.parts.append(part)
        created.append(part)
    newv = {f["key"]: f["new"] for f in prop["fields"]}
    if "description" in keys and newv.get("description"):
        part["name"] = newv["description"]
    if "revision" in keys and newv.get("revision"):
        part["revision"] = str(newv["revision"]).upper()
    if "weightLb" in keys and newv.get("weightLb") is not None:
        part["weightLb"] = newv["weightLb"]
    if "material" in keys and newv.get("material"):
        m = dict(R.mat_of(part))
        m["grade"] = newv["material"]
        part["material"] = m
    if "finish" in keys and newv.get("finish"):
        f = dict(R.fin_of(part))
        f["process"] = newv["finish"]
        part["finish"] = f
    if "drawingUrl" in keys and newv.get("drawingUrl"):
        part["drawingUrl"] = newv["drawingUrl"]
    if prop.get("revisions"):
        hist = {(r.get("rev"), r.get("date")) for r in part.get("revisionHistory") or []}
        part["revisionHistory"] = list(part.get("revisionHistory") or []) + \
            [r for r in prop["revisions"] if (r.get("rev"), r.get("date")) not in hist]
    if bom and prop["bom"]:
        comps = []
        for r in prop["bom"]:
            child = r["part"]
            if child is None and create_missing and r.get("partNumber"):
                child = M.new_part(name=r.get("description") or r["partNumber"], partNumber=r["partNumber"],
                                   type="manufactured", createdBy=by or "drawing")
                inv.parts.append(child)
                created.append(child)
                r["part"] = child
            if child is not None and child is not part:
                comps.append({"partId": child["id"], "qty": r.get("qty") or 1})
        if comps:
            part["components"] = comps
            if prop["isWeldment"]:
                part["isWeldment"] = True
            elif prop["isAssembly"]:
                part["isAssembly"] = True
    part["updatedAt"] = M.now_ms()
    return {"part": part, "created": created}
