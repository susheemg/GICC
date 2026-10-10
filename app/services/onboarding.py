"""Guided self-registration - GICC-06 F24.

Five steps, each skippable, all resumable. The design rule that shapes this
module: a step is credited by outcome, not by the supplier pressing Next. Step
3 completes when a version is published, step 5 when a grant exists. Measuring
checklist completion instead of outcome delivery is how the industry kept a 37%
median activation rate for four years while believing it was improving.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (AccessGrant, CertificateGroup, ClientAccount, Document,
                      DocumentVersion, OnboardingProgress, ShareLinkView, Tenant, now)
from . import audit

TOTAL_STEPS = 5

STEPS: dict[int, dict] = {
    1: {"key": "entity", "title": "Confirm your company",
        "why": "Buyers check the name on a certificate against the registered entity "
               "first. Getting this right is what lets a credential reach L3.",
        "unlocks": "Entity verification badge", "url": "/start/1"},
    2: {"key": "buyers", "title": "Tell us who buys from you",
        "why": "Different buyers demand different evidence. This is the only question "
               "that decides what we recommend, so it is worth thirty seconds.",
        "unlocks": "A recommended document set instead of 198 choices", "url": "/start/2"},
    3: {"key": "credential", "title": "Add your first credential",
        "why": "Uploading one certificate and having it verified is the fastest way to "
               "see what this product actually does.",
        "unlocks": "Your first verified credential — milestone M1", "url": "/start/3"},
    4: {"key": "package", "title": "Package what you share",
        "why": "A group is what you hand to a buyer. Fourteen are already set up; you "
               "only need to pick which one is your default pack.",
        "unlocks": "A shareable pack", "url": "/start/4"},
    5: {"key": "share", "title": "Share with a buyer",
        "why": "Everything before this is a filing cabinet. A grant is the moment "
               "somebody outside starts relying on you.",
        "unlocks": "Your first client grant — milestone M2", "url": "/start/5"},
}

# Buyer type -> (label, requirement profile name, headline recommendations)
BUYER_TYPES: dict[str, tuple[str, str | None, list[str]]] = {
    "financial": ("Financial services", "UK financial services buyer",
                  ["SEC-001", "SEC-008", "SEC-017", "SEC-026", "SEC-027",
                   "INS-003", "INS-004"]),
    "public": ("Public sector", "Public sector / government framework",
               ["INS-001", "INS-002", "HSE-001", "ESG-008", "SEC-012", "TAX-004"]),
    "construction": ("Construction and infrastructure", "Construction main contractor",
                     ["INS-001", "INS-002", "HSE-001", "HSE-002", "HSE-005", "HSE-011"]),
    "healthcare": ("Healthcare and life sciences", "Healthcare & life sciences buyer",
                   ["SEC-001", "SEC-005", "SEC-027", "SEC-028", "QMS-007", "PPL-003"]),
    "general": ("General commercial", None,
                ["LEG-001", "TAX-001", "INS-001", "INS-003", "SEC-030"]),
}

SECTORS = [
    "Software and IT services", "Professional services", "Facilities management",
    "Construction", "Manufacturing", "Logistics and transport",
    "Healthcare and life sciences", "Financial services", "Recruitment and staffing",
    "Energy and utilities", "Retail and consumer", "Other",
]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def get_or_create(db: Session, tenant: Tenant) -> OnboardingProgress:
    progress = db.scalar(select(OnboardingProgress).where(
        OnboardingProgress.tenant_id == tenant.id))
    if progress is None:
        progress = OnboardingProgress(
            tenant_id=tenant.id, current_step=1,
            registered_at=tenant.created_at or now(),
            steps_json=json.dumps({}))
        db.add(progress)
        db.flush()
    return progress


def _steps(progress: OnboardingProgress) -> dict:
    try:
        return json.loads(progress.steps_json or "{}")
    except ValueError:
        return {}


def _save(progress: OnboardingProgress, steps: dict) -> None:
    progress.steps_json = json.dumps(steps, default=str)


def state(db: Session, tenant: Tenant) -> dict:
    """Everything a template needs to render the guide."""
    progress = get_or_create(db, tenant)
    steps = _steps(progress)
    rows = []
    for number, meta in STEPS.items():
        entry = steps.get(str(number), {})
        rows.append({
            "number": number, **meta,
            "status": entry.get("status", "pending"),
            "completed_at": entry.get("completed_at"),
            "skipped": entry.get("status") == "skipped",
            "done": entry.get("status") == "complete",
        })
    done = sum(1 for r in rows if r["status"] in {"complete", "skipped"})
    return {
        "progress": progress, "steps": rows, "total": TOTAL_STEPS,
        "done": done, "percent": round(done / TOTAL_STEPS * 100),
        "current": progress.current_step,
        "complete": progress.completed_at is not None,
        "dismissed": progress.dismissed,
        "milestones": {"m1": progress.m1_at, "m2": progress.m2_at, "m3": progress.m3_at},
    }


def mark(db: Session, tenant: Tenant, number: int, status: str, actor=None) -> None:
    """Records a step outcome. Skips are recorded as deliberately as completions:
    a step everyone skips is a step that should not exist (J2)."""
    progress = get_or_create(db, tenant)
    steps = _steps(progress)
    entry = steps.get(str(number), {})
    if entry.get("status") == "complete" and status != "complete":
        return                                    # never downgrade a completion
    entry["status"] = status
    entry.setdefault("started_at", now().isoformat())
    if status in {"complete", "skipped"}:
        entry["completed_at"] = now().isoformat()
    steps[str(number)] = entry
    _save(progress, steps)

    settled = {n for n, e in steps.items() if e.get("status") in {"complete", "skipped"}}
    nxt = next((n for n in range(1, TOTAL_STEPS + 1) if str(n) not in settled),
               TOTAL_STEPS + 1)
    progress.current_step = nxt
    if nxt > TOTAL_STEPS and progress.completed_at is None:
        progress.completed_at = now()
        audit.record(db, tenant_id=tenant.id, action="onboarding.completed",
                     actor_id=getattr(actor, "id", None),
                     actor_label=getattr(actor, "email", "system"),
                     payload={"minutes": _minutes(progress.registered_at, now())})
    db.flush()


def dismiss(db: Session, tenant: Tenant, actor=None) -> None:
    progress = get_or_create(db, tenant)
    progress.dismissed = True
    audit.record(db, tenant_id=tenant.id, action="onboarding.dismissed",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 payload={"at_step": progress.current_step})
    db.flush()


def reopen(db: Session, tenant: Tenant) -> None:
    progress = get_or_create(db, tenant)
    progress.dismissed = False
    db.flush()


def set_profile(db: Session, tenant: Tenant, sector: str, buyer_types: list[str]) -> None:
    progress = get_or_create(db, tenant)
    progress.sector = sector or None
    progress.buyer_types = ",".join(buyer_types) or None
    db.flush()


# --------------------------------------------------------------------------
# Branching
# --------------------------------------------------------------------------
def recommended_types(db: Session, tenant: Tenant, limit: int = 6) -> list:
    """The branch point. Showing six recommendations beats showing 198 choices;
    the full catalogue stays one click away."""
    from ..models import DocumentType

    progress = get_or_create(db, tenant)
    selected = [b for b in (progress.buyer_types or "general").split(",") if b]
    codes: list[str] = []
    for buyer in selected or ["general"]:
        for code in BUYER_TYPES.get(buyer, BUYER_TYPES["general"])[2]:
            if code not in codes:
                codes.append(code)

    held = {d.document_type_id for d in db.scalars(select(Document).where(and_(
        Document.tenant_id == tenant.id, Document.deleted_at.is_(None)))).all()}
    out = []
    for code in codes:
        dtype = db.scalar(select(DocumentType).where(DocumentType.code == code))
        if dtype and dtype.id not in held:
            out.append(dtype)
        if len(out) >= limit:
            break
    return out


def profiles_for(db: Session, tenant: Tenant) -> list:
    from ..models import RequirementProfile

    progress = get_or_create(db, tenant)
    names = {BUYER_TYPES[b][1] for b in (progress.buyer_types or "").split(",")
             if b in BUYER_TYPES and BUYER_TYPES[b][1]}
    if not names:
        return []
    return list(db.scalars(select(RequirementProfile).where(
        RequirementProfile.name.in_(names))).all())


# --------------------------------------------------------------------------
# Milestone observation
# --------------------------------------------------------------------------
def observe(db: Session, tenant: Tenant) -> dict:
    """Stamps milestones from what is true, not from what was clicked (J1).

    Idempotent and cheap, so it can be called after any action that might
    satisfy one. Correct even when a supplier reaches a milestone by a route
    the guide never showed them — which happens.
    """
    progress = get_or_create(db, tenant)
    reached = []

    if progress.m1_at is None:
        verified = db.scalar(select(DocumentVersion.id).where(and_(
            DocumentVersion.tenant_id == tenant.id,
            DocumentVersion.status == "active",
            DocumentVersion.assurance_level.in_(["L3", "L4"]))).limit(1))
        if verified:
            progress.m1_at = now()
            reached.append("m1")

    if progress.m2_at is None:
        granted = db.scalar(select(AccessGrant.id).where(and_(
            AccessGrant.tenant_id == tenant.id,
            AccessGrant.status == "active")).limit(1))
        if granted:
            progress.m2_at = now()
            reached.append("m2")

    if progress.m3_at is None:
        read = db.scalar(select(ShareLinkView.id).where(and_(
            ShareLinkView.tenant_id == tenant.id,
            ShareLinkView.action.in_(["view", "download"]))).limit(1))
        if read:
            progress.m3_at = now()
            reached.append("m3")

    for milestone in reached:
        elapsed = _minutes(progress.registered_at,
                           getattr(progress, f"{milestone}_at"))
        audit.record(db, tenant_id=tenant.id, action=f"milestone.{milestone}",
                     actor_kind="system", actor_label="onboarding",
                     payload={"minutes_from_registration": elapsed})
    if reached:
        db.flush()
    return {"reached": reached, "m1": progress.m1_at, "m2": progress.m2_at,
            "m3": progress.m3_at}


def sync_steps(db: Session, tenant: Tenant) -> None:
    """Credits steps 3 and 5 from the facts. A supplier who uploads a document
    from the register rather than the guide has still done step 3."""
    progress = get_or_create(db, tenant)
    steps = _steps(progress)

    if steps.get("3", {}).get("status") != "complete":
        published = db.scalar(select(DocumentVersion.id).where(and_(
            DocumentVersion.tenant_id == tenant.id,
            DocumentVersion.status == "active")).limit(1))
        if published:
            mark(db, tenant, 3, "complete")
            steps = _steps(progress)

    if steps.get("5", {}).get("status") != "complete":
        granted = db.scalar(select(AccessGrant.id).where(and_(
            AccessGrant.tenant_id == tenant.id,
            AccessGrant.status == "active")).limit(1))
        if granted:
            mark(db, tenant, 5, "complete")


def _minutes(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    return int((end - start).total_seconds() // 60)


def next_action(db: Session, tenant: Tenant) -> dict | None:
    """One action, named. Not a list."""
    current = state(db, tenant)
    if current["complete"]:
        return None
    for row in current["steps"]:
        if row["status"] == "pending":
            return {"step": row["number"], "title": row["title"],
                    "unlocks": row["unlocks"], "url": row["url"]}
    return None
