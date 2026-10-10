"""Document and version lifecycle - GICC-01 F4/F5, GICC-04 P-12 to P-15, P-31.

The immutability guard lives here because SQLite has no row-level triggers we
can rely on across engines. Everything that writes to document_version must go
through these functions.
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..config import EXPIRING_SOON_DAYS, MAX_UPLOAD_BYTES, REMINDER_LADDER, UPLOAD_DIR
from ..models import Document, DocumentType, DocumentVersion, Task, Tenant, now
from . import audit

MUTABLE_AFTER_PUBLISH = {
    "status", "assurance_level", "superseded_at", "published_at", "updated_at",
}

ASSURANCE_ORDER = ["L0", "L1", "L2", "L3", "L4"]
ASSURANCE_FACTOR = {"L0": 0.40, "L1": 0.60, "L2": 0.80, "L3": 0.95, "L4": 1.00}


def assurance_at_least(actual: str, required: str) -> bool:
    return ASSURANCE_ORDER.index(actual) >= ASSURANCE_ORDER.index(required)


# --------------------------------------------------------------------------
# File handling
# --------------------------------------------------------------------------
def store_upload(tenant_id: str, filename: str, data: bytes) -> tuple[str, str]:
    """Writes the blob and returns (relative_path, sha256). Content-addressed,
    so re-uploading identical bytes never duplicates storage."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(filename).suffix.lower()[:10]
    tenant_dir = UPLOAD_DIR / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    target = tenant_dir / f"{digest}{suffix}"
    if not target.exists():
        target.write_bytes(data)
    return str(target.relative_to(UPLOAD_DIR)), digest


def absolute_path(rel_path: str) -> Path:
    return UPLOAD_DIR / rel_path


# --------------------------------------------------------------------------
# Version lifecycle
# --------------------------------------------------------------------------
def next_version_no(db: Session, document_id: str) -> int:
    highest = db.scalar(
        select(DocumentVersion.version_no)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no.desc()).limit(1))
    return (highest or 0) + 1


def create_version(db: Session, *, document: Document, actor, **fields) -> DocumentVersion:
    version = DocumentVersion(
        tenant_id=document.tenant_id,
        document_id=document.id,
        version_no=next_version_no(db, document.id),
        status="draft",
        **fields)
    if version.version_no > 1 and not version.change_reason:
        version.change_reason = "Replacement issued"
    db.add(version)
    db.flush()
    audit.record(db, tenant_id=document.tenant_id, action="version.staged",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="document_version", object_id=version.id,
                 payload={"document": document.title, "version": version.version_no})
    return version


def publish_version(db: Session, version: DocumentVersion, actor) -> DocumentVersion:
    """Publish, or schedule if effective_from is in the future. Supersession of
    the prior active version happens in the same transaction."""
    if version.status in {"active", "superseded", "withdrawn"}:
        raise HTTPException(400, "This version has already been published")

    document = db.get(Document, version.document_id)
    if version.effective_from and version.effective_from > date.today():
        version.status = "scheduled"
        db.flush()
        audit.record(db, tenant_id=version.tenant_id, action="version.scheduled",
                     actor_id=getattr(actor, "id", None),
                     actor_label=getattr(actor, "email", "system"),
                     object_type="document_version", object_id=version.id,
                     payload={"effective_from": version.effective_from})
        return version

    return _activate(db, version, document, actor)


def _activate(db: Session, version: DocumentVersion, document: Document, actor) -> DocumentVersion:
    prior = db.scalar(
        select(DocumentVersion).where(
            and_(DocumentVersion.document_id == document.id,
                 DocumentVersion.status == "active")))
    if prior and prior.id != version.id:
        prior.status = "superseded"
        prior.superseded_at = now()
        version.supersedes_version_id = prior.id
        db.flush()
        audit.record(db, tenant_id=version.tenant_id, action="version.superseded",
                     actor_id=getattr(actor, "id", None),
                     actor_label=getattr(actor, "email", "system"),
                     object_type="document_version", object_id=prior.id,
                     payload={"superseded_by": version.id})

    version.status = "active"
    version.published_at = now()
    document.active_version_id = version.id
    db.flush()
    refresh_document_status(db, document)
    audit.record(db, tenant_id=version.tenant_id, action="version.published",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="document_version", object_id=version.id,
                 payload={"document": document.title, "version": version.version_no,
                          "expiry": version.expiry_date})
    return version


