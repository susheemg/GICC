"""Bootstrap: create tables, seed platform reference data, optionally create a
worked demo tenant so the deployed app is explorable on first load."""
from __future__ import annotations

import secrets

import random
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import catalogue as cat
from .config import (ADMIN_EMAIL, ADMIN_NAME, ADMIN_PASSWORD, DEMO_EMAIL,
                     DEMO_PASSWORD, SEED_DEMO)
from .db import Base, SessionLocal, engine
from .models import (AccessGrant, CertificateGroup, ClientAccount, ClientContact,
                     Document, DocumentType, DocumentVersion, EntityIdentifier,
                     GroupItem, Organisation, PlatformAdmin, RequirementItem,
                     RequirementProfile, Tenant, User, now)
from .security import hash_password
from .services import access, audit, documents as docsvc, verification


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_catalogue(db)
        seed_requirement_templates(db)
        seed_platform_admin(db)
        db.commit()
        if SEED_DEMO and db.scalar(select(func.count(Tenant.id))) == 0:
            seed_demo_tenant(db)
            db.commit()


def seed_platform_admin(db: Session) -> None:
    """Bootstrap operator. Credentials come from the environment - change them
    before any real deployment."""
    from .config import ADMIN_RESET

    if db.scalar(select(func.count(PlatformAdmin.id))):
        if not ADMIN_RESET:
            return
        # Deliberate recovery, and loud about it: an operator lockout should be
        # fixable, and a silent credential rewrite should not be possible.
        existing = db.scalar(select(PlatformAdmin).where(
            func.lower(PlatformAdmin.email) == ADMIN_EMAIL.lower()))
        if existing is None:
            existing = db.scalar(select(PlatformAdmin))
            if existing is not None:
                existing.email = ADMIN_EMAIL.lower()
        if existing is not None:
            existing.password_hash = hash_password(ADMIN_PASSWORD)
            existing.is_active = True
            print(f"[admin] ADMIN_RESET applied: {existing.email} password reset. "
                  "Unset ADMIN_RESET and redeploy.", flush=True)
            return
    db.add(PlatformAdmin(email=ADMIN_EMAIL.lower(), full_name=ADMIN_NAME,
                         password_hash=hash_password(ADMIN_PASSWORD),
                         title="Platform administrator"))


def seed_catalogue(db: Session) -> None:
    existing = {code for code in db.scalars(select(DocumentType.code)).all()}
    for (code, name, issuer, validity, months, route, sensitivity, criticality) in cat.TYPES:
        if code in existing:
            continue
        db.add(DocumentType(
            code=code, domain_code=code[:3], domain_name=cat.DOMAINS[code[:3]],
            name=name, typical_issuer=issuer, validity_label=validity,
            validity_months=months, verification_route=route,
            sensitivity=sensitivity, criticality=criticality,
            verification_required=route != "manual",
            multi_instance=code in cat.MULTI_INSTANCE,
            requires_asset=code in cat.REQUIRES_ASSET,
            scope="global"))


def seed_requirement_templates(db: Session) -> None:
    for name, sector, jurisdiction, description, items in cat.REQUIREMENT_TEMPLATES:
        if db.scalar(select(RequirementProfile).where(RequirementProfile.name == name)):
            continue
        profile = RequirementProfile(name=name, sector=sector, jurisdiction=jurisdiction,
                                     description=description, audience="platform_template")
        db.add(profile)
        db.flush()
        for code, mandatory, min_assurance, max_age, min_cover, weight in items:
            db.add(RequirementItem(profile_id=profile.id, type_code=code,
                                   mandatory=mandatory, min_assurance=min_assurance,
                                   max_age_days=max_age, min_cover_amount=min_cover,
                                   weight=weight))


def provision_tenant(db: Session, *, legal_name: str, country: str, email: str,
                     full_name: str, password: str | None,
                     registry_number: str | None = None, slug: str | None = None,
                     existing_organisation: Organisation | None = None
                     ) -> tuple[Tenant, User]:
    # A claimed profile binds to the organisation that was imported, rather than
    # creating a second record for the same company (GICC-07 F27).
    if existing_organisation is not None:
        org = existing_organisation
    else:
        org = Organisation(
            legal_name=legal_name, country=country, registry_number=registry_number,
            verification_state="registry_verified" if registry_number else "unverified")
        db.add(org)
    db.flush()

    base_slug = slug or "".join(ch for ch in legal_name.lower().replace(" ", "-")
                                if ch.isalnum() or ch == "-")[:40] or "tenant"
    candidate, n = base_slug, 1
    while db.scalar(select(Tenant).where(Tenant.slug == candidate)):
        n += 1
        candidate = f"{base_slug}-{n}"

    tenant = Tenant(organisation_id=org.id, name=legal_name, slug=candidate)
    db.add(tenant)
    db.flush()

    user = User(tenant_id=tenant.id, email=email.lower(), full_name=full_name,
                password_hash=hash_password(password or secrets.token_urlsafe(24)),
                role="owner", sensitive_access=True)
    db.add(user)

    if registry_number:
        db.add(EntityIdentifier(tenant_id=tenant.id, kind="registry", value=registry_number,
                                verification_state="verified", verified_at=now(),
                                source="Company registry (simulated)"))
    seed_default_groups(db, tenant)
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="tenant.created",
                 actor_label=email, object_type="tenant", object_id=tenant.id,
                 payload={"legal_name": legal_name, "region": tenant.data_region})
    return tenant, user


def seed_default_groups(db: Session, tenant: Tenant) -> None:
    for (code, name, ceiling, nda, watermark, download, max_days, order, prefixes) in cat.DEFAULT_GROUPS:
        group = CertificateGroup(
            tenant_id=tenant.id, code=code, name=name, sensitivity_ceiling=ceiling,
            nda_required=nda, watermark_required=watermark, download_allowed=download,
            max_grant_days=max_days, is_system=True, display_order=order,
            description=f"Seeded default group covering {', '.join(prefixes[:3])}"
                        + ("..." if len(prefixes) > 3 else ""))
        db.add(group)
        db.flush()
        for prefix in prefixes:
            db.add(GroupItem(tenant_id=tenant.id, group_id=group.id,
                             item_kind="type_rule", type_prefix=prefix))


# --------------------------------------------------------------------------
# Demo data lives in app/demo.py - six synthetic entities with backdated
# history. Kept out of this module so the bootstrap path stays readable.
# --------------------------------------------------------------------------
def seed_demo_tenant(db: Session) -> None:
    from . import demo, demo_directory
    demo.build(db)
    # Real companies from the public register, loaded as unclaimed profiles so
    # buyer search has genuine scale. They hold no credentials - see the note at
    # the top of demo_directory.py for why that is not negotiable.
    loaded = demo_directory.build(db)
    if loaded:
        print(f"[seed] directory: {loaded} unclaimed company profiles", flush=True)
