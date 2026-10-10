"""REST API v1 - GICC-01 F12.

Auth: X-API-Key or Authorization: Bearer. Scopes are intersected with the
grant attached to the key, so a token can never see more than the grant allows.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..config import APP_VERSION
from ..db import get_db
from ..models import (AccessGrant, ApiClient, AuditEvent, CertificateGroup, Document,
                      DocumentType, DocumentVersion, Organisation, RequirementProfile,
                      Tenant, VerificationCheck, now)
from ..security import has_scope, require_api_client
from ..services import access, audit, requirements as reqsvc

router = APIRouter(prefix="/api/v1", tags=["API v1"])


def _need(client: ApiClient, scope: str) -> None:
    if not has_scope(client, scope):
        raise HTTPException(403, f"This key is missing the {scope} scope")


def _version_payload(db: Session, version: DocumentVersion | None) -> dict | None:
    if version is None:
        return None
    check = db.scalar(select(VerificationCheck).where(
        VerificationCheck.document_version_id == version.id)
        .order_by(desc(VerificationCheck.checked_at)).limit(1))
    return {
        "id": version.id, "version_no": version.version_no, "status": version.status,
        "issue_date": version.issue_date, "effective_from": version.effective_from,
        "expiry_date": version.expiry_date,
        "days_to_expiry": (version.expiry_date - date.today()).days if version.expiry_date else None,
        "certificate_number": version.certificate_number,
        "issuer_name": version.issuer_name, "holder_name": version.holder_name,
        "scope_statement": version.scope_statement,
        "cover_amount": version.cover_amount, "currency": version.currency,
        "assurance_level": version.assurance_level,
        "verification_url": version.verification_url,
        "content_hash": version.content_hash,
        "verification": {
            "method": check.method, "result": check.result,
            "checked_at": check.checked_at, "simulated": check.simulated,
            "detail": check.detail, "next_check_due": check.next_check_due,
        } if check else None,
    }


def _document_payload(db: Session, document: Document, full: bool = False) -> dict:
    version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
    payload = {
        "id": document.id, "type_code": document.document_type.code,
        "type_name": document.document_type.name,
        "domain": document.document_type.domain_name,
        "sensitivity": document.document_type.sensitivity,
        "title": document.title, "status": document.derived_status,
        "jurisdiction": document.jurisdiction,
        "current_version": _version_payload(db, version),
    }
    if full:
        payload["versions"] = [_version_payload(db, v) for v in db.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document.id)
            .order_by(desc(DocumentVersion.version_no))).all()]
    return payload


def _log(db: Session, client: ApiClient, request: Request, action: str,
         object_id: str | None = None, payload: dict | None = None) -> None:
    client.last_used_at = now()
    audit.record(db, tenant_id=client.tenant_id, action=action, actor_kind="api_client",
                 actor_id=client.id, actor_label=f"api:{client.name}",
                 object_id=object_id, grant_id=client.grant_id,
                 ip=request.client.host if request.client else None, payload=payload)
    db.commit()


@router.get("/meta", summary="Service metadata")
def meta(client: ApiClient = Depends(require_api_client), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, client.tenant_id)
    return {"service": "GICC", "version": APP_VERSION, "tenant": tenant.slug,
            "region": tenant.data_region, "scopes": client.scopes.split(),
            "grant_bound": bool(client.grant_id)}


@router.get("/profile", summary="Supplier profile")
def get_profile(request: Request, client: ApiClient = Depends(require_api_client),
                db: Session = Depends(get_db)):
    _need(client, "profile:read")
    tenant = db.get(Tenant, client.tenant_id)
    org = db.get(Organisation, tenant.organisation_id)
    _log(db, client, request, "api.profile_read")
    return {
        "tenant": tenant.slug, "legal_name": org.legal_name,
        "trading_name": org.trading_name, "country": org.country,
        "registry_number": org.registry_number, "lei": org.lei,
        "vat_number": org.vat_number, "duns": org.duns,
        "entity_verification": org.verification_state,
        "website": org.website, "employees": org.employees,
        "sector_code": org.sector_code, "description": org.description,
        "readiness_score": tenant.readiness_score,
        "lifecycle_state": tenant.lifecycle_state,
    }


@router.get("/documents", summary="List documents in scope")
def list_documents(request: Request, status: str | None = None,
                   type_code: str | None = None, domain: str | None = None,
                   client: ApiClient = Depends(require_api_client),
                   db: Session = Depends(get_db)):
    _need(client, "documents:read")
    documents = access.scoped_documents(db, client)
    out = []
    for document in documents:
        if status and document.derived_status != status:
            continue
        if type_code and document.document_type.code != type_code:
            continue
        if domain and document.document_type.domain_code != domain:
            continue
        out.append(_document_payload(db, document))
    _log(db, client, request, "api.documents_listed", payload={"returned": len(out)})
    return {"count": len(out), "documents": sorted(out, key=lambda d: d["type_code"])}


@router.get("/documents/{document_id}", summary="Document with full version history")
def get_document(document_id: str, request: Request,
                 client: ApiClient = Depends(require_api_client),
                 db: Session = Depends(get_db)):
    _need(client, "documents:read")
    allowed = {d.id: d for d in access.scoped_documents(db, client)}
    document = allowed.get(document_id)
    if document is None:
        raise HTTPException(404, "Document not found or outside the scope of this key")
    _log(db, client, request, "api.document_read", object_id=document_id)
    return _document_payload(db, document, full=True)


@router.get("/expiries", summary="Credentials expiring within N days")
def expiries(request: Request, within_days: int = Query(90, ge=1, le=730),
             client: ApiClient = Depends(require_api_client),
             db: Session = Depends(get_db)):
    _need(client, "documents:read")
    out = []
    for document in access.scoped_documents(db, client):
        version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        if version is None or version.expiry_date is None:
            continue
        days = (version.expiry_date - date.today()).days
        if days <= within_days:
            out.append({"document_id": document.id, "type_code": document.document_type.code,
                        "title": document.title, "expiry_date": version.expiry_date,
                        "days_remaining": days, "status": document.derived_status,
                        "assurance_level": version.assurance_level,
                        "criticality": document.document_type.criticality})
    out.sort(key=lambda r: r["days_remaining"])
    _log(db, client, request, "api.expiries_listed", payload={"within_days": within_days})
    return {"count": len(out), "within_days": within_days, "items": out}


@router.get("/verifications/{document_id}", summary="Verification history for a document")
def verifications(document_id: str, request: Request,
                  client: ApiClient = Depends(require_api_client),
                  db: Session = Depends(get_db)):
    _need(client, "verification:read")
    allowed = {d.id for d in access.scoped_documents(db, client)}
    if document_id not in allowed:
        raise HTTPException(404, "Document not found or outside the scope of this key")
    document = db.get(Document, document_id)
    versions = db.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == document_id)).all()
    checks = db.scalars(select(VerificationCheck).where(
        VerificationCheck.document_version_id.in_([v.id for v in versions] or ["-"]))
        .order_by(desc(VerificationCheck.checked_at))).all()
    _log(db, client, request, "api.verifications_read", object_id=document_id)
    return {"document_id": document_id, "title": document.title,
            "checks": [{"version_id": c.document_version_id, "method": c.method,
                        "source": c.source_code, "result": c.result,
                        "assurance": c.resulting_assurance, "simulated": c.simulated,
                        "detail": c.detail, "checked_at": c.checked_at}
                       for c in checks]}


@router.get("/groups", summary="Certificate groups")
def groups(request: Request, client: ApiClient = Depends(require_api_client),
           db: Session = Depends(get_db)):
    _need(client, "documents:read")
    if client.grant_id:
        grant = db.get(AccessGrant, client.grant_id)
        rows = [db.get(CertificateGroup, grant.scope_id)] if grant and grant.scope_kind == "group" else []
    else:
        rows = db.scalars(select(CertificateGroup).where(
            CertificateGroup.tenant_id == client.tenant_id)
            .order_by(CertificateGroup.display_order)).all()
    return {"groups": [{"id": g.id, "code": g.code, "name": g.name,
                        "sensitivity_ceiling": g.sensitivity_ceiling,
                        "nda_required": g.nda_required,
                        "download_allowed": g.download_allowed,
                        "documents": len(access.group_documents(db, client.tenant_id, g))}
                       for g in rows if g]}


@router.get("/requirement-profiles", summary="Available requirement profiles")
def requirement_profiles(client: ApiClient = Depends(require_api_client),
                         db: Session = Depends(get_db)):
    _need(client, "requirements:read")
    profiles = db.scalars(select(RequirementProfile).where(
        (RequirementProfile.tenant_id.is_(None)) |
        (RequirementProfile.tenant_id == client.tenant_id))).all()
    return {"profiles": [{"id": p.id, "name": p.name, "sector": p.sector,
                          "jurisdiction": p.jurisdiction, "items": len(p.items)}
                         for p in profiles]}


@router.post("/requirement-profiles/{profile_id}/evaluate", summary="Run a conformance check")
def evaluate(profile_id: str, request: Request,
             client: ApiClient = Depends(require_api_client),
             db: Session = Depends(get_db)):
    _need(client, "requirements:evaluate")
    profile = db.get(RequirementProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Requirement profile not found")
    tenant = db.get(Tenant, client.tenant_id)
    assessment = reqsvc.evaluate(db, tenant, profile, actor=None)
    _log(db, client, request, "api.requirements_evaluated", object_id=assessment.id)
    return {"assessment_id": assessment.id, "profile": profile.name,
            "score": assessment.score, "met": assessment.met_count,
            "gaps": assessment.gap_count, "engine_version": assessment.engine_version,
            "assessed_at": assessment.assessed_at,
            "results": [{"type_code": r.type_code, "type_name": r.type_name,
                         "outcome": r.outcome, "cited_version_id": r.cited_version_id,
                         "explanation": r.explanation} for r in assessment.results]}


@router.get("/events", summary="Audit event feed")
def events(request: Request, limit: int = Query(50, ge=1, le=500),
           client: ApiClient = Depends(require_api_client), db: Session = Depends(get_db)):
    _need(client, "events:subscribe")
    rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == client.tenant_id)
                      .order_by(desc(AuditEvent.occurred_at)).limit(limit)).all()
    return {"count": len(rows),
            "events": [{"id": e.id, "occurred_at": e.occurred_at, "action": e.action,
                        "actor_kind": e.actor_kind, "actor": e.actor_label,
                        "object_type": e.object_type, "object_id": e.object_id,
                        "grant_id": e.grant_id} for e in rows]}


@router.get("/catalogue/types", summary="Document type catalogue")
def catalogue_types(domain: str | None = None, q: str | None = None,
                    client: ApiClient = Depends(require_api_client),
                    db: Session = Depends(get_db)):
    query = select(DocumentType).where(DocumentType.is_active.is_(True))
    if domain:
        query = query.where(DocumentType.domain_code == domain)
    if q:
        query = query.where(DocumentType.name.ilike(f"%{q}%"))
    rows = db.scalars(query.order_by(DocumentType.code)).all()
    return {"count": len(rows),
            "types": [{"code": t.code, "name": t.name, "domain": t.domain_name,
                       "typical_issuer": t.typical_issuer, "validity": t.validity_label,
                       "verification_route": t.verification_route,
                       "sensitivity": t.sensitivity, "criticality": t.criticality}
                      for t in rows]}