def withdraw_version(db: Session, version: DocumentVersion, actor, reason: str) -> None:
    if version.status not in {"active", "scheduled", "draft"}:
        raise HTTPException(400, "Only a draft, scheduled or active version can be withdrawn")
    version.status = "withdrawn"
    document = db.get(Document, version.document_id)
    if document.active_version_id == version.id:
        document.active_version_id = None
    db.flush()
    refresh_document_status(db, document)
    audit.record(db, tenant_id=version.tenant_id, action="version.withdrawn",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="document_version", object_id=version.id,
                 payload={"reason": reason})


def rollback_to(db: Session, version: DocumentVersion, actor) -> DocumentVersion:
    """Rollback is forward-only: a new version is created that reuses the old
    file, so the history stays append-only (GICC-01 F5.6)."""
    if version.status != "superseded":
        raise HTTPException(400, "Only a superseded version can be restored")
    document = db.get(Document, version.document_id)
    clone = create_version(
        db, document=document, actor=actor,
        issue_date=version.issue_date,
        effective_from=date.today(),
        expiry_date=version.expiry_date,
        certificate_number=version.certificate_number,
        issuer_name=version.issuer_name,
        holder_name=version.holder_name,
        scope_statement=version.scope_statement,
        cover_amount=version.cover_amount,
        currency=version.currency,
        verification_url=version.verification_url,
        content_hash=version.content_hash,
        file_path=version.file_path,
        file_name=version.file_name,
        file_size=version.file_size,
        mime_type=version.mime_type,
        change_reason=f"Restored from version {version.version_no}")
    return _activate(db, clone, document, actor)


# --------------------------------------------------------------------------
# Status derivation - GICC-04 §7.1
# --------------------------------------------------------------------------
def derive_status(version: DocumentVersion | None, suspended: bool = False) -> str:
    if version is None:
        return "missing"
    if version.status == "withdrawn":
        return "withdrawn"
    if suspended:
        return "suspended"
    if version.expiry_date:
        today = date.today()
        if version.expiry_date < today:
            return "expired"
        if (version.expiry_date - today).days <= EXPIRING_SOON_DAYS:
            return "expiring_soon"
    return "valid"


def refresh_document_status(db: Session, document: Document) -> str:
    active = None
    if document.active_version_id:
        active = db.get(DocumentVersion, document.active_version_id)
        if active and active.status != "active":
            active = None
    suspended = bool(active and active.assurance_level == "L1"
                     and _last_result(db, active) in {"suspended", "withdrawn"})
    document.derived_status = derive_status(active, suspended)
    db.flush()
    return document.derived_status


def _last_result(db: Session, version: DocumentVersion) -> str | None:
    from ..models import VerificationCheck
    check = db.scalar(
        select(VerificationCheck)
        .where(VerificationCheck.document_version_id == version.id)
        .order_by(VerificationCheck.checked_at.desc()).limit(1))
    return check.result if check else None


# --------------------------------------------------------------------------
# Maintenance sweep - GICC-04 P-31 / P-15 scheduled activation
# --------------------------------------------------------------------------
def run_maintenance(db: Session, tenant: Tenant) -> dict:
    """Activates due scheduled versions, refreshes statuses and raises renewal
    tasks on the reminder ladder. Idempotent - safe to run repeatedly."""
    activated = expired = tasks_created = 0
    today = date.today()

    due = db.scalars(
        select(DocumentVersion).where(
            and_(DocumentVersion.tenant_id == tenant.id,
                 DocumentVersion.status == "scheduled",
                 DocumentVersion.effective_from <= today))).all()
    for version in due:
        document = db.get(Document, version.document_id)
        _activate(db, version, document, actor=None)
        activated += 1

    documents = db.scalars(
        select(Document).where(and_(Document.tenant_id == tenant.id,
                                    Document.deleted_at.is_(None)))).all()
    for document in documents:
        before = document.derived_status
        after = refresh_document_status(db, document)
        if before != after and after == "expired":
            expired += 1
            audit.record(db, tenant_id=tenant.id, action="version.expired",
                         actor_kind="system", actor_label="maintenance",
                         object_type="document", object_id=document.id,
                         payload={"title": document.title})
        if after in {"expiring_soon", "expired", "missing"}:
            if _ensure_task(db, tenant, document, after):
                tasks_created += 1

    tenant.readiness_score = readiness_score(db, tenant)
    db.commit()
    return {"activated": activated, "newly_expired": expired,
            "tasks_created": tasks_created, "readiness": round(tenant.readiness_score, 1)}


