"""The permission resolver - GICC-03 AD3.

This is the only place in the application that decides what a client or an
agent can see. The web share view, the REST API and the MCP server all call
resolve_documents(). A second implementation of these rules would be a breach
waiting to happen.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (AccessGrant, CertificateGroup, ClientAccount, Document,
                      DocumentType, DocumentVersion, GroupItem, ShareLink, now)
from . import audit

SENSITIVITY_ORDER = {"P": 0, "R": 1, "C": 2, "S": 3}


def grant_is_live(grant: AccessGrant, at: datetime | None = None) -> bool:
    at = at or now()
    if grant.status != "active" or grant.revoked_at:
        return False
    if grant.starts_at and grant.starts_at > at:
        return False
    if grant.ends_at and grant.ends_at < at:
        return False
    return True


def group_documents(db: Session, tenant_id: str, group: CertificateGroup) -> list[Document]:
    """Resolves both explicit document members and type-prefix rules, then
    applies the group's sensitivity ceiling. A document more sensitive than the
    ceiling is excluded even if a rule would otherwise include it."""
    items = db.scalars(select(GroupItem).where(GroupItem.group_id == group.id)).all()
    explicit_ids = {i.document_id for i in items if i.item_kind == "document" and i.document_id}
    prefixes = [i.type_prefix for i in items if i.item_kind == "type_rule" and i.type_prefix]

    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant_id, Document.deleted_at.is_(None)))).all()

    ceiling = SENSITIVITY_ORDER.get(group.sensitivity_ceiling, 1)
    out = []
    for document, dtype in rows:
        included = document.id in explicit_ids or any(
            dtype.code.startswith(prefix) for prefix in prefixes)
        if not included:
            continue
        if SENSITIVITY_ORDER.get(dtype.sensitivity, 1) > ceiling:
            continue
        out.append(document)
    return out


def resolve_documents(db: Session, grant: AccessGrant) -> list[Document]:
    if not grant_is_live(grant):
        return []
    if grant.scope_kind == "group":
        group = db.get(CertificateGroup, grant.scope_id)
        return group_documents(db, grant.tenant_id, group) if group else []
    if grant.scope_kind == "document":
        document = db.get(Document, grant.scope_id)
        return [document] if document and document.deleted_at is None else []
    if grant.scope_kind == "whole_profile":
        return db.scalars(select(Document).where(
            and_(Document.tenant_id == grant.tenant_id,
                 Document.deleted_at.is_(None)))).all()
    return []


def resolve_for_client(db: Session, tenant_id: str, client_id: str) -> dict[str, Document]:
    """Union of every live grant held by a client account."""
    grants = db.scalars(select(AccessGrant).where(
        and_(AccessGrant.tenant_id == tenant_id, AccessGrant.client_id == client_id))).all()
    documents: dict[str, Document] = {}
    for grant in grants:
        if not grant_is_live(grant):
            continue
        for document in resolve_documents(db, grant):
            documents[document.id] = document
    return documents


def can_download(grant: AccessGrant, group: CertificateGroup | None) -> bool:
    if not grant.download_allowed:
        return False
    if group and not group.download_allowed:
        return False
    return grant.permission_level in {"view_download", "view_download_bulk"}


def create_grant(db: Session, *, tenant_id: str, client_id: str, scope_kind: str,
                 scope_id: str | None, permission_level: str, days: int | None,
                 purpose: str, actor, contact_id: str | None = None,
                 api_client_id: str | None = None) -> AccessGrant:
    """A grant may be more restrictive than its group policy, never less
    (GICC-01 F7.3). The ceiling is applied here, at creation."""
    group = db.get(CertificateGroup, scope_id) if scope_kind == "group" else None

    nda = bool(group.nda_required) if group else False
    watermark = bool(group.watermark_required) if group else True
    download = bool(group.download_allowed) if group else True
    max_days = group.max_grant_days if group else 90

    if days is None or days > max_days:
        days = max_days
    if not download and permission_level in {"view_download", "view_download_bulk"}:
        permission_level = "view"

    grant = AccessGrant(
        tenant_id=tenant_id, client_id=client_id, contact_id=contact_id,
        api_client_id=api_client_id, scope_kind=scope_kind, scope_id=scope_id,
        permission_level=permission_level, starts_at=now(),
        ends_at=now() + timedelta(days=days) if days else None,
        nda_required=nda, watermark_required=watermark, download_allowed=download,
        status="active", purpose=purpose)
    db.add(grant)
    db.flush()

    client = db.get(ClientAccount, client_id)
    audit.record(db, tenant_id=tenant_id, action="grant.created",
                 actor_id=getattr(actor, "id", None), actor_label=getattr(actor, "email", "system"),
                 object_type="access_grant", object_id=grant.id, grant_id=grant.id,
                 payload={"client": client.name if client else client_id,
                          "scope": f"{scope_kind}:{scope_id}", "days": days,
                          "permission": permission_level, "purpose": purpose})
    return grant


def revoke_grant(db: Session, grant: AccessGrant, actor, reason: str) -> None:
    grant.status = "revoked"
    grant.revoked_at = now()
    grant.revoke_reason = reason
    for link in db.scalars(select(ShareLink).where(ShareLink.grant_id == grant.id)).all():
        if link.revoked_at is None:
            link.revoked_at = now()
    db.flush()
    audit.record(db, tenant_id=grant.tenant_id, action="grant.revoked",
                 actor_id=getattr(actor, "id", None), actor_label=getattr(actor, "email", "system"),
                 object_type="access_grant", object_id=grant.id, grant_id=grant.id,
                 payload={"reason": reason})


def offboard_client(db: Session, tenant_id: str, client: ClientAccount, actor,
                    reason: str) -> int:
    """Bulk offboarding - GICC-01 F8.10. Returns the number of grants revoked."""
    grants = db.scalars(select(AccessGrant).where(
        and_(AccessGrant.tenant_id == tenant_id, AccessGrant.client_id == client.id,
             AccessGrant.status == "active"))).all()
    for grant in grants:
        revoke_grant(db, grant, actor, f"Client offboarded: {reason}")
    client.status = "offboarded"
    client.offboarded_at = now()
    client.offboard_reason = reason
    db.flush()
    audit.record(db, tenant_id=tenant_id, action="client.offboarded",
                 actor_id=getattr(actor, "id", None), actor_label=getattr(actor, "email", "system"),
                 object_type="client_account", object_id=client.id,
                 payload={"reason": reason, "grants_revoked": len(grants)})
    return len(grants)


def scoped_documents(db: Session, client) -> list:
    """Documents an API key may see. A key bound to a client grant sees only
    that grant's documents; an unbound key is a first-party integration and sees
    the tenant's own register.

    This lived twice - once in the REST router and once in the MCP router -
    which is exactly the second implementation the design forbids. Both now call
    this.
    """
    from ..models import AccessGrant, Document

    if client.grant_id:
        grant = db.get(AccessGrant, client.grant_id)
        if grant is None or not grant_is_live(grant):
            return []
        return resolve_documents(db, grant)
    return list(db.scalars(select(Document).where(and_(
        Document.tenant_id == client.tenant_id,
        Document.deleted_at.is_(None)))).all())


def disclosable(db: Session, grant) -> list[tuple]:
    """(document, active version) pairs a grant actually discloses.

    A grant resolves to documents; a document with no *published* version has
    nothing to show. A draft is work in progress, not a shared credential, and a
    buyer seeing an empty entry would read it as a gap the supplier never
    claimed to have filled.

    The share view already dropped these inline. Putting it here means the share
    link, the buyer portal and anything added later cannot disagree about what a
    grant discloses.
    """
    from ..models import DocumentVersion

    out = []
    for document in resolve_documents(db, grant):
        version = (db.get(DocumentVersion, document.active_version_id)
                   if document.active_version_id else None)
        if version is None or version.status != "active":
            continue
        out.append((document, version))
    return out
