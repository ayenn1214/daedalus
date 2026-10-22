"""Local reference library: your own handbooks (PDF or text), indexed for search by you and by Icarus.

Documents stay on this computer (~/.daedalus/reference). PDFs with a text layer are read directly; scanned PDFs are
OCR'd when Tesseract is installed (and PyMuPDF or Poppler's pdftoppm can render pages). Text files may mark pages
with lines like '##### PDF page 12' or form feeds.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

PAGE_MARK = re.compile(r"^#{3,}\s*(?:PDF\s+)?page\s+(\d+)\s*$", re.I | re.M)
WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?", re.I)
STOP = set("the of and to in a is for be by on at as or an are with from that this it which where when".split())


class ReferenceError_(RuntimeError):
    pass


def root() -> Path:
    p = Path.home() / ".daedalus" / "reference"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(name):
    return re.sub(r"[^\w\-]+", "_", name).strip("_")[:80] or "document"


# ----------------------------------------------------------------- reading ---

def _pages_from_text(text: str) -> list[str]:
    marks = list(PAGE_MARK.finditer(text))
    if marks:
        pages = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            n = int(m.group(1))
            while len(pages) < n - 1:
                pages.append("")
            pages.append(text[m.end():end].strip())
        return pages
    if "\f" in text:
        return [p.strip() for p in text.split("\f")]
    lines = text.splitlines()
    return ["\n".join(lines[i:i + 60]) for i in range(0, len(lines), 60)] or [""]


def _pdf_text(path: Path) -> list[str]:
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(path)) as doc:
            return [pg.get_text() for pg in doc]
    except ImportError:
        pass
    exe = shutil.which("pdftotext")
    if exe:
        out = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, text=True, errors="replace")
        return out.stdout.split("\f")
    try:
        from pypdf import PdfReader
        return [p.extract_text() or "" for p in PdfReader(str(path)).pages]
    except ImportError as e:
        raise ReferenceError_("No PDF reader available — install PyMuPDF (pip install pymupdf)") from e


def tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(p).exists():
            return p
    return None


def _ocr_pdf(path: Path, progress=None, dpi=220) -> list[str]:
    tess = tesseract()
    if not tess:
        raise ReferenceError_("This PDF is scanned (no text layer). Install Tesseract OCR "
                              "(https://github.com/UB-Mannheim/tesseract/wiki on Windows) and add it again — or add "
                              "an OCR'd .txt of it.")
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            import fitz
            doc = fitz.open(str(path))
            n = len(doc)

            def render(i):
                pix = doc[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                f = tmp / f"p{i}.png"
                pix.save(str(f))
                return f
        except ImportError:
            exe = shutil.which("pdftoppm")
            if not exe:
                raise ReferenceError_("OCR needs PyMuPDF (pip install pymupdf) or Poppler's pdftoppm to render pages")
            info = subprocess.run([shutil.which("pdfinfo") or "pdfinfo", str(path)], capture_output=True, text=True)
            m = re.search(r"Pages:\s+(\d+)", info.stdout)
            n = int(m.group(1)) if m else 0

            def render(i):
                base = tmp / f"p{i}"
                subprocess.run([exe, "-r", str(dpi), "-gray", "-png", "-f", str(i + 1), "-l", str(i + 1), "-singlefile",
                                str(path), str(base)], capture_output=True)
                return base.with_suffix(".png")
        for i in range(n):
            f = render(i)
            out = subprocess.run([tess, str(f), "-", "--psm", "3"], capture_output=True, text=True, errors="replace")
            pages.append(out.stdout)
            f.unlink(missing_ok=True)
            if progress:
                progress(i + 1, n)
    return pages


def add_document(path, name: str | None = None, progress=None, ocr: bool = True) -> dict:
    """Index a PDF or text file. Returns {name, pages, chars, ocr}."""
    path = Path(path)
    if not path.exists():
        raise ReferenceError_(f"No file {path}")
    used_ocr = False
    if path.suffix.lower() == ".pdf":
        pages = _pdf_text(path)
        if sum(len(re.findall(r"[A-Za-z]{3,}", p)) for p in pages) < 20 * max(len(pages), 1) * 0.2:
            if not ocr:
                raise ReferenceError_("Scanned PDF — enable OCR")
            pages = _ocr_pdf(path, progress)
            used_ocr = True
    else:
        pages = _pages_from_text(path.read_text(encoding="utf-8", errors="replace"))
    name = name or path.stem
    doc = {"name": name, "source": str(path), "ocr": used_ocr, "pages": pages}
    (root() / f"{_slug(name)}.json").write_text(json.dumps(doc))
    _INDEX.pop(name, None)
    return {"name": name, "pages": len(pages), "chars": sum(len(p) for p in pages), "ocr": used_ocr}


def documents() -> list[dict]:
    out = []
    for f in sorted(root().glob("*.json")):
        try:
            d = json.loads(f.read_text())
            out.append({"name": d["name"], "pages": len(d["pages"]), "source": d.get("source", ""), "file": str(f)})
        except (OSError, ValueError, KeyError):
            continue
    return out


def remove_document(name: str):
    for d in documents():
        if d["name"] == name:
            Path(d["file"]).unlink(missing_ok=True)
            _INDEX.pop(name, None)
            return True
    raise ReferenceError_(f"No reference document '{name}'")


def _load(name):
    for d in documents():
        if d["name"] == name:
            return json.loads(Path(d["file"]).read_text())
    raise ReferenceError_(f"No reference document '{name}'")


def page_text(name: str, page: int) -> str:
    d = _load(name)
    if not 1 <= page <= len(d["pages"]):
        raise ReferenceError_(f"{name} has pages 1 … {len(d['pages'])}")
    return d["pages"][page - 1]


# ------------------------------------------------------------------- search ---

_INDEX: dict = {}


def _tokens(s):
    return [w.lower() for w in WORD.findall(s) if w.lower() not in STOP]


def _index(name):
    if name not in _INDEX:
        d = _load(name)
        toks = [Counter(_tokens(p)) for p in d["pages"]]
        df = Counter()
        for t in toks:
            df.update(set(t))
        _INDEX[name] = (d["pages"], toks, df)
    return _INDEX[name]


def search(query: str, doc: str | None = None, limit: int = 8) -> list[dict]:
    """Ranked pages (BM25-ish) with a snippet around the best match."""
    q = _tokens(query)
    if not q:
        return []
    hits = []
    for d in documents():
        if doc and d["name"] != doc:
            continue
        pages, toks, df = _index(d["name"])
        N = max(len(pages), 1)
        avg = sum(sum(t.values()) for t in toks) / N or 1
        for i, t in enumerate(toks):
            L = sum(t.values()) or 1
            sc = 0.0
            for w in q:
                f = t.get(w, 0)
                if not f:
                    continue
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                sc += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * L / avg))
            if sc <= 0:
                continue
            low = pages[i].lower()
            if query.lower() in low:
                sc *= 1.8
            hits.append({"doc": d["name"], "page": i + 1, "score": round(sc, 3), "snippet": _snippet(pages[i], q)})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def _snippet(text, q, width=260):
    low = text.lower()
    pos = [low.find(w) for w in q if low.find(w) >= 0]
    c = min(pos) if pos else 0
    s = max(0, c - width // 3)
    snip = " ".join(text[s:s + width].split())
    return ("… " if s else "") + snip + (" …" if s + width < len(text) else "")
