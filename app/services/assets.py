"""Asset register and calibration records - GICC-07 F29.

Calibration is the first thing in the catalogue that is asset-bearing. ISO 9001
clause 7.1.5 requires measuring equipment calibrated at specified intervals
against traceable standards, with records retained and calibration status
identifiable — commonly a serial number cross-referenced to a master log. That
means one certificate per *instrument*, and a company may hold four hundred.

Two consequences the rest of the platform has to absorb:

  * a type can resolve to a **set**, and the answer is the worst instance in it
    (K2) — a company with 399 calibrated instruments and one overdue is a
    company with an overdue instrument
  * an **out-of-tolerance** result is not an expiry. It means measurements
    since the previous calibration may be invalid, which reaches into product
    already shipped
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (Asset, CalibrationRecord, Document, DocumentType,
                      DocumentVersion, ImpactAssessment, Task, Tenant, now)
from . import audit

STATUS_RANK = {"missing": 0, "expired": 1, "suspended": 1, "withdrawn": 1,
               "expiring_soon": 2, "valid": 3}
ASSURANCE_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

# Accreditation bodies that are IAF/ILAC MRA signatories. An unrecognised body
# is a question, not a verdict — the list is never complete.
KNOWN_ACCREDITORS = {
    "ukas", "a2la", "dakks", "cofrac", "accredia", "nabl", "jab", "inab",
    "swedac", "raad voor accreditatie", "rva", "enac", "sanas", "nata",
    "ianz", "scc", "hka", "bmwfw", "danak", "finas", "norsk akkreditering",
}


# --------------------------------------------------------------------------
# Asset register
# --------------------------------------------------------------------------
def create_asset(db: Session, *, tenant: Tenant, actor, asset_tag: str, name: str,
                 **fields) -> Asset:
    asset = Asset(tenant_id=tenant.id, asset_tag=asset_tag.strip(), name=name.strip(),
                  **{k: v for k, v in fields.items() if v not in (None, "")})
    db.add(asset)
    db.flush()
    _recompute_due(db, asset)
    audit.record(db, tenant_id=tenant.id, action="asset.created",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="asset", object_id=asset.id,
                 payload={"tag": asset.asset_tag, "name": asset.name,
                          "serial": asset.serial_number})
    return asset


def assets_for(db: Session, tenant_id: str, *, include_retired: bool = False) -> list[Asset]:
    statement = select(Asset).where(Asset.tenant_id == tenant_id)
    if not include_retired:
        statement = statement.where(Asset.status != "retired")
    return list(db.scalars(statement.order_by(Asset.asset_tag)).all())


def find_asset(db: Session, tenant_id: str, *, serial: str = "",
               tag: str = "") -> Asset | None:
    """Serial number is how a certificate binds to an instrument — it is the
    only identifier the calibration laboratory knows about."""
    if serial:
        cleaned = serial.strip().upper()
        for asset in assets_for(db, tenant_id, include_retired=True):
            if (asset.serial_number or "").strip().upper() == cleaned:
                return asset
    if tag:
        return db.scalar(select(Asset).where(and_(
            Asset.tenant_id == tenant_id, Asset.asset_tag == tag.strip())))
    return None


def _recompute_due(db: Session, asset: Asset) -> None:
    latest = db.scalar(select(CalibrationRecord).where(
        CalibrationRecord.asset_id == asset.id)
        .order_by(CalibrationRecord.calibration_date.desc()).limit(1))
    if latest is None:
        asset.last_calibrated_on = None
        asset.next_due_on = None
        return
    asset.last_calibrated_on = latest.calibration_date
    if latest.next_due_on:
        asset.next_due_on = latest.next_due_on
    elif latest.calibration_date and asset.calibration_interval_months:
        asset.next_due_on = latest.calibration_date + timedelta(
            days=int(asset.calibration_interval_months * 30.44))
    db.flush()


# --------------------------------------------------------------------------
# Calibration records
# --------------------------------------------------------------------------
def record_calibration(db: Session, *, tenant: Tenant, actor, asset: Asset,
                       version: DocumentVersion, calibration_date: date | None,
                       next_due_on: date | None = None, laboratory_name: str = "",
                       accreditation_body: str = "", accreditation_number: str = "",
                       as_found: str = "not_applicable", as_left: str = "in_tolerance",
                       measurement_uncertainty: str = "",
                       traceability_statement: str = "",
                       standards_used: list | None = None,
                       ambient: dict | None = None) -> CalibrationRecord:
    verdict = ("fail" if as_left == "out_of_tolerance"
               else "pass_with_adjustment" if as_left == "adjusted_in_tolerance"
               else "pass")
    if not next_due_on and calibration_date and asset.calibration_interval_months:
        next_due_on = calibration_date + timedelta(
            days=int(asset.calibration_interval_months * 30.44))

    record = CalibrationRecord(
        tenant_id=tenant.id, document_version_id=version.id, asset_id=asset.id,
        calibration_date=calibration_date, next_due_on=next_due_on,
        interval_months=asset.calibration_interval_months,
        laboratory_name=laboratory_name.strip() or None,
        accreditation_body=accreditation_body.strip() or None,
        accreditation_number=accreditation_number.strip() or None,
        as_found=as_found, as_left=as_left, tolerance_verdict=verdict,
        measurement_uncertainty=measurement_uncertainty.strip() or None,
        traceability_statement=traceability_statement.strip() or None,
        traceability_verified=False,
        standards_used_json=json.dumps(standards_used or [], default=str),
        ambient_conditions_json=json.dumps(ambient or {}, default=str))
    db.add(record)
    db.flush()

    _recompute_due(db, asset)

    if as_found == "out_of_tolerance":
        assessment = open_impact_assessment(db, tenant=tenant, actor=actor,
                                            asset=asset, record=record)
        record.impact_assessment_id = assessment.id
        db.flush()

    audit.record(db, tenant_id=tenant.id, action="calibration.recorded",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="calibration_record", object_id=record.id,
                 payload={"asset": asset.asset_tag, "as_found": as_found,
                          "as_left": as_left, "verdict": verdict,
                          "laboratory": laboratory_name})
    return record


def open_impact_assessment(db: Session, *, tenant: Tenant, actor, asset: Asset,
                           record: CalibrationRecord) -> ImpactAssessment:
    """F29.6 — an out-of-tolerance result blocks the asset and opens a mandatory
    assessment covering the period since the previous calibration."""
    previous = db.scalar(select(CalibrationRecord).where(and_(
        CalibrationRecord.asset_id == asset.id,
        CalibrationRecord.id != record.id))
        .order_by(CalibrationRecord.calibration_date.desc()).limit(1))

    assessment = ImpactAssessment(
        tenant_id=tenant.id, asset_id=asset.id, calibration_record_id=record.id,
        period_from=previous.calibration_date if previous else None,
        period_to=record.calibration_date,
        disposition="under_assessment")
    db.add(assessment)

    asset.status = "quarantined"
    db.add(Task(tenant_id=tenant.id, kind="calibration_impact",
                title=f"Out-of-tolerance: assess impact for {asset.asset_tag} "
                      f"({asset.name})",
                severity="high", status="open",
                due_at=now() + timedelta(days=5)))
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="calibration.out_of_tolerance",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="impact_assessment", object_id=assessment.id,
                 payload={"asset": asset.asset_tag,
                          "period_from": str(assessment.period_from),
                          "period_to": str(assessment.period_to)})
    return assessment


def close_impact_assessment(db: Session, assessment: ImpactAssessment, *, actor,
                            disposition: str, conclusion: str,
                            products_affected: str = "",
                            customers_notified: bool = False) -> None:
    assessment.disposition = disposition
    assessment.conclusion = conclusion
    assessment.products_affected = products_affected or None
    assessment.customers_notified = customers_notified
    assessment.assessed_by = getattr(actor, "email", "system")
    assessment.assessed_at = now()
    assessment.closed_at = now()

    asset = db.get(Asset, assessment.asset_id)
    if asset and asset.status == "quarantined":
        asset.status = "in_service"
    db.flush()
    audit.record(db, tenant_id=assessment.tenant_id,
                 action="calibration.impact_assessed",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="impact_assessment", object_id=assessment.id,
                 payload={"disposition": disposition,
                          "customers_notified": customers_notified})


def open_assessments(db: Session, tenant_id: str) -> list[ImpactAssessment]:
    return list(db.scalars(select(ImpactAssessment).where(and_(
        ImpactAssessment.tenant_id == tenant_id,
        ImpactAssessment.closed_at.is_(None))).order_by(
        ImpactAssessment.created_at)).all())


# --------------------------------------------------------------------------
# Assurance and coverage
# --------------------------------------------------------------------------
def cap_for_accreditation(record: CalibrationRecord) -> str | None:
    """F29.7 — an unaccredited laboratory caps the record at L1 whatever the
    certificate claims. A traceability statement is a claim, not a proof."""
    body = (record.accreditation_body or "").lower()
    if not body or not record.accreditation_number:
        return "L1"
    if not any(known in body for known in KNOWN_ACCREDITORS):
        return "L1"
    return None


def coverage(db: Session, tenant_id: str) -> dict:
    """The gap direction auditors actually check: assets that should have a
    current record and do not."""
    today = date.today()
    assets = assets_for(db, tenant_id)
    requiring = [a for a in assets if a.calibration_interval_months]
    current = overdue = never = due_soon = 0
    overdue_list, never_list = [], []

    for asset in requiring:
        if asset.last_calibrated_on is None:
            never += 1
            never_list.append(asset)
        elif asset.next_due_on and asset.next_due_on < today:
            overdue += 1
            overdue_list.append(asset)
        elif asset.next_due_on and (asset.next_due_on - today).days <= 30:
            due_soon += 1
            current += 1
        else:
            current += 1

    quarantined = [a for a in assets if a.status == "quarantined"]
    return {
        "total": len(assets), "requiring": len(requiring), "current": current,
        "overdue": overdue, "never": never, "due_soon": due_soon,
        "quarantined": len(quarantined),
        "overdue_assets": overdue_list[:20], "never_assets": never_list[:20],
        "quarantined_assets": quarantined,
        "percent": round(current / len(requiring) * 100) if requiring else 100,
    }


def resolve(db: Session, tenant_id: str, type_code: str) -> dict:
    """K2 — a multi-instance type resolves to a set, and the answer is the worst
    instance in it, with the count attached so an explanation can say
    'held for 400 instruments; 1 overdue' rather than a bare pass or fail."""
    dtype = db.scalar(select(DocumentType).where(DocumentType.code == type_code))
    if dtype is None:
        return {"held": False, "count": 0, "worst": None, "impaired": 0}

    documents = db.scalars(select(Document).where(and_(
        Document.tenant_id == tenant_id,
        Document.document_type_id == dtype.id,
        Document.deleted_at.is_(None))).order_by(Document.created_at)).all()
    if not documents:
        return {"held": False, "count": 0, "worst": None, "impaired": 0,
                "multi": dtype.multi_instance}

    if not dtype.multi_instance:
        return {"held": True, "count": 1, "worst": documents[0], "impaired": 0,
                "multi": False}

    def rank(document: Document) -> tuple[int, int]:
        version = (db.get(DocumentVersion, document.active_version_id)
                   if document.active_version_id else None)
        return (STATUS_RANK.get(document.derived_status, 0),
                ASSURANCE_RANK.get(version.assurance_level if version else "L0", 0))

    ordered = sorted(documents, key=rank)
    impaired = sum(1 for d in documents
                   if STATUS_RANK.get(d.derived_status, 0) <= 1)
    return {"held": True, "count": len(documents), "worst": ordered[0],
            "impaired": impaired, "multi": True}


def sweep_due(db: Session, tenant: Tenant) -> int:
    """Per-asset expiry. A task names the instrument, not the document type —
    'CAL-001 expiring' tells a quality manager nothing useful."""
    today = date.today()
    created = 0
    for asset in assets_for(db, tenant.id):
        if not asset.next_due_on or asset.status == "retired":
            continue
        days = (asset.next_due_on - today).days
        if days > 30:
            continue
        title = (f"Calibration overdue: {asset.asset_tag} ({asset.name})"
                 if days < 0 else
                 f"Calibration due in {days} days: {asset.asset_tag} ({asset.name})")
        existing = db.scalar(select(Task).where(and_(
            Task.tenant_id == tenant.id, Task.title == title,
            Task.status == "open")))
        if existing:
            continue
        db.add(Task(tenant_id=tenant.id, kind="calibration_due", title=title,
                    severity="high" if days < 0 else "medium", status="open",
                    due_at=now() + timedelta(days=max(days, 0))))
        created += 1
    db.flush()
    return created
