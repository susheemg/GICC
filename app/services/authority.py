"""Reading where a document says it can be checked.

Most certificates now carry a way back to whoever issued them: a QR code, a
printed verification URL, or a certificate-search reference. This module finds
those pointers and classifies them. It does **not** treat finding one as
verification.

That distinction is the whole module. A QR code is printed by whoever made the
PDF. A forger who copies a genuine certificate copies its QR code too, or points
it at a page they control. So a pointer is evidence of *where to look*, and the
assurance it earns is decided downstream by what looking finds:

  * pointer to a recognised authority  -> becomes the version's
    verification_url, and the normal link check / CertSearch reconciliation
    runs against it. That is L1 at most until something confirms the content.
  * pointer to a look-alike domain     -> an authenticity flag. "bsi-verify.co"
    is not BSI, and a certificate whose QR goes there deserves a person's
    attention before anyone relies on it.
  * pointer to an unknown domain       -> recorded, not trusted, not flagged.
    Our list of authorities is incomplete and will stay that way.

QR decoding uses OpenCV when it is installed. Without it the module still reads
printed URLs from the text layer, and says which method it used.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse

# Domain -> (authority, what it can confirm). Matching is on the registrable
# domain and its subdomains, never on a substring, so look-alikes do not pass.
AUTHORITIES: dict[str, tuple[str, str]] = {
    "iafcertsearch.org": ("IAF CertSearch", "management-system certification"),
    "bsigroup.com": ("BSI Group", "certificate status"),
    "lrqa.com": ("LRQA", "certificate status"),
    "sgs.com": ("SGS", "certificate status"),
    "bureauveritas.com": ("Bureau Veritas", "certificate status"),
    "dnv.com": ("DNV", "certificate status"),
    "tuv.com": ("TÜV Rheinland", "certificate status"),
    "tuvsud.com": ("TÜV SÜD", "certificate status"),
    "intertek.com": ("Intertek", "certificate status"),
    "nqa.com": ("NQA", "certificate status"),
    "ukas.com": ("UKAS", "accreditation of the certifying body"),
    "a2la.org": ("A2LA", "laboratory accreditation"),
    "dakks.de": ("DAkkS", "accreditation"),
    "find-and-update.company-information.service.gov.uk":
        ("Companies House", "company registration"),
    "company-information.service.gov.uk": ("Companies House", "company registration"),
    "mca.gov.in": ("Ministry of Corporate Affairs (India)", "company registration"),
    "gleif.org": ("GLEIF", "Legal Entity Identifier"),
    "lei-lookup.com": ("LEI lookup", "Legal Entity Identifier"),
    "cyberessentials.ncsc.gov.uk": ("NCSC Cyber Essentials", "Cyber Essentials"),
    "iasme.co.uk": ("IASME", "Cyber Essentials certification"),
    "ecovadis.com": ("EcoVadis", "sustainability rating"),
    "cdp.net": ("CDP", "climate disclosure"),
    "fca.org.uk": ("FCA Register", "financial-services authorisation"),
    "ico.org.uk": ("ICO", "data-protection registration"),
    "gov.uk": ("UK Government", "varies by service"),
}

# Words a look-alike borrows. A domain containing one of these but not on the
# list above is the pattern worth a second look.
BRAND_TOKENS = {"iaf", "certsearch", "bsi", "lrqa", "sgs", "veritas", "dnv",
                "tuv", "intertek", "nqa", "ukas", "a2la", "dakks",
                "companieshouse", "gleif", "ecovadis", "ncsc", "iasme", "fca"}

URL_PATTERN = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def classify_url(url: str) -> dict:
    host = _host(url)
    if not host:
        return {"url": url, "host": "", "kind": "invalid"}

    for domain, (authority, confirms) in AUTHORITIES.items():
        if host == domain or host.endswith("." + domain):
            return {"url": url, "host": host, "kind": "authority",
                    "authority": authority, "confirms": confirms,
                    "https": url.lower().startswith("https://")}

    compact = re.sub(r"[^a-z0-9]", "", host)
    borrowed = sorted(token for token in BRAND_TOKENS if token in compact)
    if borrowed:
        return {"url": url, "host": host, "kind": "lookalike",
                "imitates": borrowed,
                "reason": (f"The domain borrows '{borrowed[0]}' but is not one the "
                           "programme recognises for that authority.")}
    return {"url": url, "host": host, "kind": "unknown"}


def urls_in_text(text: str) -> list[str]:
    found = []
    for match in URL_PATTERN.findall(text or ""):
        cleaned = match.rstrip(".,;:")
        if cleaned not in found:
            found.append(cleaned)
    return found[:20]


def qr_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def _decode_image(data: bytes) -> list[str]:
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    # Certificates arrive as JPEG-compressed scans, and a dense code (a long
    # verification URL) does not survive compression cleanly. Try the image as
    # it is, then binarised, then upscaled. Note that OpenCV can report success
    # with an empty string, so emptiness - not the flag - decides.
    detector = cv2.QRCodeDetector()
    _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    attempts = [image, binary]
    for scale in (2.0, 3.0):
        attempts.append(cv2.resize(binary, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_NEAREST))
    attempts.append(cv2.medianBlur(binary, 3))
    for candidate in attempts:
        try:
            ok, decoded, _points, _ = detector.detectAndDecodeMulti(candidate)
            payloads = [p for p in (decoded or ()) if p] if ok else []
        except Exception:
            payloads = []
        if not payloads:
            text, _points, _ = detector.detectAndDecode(candidate)
            payloads = [text] if text else []
        if payloads:
            return payloads
    return []


def qr_payloads(data: bytes, filename: str) -> list[str]:
    """QR contents from an image, or from the images embedded in a PDF."""
    name = (filename or "").lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif",
                      ".tiff")):
        return _decode_image(data)
    if data[:5] != b"%PDF-":
        return []
    payloads: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:6]:          # certificates are short
            for image in getattr(page, "images", []) or []:
                payloads.extend(_decode_image(image.data))
                if len(payloads) >= 5:
                    return payloads
    except Exception:
        return payloads
    return payloads


def findings(data: bytes, filename: str, text: str) -> dict:
    """Everything the document says about where it can be checked."""
    from_qr = [p for p in qr_payloads(data, filename) if p]
    qr_urls = [p for p in from_qr if URL_PATTERN.match(p)]
    text_urls = [u for u in urls_in_text(text) if u not in qr_urls]

    entries = ([{**classify_url(u), "via": "qr"} for u in qr_urls]
               + [{**classify_url(u), "via": "text"} for u in text_urls])
    authority = [e for e in entries if e["kind"] == "authority"]
    lookalike = [e for e in entries if e["kind"] == "lookalike"]

    # Prefer what the QR says over what the text says: the QR is what a
    # reader's phone would actually open.
    best = next((e for e in authority if e["via"] == "qr"), None) or \
        (authority[0] if authority else None)
    return {
        "qr_method": "opencv" if qr_available() else "unavailable",
        "qr_payloads": from_qr[:5],
        "entries": entries[:20],
        "authority": authority,
        "lookalike": lookalike,
        "best": best,
        # The doctored-certificate pattern: the visible text still names a real
        # authority, but the code a phone would open goes somewhere that is not
        # one. Two *different* genuine authorities (a BSI link and an IAF code)
        # is common on real certificates and is not flagged.
        "qr_text_disagree": bool(
            qr_urls
            and any(e["via"] == "text" and e["kind"] == "authority" for e in entries)
            and not any(e["via"] == "qr" and e["kind"] == "authority" for e in entries)),
    }
