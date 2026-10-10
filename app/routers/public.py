"""The public front door - search, results, and the gate to a supplier profile.

The whole page is one question: who are you looking for? Everything else on it
is secondary, which is why the search sits alone above the fold and the two
account buttons are panels rather than pages - sending somebody to a separate
screen to sign in loses the search they came to run.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Organisation, Tenant
from ..services import directory

router = APIRouter()


def _public_render(request: Request, template: str, **ctx):
    from ..main import render
    return render(request, template, ctx)


@router.get("/", include_in_schema=False)
def home(request: Request, db: Session = Depends(get_db)):
    # A signed-in supplier has no use for the public search as a landing page.
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)

    members = db.scalar(select(func.count(Tenant.id)).where(
        Tenant.lifecycle_state == "active", Tenant.kind != "buyer")) or 0
    listed = db.scalar(select(func.count(Organisation.id))) or 0
    return _public_render(request, "public_home.html", members=members, listed=listed,
                   panel=request.query_params.get("panel", ""))


@router.get("/search")
def search(request: Request, q: str = "", db: Session = Depends(get_db)):
    outcome = directory.search(db, q)
    members = db.scalar(select(func.count(Tenant.id)).where(
        Tenant.lifecycle_state == "active")) or 0
    return _public_render(request, "public_results.html", q=q, outcome=outcome,
                   members=members, min_query=directory.MIN_QUERY,
                   signed_in=bool(request.session.get("user_id")))


@router.get("/company/{organisation_id}")
def company(organisation_id: str, request: Request, db: Session = Depends(get_db)):
    """The gate. Identity details are behind a sign-in, deliberately - see the
    note at the top of services/directory.py."""
    organisation = db.get(Organisation, organisation_id)
    if organisation is None:
        return RedirectResponse("/search", status_code=303)

    if not request.session.get("user_id"):
        request.session["_after_login"] = f"/company/{organisation_id}"
        return _public_render(request, "public_gate.html", organisation=organisation,
                       member=organisation.claim_status == "claimed")

    # Signed in: buyers get the full profile in their own portal.
    return RedirectResponse(f"/buyer/companies/{organisation_id}", status_code=303)
