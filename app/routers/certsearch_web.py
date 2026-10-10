"""IAF CertSearch routes - GICC F31.

The trigger opens the source in a new tab and holds a lookup open beside it.
That order matters: the operator is looking at IAF's own page while deciding,
so the attestation they give is about something they actually saw.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CertSearchLookup, Document, DocumentVersion, Tenant, User
from ..security import require_tenant, require_user
from ..services import certsearch, roles
from .web_core import page

router = APIRouter()


def _version_for(db: Session, tenant: Tenant, version_id: str) -> DocumentVersion:
    version = db.get(DocumentVersion, version_id)
    if version is None or version.tenant_id != tenant.id:
        raise HTTPException(404, "Version not found")
    return version


@router.post("/versions/{version_id}/certsearch")
def open_lookup(version_id: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_user),
                tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    version = _version_for(db, tenant, version_id)
    document = db.get(Document, version.document_id)
    type_code = document.document_type.code

    if not certsearch.supports(type_code):
        flash(request, f"{type_code} is not a management-system certificate, so IAF "
                       "CertSearch has nothing to say about it.", "warn")
        return RedirectResponse(f"/documents/{document.id}", status_code=303)

    lookup = certsearch.start(db, tenant=tenant, version=version, actor=user,
                              type_code=type_code)

    # The automated route runs only where a deployment has enabled it. Where it
    # has not, the operator reads the page themselves - which is the route that
    # works today and the one that keeps a named person behind the attestation.
    payload, error = certsearch.fetch(lookup.search_url)
    if payload:
        lookup.transport = "scrape"
        lookup.assurance_ceiling = certsearch.TRANSPORT_CEILING["scrape"]
        certsearch.record_payload(db, lookup, payload)
    elif error:
        lookup.error = error
    db.commit()
    return RedirectResponse(f"/certsearch/{lookup.id}", status_code=303)


@router.get("/certsearch/{lookup_id}")
def lookup_detail(lookup_id: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_user),
                  tenant: Tenant = Depends(require_tenant)):
    lookup = db.get(CertSearchLookup, lookup_id)
    if lookup is None or lookup.tenant_id != tenant.id:
        raise HTTPException(404, "Lookup not found")

    version = db.get(DocumentVersion, lookup.document_version_id)
    document = db.get(Document, version.document_id)
    candidates = json.loads(lookup.candidates_json or "[]")

    # Preview each candidate against the held version, so the operator picks
    # with the disagreements already visible rather than after committing.
    previews = [{"index": index, "candidate": candidate,
                 "comparison": certsearch.compare(
                     version, candidate, organisation=tenant.organisation,
                     type_code=document.document_type.code)}
                for index, candidate in enumerate(candidates)]

    return page(request, "certsearch.html", user, tenant, db,
                page_title="Confirm against IAF CertSearch", crumb="Verification",
                nav="verification", lookup=lookup, version=version,
                document=document, dtype=document.document_type,
                previews=previews,
                comparison=json.loads(lookup.comparison_json or "null"),
                ceiling=lookup.assurance_ceiling,
                can_write=roles.can(user, roles.DOCUMENTS_WRITE))


@router.post("/certsearch/{lookup_id}/payload")
def capture_payload(lookup_id: str, request: Request, payload: str = Form(...),
                    db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    tenant: Tenant = Depends(require_tenant)):
    """The operator pastes what the source showed them."""
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    lookup = db.get(CertSearchLookup, lookup_id)
    if lookup is None or lookup.tenant_id != tenant.id:
        raise HTTPException(404, "Lookup not found")

    candidates = certsearch.record_payload(db, lookup, payload)
    db.commit()
    if not candidates:
        flash(request, "Nothing readable in that. Paste the certificate block from the "
                       "IAF page, or enter the fields by hand below.", "warn")
    return RedirectResponse(f"/certsearch/{lookup_id}", status_code=303)


@router.post("/certsearch/{lookup_id}/manual")
def capture_manual(lookup_id: str, request: Request,
                   certificate_number: str = Form(""), standard: str = Form(""),
                   status: str = Form(""), holder_name: str = Form(""),
                   issue_date: str = Form(""), expiry_date: str = Form(""),
                   certification_body: str = Form(""),
                   accreditation_body: str = Form(""),
                   scope_statement: str = Form(""),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_user),
                   tenant: Tenant = Depends(require_tenant)):
    """Typed from the page. Recorded as one candidate and compared identically —
    the comparison must not be softer because a person did the typing."""
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    lookup = db.get(CertSearchLookup, lookup_id)
    if lookup is None or lookup.tenant_id != tenant.id:
        raise HTTPException(404, "Lookup not found")

    candidate = {"certificate_number": certificate_number.strip() or None,
                 "standard": standard.strip() or None,
                 "status": status.strip() or None,
                 "holder_name": holder_name.strip() or None,
                 "issue_date": issue_date.strip() or None,
                 "expiry_date": expiry_date.strip() or None,
                 "certification_body": certification_body.strip() or None,
                 "accreditation_body": accreditation_body.strip() or None,
                 "scope_statement": scope_statement.strip() or None}
    if not any(candidate.values()):
        flash(request, "Enter at least the certificate number and status.", "bad")
        return RedirectResponse(f"/certsearch/{lookup_id}", status_code=303)

    lookup.transport = "assisted"
    lookup.assurance_ceiling = certsearch.TRANSPORT_CEILING["assisted"]
    lookup.candidates_json = json.dumps([candidate], default=str)
    lookup.status = "candidates"
    db.commit()
    return RedirectResponse(f"/certsearch/{lookup_id}", status_code=303)


@router.post("/certsearch/{lookup_id}/apply")
def apply_candidate(lookup_id: str, request: Request, candidate_index: int = Form(0),
                    note: str = Form(""), attest: str = Form(""),
                    db: Session = Depends(get_db),
                    user: User = Depends(require_user),
                    tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    lookup = db.get(CertSearchLookup, lookup_id)
    if lookup is None or lookup.tenant_id != tenant.id:
        raise HTTPException(404, "Lookup not found")
    if lookup.status == "applied":
        flash(request, "That lookup has already been applied.", "info")
        return RedirectResponse(f"/certsearch/{lookup_id}", status_code=303)

    candidates = json.loads(lookup.candidates_json or "[]")
    if candidate_index >= len(candidates):
        raise HTTPException(400, "No such candidate")

    # An attestation is a person putting their name to what they saw. Without
    # it the assisted route is just a form, and the L2 it produces would not
    # mean anything.
    if lookup.transport != "licensed_api" and attest != "on":
        flash(request, "Confirm that you read this on the IAF CertSearch page. The "
                       "assurance level rests on that.", "bad")
        return RedirectResponse(f"/certsearch/{lookup_id}", status_code=303)

    version = db.get(DocumentVersion, lookup.document_version_id)
    document = db.get(Document, version.document_id)
    outcome = certsearch.apply(db, tenant=tenant, lookup=lookup, actor=user,
                               candidate=candidates[candidate_index],
                               type_code=document.document_type.code, note=note)
    db.commit()

    verdict = outcome["comparison"]["verdict"]
    if verdict == "match":
        flash(request, f"Confirmed against IAF CertSearch. This credential now sits at "
                       f"{outcome['assurance']}.")
    elif verdict == "mismatch":
        fields = ", ".join(outcome["comparison"]["decisive_mismatches"])
        flash(request, f"IAF CertSearch disagrees with the uploaded certificate on "
                       f"{fields}. Recorded as a mismatch — this needs resolving with "
                       "the certification body, not re-running.", "bad")
    else:
        flash(request, "Recorded as a partial match. Some fields could not be "
                       "compared, so the credential has not been fully confirmed.",
              "warn")
    return RedirectResponse(f"/documents/{document.id}", status_code=303)


@router.post("/certsearch/{lookup_id}/not-found")
def not_found(lookup_id: str, request: Request, note: str = Form(""),
              db: Session = Depends(get_db), user: User = Depends(require_user),
              tenant: Tenant = Depends(require_tenant)):
    from ..main import flash

    roles.require(user, roles.DOCUMENTS_WRITE)
    lookup = db.get(CertSearchLookup, lookup_id)
    if lookup is None or lookup.tenant_id != tenant.id:
        raise HTTPException(404, "Lookup not found")
    certsearch.mark_not_found(db, tenant=tenant, lookup=lookup, actor=user, note=note)
    version = db.get(DocumentVersion, lookup.document_version_id)
    db.commit()
    flash(request, "Recorded as not found. Coverage varies by scheme and region, so "
                   "this does not lower the credential's assurance — it just means IAF "
                   "could not confirm it.", "warn")
    return RedirectResponse(f"/documents/{version.document_id}", status_code=303)
