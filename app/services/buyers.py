"""The buyer side - directory search, supplier lists, access requests.

The whole feature turns on one question: **what may a logged-in buyer see about
a company that has not agreed to show them anything?**

The answer here is a three-tier ladder, and the rules live in this module alone
so there is one place to audit them.

  Tier 0, anonymous. The public credential page only. Unchanged.

  Tier 1, an authenticated buyer browsing the directory. Public-register
  identity: legal name, country, registry number, LEI, VAT, DUNS, sector,
  incorporation date, website, and the registered address - subject to the
  claimed/unclaimed rule below. Nothing about the credential register: not a
  document count, not an expiry, not a Trust Index. Those are the supplier's to
  disclose, and a count alone leaks a judgement ("holds 2 credentials" reads as
  a warning).

  Tier 2, a granted buyer. Whatever the grant says, through the existing
  disclosure machinery.

**The claimed/unclaimed rule.** A claimed profile belongs to a company that
chose to be on this platform. An imported profile does not: it was created from
public-register data about a company that never asked. Both are lawful to hold,
but they are not the same thing, and a registered address is where that stops
being academic - for a great many small companies the registered address is a
home address. It is on a public register, and republishing it in a searchable
directory is still a different act from a single lookup.

So: full registered address for claimed profiles, locality and country only for
unclaimed ones. A company that claims its profile gets the full entry; until
then the buyer gets enough to recognise the company and invite it.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from ..models import (AccessGrant, AccessRequest, ClientAccount, DirectorySearch,
                      Document, Organisation, ProfileView, SupplierLink, Tenant,
                      User, now)
from . import access, audit

MAX_RESULTS = 25
MIN_QUERY = 3
# A buyer with a real supply base runs a handful of searches a day. This is set
# well above ordinary use and exists to make harvesting hit a wall, not to
# ration legitimate work.
SEARCH_LIMIT_PER_DAY = 200


class SearchLimited(Exception):
    """Raised when a buyer exceeds the daily directory search allowance."""


def searches_today(db: Session, buyer_tenant_id: str) -> int:
    since = now() - timedelta(days=1)
    return db.scalar(select(func.count(DirectorySearch.id)).where(and_(
        DirectorySearch.buyer_tenant_id == buyer_tenant_id,
        DirectorySearch.created_at >= since))) or 0


# --------------------------------------------------------------------------
# What a buyer may see
# --------------------------------------------------------------------------
def _locality(address: str | None) -> str:
    """The town and region, without the street. Enough to tell two identically
    named companies apart; not enough to turn the directory into a mailing list.

    Registers disagree about formatting - some emit multi-line addresses, the
    Indian MCA register emits one comma-separated line. Splitting on newlines
    alone returned nothing for the latter, which left a buyer unable to
    distinguish two companies of the same name. Both shapes are handled, and the
    *tail* is taken because that is the town and region; the head is the street.
    """
    if not address:
        return ""
    parts = [line.strip() for line in address.splitlines() if line.strip()]
    if len(parts) < 2:
        parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 2:
        return ""
    # Registers append postcodes and country names as their own fragments. Left
    # in, the buyer sees "110034-India, Delhi" where a town should be, which is
    # noise rather than the disambiguation this field exists to provide.
    cleaned = []
    for part in parts[-4:]:
        part = re.sub(r"[\s,-]*\b(india|united kingdom|uk|usa)\b\s*$", "", part,
                      flags=re.I).strip(" ,-")
        digits = sum(character.isdigit() for character in part)
        if len(part) <= 2 or digits >= len(part) / 2:
            continue                       # a bare postcode, not a place
        # Register data is often shouted. Title-casing helps, but only where
        # the fragment is pure letters - "M1 4AB" must not become "M1 4Ab".
        readable = (part.title() if part.isupper()
                    and all(character.isalpha() or character.isspace()
                            for character in part) else part)
        cleaned.append(readable)
    # De-duplicate while keeping order: "Delhi, Delhi" reads as a mistake.
    seen, tail = set(), []
    for part in cleaned:
        if part.lower() not in seen:
            seen.add(part.lower())
            tail.append(part)
    return ", ".join(tail[-2:])


def directory_entry(organisation: Organisation, *, tenant: Tenant | None = None) -> dict:
    """Tier 1. The single definition of what a logged-in buyer sees about a
    company in the directory, used by search and the profile page alike."""
    claimed = organisation.claim_status == "claimed"
    return {
        "id": organisation.id,
        "legal_name": organisation.legal_name,
        "trading_name": organisation.trading_name,
        "country": organisation.country,
        "registry_number": organisation.registry_number,
        "registry_authority": organisation.registry_authority,
        "lei": organisation.lei,
        "vat_number": organisation.vat_number if claimed else None,
        "duns": organisation.duns if claimed else None,
        "entity_type": organisation.entity_type,
        "incorporated_on": organisation.incorporated_on,
        "sector_code": organisation.sector_code,
        "website": organisation.website,
        # The rule this whole module exists to hold in one place.
        "registered_address": organisation.registered_address if claimed else None,
        "locality": _locality(organisation.registered_address),
        "address_withheld": bool(organisation.registered_address) and not claimed,
        "identity_verification": organisation.verification_state,
        "claimed": claimed,
        "claim_status": organisation.claim_status,
        "on_platform": tenant is not None and tenant.lifecycle_state == "active",
        "slug": tenant.slug if tenant is not None else None,
        "source": organisation.source,
    }


def tenant_for(db: Session, organisation_id: str) -> Tenant | None:
    return db.scalar(select(Tenant).where(and_(
        Tenant.organisation_id == organisation_id,
        Tenant.lifecycle_state != "closed")))


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
def search(db: Session, *, buyer: Tenant, user: User, query: str,
           country: str = "") -> list[dict]:
    text = (query or "").strip()
    if len(text) < MIN_QUERY:
        return []
    if searches_today(db, buyer.id) >= SEARCH_LIMIT_PER_DAY:
        raise SearchLimited(
            f"You have run {SEARCH_LIMIT_PER_DAY} directory searches in the last "
            "24 hours. That is far more than sourcing normally needs, so the "
            "allowance has paused. Contact support if this is genuine work.")

    statement = select(Organisation).where(or_(
        Organisation.legal_name.ilike(f"%{text}%"),
        Organisation.trading_name.ilike(f"%{text}%"),
        Organisation.registry_number == text,
        Organisation.lei == text.upper()))
    if country:
        statement = statement.where(func.upper(Organisation.country) == country.upper())

    rows = db.scalars(statement.limit(MAX_RESULTS)).all()
    linked = {link.organisation_id: link for link in db.scalars(
        select(SupplierLink).where(and_(
            SupplierLink.buyer_tenant_id == buyer.id,
            SupplierLink.removed_at.is_(None)))).all()}

    results = []
    for organisation in rows:
        if organisation.id == buyer.organisation_id:
            continue                      # a buyer does not source from itself
        entry = directory_entry(organisation, tenant=tenant_for(db, organisation.id))
        link = linked.get(organisation.id)
        entry["link_id"] = link.id if link else None
        entry["link_state"] = link.state if link else None
        results.append(entry)

    db.add(DirectorySearch(buyer_tenant_id=buyer.id, user_id=user.id,
                           query=text[:250], country=country[:2] or None,
                           result_count=len(results)))
    db.flush()
    return results


def record_view(db: Session, *, buyer: Tenant, user: User,
                organisation: Organisation) -> None:
    db.add(ProfileView(organisation_id=organisation.id, buyer_tenant_id=buyer.id,
                       user_id=user.id))
    db.flush()


def viewers_of(db: Session, organisation_id: str, limit: int = 20) -> list[dict]:
    """Shown to the supplier: who has been looking at their entry. A directory
    that watches you without telling you is a different product."""
    rows = db.scalars(select(ProfileView)
                      .where(ProfileView.organisation_id == organisation_id)
                      .order_by(desc(ProfileView.created_at)).limit(limit)).all()
    out = []
    for row in rows:
        buyer = db.get(Tenant, row.buyer_tenant_id)
        out.append({"buyer": buyer.name if buyer else "A buyer",
                    "at": row.created_at})
    return out


# --------------------------------------------------------------------------
# The buyer's supplier list
# --------------------------------------------------------------------------
def add_supplier(db: Session, *, buyer: Tenant, user: User,
                 organisation: Organisation, internal_reference: str = "",
                 category: str = "", criticality: int = 3) -> SupplierLink:
    existing = db.scalar(select(SupplierLink).where(and_(
        SupplierLink.buyer_tenant_id == buyer.id,
        SupplierLink.organisation_id == organisation.id)))
    if existing is not None:
        if existing.removed_at is not None:
            existing.removed_at = None
            existing.state = "watching"
            db.flush()
        return existing

    link = SupplierLink(
        buyer_tenant_id=buyer.id, organisation_id=organisation.id,
        internal_reference=internal_reference.strip() or None,
        category=category.strip() or None, criticality=criticality,
        added_by_user_id=user.id,
        state="watching" if organisation.claim_status == "claimed" else "invited")
    db.add(link)
    db.flush()
    audit.record(db, tenant_id=buyer.id, action="supplier.linked",
                 actor_id=user.id, actor_label=user.email,
                 object_type="supplier_link", object_id=link.id,
                 payload={"organisation": organisation.legal_name,
                          "claimed": organisation.claim_status == "claimed"})
    return link


def remove_supplier(db: Session, *, buyer: Tenant, user: User,
                    link: SupplierLink) -> None:
    link.removed_at = now()
    link.state = "removed"
    db.flush()
    audit.record(db, tenant_id=buyer.id, action="supplier.unlinked",
                 actor_id=user.id, actor_label=user.email,
                 object_type="supplier_link", object_id=link.id, payload={})


def supplier_list(db: Session, buyer_tenant_id: str) -> list[dict]:
    links = db.scalars(select(SupplierLink).where(and_(
        SupplierLink.buyer_tenant_id == buyer_tenant_id,
        SupplierLink.removed_at.is_(None))).order_by(
        SupplierLink.criticality, SupplierLink.created_at)).all()
    out = []
    for link in links:
        organisation = db.get(Organisation, link.organisation_id)
        if organisation is None:
            continue
        tenant = tenant_for(db, organisation.id)
        entry = directory_entry(organisation, tenant=tenant)
        entry.update(link_id=link.id, link_state=link.state,
                     internal_reference=link.internal_reference,
                     category=link.category, criticality=link.criticality,
                     documents=len(granted_documents(db, buyer_tenant_id,
                                                     organisation.id)))
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Requesting access
# --------------------------------------------------------------------------
def client_account_for(db: Session, *, supplier: Tenant, buyer: Tenant,
                       user: User) -> ClientAccount:
    """The supplier's record of this buyer.

    Matching an existing record by buyer tenant first, then by name, so a
    supplier who already added 'Northwind Trading' by hand does not end up with
    two client accounts for the same company once that buyer arrives.
    """
    existing = db.scalar(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == supplier.id,
        ClientAccount.buyer_tenant_id == buyer.id)))
    if existing is not None:
        return existing

    by_name = db.scalar(select(ClientAccount).where(and_(
        ClientAccount.tenant_id == supplier.id,
        func.lower(ClientAccount.name) == buyer.name.lower(),
        ClientAccount.buyer_tenant_id.is_(None))))
    if by_name is not None:
        by_name.buyer_tenant_id = buyer.id
        db.flush()
        return by_name

    organisation = db.get(Organisation, buyer.organisation_id)
    client = ClientAccount(
        tenant_id=supplier.id, name=buyer.name, relationship_type="customer",
        country=organisation.country if organisation else None,
        buyer_tenant_id=buyer.id, status="active")
    db.add(client)
    db.flush()
    return client


def request_access(db: Session, *, buyer: Tenant, user: User, link: SupplierLink,
                   purpose: str, scope_kind: str = "whole_profile",
                   scope_id: str | None = None) -> AccessRequest | None:
    """Raises a request against the supplier's workspace.

    Returns None when the profile is unclaimed - there is nobody to answer, so
    the honest outcome is an invitation rather than a request that will sit open
    forever.
    """
    organisation = db.get(Organisation, link.organisation_id)
    supplier = tenant_for(db, link.organisation_id)
    if supplier is None or organisation.claim_status != "claimed":
        link.state = "invited"
        db.flush()
        audit.record(db, tenant_id=buyer.id, action="supplier.invited",
                     actor_id=user.id, actor_label=user.email,
                     object_type="supplier_link", object_id=link.id,
                     payload={"organisation": organisation.legal_name,
                              "reason": "no claimed workspace to receive a request"})
        return None

    open_already = db.scalar(select(AccessRequest).where(and_(
        AccessRequest.tenant_id == supplier.id,
        AccessRequest.requested_by_tenant_id == buyer.id,
        AccessRequest.status == "open")))
    if open_already is not None:
        return open_already

    client = client_account_for(db, supplier=supplier, buyer=buyer, user=user)
    access_request = AccessRequest(
        tenant_id=supplier.id, client_id=client.id, contact_email=user.email,
        scope_kind=scope_kind, scope_id=scope_id, purpose=purpose.strip() or None,
        status="open", origin="buyer_portal", requested_by_user_id=user.id,
        requested_by_tenant_id=buyer.id,
        sla_due_at=now() + timedelta(days=2))
    db.add(access_request)
    db.flush()

    link.state = "requested"
    link.last_request_id = access_request.id
    db.flush()

    from . import notifications
    notifications.on_access_request(db, supplier, access_request,
                                    "your whole profile" if scope_kind == "whole_profile"
                                    else "a certificate group", buyer.name)
    audit.record(db, tenant_id=buyer.id, action="access.requested",
                 actor_id=user.id, actor_label=user.email,
                 object_type="access_request", object_id=access_request.id,
                 payload={"supplier": organisation.legal_name, "purpose": purpose})
    return access_request


def requests_for(db: Session, buyer_tenant_id: str) -> list[AccessRequest]:
    return list(db.scalars(select(AccessRequest)
                           .where(AccessRequest.requested_by_tenant_id == buyer_tenant_id)
                           .order_by(desc(AccessRequest.created_at))).all())


# --------------------------------------------------------------------------
# What the buyer got
# --------------------------------------------------------------------------
def live_grants(db: Session, buyer_tenant_id: str,
                organisation_id: str | None = None) -> list[AccessGrant]:
    clients = db.scalars(select(ClientAccount).where(
        ClientAccount.buyer_tenant_id == buyer_tenant_id)).all()
    if organisation_id:
        supplier = tenant_for(db, organisation_id)
        clients = [c for c in clients if supplier and c.tenant_id == supplier.id]
    if not clients:
        return []
    grants = db.scalars(select(AccessGrant).where(
        AccessGrant.client_id.in_([c.id for c in clients]))).all()
    return [g for g in grants if access.grant_is_live(g)]


def granted_documents(db: Session, buyer_tenant_id: str,
                      organisation_id: str) -> list[Document]:
    """Resolved through the same permission code the share link and the API use.
    A buyer viewing in their own account must not be a second answer to the
    question of what a grant permits."""
    seen: dict[str, Document] = {}
    for grant in live_grants(db, buyer_tenant_id, organisation_id):
        for document, _version in access.disclosable(db, grant):
            seen[document.id] = document
    return list(seen.values())


def connect_state(db: Session, buyer_tenant_id: str, link: SupplierLink) -> str:
    if live_grants(db, buyer_tenant_id, link.organisation_id):
        if link.state != "connected":
            link.state = "connected"
            link.connected_at = link.connected_at or now()
            db.flush()
        return "connected"
    return link.state


def summary(db: Session, buyer_tenant_id: str) -> dict:
    links = db.scalars(select(SupplierLink).where(and_(
        SupplierLink.buyer_tenant_id == buyer_tenant_id,
        SupplierLink.removed_at.is_(None)))).all()
    states = {"watching": 0, "invited": 0, "requested": 0, "connected": 0}
    for link in links:
        state = connect_state(db, buyer_tenant_id, link)
        states[state] = states.get(state, 0) + 1
    open_requests = db.scalar(select(func.count(AccessRequest.id)).where(and_(
        AccessRequest.requested_by_tenant_id == buyer_tenant_id,
        AccessRequest.status == "open"))) or 0
    return {"total": len(links), **states, "open_requests": open_requests,
            "searches_today": searches_today(db, buyer_tenant_id)}
