from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..catalogue import DOMAINS
from ..db import get_db
from ..models import (CertificateGroup, Document, DocumentType, DocumentVersion,
                      GroupItem, Tenant, User, VerificationCheck, now)
from ..security import require_tenant, require_user
from ..services import (audit, authenticity, certsearch,
                        documents as docsvc, onboarding, roles, verification)
from .web_core import page

router = APIRouter()


def _extract_hint(dtype: DocumentType, filename: str) -> dict:
    """Stand-in for the extraction step (GICC-04 P-13). It proposes values from
    the catalogue defaults and the filename; a human confirms before anything
    is stored as fact. Real extraction plugs in here."""
    today = date.today()
    expiry = None
    if dtype.validity_months:
        expiry = today + timedelta(days=int(dtype.validity_months * 30.44))
    return {"issue_date": today, "expiry_date": expiry,
            "issuer_name": dtype.typical_issuer, "confidence": "proposed"}


# --------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------
@router.get("/documents")
def register(request: Request, status: str = "", domain: str = "",
             db: Session = Depends(get_db), user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    query = (select(Document, DocumentType)
             .join(DocumentType, Document.document_type_id == DocumentType.id)
             .where(and_(Document.tenant_id == tenant.id, Document.deleted_at.is_(None))))
    if status:
        query = query.where(Document.derived_status == status)
    if domain:
        query = query.where(DocumentType.domain_code == domain)

    rank = {"expired": 0, "suspended": 1, "missing": 2, "expiring_soon": 3,
            "withdrawn": 4, "valid": 5}
    rows = []
    for document, dtype in db.execute(query).all():
        version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        rows.append({"document": document, "type": dtype, "version": version,
                     "days": (version.expiry_date - date.today()).days
                     if version and version.expiry_date else None})
    rows.sort(key=lambda r: (rank.get(r["document"].derived_status, 9),
                             r["days"] if r["days"] is not None else 9999))

    return page(request, "documents_register.html", user, tenant, db,
                page_title="Document register", crumb="Credentials", nav="documents",
                rows=rows, status=status, domain=domain, domains=DOMAINS)


# --------------------------------------------------------------------------
# Catalogue picker
# --------------------------------------------------------------------------
@router.get("/documents/catalogue")
def catalogue(request: Request, q: str = "", domain: str = "",
              db: Session = Depends(get_db), user: User = Depends(require_user),
              tenant: Tenant = Depends(require_tenant)):
    query = select(DocumentType).where(DocumentType.is_active.is_(True))
    if domain:
        query = query.where(DocumentType.domain_code == domain)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(DocumentType.name.ilike(like) | DocumentType.code.ilike(like)
                            | DocumentType.typical_issuer.ilike(like))
    types = db.scalars(query.order_by(DocumentType.code)).all()

    held = {d.document_type_id: d for d in db.scalars(select(Document).where(and_(
        Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()}

    from ..services.requirements import gap_summary
    recommended = {g["code"] for g in gap_summary(db, tenant)[:12]}

    grouped: dict[str, list] = {}
    for dtype in types:
        grouped.setdefault(dtype.domain_code, []).append(dtype)

    return page(request, "documents_catalogue.html", user, tenant, db,
                page_title="Add from catalogue", crumb="Credentials", nav="catalogue",
                grouped=grouped, domains=DOMAINS, held=held, q=q, domain=domain,
                recommended=recommended, total=len(types))


@router.post("/documents/create")
def create_document(request: Request, type_code: str = Form(...),
                    db: Session = Depends(get_db), user: User = Depends(require_user),
                    tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.DOCUMENTS_WRITE)
    from ..main import flash
    dtype = db.scalar(select(DocumentType).where(DocumentType.code == type_code))
    if dtype is None:
        raise HTTPException(404, "Unknown document type")

    existing = db.scalar(select(Document).where(and_(
        Document.tenant_id == tenant.id, Document.document_type_id == dtype.id,
        Document.deleted_at.is_(None))))
    if existing:
        flash(request, f"{dtype.name} already exists — add a new version instead.", "info")
        return RedirectResponse(f"/documents/{existing.id}", status_code=303)

    document = Document(tenant_id=tenant.id, document_type_id=dtype.id, title=dtype.name,
                        jurisdiction=db.get(Tenant, tenant.id).organisation.country,
                        owner_user_id=user.id, derived_status="missing")
    db.add(document)
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="document.created", actor_id=user.id,
                 actor_label=user.email, object_type="document", object_id=document.id,
                 payload={"type": dtype.code, "title": dtype.name})
    db.commit()
    return RedirectResponse(f"/documents/{document.id}/versions/new", status_code=303)


# --------------------------------------------------------------------------
# Document detail
# --------------------------------------------------------------------------
def _get_document(db: Session, tenant: Tenant, document_id: str) -> Document:
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != tenant.id or document.deleted_at:
        raise HTTPException(404, "Document not found")
    return document


@router.get("/documents/{document_id}")
def document_detail(document_id: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    document = _get_document(db, tenant, document_id)
    versions = db.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == document.id)
        .order_by(desc(DocumentVersion.version_no))).all()
    checks = {}
    for version in versions:
        checks[version.id] = db.scalars(select(VerificationCheck).where(
            VerificationCheck.document_version_id == version.id)
            .order_by(desc(VerificationCheck.checked_at)).limit(4)).all()

    groups = []
    for group in db.scalars(select(CertificateGroup).where(
            CertificateGroup.tenant_id == tenant.id).order_by(CertificateGroup.display_order)).all():
        items = db.scalars(select(GroupItem).where(GroupItem.group_id == group.id)).all()
        if any(i.document_id == document.id for i in items) or any(
                i.type_prefix and document.document_type.code.startswith(i.type_prefix)
                for i in items):
            groups.append(group)

    from ..services.access import SENSITIVITY_ORDER
    shared_via = [g for g in groups
                  if SENSITIVITY_ORDER.get(document.document_type.sensitivity, 1)
                  <= SENSITIVITY_ORDER.get(g.sensitivity_ceiling, 1)]

    signals = {v.id: authenticity.for_version(db, v.id) for v in versions}
    return page(request, "document_detail.html", user, tenant, db,
                signals=signals,
                page_title=document.title, crumb=f"Credentials · {document.document_type.code}",
                nav="documents", document=document, dtype=document.document_type,
                versions=versions, checks=checks, groups=shared_via,
                certsearch_supported=certsearch.supports(document.document_type.code),
                verification_result_labels=verification.RESULT_LABELS)


@router.get("/documents/{document_id}/versions/new")
def new_version_form(document_id: str, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    document = _get_document(db, tenant, document_id)
    hint = _extract_hint(document.document_type, "")
    org = db.get(Tenant, tenant.id).organisation
    return page(request, "version_new.html", user, tenant, db,
                page_title=f"Add version · {document.title}", crumb="Credentials",
                nav="documents", document=document, dtype=document.document_type,
                hint=hint, org=org,
                next_no=docsvc.next_version_no(db, document.id))


@router.post("/documents/{document_id}/versions")
async def create_version(document_id: str, request: Request,
                         file: UploadFile = File(None),
                         issue_date: str = Form(""), effective_from: str = Form(""),
                         expiry_date: str = Form(""), certificate_number: str = Form(""),
                         issuer_name: str = Form(""), holder_name: str = Form(""),
                         scope_statement: str = Form(""), cover_amount: str = Form(""),
                         verification_url: str = Form(""), change_reason: str = Form(""),
                         publish: str = Form("1"),
                         db: Session = Depends(get_db), user: User = Depends(require_user),
                         tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.DOCUMENTS_WRITE)
    from ..main import flash
    document = _get_document(db, tenant, document_id)

    file_path = file_name = mime = content_hash = None
    file_size = None
    if file is not None and file.filename:
        data = await file.read()
        if data:
            file_path, content_hash = docsvc.store_upload(tenant.id, file.filename, data)
            file_name, mime, file_size = file.filename, file.content_type, len(data)

    def parse(value: str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    cover = None
    if cover_amount.strip():
        try:
            cover = float(cover_amount.replace(",", ""))
        except ValueError:
            cover = None

    version = docsvc.create_version(
        db, document=document, actor=user,
        issue_date=parse(issue_date), effective_from=parse(effective_from) or date.today(),
        expiry_date=parse(expiry_date), certificate_number=certificate_number.strip() or None,
        issuer_name=issuer_name.strip() or None, holder_name=holder_name.strip() or None,
        scope_statement=scope_statement.strip() or None, cover_amount=cover,
        currency="GBP" if cover else None,
        verification_url=verification_url.strip() or None,
        change_reason=change_reason.strip() or None,
        file_path=file_path, file_name=file_name, mime_type=mime,
        file_size=file_size, content_hash=content_hash)

    if publish == "1":
        docsvc.publish_version(db, version, user)
        verification.run_check(db, version, actor=None)
        docsvc.run_maintenance(db, tenant)
        onboarding.sync_steps(db, tenant)
        onboarding.observe(db, tenant)
        flash(request, f"Version {version.version_no} published and verification run. "
                       f"Assurance is now {version.assurance_level}.")
    else:
        db.commit()
        flash(request, f"Version {version.version_no} saved as a draft.", "info")
    db.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@router.post("/documents/{document_id}/versions/{version_id}/{action}")
def version_action(document_id: str, version_id: str, action: str, request: Request,
                   reason: str = Form(""), notes: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.DOCUMENTS_WRITE)
    from ..main import flash
    document = _get_document(db, tenant, document_id)
    version = db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document.id:
        raise HTTPException(404, "Version not found")

    if action == "publish":
        docsvc.publish_version(db, version, user)
        verification.run_check(db, version, actor=None)
        flash(request, f"Version {version.version_no} is now {version.status}.")
    elif action == "withdraw":
        docsvc.withdraw_version(db, version, user, reason or "No reason given")
        flash(request, f"Version {version.version_no} withdrawn.", "warn")
    elif action == "rollback":
        clone = docsvc.rollback_to(db, version, user)
        flash(request, f"Restored as version {clone.version_no}. History is unchanged — "
                       "rollback is forward-only.")
    elif action == "verify":
        check = verification.run_check(db, version, actor=user)
        authenticity.evaluate(db, version, actor=user)
        tone = "ok" if check.result == "match" else "warn"
        flash(request, f"{verification.RESULT_LABELS.get(check.result, check.result)}. "
                       f"Assurance {version.assurance_level}."
                       + (" Result came from a simulated connector." if check.simulated else ""), tone)
    elif action in {"confirm", "reject"}:
        verification.manual_decision(db, version, user, "confirm" if action == "confirm" else "reject",
                                     notes or "No notes")
        flash(request, f"Manual review recorded. Assurance {version.assurance_level}. "
                       "A human review can reach L2 but never L3 or L4.", "info")
    else:
        raise HTTPException(400, "Unknown action")

    docsvc.run_maintenance(db, tenant)
    onboarding.observe(db, tenant)
    db.commit()
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@router.get("/documents/{document_id}/versions/{version_id}/file")
def download_version(document_id: str, version_id: str, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(require_user),
                     tenant: Tenant = Depends(require_tenant)):
    document = _get_document(db, tenant, document_id)
    version = db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document.id or not version.file_path:
        raise HTTPException(404, "No file is attached to this version")
    path = docsvc.absolute_path(version.file_path)
    if not path.exists():
        raise HTTPException(404, "The stored file is no longer available")
    audit.record(db, tenant_id=tenant.id, action="document.downloaded", actor_id=user.id,
                 actor_label=user.email, object_type="document_version", object_id=version.id,
                 payload={"by": "owner"})
    db.commit()
    return FileResponse(path, filename=version.file_name or "document",
                        media_type=version.mime_type or "application/octet-stream")


@router.post("/documents/{document_id}/hold")
def toggle_hold(document_id: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.HOLD_WRITE)
    from ..main import flash
    document = _get_document(db, tenant, document_id)
    document.legal_hold = not document.legal_hold
    audit.record(db, tenant_id=tenant.id,
                 action="legal_hold.placed" if document.legal_hold else "legal_hold.released",
                 actor_id=user.id, actor_label=user.email,
                 object_type="document", object_id=document.id)
    db.commit()
    flash(request, "Legal hold placed — this document cannot be purged." if document.legal_hold
          else "Legal hold released.", "warn" if document.legal_hold else "ok")
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


# --------------------------------------------------------------------------
# Verification workbench
# --------------------------------------------------------------------------
@router.get("/verification")
def verification_view(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    rows = []
    versions = db.scalars(select(DocumentVersion).where(and_(
        DocumentVersion.tenant_id == tenant.id, DocumentVersion.status == "active"))).all()
    for version in versions:
        document = db.get(Document, version.document_id)
        latest = db.scalar(select(VerificationCheck).where(
            VerificationCheck.document_version_id == version.id)
            .order_by(desc(VerificationCheck.checked_at)).limit(1))
        rows.append({"version": version, "document": document,
                     "type": document.document_type, "check": latest,
                     "due": latest is None or (latest.next_check_due and latest.next_check_due <= now())})
    order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
    rows.sort(key=lambda r: (order.get(r["version"].assurance_level, 0),
                             -(r["type"].criticality or 3)))
    ladder_counts = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}
    for row in rows:
        ladder_counts[row["version"].assurance_level] += 1
    return page(request, "verification.html", user, tenant, db,
                page_title="Verification", crumb="Credentials", nav="verification",
                rows=rows, ladder_counts=ladder_counts,
                due_count=sum(1 for r in rows if r["due"]),
                labels=verification.RESULT_LABELS)


@router.post("/verification/run-all")
def run_all_checks(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.DOCUMENTS_WRITE)
    from ..main import flash
    due = verification.due_for_recheck(db, tenant.id)
    for version in due:
        verification.run_check(db, version, actor=user)
    docsvc.run_maintenance(db, tenant)
    db.commit()
    flash(request, f"Ran {len(due)} check(s). Inconclusive results never downgrade an "
                   "existing assurance level — only a completed negative result does.", "info")
    return RedirectResponse("/verification", status_code=303)
