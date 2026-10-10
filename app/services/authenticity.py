"""Authenticity signals - GICC-05 F19.

These are observations, not findings. A signal says "this warrants a look"; it
never says a document is forged, and it never moves the assurance ladder in
either direction. Only a completed negative verification lowers assurance.

Wording rule (F19.5): describe the observation, never allege intent.
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (AuthenticitySignal, Document, DocumentType, DocumentVersion,
                      Organisation, Tenant, now)
from . import audit
from .verification import name_match

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Certificate number shapes for bodies we know. A miss is a signal, not a verdict:
# certification bodies change their formats and our list is never complete.
NUMBER_FORMATS = [
    ("BSI", re.compile(r"^(IS|FS|EMS|OHS|BCMS|MD|PIMS|TPS)\s?\d{5,7}$", re.I)),
    ("IASME", re.compile(r"^(CE|IASME)[-\s]?\d{4}[-\s]?\d{3,6}$", re.I)),
    ("Companies House", re.compile(r"^(SC|NI|OC|SO)?\d{6,8}$", re.I)),
    ("VAT", re.compile(r"^[A-Z]{2}\d{9,12}$", re.I)),
    ("LEI", re.compile(r"^[A-Z0-9]{18}\d{2}$", re.I)),
]

KNOWN_BODIES = {
    "bsi", "lrqa", "nqa", "sgs", "dnv", "tuv", "tüv", "bureau veritas", "intertek",
    "iasme", "ukas", "companies house", "hm revenue", "gleif", "ecovadis", "chas",
    "notified body", "afnor", "dekra", "kiwa", "qms", "alcumus",
}

# Type-level norms: how long a certificate of this kind normally lasts, in months.
VALIDITY_NORMS = {"QMS": (24, 42), "SEC": (12, 42), "INS": (10, 14), "HSE": (10, 38),
                  "ESG": (10, 62), "LIC": (10, 62)}


def evaluate(db: Session, version: DocumentVersion, actor=None) -> list[AuthenticitySignal]:
    """Runs every rule against one version, replacing any open signals from a
    previous run so the panel reflects the current document."""
    document = db.get(Document, version.document_id)
    dtype = document.document_type
    tenant = db.get(Tenant, version.tenant_id)
    org = db.get(Organisation, tenant.organisation_id)

    for existing in db.scalars(select(AuthenticitySignal).where(and_(
            AuthenticitySignal.document_version_id == version.id,
            AuthenticitySignal.status == "open"))).all():
        db.delete(existing)
    db.flush()

    found: list[tuple[str, str, str]] = []

    # 1. Holder name against the registered entity
    match = name_match(version.holder_name or "", org.legal_name)
    if version.holder_name and match == "mismatch":
        found.append(("holder_mismatch", "high",
                      f"The holder named on this document ({version.holder_name}) does not "
                      f"match the registered legal entity ({org.legal_name}). Buyers check "
                      "this first."))
    elif not version.holder_name and dtype.verification_required:
        found.append(("holder_missing", "low",
                      "No holder name is recorded, so the document cannot be tied to your "
                      "legal entity."))

    # 2. Certificate number shape
    if version.certificate_number:
        number = version.certificate_number.strip()
        recognised = any(pattern.match(number) for _, pattern in NUMBER_FORMATS)
        if not recognised and dtype.verification_route in {"certsearch", "registry_api"}:
            found.append(("number_format", "medium",
                          f"The number '{number}' does not match the formats used by the "
                          "certification bodies and registries we recognise. Check it "
                          "against the certificate."))
    elif dtype.verification_required:
        found.append(("number_missing", "low",
                      "No certificate or registration number is recorded, which stops this "
                      "document reaching L3."))

    # 3. Issuer recognition
    if version.issuer_name:
        issuer = version.issuer_name.lower()
        if (dtype.verification_route in {"certsearch", "registry_api"}
                and not any(body in issuer for body in KNOWN_BODIES)):
            found.append(("issuer_unknown", "medium",
                          f"'{version.issuer_name}' is not on our list of recognised bodies "
                          "for this document type. That may simply mean our list is "
                          "incomplete — confirm the body is accredited."))

    # 4. Date plausibility
    today = date.today()
    if version.issue_date and version.issue_date > today:
        found.append(("issue_future", "high",
                      f"The issue date ({version.issue_date:%d %b %Y}) is in the future."))
    if version.issue_date and version.expiry_date and version.expiry_date <= version.issue_date:
        found.append(("expiry_before_issue", "high",
                      "The expiry date is on or before the issue date."))

    # 5. Validity against scheme norms
    if version.issue_date and version.expiry_date:
        months = round((version.expiry_date - version.issue_date).days / 30.44)
        low, high = VALIDITY_NORMS.get(dtype.code[:3], (0, 0))
        if low and not (low - 1 <= months <= high + 1):
            found.append(("validity_unusual", "medium",
                          f"This runs for {months} months. Documents of this type "
                          f"normally run {low}–{high} months."))

    # 6. Missing authentication link where one is expected
    if dtype.verification_route in {"certsearch", "registry_api", "issuer_portal"} \
            and not version.verification_url:
        found.append(("no_auth_link", "low",
                      "This type can normally be checked at source, but no authentication "
                      "link is recorded."))

    # 7. Duplicate certificate number across entities - the classic tell, and
    #    the one signal no single-tenant system can see.
    if version.certificate_number:
        clash = db.execute(
            select(DocumentVersion, Tenant)
            .join(Tenant, DocumentVersion.tenant_id == Tenant.id)
            .where(and_(
                DocumentVersion.certificate_number == version.certificate_number,
                DocumentVersion.tenant_id != version.tenant_id,
                DocumentVersion.status == "active"))).first()
        if clash:
            found.append(("duplicate_number", "high",
                          "This certificate number is also held by a different legal entity "
                          "on this platform. One of the two records is wrong."))

    signals = []
    for code, severity, detail in found:
        signal = AuthenticitySignal(
            tenant_id=version.tenant_id, document_version_id=version.id,
            code=code, severity=severity, detail=detail, source="rule")
        db.add(signal)
        signals.append(signal)
    db.flush()

    high = [s for s in signals if s.severity == "high"]
    if high:
        audit.record(db, tenant_id=version.tenant_id, action="authenticity.signals_raised",
                     actor_kind="system", actor_label="authenticity",
                     object_type="document_version", object_id=version.id,
                     payload={"high": len(high), "codes": [s.code for s in signals]})
    return signals


def open_signals(db: Session, tenant_id: str) -> list[AuthenticitySignal]:
    rows = db.scalars(select(AuthenticitySignal).where(and_(
        AuthenticitySignal.tenant_id == tenant_id,
        AuthenticitySignal.status == "open"))).all()
    return sorted(rows, key=lambda s: SEVERITY_ORDER.get(s.severity, 3))


def for_version(db: Session, version_id: str) -> list[AuthenticitySignal]:
    rows = db.scalars(select(AuthenticitySignal).where(
        AuthenticitySignal.document_version_id == version_id)).all()
    return sorted(rows, key=lambda s: SEVERITY_ORDER.get(s.severity, 3))


def high_count(db: Session, tenant_id: str) -> int:
    return sum(1 for s in open_signals(db, tenant_id) if s.severity == "high")


def resolve(db: Session, signal: AuthenticitySignal, actor, outcome: str,
            note: str) -> None:
    signal.status = "confirmed" if outcome == "confirm" else "dismissed"
    signal.resolved_by = getattr(actor, "email", "system")
    signal.resolved_at = now()
    signal.resolution_note = note
    db.flush()
    audit.record(db, tenant_id=signal.tenant_id, action="authenticity.signal_resolved",
                 actor_id=getattr(actor, "id", None), actor_label=getattr(actor, "email", None),
                 object_type="authenticity_signal", object_id=signal.id,
                 payload={"code": signal.code, "outcome": signal.status, "note": note})
