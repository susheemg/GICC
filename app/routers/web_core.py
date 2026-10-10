from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (AccessGrant, AccessRequest, AgentCallLog, ApiClient, Assessment,
                      AssessmentResult, AuditEvent, CertificateGroup, ClientAccount,
                      ClientContact, Document, DocumentType, DocumentVersion,
                      EntityIdentifier, GroupItem, Organisation, RequirementItem,
                      RequirementProfile, ShareLink, ShareLinkView, Task, Tenant,
                      User, VerificationCheck, now)
from ..security import new_api_key, require_tenant, require_user
from ..services import access, audit, documents as docsvc, requirements as reqsvc, roles

router = APIRouter()


def page(request: Request, template: str, user: User, tenant: Tenant, db: Session, **ctx):
    from ..main import render, sidebar_counts
    from ..security import active_break_glass
    from ..models import PlatformAdmin

    banner = None
    session_record = active_break_glass(request, db)
    if session_record:
        operator = db.get(PlatformAdmin, session_record.admin_id)
        session_record.actions_taken += 1
        db.commit()
        banner = {"admin_email": operator.email if operator else "platform operator",
                  "reason": session_record.reason,
                  "ticket_reference": session_record.ticket_reference,
                  "expires_at": session_record.expires_at}

    from ..services import notifications
    counts = sidebar_counts(db, tenant.id)
    counts["unread"] = notifications.unread_count(db, tenant.id, user.id)
    from ..services import onboarding as onboarding_service
    guide = onboarding_service.state(db, tenant)
    counts["onboarding"] = (0 if guide["complete"] or guide["dismissed"]
                            else guide["total"] - guide["done"])
    ctx.setdefault("guide", guide)
    ctx.update(user=user, tenant=tenant, counts=counts, break_glass=banner)
    return render(request, template, ctx)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    docsvc.run_maintenance(db, tenant)

    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()

    status_counts = {"valid": 0, "expiring_soon": 0, "expired": 0,
                     "suspended": 0, "missing": 0, "withdrawn": 0}
    assurance_counts = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}
    attention = []
    for document, dtype in rows:
        status_counts[document.derived_status] = status_counts.get(document.derived_status, 0) + 1
        version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        if version:
            assurance_counts[version.assurance_level] = assurance_counts.get(version.assurance_level, 0) + 1
        if document.derived_status in {"expired", "suspended", "expiring_soon", "missing"}:
            attention.append({"document": document, "type": dtype, "version": version,
                              "days": (version.expiry_date - date.today()).days
                              if version and version.expiry_date else None})
    attention.sort(key=lambda r: (r["days"] if r["days"] is not None else -999))

    grants = db.scalars(select(AccessGrant).where(and_(
        AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all()
    clients = {c.id: c for c in db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()}
    recent_views = db.scalars(
        select(ShareLinkView).where(ShareLinkView.tenant_id == tenant.id)
        .order_by(desc(ShareLinkView.viewed_at)).limit(8)).all()
    recent_events = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
        .order_by(desc(AuditEvent.seq)).limit(10)).all()
    open_tasks = db.scalars(select(Task).where(and_(
        Task.tenant_id == tenant.id, Task.status == "open"))
        .order_by(Task.due_at).limit(6)).all()

    health, signals = docsvc.health_score(db, tenant)
    band, tone = docsvc.health_band(health)
    gaps = reqsvc.gap_summary(db, tenant)[:5]

    from ..services import onboarding as onboarding_service
    onboarding_service.sync_steps(db, tenant)
    onboarding_service.observe(db, tenant)
    db.commit()
    next_step = onboarding_service.next_action(db, tenant)

    milestones = {
        "m1": assurance_counts.get("L3", 0) + assurance_counts.get("L4", 0) > 0,
        "m2": len(grants) > 0,
        "m3": len(recent_views) > 0 or db.scalar(select(func.count(ShareLinkView.id)).where(
            ShareLinkView.tenant_id == tenant.id)) > 0,
    }

    return page(request, "dashboard.html", user, tenant, db,
                page_title="Home", crumb=tenant.name, nav="dashboard",
                total_docs=len(rows), status_counts=status_counts,
                assurance_counts=assurance_counts, attention=attention[:8],
                grants=grants, clients=clients, recent_views=recent_views,
                recent_events=recent_events, open_tasks=open_tasks,
                health=health, band=band, tone=tone, signals=signals,
                gaps=gaps, milestones=milestones, next_step=next_step)


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------
@router.get("/profile")
def profile(request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    org = db.get(Organisation, tenant.organisation_id)
    identifiers = db.scalars(select(EntityIdentifier).where(
        EntityIdentifier.tenant_id == tenant.id)).all()
    return page(request, "profile.html", user, tenant, db,
                page_title="Company profile", crumb="Profile", nav="profile",
                org=org, identifiers=identifiers)


@router.post("/profile")
def save_profile(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant),
                 legal_name: str = Form(...), trading_name: str = Form(""),
                 country: str = Form("GB"), registry_number: str = Form(""),
                 lei: str = Form(""), vat_number: str = Form(""), duns: str = Form(""),
                 website: str = Form(""), employees: str = Form(""),
                 annual_turnover: str = Form(""), sector_code: str = Form(""),
                 registered_address: str = Form(""), description: str = Form("")):
    roles.require(user, roles.PROFILE_WRITE)
    from ..main import flash
    org = db.get(Organisation, tenant.organisation_id)
    changes = {}
    for field, value in [("legal_name", legal_name.strip()), ("trading_name", trading_name.strip()),
                         ("country", country), ("registry_number", registry_number.strip()),
                         ("lei", lei.strip()), ("vat_number", vat_number.strip()),
                         ("duns", duns.strip()), ("website", website.strip()),
                         ("sector_code", sector_code.strip()),
                         ("registered_address", registered_address.strip()),
                         ("description", description.strip())]:
        if getattr(org, field) != (value or None):
            changes[field] = value
            setattr(org, field, value or None)
    org.employees = int(employees) if employees.strip().isdigit() else org.employees
    try:
        org.annual_turnover = float(annual_turnover.replace(",", "")) if annual_turnover.strip() else org.annual_turnover
    except ValueError:
        pass
    tenant.name = org.legal_name

    audit.record(db, tenant_id=tenant.id, action="profile.updated", actor_id=user.id,
                 actor_label=user.email, object_type="organisation", object_id=org.id,
                 payload={"fields": list(changes)})
    db.commit()
    flash(request, "Profile saved.")
    return RedirectResponse("/profile", status_code=303)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
@router.get("/tasks")
def tasks(request: Request, db: Session = Depends(get_db),
          user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    open_tasks = db.scalars(select(Task).where(and_(
        Task.tenant_id == tenant.id, Task.status == "open")).order_by(Task.due_at)).all()
    done_tasks = db.scalars(select(Task).where(and_(
        Task.tenant_id == tenant.id, Task.status == "done"))
        .order_by(desc(Task.closed_at)).limit(20)).all()
    return page(request, "tasks.html", user, tenant, db, page_title="Tasks",
                crumb="Operate", nav="tasks", open_tasks=open_tasks, done_tasks=done_tasks)


@router.post("/tasks/{task_id}/close")
def close_task(task_id: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.TASKS_WRITE)
    from ..main import flash
    task = db.get(Task, task_id)
    if task and task.tenant_id == tenant.id:
        task.status = "done"
        task.closed_at = now()
        audit.record(db, tenant_id=tenant.id, action="task.closed", actor_id=user.id,
                     actor_label=user.email, object_type="task", object_id=task.id)
        db.commit()
        flash(request, "Task closed.")
    return RedirectResponse("/tasks", status_code=303)


# --------------------------------------------------------------------------
# Activity feed - everything done on this account, by anyone
# --------------------------------------------------------------------------
@router.get("/activity")
def activity(request: Request, actor: str = "", significance: str = "",
             days: int = 90, q: str = "", db: Session = Depends(get_db),
             user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    from ..services import activity as activity_service
    rows = activity_service.feed(db, tenant.id, actor=actor, significance=significance,
                                 days=days, search=q)
    return page(request, "activity.html", user, tenant, db, page_title="Activity",
                crumb="Operate", nav="activity", rows=rows,
                counts_by_actor=activity_service.summary(db, tenant.id),
                receipts=activity_service.buyer_receipts(db, tenant.id),
                # Moved off the dashboard: it belongs with everything else that
                # happened, not on a page meant to show what needs doing.
                recent_views=db.scalars(
                    select(ShareLinkView).where(ShareLinkView.tenant_id == tenant.id)
                    .order_by(desc(ShareLinkView.viewed_at)).limit(15)).all(),
                actor=actor, significance=significance, days=days, q=q)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
@router.get("/audit")
def audit_log(request: Request, action: str = "", db: Session = Depends(get_db),
              user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    query = select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
    if action:
        query = query.where(AuditEvent.action.like(f"{action}%"))
    events = db.scalars(query.order_by(desc(AuditEvent.seq)).limit(250)).all()
    actions = sorted({row for row in db.scalars(
        select(AuditEvent.action).where(AuditEvent.tenant_id == tenant.id).distinct()).all()})
    intact, checked, broken = audit.verify_chain(db, tenant.id)
    return page(request, "audit.html", user, tenant, db, page_title="Audit trail",
                crumb="Operate", nav="audit", events=events, actions=actions,
                selected=action, intact=intact, checked=checked, broken=broken)


# --------------------------------------------------------------------------
# Settings, API clients
# --------------------------------------------------------------------------
@router.get("/settings")
def settings(request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    clients = db.scalars(select(ApiClient).where(ApiClient.tenant_id == tenant.id)).all()
    users = db.scalars(select(User).where(User.tenant_id == tenant.id)).all()
    client_accounts = db.scalars(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == tenant.id, ClientAccount.status == "active"))).all()
    new_key = request.session.pop("_new_api_key", None)
    base_url = str(request.base_url).rstrip("/")
    return page(request, "settings.html", user, tenant, db, page_title="Settings & API",
                crumb="Operate", nav="settings", api_clients=clients, users=users,
                client_accounts=client_accounts, new_key=new_key, base_url=base_url)


@router.post("/settings/api-clients")
def create_api_client(request: Request, name: str = Form(...), scopes: list[str] = Form([]),
                      client_account_id: str = Form(""), db: Session = Depends(get_db),
                      user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.SETTINGS_WRITE)
    from ..main import flash
    key, prefix, digest = new_api_key()
    grant_id = None
    if client_account_id:
        grant = db.scalar(select(AccessGrant).where(and_(
            AccessGrant.tenant_id == tenant.id,
            AccessGrant.client_id == client_account_id,
            AccessGrant.status == "active")))
        grant_id = grant.id if grant else None

    client = ApiClient(tenant_id=tenant.id, name=name.strip(), key_prefix=prefix,
                       key_hash=digest, scopes=" ".join(scopes) or "profile:read documents:read",
                       grant_id=grant_id)
    db.add(client)
    audit.record(db, tenant_id=tenant.id, action="api_client.created", actor_id=user.id,
                 actor_label=user.email, object_type="api_client", object_id=client.id,
                 payload={"name": name, "scopes": client.scopes})
    db.commit()
    request.session["_new_api_key"] = key
    flash(request, "API key created. Copy it now — it is not shown again.", "warn")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/api-clients/{client_id}/revoke")
def revoke_api_client(client_id: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.SETTINGS_WRITE)
    from ..main import flash
    client = db.get(ApiClient, client_id)
    if client and client.tenant_id == tenant.id:
        client.revoked_at = now()
        audit.record(db, tenant_id=tenant.id, action="api_client.revoked", actor_id=user.id,
                     actor_label=user.email, object_type="api_client", object_id=client.id)
        db.commit()
        flash(request, f"Key for {client.name} revoked.")
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------
# Account lifecycle - GICC-04 S11-S13
# --------------------------------------------------------------------------
@router.get("/lifecycle")
def lifecycle(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    grants = db.scalars(select(AccessGrant).where(and_(
        AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all()
    clients = db.scalars(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == tenant.id, ClientAccount.status == "active"))).all()
    doc_count = db.scalar(select(func.count(Document.id)).where(and_(
        Document.tenant_id == tenant.id, Document.deleted_at.is_(None))))
    version_count = db.scalar(select(func.count(DocumentVersion.id)).where(
        DocumentVersion.tenant_id == tenant.id))
    next_expiry = db.scalar(
        select(DocumentVersion.expiry_date).where(and_(
            DocumentVersion.tenant_id == tenant.id, DocumentVersion.status == "active",
            DocumentVersion.expiry_date.isnot(None)))
        .order_by(DocumentVersion.expiry_date).limit(1))
    health, signals = docsvc.health_score(db, tenant)
    band, tone = docsvc.health_band(health)
    grace_ends = (tenant.closing_started_at + timedelta(days=30)) if tenant.closing_started_at else None
    return page(request, "lifecycle.html", user, tenant, db,
                page_title="Account lifecycle", crumb="Operate", nav="lifecycle",
                grants=grants, clients=clients, doc_count=doc_count,
                version_count=version_count, next_expiry=next_expiry,
                health=health, band=band, tone=tone, signals=signals,
                grace_ends=grace_ends)


@router.get("/lifecycle/export")
def export_tenant(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.LIFECYCLE_WRITE)
    """P-49 portability export. Open format, complete, no fee."""
    org = db.get(Organisation, tenant.organisation_id)
    payload = {
        "export_version": "1.0",
        "generated_at": now().isoformat(),
        "tenant": {"id": tenant.id, "name": tenant.name, "slug": tenant.slug,
                   "region": tenant.data_region, "lifecycle_state": tenant.lifecycle_state},
        "organisation": {c.name: getattr(org, c.name) for c in org.__table__.columns},
        "documents": [], "clients": [], "grants": [], "audit": [],
    }
    for document in db.scalars(select(Document).where(Document.tenant_id == tenant.id)).all():
        versions = db.scalars(select(DocumentVersion).where(
            DocumentVersion.document_id == document.id)).all()
        payload["documents"].append({
            "id": document.id, "type_code": document.document_type.code,
            "title": document.title, "status": document.derived_status,
            "versions": [{c.name: getattr(v, c.name) for c in v.__table__.columns}
                         for v in versions]})
    for client in db.scalars(select(ClientAccount).where(ClientAccount.tenant_id == tenant.id)).all():
        contacts = db.scalars(select(ClientContact).where(
            ClientContact.client_id == client.id)).all()
        payload["clients"].append({
            "id": client.id, "name": client.name, "status": client.status,
            "relationship": client.relationship_type,
            "contacts": [{"email": c.email, "name": c.full_name} for c in contacts]})
    for grant in db.scalars(select(AccessGrant).where(AccessGrant.tenant_id == tenant.id)).all():
        payload["grants"].append({c.name: getattr(grant, c.name) for c in grant.__table__.columns})
    for event in db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
                            .order_by(AuditEvent.occurred_at)).all():
        payload["audit"].append({"at": event.occurred_at, "action": event.action,
                                 "actor": event.actor_label, "object": event.object_type,
                                 "object_id": event.object_id, "hash": event.row_hash})

    audit.record(db, tenant_id=tenant.id, action="export.completed", actor_id=user.id,
                 actor_label=user.email, object_type="tenant", object_id=tenant.id,
                 payload={"documents": len(payload["documents"])})
    db.commit()
    body = json.dumps(payload, indent=2, default=str)
    return JSONResponse(
        content=json.loads(body),
        headers={"Content-Disposition": f'attachment; filename="gicc-export-{tenant.slug}.json"'})


@router.post("/lifecycle/close")
def start_closure(request: Request, reason: str = Form(...), confirm: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.LIFECYCLE_WRITE)
    """P-46 voluntary closure. Grants stay live through the 30-day grace window;
    grantees are notified immediately (P-48)."""
    from ..main import flash
    if confirm.strip().upper() != "CLOSE":
        flash(request, 'Type CLOSE in the confirmation box to proceed.', "bad")
        return RedirectResponse("/lifecycle", status_code=303)

    tenant.lifecycle_state = "closing"
    tenant.closing_started_at = now()
    tenant.close_reason = reason
    grants = db.scalars(select(AccessGrant).where(and_(
        AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all()
    notified = {g.client_id for g in grants}

    audit.record(db, tenant_id=tenant.id, action="tenant.closing", actor_id=user.id,
                 actor_label=user.email, object_type="tenant", object_id=tenant.id,
                 payload={"reason": reason, "grants_live": len(grants)})
    for client_id in notified:
        client = db.get(ClientAccount, client_id)
        audit.record(db, tenant_id=tenant.id, action="supplier.exiting", actor_kind="system",
                     actor_label="lifecycle", object_type="client_account", object_id=client_id,
                     payload={"client": client.name if client else client_id,
                              "access_ends_at": (now() + timedelta(days=30)).isoformat(),
                              "download_enabled_for_exit": True})
    db.commit()
    flash(request, f"Closure started. {len(notified)} client(s) notified that access ends in "
                   "30 days, with download enabled for the exit window. You can cancel any "
                   "time before then.", "warn")
    return RedirectResponse("/lifecycle", status_code=303)


@router.post("/lifecycle/cancel-close")
def cancel_closure(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.LIFECYCLE_WRITE)
    from ..main import flash
    tenant.lifecycle_state = "active"
    tenant.closing_started_at = None
    audit.record(db, tenant_id=tenant.id, action="tenant.closure_cancelled", actor_id=user.id,
                 actor_label=user.email, object_type="tenant", object_id=tenant.id)
    db.commit()
    flash(request, "Closure cancelled. Clients have been told access continues.")
    return RedirectResponse("/lifecycle", status_code=303)


@router.post("/lifecycle/purge")
def purge_tenant(request: Request, confirm: str = Form(""), db: Session = Depends(get_db),
                 user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.LIFECYCLE_WRITE)
    """Completes the closure: revokes every grant, removes documents and files,
    keeps the audit skeleton so the hash chain survives (GICC-04 §12.5)."""
    from ..main import flash
    from ..services.documents import absolute_path
    if tenant.lifecycle_state != "closing" or confirm.strip().upper() != "PURGE":
        flash(request, "Start the closure first, then type PURGE to confirm.", "bad")
        return RedirectResponse("/lifecycle", status_code=303)

    held = db.scalar(select(func.count(Document.id)).where(and_(
        Document.tenant_id == tenant.id, Document.legal_hold.is_(True))))
    if held:
        flash(request, f"{held} document(s) are under legal hold. The purge is blocked "
                       "until the hold is released.", "bad")
        return RedirectResponse("/lifecycle", status_code=303)

    for grant in db.scalars(select(AccessGrant).where(and_(
            AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all():
        access.revoke_grant(db, grant, user, "Tenant closed")

    # Crypto-shred stand-in: remove the blobs first, so "deleted" is true at the
    # moment of the request rather than at the end of a batch job.
    files_removed = 0
    for version in db.scalars(select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant.id)).all():
        if version.file_path:
            path = absolute_path(version.file_path)
            if path.exists():
                path.unlink()
                files_removed += 1

    doc_count = db.scalar(select(func.count(Document.id)).where(
        Document.tenant_id == tenant.id)) or 0

    # Ordered deletion. Children before parents, or SQLite rejects the lot.
    version_ids = list(db.scalars(select(DocumentVersion.id).where(
        DocumentVersion.tenant_id == tenant.id)).all()) or ["-"]
    grant_ids = list(db.scalars(select(AccessGrant.id).where(
        AccessGrant.tenant_id == tenant.id)).all()) or ["-"]
    link_ids = list(db.scalars(select(ShareLink.id).where(
        ShareLink.tenant_id == tenant.id)).all()) or ["-"]
    assessment_ids = list(db.scalars(select(Assessment.id).where(
        Assessment.tenant_id == tenant.id)).all()) or ["-"]
    profile_ids = list(db.scalars(select(RequirementProfile.id).where(
        RequirementProfile.tenant_id == tenant.id)).all()) or ["-"]

    for statement in [
        delete(ShareLinkView).where(ShareLinkView.share_link_id.in_(link_ids)),
        delete(ShareLink).where(ShareLink.tenant_id == tenant.id),
        delete(AccessRequest).where(AccessRequest.tenant_id == tenant.id),
        delete(AccessGrant).where(AccessGrant.id.in_(grant_ids)),
        delete(ClientContact).where(ClientContact.tenant_id == tenant.id),
        delete(ClientAccount).where(ClientAccount.tenant_id == tenant.id),
        delete(VerificationCheck).where(VerificationCheck.document_version_id.in_(version_ids)),
        delete(GroupItem).where(GroupItem.tenant_id == tenant.id),
        delete(CertificateGroup).where(CertificateGroup.tenant_id == tenant.id),
        delete(AssessmentResult).where(AssessmentResult.assessment_id.in_(assessment_ids)),
        delete(Assessment).where(Assessment.tenant_id == tenant.id),
        delete(RequirementItem).where(RequirementItem.profile_id.in_(profile_ids)),
        delete(RequirementProfile).where(RequirementProfile.tenant_id == tenant.id),
        delete(DocumentVersion).where(DocumentVersion.tenant_id == tenant.id),
        delete(Document).where(Document.tenant_id == tenant.id),
        delete(Task).where(Task.tenant_id == tenant.id),
        delete(EntityIdentifier).where(EntityIdentifier.tenant_id == tenant.id),
        delete(AgentCallLog).where(AgentCallLog.tenant_id == tenant.id),
        delete(ApiClient).where(ApiClient.tenant_id == tenant.id),
    ]:
        db.execute(statement)
    db.flush()

    tenant.lifecycle_state = "closed"
    tenant.closed_at = now()
    audit.record(db, tenant_id=tenant.id, action="erasure.completed", actor_id=user.id,
                 actor_label=user.email, object_type="tenant", object_id=tenant.id,
                 payload={"documents_purged": doc_count, "files_removed": files_removed,
                          "retained": "audit skeleton, billing records, directory identity"})

    # Scrub personal data from the audit skeleton. The chain hashes actor_id and
    # payload_hash, never the free text, so this leaves it verifiable.
    for event in db.scalars(select(AuditEvent).where(
            AuditEvent.tenant_id == tenant.id)).all():
        event.payload = None
        event.ip = None
        event.actor_label = ("actor:" + event.actor_id[:8]) if event.actor_id else event.actor_kind

    # Users are personal data with no independent lawful basis once the account
    # is gone. The organisation record stays in the directory - it is public
    # register data, and removing it invites duplicate-entity chaos on re-registration.
    for member in db.scalars(select(User).where(User.tenant_id == tenant.id)).all():
        db.delete(member)

    db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
