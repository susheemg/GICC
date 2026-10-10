"""Trust Index - GICC-05 F22.

Deterministic arithmetic, not a model. A buyer citing this in a regulatory
register needs it reproducible six months later against archived data, and
every point traceable to a document version.

What it measures: coverage and evidence quality. Not whether the supplier is a
good company - a firm with three impeccable certificates and nothing else
scores low, correctly.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..config import TRUST_ALGORITHM_VERSION
from ..models import (Document, DocumentType, DocumentVersion, Organisation,
                      RequirementProfile, Tenant, TrustDomainScore, TrustScore, now)
from . import assets, audit
from .documents import ASSURANCE_FACTOR

# domain -> (label, weight, catalogue prefixes, explicit type codes)
DOMAINS: dict[str, tuple[str, float, tuple[str, ...], tuple[str, ...]]] = {
    "identity": ("Corporate identity", 0.15, ("LEG-", "OWN-"), ()),
    "regulatory": ("Regulatory & licensing", 0.15, ("LIC-", "TAX-"), ()),
    "financial": ("Financial standing", 0.10, ("FIN-",), ()),
    "insurance": ("Insurance", 0.10, ("INS-",), ()),
    "security": ("Security & privacy", 0.20, ("SEC-",), ()),
    "resilience": ("Resilience", 0.10, (), ("QMS-005", "SEC-020", "SEC-021",
                                            "SEC-022", "COM-005")),
    "safety": ("Safety & environment", 0.10, ("HSE-", "ESG-"), ()),
    "delivery": ("People & delivery", 0.10, ("PPL-", "COM-", "TEC-", "QMS-"), ()),
}

# When no requirement profile applies, these are the types a domain is expected
# to hold. Deliberately short: marking a supplier down for a certificate its
# sector never issues would make the score meaningless.
DEFAULT_EXPECTED = {
    "identity": ["LEG-001", "LEG-002", "OWN-001", "OWN-009"],
    "regulatory": ["TAX-001", "TAX-004"],
    "financial": ["FIN-001"],
    "insurance": ["INS-001", "INS-002", "INS-003"],
    "security": ["SEC-001", "SEC-026", "SEC-027", "SEC-030"],
    "resilience": ["SEC-020", "COM-005"],
    "safety": ["HSE-001", "ESG-008"],
    "delivery": ["COM-001", "PPL-006"],
}

FRESHNESS = {"valid": 1.0, "expiring_soon": 0.7, "expired": 0.0,
             "suspended": 0.0, "withdrawn": 0.0, "missing": 0.0}

BANDS = [(80, "Strong"), (60, "Adequate"), (40, "Developing"), (0, "Weak")]


def band_for(score: float) -> str:
    for threshold, label in BANDS:
        if score >= threshold:
            return label
    return "Weak"


def band_tone(band: str) -> str:
    return {"Strong": "ok", "Adequate": "ok", "Developing": "warn", "Weak": "bad"}.get(
        band, "neutral")


def _expected_types(db: Session, tenant: Tenant, held_codes: set[str]) -> dict[str, list[str]]:
    """What this supplier is expected to hold, per domain:

      * a baseline every business should have,
      * plus whatever requirement profiles matching its sector demand,
      * plus whatever it already holds - because an expired or unverified
        document you do hold is a real weakness, not a free pass.

    Unioning every published profile regardless of sector would make a freight
    forwarder responsible for a medical devices QMS, and the score meaningless.
    """
    org = db.get(Organisation, tenant.organisation_id)
    sector_text = f"{org.sector_code or ''} {org.description or ''}".lower()

    demanded: set[str] = set()
    for profile in db.scalars(select(RequirementProfile)).all():
        sector = (profile.sector or "").lower()
        keywords = [w for w in sector.replace("&", " ").split() if len(w) > 3]
        if keywords and any(word in sector_text for word in keywords):
            demanded.update(item.type_code for item in profile.items if item.mandatory)

    expected: dict[str, list[str]] = {}
    for key, (_label, _weight, prefixes, codes) in DOMAINS.items():
        def in_domain(code: str) -> bool:
            return (bool(prefixes) and code.startswith(prefixes)) or code in codes

        combined = set(DEFAULT_EXPECTED.get(key, []))
        combined |= {c for c in demanded if in_domain(c)}
        combined |= {c for c in held_codes if in_domain(c)}
        expected[key] = sorted(combined)
    return expected


def compute(db: Session, tenant: Tenant, *, actor=None, persist: bool = True) -> TrustScore:
    from . import authenticity

    org = db.get(Organisation, tenant.organisation_id)
    # K1 - tenant-scoped proposed types are excluded from domain scoring until
    # an operator promotes them, or one tenant's local naming would move a
    # score that buyers compare across tenants.
    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id,
                    Document.deleted_at.is_(None),
                    DocumentType.scope == "global"))).all()
    held: dict[str, tuple] = {}
    for document, dtype in rows:
        if dtype.code not in held:
            held[dtype.code] = (document, dtype)
    expected_map = _expected_types(db, tenant, set(held))
    catalogue = {t.code: t for t in db.scalars(select(DocumentType)).all()}

    fingerprint_parts: list[str] = []
    domain_rows: list[TrustDomainScore] = []
    composite = 0.0

    for key, (label, weight, prefixes, codes) in DOMAINS.items():
        expected = expected_map.get(key, [])
        numerator = denominator = 0.0
        detail = []
        held_count = verified_count = impaired_count = 0

        for code in expected:
            dtype = catalogue.get(code)
            type_weight = float(dtype.criticality if dtype else 3)
            denominator += type_weight
            entry = held.get(code)
            if entry is None:
                detail.append({"code": code, "name": dtype.name if dtype else code,
                               "state": "missing", "contribution": 0.0})
                continue

            document, entry_type = entry
            if entry_type.multi_instance:
                resolution = assets.resolve(db, tenant.id, code)
                if resolution["worst"] is not None:
                    document = resolution["worst"]
            version = (db.get(DocumentVersion, document.active_version_id)
                       if document.active_version_id else None)
            if version is None:
                detail.append({"code": code, "name": document.title,
                               "state": "no version", "contribution": 0.0})
                continue

            held_count += 1
            assurance = ASSURANCE_FACTOR.get(version.assurance_level, 0.4)
            freshness = FRESHNESS.get(document.derived_status, 0.0)
            if version.assurance_level in {"L3", "L4"}:
                verified_count += 1
            if freshness == 0.0:
                impaired_count += 1
            contribution = type_weight * assurance * freshness
            numerator += contribution
            fingerprint_parts.append(f"{version.id}:{version.assurance_level}:"
                                     f"{document.derived_status}")
            detail.append({"code": code, "name": document.title,
                           "state": document.derived_status,
                           "assurance": version.assurance_level,
                           "version_id": version.id,
                           "contribution": round(contribution / max(denominator, 1) * 100, 1)})

        score = round((numerator / denominator) * 100, 1) if denominator else 0.0
        composite += weight * score
        domain_rows.append(TrustDomainScore(
            domain=key, label=label, score=score, weight=weight,
            expected=len(expected), held=held_count, verified=verified_count,
            impaired=impaired_count, detail_json=json.dumps(detail, default=str)))

    modifiers = []
    if org.verification_state in {"registry_verified", "lei_bound"}:
        composite += 3
        modifiers.append({"name": "Registry verified entity", "points": 3})
    if org.lei and org.verification_state == "lei_bound":
        composite += 2
        modifiers.append({"name": "LEI bound", "points": 2})

    signals = authenticity.high_count(db, tenant.id)
    if signals:
        penalty = min(15, signals * 5)
        composite -= penalty
        modifiers.append({"name": f"{signals} unresolved high authenticity signal(s)",
                          "points": -penalty})

    composite = round(max(0.0, min(100.0, composite)), 1)
    withheld = None
    if tenant.lifecycle_state == "suspended":
        withheld = "Score withheld while this account is under review"

    fingerprint = hashlib.sha256("|".join(sorted(fingerprint_parts)).encode()).hexdigest()
    previous = latest(db, tenant.id)

    score_row = TrustScore(
        tenant_id=tenant.id, composite=composite, band=band_for(composite),
        algorithm_version=TRUST_ALGORITHM_VERSION, evidence_fingerprint=fingerprint,
        modifiers_json=json.dumps(modifiers), withheld_reason=withheld,
        published=previous.published if previous else False)
    score_row.domains = domain_rows

    if persist:
        # Only keep a new row when something actually moved, so the history is a
        # trend line rather than a log of recomputations.
        if previous and previous.evidence_fingerprint == fingerprint \
                and abs(previous.composite - composite) < 0.05:
            return previous
        db.add(score_row)
        db.flush()
        audit.record(db, tenant_id=tenant.id, action="trust.computed",
                     actor_kind="system" if actor is None else "user",
                     actor_id=getattr(actor, "id", None),
                     actor_label=getattr(actor, "email", "trust-engine"),
                     object_type="trust_score", object_id=score_row.id,
                     payload={"composite": composite, "band": score_row.band,
                              "algorithm": TRUST_ALGORITHM_VERSION})
        if previous and previous.band != score_row.band:
            from . import notifications
            notifications.on_trust_band_change(db, tenant, previous.band,
                                               score_row.band, composite)
    return score_row


def latest(db: Session, tenant_id: str) -> TrustScore | None:
    return db.scalar(select(TrustScore).where(TrustScore.tenant_id == tenant_id)
                     .order_by(desc(TrustScore.computed_at)).limit(1))


def history(db: Session, tenant_id: str, limit: int = 12) -> list[TrustScore]:
    rows = db.scalars(select(TrustScore).where(TrustScore.tenant_id == tenant_id)
                      .order_by(desc(TrustScore.computed_at)).limit(limit)).all()
    return list(reversed(rows))


def next_best_action(db: Session, tenant: Tenant, score: TrustScore) -> dict | None:
    """The single change that raises the composite most - F22.3. One action, not
    a list: a ranked backlog is a way of doing nothing."""
    best = None
    for domain in score.domains:
        try:
            detail = json.loads(domain.detail_json or "[]")
        except ValueError:
            continue
        for entry in detail:
            state = entry.get("state")
            if state in {"missing", "no version"}:
                gain = domain.weight * (100 / max(domain.expected, 1))
                action = ("add", f"Add {entry['name']}", entry["code"])
            elif state in {"expired", "suspended"}:
                gain = domain.weight * (100 / max(domain.expected, 1))
                action = ("renew", f"Replace {entry['name']} — it is {state}", entry["code"])
            elif entry.get("assurance") in {"L0", "L1"}:
                gain = domain.weight * (100 / max(domain.expected, 1)) * 0.4
                action = ("verify", f"Get {entry['name']} verified — it is "
                                    f"{entry.get('assurance')}", entry["code"])
            else:
                continue
            if best is None or gain > best["gain"]:
                best = {"gain": round(gain, 1), "kind": action[0], "text": action[1],
                        "type_code": action[2], "domain": domain.label}
    return best
