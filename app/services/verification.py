"""Verification plane - GICC-01 F6, GICC-04 P-16 to P-21.

Connector adapters are pluggable. The adapters shipped here are *simulated*
except for link liveness: they apply the same matching logic a live adapter
would (holder name, certificate number, status) against locally held data, and
every check they write is flagged simulated=True so nothing in the UI or the
API can present a simulated result as a real registry response.

To go live, implement a real adapter and register it in ADAPTERS; the rest of
the pipeline - scheduling, assurance computation, downgrade rules, audit -
is unchanged.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import LIVE_LINK_CHECKS
from ..models import Document, DocumentVersion, Organisation, Tenant, VerificationCheck, now
from . import audit
from .documents import refresh_document_status

# Route -> (source label, method, recheck interval in days)
ROUTES = {
    "registry_api": ("Company registry / GLEIF / VIES", "registry_api", 30),
    "certsearch": ("IAF CertSearch", "certsearch_api", 14),
    "issuer_portal": ("Issuing authority portal", "link_liveness", 30),
    "issuer_callback": ("Issuer confirmation", "issuer_callback", 60),
    "manual": ("Manual review", "manual_review", 180),
    "cryptographic": ("Signature / credential validation", "signature_validation", 1),
}

RESULT_LABELS = {
    "match": "Verified against source",
    "partial_match": "Partial match - some fields differ",
    "no_match": "No matching record found",
    "not_found": "Not found at source",
    "suspended": "Certificate suspended at source",
    "withdrawn": "Certificate withdrawn at source",
    "expired": "Expired at source",
    "unreachable": "Source unreachable",
    "error": "Check could not be completed",
    "link_ok": "Authentication link resolves",
    "link_dead": "Authentication link does not resolve",
}

NEGATIVE_RESULTS = {"no_match", "not_found", "suspended", "withdrawn", "expired", "link_dead"}
INCONCLUSIVE = {"unreachable", "error"}


def _normalise(name: str | None) -> str:
    if not name:
        return ""
    text = name.lower()
    for token in [" limited", " ltd", " plc", " llp", " inc", " gmbh", " bv", " pte",
                  " llc", ".", ",", "(", ")", "&"]:
        text = text.replace(token, " ")
    return re.sub(r"\s+", " ", text).strip()


def name_match(holder: str | None, legal_name: str | None) -> str:
    a, b = _normalise(holder), _normalise(legal_name)
    if not a or not b:
        return "unknown"
    if a == b:
        return "exact"
    if a in b or b in a:
        return "partial"
    return "mismatch"


def _check_link(url: str) -> tuple[str, str]:
    """Real outbound check, off unless LIVE_LINK_CHECKS=1."""
    if not url:
        return "no_match", "No authentication link recorded"
    if not url.lower().startswith(("http://", "https://")):
        return "link_dead", "Link is not a resolvable http(s) URL"
    if not LIVE_LINK_CHECKS:
        return "link_ok", ("Link format accepted. Live fetching is disabled "
                           "(set LIVE_LINK_CHECKS=1 to fetch the URL).")
    try:
        import httpx
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code < 400:
            return "link_ok", f"Source responded {response.status_code}"
        return "link_dead", f"Source responded {response.status_code}"
    except Exception as exc:  # network is best-effort; never fail the request
        return "unreachable", f"Could not reach the source: {type(exc).__name__}"


def run_check(db: Session, version: DocumentVersion, actor=None,
              method_override: str | None = None) -> VerificationCheck:
    document = db.get(Document, version.document_id)
    dtype = document.document_type
    tenant = db.get(Tenant, version.tenant_id)
    org = db.get(Organisation, tenant.organisation_id)

    route = method_override or dtype.verification_route
    source_label, method, interval = ROUTES.get(route, ROUTES["manual"])

    matched: dict[str, object] = {}
    simulated = True

    if method == "link_liveness" or (route in {"issuer_portal", "manual"} and version.verification_url):
        result, detail = _check_link(version.verification_url or "")
        simulated = not LIVE_LINK_CHECKS
        method = "link_liveness"
        source_label = "Authentication link"
    elif method in {"registry_api", "certsearch_api", "issuer_callback", "signature_validation"}:
        result, detail, matched = _simulated_source_check(version, org, dtype)
    else:
        result, detail = ("no_match", "Manual review has not been completed")
        method = "manual_review"

    version.name_match = name_match(version.holder_name or org.legal_name, org.legal_name)
    new_level = compute_assurance(db, version, result, method)

    check = VerificationCheck(
        tenant_id=version.tenant_id, document_version_id=version.id,
        method=method, source_code=source_label, result=result,
        resulting_assurance=new_level,
        matched_fields=json.dumps(matched) if matched else None,
        detail=detail, simulated=simulated,
        next_check_due=now() + timedelta(days=interval),
        checked_by=getattr(actor, "email", "system"))
    db.add(check)

    previous = version.assurance_level
    version.assurance_level = new_level
    db.flush()
    refresh_document_status(db, document)

    audit.record(db, tenant_id=version.tenant_id, action="verification.completed",
                 actor_kind="system" if actor is None else "user",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "verification-worker"),
                 object_type="document_version", object_id=version.id,
                 payload={"result": result, "method": method, "assurance": new_level,
                          "previous_assurance": previous, "simulated": simulated})
    if previous != new_level:
        audit.record(db, tenant_id=version.tenant_id, action="assurance.changed",
                     actor_kind="system", actor_label="verification-worker",
                     object_type="document_version", object_id=version.id,
                     payload={"from": previous, "to": new_level})
    return check


def _simulated_source_check(version: DocumentVersion, org: Organisation,
                            dtype) -> tuple[str, str, dict]:
    """Applies the matching rules a live connector would apply, against the data
    held locally. Deterministic, explainable, and always flagged as simulated."""
    matched = {}
    reasons = []

    has_number = bool(version.certificate_number and version.certificate_number.strip())
    matched["certificate_number"] = has_number
    if not has_number:
        reasons.append("no certificate or registration number recorded")

    holder = name_match(version.holder_name or org.legal_name, org.legal_name)
    matched["holder"] = holder
    if holder == "mismatch":
        reasons.append("holder name does not match the registered legal entity")

    has_issuer = bool(version.issuer_name and version.issuer_name.strip())
    matched["issuer"] = has_issuer
    if not has_issuer:
        reasons.append("no issuing authority recorded")

    if version.expiry_date and version.expiry_date < date.today():
        matched["status"] = "expired"
        return "expired", "Source record shows the certificate has expired.", matched

    if not has_number or holder == "mismatch" or not has_issuer:
        return "no_match", ("Simulated source lookup could not confirm the record: "
                            + "; ".join(reasons) + "."), matched

    matched["status"] = "valid"
    return "match", (f"Simulated {dtype.verification_route} lookup confirmed holder, "
                     "number and status. Replace with a live adapter before relying "
                     "on this in production."), matched


def compute_assurance(db: Session, version: DocumentVersion, result: str, method: str) -> str:
    """The one rule that matters: an inconclusive check never downgrades.
    Only a completed negative result, or the absence of any evidence, does.
    (GICC-04 §7 - prevents a connector outage mass-downgrading the platform.)"""
    current = version.assurance_level or "L0"

    if result in INCONCLUSIVE:
        return current  # stale, not downgraded

    if result in NEGATIVE_RESULTS:
        return "L1" if version.verification_url else "L0"

    if method == "signature_validation" and result == "match":
        return "L4"
    if method in {"registry_api", "certsearch_api", "issuer_callback"} and result == "match":
        return "L3"
    if method == "manual_review" and result == "match":
        return "L2"
    if result in {"link_ok", "partial_match"}:
        return "L2" if current in {"L2", "L3", "L4"} and result == "partial_match" else "L1"
    return "L0"


def manual_decision(db: Session, version: DocumentVersion, actor, outcome: str,
                    notes: str) -> VerificationCheck:
    """Verification Officer decision - GICC-04 P-19. A human may reach L2 but
    never L3 or L4; those levels are machine-derived only."""
    result = "match" if outcome == "confirm" else "no_match"
    level = "L2" if outcome == "confirm" else ("L1" if version.verification_url else "L0")
    check = VerificationCheck(
        tenant_id=version.tenant_id, document_version_id=version.id,
        method="manual_review", source_code="Verification officer",
        result=result, resulting_assurance=level, detail=notes, simulated=False,
        next_check_due=now() + timedelta(days=180),
        checked_by=getattr(actor, "email", "officer"))
    db.add(check)
    previous = version.assurance_level
    version.assurance_level = level
    db.flush()
    refresh_document_status(db, db.get(Document, version.document_id))
    audit.record(db, tenant_id=version.tenant_id, action="verification.manual_decision",
                 actor_id=getattr(actor, "id", None), actor_label=getattr(actor, "email", None),
                 object_type="document_version", object_id=version.id,
                 payload={"outcome": outcome, "from": previous, "to": level, "notes": notes})
    return check


def due_for_recheck(db: Session, tenant_id: str) -> list[DocumentVersion]:
    versions = db.scalars(
        select(DocumentVersion).where(DocumentVersion.tenant_id == tenant_id,
                                      DocumentVersion.status == "active")).all()
    due = []
    for version in versions:
        latest = db.scalar(
            select(VerificationCheck)
            .where(VerificationCheck.document_version_id == version.id)
            .order_by(VerificationCheck.checked_at.desc()).limit(1))
        if latest is None or (latest.next_check_due and latest.next_check_due <= now()):
            due.append(version)
    return due
