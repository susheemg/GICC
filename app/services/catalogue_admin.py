"""Type proposals and catalogue administration - GICC-07 K1.

The catalogue code is the join key for requirement profiles, the Trust Index
domain map, gap ranking and buyer matching. If unmatched uploads spawned global
types, six months of uploads would produce four hundred near-duplicates and
every one of those features would silently stop matching. Nothing would break
loudly; results would just quietly go wrong.

So an unmatched document gets a **tenant-scoped** type: immediately usable by
that tenant, excluded from cross-tenant matching, and promoted to the global
catalogue only by a named operator.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..catalogue import DOMAINS
from ..models import Document, DocumentType, DocumentTypeProposal, now
from . import audit


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _similar_key(name: str) -> str:
    """Loose enough that 'Public Liability Insurance' and 'public liability cert'
    collide, which is the whole point of the occurrence count."""
    words = [w for w in _slug(name).split()
             if w not in {"certificate", "cert", "document", "record", "the", "of",
                          "and", "for", "policy", "report", "statement"}]
    return " ".join(sorted(set(words))[:5])


def next_tenant_code(db: Session, tenant_id: str, domain: str) -> str:
    """Tenant codes are visibly local: TEN-<domain>-nnn. Nobody should mistake
    one for a catalogue entry."""
    existing = db.scalar(select(func.count(DocumentType.id)).where(and_(
        DocumentType.owner_tenant_id == tenant_id,
        DocumentType.scope == "tenant"))) or 0
    return f"TEN-{domain}-{existing + 1:03d}"


def propose(db: Session, *, tenant_id: str, name: str, domain: str | None,
            nearest_code: str | None, similarity: float, rationale: str,
            source: str, sample_document_id: str | None = None) -> tuple[
                DocumentTypeProposal, DocumentType]:
    """Creates the proposal and a tenant-scoped type so the document can be
    filed immediately. The tenant is never blocked waiting for a review."""
    domain = (domain or "COM").upper()[:3]
    if domain not in DOMAINS:
        domain = "COM"

    key = _similar_key(name)
    sibling = db.scalar(select(DocumentTypeProposal).where(and_(
        DocumentTypeProposal.status == "proposed",
        DocumentTypeProposal.proposed_domain == domain)))
    occurrences = 1
    for candidate in db.scalars(select(DocumentTypeProposal).where(
            DocumentTypeProposal.status == "proposed")).all():
        if _similar_key(candidate.proposed_name) == key:
            occurrences = candidate.occurrences + 1
            candidate.occurrences = occurrences

    code = next_tenant_code(db, tenant_id, domain)
    dtype = DocumentType(
        code=code, domain_code=domain, domain_name=DOMAINS[domain],
        name=name[:160], typical_issuer="Not yet classified",
        validity_label="Unknown", validity_months=None,
        verification_route="manual", sensitivity="R", criticality=3,
        verification_required=False, is_active=True,
        scope="tenant", owner_tenant_id=tenant_id)
    db.add(dtype)
    db.flush()

    proposal = DocumentTypeProposal(
        tenant_id=tenant_id, proposed_name=name[:160], proposed_domain=domain,
        nearest_type_code=nearest_code, similarity=similarity,
        rationale=rationale[:1000] if rationale else None, source=source,
        sample_document_id=sample_document_id, created_type_code=code,
        occurrences=occurrences, status="proposed")
    db.add(proposal)
    dtype.proposal_id = proposal.id
    db.flush()

    audit.record(db, tenant_id=tenant_id, action="document_type.proposed",
                 actor_kind="system", actor_label="classification",
                 object_type="document_type_proposal", object_id=proposal.id,
                 payload={"name": name, "code": code, "domain": domain,
                          "nearest": nearest_code, "source": source})
    return proposal, dtype


def queue(db: Session, status: str = "proposed") -> list[DocumentTypeProposal]:
    """Ranked by occurrences: five tenants independently proposing the same
    thing is the signal that the catalogue is genuinely missing something."""
    return list(db.scalars(select(DocumentTypeProposal)
                           .where(DocumentTypeProposal.status == status)
                           .order_by(DocumentTypeProposal.occurrences.desc(),
                                     DocumentTypeProposal.created_at)).all())


def next_global_code(db: Session, domain: str) -> str:
    highest = 0
    for code in db.scalars(select(DocumentType.code).where(and_(
            DocumentType.domain_code == domain,
            DocumentType.scope == "global"))).all():
        match = re.match(rf"^{domain}-(\d+)$", code)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{domain}-{highest + 1:03d}"


def promote(db: Session, proposal: DocumentTypeProposal, *, actor,
            name: str | None = None, domain: str | None = None,
            typical_issuer: str = "", validity_months: int | None = None,
            sensitivity: str = "R", criticality: int = 3,
            verification_route: str = "manual", multi_instance: bool = False,
            note: str = "") -> DocumentType:
    """Allocates a real catalogue code and migrates every tenant document under
    the proposed type onto it, in one transaction."""
    domain = (domain or proposal.proposed_domain or "COM").upper()[:3]
    code = next_global_code(db, domain)

    dtype = DocumentType(
        code=code, domain_code=domain, domain_name=DOMAINS.get(domain, domain),
        name=(name or proposal.proposed_name)[:160],
        typical_issuer=typical_issuer or "Various",
        validity_label=f"{validity_months} months" if validity_months else "Variable",
        validity_months=validity_months, verification_route=verification_route,
        sensitivity=sensitivity, criticality=criticality,
        verification_required=verification_route != "manual",
        is_active=True, scope="global", multi_instance=multi_instance,
        proposal_id=proposal.id)
    db.add(dtype)
    db.flush()

    migrated = _migrate(db, proposal.created_type_code, dtype.id)

    proposal.status = "promoted"
    proposal.resulting_type_code = code
    proposal.reviewed_by = getattr(actor, "email", "operator")
    proposal.reviewed_at = now()
    proposal.review_note = note or None
    db.flush()

    audit.record(db, tenant_id=proposal.tenant_id, action="document_type.promoted",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="document_type", object_id=dtype.id,
                 payload={"code": code, "name": dtype.name, "migrated": migrated})
    return dtype


def merge(db: Session, proposal: DocumentTypeProposal, *, target_code: str, actor,
          note: str = "") -> int:
    target = db.scalar(select(DocumentType).where(and_(
        DocumentType.code == target_code, DocumentType.scope == "global")))
    if target is None:
        raise ValueError(f"No global catalogue type with code {target_code}")

    migrated = _migrate(db, proposal.created_type_code, target.id)
    proposal.status = "merged"
    proposal.merged_into_code = target_code
    proposal.reviewed_by = getattr(actor, "email", "operator")
    proposal.reviewed_at = now()
    proposal.review_note = note or None
    db.flush()

    audit.record(db, tenant_id=proposal.tenant_id, action="document_type.merged",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="document_type_proposal", object_id=proposal.id,
                 payload={"into": target_code, "migrated": migrated})
    return migrated


def reject(db: Session, proposal: DocumentTypeProposal, *, actor,
           note: str = "") -> None:
    """The tenant keeps their local type and loses nothing; it simply stays out
    of cross-tenant matching permanently."""
    proposal.status = "rejected"
    proposal.reviewed_by = getattr(actor, "email", "operator")
    proposal.reviewed_at = now()
    proposal.review_note = note or "Kept as a local type"
    db.flush()
    audit.record(db, tenant_id=proposal.tenant_id, action="document_type.proposal_rejected",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="document_type_proposal", object_id=proposal.id,
                 payload={"note": proposal.review_note})


def _migrate(db: Session, from_code: str | None, to_type_id: str) -> int:
    """Moves every document from the tenant-scoped type onto the target, then
    retires the local type. Versions and assurance are untouched — this changes
    what the document is called, not what it proves."""
    if not from_code:
        return 0
    source = db.scalar(select(DocumentType).where(DocumentType.code == from_code))
    if source is None:
        return 0
    documents = db.scalars(select(Document).where(
        Document.document_type_id == source.id)).all()
    target = db.get(DocumentType, to_type_id)
    for document in documents:
        document.document_type_id = to_type_id
        if document.title == source.name:
            document.title = target.name
    source.is_active = False
    db.flush()
    return len(documents)


def tenant_types(db: Session, tenant_id: str) -> list[DocumentType]:
    return list(db.scalars(select(DocumentType).where(and_(
        DocumentType.owner_tenant_id == tenant_id,
        DocumentType.scope == "tenant",
        DocumentType.is_active.is_(True))).order_by(DocumentType.code)).all())
