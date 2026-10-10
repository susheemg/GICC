"""Platform administration.

Unrestricted by design, accountable by construction. Three rules hold
throughout, and they are the reason this surface is safe to have:

  1. Every admin action that touches a tenant writes an event into *that
     tenant's own* activity log, with the admin named. The supplier can see
     everything done to their account, including by us.
  2. Reading a tenant's workspace requires an explicit break-glass session with
     a stated reason and a time box. It expires on its own.
  3. Automated systems may restrict; only a named human may terminate. Every
     suspension and closure records who decided and why.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from ..config import BREAK_GLASS_MINUTES
from ..db import get_db
from ..models import (DocumentTypeProposal, ProfileClaim,
                      AccessGrant, AgentCallLog, ApiClient, AuditEvent, BreakGlassSession,
                      CertificateGroup, ClientAccount, Document, DocumentType,
                      DocumentVersion, Organisation, PlatformAdmin, ShareLinkView,
                      Tenant, User, VerificationCheck, now)
from ..security import hash_password, require_admin, verify_password
from ..services import audit, documents as docsvc
from ..services.roles import ROLE_ORDER, ROLES, role_label

router = APIRouter(prefix="/admin", tags=["Platform admin"])


def admin_page(request: Request, template: str, admin: PlatformAdmin, db: Session, **ctx):
    """Route context wins over the defaults. Injecting `counts` on top of a
    route's own `counts` silently replaced it and the template failed at render
    time rather than anywhere near the cause."""
    from ..main import render
    base = {"admin": admin, "counts": platform_counts(db)}
    base.update(ctx)
    return render(request, template, base)


def platform_counts(db: Session) -> dict:
    return {
        "tenants": db.scalar(select(func.count(Tenant.id))) or 0,
        "active": db.scalar(select(func.count(Tenant.id)).where(
            Tenant.lifecycle_state == "active")) or 0,
        "suspended": db.scalar(select(func.count(Tenant.id)).where(
            Tenant.lifecycle_state == "suspended")) or 0,
        "users": db.scalar(select(func.count(User.id))) or 0,
        "open_holds": db.scalar(select(func.count(Document.id)).where(
            Document.legal_hold.is_(True))) or 0,
        "claims": db.scalar(select(func.count(ProfileClaim.id)).where(
            ProfileClaim.status == "open")) or 0,
        "proposals": db.scalar(select(func.count(DocumentTypeProposal.id)).where(
            DocumentTypeProposal.status == "proposed")) or 0,
    }


def tenant_event(db: Session, admin: PlatformAdmin, tenant_id: str, action: str,
                 request: Request, object_type: str | None = None,
                 object_id: str | None = None, payload: dict | None = None) -> None:
    """Writes into the tenant's own log. This is the whole accountability model:
    the supplier sees platform actions in the same feed as their own."""
    audit.record(db, tenant_id=tenant_id, action=action, actor_kind="platform_admin",
                 actor_id=admin.id, actor_label=admin.email,
                 object_type=object_type, object_id=object_id,
                 ip=request.client.host if request.client else None, payload=payload)


# --------------------------------------------------------------------------
# Sign in
# --------------------------------------------------------------------------
@router.get("/login")
def admin_login_form(request: Request):
    from ..main import render
    if request.session.get("admin_id"):
        return RedirectResponse("/admin", status_code=303)
    return render(request, "admin_login.html", {})


@router.post("/login")
def admin_login(request: Request, email: str = Form(...), password: str = Form(...),
                db: Session = Depends(get_db)):
    from ..main import flash
    admin = db.scalar(select(PlatformAdmin).where(PlatformAdmin.email == email.strip().lower()))
    if not admin or not verify_password(password, admin.password_hash) or not admin.is_active:
        flash(request, "That email and password combination did not match an operator account.", "bad")
        return RedirectResponse("/admin/login", status_code=303)
    admin.last_login_at = now()
    db.commit()
    request.session["admin_id"] = admin.id
    request.session.pop("user_id", None)
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
def admin_logout(request: Request):
    request.session.pop("admin_id", None)
    request.session.pop("break_glass_id", None)
    request.session.pop("user_id", None)
    return RedirectResponse("/admin/login", status_code=303)


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------
@router.get("")
def admin_home(request: Request, db: Session = Depends(get_db),
               admin: PlatformAdmin = Depends(require_admin)):
    tenants = db.scalars(select(Tenant).order_by(desc(Tenant.created_at))).all()
    rows = []
    for tenant in tenants:
        docs = db.scalar(select(func.count(Document.id)).where(and_(
            Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))) or 0
        grants = db.scalar(select(func.count(AccessGrant.id)).where(and_(
            AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))) or 0
        users = db.scalar(select(func.count(User.id)).where(User.tenant_id == tenant.id)) or 0
        rows.append({"tenant": tenant, "documents": docs, "grants": grants, "users": users})

    ladder = {level: db.scalar(select(func.count(DocumentVersion.id)).where(and_(
        DocumentVersion.status == "active", DocumentVersion.assurance_level == level))) or 0
        for level in ["L0", "L1", "L2", "L3", "L4"]}

    stale = db.scalar(select(func.count(VerificationCheck.id)).where(
        VerificationCheck.result.in_(["unreachable", "error"]))) or 0
    negative = db.scalar(select(func.count(VerificationCheck.id)).where(
        VerificationCheck.result.in_(["no_match", "not_found", "suspended",
                                      "withdrawn", "link_dead"]))) or 0
    agent_calls = db.scalar(select(func.count(AgentCallLog.id)).where(
        AgentCallLog.occurred_at >= now() - timedelta(days=7))) or 0
    live_glass = db.scalars(select(BreakGlassSession).where(
        BreakGlassSession.ended_at.is_(None)).order_by(desc(BreakGlassSession.started_at))).all()
    recent = db.scalars(select(AuditEvent).order_by(desc(AuditEvent.occurred_at)).limit(15)).all()
    tenant_names = {t.id: t.name for t in tenants}

    return admin_page(request, "admin_dashboard.html", admin, db,
                      page_title="Platform console", rows=rows, ladder=ladder,
                      stale=stale, negative=negative, agent_calls=agent_calls,
                      live_glass=live_glass, recent=recent, tenant_names=tenant_names,
                      nav="console")


# --------------------------------------------------------------------------
# Tenants
# --------------------------------------------------------------------------
@router.get("/tenants")
def admin_tenants(request: Request, q: str = "", state: str = "",
                  db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    query = select(Tenant)
    if state:
        query = query.where(Tenant.lifecycle_state == state)
    if q:
        query = query.where(Tenant.name.ilike(f"%{q.strip()}%"))
    tenants = db.scalars(query.order_by(Tenant.name)).all()
    rows = []
    for tenant in tenants:
        org = db.get(Organisation, tenant.organisation_id)
        rows.append({
            "tenant": tenant, "org": org,
            "documents": db.scalar(select(func.count(Document.id)).where(and_(
                Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))) or 0,
            "users": db.scalar(select(func.count(User.id)).where(
                User.tenant_id == tenant.id)) or 0,
            "grants": db.scalar(select(func.count(AccessGrant.id)).where(and_(
                AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))) or 0,
        })
    # duplicate legal entities - P-06 adjudication queue
    duplicates = db.execute(
        select(Organisation.country, Organisation.registry_number, func.count(Organisation.id))
        .where(Organisation.registry_number.isnot(None))
        .group_by(Organisation.country, Organisation.registry_number)
        .having(func.count(Organisation.id) > 1)).all()
    return admin_page(request, "admin_tenants.html", admin, db, page_title="Entities",
                      rows=rows, q=q, state=state, duplicates=duplicates, nav="tenants")


@router.get("/tenants/{tenant_id}")
def admin_tenant_detail(tenant_id: str, request: Request, db: Session = Depends(get_db),
                        admin: PlatformAdmin = Depends(require_admin)):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Entity not found")
    org = db.get(Organisation, tenant.organisation_id)
    users = db.scalars(select(User).where(User.tenant_id == tenant.id)
                       .order_by(User.created_at)).all()
    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()
    documents = []
    for document, dtype in rows:
        version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        documents.append({"document": document, "type": dtype, "version": version})
    documents.sort(key=lambda r: r["type"].code)
    clients = db.scalars(select(ClientAccount).where(
        ClientAccount.tenant_id == tenant.id)).all()
    grants = db.scalars(select(AccessGrant).where(and_(
        AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all()
    keys = db.scalars(select(ApiClient).where(ApiClient.tenant_id == tenant.id)).all()
    events = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
                        .order_by(desc(AuditEvent.seq)).limit(40)).all()
    glass = db.scalars(select(BreakGlassSession).where(
        BreakGlassSession.tenant_id == tenant.id)
        .order_by(desc(BreakGlassSession.started_at)).limit(10)).all()
    intact, checked, broken = audit.verify_chain(db, tenant.id)
    return admin_page(request, "admin_tenant_detail.html", admin, db,
                      page_title=tenant.name, tenant=tenant, org=org, users=users,
                      documents=documents, clients=clients, grants=grants, keys=keys,
                      events=events, glass=glass, intact=intact, checked=checked,
                      roles=ROLES, role_order=ROLE_ORDER, nav="tenants",
                      break_glass_minutes=BREAK_GLASS_MINUTES)


@router.post("/tenants/{tenant_id}/suspend")
def suspend_tenant(tenant_id: str, request: Request, reason: str = Form(...),
                   ground: str = Form("terms_breach"), db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    """P-47. Disclosure freezes immediately; nothing is deleted."""
    from ..main import flash
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Entity not found")
    tenant.lifecycle_state = "suspended"
    tenant.suspended_at = now()
    tenant.suspension_reason = f"{ground}: {reason}"
    tenant.suspended_by = admin.id
    tenant_event(db, admin, tenant.id, "tenant.suspended", request, "tenant", tenant.id,
                 {"ground": ground, "reason": reason, "decided_by": admin.email})
    for grant in db.scalars(select(AccessGrant).where(and_(
            AccessGrant.tenant_id == tenant.id, AccessGrant.status == "active"))).all():
        tenant_event(db, admin, tenant.id, "supplier.access_frozen", request,
                     "access_grant", grant.id, {"ground": ground})
    db.commit()
    flash(request, f"{tenant.name} suspended. Disclosure is frozen and every grantee "
                   "notification is recorded. Nothing has been deleted.", "warn")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/reinstate")
def reinstate_tenant(tenant_id: str, request: Request, note: str = Form(""),
                     db: Session = Depends(get_db),
                     admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Entity not found")
    tenant.lifecycle_state = "active"
    tenant.suspended_at = None
    tenant.suspension_reason = None
    tenant_event(db, admin, tenant.id, "tenant.reinstated", request, "tenant", tenant.id,
                 {"note": note, "decided_by": admin.email})
    db.commit()
    flash(request, f"{tenant.name} reinstated.")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/note")
def tenant_note(tenant_id: str, request: Request, note: str = Form(""),
                db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Entity not found")
    tenant.admin_note = note.strip() or None
    db.commit()
    flash(request, "Note saved. Operator notes are internal and are not shown to the tenant.")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/hold")
def platform_hold(tenant_id: str, request: Request, document_id: str = Form(...),
                  reason: str = Form(...), db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    """P-52. A platform hold cannot be released by the tenant."""
    from ..main import flash
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != tenant_id:
        raise HTTPException(404, "Document not found")
    if document.legal_hold and document.hold_placed_by == "platform":
        document.legal_hold = False
        document.hold_placed_by = None
        action, message = "legal_hold.released", "Platform legal hold released."
    else:
        document.legal_hold = True
        document.hold_placed_by = "platform"
        action, message = "legal_hold.placed", "Platform legal hold placed. The tenant cannot release it."
    tenant_event(db, admin, tenant_id, action, request, "document", document.id,
                 {"reason": reason, "placed_by": "platform", "admin": admin.email})
    db.commit()
    flash(request, message, "warn")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


# --------------------------------------------------------------------------
# Users across entities
# --------------------------------------------------------------------------
@router.get("/users")
def admin_users(request: Request, q: str = "", tenant_id: str = "",
                db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    query = select(User)
    if tenant_id:
        query = query.where(User.tenant_id == tenant_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
    users = db.scalars(query.order_by(User.email)).all()
    tenants = {t.id: t for t in db.scalars(select(Tenant).order_by(Tenant.name)).all()}
    admins = db.scalars(select(PlatformAdmin).order_by(PlatformAdmin.email)).all()
    new_password = request.session.pop("_new_password", None)
    return admin_page(request, "admin_users.html", admin, db, page_title="Users",
                      users=users, tenants=tenants, admins=admins, q=q,
                      tenant_id=tenant_id, roles=ROLES, role_order=ROLE_ORDER,
                      new_password=new_password, nav="users")


@router.post("/users")
def create_user(request: Request, tenant_id: str = Form(...), email: str = Form(...),
                full_name: str = Form(...), role: str = Form("viewer"),
                sensitive_access: str = Form(""), password: str = Form(...),
                db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "A user already exists with that email.", "bad")
        return RedirectResponse("/admin/users", status_code=303)
    if len(password) < 8:
        flash(request, "Set a password of at least 8 characters.", "bad")
        return RedirectResponse("/admin/users", status_code=303)
    user = User(tenant_id=tenant_id, email=email, full_name=full_name.strip(),
                password_hash=hash_password(password),
                role=role if role in ROLES else "viewer",
                sensitive_access=sensitive_access == "on")
    db.add(user)
    db.flush()
    tenant_event(db, admin, tenant_id, "user.created_by_platform", request, "user", user.id,
                 {"email": email, "role": user.role, "admin": admin.email})
    db.commit()
    flash(request, f"{email} created as {role_label(user.role)}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/update")
def update_user(user_id: str, request: Request, role: str = Form(...),
                sensitive_access: str = Form(""), full_name: str = Form(""),
                db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    before = {"role": user.role, "sensitive_access": user.sensitive_access}
    user.role = role if role in ROLES else user.role
    user.sensitive_access = sensitive_access == "on"
    if full_name.strip():
        user.full_name = full_name.strip()
    tenant_event(db, admin, user.tenant_id, "user.role_changed_by_platform", request,
                 "user", user.id, {"email": user.email, "from": before,
                                   "to": {"role": user.role,
                                          "sensitive_access": user.sensitive_access},
                                   "admin": admin.email})
    db.commit()
    flash(request, f"{user.email} is now {role_label(user.role)}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, request: Request, new_password: str = Form(...),
                   db: Session = Depends(get_db),
                   admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if len(new_password) < 8:
        flash(request, "Set a password of at least 8 characters.", "bad")
        return RedirectResponse("/admin/users", status_code=303)
    user.password_hash = hash_password(new_password)
    tenant_event(db, admin, user.tenant_id, "user.password_reset_by_platform", request,
                 "user", user.id, {"email": user.email, "admin": admin.email})
    db.commit()
    request.session["_new_password"] = f"{user.email} → {new_password}"
    flash(request, "Password reset. The tenant can see in their activity log that the "
                   "platform did this.", "warn")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle-active")
def toggle_user(user_id: str, request: Request, db: Session = Depends(get_db),
                admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.is_active = not user.is_active
    tenant_event(db, admin, user.tenant_id,
                 "user.enabled_by_platform" if user.is_active else "user.disabled_by_platform",
                 request, "user", user.id, {"email": user.email, "admin": admin.email})
    db.commit()
    flash(request, f"{user.email} {'enabled' if user.is_active else 'disabled'}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/move")
def move_user(user_id: str, request: Request, tenant_id: str = Form(...),
              db: Session = Depends(get_db), admin: PlatformAdmin = Depends(require_admin)):
    """Used when an entity is restructured or a registration landed in the wrong
    workspace. Both tenants see it in their own log."""
    from ..main import flash
    user = db.get(User, user_id)
    target = db.get(Tenant, tenant_id)
    if user is None or target is None:
        raise HTTPException(404, "User or entity not found")
    origin = user.tenant_id
    user.tenant_id = target.id
    tenant_event(db, admin, origin, "user.moved_out_by_platform", request, "user", user.id,
                 {"email": user.email, "to": target.name, "admin": admin.email})
    tenant_event(db, admin, target.id, "user.moved_in_by_platform", request, "user", user.id,
                 {"email": user.email, "admin": admin.email})
    db.commit()
    flash(request, f"{user.email} moved to {target.name}. Both entities can see it.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/operators")
def create_operator(request: Request, email: str = Form(...), full_name: str = Form(...),
                    title: str = Form("Platform administrator"), password: str = Form(...),
                    db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    email = email.strip().lower()
    if db.scalar(select(PlatformAdmin).where(PlatformAdmin.email == email)):
        flash(request, "An operator already exists with that email.", "bad")
        return RedirectResponse("/admin/users", status_code=303)
    if len(password) < 8:
        flash(request, "Set a password of at least 8 characters.", "bad")
        return RedirectResponse("/admin/users", status_code=303)
    db.add(PlatformAdmin(email=email, full_name=full_name.strip(), title=title.strip(),
                         password_hash=hash_password(password)))
    db.commit()
    flash(request, f"Operator {email} created.")
    return RedirectResponse("/admin/users", status_code=303)


# --------------------------------------------------------------------------
# Break-glass
# --------------------------------------------------------------------------
@router.post("/tenants/{tenant_id}/break-glass")
def start_break_glass(tenant_id: str, request: Request, reason: str = Form(...),
                      ticket_reference: str = Form(""), user_id: str = Form(""),
                      db: Session = Depends(get_db),
                      admin: PlatformAdmin = Depends(require_admin)):
    """P-55. Time-boxed, reason-bound, and announced in the tenant's own log."""
    from ..main import flash
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Entity not found")
    acting = db.get(User, user_id) if user_id else db.scalar(
        select(User).where(User.tenant_id == tenant.id).order_by(User.created_at).limit(1))
    if acting is None:
        flash(request, "That entity has no users to act as. Create one first.", "bad")
        return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)

    session_record = BreakGlassSession(
        admin_id=admin.id, tenant_id=tenant.id, acting_user_id=acting.id,
        reason=reason.strip(), ticket_reference=ticket_reference.strip() or None,
        expires_at=now() + timedelta(minutes=BREAK_GLASS_MINUTES))
    db.add(session_record)
    db.flush()
    tenant_event(db, admin, tenant.id, "support.break_glass", request, "tenant", tenant.id,
                 {"admin": admin.email, "reason": reason,
                  "ticket": ticket_reference or None, "acting_as": acting.email,
                  "expires_at": session_record.expires_at.isoformat()})
    from ..services import notifications
    notifications.on_break_glass(db, tenant, admin.email, reason.strip())
    db.commit()
    request.session["break_glass_id"] = session_record.id
    request.session["user_id"] = acting.id
    flash(request, f"Break-glass open for {BREAK_GLASS_MINUTES} minutes, acting as "
                   f"{acting.email}. {tenant.name} can see this in their activity log.", "warn")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/break-glass/end")
