"""Public client-facing view - GICC-04 P-27/P-28/P-30.

No account required. Every read re-resolves the grant, applies the NDA gate,
watermarks the rendition and writes an access event naming the grant.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (AccessGrant, CertificateGroup, ClientAccount, ClientContact,
                      Document, DocumentVersion, ShareLink, ShareLinkView, Tenant, now)
from ..security import hash_token
from ..services import access, audit, onboarding

router = APIRouter()

NDA_TEXT = """Access to these documents is provided in confidence. By continuing you
confirm that you will use the material only to assess this supplier, will not
copy or circulate it beyond the people in your organisation who need it for that
assessment, and will destroy or return it on request. Your name, email and the
time of every document you open are recorded and shared with the supplier."""
NDA_HASH = hashlib.sha256(NDA_TEXT.encode()).hexdigest()[:16]


def _resolve_link(db: Session, token: str) -> tuple[ShareLink, AccessGrant, Tenant]:
    link = db.scalar(select(ShareLink).where(ShareLink.token_hash == hash_token(token)))
    if link is None:
        raise HTTPException(404, "This link is not recognised.")
    if link.revoked_at:
        raise HTTPException(403, "This link has been revoked by the supplier.")
    if link.expires_at and link.expires_at < now():
        raise HTTPException(403, "This link has expired.")
    if link.max_views and link.view_count >= link.max_views:
        raise HTTPException(403, "This link has reached its view limit.")
    grant = db.get(AccessGrant, link.grant_id)
    if grant is None or not access.grant_is_live(grant):
        raise HTTPException(403, "The access behind this link is no longer active.")
    tenant = db.get(Tenant, link.tenant_id)
    if tenant is None or tenant.lifecycle_state == "suspended":
        raise HTTPException(403, "This supplier's disclosure is suspended. Contact them "
                                 "directly before relying on anything they have shared.")
    return link, grant, tenant


def _viewer_key(token: str) -> str:
    return f"share_viewer_{hash_token(token)[:12]}"


@router.get("/s/{token}")
def share_view(token: str, request: Request, db: Session = Depends(get_db)):
    from ..main import render
    link, grant, tenant = _resolve_link(db, token)
    client = db.get(ClientAccount, grant.client_id)
    group = db.get(CertificateGroup, grant.scope_id) if grant.scope_kind == "group" else None
    session_key = _viewer_key(token)
    viewer = request.session.get(session_key)

    if not viewer:
        return render(request, "share_gate.html", {
            "tenant": tenant, "client": client, "group": group, "token": token,
            "nda_required": grant.nda_required,
            "nda_text": NDA_TEXT, "nda_hash": NDA_HASH})

    rows = [{"document": document, "type": document.document_type,
             "version": version}
            for document, version in access.disclosable(db, grant)]
    rows.sort(key=lambda r: r["type"].code)

    link.view_count += 1
    db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                         viewer_email=(viewer or {}).get("email"),
                         ip=request.client.host if request.client else None,
                         user_agent=request.headers.get("user-agent", "")[:250],
                         action="open_index"))
    grant.last_used_at = now()
    audit.record(db, tenant_id=tenant.id, action="share_link.viewed", actor_kind="client_contact",
                 actor_label=(viewer or {}).get("email", "anonymous viewer"),
                 object_type="share_link", object_id=link.id, grant_id=grant.id,
                 ip=request.client.host if request.client else None,
                 payload={"documents_visible": len(rows)})
    db.commit()

    can_download = access.can_download(grant, group)
    return render(request, "share_view.html", {
        "tenant": tenant, "client": client, "group": group, "grant": grant,
        "rows": rows, "token": token, "viewer": viewer, "can_download": can_download,
        "link": link})


@router.post("/s/{token}/accept")
def accept_nda(token: str, request: Request, email: str = Form(...),
               full_name: str = Form(""), accept: str = Form(""),
               db: Session = Depends(get_db)):
    from ..main import flash
    link, grant, tenant = _resolve_link(db, token)
    if grant.nda_required and accept != "on":
        flash(request, "Tick the confidentiality box to continue.", "bad")
        return RedirectResponse(f"/s/{token}", status_code=303)

    email = email.strip().lower()
    contact = db.scalar(select(ClientContact).where(
        ClientContact.client_id == grant.client_id, ClientContact.email == email))
    if contact is None:
        contact = ClientContact(tenant_id=tenant.id, client_id=grant.client_id,
                                email=email, full_name=full_name.strip() or None)
        db.add(contact)
        db.flush()
    contact.last_seen_at = now()
    if grant.nda_required:
        contact.nda_accepted_at = now()
        contact.nda_template_hash = NDA_HASH
        audit.record(db, tenant_id=tenant.id, action="agreement.accepted",
                     actor_kind="client_contact", actor_label=email,
                     object_type="client_contact", object_id=contact.id, grant_id=grant.id,
                     ip=request.client.host if request.client else None,
                     payload={"template_hash": NDA_HASH, "name": full_name})
    else:
        audit.record(db, tenant_id=tenant.id, action="viewer.identified",
                     actor_kind="client_contact", actor_label=email,
                     object_type="client_contact", object_id=contact.id, grant_id=grant.id,
                     ip=request.client.host if request.client else None,
                     payload={"name": full_name})
    db.commit()
    request.session[_viewer_key(token)] = {"email": email, "name": full_name,
                                           "accepted_at": now().isoformat()}
    return RedirectResponse(f"/s/{token}", status_code=303)


@router.get("/s/{token}/d/{document_id}")
def share_document(token: str, document_id: str, request: Request,
                   db: Session = Depends(get_db)):
    from ..main import render
    link, grant, tenant = _resolve_link(db, token)
    viewer = request.session.get(_viewer_key(token))
    if not viewer:
        return RedirectResponse(f"/s/{token}", status_code=303)

    allowed = {d.id for d in access.resolve_documents(db, grant)}
    if document_id not in allowed:
        audit.record(db, tenant_id=tenant.id, action="access.denied",
                     actor_kind="client_contact",
                     actor_label=(viewer or {}).get("email", "anonymous"),
                     object_type="document", object_id=document_id, grant_id=grant.id,
                     payload={"reason": "outside grant scope"})
        db.commit()
        raise HTTPException(403, "That document is not covered by this access grant.")

    document = db.get(Document, document_id)
    version = db.get(DocumentVersion, document.active_version_id)
    group = db.get(CertificateGroup, grant.scope_id) if grant.scope_kind == "group" else None
    client = db.get(ClientAccount, grant.client_id)

    db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                         viewer_email=(viewer or {}).get("email"),
                         ip=request.client.host if request.client else None,
                         document_id=document_id, action="view"))
    audit.record(db, tenant_id=tenant.id, action="document.viewed",
                 actor_kind="client_contact",
                 actor_label=(viewer or {}).get("email", "anonymous viewer"),
                 object_type="document_version", object_id=version.id if version else None,
                 grant_id=grant.id, ip=request.client.host if request.client else None,
                 payload={"document": document.title, "client": client.name if client else None})
    onboarding.observe(db, tenant)
    db.commit()

    watermark = None
    if grant.watermark_required:
        who = (viewer or {}).get("email") or (client.name if client else "viewer")
        watermark = f"{who} · {client.name if client else ''} · {now():%d %b %Y %H:%M} UTC"

    return render(request, "share_document.html", {
        "tenant": tenant, "client": client, "document": document, "version": version,
        "type": document.document_type, "token": token, "watermark": watermark,
        "can_download": access.can_download(grant, group), "viewer": viewer})


@router.get("/s/{token}/d/{document_id}/file")
def share_download(token: str, document_id: str, request: Request,
                   db: Session = Depends(get_db)):
    from ..services.documents import absolute_path
    link, grant, tenant = _resolve_link(db, token)
    viewer = request.session.get(_viewer_key(token))
    group = db.get(CertificateGroup, grant.scope_id) if grant.scope_kind == "group" else None
    if not access.can_download(grant, group):
        raise HTTPException(403, "This grant is view-only. Downloads are not permitted.")

    allowed = {d.id for d in access.resolve_documents(db, grant)}
    if document_id not in allowed:
        raise HTTPException(403, "That document is not covered by this access grant.")

    document = db.get(Document, document_id)
    version = db.get(DocumentVersion, document.active_version_id)
    if not version or not version.file_path:
        raise HTTPException(404, "No file is attached to the current version.")
    path = absolute_path(version.file_path)
    if not path.exists():
        raise HTTPException(404, "The stored file is no longer available.")

    db.add(ShareLinkView(tenant_id=tenant.id, share_link_id=link.id,
                         viewer_email=(viewer or {}).get("email"),
                         ip=request.client.host if request.client else None,
                         document_id=document_id, action="download"))
    audit.record(db, tenant_id=tenant.id, action="document.downloaded",
                 actor_kind="client_contact",
                 actor_label=(viewer or {}).get("email", "anonymous viewer"),
                 object_type="document_version", object_id=version.id, grant_id=grant.id,
                 payload={"document": document.title})
    db.commit()
    return FileResponse(path, filename=version.file_name or "document",
                        media_type=version.mime_type or "application/octet-stream")


@router.get("/t/{slug}")
def trust_page(slug: str, request: Request, db: Session = Depends(get_db)):
    """Public trust page - GICC-01 F11.2. Shows only documents that sit inside a
    group with a Public sensitivity ceiling."""
    from ..main import render
    tenant = db.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None or tenant.lifecycle_state == "closed":
        raise HTTPException(404, "No public profile is published at this address.")

    public_groups = db.scalars(select(CertificateGroup).where(
        CertificateGroup.tenant_id == tenant.id,
        CertificateGroup.sensitivity_ceiling == "P").order_by(
        CertificateGroup.display_order)).all()
    rows = []
    seen = set()
    for group in public_groups:
        for document in access.group_documents(db, tenant.id, group):
            if document.id in seen:
                continue
            version = db.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
            if version is None:
                continue
            seen.add(document.id)
            rows.append({"document": document, "type": document.document_type,
                         "version": version})
    rows.sort(key=lambda r: r["type"].code)
    org = tenant.organisation
    return render(request, "trust_page.html", {
        "tenant": tenant, "org": org, "rows": rows,         "exiting": tenant.lifecycle_state == "closing"})
