"""Files given to Icarus: images (sent as pictures), spreadsheets (Excel / CSV, turned into text tables) and PDFs
(text extracted; Claude also gets the PDF itself, so scanned pages and drawings can be read). Qt-free."""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SHEET_EXTS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
PDF_EXTS = {".pdf"}
ALL_EXTS = IMAGE_EXTS | SHEET_EXTS | PDF_EXTS
FILTER = ("Images, spreadsheets, PDFs (*.png *.jpg *.jpeg *.webp *.xlsx *.xlsm *.xls *.csv *.tsv *.pdf);;"
          "Spreadsheets (*.xlsx *.xlsm *.xls *.csv *.tsv);;PDF (*.pdf);;Images (*.png *.jpg *.jpeg *.webp)")
MAX_CHARS = 60_000
MAX_ROWS = 400


def kind_of(path) -> str:
    ext = os.path.splitext(str(path))[1].lower()
    return "image" if ext in IMAGE_EXTS else "sheet" if ext in SHEET_EXTS else "pdf" if ext in PDF_EXTS else ""


# ------------------------------------------------------------------ spreadsheets
def sheets(path) -> dict[str, list[list]]:
    """{sheet name: rows (lists of cell values)}."""
    p = str(path)
    ext = os.path.splitext(p)[1].lower()
    if ext in (".csv", ".tsv"):
        raw = Path(p).read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        delim = "\t" if ext == ".tsv" else (csv.Sniffer().sniff(text[:4000], ",;\t|").delimiter
                                           if text.strip() else ",")
        return {Path(p).stem: [r for r in csv.reader(io.StringIO(text), delimiter=delim)]}
    if ext == ".xls":
        try:
            import xlrd
        except ImportError as e:
            raise ValueError("Old .xls files need converting — open it in Excel and Save As .xlsx") from e
        wb = xlrd.open_workbook(p)
        return {s.name: [s.row_values(i) for i in range(s.nrows)] for s in wb.sheets()}
    import openpyxl
    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    out = {}
    for ws in wb.worksheets:
        out[ws.title] = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return out


def _clean(rows):
    rows = [["" if c is None else c for c in r] for r in rows]
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    width = max((max((i + 1 for i, c in enumerate(r) if str(c).strip()), default=0) for r in rows), default=0)
    return [r[:width] for r in rows]


def _cell(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if hasattr(v, "isoformat"):
        v = v.isoformat()[:10] if getattr(v, "hour", 0) == 0 and getattr(v, "minute", 0) == 0 else v.isoformat()
    return str(v).replace("\n", " ").replace("|", "/").strip()


def read_rows(path, sheet: str | None = None) -> list[dict]:
    """Records from a sheet: the header is the first row with 2+ text cells."""
    all_ = sheets(path)
    rows = all_.get(sheet) if sheet else next(iter(all_.values()), [])
    if rows is None:
        raise ValueError(f"No sheet '{sheet}' — sheets: {', '.join(all_)}")
    rows = _clean(rows)
    for i, r in enumerate(rows):
        if sum(isinstance(c, str) and c.strip() != "" for c in r) >= 2:
            head = [str(c).strip() or f"Column {j + 1}" for j, c in enumerate(r)]
            return [dict(zip(head, x)) for x in rows[i + 1:]]
    return []


def sheet_text(path, start: int = 0, limit: int = MAX_ROWS, sheet: str | None = None) -> str:
    """Sheets as pipe tables (header + rows), with row numbers so later reads can continue."""
    out, used = [], 0
    for name, rows in sheets(path).items():
        if sheet and name != sheet:
            continue
        rows = _clean(rows)
        if not rows:
            out.append(f"## Sheet '{name}' (empty)")
            continue
        part = rows[start:start + limit]
        lines = [f"## Sheet '{name}' — {len(rows)} rows" + (f" (showing {start + 1}–{start + len(part)})"
                                                             if len(rows) > limit or start else "")]
        for i, r in enumerate(part, start=start + 1):
            line = f"{i}| " + " | ".join(_cell(c) for c in r)
            if used + len(line) > MAX_CHARS:
                lines.append(f"… truncated at row {i - 1}; read more with dims_read_file(start={i - 1})")
                break
            lines.append(line)
            used += len(line) + 1
        out.append("\n".join(lines))
    return "\n\n".join(out)


# ------------------------------------------------------------------ PDF
def pdf_text(path, max_chars: int = MAX_CHARS) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "(PDF text extraction needs the pypdf package — pip install pypdf)"
    r = PdfReader(str(path))
    out, used = [f"{len(r.pages)} page(s)"], 0
    for i, pg in enumerate(r.pages, start=1):
        t = re.sub(r"[ \t]+", " ", (pg.extract_text() or "")).strip()
        chunk = f"--- page {i} ---\n{t or '(no text layer — scanned page or drawing)'}"
        if used + len(chunk) > max_chars:
            out.append(f"… truncated at page {i - 1} of {len(r.pages)}")
            break
        out.append(chunk)
        used += len(chunk)
    return "\n".join(out)


# ------------------------------------------------------------------ for a prompt
def describe(path) -> dict:
    """{"name", "path", "kind", "text", "data"}: data = bytes for images / PDFs."""
    p = Path(path)
    k = kind_of(p)
    if not k:
        raise ValueError(f"Can't attach {p.suffix or 'this file'} — images, Excel / CSV and PDF only")
    d = {"name": p.name, "path": str(p.resolve()), "kind": k, "text": "", "data": None}
    if k == "image":
        d["data"] = p.read_bytes()
    elif k == "sheet":
        d["text"] = sheet_text(p)
    else:
        d["data"] = p.read_bytes()
        d["text"] = pdf_text(p)
    return d


def prompt_block(d: dict) -> str:
    return (f'<attachment name="{d["name"]}" path="{d["path"]}" type="{d["kind"]}">\n{d["text"]}\n</attachment>')
