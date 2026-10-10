from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (AccessGrant, AccessRequest, Assessment, CertificateGroup,
                      ClientAccount, ClientContact, Document, DocumentType, GroupItem,
                      RequirementProfile, ShareLink, ShareLinkView, Tenant, User, now)
from ..security import new_share_token, require_tenant, require_user
from ..services import (access, audit, notifications, onboarding,
                        requirements as reqsvc, roles)
from .web_core import page

router = APIRouter()


# --------------------------------------------------------------------------
# Certificate groups
# --------------------------------------------------------------------------
@router.get("/groups")
def groups(request: Request, db: Session = Depends(get_db),
           user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    rows = []
    for group in db.scalars(select(CertificateGroup).where(
            CertificateGroup.tenant_id == tenant.id)
            .order_by(CertificateGroup.display_order)).all():
        documents = access.group_documents(db, tenant.id, group)
        grants = db.scalars(select(AccessGrant).where(and_(
            AccessGrant.tenant_id == tenant.id, AccessGrant.scope_kind == "group",
            AccessGrant.scope_id == group.id, AccessGrant.status == "active"))).all()
        rows.append({"group": group, "documents": documents, "grants": len(grants)})
    return page(request, "groups.html", user, tenant, db, page_title="Certificate groups",
                crumb="Credentials", nav="groups", rows=rows)


@router.get("/groups/{group_id}")
def group_detail(group_id: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    group = db.get(CertificateGroup, group_id)
    if group is None or group.tenant_id != tenant.id:
        raise HTTPException(404, "Group not found")
    documents = access.group_documents(db, tenant.id, group)
    items = db.scalars(select(GroupItem).where(GroupItem.group_id == group.id)).all()
    grants = db.scalars(select(AccessGrant).where(and_(
        AccessGrant.scope_kind == "group", AccessGrant.scope_id == group.id))).all()
    clients = {c.id: c for c in db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()}
    excluded = []
    from ..services.access import SENSITIVITY_ORDER
    for item in items:
        if item.type_prefix:
            for dtype in db.scalars(select(DocumentType).where(
                    DocumentType.code.startswith(item.type_prefix))).all():
                if SENSITIVITY_ORDER.get(dtype.sensitivity, 1) > SENSITIVITY_ORDER.get(
                        group.sensitivity_ceiling, 1):
                    excluded.append(dtype)
    return page(request, "group_detail.html", user, tenant, db, page_title=group.name,
                crumb="Certificate groups", nav="groups", group=group, documents=documents,
                items=items, grants=grants, clients=clients, excluded=excluded[:8])


@router.post("/groups/{group_id}/policy")
def update_policy(group_id: str, request: Request, nda_required: str = Form(""),
                  watermark_required: str = Form(""), download_allowed: str = Form(""),
                  max_grant_days: int = Form(90), db: Session = Depends(get_db),
                  user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GROUPS_WRITE)
    from ..main import flash
    group = db.get(CertificateGroup, group_id)
    if group is None or group.tenant_id != tenant.id:
        raise HTTPException(404, "Group not found")
    group.nda_required = nda_required == "on"
    group.watermark_required = watermark_required == "on"
    group.download_allowed = download_allowed == "on"
    group.max_grant_days = max(1, min(int(max_grant_days), 3650))
    audit.record(db, tenant_id=tenant.id, action="group.policy_changed", actor_id=user.id,
                 actor_label=user.email, object_type="certificate_group", object_id=group.id,
                 payload={"nda": group.nda_required, "watermark": group.watermark_required,
                          "download": group.download_allowed, "max_days": group.max_grant_days})
    db.commit()
    flash(request, "Policy saved. Existing grants keep the terms they were issued under; "
                   "new grants use the updated ceiling.")
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
@router.get("/clients")
def clients(request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    rows = []
    for client in db.scalars(select(ClientAccount).where(
            ClientAccount.tenant_id == tenant.id).order_by(ClientAccount.name)).all():
        grants = db.scalars(select(AccessGrant).where(and_(
            AccessGrant.client_id == client.id, AccessGrant.status == "active"))).all()
        live = [g for g in grants if access.grant_is_live(g)]
        views = db.scalars(select(ShareLinkView).where(and_(
            ShareLinkView.tenant_id == tenant.id)).order_by(desc(ShareLinkView.viewed_at))).all()
        docs = access.resolve_for_client(db, tenant.id, client.id)
        soonest = min((g.ends_at for g in live if g.ends_at), default=None)
        rows.append({"client": client, "grants": live, "documents": len(docs),
                     "next_expiry": soonest,
                     "last_view": next((v.viewed_at for v in views), None)})
    return page(request, "clients.html", user, tenant, db, page_title="Clients",
                crumb="Disclosure", nav="clients", rows=rows)


@router.post("/clients")
def create_client(request: Request, name: str = Form(...),
                  relationship_type: str = Form("customer"), country: str = Form("GB"),
                  internal_reference: str = Form(""), contact_email: str = Form(""),
                  contact_name: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.CLIENTS_WRITE)
    from ..main import flash
    client = ClientAccount(tenant_id=tenant.id, name=name.strip(),
                           relationship_type=relationship_type, country=country,
                           internal_reference=internal_reference.strip() or None)
    db.add(client)
    db.flush()
    if contact_email.strip():
        db.add(ClientContact(tenant_id=tenant.id, client_id=client.id,
                             email=contact_email.strip().lower(),
                             full_name=contact_name.strip() or None))
    audit.record(db, tenant_id=tenant.id, action="client.created", actor_id=user.id,
                 actor_label=user.email, object_type="client_account", object_id=client.id,
                 payload={"name": client.name, "relationship": relationship_type})
    db.commit()
    flash(request, f"{client.name} added. Grant them a certificate group next.")
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(client_id: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    client = db.get(ClientAccount, client_id)
    if client is None or client.tenant_id != tenant.id:
        raise HTTPException(404, "Client not found")
    contacts = db.scalars(select(ClientContact).where(
        ClientContact.client_id == client.id)).all()
    grants = db.scalars(select(AccessGrant).where(
        AccessGrant.client_id == client.id).order_by(desc(AccessGrant.created_at))).all()
    groups = {g.id: g for g in db.scalars(select(CertificateGroup).where(
        CertificateGroup.tenant_id == tenant.id).order_by(CertificateGroup.display_order)).all()}
    links = db.scalars(select(ShareLink).where(and_(
        ShareLink.tenant_id == tenant.id,
        ShareLink.grant_id.in_([g.id for g in grants] or ["-"])))).all()
    views = db.scalars(select(ShareLinkView).where(and_(
        ShareLinkView.tenant_id == tenant.id,
        ShareLinkView.share_link_id.in_([l.id for l in links] or ["-"])))
        .order_by(desc(ShareLinkView.viewed_at)).limit(25)).all()
    visible = access.resolve_for_client(db, tenant.id, client.id)
    new_link = request.session.pop("_new_share_link", None)
    base_url = str(request.base_url).rstrip("/")
    return page(request, "client_detail.html", user, tenant, db, page_title=client.name,
                crumb="Disclosure · Clients", nav="clients", client=client,
                contacts=contacts, grants=grants, groups=groups, links=links,
                views=views, visible=list(visible.values()), new_link=new_link,
                base_url=base_url, grant_is_live=access.grant_is_live)


@router.post("/clients/{client_id}/contacts")
def add_contact(client_id: str, request: Request, email: str = Form(...),
                full_name: str = Form(""), job_title: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.CLIENTS_WRITE)
    from ..main import flash
    client = db.get(ClientAccount, client_id)
    if client is None or client.tenant_id != tenant.id:
        raise HTTPException(404, "Client not found")
    db.add(ClientContact(tenant_id=tenant.id, client_id=client.id,
                         email=email.strip().lower(), full_name=full_name.strip() or None,
                         job_title=job_title.strip() or None))
    audit.record(db, tenant_id=tenant.id, action="client.contact_added", actor_id=user.id,
                 actor_label=user.email, object_type="client_account", object_id=client.id,
                 payload={"email": email})
    db.commit()
    flash(request, "Contact added.")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/grants")
def create_grant(client_id: str, request: Request, group_id: str = Form(...),
                 permission_level: str = Form("view"), days: str = Form(""),
                 purpose: str = Form(...), contact_id: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GRANTS_WRITE)
    from ..main import flash
    client = db.get(ClientAccount, client_id)
    if client is None or client.tenant_id != tenant.id:
        raise HTTPException(404, "Client not found")
    group = db.get(CertificateGroup, group_id)
    if group is None or group.tenant_id != tenant.id:
        raise HTTPException(404, "Group not found")

    requested_days = int(days) if days.strip().isdigit() else None
    grant = access.create_grant(
        db, tenant_id=tenant.id, client_id=client.id, scope_kind="group",
        scope_id=group.id, permission_level=permission_level, days=requested_days,
        purpose=purpose.strip(), actor=user, contact_id=contact_id or None)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)
    db.commit()

    notes = []
    if requested_days and grant.ends_at and requested_days > group.max_grant_days:
        notes.append(f"duration capped at the group ceiling of {group.max_grant_days} days")
    if permission_level != grant.permission_level:
        notes.append("download removed because the group policy disallows it")
    flash(request, f"Access granted to {group.name}."
          + (" Adjusted: " + "; ".join(notes) + "." if notes else ""),
          "warn" if notes else "ok")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/grants/{grant_id}/revoke")
def revoke(grant_id: str, request: Request, reason: str = Form("No reason given"),
           db: Session = Depends(get_db), user: User = Depends(require_user),
           tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GRANTS_WRITE)
    from ..main import flash
    grant = db.get(AccessGrant, grant_id)
    if grant is None or grant.tenant_id != tenant.id:
        raise HTTPException(404, "Grant not found")
    access.revoke_grant(db, grant, user, reason)
    db.commit()
    flash(request, "Grant revoked. Any share links issued against it stopped working "
                   "immediately.", "warn")
    return RedirectResponse(f"/clients/{grant.client_id}", status_code=303)


@router.post("/grants/{grant_id}/share-link")
def create_share_link(grant_id: str, request: Request, days: int = Form(14),
                      label: str = Form(""), max_views: str = Form(""),
                      db: Session = Depends(get_db), user: User = Depends(require_user),
                      tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GRANTS_WRITE)
    from ..main import flash
    grant = db.get(AccessGrant, grant_id)
    if grant is None or grant.tenant_id != tenant.id:
        raise HTTPException(404, "Grant not found")
    if not access.grant_is_live(grant):
        flash(request, "That grant is not live, so a link cannot be issued.", "bad")
        return RedirectResponse(f"/clients/{grant.client_id}", status_code=303)

    token, digest = new_share_token()
    expires = now() + timedelta(days=max(1, min(days, 365)))
    if grant.ends_at and expires > grant.ends_at:
        expires = grant.ends_at
    link = ShareLink(tenant_id=tenant.id, grant_id=grant.id, token_hash=digest,
                     label=label.strip() or None, expires_at=expires,
                     max_views=int(max_views) if max_views.strip().isdigit() else None)
    db.add(link)
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="share_link.created", actor_id=user.id,
                 actor_label=user.email, object_type="share_link", object_id=link.id,
                 grant_id=grant.id, payload={"expires_at": expires.isoformat()})
    db.commit()
    request.session["_new_share_link"] = token
    flash(request, "Share link created. The token is shown once — only its hash is stored.",
          "warn")
    return RedirectResponse(f"/clients/{grant.client_id}", status_code=303)


@router.post("/share-links/{link_id}/revoke")
def revoke_link(link_id: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GRANTS_WRITE)
    from ..main import flash
    link = db.get(ShareLink, link_id)
    if link is None or link.tenant_id != tenant.id:
        raise HTTPException(404, "Link not found")
    link.revoked_at = now()
    grant = db.get(AccessGrant, link.grant_id)
    audit.record(db, tenant_id=tenant.id, action="share_link.revoked", actor_id=user.id,
                 actor_label=user.email, object_type="share_link", object_id=link.id)
    db.commit()
    flash(request, "Link revoked.")
    return RedirectResponse(f"/clients/{grant.client_id}", status_code=303)


@router.post("/clients/{client_id}/offboard")
def offboard(client_id: str, request: Request, reason: str = Form(...),
             db: Session = Depends(get_db), user: User = Depends(require_user),
             tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.CLIENTS_WRITE)
    from ..main import flash
    client = db.get(ClientAccount, client_id)
    if client is None or client.tenant_id != tenant.id:
        raise HTTPException(404, "Client not found")
    count = access.offboard_client(db, tenant.id, client, user, reason)
    db.commit()
    flash(request, f"{client.name} offboarded. {count} grant(s) revoked and a revocation "
                   "record written to the audit trail.", "warn")
    return RedirectResponse("/clients", status_code=303)


# --------------------------------------------------------------------------
# Access requests
# --------------------------------------------------------------------------
@router.get("/requests")
def requests_view(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    rows = db.scalars(select(AccessRequest).where(AccessRequest.tenant_id == tenant.id)
                      .order_by(desc(AccessRequest.created_at))).all()
    clients = {c.id: c for c in db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()}
    groups = {g.id: g for g in db.scalars(select(CertificateGroup).where(
        CertificateGroup.tenant_id == tenant.id)).all()}
    return page(request, "requests.html", user, tenant, db, page_title="Access requests",
                crumb="Disclosure", nav="requests", rows=rows, clients=clients, groups=groups)


@router.post("/requests/{request_id}/decide")
def decide_request(request_id: str, request: Request, decision: str = Form(...),
                   note: str = Form(""), days: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.GRANTS_WRITE)
    from ..main import flash
    access_request = db.get(AccessRequest, request_id)
    if access_request is None or access_request.tenant_id != tenant.id:
        raise HTTPException(404, "Request not found")
    if access_request.status != "open":
        flash(request, "That request has already been decided.", "info")
        return RedirectResponse("/requests", status_code=303)

    access_request.decided_at = now()
    access_request.decision_note = note.strip() or None
    if decision == "approve":
        grant = access.create_grant(
            db, tenant_id=tenant.id, client_id=access_request.client_id,
            scope_kind=access_request.scope_kind, scope_id=access_request.scope_id,
            permission_level="view", days=int(days) if days.strip().isdigit() else None,
            purpose=access_request.purpose or "Approved access request", actor=user)
        access_request.status = "approved"
        access_request.grant_id = grant.id
        flash(request, "Request approved and a grant issued.")
    else:
        access_request.status = "denied"
        flash(request, "Request denied. The reason is recorded and shown to the requester.",
              "warn")
    client = db.get(ClientAccount, access_request.client_id)
    contact = db.scalar(select(ClientContact).where(
        ClientContact.client_id == access_request.client_id))
    notifications.on_access_request_actioned(
        db, tenant, access_request, access_request.status,
        client.name if client else "the client", contact)
    audit.record(db, tenant_id=tenant.id, action="access_request.decided", actor_id=user.id,
                 actor_label=user.email, object_type="access_request",
                 object_id=access_request.id, payload={"decision": access_request.status,
                                                       "note": note})
    db.commit()
    return RedirectResponse("/requests", status_code=303)


# --------------------------------------------------------------------------
# Requirements and gaps
# --------------------------------------------------------------------------
@router.get("/requirements")
def requirements_view(request: Request, profile_id: str = "",
                      db: Session = Depends(get_db), user: User = Depends(require_user),
                      tenant: Tenant = Depends(require_tenant)):
    profiles = db.scalars(select(RequirementProfile).where(
        (RequirementProfile.tenant_id.is_(None)) |
        (RequirementProfile.tenant_id == tenant.id)).order_by(RequirementProfile.name)).all()
    selected = db.get(RequirementProfile, profile_id) if profile_id else (profiles[0] if profiles else None)
    assessment = None
    if selected:
        assessment = db.scalar(select(Assessment).where(and_(
            Assessment.tenant_id == tenant.id, Assessment.profile_id == selected.id))
            .order_by(desc(Assessment.assessed_at)).limit(1))
    gaps = reqsvc.gap_summary(db, tenant)
    return page(request, "requirements.html", user, tenant, db,
                page_title="Requirements & gaps", crumb="Disclosure", nav="requirements",
                profiles=profiles, selected=selected, assessment=assessment, gaps=gaps)


@router.post("/requirements/{profile_id}/evaluate")
def evaluate_profile(profile_id: str, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    roles.require(user, roles.REQUIREMENTS_RUN)
    from ..main import flash
    profile = db.get(RequirementProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Requirement profile not found")
    assessment = reqsvc.evaluate(db, tenant, profile, actor=user)
    db.commit()
    flash(request, f"Evaluated against {profile.name}: {assessment.score}% "
                   f"({assessment.met_count} met, {assessment.gap_count} gaps). "
                   "Every result cites the version it read, so this run is reproducible.")
    return RedirectResponse(f"/requirements?profile_id={profile_id}", status_code=303)
