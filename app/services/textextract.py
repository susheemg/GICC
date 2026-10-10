"""Text acquisition for the AI layer - GICC-05 §3.

Honest about its limits: PDF text is extracted properly, plain formats are
decoded, and everything else falls back to the filename. A silent empty string
would make classification look broken rather than limited.
"""
from __future__ import annotations

from ..config import AI_MAX_INPUT_CHARS

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm"}


def extract(data: bytes, filename: str) -> tuple[str, str]:
    """Returns (text, method). Never raises: a file we cannot read must still
    upload."""
    suffix = ("." + filename.lower().rsplit(".", 1)[-1]) if "." in filename else ""

    if suffix == ".pdf":
        text, method = _pdf(data)
        if text.strip():
            return text[:AI_MAX_INPUT_CHARS], method
        return "", "pdf-no-text"      # scanned image, needs OCR we do not have

    if suffix in TEXT_SUFFIXES:
        try:
            return data.decode("utf-8-sig", errors="replace")[:AI_MAX_INPUT_CHARS], "text"
        except Exception:
            return "", "decode-failed"

    if suffix in {".docx", ".xlsx", ".pptx"}:
        text = _ooxml(data)
        return text[:AI_MAX_INPUT_CHARS], "ooxml" if text else "ooxml-empty"

    return "", "unsupported"


def _pdf(data: bytes) -> tuple[str, str]:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:12]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages), "pdf"
    except Exception:
        return "", "pdf-failed"


def _ooxml(data: bytes) -> str:
    """Office formats are zipped XML; pulling the text out needs no extra
    dependency and is good enough to classify on."""
    try:
        import io
        import re
        import zipfile

        text_parts = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml") or "docProps" in name:
                    continue
                if not any(part in name for part in
                           ("document", "sharedStrings", "sheet", "slide")):
                    continue
                raw = archive.read(name).decode("utf-8", errors="replace")
                text_parts.append(re.sub(r"<[^>]+>", " ", raw))
                if sum(len(p) for p in text_parts) > AI_MAX_INPUT_CHARS:
                    break
        return re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    except Exception:
        return ""
