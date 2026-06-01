"""Local OCR for images / scanned PDFs (Tesseract via pytesseract).

This is the *interpreter* side of the pipeline, not the harvester. The client just
downloads and preserves the raw file (the harvester records its path as `content_ref`);
whatever machine runs interpretation reads those bytes and OCRs them. So OCR runs wherever
Stage 2 runs (your GPU desktop, eventually), and a browser-extension-style thin client
never needs a Tesseract binary.

Graceful: if Tesseract isn't installed, `ocr_available()` is False and `ocr_file()`
returns None — the item just stays `needs_vision` for a future vision model.

TODO (multi-user): cache results keyed by file hash (see changeset.content_hash), so a
scanned document shared across a section is OCR'd once for everyone, not once per run.
"""

from __future__ import annotations

import io
from pathlib import Path

# 200 DPI reads clean scans well; cap pages so a giant scanned book can't stall a run.
_OCR_DPI = 200
_OCR_MAX_PAGES = 20
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}

# Tesseract availability is probed once and cached (probing it is not free).
_checked = False
_ok = False


def ocr_available() -> bool:
    """True if pytesseract + the Tesseract binary are both usable. Cached."""
    global _checked, _ok
    if _checked:
        return _ok
    _checked = True
    try:
        import pytesseract

        pytesseract.get_tesseract_version()  # raises if the binary is missing
        _ok = True
    except Exception:
        _ok = False
    return _ok


def _ocr_image(path: Path) -> str | None:
    try:
        import pytesseract
        from PIL import Image

        with Image.open(path) as img:
            text = pytesseract.image_to_string(img)
        return text.strip() or None
    except Exception:
        return None


def _ocr_pdf(path: Path) -> str | None:
    """OCR a scanned (text-less) PDF by rasterizing pages with PyMuPDF, then Tesseract."""
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except Exception:
        return None
    parts: list[str] = []
    try:
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= _OCR_MAX_PAGES:
                    break
                pix = page.get_pixmap(dpi=_OCR_DPI)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                txt = pytesseract.image_to_string(img)
                if txt.strip():
                    parts.append(txt.strip())
    except Exception:
        return None
    return "\n".join(parts).strip() or None


def ocr_file(path: Path) -> str | None:
    """OCR an image or scanned PDF to text. None if OCR is unavailable or fails."""
    if not ocr_available():
        return None
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _ocr_pdf(path)
    if ext in _IMAGE_EXTS:
        return _ocr_image(path)
    return None
