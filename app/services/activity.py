"""The supplier's activity feed.

The audit log is a forensic record; this is the readable version of the same
rows. Nothing is filtered out - a supplier can see every action taken on their
account, whoever took it, including the platform operator.

Actor classes, which are what the filter tabs are built on:
  team      - the supplier's own users
  buyer     - client contacts opening shared documents
  agent     - API keys and MCP agents
  platform  - Anthropic-side operators using break-glass or admin tools
  system    - scheduled and automatic actions
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from ..models import AuditEvent, ClientAccount, Document, Tenant, now

ACTOR_CLASS = {
    "user": "team",
    "client_contact": "buyer",
    "agent": "agent",
    "api_client": "agent",
    "platform_admin": "platform",
    "system": "system",
}

CLASS_LABEL = {"team": "Your team", "buyer": "Buyer", "agent": "Agent",
               "platform": "Platform", "system": "Automatic"}
CLASS_TONE = {"team": "info", "buyer": "ok", "agent": "neutral",
              "platform": "bad", "system": "neutral"}

# action -> (icon-free plain sentence template, significance)
# {actor} is filled in by the caller; {x} pulls from the payload.
TEMPLATES: dict[str, tuple[str, str]] = {
    "tenant.created": ("created the workspace", "normal"),
    "tenant.closing": ("started closing the account — {reason}", "high"),
    "tenant.closure_cancelled": ("cancelled the account closure", "high"),
    "tenant.suspended": ("suspended this account — {reason}", "high"),
    "tenant.reinstated": ("reinstated this account", "high"),
    "erasure.completed": ("purged the account: {documents_purged} documents removed", "high"),
    "export.completed": ("exported the full account data", "normal"),

    "user.signed_in": ("signed in", "low"),
    "user.invited": ("invited a colleague", "normal"),
    "user.created_by_platform": ("created the user {email} as {role}", "high"),
    "user.role_changed_by_platform": ("changed a user's role", "high"),
    "user.password_reset_by_platform": ("reset the password for {email}", "high"),
    "user.disabled_by_platform": ("disabled the user {email}", "high"),
    "user.enabled_by_platform": ("re-enabled the user {email}", "high"),
    "user.moved_in_by_platform": ("moved {email} into this workspace", "high"),
    "user.moved_out_by_platform": ("moved {email} out to {to}", "high"),

    "profile.updated": ("updated the company profile", "low"),
    "document.created": ("added {title} to the register", "normal"),
    "version.staged": ("drafted version {version} of {document}", "low"),
    "version.published": ("published version {version} of {document}", "normal"),
    "version.scheduled": ("scheduled a version to take effect on {effective_from}", "normal"),
    "version.superseded": ("superseded an earlier version", "low"),
    "version.withdrawn": ("withdrew a version — {reason}", "high"),
    "version.expired": ("{title} expired", "high"),
    "document.downloaded": ("downloaded {document}", "normal"),
    "document.viewed": ("opened {document}", "normal"),
    "legal_hold.placed": ("placed a legal hold — {reason}", "high"),
    "legal_hold.released": ("released a legal hold", "high"),

    "verification.completed": ("verified a credential: {result} ({method})", "normal"),
    "verification.manual_decision": ("recorded a manual review: {outcome}", "normal"),
    "assurance.changed": ("assurance moved from {from} to {to}", "normal"),

    "group.policy_changed": ("changed a certificate group's disclosure policy", "normal"),
    "client.created": ("added the client {name}", "normal"),
    "client.contact_added": ("added the contact {email}", "low"),
    "client.offboarded": ("offboarded a client — {grants_revoked} grants revoked", "high"),
    "grant.created": ("granted {client} access to {scope} for {days} days", "normal"),
    "grant.revoked": ("revoked an access grant — {reason}", "high"),
    "agreement.accepted": ("accepted the confidentiality terms", "normal"),
    "viewer.identified": ("identified themselves to open the shared pack", "low"),
    "share_link.created": ("created a share link", "normal"),
    "share_link.viewed": ("opened the shared pack ({documents_visible} documents)", "normal"),
    "share_link.revoked": ("revoked a share link", "normal"),
    "access.denied": ("was refused a document outside their grant", "high"),
    "access_request.raised": ("requested access to {group}", "normal"),
    "access_request.decided": ("decided an access request: {decision}", "normal"),

    "requirement.evaluated": ("ran a conformance check: {profile} scored {score}%", "normal"),
    "task.closed": ("closed a task", "low"),
    "api_client.created": ("created the API key {name}", "high"),
    "api_client.revoked": ("revoked an API key", "high"),

    "api.profile_read": ("read the company profile over the API", "low"),
    "api.documents_listed": ("listed {returned} credentials over the API", "low"),
    "api.document_read": ("read a credential over the API", "low"),
    "api.expiries_listed": ("listed upcoming expiries over the API", "low"),
    "api.verifications_read": ("read verification history over the API", "low"),
    "api.requirements_evaluated": ("ran a conformance check over the API", "normal"),

    "mcp.profile_read": ("read the company profile", "low"),
    "mcp.documents_listed": ("listed {returned} credentials", "low"),
    "mcp.document_read": ("read credential detail", "low"),
    "mcp.verification_read": ("checked a verification record", "low"),
    "mcp.expiries_listed": ("listed upcoming expiries", "low"),
    "mcp.content_disclosed": ("was shown the contents of {document}", "high"),
    "mcp.requirements_evaluated": ("ran a conformance check: {profile}", "normal"),

    "support.break_glass": ("opened a support session on this account — {reason}", "high"),
    "support.break_glass_ended": ("closed the support session after {duration_minutes} minutes", "high"),
    "supplier.exiting": ("notified {client} that access is ending", "high"),
    "supplier.access_frozen": ("froze a grant during suspension", "high"),
}


def describe(event: AuditEvent) -> tuple[str, str]:
    """Returns (sentence, significance). Falls back to a readable form of the
    action code rather than hiding an event we have no template for - a feed
    that silently drops the unfamiliar is worse than an ugly one."""
    template, significance = TEMPLATES.get(event.action,
                                           (event.action.replace(".", " ").replace("_", " "),
                                            "normal"))
    payload = {}
    if event.payload:
        try:
            payload = json.loads(event.payload)
        except (ValueError, TypeError):
            payload = {}
    try:
        sentence = template.format(**{k: _short(v) for k, v in payload.items()})
    except (KeyError, IndexError):
        sentence = template.split(" —")[0].split(" (")[0]
        for placeholder in ("{", "}"):
            sentence = sentence.replace(placeholder, "")
        sentence = " ".join(w for w in sentence.split() if not w.startswith("{"))
    return sentence, significance


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def actor_class(event: AuditEvent) -> str:
    return ACTOR_CLASS.get(event.actor_kind, "system")


def feed(db: Session, tenant_id: str, *, actor: str = "", significance: str = "",
         days: int = 90, search: str = "", limit: int = 200) -> list[dict]:
    query = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if days:
        query = query.where(AuditEvent.occurred_at >= now() - timedelta(days=days))
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(AuditEvent.action.ilike(like),
                                AuditEvent.actor_label.ilike(like),
                                AuditEvent.payload.ilike(like)))
    events = db.scalars(query.order_by(desc(AuditEvent.seq)).limit(limit * 2)).all()

    rows = []
    for event in events:
        klass = actor_class(event)
        if actor and klass != actor:
            continue
        sentence, weight = describe(event)
        if significance == "high" and weight != "high":
            continue
        rows.append({
            "event": event, "actor_class": klass,
            "actor_label": CLASS_LABEL[klass], "tone": CLASS_TONE[klass],
            "who": event.actor_label or CLASS_LABEL[klass],
            "sentence": sentence, "significance": weight,
        })
        if len(rows) >= limit:
            break
    return rows


def summary(db: Session, tenant_id: str, days: int = 30) -> dict:
    """Counts by actor class, so the supplier can see at a glance whether the
    activity on their account is their own or somebody else's."""
    since = now() - timedelta(days=days)
    counts = {"team": 0, "buyer": 0, "agent": 0, "platform": 0, "system": 0}
    events = db.scalars(select(AuditEvent).where(and_(
        AuditEvent.tenant_id == tenant_id, AuditEvent.occurred_at >= since))).all()
    for event in events:
        counts[actor_class(event)] += 1
    return counts


def buyer_receipts(db: Session, tenant_id: str, limit: int = 10) -> list[dict]:
    """Who read what, most recent first. This is the answer to 'has the client
    actually looked at it?', which is the question suppliers ask most."""
    events = db.scalars(
        select(AuditEvent).where(and_(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.actor_kind.in_(["client_contact", "agent", "api_client"]),
            AuditEvent.action.in_(["document.viewed", "document.downloaded",
                                   "share_link.viewed", "mcp.content_disclosed"])))
        .order_by(desc(AuditEvent.seq)).limit(limit)).all()
    rows = []
    for event in events:
        payload = json.loads(event.payload) if event.payload else {}
        rows.append({
            "at": event.occurred_at, "who": event.actor_label or "anonymous viewer",
            "action": {"document.viewed": "opened",
                       "document.downloaded": "downloaded",
                       "share_link.viewed": "opened the pack",
                       "mcp.content_disclosed": "read via an agent"}.get(event.action, event.action),
            "what": payload.get("document") or payload.get("client") or "shared documents",
            "grant_id": event.grant_id,
        })
    return rows
