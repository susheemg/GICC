"""Profile search and claim - GICC-07 F27.

Public routes, because a company searching for itself has no account yet. Every
response here is written on the assumption that the person on the other end may
not be who they say they are.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Organisation, ProfileClaim, Tenant, now
from ..services import ai, audit, claims, documents as docsvc, textextract

router = APIRouter()


def _claim_render(request: Request, template: str, **ctx):
    from ..main import render
    return render(request, template, ctx)


@router.get("/claim")
def claim_search(request: Request, q: str = "", country: str = "",
                 db: Session = Depends(get_db)):
    results = claims.search(db, query=q, country=country) if q else []
    return _claim_render(request, "claim_search.html", query=q, country=country,
                   results=results, searched=bool(q))


@router.get("/claim/{organisation_id}")
def claim_start(organisation_id: str, request: Request,
                db: Session = Depends(get_db)):
    organisation = db.get(Organisation, organisation_id)
    if organisation is None:
        raise HTTPException(404, "Profile not found")
    if organisation.claim_status == "claimed":
        return _claim_render(request, "claim_unavailable.html", organisation=organisation,
                       reason="This profile has already been claimed.")
    existing = claims.open_claim_for(db, organisation.id)
    return _claim_render(request, "claim_start.html", organisation=organisation,
                   pending=existing is not None,
                   result=request.session.pop("_claim_result", None))


@router.post("/claim/{organisation_id}")
async def claim_submit(organisation_id: str, request: Request,
                       claimant_email: str = Form(...), claimant_name: str = Form(""),
                       evidence: UploadFile = File(...),
                       db: Session = Depends(get_db)):
    from ..main import flash

    organisation = db.get(Organisation, organisation_id)
    if organisation is None:
        raise HTTPException(404, "Profile not found")
    if organisation.claim_status == "claimed":
        flash(request, "That profile has already been claimed.", "bad")
        return RedirectResponse("/claim", status_code=303)

    email = claimant_email.strip().lower()
    cooldown = claims.in_cooldown(db, email)
    if cooldown:
        flash(request, f"Two claims from this address have already been rejected. "
                       f"You can try again after {cooldown:%d %b %Y}, or contact "
                       "support if you believe that is wrong.", "bad")
        return RedirectResponse(f"/claim/{organisation_id}", status_code=303)

    data = await evidence.read()
    if not data:
        flash(request, "That file was empty.", "bad")
        return RedirectResponse(f"/claim/{organisation_id}", status_code=303)

    path, digest = docsvc.store_upload("claims", evidence.filename or "evidence",
                                       data)
    text, _method = textextract.extract(data, evidence.filename or "evidence.pdf")
    extracted, source = _extract_registration(db, text, evidence.filename or "")

    claim = claims.submit(
        db, organisation=organisation, claimant_email=email,
        claimant_name=claimant_name.strip(), evidence_path=path,
        evidence_hash=digest, evidence_filename=evidence.filename or "evidence",
        extracted=extracted, ai_source=source)

    competing = claims.contested(db, organisation.id)
    if len(competing) > 1:
        # F27.8 — two open claims suspend both and raise an adjudication.
        claim.band = "review"
        organisation.claim_status = "disputed"
        db.flush()

    outcome = {"band": claim.band, "score": claim.score,
               "signals": json.loads(claim.signals_json or "[]"),
               "contested": len(competing) > 1,
               "organisation": organisation.legal_name,
               "claim_id": claim.id}

    if claim.band == "auto_approve" and len(competing) == 1:
        from ..seed import provision_tenant
        tenant, owner = provision_tenant(
            db, legal_name=organisation.legal_name, country=organisation.country,
            email=email, full_name=claimant_name.strip() or "Owner",
            password=None, registry_number=organisation.registry_number,
            existing_organisation=organisation)
        claims.approve(db, claim, tenant=tenant, actor=None,
                       note="Auto-approved on score")
        outcome["approved"] = True
    elif claim.band == "reject":
        claims.reject(db, claim, actor=None,
                      note="The evidence did not establish ownership")
        outcome["rejected"] = True

    db.commit()
    request.session["_claim_result"] = outcome
    return RedirectResponse(f"/claim/{organisation_id}", status_code=303)


def _extract_registration(db: Session, text: str, filename: str) -> tuple[dict, str]:
    """Pull the fields a registration document should carry. The model is one of
    five signals and capped at 10 points, so a failure here degrades the claim
    to review rather than blocking it."""
    if not text.strip():
        return {}, "none"

    if ai.is_enabled(db):
        system = ("You read company registration documents. Extract only what is "
                  "present; never infer. Reply with JSON only: "
                  '{"legal_name": str|null, "registry_number": str|null, '
                  '"jurisdiction": "ISO 3166-1 alpha-2"|null, '
                  '"incorporated_on": "YYYY-MM-DD"|null, '
                  '"issuing_authority": str|null, "entity_type": str|null, '
                  '"anomalies": [str]}')
        result, error = ai._call_model(system, f"Filename: {filename}\n\n{text[:6000]}",
                                       db)
        if result:
            return result, "model"

    return _extract_heuristic(text), "heuristic"


def _extract_heuristic(text: str) -> dict:
    import re

    window = text[:12000]
    out: dict = {}

    number = re.search(r"(?:company|registration|registered|entity)\s*(?:number|no\.?|#)"
                       r"\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{4,15})", window, re.I)
    if number:
        out["registry_number"] = number.group(1).strip()

    name = re.search(r"(?:name of (?:the )?company|company name|entity name)\s*[:\-]?\s*"
                     r"([A-Z][A-Za-z0-9&.,'\- ]{3,80})", window, re.I)
    if name:
        out["legal_name"] = name.group(1).strip()
    else:
        heading = re.search(r"^\s*([A-Z][A-Z0-9&.,'\- ]{4,70}(?:LIMITED|LTD|PLC|LLP|"
                            r"INC|LLC|GMBH|BV|AG|SA))\s*$", window, re.M)
        if heading:
            out["legal_name"] = heading.group(1).strip()

    for registry in ["Companies House", "Registrar of Companies", "Kamer van Koophandel",
                     "Handelsregister", "Secretary of State", "Companies Registration Office",
                     "Department of Economic Development", "ACRA", "ASIC"]:
        if registry.lower() in window.lower():
            out["issuing_authority"] = registry
            break

    dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", window)
    if dates:
        out["incorporated_on"] = "-".join(dates[0])
    return out
