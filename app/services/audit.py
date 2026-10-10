"""Append-only audit log with a per-tenant hash chain (GICC-02 §4.15).

Each row hashes the previous row's hash together with its own canonical
content, so a deleted or edited row breaks the chain and is detectable.
"""
from __future__ import annotations

import hashlib
import json
import threading

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditEvent, now


# One process, one writer at a time. The hash chain requires a total order per
# tenant, and two threads reading the same highest seq breaks it.
_SEQUENCE_LOCK = threading.Lock()


def _canonical(event: AuditEvent) -> str:
    """The exact bytes that are hashed. Kept in one place so writing and
    verifying can never drift apart."""
    return json.dumps({
        "tenant": event.tenant_id, "seq": event.seq, "action": event.action,
        "actor_kind": event.actor_kind, "actor_id": event.actor_id,
        "object": f"{event.object_type}:{event.object_id}",
        "at": event.occurred_at.isoformat() if event.occurred_at else "",
        "payload_hash": event.payload_hash,
    }, sort_keys=True, default=str)


def record(db: Session, *, tenant_id: str, action: str, actor_kind: str = "user",
           actor_id: str | None = None, actor_label: str | None = None,
           object_type: str | None = None, object_id: str | None = None,
           grant_id: str | None = None, ip: str | None = None,
           payload: dict | None = None, at=None) -> AuditEvent:
    with _SEQUENCE_LOCK:
        prev = db.scalar(
            select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
            .order_by(desc(AuditEvent.seq)).limit(1))
        prev_hash = prev.row_hash if prev else ""
        seq = (prev.seq + 1) if prev else 1

    event = AuditEvent(
        tenant_id=tenant_id, seq=seq, occurred_at=at or now(), action=action, actor_kind=actor_kind, actor_id=actor_id,
        actor_label=actor_label, object_type=object_type, object_id=object_id,
        grant_id=grant_id, ip=ip,
        payload=json.dumps(payload, default=str) if payload else None,
        prev_hash=prev_hash)
    event.payload_hash = hashlib.sha256((event.payload or "").encode()).hexdigest()

    canonical = _canonical(event)
    event.row_hash = hashlib.sha256((prev_hash + canonical).encode()).hexdigest()

    db.add(event)
    # Flush immediately: the next record() in the same transaction reads the
    # highest seq with a SELECT, and an unflushed row would be invisible to it.
    #
    # Two writers can read the same highest seq before either flushes - the
    # background sweep and a signed-in user, for instance. The unique index
    # catches it; we take the next number and rebuild the hash rather than
    # losing the event.
    for attempt in range(5):
        try:
            db.flush()
            return event
        except IntegrityError:
            db.rollback() if attempt == 4 else None
            if attempt == 4:
                raise
            db.expunge(event)
            highest = db.scalar(
                select(func.max(AuditEvent.seq)).where(
                    AuditEvent.tenant_id == tenant_id)) or 0
            previous_row = db.scalar(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
                .order_by(desc(AuditEvent.seq)).limit(1))
            event.seq = highest + 1
            event.prev_hash = previous_row.row_hash if previous_row else ""
            event.row_hash = hashlib.sha256(
                (event.prev_hash + _canonical(event)).encode()).hexdigest()
            db.add(event)
    return event


def verify_chain(db: Session, tenant_id: str) -> tuple[bool, int, str | None]:
    """Returns (intact, rows_checked, first_broken_id)."""
    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq)).all()
    prev_hash = ""
    for row in rows:
        expected = hashlib.sha256((prev_hash + _canonical(row)).encode()).hexdigest()
        if expected != row.row_hash:
            return False, len(rows), row.id
        prev_hash = row.row_hash
    return True, len(rows), None
