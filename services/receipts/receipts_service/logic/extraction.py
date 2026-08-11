"""Text extraction from uploaded documents.

- PDFs: pdfplumber (embedded text). A scanned PDF with no text layer gets a
  note instead of garbage.
- Images: pytesseract OCR (requires the tesseract binary — present in the
  Docker image; degraded gracefully elsewhere).
- HTML (Amazon invoices saved from a browser): tags stripped.
- Plain text passes through.

Extraction never raises: it returns (text, note) and the worst case is an
empty text with an explanatory note — the user can still fill fields by hand.
"""

from __future__ import annotations

import io
import re

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _HTML_RE.sub("\n", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#36;", "$")
    text = _WS_RE.sub(" ", text)
    return _NL_RE.sub("\n\n", text).strip()


def extract_text(content: bytes, content_type: str, filename: str) -> tuple[str, str]:
    """Returns (extracted_text, note). Empty note means clean extraction."""
    name = filename.lower()
    kind = (content_type or "").lower()

    if kind.startswith("application/pdf") or name.endswith(".pdf"):
        return _extract_pdf(content)
    if kind.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp")):
        return _extract_image(content)
    if kind.startswith("text/html") or name.endswith((".html", ".htm")):
        return strip_html(content.decode("utf-8", errors="replace")), ""
    if kind.startswith("text/") or name.endswith((".txt", ".csv")):
        return content.decode("utf-8", errors="replace").strip(), ""
    return "", f"unsupported file type {content_type or filename!r} — enter details manually"


def _extract_pdf(content: bytes) -> tuple[str, str]:
    try:
        import pdfplumber
    except ImportError:
        return "", "pdfplumber not installed — enter details manually"
    try:
        pages = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        text = "\n\n".join(pages).strip()
        if not text:
            return "", "PDF has no text layer (scanned image?) — enter details manually"
        return text, ""
    except Exception as exc:  # noqa: BLE001 — any parser failure ends up as a note
        return "", f"could not read PDF: {exc}"


def _extract_image(content: bytes) -> tuple[str, str]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", "OCR not available (pytesseract/PIL missing) — enter details manually"
    try:
        image = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(image).strip()
        if not text:
            return "", "OCR found no text — enter details manually"
        return text, ""
    except Exception as exc:  # noqa: BLE001
        return "", f"OCR failed: {exc}"
