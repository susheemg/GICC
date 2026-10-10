"""Batch document upload - GICC-07 F28."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..config import MAX_BATCH_FILES
from ..db import get_db
from ..models import Document, DocumentType, DocumentVersion, Job, Tenant, User
from ..security import require_tenant, require_user
from ..services import batch, jobs, roles
from .web_core import page

router = APIRouter()

MAX_FILE_BYTES = 25 * 1024 * 1024


@router.get("/documents/bulk")
def bulk_index(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_user),
               tenant: Tenant = Depends(require_tenant)):
    recent = db.scalars(select(Job).where(and_(
        Job.tenant_id == tenant.id, Job.kind == batch.KIND))
        .order_by(desc(Job.created_at)).limit(10)).all()
    return page(request, "bulk_upload.html", user, tenant, db,
                page_title="Bulk upload", crumb="Credentials", nav="documents",
                recent=recent, max_files=MAX_BATCH_FILES,
                can_write=roles.can(user, roles.DOCUMENTS_WRITE))


@router.post("/documents/bulk")
async def bulk_start(request: Request, files: list[UploadFile] = File(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_user),
                     tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    accepted, rejected = [], []

    # Pre-flight rejection happens before anything starts, with a specific
    # reason per file. Telling somebody at the end that two files failed is
    # worse than telling them now which two and why.
    for upload in files:
        data = await upload.read()
        name = upload.filename or "file"
        if not data:
            rejected.append(f"{name}: the file was empty")
        elif len(data) > MAX_FILE_BYTES:
            rejected.append(f"{name}: {len(data) / 1_048_576:.0f} MB exceeds the "
                            f"{MAX_FILE_BYTES // 1_048_576} MB limit")
        elif data[:5] == b"%PDF-" and b"/Encrypt" in data[:4096]:
            rejected.append(f"{name}: the PDF is password-protected, so its text "
                            "cannot be read")
        else:
            accepted.append((name, data, upload.content_type or ""))

    if len(accepted) > MAX_BATCH_FILES:
        rejected.extend(f"{n}: over the {MAX_BATCH_FILES}-file limit for one batch"
                        for n, _, _ in accepted[MAX_BATCH_FILES:])
        accepted = accepted[:MAX_BATCH_FILES]

    if not accepted:
        flash(request, "Nothing could be processed. " + "; ".join(rejected[:4]), "bad")
        return RedirectResponse("/documents/bulk", status_code=303)

    job = batch.start_batch(db, tenant=tenant, actor=user, files=accepted)
    db.commit()
    if rejected:
        request.session["_bulk_rejected"] = rejected[:10]
    jobs.start(job.id)
    return RedirectResponse(f"/documents/bulk/{job.id}", status_code=303)


@router.get("/documents/bulk/{job_id}")
def bulk_progress(job_id: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    job = db.get(Job, job_id)
    if job is None or job.tenant_id != tenant.id:
        raise HTTPException(404, "Batch not found")

    detail = batch.summary(db, job)
    versions = {}
    for outcome in detail["outcomes"]:
        version_id = outcome.get("version_id")
        if not version_id:
            continue
        version = db.get(DocumentVersion, version_id)
        if version is None:
            continue
        document = db.get(Document, version.document_id)
        dtype = db.get(DocumentType, document.document_type_id) if document else None
        versions[version_id] = {"version": version, "document": document,
                                "type": dtype}

    return page(request, "bulk_progress.html", user, tenant, db,
                page_title="Bulk upload", crumb="Credentials", nav="documents",
                job=job, detail=detail, versions=versions,
                rejected=request.session.pop("_bulk_rejected", None),
                progress=jobs.progress(db, job.id),
                can_write=roles.can(user, roles.DOCUMENTS_WRITE))


@router.get("/documents/bulk/{job_id}/progress")
def bulk_progress_json(job_id: str, db: Session = Depends(get_db),
                       user: User = Depends(require_user),
                       tenant: Tenant = Depends(require_tenant)):
    job = db.get(Job, job_id)
    if job is None or job.tenant_id != tenant.id:
        raise HTTPException(404, "Batch not found")
    return JSONResponse(jobs.progress(db, job.id))


@router.post("/documents/bulk/{job_id}/discard")
def bulk_discard(job_id: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    job = db.get(Job, job_id)
    if job is None or job.tenant_id != tenant.id:
        raise HTTPException(404, "Batch not found")
    removed = batch.discard(db, tenant, job, user)
    db.commit()
    flash(request, f"Batch discarded. {removed} draft(s) removed. Anything you had "
                   "already confirmed was left alone.", "warn")
    return RedirectResponse("/documents", status_code=303)
