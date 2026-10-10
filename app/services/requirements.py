"""Conformance engine - GICC-01 F10.2, GICC-04 P-37.

Every result cites the exact document version it evaluated, and the engine
version is stamped on the assessment, so a run can be reproduced later.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (Assessment, AssessmentResult, Document, DocumentType,
                      DocumentVersion, RequirementItem, RequirementProfile, Tenant)
from . import assets, audit
from .documents import assurance_at_least

ENGINE_VERSION = "1.0.0"

OUTCOME_ORDER = {"not_met": 0, "expired": 1, "partially_met": 2, "met": 3, "not_applicable": 4}


def evaluate(db: Session, tenant: Tenant, profile: RequirementProfile, actor=None) -> Assessment:
    assessment = Assessment(tenant_id=tenant.id, profile_id=profile.id,
                            engine_version=ENGINE_VERSION)
    db.add(assessment)
    db.flush()

    met = gaps = 0
    weighted_total = weighted_met = 0.0

    for item in sorted(profile.items, key=lambda i: i.type_code):
        dtype = db.scalar(select(DocumentType).where(DocumentType.code == item.type_code))
        type_name = dtype.name if dtype else item.type_code
        outcome, version_id, explanation = _evaluate_item(db, tenant, item, dtype)

        db.add(AssessmentResult(
            assessment_id=assessment.id, type_code=item.type_code, type_name=type_name,
            outcome=outcome, cited_version_id=version_id, explanation=explanation))

        weight = item.weight or 3
        if item.mandatory:
            weighted_total += weight
            if outcome == "met":
                weighted_met += weight
            elif outcome == "partially_met":
                weighted_met += weight * 0.5
        if outcome == "met":
            met += 1
        elif outcome != "not_applicable":
            gaps += 1

    assessment.met_count = met
    assessment.gap_count = gaps
    assessment.score = round((weighted_met / weighted_total) * 100, 1) if weighted_total else 100.0
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="requirement.evaluated",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="assessment", object_id=assessment.id,
                 payload={"profile": profile.name, "score": assessment.score,
                          "met": met, "gaps": gaps, "engine": ENGINE_VERSION})
    return assessment


def _evaluate_item(db: Session, tenant: Tenant, item: RequirementItem,
                   dtype: DocumentType | None) -> tuple[str, str | None, str]:
    if dtype is None:
        return "not_applicable", None, f"{item.type_code} is not in the catalogue"

    # K2 - a multi-instance type resolves to a set. Evaluating an arbitrary
    # row would let 399 good instruments hide one overdue instrument.
    resolution = assets.resolve(db, tenant.id, dtype.code)
    if not resolution["held"]:
        return "not_met", None, "No document of this type has been added"
    document = resolution["worst"]
    instance_note = ""
    if resolution.get("multi") and resolution["count"] > 1:
        instance_note = (f" Held for {resolution['count']} instances"
                         + (f"; {resolution['impaired']} impaired."
                            if resolution["impaired"] else "."))

    version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
    if version is None or version.status != "active":
        return "not_met", None, "The document exists but has no published version"

    failures = []
    if version.expiry_date and version.expiry_date < date.today():
        return ("expired", version.id,
                f"Expired on {version.expiry_date:%d %b %Y}" + instance_note)

    if not assurance_at_least(version.assurance_level, item.min_assurance):
        failures.append(f"assurance is {version.assurance_level}, "
                        f"{item.min_assurance} required")

    if item.max_age_days:
        reference = version.issue_date or version.effective_from
        if reference and (date.today() - reference).days > item.max_age_days:
            failures.append(f"issued {(date.today() - reference).days} days ago, "
                            f"maximum {item.max_age_days}")

    if item.min_cover_amount:
        if not version.cover_amount:
            failures.append("no cover amount recorded")
        elif version.cover_amount < item.min_cover_amount:
            failures.append(f"cover {version.cover_amount:,.0f} below the "
                            f"{item.min_cover_amount:,.0f} required")

    if not failures:
        return "met", version.id, (f"v{version.version_no}, assurance "
                                   f"{version.assurance_level}"
                                   + (f", expires {version.expiry_date:%d %b %Y}"
                                      if version.expiry_date else "")
                                   + instance_note)
    if len(failures) == 1 and "assurance" in failures[0]:
        return ("partially_met", version.id,
                "Document held but " + failures[0] + instance_note)
    return "partially_met" if len(failures) == 1 else "not_met", version.id, \
        "Held but " + "; ".join(failures)


def gap_summary(db: Session, tenant: Tenant) -> list[dict]:
    """Ranks missing document types by how many published requirement profiles
    demand them - GICC-01 F10.3."""
    profiles = db.scalars(select(RequirementProfile)).all()
    demand: dict[str, dict] = {}
    for profile in profiles:
        for item in profile.items:
            entry = demand.setdefault(item.type_code, {"code": item.type_code,
                                                       "demanded_by": 0, "profiles": []})
            entry["demanded_by"] += 1
            entry["profiles"].append(profile.name)

    held = {}
    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()
    for document, dtype in rows:
        held[dtype.code] = document.derived_status

    out = []
    for code, entry in demand.items():
        status = held.get(code)
        if status in {"valid", "expiring_soon"}:
            continue
        dtype = db.scalar(select(DocumentType).where(DocumentType.code == code))
        out.append({**entry,
                    "name": dtype.name if dtype else code,
                    "criticality": dtype.criticality if dtype else 3,
                    "status": status or "missing"})
    out.sort(key=lambda e: (-e["demanded_by"], -e["criticality"]))
    return out