def _ensure_task(db: Session, tenant: Tenant, document: Document, status: str) -> bool:
    existing = db.scalar(
        select(Task).where(and_(Task.tenant_id == tenant.id,
                                Task.document_id == document.id,
                                Task.status == "open")))
    if existing:
        return False
    active = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
    due = active.expiry_date if active and active.expiry_date else date.today()
    days_left = (due - date.today()).days if due else 0
    severity = "high" if status == "expired" or days_left <= 30 else "medium"
    band = next((d for d in REMINDER_LADDER if days_left >= d), 0)
    title = (f"{document.title} has expired" if status == "expired"
             else f"Renew {document.title} - {days_left} days left (T-{band} reminder)")
    db.add(Task(tenant_id=tenant.id, kind="renewal", title=title,
                document_id=document.id, due_at=due, severity=severity))
    return True


# --------------------------------------------------------------------------
# Readiness score - GICC-04 §7.2
# --------------------------------------------------------------------------
def readiness_score(db: Session, tenant: Tenant) -> float:
    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()
    if not rows:
        return 0.0
    numerator = denominator = 0.0
    for document, dtype in rows:
        weight = dtype.criticality or 3
        denominator += weight
        active = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        if active is None:
            continue
        freshness = {"valid": 1.0, "expiring_soon": 0.7}.get(document.derived_status, 0.0)
        numerator += weight * ASSURANCE_FACTOR.get(active.assurance_level, 0.4) * freshness
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def health_score(db: Session, tenant: Tenant) -> tuple[float, list[str]]:
    """Churn-signal model - GICC-04 §12.1, simplified to the signals this
    application actually observes."""
    from ..models import AccessGrant, ShareLinkView
    signals, score = [], 0.0
    cutoff = now() - timedelta(days=30)

    reads = db.scalar(select(ShareLinkView.id).where(
        and_(ShareLinkView.tenant_id == tenant.id, ShareLinkView.viewed_at >= cutoff)).limit(1))
    reads_count = len(db.scalars(select(ShareLinkView.id).where(
        and_(ShareLinkView.tenant_id == tenant.id, ShareLinkView.viewed_at >= cutoff))).all())
    score += 25 if reads_count >= 5 else (12 if reads else 0)
    if not reads:
        signals.append("No client has opened a shared document in 30 days")

    grants = len(db.scalars(select(AccessGrant.id).where(
        and_(AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all())
    score += 20 if grants >= 3 else (10 if grants else 0)
    if grants == 0:
        signals.append("No active client grants - nobody is relying on this profile yet")

    bad = len(db.scalars(select(Document.id).where(
        and_(Document.tenant_id == tenant.id,
             Document.derived_status.in_(["expired", "suspended"])))).all())
    score += 15 if bad == 0 else (7 if bad <= 2 else 0)
    if bad:
        signals.append(f"{bad} document(s) expired or suspended")

    score += 15  # login signal - the session that triggered this call
    verified = len(db.scalars(select(DocumentVersion.id).where(
        and_(DocumentVersion.tenant_id == tenant.id, DocumentVersion.status == "active",
             DocumentVersion.assurance_level.in_(["L3", "L4"])))).all())
    total = len(db.scalars(select(DocumentVersion.id).where(
        and_(DocumentVersion.tenant_id == tenant.id, DocumentVersion.status == "active"))).all())
    ratio = verified / total if total else 0
    score += 10 if ratio >= 0.6 else (5 if ratio >= 0.3 else 0)
    if ratio < 0.3:
        signals.append("Fewer than 30% of live documents are machine-verified (L3+)")

    overdue = len(db.scalars(select(Task.id).where(
        and_(Task.tenant_id == tenant.id, Task.status == "open",
             Task.due_at < date.today()))).all())
    score += 10 if overdue == 0 else (5 if overdue <= 2 else 0)
    if overdue:
        signals.append(f"{overdue} renewal task(s) overdue")

    score += 5  # payment state - always current in this build
    return round(score, 1), signals


def health_band(score: float) -> tuple[str, str]:
    if score >= 70:
        return "Healthy", "ok"
    if score >= 40:
        return "At risk", "warn"
    if score >= 15:
        return "Declining", "warn"
    return "Critical", "bad"
