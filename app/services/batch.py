"""Batch document upload - GICC-07 F28.

Up to fifty files, classified, extracted, filed as drafts. Two things govern the
design.

**Nothing publishes automatically.** Every version stays a draft until a human
confirms it. High confidence buys speed, not authority.

**Profile construction runs once, after the batch.** Running the Trust Index
fifty times would produce fifty rows of history describing an import rather
than a business.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..models import (Document, DocumentType, DocumentVersion, Job, JobItem,
                      Tenant, User, now)
from . import (ai, audit, catalogue_admin, documents as docsvc, jobs,
               onboarding, requirements as reqsvc, textextract, trust)

KIND = "batch_classify"
MAX_FILES = 50

FILE_THRESHOLD = 0.80        # filed as a draft, flagged for confirmation
PROPOSE_THRESHOLD = 0.50     # proposed with alternatives
# Below PROPOSE_THRESHOLD a document is quarantined as unclassified rather than
# guessed at. An honest "we do not know" beats a wrong filing a supplier
# discovers six months later.


def start_batch(db: Session, *, tenant: Tenant, actor: User,
                files: list[tuple[str, bytes, str]]) -> Job:
    """files: (filename, data, content_type). Stores everything first, so the
    upload is durable before any processing begins."""
    items = []
    seen: set[str] = set()
    duplicates = []

    for filename, data, content_type in files[:MAX_FILES]:
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            duplicates.append({"filename": filename, "reason": "duplicate within batch"})
            continue
        seen.add(digest)

        existing = db.scalar(select(DocumentVersion).where(and_(
            DocumentVersion.tenant_id == tenant.id,
            DocumentVersion.content_hash == digest)).limit(1))
        if existing is not None:
            duplicates.append({"filename": filename,
                               "reason": "already held in your register"})
            continue

        path, _ = docsvc.store_upload(tenant.id, filename, data)
        items.append({"ref": filename, "filename": filename, "path": path,
                      "digest": digest, "size": len(data),
                      "mime": content_type or "application/octet-stream"})

    job = jobs.create(db, kind=KIND, tenant_id=tenant.id, items=items,
                      payload={"actor_id": actor.id, "actor_email": actor.email,
                               "duplicates": duplicates},
                      created_by=actor.email)
    audit.record(db, tenant_id=tenant.id, action="batch.started", actor_id=actor.id,
                 actor_label=actor.email, object_type="job", object_id=job.id,
                 payload={"files": len(items), "duplicates": len(duplicates)})
    return job


def _classify_item(db: Session, job: Job, item: JobItem, payload: dict) -> dict:
    """One file. Runs in its own session; an exception fails this item only."""
    tenant = db.get(Tenant, job.tenant_id)
    actor = db.get(User, payload.get("actor_id"))
    filename = payload["filename"]

    # store_upload returns a path relative to the upload root, so it has to be
    # resolved before opening. Opening it directly worked only when the process
    # happened to be running from the data directory.
    with open(docsvc.absolute_path(payload["path"]), "rb") as handle:
        data = handle.read()
    text, method = textextract.extract(data, filename)

    catalogue = list(db.scalars(select(DocumentType).where(and_(
        DocumentType.is_active.is_(True),
        DocumentType.scope == "global"))).all())

    if payload.get("_ai_budget_exhausted"):
        result = ai._classify_heuristic(filename, text, catalogue)
        result["source"] = "heuristic"
        result.setdefault("field_confidence", {})
        result["fields"] = ai._clean_fields(result.get("fields", {}),
                                            result.get("field_confidence", {}))
    else:
        result = ai.classify_document(db, tenant.id, filename=filename,
                                      text=text, catalogue=catalogue)
        if result.get("source") == "model":
            jobs.note_ai_call(db, job)

    confidence = float(result.get("confidence") or 0)
    type_code = result.get("type_code")
    dtype = (db.scalar(select(DocumentType).where(DocumentType.code == type_code))
             if type_code else None)

    outcome = {"filename": filename, "confidence": round(confidence, 2),
               "engine": result.get("source"), "text_method": method}

    # Below the propose threshold, or no candidate at all: this is where an
    # unmatched document becomes a tenant-scoped proposed type (K1).
    if dtype is None or confidence < PROPOSE_THRESHOLD:
        proposal_name = _propose_name(filename, text, result)
        if proposal_name and confidence >= 0.15:
            proposal, dtype = catalogue_admin.propose(
                db, tenant_id=tenant.id, name=proposal_name,
                domain=result.get("domain") or (type_code or "COM")[:3],
                nearest_code=type_code, similarity=confidence,
                rationale=result.get("rationale", ""),
                source=result.get("source", "heuristic"))
            outcome["band"] = "proposed_type"
            outcome["proposal_id"] = proposal.id
            outcome["type_code"] = dtype.code
        else:
            outcome["band"] = "unclassified"
            outcome["reason"] = ("Nothing in the catalogue matched, and there was not "
                                 "enough text to propose a new type")
            return outcome
    else:
        outcome["band"] = "filed" if confidence >= FILE_THRESHOLD else "needs_confirmation"
        outcome["type_code"] = dtype.code

    document = db.scalar(select(Document).where(and_(
        Document.tenant_id == tenant.id,
        Document.document_type_id == dtype.id,
        Document.deleted_at.is_(None))))
    if document is None:
        document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                            title=dtype.name, owner_user_id=actor.id if actor else None,
                            batch_id=job.id)
        db.add(document)
        db.flush()
    elif not dtype.multi_instance:
        outcome["note"] = "Filed as a new version of a document you already hold"

    fields = result.get("fields", {})

    def as_date(value):
        try:
            return date.fromisoformat(str(value)) if value else None
        except (ValueError, TypeError):
            return None

    version = docsvc.create_version(
        db, document=document, actor=actor,
        issue_date=as_date(fields.get("issue_date")),
        effective_from=as_date(fields.get("issue_date")) or date.today(),
        expiry_date=as_date(fields.get("expiry_date")),
        certificate_number=fields.get("certificate_number"),
        issuer_name=fields.get("issuer_name"),
        holder_name=fields.get("holder_name") or tenant.name,
        scope_statement=fields.get("scope_statement"),
        cover_amount=fields.get("cover_amount"),
        currency="GBP" if fields.get("cover_amount") else None,
        file_path=payload["path"], file_name=filename, mime_type=payload["mime"],
        file_size=payload["size"], content_hash=payload["digest"],
        change_reason="Added by bulk upload")

    # F28.8 — nothing is published automatically. The version stays a draft
    # until a person confirms it, whatever the confidence was.
    outcome["document_id"] = document.id
    outcome["version_id"] = version.id
    outcome["alternatives"] = result.get("alternatives", [])[:3]
    return outcome


def _propose_name(filename: str, text: str, result: dict) -> str | None:
    """A proposal needs a name a human will recognise in a review queue."""
    proposed = (result.get("proposed_name") or "").strip()
    if proposed:
        return proposed[:160]
    stem = filename.rsplit(".", 1)[0]
    cleaned = stem.replace("_", " ").replace("-", " ").strip()
    cleaned = " ".join(w for w in cleaned.split() if not w.isdigit())
    if len(cleaned) < 4:
        return None
    return cleaned[:160].title()


def _finalise_batch(db: Session, job: Job) -> dict:
    """Runs once, after every item. F28.12 — build the profile; F28.13 — set no
    assurance, because every version earns its level through verification."""
    tenant = db.get(Tenant, job.tenant_id)
    if tenant is None:
        return {}

    bands = {"filed": 0, "needs_confirmation": 0, "proposed_type": 0,
             "unclassified": 0}
    proposals = []
    for item in jobs.items(db, job.id):
        if item.status != "done" or not item.result_json:
            continue
        try:
            outcome = json.loads(item.result_json)
        except ValueError:
            continue
        band = outcome.get("band")
        if band in bands:
            bands[band] += 1
        if outcome.get("proposal_id"):
            proposals.append(outcome.get("type_code"))

    docsvc.run_maintenance(db, tenant)
    trust.compute(db, tenant)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)

    payload = json.loads(job.payload_json or "{}")
    summary = {**bands, "failed": job.failed,
               "duplicates": len(payload.get("duplicates", [])),
               "new_types": proposals}
    audit.record(db, tenant_id=tenant.id, action="batch.completed",
                 actor_kind="system", actor_label="batch upload",
                 object_type="job", object_id=job.id, payload=summary)
    db.commit()
    return summary


def summary(db: Session, job: Job) -> dict:
    payload = json.loads(job.payload_json or "{}")
    outcomes = []
    for item in jobs.items(db, job.id):
        entry = {"position": item.position, "ref": item.ref, "status": item.status,
                 "error": item.error}
        if item.status == "done" and item.result_json:
            try:
                entry.update(json.loads(item.result_json))
            except ValueError:
                pass
        outcomes.append(entry)
    return {"job": job, "outcomes": outcomes,
            "duplicates": payload.get("duplicates", []),
            "result": json.loads(job.result_json) if job.result_json else None}


def discard(db: Session, tenant: Tenant, job: Job, actor) -> int:
    """F28.15 — the whole batch can be thrown away before confirmation."""
    removed = 0
    for item in jobs.items(db, job.id):
        if item.status != "done" or not item.result_json:
            continue
        try:
            outcome = json.loads(item.result_json)
        except ValueError:
            continue
        version = (db.get(DocumentVersion, outcome["version_id"])
                   if outcome.get("version_id") else None)
        if version is None or version.status != "draft":
            continue                     # confirmed already; leave it alone
        document = db.get(Document, version.document_id)
        db.delete(version)
        remaining = db.scalar(select(func.count(DocumentVersion.id)).where(
            DocumentVersion.document_id == document.id)) if document else 0
        if document and remaining <= 1:
            document.deleted_at = now()
        removed += 1
    job.status = "cancelled"
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="batch.discarded",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="job", object_id=job.id,
                 payload={"drafts_removed": removed})
    return removed


jobs.register(KIND, _classify_item)
jobs.register(f"{KIND}:finalise", _finalise_batch)
