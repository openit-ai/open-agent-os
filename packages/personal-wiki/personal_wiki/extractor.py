"""Attachment text extraction — lazy deps, no hard failures.

Handles:
  pdf  -> pypdf (PdfReader) fallback pdfminer.six
  docx -> python-docx
  xlsx -> openpyxl
  pptx -> python-pptx
  txt/md/csv/json -> plain read
  images (png/jpg/jpeg/gif/webp/bmp/tiff) -> OCR stub (pytesseract/easyocr if available else placeholder)

All imports are inside functions (lazy). If lib missing, fallback to "" or stub string.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".md", ".csv", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
})

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"})
_MAX_CHARS_DEFAULT = 20000


def _truncate(text: str, max_chars: int) -> str:
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text)-max_chars} chars]"
    return text


def _read_txt(path: Path, max_chars: int) -> str:
    try:
        # try utf-8 then latin1
        try:
            t = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            t = path.read_text(encoding="latin-1")
        return _truncate(t, max_chars)
    except Exception as e:
        return f"[txt read error: {e}]"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_pdf(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    p = Path(path)
    # Try pypdf first (lazy)
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(p))
        parts: list[str] = []
        for page in getattr(reader, "pages", []):
            try:
                txt = page.extract_text() or ""
                if txt:
                    parts.append(txt)
            except Exception:
                continue
        if parts:
            return _truncate("\n".join(parts), max_chars)
    except Exception:
        pass
    # fallback pdfminer.six
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract  # type: ignore
        txt = pdfminer_extract(str(p)) or ""
        if txt.strip():
            return _truncate(txt, max_chars)
    except Exception:
        pass
    # last fallback: try to read as binary and report
    try:
        size = p.stat().st_size
        return f"[pdf extraction unavailable — install pypdf or pdfminer.six (file {p.name}, {size} bytes)]"
    except Exception as e:
        return f"[pdf extraction failed: {e}]"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def extract_docx(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    p = Path(path)
    try:
        from docx import Document  # type: ignore
        doc = Document(str(p))
        parts: list[str] = []
        for para in getattr(doc, "paragraphs", []):
            t = getattr(para, "text", "")
            if t:
                parts.append(t)
        # tables
        for table in getattr(doc, "tables", []):
            for row in getattr(table, "rows", []):
                cells = [getattr(c, "text", "") for c in getattr(row, "cells", [])]
                if any(cells):
                    parts.append(" | ".join(cells))
        if parts:
            return _truncate("\n".join(parts), max_chars)
        return ""
    except Exception as e:
        # if docx missing, fallback to zip read? but just report
        try:
            # detect missing lib vs corrupt file
            import importlib.util
            if importlib.util.find_spec("docx") is None:
                return f"[docx extraction unavailable — install python-docx (file {p.name})]"
        except Exception:
            pass
        return f"[docx extraction failed: {e}]"


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

def extract_xlsx(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    p = Path(path)
    try:
        from openpyxl import load_workbook  # type: ignore
        wb = load_workbook(str(p), read_only=True, data_only=True)
        out: list[str] = []
        for ws in wb.worksheets:
            out.append(f"# Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                # skip empty rows
                vals = [str(v) if v is not None else "" for v in row]
                if any(v.strip() for v in vals):
                    out.append("\t".join(vals))
            out.append("")
        wb.close()
        text = "\n".join(out).strip()
        return _truncate(text, max_chars) if text else ""
    except Exception as e:
        try:
            import importlib.util
            if importlib.util.find_spec("openpyxl") is None:
                return f"[xlsx extraction unavailable — install openpyxl (file {p.name})]"
        except Exception:
            pass
        return f"[xlsx extraction failed: {e}]"


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------

def extract_pptx(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    p = Path(path)
    try:
        from pptx import Presentation  # type: ignore
        prs = Presentation(str(p))
        parts: list[str] = []
        for idx, slide in enumerate(prs.slides, 1):  # type: ignore[attr-defined]
            parts.append(f"# Slide {idx}")
            for shape in slide.shapes:  # type: ignore[attr-defined]
                try:
                    txt = getattr(shape, "text", "") or ""
                    if txt:
                        parts.append(txt)
                except Exception:
                    pass
                # tables inside shapes
                if getattr(shape, "has_table", False):  # type: ignore[attr-defined]
                    try:
                        for row in shape.table.rows:  # type: ignore
                            cells = [getattr(c, "text", "") for c in row.cells]  # type: ignore
                            if any(cells):
                                parts.append(" | ".join(cells))
                    except Exception:
                        pass
            parts.append("")
        text = "\n".join(parts).strip()
        return _truncate(text, max_chars) if text else ""
    except Exception as e:
        try:
            import importlib.util
            if importlib.util.find_spec("pptx") is None:
                return f"[pptx extraction unavailable — install python-pptx (file {p.name})]"
        except Exception:
            pass
        return f"[pptx extraction failed: {e}]"


# ---------------------------------------------------------------------------
# Images — OCR stub
# ---------------------------------------------------------------------------

def extract_image(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    """OCR stub: tries pytesseract / easyocr if available, else returns placeholder."""
    p = Path(path)
    # Try pytesseract (lazy)
    try:
        import importlib.util
        if importlib.util.find_spec("pytesseract") is not None:
            try:
                from PIL import Image  # type: ignore
                import pytesseract  # type: ignore
                img = Image.open(str(p))
                txt = pytesseract.image_to_string(img) or ""
                if txt.strip():
                    return _truncate(txt, max_chars)
            except Exception:
                pass
    except Exception:
        pass
    # Try easyocr
    try:
        import importlib.util
        if importlib.util.find_spec("easyocr") is not None:
            try:
                import easyocr  # type: ignore
                reader = easyocr.Reader(["en", "ko"], gpu=False, verbose=False)  # type: ignore
                result = reader.readtext(str(p), detail=0)  # type: ignore
                if result:
                    txt = "\n".join(result)
                    if txt.strip():
                        return _truncate(txt, max_chars)
            except Exception:
                pass
    except Exception:
        pass
    # stub fallback — never fails, returns placeholder with file info
    try:
        size = p.stat().st_size
        return f"[OCR stub: image {p.name} ({size} bytes) — install pytesseract or easyocr for text extraction]"
    except Exception as e:
        return f"[OCR stub: image {p.name} — {e}]"


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def extract_text(path: Path | str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    """Extract text from attachment by extension (lazy deps, no hard failure)."""
    p = Path(path)
    ext = p.suffix.lower()
    if not p.exists():
        return f"[file not found: {p}]"
    if ext == ".pdf":
        return extract_pdf(p, max_chars)
    if ext in (".docx", ".doc"):
        # .doc (legacy) we still try docx extractor; will fallback
        return extract_docx(p, max_chars)
    if ext in (".xlsx", ".xls"):
        return extract_xlsx(p, max_chars)
    if ext in (".pptx", ".ppt"):
        return extract_pptx(p, max_chars)
    if ext in _IMAGE_EXTS:
        return extract_image(p, max_chars)
    if ext in (".txt", ".md", ".csv", ".json"):
        return _read_txt(p, max_chars)
    # unknown: try txt read, then stub
    try:
        txt = _read_txt(p, max_chars)
        # if binary, truncate
        if txt and len(txt) < max_chars:
            return txt
        # fallback stub for binary
        if ext not in SUPPORTED_EXTENSIONS:
            return f"[unsupported extension {ext} — treated as binary, size {p.stat().st_size} bytes]"
        return txt
    except Exception as e:
        return f"[extraction failed for {p.name}: {e}]"


# Alias
extract_attachment = extract_text


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
