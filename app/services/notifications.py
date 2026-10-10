"""Notification engine - GICC-05 F20.

Persist first, deliver second (AD13). Delivery transports fail; the record of
what a supplier was told, and when, must not.

Every notification names one action. A digest of everything outstanding is a
report, and people do not act on reports.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from ..config import NOTIFY_FROM, NOTIFY_TRANSPORT, REMINDER_LADDER
from ..models import (AccessGrant, ClientAccount, ClientContact, Document, DocumentType,
                      DocumentVersion, Notification, NotificationPreference, Tenant,
                      User, now)
from . import audit

# kind -> (default severity, roles that receive it, plain label)
KINDS: dict[str, tuple[str, set[str], str]] = {
    "document.uploaded": ("info", {"owner", "compliance"}, "Document uploaded"),
    "version.published": ("info", {"owner", "compliance"}, "Version published"),
    "access_request.received": ("warn", {"owner", "sales"}, "Access request received"),
    "access_request.actioned": ("info", {"owner", "sales"}, "Access request actioned"),
    "certificate.expiring": ("warn", {"owner", "compliance"}, "Certificate due to expire"),
    "certificate.expired": ("high", {"owner", "compliance"}, "Certificate expired"),
    "verification.adverse": ("high", {"owner", "compliance"}, "Adverse verification result"),
    "authenticity.signal": ("warn", {"owner", "compliance"}, "Authenticity signal raised"),
    "support.break_glass": ("high", {"owner"}, "Platform support session"),
    "trust.band_changed": ("info", {"owner"}, "Trust Index band changed"),
    "buyer.read": ("info", {"owner", "sales"}, "A buyer opened your documents"),
}


def recipients(db: Session, tenant_id: str, kind: str) -> list[User]:
    _, roles, _ = KINDS.get(kind, ("info", {"owner"}, kind))
    users = db.scalars(select(User).where(and_(
        User.tenant_id == tenant_id, User.is_active.is_(True)))).all()
    chosen = [u for u in users if u.role in roles] or [u for u in users if u.role == "owner"]
    allowed = []
    for user in chosen:
        preference = db.scalar(select(NotificationPreference).where(and_(
            NotificationPreference.user_id == user.id,
            NotificationPreference.kind == kind)))
        if preference and preference.channel == "off":
            continue
        allowed.append(user)
    return allowed


def emit(db: Session, *, tenant_id: str, kind: str, title: str, body: str,
         dedupe_key: str, action_label: str | None = None, action_url: str | None = None,
         severity: str | None = None, object_type: str | None = None,
         object_id: str | None = None, contact: ClientContact | None = None) -> int:
    """Creates one notification per recipient. Deduplicated per tenant, so the
    30-day reminder fires once rather than once per sweep."""
    default_severity = KINDS.get(kind, ("info", set(), kind))[0]
    severity = severity or default_severity
    created = 0

    targets: list[tuple[str | None, str | None, str | None]] = []
    if contact is not None:
        targets.append((None, contact.id, contact.email))
    else:
        targets = [(user.id, None, user.email) for user in recipients(db, tenant_id, kind)]

    for user_id, contact_id, email in targets:
        key = f"{dedupe_key}:{user_id or contact_id}"
        existing = db.scalar(select(Notification).where(and_(
            Notification.tenant_id == tenant_id, Notification.dedupe_key == key)))
        if existing:
            continue
        db.add(Notification(
            tenant_id=tenant_id, recipient_user_id=user_id, recipient_contact_id=contact_id,
            recipient_email=email, kind=kind, severity=severity, title=title, body=body,
            action_label=action_label, action_url=action_url, dedupe_key=key,
            object_type=object_type, object_id=object_id))
        created += 1
    if created:
        db.flush()
    return created


def unread_count(db: Session, tenant_id: str, user_id: str) -> int:
    return db.scalar(select(func.count(Notification.id)).where(and_(
        Notification.tenant_id == tenant_id,
        Notification.recipient_user_id == user_id,
        Notification.read_at.is_(None)))) or 0


def inbox(db: Session, tenant_id: str, user_id: str, *, unread_only: bool = False,
          kind: str = "", limit: int = 100) -> list[Notification]:
    query = select(Notification).where(and_(
        Notification.tenant_id == tenant_id,
        or_(Notification.recipient_user_id == user_id,
            Notification.recipient_user_id.is_(None))))
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if kind:
        query = query.where(Notification.kind == kind)
    return list(db.scalars(query.order_by(desc(Notification.created_at)).limit(limit)).all())


def mark_read(db: Session, tenant_id: str, user_id: str,
              notification_id: str | None = None) -> int:
    query = select(Notification).where(and_(
        Notification.tenant_id == tenant_id, Notification.read_at.is_(None)))
    if notification_id:
        query = query.where(Notification.id == notification_id)
    else:
        query = query.where(Notification.recipient_user_id == user_id)
    rows = db.scalars(query).all()
    for row in rows:
        row.read_at = now()
    db.flush()
    return len(rows)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def deliver_pending(db: Session, limit: int = 200) -> dict:
    """The inbox is always written. This is the outbound leg, which is allowed
    to fail without losing the notification."""
    pending = db.scalars(select(Notification).where(and_(
        Notification.delivery_state == "pending", Notification.attempts < 5))
        .order_by(Notification.created_at).limit(limit)).all()
    sent = failed = 0
    for notification in pending:
        notification.attempts += 1
        ok, channel = _transport(notification)
        if ok:
            notification.delivery_state = "sent"
            notification.delivery_channel = channel
            notification.sent_at = now()
            sent += 1
        else:
            notification.delivery_state = "pending" if notification.attempts < 5 else "failed"
            failed += 1
    db.flush()
    return {"sent": sent, "failed": failed, "considered": len(pending)}


def _transport(notification: Notification) -> tuple[bool, str]:
    if NOTIFY_TRANSPORT == "off":
        return True, "inbox"
    if NOTIFY_TRANSPORT == "smtp":
        return _send_smtp(notification)
    # Default: structured log line. A real deployment swaps in SMTP by setting
    # NOTIFY_TRANSPORT=smtp and SMTP_URL - nothing else changes.
    print(f"[notify] to={notification.recipient_email} kind={notification.kind} "
          f"severity={notification.severity} title={notification.title!r}", flush=True)
    return True, "log"


def _send_smtp(notification: Notification) -> tuple[bool, str]:
    from ..config import SMTP_URL
    if not SMTP_URL or not notification.recipient_email:
        return False, "smtp"
    try:
        import smtplib
        from email.message import EmailMessage
        from urllib.parse import urlparse

        parsed = urlparse(SMTP_URL)
        message = EmailMessage()
        message["From"] = NOTIFY_FROM
        message["To"] = notification.recipient_email
        message["Subject"] = notification.title
        body = notification.body
        if notification.action_url:
            body += f"\n\n{notification.action_label or 'Open'}: {notification.action_url}"
        message.set_content(body)

        server = (smtplib.SMTP_SSL if parsed.scheme == "smtps" else smtplib.SMTP)(
            parsed.hostname, parsed.port or (465 if parsed.scheme == "smtps" else 587),
            timeout=15)
        with server:
            if parsed.scheme == "smtp":
                server.starttls()
            if parsed.username:
                server.login(parsed.username, parsed.password or "")
            server.send_message(message)
        return True, "smtp"
    except Exception as exc:
        print(f"[notify] smtp failed: {type(exc).__name__}: {exc}", flush=True)
        return False, "smtp"


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------
def on_document_uploaded(db: Session, tenant: Tenant, document: Document,
                         version: DocumentVersion, actor) -> None:
    emit(db, tenant_id=tenant.id, kind="document.uploaded",
         title=f"{document.title} added",
         body=(f"{getattr(actor, 'full_name', 'Someone')} added {document.title}"
               + (f", valid to {version.expiry_date:%d %b %Y}" if version.expiry_date else "")
               + ". Check the extracted details are right before a buyer sees them."),
         dedupe_key=f"uploaded:{version.id}", action_label="Review",
         action_url=f"/documents/{document.id}",
         object_type="document", object_id=document.id)


def on_access_request(db: Session, tenant: Tenant, request_row, group_name: str,
                      client_name: str) -> None:
    emit(db, tenant_id=tenant.id, kind="access_request.received",
         title=f"{client_name} asked for {group_name}",
         body=(f"{request_row.contact_email or client_name} requested access to "
               f"{group_name}. Purpose: {request_row.purpose or 'not stated'}. "
               "Decide within two business days."),
         dedupe_key=f"request:{request_row.id}", action_label="Decide",
         action_url="/requests", object_type="access_request", object_id=request_row.id)


def on_access_request_actioned(db: Session, tenant: Tenant, request_row,
                               decision: str, client_name: str,
                               contact: ClientContact | None) -> None:
    emit(db, tenant_id=tenant.id, kind="access_request.actioned",
         title=f"Access request {decision}",
         body=f"The request from {client_name} was {decision}.",
         dedupe_key=f"request-actioned:{request_row.id}", action_label="View",
         action_url="/requests", object_type="access_request", object_id=request_row.id)
    if contact is not None:
        emit(db, tenant_id=tenant.id, kind="access_request.actioned",
             title=f"Your request was {decision}",
             body=(f"{tenant.name} {decision} your access request."
                   + (" You can now open the shared pack."
                      if decision == "approved" else
                      f" Reason: {request_row.decision_note or 'not stated'}.")),
             dedupe_key=f"request-actioned-contact:{request_row.id}",
             object_type="access_request", object_id=request_row.id, contact=contact)


def on_adverse_verification(db: Session, tenant: Tenant, document: Document,
                            version: DocumentVersion, result: str) -> None:
    emit(db, tenant_id=tenant.id, kind="verification.adverse",
         title=f"{document.title}: {result.replace('_', ' ')}",
         body=("A verification check against the issuing source returned "
               f"'{result.replace('_', ' ')}'. Every client holding a grant to this "
               "document can see the change."),
         dedupe_key=f"adverse:{version.id}:{result}", action_label="Open",
         action_url=f"/documents/{document.id}",
         object_type="document", object_id=document.id)


def on_authenticity_signals(db: Session, tenant: Tenant, document: Document,
                            signals: list) -> None:
    high = [s for s in signals if s.severity == "high"]
    if not high:
        return
    emit(db, tenant_id=tenant.id, kind="authenticity.signal",
         title=f"{document.title}: {len(high)} signal(s) to check",
         body=high[0].detail,
         dedupe_key=f"signal:{high[0].document_version_id}", action_label="Review",
         action_url=f"/documents/{document.id}",
         object_type="document", object_id=document.id)


def on_break_glass(db: Session, tenant: Tenant, admin_email: str, reason: str) -> None:
    emit(db, tenant_id=tenant.id, kind="support.break_glass",
         title="A platform operator opened a support session",
         body=(f"{admin_email} opened a time-boxed session on your workspace. "
               f"Stated reason: {reason}. Everything they do appears in your activity log."),
         dedupe_key=f"breakglass:{now():%Y%m%d%H%M}", action_label="See activity",
         action_url="/activity?actor=platform")


def on_trust_band_change(db: Session, tenant: Tenant, previous: str, band: str,
                         composite: float) -> None:
    emit(db, tenant_id=tenant.id, kind="trust.band_changed",
         title=f"Trust Index moved from {previous} to {band}",
         body=f"Your composite score is now {composite:.0f}. See which domain moved.",
         dedupe_key=f"trustband:{tenant.id}:{previous}->{band}:{date.today()}",
         action_label="Open Trust Index", action_url="/trust")


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------
def sweep(db: Session, tenant: Tenant) -> dict:
    """Expiry ladder plus the daily expired reminder. Idempotent: dedupe keys
    carry the ladder band or the date, so a restart mid-sweep is harmless."""
    today = date.today()
    created = 0

    rows = db.execute(
        select(Document, DocumentType)
        .join(DocumentType, Document.document_type_id == DocumentType.id)
        .where(and_(Document.tenant_id == tenant.id,
                    Document.deleted_at.is_(None)))).all()

    for document, dtype in rows:
        if not document.active_version_id:
            continue
        version = db.get(DocumentVersion, document.active_version_id)
        if version is None or version.expiry_date is None:
            continue
        days = (version.expiry_date - today).days

        if days < 0:
            grantees = _grantee_count(db, tenant.id, document.id)
            created += emit(
                db, tenant_id=tenant.id, kind="certificate.expired",
                title=f"{document.title} has expired",
                body=(f"It expired {abs(days)} day(s) ago"
                      + (f" and {grantees} client(s) can see that it has."
                         if grantees else ". No client is relying on it yet.")
                      + " Upload the replacement to clear it."),
                dedupe_key=f"expired:{document.id}:{today.isoformat()}",
                action_label="Upload replacement",
                action_url=f"/documents/{document.id}/versions/new",
                object_type="document", object_id=document.id)
            continue

        band = next((b for b in REMINDER_LADDER if b > 0 and days <= b), None)
        if band is not None:
            grantees = _grantee_count(db, tenant.id, document.id)
            created += emit(
                db, tenant_id=tenant.id, kind="certificate.expiring",
                title=f"{document.title} expires in {days} day(s)",
                body=(f"Expires {version.expiry_date:%d %b %Y}"
                      + (f" and {grantees} client(s) rely on it." if grantees
                         else ".")
                      + " Renewals through the issuing body usually take longer than the "
                        "window left."),
                dedupe_key=f"expiring:{document.id}:{band}",
                action_label="Add the replacement",
                action_url=f"/documents/{document.id}/versions/new",
                severity="high" if days <= 7 else "warn",
                object_type="document", object_id=document.id)

    delivery = deliver_pending(db)
    if created:
        audit.record(db, tenant_id=tenant.id, action="notification.swept",
                     actor_kind="system", actor_label="notification-sweep",
                     payload={"created": created, **delivery})
    db.commit()
    return {"created": created, **delivery}


def _grantee_count(db: Session, tenant_id: str, document_id: str) -> int:
    from . import access
    count = 0
    for grant in db.scalars(select(AccessGrant).where(and_(
            AccessGrant.tenant_id == tenant_id, AccessGrant.status == "active"))).all():
        if any(d.id == document_id for d in access.resolve_documents(db, grant)):
            count += 1
    return count


def sweep_all(db: Session) -> dict:
    totals = {"tenants": 0, "created": 0, "sent": 0}
    for tenant in db.scalars(select(Tenant).where(
            Tenant.lifecycle_state.in_(["active", "closing"]))).all():
        result = sweep(db, tenant)
        totals["tenants"] += 1
        totals["created"] += result["created"]
        totals["sent"] += result.get("sent", 0)
    return totals
