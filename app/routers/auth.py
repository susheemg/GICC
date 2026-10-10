from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, now
from ..security import verify_password
from ..seed import provision_tenant
from ..services import audit

router = APIRouter()

COUNTRIES = [("GB", "United Kingdom"), ("IE", "Ireland"), ("DE", "Germany"),
             ("FR", "France"), ("NL", "Netherlands"), ("ES", "Spain"),
             ("AE", "United Arab Emirates"), ("SG", "Singapore"), ("IN", "India"),
             ("US", "United States"), ("AU", "Australia"), ("ZA", "South Africa")]


def _render(request: Request, template: str, **ctx):
    from ..main import render
    ctx.setdefault("countries", COUNTRIES)
    return render(request, template, ctx)


@router.get("/login")
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    from ..config import DEMO_EMAIL, DEMO_PASSWORD, SEED_DEMO
    return _render(request, "login.html", demo_email=DEMO_EMAIL if SEED_DEMO else None,
                   demo_password=DEMO_PASSWORD if SEED_DEMO else None)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    from ..main import flash
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        flash(request, "That email and password combination did not match an account.", "bad")
        return RedirectResponse("/login", status_code=303)

    user.last_login_at = now()
    tenant = user.tenant
    if tenant.lifecycle_state == "suspended":
        flash(request, "This workspace is suspended. Contact the platform operator — the "
                       "reason is recorded in your activity log.", "bad")
        return RedirectResponse("/login", status_code=303)
    if tenant.lifecycle_state == "closed":
        flash(request, "This workspace has been closed. Contact support to discuss "
                       "reactivation or a fresh registration.", "bad")
        return RedirectResponse("/login", status_code=303)
    tenant.last_activity_at = now()
    audit.record(db, tenant_id=user.tenant_id, action="user.signed_in", actor_id=user.id,
                 actor_label=user.email, ip=request.client.host if request.client else None)
    db.commit()
    request.session["user_id"] = user.id
    # Somebody who signed in from a company page came here to see that company,
    # not a dashboard. Sending them to the dashboard loses the thing they were
    # doing and makes them search for it again.
    destination = request.session.pop("_after_login", None)
    if not destination:
        # A buyer-only workspace has no credential register to land on.
        destination = "/buyer" if tenant.kind == "buyer" else "/dashboard"
    return RedirectResponse(destination, status_code=303)


@router.get("/register")
def register_form(request: Request):
    return _render(request, "register.html")


@router.post("/register")
def register(request: Request,
             legal_name: str = Form(...), country: str = Form("GB"),
             registry_number: str = Form(""), full_name: str = Form(...),
             email: str = Form(...), password: str = Form(...),
             db: Session = Depends(get_db)):
    from ..main import flash
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "An account already exists for that email. Sign in instead.", "warn")
        return RedirectResponse("/login", status_code=303)
    if len(password) < 8:
        flash(request, "Choose a password of at least 8 characters.", "bad")
        return RedirectResponse("/register", status_code=303)

    tenant, user = provision_tenant(
        db, legal_name=legal_name.strip(), country=country,
        registry_number=registry_number.strip() or None,
        email=email, full_name=full_name.strip(), password=password)
    from ..services import onboarding
    onboarding.get_or_create(db, tenant)
    db.commit()
    request.session["user_id"] = user.id
    flash(request, f"Workspace created for {tenant.name}. Five short steps and a buyer "
                   "can be reading your credentials.", "ok")
    return RedirectResponse("/start", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
