"""Profile claim - GICC-07 F27.

A successful claim lets somebody publish credentials **as that company** and
grant buyers access in its name. A wrongful claim is therefore not a
data-quality problem, it is impersonation, and every decision below follows
from that.

The model contributes at most 10 of 100 points and cannot by itself move a
claim between bands. A model can be fooled by a well-made forgery in a way that
a registry-number match cannot.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Organisation, ProfileClaim, Tenant, now
from . import audit
from .verification import name_match

AUTO_APPROVE = 80
REVIEW_FLOOR = 45
COOLDOWN_DAYS = 30

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "yandex.com", "qq.com",
}

KNOWN_REGISTRIES = {
    "companies house", "registrar of companies", "kamer van koophandel", "kvk",
    "handelsregister", "registre du commerce", "infogreffe", "registro mercantil",
    "camera di commercio", "bolagsverket", "brreg", "cvr", "asic", "acra",
    "mca", "sec", "delaware", "secretary of state", "dubai", "free zone",
    "department of economic development", "companies registration office",
}


def _domain(email: str) -> str:
    return email.split("@")[-1].strip().lower() if "@" in email else ""


def _site_domain(website: str | None) -> str:
    if not website:
        return ""
    cleaned = re.sub(r"^https?://", "", website.strip().lower()).split("/")[0]
    return cleaned[4:] if cleaned.startswith("www.") else cleaned


def search(db: Session, *, query: str, country: str = "", limit: int = 8) -> list[dict]:
    """F27.2 — results carry only public-register fields. Never document counts,
    never anything a claimant could use to make a claim more convincing."""
    text = (query or "").strip()
    if len(text) < 3:
        return []
    statement = select(Organisation).where(Organisation.legal_name.ilike(f"%{text}%"))
    if country:
        statement = statement.where(func.upper(Organisation.country) == country.upper())
    rows = db.scalars(statement.limit(limit * 3)).all()

    out = []
    for organisation in rows:
        address = (organisation.registered_address or "").splitlines()
        out.append({
            "id": organisation.id,
            "legal_name": organisation.legal_name,
            "country": organisation.country,
            "registry_number": organisation.registry_number,
            "incorporated_on": organisation.incorporated_on,
            # Masked: enough to recognise your own company, not enough to
            # impersonate one you have never seen.
            "locality": address[-2].strip() if len(address) > 1 else "",
            "claimable": organisation.claim_status == "unclaimed",
            "status": organisation.claim_status,
        })
        if len(out) >= limit:
            break
    return out


def open_claim_for(db: Session, organisation_id: str) -> ProfileClaim | None:
    return db.scalar(select(ProfileClaim).where(and_(
        ProfileClaim.organisation_id == organisation_id,
        ProfileClaim.status == "open")))


def in_cooldown(db: Session, email: str) -> date | None:
    """A second rejection imposes a cooldown. Claim farming is the abuse this
    feature invites."""
    rejections = db.scalars(select(ProfileClaim).where(and_(
        ProfileClaim.claimant_email == email.lower(),
        ProfileClaim.status == "rejected"))
        .order_by(ProfileClaim.decided_at.desc()).limit(2)).all()
    if len(rejections) >= 2 and rejections[0].decided_at:
        until = (rejections[0].decided_at + timedelta(days=COOLDOWN_DAYS)).date()
        if until >= date.today():
            return until
    return None


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def score_claim(db: Session, *, organisation: Organisation, extracted: dict,
                claimant_email: str, ai_source: str = "heuristic") -> dict:
    """Five independent signals. The model contributes one, capped at 10."""
    signals = []
    adverse = []

    # 1. Registry number — 35
    doc_number = (extracted.get("registry_number") or "").replace(" ", "").upper()
    profile_number = (organisation.registry_number or "").replace(" ", "").upper()
    if doc_number and profile_number and doc_number == profile_number:
        signals.append({"name": "Registry number matches", "points": 35, "max": 35,
                        "detail": f"{organisation.registry_number}"})
    elif doc_number and profile_number:
        signals.append({"name": "Registry number does not match", "points": 0, "max": 35,
                        "detail": f"document shows {doc_number}, "
                                  f"profile holds {profile_number}"})
        adverse.append("registry_number_conflict")
    else:
        signals.append({"name": "Registry number unavailable", "points": 0, "max": 35,
                        "detail": "not present on the document or the profile"})

    # 2. Legal name — 25
    match = name_match(extracted.get("legal_name", ""), organisation.legal_name)
    points = {"exact": 25, "partial": 15}.get(match, 0)
    signals.append({"name": f"Legal name {match}", "points": points, "max": 25,
                    "detail": extracted.get("legal_name") or "not extracted"})
    if match == "mismatch" and extracted.get("legal_name"):
        adverse.append("name_conflict")

    # 3. Registry lookup — 20. Unavailable is not the same as failed.
    lookup = _registry_lookup(organisation, extracted)
    signals.append(lookup)
    if lookup.get("adverse"):
        adverse.append("registry_lookup_conflict")

    # 4. Email domain — 10
    claimant_domain = _domain(claimant_email)
    site = _site_domain(organisation.website)
    if site and claimant_domain and claimant_domain == site:
        signals.append({"name": "Email domain matches the company website",
                        "points": 10, "max": 10, "detail": claimant_domain})
    elif claimant_domain in FREE_EMAIL_DOMAINS:
        signals.append({"name": "Free email domain", "points": 0, "max": 10,
                        "detail": claimant_domain})
    else:
        signals.append({"name": "Email domain not corroborated", "points": 3, "max": 10,
                        "detail": claimant_domain or "unknown"})

    # 5. Document plausibility — 10, and this is the only AI contribution.
    plausibility = _plausibility(extracted, organisation)
    signals.append(plausibility)
    adverse.extend(plausibility.get("flags", []))

    total = sum(s["points"] for s in signals)

    # Adverse conditions force review whatever the score (F27.7).
    if organisation.claim_status != "unclaimed":
        adverse.append("profile_not_claimable")
    if claimant_domain in FREE_EMAIL_DOMAINS and total < 90:
        adverse.append("free_email_below_threshold")
    if organisation.source == "import":
        adverse.append("import_sourced_profile")

    reused = db.scalar(select(func.count(ProfileClaim.id)).where(and_(
        ProfileClaim.evidence_hash == extracted.get("_evidence_hash", "-"),
        ProfileClaim.organisation_id != organisation.id)))
    if reused:
        adverse.append("evidence_reused_across_claims")

    if total >= AUTO_APPROVE and not adverse:
        band = "auto_approve"
    elif total >= REVIEW_FLOOR or adverse:
        band = "review" if total >= REVIEW_FLOOR else "reject"
    else:
        band = "reject"
    # An adverse flag can never produce an auto-approval.
    if adverse and band == "auto_approve":
        band = "review"

    return {"score": total, "band": band, "signals": signals,
            "adverse": sorted(set(adverse)), "ai_source": ai_source}


def _registry_lookup(organisation: Organisation, extracted: dict) -> dict:
    """Simulated, exactly as the rest of the verification layer is. A live
    connector replaces this function and nothing else changes."""
    number = organisation.registry_number or extracted.get("registry_number")
    if not number:
        return {"name": "Registry lookup not possible", "points": 0, "max": 20,
                "detail": "no registry number to look up", "simulated": True}
    issuer = (extracted.get("issuing_authority") or "").lower()
    recognised = any(registry in issuer for registry in KNOWN_REGISTRIES)
    if recognised:
        return {"name": "Registry lookup confirms the record", "points": 20, "max": 20,
                "detail": f"{extracted.get('issuing_authority')} (simulated)",
                "simulated": True}
    if issuer:
        return {"name": "Issuing authority not recognised", "points": 5, "max": 20,
                "detail": f"'{extracted.get('issuing_authority')}' is not on our list "
                          "of known registries — our list may simply be incomplete",
                "simulated": True}
    return {"name": "Issuing authority not stated", "points": 0, "max": 20,
            "detail": "the document does not name a registry", "simulated": True}


def _plausibility(extracted: dict, organisation: Organisation) -> dict:
    """Capped at 10 points by design (K3)."""
    flags = []
    points = 10
    detail = []

    jurisdiction = (extracted.get("jurisdiction") or "").upper()[:2]
    if jurisdiction and organisation.country and jurisdiction != organisation.country.upper():
        flags.append("jurisdiction_conflict")
        points -= 6
        detail.append(f"document jurisdiction {jurisdiction} vs profile "
                      f"{organisation.country}")

    incorporated = extracted.get("incorporated_on")
    if incorporated and organisation.incorporated_on:
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(str(incorporated)).date()
            if abs((parsed - organisation.incorporated_on).days) > 370:
                flags.append("incorporation_date_conflict")
                points -= 4
                detail.append("incorporation date differs by more than a year")
        except (ValueError, TypeError):
            pass

    if not extracted.get("issuing_authority"):
        points -= 3
        detail.append("no issuing authority found in the document")
    if not extracted.get("legal_name"):
        points -= 3
        detail.append("no legal name found in the document")

    return {"name": "Document plausibility", "points": max(0, points), "max": 10,
            "detail": "; ".join(detail) or "no anomalies found",
            "flags": flags, "ai": True}


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def submit(db: Session, *, organisation: Organisation, claimant_email: str,
           claimant_name: str, evidence_path: str, evidence_hash: str,
           evidence_filename: str, extracted: dict, ai_source: str) -> ProfileClaim:
    extracted = {**extracted, "_evidence_hash": evidence_hash}
    result = score_claim(db, organisation=organisation, extracted=extracted,
                         claimant_email=claimant_email, ai_source=ai_source)

    claim = ProfileClaim(
        organisation_id=organisation.id, claimant_email=claimant_email.lower(),
        claimant_name=claimant_name or None, evidence_file_path=evidence_path,
        evidence_hash=evidence_hash, evidence_filename=evidence_filename,
        extracted_json=json.dumps(extracted, default=str)[:4000],
        signals_json=json.dumps(result["signals"], default=str)[:4000],
        score=result["score"], band=result["band"],
        adverse_flags=",".join(result["adverse"]) or None,
        status="open")
    db.add(claim)

    if organisation.claim_status == "unclaimed":
        organisation.claim_status = "claim_pending"
    db.flush()

    audit.record(db, tenant_id="platform", action="claim.submitted",
                 actor_kind="system", actor_label=claimant_email,
                 object_type="profile_claim", object_id=claim.id,
                 payload={"organisation": organisation.legal_name,
                          "score": result["score"], "band": result["band"],
                          "adverse": result["adverse"], "ai_source": ai_source})
    return claim


def contested(db: Session, organisation_id: str) -> list[ProfileClaim]:
    return list(db.scalars(select(ProfileClaim).where(and_(
        ProfileClaim.organisation_id == organisation_id,
        ProfileClaim.status == "open")).order_by(ProfileClaim.created_at)).all())


def approve(db: Session, claim: ProfileClaim, *, tenant: Tenant, actor=None,
            note: str = "") -> None:
    organisation = db.get(Organisation, claim.organisation_id)
    claim.status = "approved"
    claim.claimant_tenant_id = tenant.id
    claim.decided_by = getattr(actor, "email", "system")
    claim.decided_at = now()
    claim.decision_note = note or None

    organisation.claim_status = "claimed"
    organisation.claimed_at = now()
    organisation.claimed_by_tenant_id = tenant.id
    # F27.10 — the claim proves identity, not credentials. Entity verification
    # reflects only what was actually proved.
    if organisation.registry_number and claim.score >= AUTO_APPROVE:
        organisation.verification_state = "registry_verified"
    else:
        organisation.verification_state = "unverified"

    # Any competing claim is superseded, not silently dropped.
    for other in contested(db, organisation.id):
        if other.id != claim.id:
            other.status = "superseded"
            other.decided_at = now()
            other.decision_note = "Another claim on this profile was approved"
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="claim.approved",
                 actor_kind="platform_admin" if actor else "system",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="profile_claim", object_id=claim.id,
                 payload={"organisation": organisation.legal_name,
                          "score": claim.score, "band": claim.band})


def reject(db: Session, claim: ProfileClaim, *, actor=None, note: str = "") -> None:
    organisation = db.get(Organisation, claim.organisation_id)
    claim.status = "rejected"
    claim.decided_by = getattr(actor, "email", "system")
    claim.decided_at = now()
    claim.decision_note = note or "The evidence did not establish ownership"
    if organisation and not contested(db, organisation.id):
        organisation.claim_status = "unclaimed"
    db.flush()
    audit.record(db, tenant_id="platform", action="claim.rejected",
                 actor_kind="platform_admin" if actor else "system",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="profile_claim", object_id=claim.id,
                 payload={"score": claim.score, "note": claim.decision_note})


def reverse(db: Session, claim: ProfileClaim, *, actor, reason: str) -> None:
    """F27.12 — a wrongful claim must be undoable. The workspace is suspended
    rather than deleted, because the evidence of what happened matters."""
    organisation = db.get(Organisation, claim.organisation_id)
    claim.status = "reversed"
    claim.reversed_at = now()
    claim.reversal_reason = reason
    if organisation:
        organisation.claim_status = "unclaimed"
        organisation.claimed_at = None
        organisation.claimed_by_tenant_id = None
        organisation.verification_state = "unverified"
    if claim.claimant_tenant_id:
        tenant = db.get(Tenant, claim.claimant_tenant_id)
        if tenant:
            tenant.lifecycle_state = "suspended"
            tenant.suspension_reason = f"Profile claim reversed: {reason}"
            tenant.suspended_at = now()
            audit.record(db, tenant_id=tenant.id, action="tenant.suspended",
                         actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                         actor_label=getattr(actor, "email", "operator"),
                         object_type="tenant", object_id=tenant.id,
                         payload={"ground": "claim_reversed", "reason": reason})
    db.flush()
    audit.record(db, tenant_id="platform", action="claim.reversed",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="profile_claim", object_id=claim.id,
                 payload={"reason": reason})


def queue(db: Session) -> list[ProfileClaim]:
    return list(db.scalars(select(ProfileClaim).where(
        ProfileClaim.status == "open").order_by(ProfileClaim.created_at)).all())
