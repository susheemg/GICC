"""Operator routes for bulk import and the review queues - GICC-07.

Import is deliberately three steps: upload and map, validate as a dry run,
then commit. Nothing reaches a live table until somebody has read the outcome
report.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (DocumentType, DocumentTypeProposal, ImportBatch, ImportRow,
                      Organisation, PlatformAdmin, ProfileClaim, Tenant, User, now)
from ..security import require_admin
from ..services import audit, catalogue_admin, claims, importer
from .admin import admin_page

router = APIRouter(prefix="/admin")


# --------------------------------------------------------------------------
# F26 — bulk import
# --------------------------------------------------------------------------
@router.get("/import")
def import_index(request: Request, db: Session = Depends(get_db),
                 admin: PlatformAdmin = Depends(require_admin)):
    batches = db.scalars(select(ImportBatch)
                         .order_by(desc(ImportBatch.created_at)).limit(30)).all()
    return admin_page(request, "admin_import.html", admin, db,
                      page_title="Bulk profile import", nav="import",
                      batches=batches,
                      permitted=importer.PERMITTED_FIELDS, batch_counts=importer.counts)


@router.post("/import/upload")
async def import_upload(request: Request, file: UploadFile = File(...),
                        db: Session = Depends(get_db),
                        admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    data = await file.read()
    header, rows = importer.parse(data, file.filename or "import.csv")
    if not header or not rows:
        flash(request, "No rows found. Expecting a header row and at least one "
                       "data row.", "bad")
        return RedirectResponse("/admin/import", status_code=303)

    batch = importer.stage(db, filename=file.filename or "import.csv",
                           header=header, rows=rows, actor=admin)
    db.commit()
    refused = importer.prohibited_columns(header)
    if refused:
        flash(request, f"{len(refused)} column(s) were refused and will not be "
                       "imported under any name.", "warn")
    return RedirectResponse(f"/admin/import/{batch.id}", status_code=303)


@router.post("/import/{batch_id}/validate")
async def import_validate(batch_id: str, request: Request,
                          db: Session = Depends(get_db),
                          admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")

    form = await request.form()
    mapping = {}
    for field in importer.PERMITTED_FIELDS:
        raw = (form.get(f"map_{field}") or "").strip()
        if raw.isdigit():
            mapping[field] = int(raw)

    if "legal_name" not in mapping or "country" not in mapping:
        flash(request, "Legal name and country must both be mapped — a profile "
                       "without them cannot be matched or claimed.", "bad")
        return RedirectResponse(f"/admin/import/{batch_id}", status_code=303)

    importer.validate(db, batch, mapping=mapping, actor=admin)
    db.commit()
    flash(request, "Dry run complete. Nothing has been written yet — review the "
                   "outcome and commit when you are satisfied.")
    return RedirectResponse(f"/admin/import/{batch.id}", status_code=303)


@router.get("/import/{batch_id}")
def import_detail(batch_id: str, request: Request, classification: str = "",
                  db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    query = select(ImportRow).where(ImportRow.batch_id == batch.id)
    if classification:
        query = query.where(ImportRow.classification == classification)
    rows = db.scalars(query.order_by(ImportRow.position).limit(400)).all()

    parsed = []
    for row in rows:
        try:
            values = json.loads(row.normalised_json or "{}")
        except ValueError:
            values = {}
        parsed.append({"row": row, "fields": values})

    sample = []
    if batch.status == "staged" and parsed:
        try:
            sample = json.loads(parsed[0]["row"].raw_json or "[]")
        except ValueError:
            sample = []

    return admin_page(request, "admin_import_detail.html", admin, db,
                      page_title=f"Import — {batch.filename or batch.id[:8]}",
                      nav="import", batch=batch, rows=parsed,
                      header=importer.header_of(batch),
                      mapping=importer.mapping_of(batch), sample=sample,
                      permitted=importer.PERMITTED_FIELDS,
                      outcome=importer.counts(batch), classification=classification,
                      blocker=importer.reversal_blocker(db, batch)
                      if batch.status == "committed" else None)


@router.post("/import/{batch_id}/commit")
def import_commit(batch_id: str, request: Request, db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    try:
        result = importer.commit(db, batch, actor=admin)
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "bad")
        return RedirectResponse(f"/admin/import/{batch_id}", status_code=303)
    flash(request, f"{result['created']} profile(s) created, {result['updated']} "
                   "updated. All are unclaimed until a company proves ownership.")
    return RedirectResponse(f"/admin/import/{batch_id}", status_code=303)


@router.post("/import/{batch_id}/reverse")
def import_reverse(batch_id: str, request: Request, db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    try:
        result = importer.reverse(db, batch, actor=admin)
        db.commit()
        flash(request, f"Batch reversed. {result['removed']} unclaimed profile(s) "
                       "removed.", "warn")
    except ValueError as exc:
        db.commit()
        flash(request, str(exc), "bad")
    return RedirectResponse(f"/admin/import/{batch_id}", status_code=303)


@router.post("/import/{batch_id}/discard")
def import_discard(batch_id: str, request: Request, db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    importer.discard(db, batch, actor=admin)
    db.commit()
    flash(request, "Staged rows discarded. Nothing was written.")
    return RedirectResponse("/admin/import", status_code=303)


@router.get("/import/{batch_id}/report")
def import_report(batch_id: str, db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    import csv
    import io

    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Row", "Classification", "Reason", "Legal name", "Country",
                     "Registry number"])
    for row in db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id)
                          .order_by(ImportRow.position)).all():
        values = json.loads(row.normalised_json or "{}")
        writer.writerow([row.position, row.classification, row.reason or "",
                         values.get("legal_name", ""), values.get("country", ""),
                         values.get("registry_number", "")])
    return PlainTextResponse(
        output.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="import-{batch.id[:8]}-report.csv"'})


# --------------------------------------------------------------------------
# F27 — claim adjudication
# --------------------------------------------------------------------------
@router.get("/claims")
def claim_queue(request: Request, db: Session = Depends(get_db),
                admin: PlatformAdmin = Depends(require_admin)):
    open_claims = claims.queue(db)
    rows = []
    for claim in open_claims:
        organisation = db.get(Organisation, claim.organisation_id)
        competing = claims.contested(db, claim.organisation_id)
        rows.append({
            "claim": claim, "organisation": organisation,
            "signals": json.loads(claim.signals_json or "[]"),
            "extracted": json.loads(claim.extracted_json or "{}"),
            "contested": len(competing) > 1,
            "flags": [f for f in (claim.adverse_flags or "").split(",") if f],
        })
    decided = db.scalars(select(ProfileClaim).where(
        ProfileClaim.status != "open")
        .order_by(desc(ProfileClaim.decided_at)).limit(20)).all()
    return admin_page(request, "admin_claims.html", admin, db,
                      page_title="Profile claims", nav="claims", rows=rows,
                      decided=decided)


@router.post("/claims/{claim_id}/decide")
def claim_decide(claim_id: str, request: Request, decision: str = Form(...),
                 note: str = Form(""), db: Session = Depends(get_db),
                 admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    from ..security import hash_password
    from ..seed import provision_tenant

    claim = db.get(ProfileClaim, claim_id)
    if claim is None:
        raise HTTPException(404, "Claim not found")
    organisation = db.get(Organisation, claim.organisation_id)

    if decision == "approve":
        tenant, owner = provision_tenant(
            db, legal_name=organisation.legal_name, country=organisation.country,
            email=claim.claimant_email, full_name=claim.claimant_name or "Owner",
            password=None, registry_number=organisation.registry_number,
            existing_organisation=organisation)
        claims.approve(db, claim, tenant=tenant, actor=admin, note=note)
        db.commit()
        flash(request, f"Claim approved. {organisation.legal_name} now belongs to "
                       f"{claim.claimant_email}, who must reset their password to "
                       "sign in.")
    else:
        claims.reject(db, claim, actor=admin, note=note)
        db.commit()
        flash(request, "Claim rejected. The claimant is told the reason and may try "
                       "once more with better evidence.", "warn")
    return RedirectResponse("/admin/claims", status_code=303)


@router.post("/claims/{claim_id}/reverse")
def claim_reverse(claim_id: str, request: Request, reason: str = Form(...),
                  db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    claim = db.get(ProfileClaim, claim_id)
    if claim is None:
        raise HTTPException(404, "Claim not found")
    claims.reverse(db, claim, actor=admin, reason=reason.strip())
    db.commit()
    flash(request, "Claim reversed. The profile is unclaimed again and the workspace "
                   "is suspended pending review.", "warn")
    return RedirectResponse("/admin/claims", status_code=303)


# --------------------------------------------------------------------------
# F28 — document type proposals
# --------------------------------------------------------------------------
@router.get("/proposals")
def proposal_queue(request: Request, status: str = "proposed",
                   db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    proposals = catalogue_admin.queue(db, status)
    rows = []
    for proposal in proposals:
        tenant = db.scalar(select(Tenant).where(Tenant.id == proposal.tenant_id))
        nearest = (db.scalar(select(DocumentType).where(
            DocumentType.code == proposal.nearest_type_code))
            if proposal.nearest_type_code else None)
        rows.append({"proposal": proposal, "tenant": tenant, "nearest": nearest})
    domains = db.execute(select(DocumentType.domain_code, DocumentType.domain_name)
                         .where(DocumentType.scope == "global").distinct()).all()
    return admin_page(request, "admin_proposals.html", admin, db,
                      page_title="Document type proposals", nav="proposals",
                      rows=rows, status=status, domains=sorted(set(domains)))


@router.post("/proposals/{proposal_id}/promote")
def proposal_promote(proposal_id: str, request: Request, name: str = Form(""),
                     domain: str = Form(""), typical_issuer: str = Form(""),
                     validity_months: str = Form(""), sensitivity: str = Form("R"),
                     criticality: int = Form(3), verification_route: str = Form("manual"),
                     multi_instance: str = Form(""), note: str = Form(""),
                     db: Session = Depends(get_db),
                     admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    proposal = db.get(DocumentTypeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    dtype = catalogue_admin.promote(
        db, proposal, actor=admin, name=name or None, domain=domain or None,
        typical_issuer=typical_issuer,
        validity_months=int(validity_months) if validity_months.strip().isdigit() else None,
        sensitivity=sensitivity, criticality=int(criticality),
        verification_route=verification_route,
        multi_instance=multi_instance == "on", note=note)
    db.commit()
    flash(request, f"Promoted to {dtype.code}. Every tenant document under the local "
                   "type has been migrated onto it.")
    return RedirectResponse("/admin/proposals", status_code=303)


@router.post("/proposals/{proposal_id}/merge")
def proposal_merge(proposal_id: str, request: Request, target_code: str = Form(...),
                   note: str = Form(""), db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    proposal = db.get(DocumentTypeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    try:
        migrated = catalogue_admin.merge(db, proposal, target_code=target_code.strip(),
                                         actor=admin, note=note)
        db.commit()
        flash(request, f"Merged into {target_code}; {migrated} document(s) migrated.")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "bad")
    return RedirectResponse("/admin/proposals", status_code=303)


@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(proposal_id: str, request: Request, note: str = Form(""),
                    db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash

    proposal = db.get(DocumentTypeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    catalogue_admin.reject(db, proposal, actor=admin, note=note)
    db.commit()
    flash(request, "Rejected. The tenant keeps it as a local type and loses nothing; "
                   "it simply stays out of cross-tenant matching.")
    return RedirectResponse("/admin/proposals", status_code=303)
