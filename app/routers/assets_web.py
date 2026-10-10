"""Asset register and calibration records - GICC-07 F29."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (Asset, CalibrationRecord, Document, DocumentType,
                      DocumentVersion, ImpactAssessment, Tenant, User)
from ..security import require_tenant, require_user
from ..services import assets, documents as docsvc, roles
from .web_core import page

router = APIRouter()

CALIBRATION_TYPES = ["CAL-001", "CAL-002", "CAL-003", "CAL-011", "CAL-015"]


@router.get("/assets")
def asset_register(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    coverage = assets.coverage(db, tenant.id)
    rows = []
    for asset in assets.assets_for(db, tenant.id):
        latest = db.scalar(select(CalibrationRecord)
                           .where(CalibrationRecord.asset_id == asset.id)
                           .order_by(desc(CalibrationRecord.calibration_date))
                           .limit(1))
        rows.append({"asset": asset, "latest": latest,
                     "days": (asset.next_due_on - date.today()).days
                             if asset.next_due_on else None})
    return page(request, "assets.html", user, tenant, db,
                page_title="Asset register", crumb="Calibration", nav="assets",
                rows=rows, coverage=coverage,
                assessments=assets.open_assessments(db, tenant.id),
                can_write=roles.can(user, roles.DOCUMENTS_WRITE))


@router.post("/assets")
def asset_create(request: Request, asset_tag: str = Form(...), name: str = Form(...),
                 serial_number: str = Form(""), manufacturer: str = Form(""),
                 model: str = Form(""), category: str = Form("measuring_instrument"),
                 location: str = Form(""), criticality: int = Form(3),
                 calibration_interval_months: str = Form(""),
                 interval_basis: str = Form(""),
                 db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    if assets.find_asset(db, tenant.id, tag=asset_tag) is not None:
        flash(request, f"An asset with tag {asset_tag} already exists.", "bad")
        return RedirectResponse("/assets", status_code=303)

    interval = (int(calibration_interval_months)
                if calibration_interval_months.strip().isdigit() else None)
    assets.create_asset(
        db, tenant=tenant, actor=user, asset_tag=asset_tag, name=name,
        serial_number=serial_number or None, manufacturer=manufacturer or None,
        model=model or None, category=category, location=location or None,
        criticality=int(criticality), calibration_interval_months=interval,
        interval_basis=interval_basis or None)
    db.commit()
    flash(request, f"{asset_tag} added to the register.")
    return RedirectResponse("/assets", status_code=303)


@router.get("/assets/{asset_id}")
def asset_detail(asset_id: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    asset = db.get(Asset, asset_id)
    if asset is None or asset.tenant_id != tenant.id:
        raise HTTPException(404, "Asset not found")

    records = db.scalars(select(CalibrationRecord)
                         .where(CalibrationRecord.asset_id == asset.id)
                         .order_by(desc(CalibrationRecord.calibration_date))).all()
    enriched = [{"record": r, "version": db.get(DocumentVersion, r.document_version_id),
                 "cap": assets.cap_for_accreditation(r)} for r in records]

    types = db.scalars(select(DocumentType)
                       .where(DocumentType.code.in_(CALIBRATION_TYPES))
                       .order_by(DocumentType.code)).all()
    assessments = db.scalars(select(ImpactAssessment)
                             .where(ImpactAssessment.asset_id == asset.id)
                             .order_by(desc(ImpactAssessment.created_at))).all()
    return page(request, "asset_detail.html", user, tenant, db,
                page_title=asset.asset_tag, crumb="Calibration", nav="assets",
                asset=asset, records=enriched, calibration_types=types,
                assessments=assessments,
                can_write=roles.can(user, roles.DOCUMENTS_WRITE))


@router.post("/assets/{asset_id}/calibration")
async def calibration_add(asset_id: str, request: Request,
                          type_code: str = Form("CAL-001"),
                          calibration_date: str = Form(""),
                          next_due_on: str = Form(""),
                          laboratory_name: str = Form(""),
                          accreditation_body: str = Form(""),
                          accreditation_number: str = Form(""),
                          as_found: str = Form("not_applicable"),
                          as_left: str = Form("in_tolerance"),
                          measurement_uncertainty: str = Form(""),
                          traceability_statement: str = Form(""),
                          certificate: UploadFile = File(...),
                          db: Session = Depends(get_db),
                          user: User = Depends(require_user),
                          tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    asset = db.get(Asset, asset_id)
    if asset is None or asset.tenant_id != tenant.id:
        raise HTTPException(404, "Asset not found")

    dtype = db.scalar(select(DocumentType).where(DocumentType.code == type_code))
    if dtype is None:
        flash(request, f"{type_code} is not in the catalogue.", "bad")
        return RedirectResponse(f"/assets/{asset_id}", status_code=303)

    data = await certificate.read()
    if not data:
        flash(request, "The certificate file was empty.", "bad")
        return RedirectResponse(f"/assets/{asset_id}", status_code=303)
    path, digest = docsvc.store_upload(tenant.id, certificate.filename or "cert.pdf",
                                       data)

    def parse(value: str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    # A multi-instance type gets one Document per asset, so each instrument
    # carries its own version history and its own expiry.
    document = db.scalar(select(Document).where(and_(
        Document.tenant_id == tenant.id, Document.document_type_id == dtype.id,
        Document.asset_id == asset.id, Document.deleted_at.is_(None))))
    if document is None:
        document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                            title=f"{dtype.name} - {asset.asset_tag}",
                            owner_user_id=user.id, asset_id=asset.id)
        db.add(document)
        db.flush()

    calibrated = parse(calibration_date)
    due = parse(next_due_on)
    version = docsvc.create_version(
        db, document=document, actor=user, issue_date=calibrated,
        effective_from=calibrated or date.today(), expiry_date=due,
        issuer_name=laboratory_name or None, holder_name=tenant.name,
        scope_statement=f"{asset.name} (serial {asset.serial_number or 'not recorded'})",
        file_path=path, file_name=certificate.filename or "certificate.pdf",
        mime_type=certificate.content_type or "application/pdf",
        file_size=len(data), content_hash=digest,
        change_reason="Calibration record added")

    record = assets.record_calibration(
        db, tenant=tenant, actor=user, asset=asset, version=version,
        calibration_date=calibrated, next_due_on=due,
        laboratory_name=laboratory_name, accreditation_body=accreditation_body,
        accreditation_number=accreditation_number, as_found=as_found,
        as_left=as_left, measurement_uncertainty=measurement_uncertainty,
        traceability_statement=traceability_statement)

    if not version.expiry_date and record.next_due_on:
        version.expiry_date = record.next_due_on
    docsvc.run_maintenance(db, tenant)
    db.commit()

    if as_found == "out_of_tolerance":
        flash(request, f"Recorded as out-of-tolerance. {asset.asset_tag} is "
                       "quarantined and an impact assessment covering the period "
                       "since the last calibration must be completed.", "warn")
    else:
        cap = assets.cap_for_accreditation(record)
        if cap:
            flash(request, "Recorded. Without a recognised accreditation body and "
                           f"number this record is capped at {cap} - a traceability "
                           "statement is a claim, not a proof.", "warn")
        else:
            flash(request, "Calibration recorded.")
    return RedirectResponse(f"/assets/{asset_id}", status_code=303)


@router.post("/assessments/{assessment_id}/close")
def assessment_close(assessment_id: str, request: Request,
                     disposition: str = Form(...), conclusion: str = Form(...),
                     products_affected: str = Form(""),
                     customers_notified: str = Form(""),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_user),
                     tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    assessment = db.get(ImpactAssessment, assessment_id)
    if assessment is None or assessment.tenant_id != tenant.id:
        raise HTTPException(404, "Assessment not found")
    assets.close_impact_assessment(
        db, assessment, actor=user, disposition=disposition, conclusion=conclusion,
        products_affected=products_affected,
        customers_notified=customers_notified == "on")
    db.commit()
    flash(request, "Impact assessment closed and the asset returned to service.")
    return RedirectResponse(f"/assets/{assessment.asset_id}", status_code=303)
