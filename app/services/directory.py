"""The public company directory behind the home page search.

Two sources, merged into one result list.

**Members first.** A company registered with the Trusted Supplier Program is
what the search exists to surface, so members sort above everything else and
carry the star. That ordering is the product: a buyer who searches a name and
finds the supplier already holds verified credentials has just learned the only
thing the home page needs to teach them.

**Everyone else from the open register.** OpenCorporates where a token is
configured, the bundled register extract otherwise. The fallback is named in
the response rather than hidden, because a directory that quietly answers from
stale local data while implying it is live is worse than one that admits it.

**What a stranger may see.** This search is public and unauthenticated, so the
result carries only what a company register puts on its own front page: name,
jurisdiction, company number, status, and whether they are a member. No
address, no contact, no credential count — the registered address in particular
stays behind a login, because for a great many small companies it is a home
address and a public, searchable directory is not the same act as a register
lookup. Opening a result asks the visitor to sign in, and that is where the
identity block appears.
"""
from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..config import OPENCORPORATES_BASE, OPENCORPORATES_TOKEN
from ..models import Organisation, Tenant

MAX_RESULTS = 20
MIN_QUERY = 2


def _member_rows(db: Session, query: str, limit: int) -> list[dict]:
    """Registered members - organisations with a live workspace."""
    text = query.strip()
    rows = db.execute(
        select(Organisation, Tenant)
        .join(Tenant, Tenant.organisation_id == Organisation.id)
        .where(and_(Tenant.lifecycle_state == "active",
                    # A buyer is on the platform but is not a supplier, so it
                    # does not get the star.
                    Tenant.kind != "buyer",
                    Organisation.claim_status == "claimed",
                    or_(Organisation.legal_name.ilike(f"%{text}%"),
                        Organisation.trading_name.ilike(f"%{text}%"),
                        Organisation.registry_number == text)))
        .limit(limit)).all()
    return [{
        "id": organisation.id,
        "name": organisation.legal_name,
        "jurisdiction": organisation.country,
        "company_number": organisation.registry_number,
        "status": "Active",
        "member": True,
        "slug": tenant.slug,
        "source": "Trusted Supplier Program",
    } for organisation, tenant in rows]


def _local_rows(db: Session, query: str, limit: int,
                exclude: set[str]) -> list[dict]:
    text = query.strip()
    rows = db.scalars(
        select(Organisation)
        .where(and_(Organisation.claim_status != "claimed",
                    or_(Organisation.legal_name.ilike(f"%{text}%"),
                        Organisation.registry_number == text)))
        .limit(limit * 2)).all()
    out = []
    for organisation in rows:
        if organisation.id in exclude:
            continue
        status = "Active"
        note = organisation.description or ""
        for word in ("Struck off", "Dissolved", "Amalgamated", "Converted",
                     "Inactive"):
            if word in note:
                status = word
                break
        out.append({
            "id": organisation.id,
            "name": organisation.legal_name,
            "jurisdiction": organisation.country,
            "company_number": organisation.registry_number,
            "status": status,
            "member": False,
            "slug": None,
            "source": "Public register",
        })
        if len(out) >= limit:
            break
    return out


def _opencorporates_rows(query: str, limit: int) -> tuple[list[dict], str | None]:
    """Live lookup. Returns (rows, error); an error is surfaced, not swallowed."""
    if not OPENCORPORATES_TOKEN:
        return [], None
    try:
        import httpx

        response = httpx.get(f"{OPENCORPORATES_BASE}/companies/search",
                             params={"q": query, "per_page": limit,
                                     "api_token": OPENCORPORATES_TOKEN},
                             timeout=12)
        if response.status_code == 401:
            return [], "The OpenCorporates token was rejected."
        if response.status_code == 429:
            return [], "OpenCorporates rate limit reached."
        if response.status_code != 200:
            return [], f"OpenCorporates returned HTTP {response.status_code}."
        payload = response.json().get("results", {}).get("companies", [])
    except Exception as exc:
        return [], f"OpenCorporates unreachable: {type(exc).__name__}."

    rows = []
    for entry in payload:
        company = entry.get("company", {})
        rows.append({
            "id": None,
            "name": company.get("name"),
            "jurisdiction": (company.get("jurisdiction_code") or "")[:2].upper(),
            "company_number": company.get("company_number"),
            "status": company.get("current_status") or "Unknown",
            "member": False,
            "slug": None,
            "source": "OpenCorporates",
            "opencorporates_url": company.get("opencorporates_url"),
        })
    return rows, None


def search(db: Session, query: str, limit: int = MAX_RESULTS) -> dict:
    text = (query or "").strip()
    if len(text) < MIN_QUERY:
        return {"results": [], "members": 0, "provider": None, "notice": None}

    members = _member_rows(db, text, limit)
    remaining = max(0, limit - len(members))

    live, error = _opencorporates_rows(text, remaining)
    if live:
        others, provider = live, "OpenCorporates"
        notice = None
    else:
        others = _local_rows(db, text, remaining, {m["id"] for m in members})
        provider = "Public register extract"
        notice = error or (
            "Showing a bundled register extract. Set OPENCORPORATES_TOKEN to "
            "search the live OpenCorporates index."
            if not OPENCORPORATES_TOKEN else None)

    return {"results": members + others, "members": len(members),
            "provider": provider, "notice": notice, "query": text}