def end_break_glass(request: Request, db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    session_id = request.session.pop("break_glass_id", None)
    request.session.pop("user_id", None)
    if session_id:
        record = db.get(BreakGlassSession, session_id)
        if record and not record.ended_at:
            record.ended_at = now()
            tenant_event(db, admin, record.tenant_id, "support.break_glass_ended", request,
                         "tenant", record.tenant_id,
                         {"admin": admin.email, "actions_taken": record.actions_taken,
                          "duration_minutes": int((now() - record.started_at).total_seconds() // 60)})
            db.commit()
    flash(request, "Break-glass session closed.")
    return RedirectResponse("/admin", status_code=303)


# --------------------------------------------------------------------------
# Catalogue and platform-wide log
# --------------------------------------------------------------------------
@router.get("/catalogue")
def admin_catalogue(request: Request, q: str = "", domain: str = "",
                    db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..catalogue import DOMAINS
    query = select(DocumentType)
    if domain:
        query = query.where(DocumentType.domain_code == domain)
    if q:
        query = query.where(DocumentType.name.ilike(f"%{q.strip()}%"))
    types = db.scalars(query.order_by(DocumentType.code)).all()
    usage = {code: count for code, count in db.execute(
        select(DocumentType.code, func.count(Document.id))
        .join(Document, Document.document_type_id == DocumentType.id)
        .group_by(DocumentType.code)).all()}
    return admin_page(request, "admin_catalogue.html", admin, db,
                      page_title="Document catalogue", types=types, usage=usage,
                      domains=DOMAINS, q=q, domain=domain, nav="catalogue")


@router.post("/catalogue/{code}/toggle")
def toggle_type(code: str, request: Request, db: Session = Depends(get_db),
                admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    dtype = db.scalar(select(DocumentType).where(DocumentType.code == code))
    if dtype is None:
        raise HTTPException(404, "Type not found")
    dtype.is_active = not dtype.is_active
    db.commit()
    flash(request, f"{code} {'enabled' if dtype.is_active else 'retired'}. Existing "
                   "documents of this type are unaffected.")
    return RedirectResponse("/admin/catalogue", status_code=303)


@router.get("/audit")
def admin_audit(request: Request, action: str = "", tenant_id: str = "",
                actor_kind: str = "", db: Session = Depends(get_db),
                admin: PlatformAdmin = Depends(require_admin)):
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action.like(f"{action}%"))
    if tenant_id:
        query = query.where(AuditEvent.tenant_id == tenant_id)
    if actor_kind:
        query = query.where(AuditEvent.actor_kind == actor_kind)
    events = db.scalars(query.order_by(desc(AuditEvent.occurred_at)).limit(300)).all()
    tenants = {t.id: t.name for t in db.scalars(select(Tenant)).all()}
    actions = sorted({a for a in db.scalars(select(AuditEvent.action).distinct()).all()})
    return admin_page(request, "admin_audit.html", admin, db, page_title="Platform log",
                      events=events, tenants=tenants, actions=actions, action=action,
                      tenant_id=tenant_id, actor_kind=actor_kind, nav="audit")


# --------------------------------------------------------------------------
# AI provider configuration
# --------------------------------------------------------------------------
@router.get("/ai")
def ai_providers(request: Request, db: Session = Depends(get_db),
                 admin: PlatformAdmin = Depends(require_admin)):
    from ..config import ANTHROPIC_API_KEY
    from ..models import AiInference
    from ..services import providers

    configs = providers.all_configs(db)
    recent = db.scalars(select(AiInference).order_by(desc(AiInference.created_at))
                        .limit(20)).all()
    by_source = {
        "model": db.scalar(select(func.count(AiInference.id)).where(
            AiInference.source == "model")) or 0,
        "heuristic": db.scalar(select(func.count(AiInference.id)).where(
            AiInference.source == "heuristic")) or 0,
        "errors": db.scalar(select(func.count(AiInference.id)).where(
            AiInference.error.isnot(None))) or 0,
    }
    tenant_names = {t.id: t.name for t in db.scalars(select(Tenant)).all()}
    return admin_page(request, "admin_ai.html", admin, db, page_title="AI providers",
                      configs=configs, catalogue=providers.PROVIDERS,
                      status=providers.status_summary(db), recent=recent,
                      by_source=by_source, tenant_names=tenant_names,
                      env_key_present=bool(ANTHROPIC_API_KEY), nav="ai")


@router.post("/ai")
def create_provider(request: Request, provider: str = Form(...), label: str = Form(""),
                    model: str = Form(""), base_url: str = Form(""),
                    api_key: str = Form(""), max_tokens: int = Form(1500),
                    data_boundary: str = Form("third_party"), notes: str = Form(""),
                    enable: str = Form(""), db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    from ..models import AiProviderConfig
    from ..services import providers

    meta = providers.provider_meta(provider)
    if provider not in providers.PROVIDERS:
        flash(request, "Unknown provider.", "bad")
        return RedirectResponse("/admin/ai", status_code=303)

    config = AiProviderConfig(
        provider=provider, label=label.strip() or meta["label"],
        model=model.strip() or meta["default_model"],
        base_url=base_url.strip() or meta["default_base_url"],
        max_tokens=max(64, min(int(max_tokens), 8000)),
        data_boundary=data_boundary, notes=notes.strip() or None,
        created_by=admin.email,
        priority=len(providers.all_configs(db)) + 1)

    if api_key.strip():
        config.api_key_encrypted = providers.encrypt(api_key.strip())
        config.api_key_mask = providers.mask(api_key.strip())
        config.api_key_fingerprint = providers.fingerprint(api_key.strip())

    db.add(config)
    db.flush()

    if enable == "on":
        for other in providers.all_configs(db):
            other.enabled = other.id == config.id
    audit.record(db, tenant_id="platform", action="ai_provider.created",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="ai_provider_config", object_id=config.id,
                 payload={"provider": provider, "model": config.model,
                          "key_fingerprint": config.api_key_fingerprint,
                          "boundary": data_boundary})
    db.commit()

    result = providers.test_connection(db, config, actor=admin)
    db.commit()
    flash(request, f"{config.label} saved. Connection test: "
                   f"{'passed' if result['ok'] else 'failed'} — {result['detail']}",
          "ok" if result["ok"] else "warn")
    return RedirectResponse("/admin/ai", status_code=303)


@router.post("/ai/{config_id}/update")
def update_provider(config_id: str, request: Request, model: str = Form(""),
                    base_url: str = Form(""), api_key: str = Form(""),
                    max_tokens: int = Form(1500), notes: str = Form(""),
                    data_boundary: str = Form("third_party"),
                    db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    from ..models import AiProviderConfig
    from ..services import providers

    config = db.get(AiProviderConfig, config_id)
    if config is None:
        raise HTTPException(404, "Provider configuration not found")

    changed = []
    if model.strip() and model.strip() != config.model:
        changed.append("model")
        config.model = model.strip()
    if base_url.strip() != (config.base_url or ""):
        changed.append("base_url")
        config.base_url = base_url.strip() or None
    if data_boundary != config.data_boundary:
        changed.append("data_boundary")
        config.data_boundary = data_boundary
    config.max_tokens = max(64, min(int(max_tokens), 8000))
    config.notes = notes.strip() or None

    # A blank key field leaves the stored key alone, so an operator editing the
    # model does not have to paste the secret again.
    if api_key.strip():
        changed.append("api_key")
        config.api_key_encrypted = providers.encrypt(api_key.strip())
        config.api_key_mask = providers.mask(api_key.strip())
        config.api_key_fingerprint = providers.fingerprint(api_key.strip())

    audit.record(db, tenant_id="platform", action="ai_provider.updated",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="ai_provider_config", object_id=config.id,
                 payload={"fields": changed,
                          "key_fingerprint": config.api_key_fingerprint})
    db.commit()
    flash(request, f"{config.label} updated"
                   + (" — re-test the connection." if changed else "."))
    return RedirectResponse("/admin/ai", status_code=303)


@router.post("/ai/{config_id}/test")
def test_provider(config_id: str, request: Request, db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    from ..models import AiProviderConfig
    from ..services import providers

    config = db.get(AiProviderConfig, config_id)
    if config is None:
        raise HTTPException(404, "Provider configuration not found")
    result = providers.test_connection(db, config, actor=admin)
    db.commit()
    flash(request, f"{config.label}: {result['detail']}", "ok" if result["ok"] else "bad")
    return RedirectResponse("/admin/ai", status_code=303)


@router.post("/ai/{config_id}/enable")
def enable_provider(config_id: str, request: Request, db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    """One provider active at a time. Silent failover would make an inference
    log ambiguous about which model produced a proposal."""
    from ..main import flash
    from ..models import AiProviderConfig
    from ..services import providers

    config = db.get(AiProviderConfig, config_id)
    if config is None:
        raise HTTPException(404, "Provider configuration not found")

    turning_on = not config.enabled
    if turning_on and not config.last_test_ok:
        flash(request, "Test the connection before enabling it — an untested provider "
                       "fails during a document upload instead of here.", "warn")
        return RedirectResponse("/admin/ai", status_code=303)

    for other in providers.all_configs(db):
        other.enabled = turning_on and other.id == config.id
    audit.record(db, tenant_id="platform",
                 action="ai_provider.enabled" if turning_on else "ai_provider.disabled",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="ai_provider_config", object_id=config.id,
                 payload={"provider": config.provider, "model": config.model,
                          "boundary": config.data_boundary})
    db.commit()
    flash(request, f"{config.label} is now {'the active provider' if turning_on else 'disabled'}."
          + (" Every AI feature falls back to deterministic rules." if not turning_on else
             " Document text will be sent to this provider — confirm your DPA and "
             "subprocessor list are current."),
          "warn" if turning_on else "ok")
    return RedirectResponse("/admin/ai", status_code=303)


@router.post("/ai/{config_id}/delete")
def delete_provider(config_id: str, request: Request, db: Session = Depends(get_db),
                    admin: PlatformAdmin = Depends(require_admin)):
    from ..main import flash
    from ..models import AiProviderConfig

    config = db.get(AiProviderConfig, config_id)
    if config is None:
        raise HTTPException(404, "Provider configuration not found")
    label = config.label
    audit.record(db, tenant_id="platform", action="ai_provider.deleted",
                 actor_kind="platform_admin", actor_id=admin.id, actor_label=admin.email,
                 object_type="ai_provider_config", object_id=config.id,
                 payload={"provider": config.provider,
                          "key_fingerprint": config.api_key_fingerprint})
    db.delete(config)
    db.commit()
    flash(request, f"{label} removed. Stored key destroyed with it.", "warn")
    return RedirectResponse("/admin/ai", status_code=303)


# --------------------------------------------------------------------------
# Platform analytics - GICC-06 F25
# --------------------------------------------------------------------------
@router.get("/analytics")
def adm_analytics(request: Request, period: str = "30", section: str = "overview",
                  db: Session = Depends(get_db),
                  admin: PlatformAdmin = Depends(require_admin)):
    from ..services import analytics

    stats = analytics.compile_all(db, period if period in analytics.PERIODS else "30")
    return admin_page(request, "admin_analytics.html", admin, db,
                      page_title="Platform analytics", stats=stats,
                      periods=analytics.PERIODS, period=stats["period"],
                      section=section, nav="analytics")


@router.get("/analytics/export")
def adm_analytics_export(request: Request, period: str = "30",
                         db: Session = Depends(get_db),
                         admin: PlatformAdmin = Depends(require_admin)):
    """The full payload without the SVG blobs, for analysis elsewhere."""
    from fastapi.responses import JSONResponse

    from ..services import analytics

    payload = analytics.as_json(analytics.compile_all(db, period))
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition":
                 f'attachment; filename="gicc-platform-stats-{period}.json"'})


@router.get("/analytics/tenants")
def adm_analytics_tenants(request: Request, metric: str = "at_risk",
                          db: Session = Depends(get_db),
                          admin: PlatformAdmin = Depends(require_admin)):
    """Every aggregate traces back to the tenants behind it (F25.6)."""
    from ..models import OnboardingProgress
    from ..services import analytics

    rows, title = [], metric.replace("_", " ").title()
    if metric == "at_risk":
        rows = [{"tenant": r["tenant"], "detail": f"{r['band']} · {r['score']:.0f}",
                 "note": r["signals"][0] if r["signals"] else ""}
                for r in analytics.health(db)["at_risk"]]
        title = "Tenants below the healthy band"
    elif metric == "not_activated":
        for progress in db.scalars(select(OnboardingProgress).where(
                OnboardingProgress.m2_at.is_(None))).all():
            tenant = db.get(Tenant, progress.tenant_id)
            if tenant is None:
                continue
            days = (now() - progress.registered_at).days
            rows.append({"tenant": tenant, "detail": f"{days} days since registration",
                         "note": "Reached M1" if progress.m1_at else "No verified credential"})
        title = "Registered but never granted access (no M2)"
    elif metric == "signals":
        from ..models import AuthenticitySignal
        for signal in db.scalars(select(AuthenticitySignal).where(
                AuthenticitySignal.status == "open").order_by(
                AuthenticitySignal.severity)).all():
            tenant = db.get(Tenant, signal.tenant_id)
            if tenant is None:
                continue
            rows.append({"tenant": tenant, "detail": f"{signal.severity} · {signal.code}",
                         "note": signal.detail[:110]})
        title = "Open authenticity signals"
    return admin_page(request, "admin_analytics_tenants.html", admin, db,
                      page_title=title, rows=rows, metric=metric, nav="analytics")
