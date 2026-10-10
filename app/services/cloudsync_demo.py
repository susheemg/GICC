"""A demo folder that behaves like a connected drive.

Real Google and Microsoft connections need an OAuth app registered with each
provider. Until one is, this folder lets the whole flow be exercised end to end -
first sync, classification, QR reading, a renewal arriving, a file being
removed, the version history that results - through exactly the same sync code.
Only the transport differs.

The files are generated, not copied from anywhere, and every one is about the
workspace that connected it. One carries a QR code pointing at a look-alike
domain on purpose, so the demo shows the check that matters most.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR
from . import cloudsync
from .cloudsync import Provider, RemoteFile

ROOT = DATA_DIR / "demo_drive"


MANIFEST = ".drive-ids.json"


def folder(connection_id: str) -> Path:
    return ROOT / connection_id


def _ids(base: Path) -> dict:
    """path -> id, kept in a manifest like a real drive keeps file ids: stable
    across renames and never reused. (An earlier version used inode numbers,
    and the filesystem handed a deleted file's number to the next new file.)"""
    try:
        return json.loads((base / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


def _save_ids(base: Path, ids: dict) -> None:
    (base / MANIFEST).write_text(json.dumps(ids, indent=1))


# --------------------------------------------------------------------------
# A small PDF writer: text plus an optional QR image, no third-party library
# --------------------------------------------------------------------------
def _qr_jpeg(url: str) -> tuple[bytes, int] | None:
    try:
        import cv2
    except Exception:
        return None
    image = cv2.QRCodeEncoder.create().encode(url)
    image = cv2.resize(image, (220, 220), interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(image, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return (buffer.tobytes(), image.shape[0]) if ok else None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines: list[str], qr_url: str | None = None) -> bytes:
    content = ["BT /F1 15 Tf 50 780 Td"]
    first = True
    for line in lines:
        if not first:
            content.append("0 -20 Td")
        content.append(f"({_escape(line)}) Tj")
        if first:
            content.append("/F1 10 Tf")
            first = False
    content.append("ET")

    qr = _qr_jpeg(qr_url) if qr_url else None
    if qr:
        content.append(f"q {qr[1]} 0 0 {qr[1]} 360 80 cm /QR Do Q")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]
    resources = b"<< /Font << /F1 5 0 R >>" + (b" /XObject << /QR 6 0 R >>" if qr else b"") + b" >>"
    objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                   b"/Resources " + resources + b" /Contents 4 0 R >>")
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                   + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if qr:
        data, size = qr
        objects.append(b"<< /Type /XObject /Subtype /Image /Width " + str(size).encode()
                       + b" /Height " + str(size).encode()
                       + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /DCTDecode"
                       + b" /Length " + str(len(data)).encode() + b" >>\nstream\n"
                       + data + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    return bytes(out)


# --------------------------------------------------------------------------
# The sample folder
# --------------------------------------------------------------------------
def _demo_documents(company: str) -> dict[str, tuple[list[str], str | None]]:
    return {
        "Certificates/ISO 27001 Certificate.pdf": ([
            "Certificate of Registration",
            "ISO/IEC 27001:2022 Information Security Management System",
            f"This is to certify that {company}",
            "operates an information security management system which complies",
            "with the requirements of ISO/IEC 27001:2022.",
            "Certificate number: IS 776541",
            "Issue date: 2024-01-15   Expiry date: 2027-01-14",
            "Issued by BSI Assurance UK Limited",
            "Verify at https://www.iafcertsearch.org/certification/IS-776541",
        ], "https://www.iafcertsearch.org/certification/IS-776541"),
        "Certificates/Cyber Essentials Plus.pdf": ([
            "Cyber Essentials Plus Certificate",
            f"Awarded to {company}",
            "Certificate number: CEP-2026-118204",
            "Issue date: 2026-03-02   Expiry date: 2027-03-01",
            "Certification body: IASME Consortium",
        ], "https://iasme.co.uk/cyber-essentials/certificate/CEP-2026-118204"),
        "Certificates/Quality Certificate ISO 9001.pdf": ([
            "Certificate of Registration",
            "ISO 9001:2015 Quality Management System",
            f"This is to certify that {company}",
            "Certificate number: FS 44710",
            "Issue date: 2025-06-01   Expiry date: 2028-05-31",
            "Verify this certificate at https://www.bsigroup.com/en-GB/validate",
        ], "https://bsi-verify-certs.co/c/FS-44710"),
        "Insurance/Public Liability Insurance 2026.pdf": ([
            "Certificate of Insurance - Public and Products Liability",
            f"Insured: {company}",
            "Policy number: PL-90231-26",
            "Limit of indemnity: GBP 5,000,000 any one occurrence",
            "Period of insurance: 2026-04-01 to 2027-03-31",
            "Insurer: Aviva Insurance Limited",
        ], None),
        "Corporate/Certificate of Incorporation.pdf": ([
            "Certificate of Incorporation of a Private Limited Company",
            f"Company name: {company}",
            "Company number 09912345",
            "Registered by the Registrar of Companies for England and Wales",
            "Check the register: https://find-and-update.company-information.service.gov.uk/company/09912345",
        ], None),
    }


def seed(connection_id: str, company: str) -> None:
    target = folder(connection_id)
    if target.exists():
        return
    for path, (lines, qr) in _demo_documents(company).items():
        file = target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(make_pdf(lines, qr))
    (target / "Misc").mkdir(parents=True, exist_ok=True)
    (target / "Misc/Team offsite notes.txt").write_text(
        "Agenda: roadmap, hiring, the summer party. Nothing to do with compliance.")


def simulate(connection_id: str, company: str, action: str) -> str:
    """Changes the demo folder the way a supplier would change a real one."""
    target = folder(connection_id)
    if action == "renew":
        lines, qr = _demo_documents(company)["Certificates/ISO 27001 Certificate.pdf"]
        lines = [l.replace("Issue date: 2024-01-15   Expiry date: 2027-01-14",
                           "Issue date: 2027-01-10   Expiry date: 2030-01-09")
                 for l in lines]
        (target / "Certificates/ISO 27001 Certificate.pdf").write_bytes(
            make_pdf(lines + ["Recertification audit completed January 2027"], qr))
        return "ISO 27001 certificate replaced with the recertified version"
    if action == "add":
        file = target / "Corporate/Modern Slavery Statement 2026.pdf"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(make_pdf([
            "Modern Slavery and Human Trafficking Statement 2026",
            f"{company} - statement under section 54 of the Modern Slavery Act 2015",
            "Approved by the board on 2026-06-30",
            "Published at https://www.gov.uk/government/collections/modern-slavery"]))
        return "Modern slavery statement added"
    if action == "remove":
        file = target / "Insurance/Public Liability Insurance 2026.pdf"
        if file.exists():
            file.unlink()
        return "Public liability certificate deleted from the folder"
    if action == "overwrite":
        # Same file, same id, different kind of document - the case the
        # overwrite guard in cloudsync exists for.
        target.joinpath("Corporate").mkdir(parents=True, exist_ok=True)
        file = target / "Corporate/Certificate of Incorporation.pdf"
        file.write_bytes(make_pdf([
            "Modern Slavery and Human Trafficking Statement 2026",
            f"{company} - statement under section 54 of the Modern Slavery Act 2015",
            "Approved by the board on 2026-06-30"]))
        return ("a modern slavery statement saved over the certificate of "
                "incorporation file")
    if action == "rename":
        old, new = ("Certificates/Cyber Essentials Plus.pdf",
                    "Certificates/CE+ certificate (current).pdf")
        source = target / old
        if source.exists():
            source.rename(target / new)
            ids = _ids(target)
            if old in ids:
                ids[new] = ids.pop(old)
                _save_ids(target, ids)
        return "Cyber Essentials file renamed"
    return "Nothing changed"


def reset(connection_id: str) -> None:
    shutil.rmtree(folder(connection_id), ignore_errors=True)


class DemoDrive(Provider):
    key = "demo"
    configured = True

    def account(self, token):
        return "demo@drive.example"

    def folders(self, token, parent):
        return [{"id": "demo", "name": "Compliance documents (demo)"}]

    def walk(self, token, folder_id):
        base = folder(folder_id)
        ids = _ids(base)
        present = set()
        files = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name == MANIFEST:
                continue
            data = path.read_bytes()
            relative = str(path.relative_to(base))
            present.add(relative)
            if relative not in ids:
                ids[relative] = f"demo-{uuid.uuid4().hex[:16]}"
            identifier = ids[relative]
            files.append(RemoteFile(
                id=identifier, name=path.name, path=relative,
                mime_type="application/pdf" if path.suffix == ".pdf" else "text/plain",
                revision=hashlib.sha1(data).hexdigest()[:16],
                modified_at=datetime.utcfromtimestamp(int(path.stat().st_mtime)),
                size=len(data), extra={"local": str(path)}))
        # Forget deleted files so their ids are never handed out again.
        _save_ids(base, {k: v for k, v in ids.items() if k in present})
        return files

    def download(self, token, item):
        return Path(item.extra["local"]).read_bytes()


# Module-qualified on purpose: the single-file build rewrites
# `cloudsync.name` but not a bare imported function name.
cloudsync.register_provider("demo", DemoDrive())
