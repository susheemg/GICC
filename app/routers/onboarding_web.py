"""The guided self-registration journey - GICC-06 F24.

Five steps, each skippable, all resumable. Steps that create real objects hand
off to the existing routes rather than duplicating them: step 3 posts to the
upload flow, step 5 to the client and grant flow. The guide orchestrates; it
does not reimplement.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (AccessGrant, CertificateGroup, ClientAccount, ClientContact,
                      Document, DocumentType, EntityIdentifier, Organisation,
                      Tenant, User, now)
from ..security import new_share_token, require_tenant, require_user
from ..services import access, audit, onboarding, roles
from .web_core import page

router = APIRouter()


def _guard(db: Session, tenant: Tenant, number: int) -> None:
    if number < 1 or number > onboarding.TOTAL_STEPS:
        raise HTTPException(404, "No such step")


@router.get("/start")
def start(request: Request, db: Session = Depends(get_db),
          user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    onboarding.reopen(db, tenant)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)
    db.commit()
    # An overview rather than a redirect into the next step. Being dropped mid-
    # flow with no sense of the whole is what makes setup feel endless; seeing
    # five items with three ticked is what makes it feel nearly done.
    current = onboarding.state(db, tenant)
    return page(request, "start_overview.html", user, tenant, db,
                page_title="Getting started", crumb="Start", nav="start",
                state=current)


@router.get("/start/done")
def start_done(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_user),
               tenant: Tenant = Depends(require_tenant)):
    onboarding.observe(db, tenant)
    db.commit()
    return page(request, "start_done.html", user, tenant, db,
                page_title="You are set up", crumb="Getting started", nav="start",
                state=onboarding.state(db, tenant))


@router.get("/start/{number}")
def step(number: int, request: Request, db: Session = Depends(get_db),
         user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    _guard(db, tenant, number)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)
    db.commit()

    current = onboarding.state(db, tenant)
    org = db.get(Organisation, tenant.organisation_id)
    context = {"state": current, "number": number, "meta": onboarding.STEPS[number],
               "org": org, "sectors": onboarding.SECTORS,
               "buyer_types": onboarding.BUYER_TYPES}

    if number == 2:
        selected = (current["progress"].buyer_types or "").split(",")
        context["selected_buyers"] = [b for b in selected if b]
    if number == 3:
        context["recommended"] = onboarding.recommended_types(db, tenant)
        context["held"] = db.scalar(select(Document).where(and_(
            Document.tenant_id == tenant.id, Document.deleted_at.is_(None))))
    if number == 4:
        context["groups"] = db.scalars(select(CertificateGroup).where(
            CertificateGroup.tenant_id == tenant.id)
            .order_by(CertificateGroup.display_order)).all()
        context["profiles"] = onboarding.profiles_for(db, tenant)
    if number == 5:
        context["groups"] = db.scalars(select(CertificateGroup).where(
            CertificateGroup.tenant_id == tenant.id)
            .order_by(CertificateGroup.display_order)).all()
        context["clients"] = db.scalars(select(ClientAccount).where(
            ClientAccount.tenant_id == tenant.id)).all()
        context["share_token"] = request.session.pop("_start_share_token", None)
        context["base_url"] = str(request.base_url).rstrip("/")

    return page(request, f"start_step{number}.html", user, tenant, db,
                page_title=onboarding.STEPS[number]["title"],
                crumb=f"Getting started · step {number} of {onboarding.TOTAL_STEPS}",
                nav="start", **context)


@router.post("/start/{number}/skip")
def skip_step(number: int, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_user),
              tenant: Tenant = Depends(require_tenant)):
    """A skip is recorded as deliberately as a completion. A step everyone skips
    is a step that should not exist (J2)."""
    from ..main import flash
    _guard(db, tenant, number)
    onboarding.mark(db, tenant, number, "skipped", actor=user)
    db.commit()
    flash(request, "Skipped. You can come back to it from Getting started at any time.",
          "info")
    # An overview rather than a redirect into the next step. Being dropped mid-
    # flow with no sense of the whole is what makes setup feel endless; seeing
    # five items with three ticked is what makes it feel nearly done.
    current = onboarding.state(db, tenant)
    return page(request, "start_overview.html", user, tenant, db,
                page_title="Getting started", crumb="Start", nav="start",
                state=current)


@router.post("/start/dismiss")
def dismiss(request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_user), tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    onboarding.dismiss(db, tenant, actor=user)
    db.commit()
    flash(request, "Guide hidden. Reopen it from Getting started whenever you want.")
    return RedirectResponse("/dashboard", status_code=303)


# --------------------------------------------------------------------------
# Step 1 — confirm the entity
# --------------------------------------------------------------------------
@router.post("/start/1")
def save_entity(request: Request, legal_name: str = Form(...), country: str = Form("GB"),
                registry_number: str = Form(""), trading_name: str = Form(""),
                website: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    roles.require(user, roles.PROFILE_WRITE)
    org = db.get(Organisation, tenant.organisation_id)
    org.legal_name = legal_name.strip() or org.legal_name
    org.country = country
    org.trading_name = trading_name.strip() or None
    org.website = website.strip() or None
    tenant.name = org.legal_name

    number = registry_number.strip()
    if number and number != (org.registry_number or ""):
        org.registry_number = number
        # Progressive verification: the lookup is simulated here, exactly as
        # the rest of the verification layer is, and never blocks the journey.
        org.verification_state = "registry_verified"
        existing = db.scalar(select(EntityIdentifier).where(and_(
            EntityIdentifier.tenant_id == tenant.id,
            EntityIdentifier.kind == "registry")))
        if existing is None:
            db.add(EntityIdentifier(tenant_id=tenant.id, kind="registry", value=number,
                                    verification_state="verified", verified_at=now(),
                                    source="Company registry (simulated)"))
    audit.record(db, tenant_id=tenant.id, action="onboarding.entity_confirmed",
                 actor_id=user.id, actor_label=user.email,
                 payload={"legal_name": org.legal_name,
                          "registry": bool(org.registry_number)})
    onboarding.mark(db, tenant, 1, "complete", actor=user)
    db.commit()
    flash(request, "Company confirmed."
          + (" Registry verified." if org.registry_number else
             " No registry number yet — you can add it later from your profile."))
    return RedirectResponse("/start/2", status_code=303)


# --------------------------------------------------------------------------
# Step 2 — who buys from you
# --------------------------------------------------------------------------
@router.post("/start/2")
def save_buyers(request: Request, sector: str = Form(""),
                buyer_types: list[str] = Form([]), db: Session = Depends(get_db),
                user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    org = db.get(Organisation, tenant.organisation_id)
    if sector:
        org.sector_code = sector
    onboarding.set_profile(db, tenant, sector, buyer_types or ["general"])
    audit.record(db, tenant_id=tenant.id, action="onboarding.buyers_set",
                 actor_id=user.id, actor_label=user.email,
                 payload={"sector": sector, "buyers": buyer_types})
    onboarding.mark(db, tenant, 2, "complete", actor=user)
    db.commit()
    labels = [onboarding.BUYER_TYPES[b][0] for b in (buyer_types or ["general"])
              if b in onboarding.BUYER_TYPES]
    flash(request, "Recommendations set for " + (", ".join(labels) or "general buyers")
          + ". You will see the documents those buyers ask for first.")
    return RedirectResponse("/start/3", status_code=303)


# --------------------------------------------------------------------------
# Step 4 — package
# --------------------------------------------------------------------------
@router.post("/start/4")
def save_package(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_user),
                 tenant: Tenant = Depends(require_tenant)):
    from ..main import flash
    onboarding.mark(db, tenant, 4, "complete", actor=user)
    db.commit()
    flash(request, "Groups confirmed. Now share one with a buyer — that is the step "
                   "that turns this from a filing cabinet into something they rely on.")
    return RedirectResponse("/start/5", status_code=303)


# --------------------------------------------------------------------------
# Step 5 — share with a buyer
# --------------------------------------------------------------------------
@router.post("/start/5")
def first_share(request: Request, client_name: str = Form(...),
                contact_email: str = Form(""), contact_name: str = Form(""),
                group_id: str = Form(...), days: str = Form("90"),
                purpose: str = Form("Initial supplier review"),
                db: Session = Depends(get_db), user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    """Creates the client, the grant and the share link in one action, because
    asking a supplier to visit three screens to reach their activation moment
    is how activation rates end up at the median."""
    from ..main import flash
    roles.require(user, roles.GRANTS_WRITE)

    group = db.get(CertificateGroup, group_id)
    if group is None or group.tenant_id != tenant.id:
        raise HTTPException(404, "Group not found")

    client = db.scalar(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == tenant.id,
        ClientAccount.name == client_name.strip())))
    if client is None:
        client = ClientAccount(tenant_id=tenant.id, name=client_name.strip(),
                               relationship_type="customer",
                               country=db.get(Organisation, tenant.organisation_id).country)
        db.add(client)
        db.flush()
        audit.record(db, tenant_id=tenant.id, action="client.created", actor_id=user.id,
                     actor_label=user.email, object_type="client_account",
                     object_id=client.id, payload={"name": client.name, "via": "guide"})

    contact = None
    if contact_email.strip():
        contact = db.scalar(select(ClientContact).where(and_(
            ClientContact.client_id == client.id,
            ClientContact.email == contact_email.strip().lower())))
        if contact is None:
            contact = ClientContact(tenant_id=tenant.id, client_id=client.id,
                                    email=contact_email.strip().lower(),
                                    full_name=contact_name.strip() or None)
            db.add(contact)
            db.flush()

    grant = access.create_grant(
        db, tenant_id=tenant.id, client_id=client.id, scope_kind="group",
        scope_id=group.id, permission_level="view_download",
        days=int(days) if days.strip().isdigit() else None,
        purpose=purpose.strip() or "Initial supplier review", actor=user,
        contact_id=contact.id if contact else None)

    from ..models import ShareLink
    token, digest = new_share_token()
    expires = now() + timedelta(days=30)
    if grant.ends_at and expires > grant.ends_at:
        expires = grant.ends_at
    link = ShareLink(tenant_id=tenant.id, grant_id=grant.id, token_hash=digest,
                     label=f"First pack — {client.name}", expires_at=expires)
    db.add(link)
    db.flush()
    audit.record(db, tenant_id=tenant.id, action="share_link.created", actor_id=user.id,
                 actor_label=user.email, object_type="share_link", object_id=link.id,
                 grant_id=grant.id, payload={"via": "guide"})

    onboarding.mark(db, tenant, 5, "complete", actor=user)
    reached = onboarding.observe(db, tenant)
    db.commit()

    request.session["_start_share_token"] = token
    message = f"{client.name} can now see {group.name}."
    if "m2" in reached["reached"]:
        message += " That is milestone M2 — somebody outside now relies on your profile."
    flash(request, message)
    return RedirectResponse("/start/5", status_code=303)
