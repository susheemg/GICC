"""ORM models. This is the GICC-02 design reduced to what SQLite can enforce
honestly: partial unique indexes, check constraints and application-level
immutability guards on document_version."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def uid() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# --------------------------------------------------------------------------
# Tenancy and identity
# --------------------------------------------------------------------------
class Organisation(Base, TimestampMixin):
    __tablename__ = "organisation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    legal_name: Mapped[str] = mapped_column(String(255))
    trading_name: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(2), default="GB")
    registry_number: Mapped[str | None] = mapped_column(String(64))
    lei: Mapped[str | None] = mapped_column(String(20))
    vat_number: Mapped[str | None] = mapped_column(String(32))
    duns: Mapped[str | None] = mapped_column(String(16))
    entity_type: Mapped[str] = mapped_column(String(32), default="private_limited")
    incorporated_on: Mapped[date | None] = mapped_column(Date)
    verification_state: Mapped[str] = mapped_column(String(32), default="unverified")
    website: Mapped[str | None] = mapped_column(String(255))
    employees: Mapped[int | None] = mapped_column(Integer)
    annual_turnover: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    sector_code: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    registered_address: Mapped[str | None] = mapped_column(Text)
    # GICC-07 F26/F27: an organisation may exist without a tenant. Bulk import
    # creates unclaimed profiles; a claim binds one to a workspace.
    registry_authority: Mapped[str | None] = mapped_column(String(96))
    claim_status: Mapped[str] = mapped_column(String(16), default="claimed")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_by_tenant_id: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(24), default="registration")
    import_batch_id: Mapped[str | None] = mapped_column(String(32), index=True)


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenant"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    # vendor | buyer | both. A company that sells and buys is one identity with
    # two capabilities, not two accounts - duplicating the organisation would
    # break the one thing this platform exists to establish.
    kind: Mapped[str] = mapped_column(String(16), default="vendor")
    lifecycle_state: Mapped[str] = mapped_column(String(24), default="active")
    data_region: Mapped[str] = mapped_column(String(4), default="uk1")
    primary_domain: Mapped[str | None] = mapped_column(String(255))
    domain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    readiness_score: Mapped[float] = mapped_column(Float, default=0.0)
    health_score: Mapped[float] = mapped_column(Float, default=100.0)
    closing_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    close_reason: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime)
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    suspended_by: Mapped[str | None] = mapped_column(String(32))
    admin_note: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, default=now)

    organisation: Mapped[Organisation] = relationship()


class User(Base, TimestampMixin):
    __tablename__ = "user_account"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="owner")
    sensitive_access: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    tenant: Mapped[Tenant] = relationship()


class PlatformAdmin(Base, TimestampMixin):
    """Platform operators live in their own table, not as a flag on user_account.
    A compromised supplier login must never be one column away from cross-tenant
    read (GICC-03 threat model T7)."""
    __tablename__ = "platform_admin"
    __table_args__ = (UniqueConstraint("email", name="uq_admin_email"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(64), default="Platform administrator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class BreakGlassSession(Base):
    """Time-boxed admin access to a tenant workspace. Always visible to the
    tenant in their own activity log - support access a customer cannot see is
    indistinguishable from a breach."""
    __tablename__ = "break_glass_session"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    admin_id: Mapped[str] = mapped_column(ForeignKey("platform_admin.id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    acting_user_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    ticket_reference: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    actions_taken: Mapped[int] = mapped_column(Integer, default=0)


class EntityIdentifier(Base, TimestampMixin):
    __tablename__ = "entity_identifier"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str] = mapped_column(String(24))
    value: Mapped[str] = mapped_column(String(128))
    verification_state: Mapped[str] = mapped_column(String(24), default="unverified")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str | None] = mapped_column(String(64))


# --------------------------------------------------------------------------
# Catalogue (platform reference data)
# --------------------------------------------------------------------------
class DocumentType(Base):
    __tablename__ = "document_type"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    domain_code: Mapped[str] = mapped_column(String(8))
    domain_name: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    typical_issuer: Mapped[str] = mapped_column(String(120))
    validity_label: Mapped[str] = mapped_column(String(48))
    validity_months: Mapped[int | None] = mapped_column(Integer)
    verification_route: Mapped[str] = mapped_column(String(32))
    sensitivity: Mapped[str] = mapped_column(String(2), default="R")
    criticality: Mapped[int] = mapped_column(Integer, default=3)
    verification_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # GICC-07 K2: instance-bearing types resolve to a set, not a row.
    multi_instance: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_asset: Mapped[bool] = mapped_column(Boolean, default=False)
    # K1: tenant-scoped types are usable by their tenant but excluded from
    # cross-tenant matching until an operator promotes them.
    scope: Mapped[str] = mapped_column(String(16), default="global")
    owner_tenant_id: Mapped[str | None] = mapped_column(String(32), index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(32))


# --------------------------------------------------------------------------
# Documents and versions
# --------------------------------------------------------------------------
class Document(Base, TimestampMixin):
    __tablename__ = "document"
    __table_args__ = (
        Index("ix_document_tenant_status", "tenant_id", "derived_status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    document_type_id: Mapped[str] = mapped_column(ForeignKey("document_type.id"))
    title: Mapped[str] = mapped_column(String(255))
    jurisdiction: Mapped[str | None] = mapped_column(String(2))
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("user_account.id"))
    derived_status: Mapped[str] = mapped_column(String(24), default="missing")
    active_version_id: Mapped[str | None] = mapped_column(String(32))
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    hold_placed_by: Mapped[str | None] = mapped_column(String(24))  # tenant | platform
    asset_id: Mapped[str | None] = mapped_column(String(32), index=True)
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    document_type: Mapped[DocumentType] = relationship(lazy="joined")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="desc(DocumentVersion.version_no)")


class DocumentVersion(Base, TimestampMixin):
    """Append-only. Only status, assurance_level and the *_at stamps may change
    after publication - enforced in services.documents."""
    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_version_no"),
        # Invariant I2 (GICC-01 §4): exactly one active version per document.
        Index("uq_version_active", "document_id", unique=True,
              sqlite_where=text("status = 'active'")),
        CheckConstraint("version_no > 0", name="ck_version_no"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    issue_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date] = mapped_column(Date, default=date.today)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    certificate_number: Mapped[str | None] = mapped_column(String(120))
    issuer_name: Mapped[str | None] = mapped_column(String(200))
    holder_name: Mapped[str | None] = mapped_column(String(200))
    scope_statement: Mapped[str | None] = mapped_column(Text)
    cover_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    verification_url: Mapped[str | None] = mapped_column(String(500))
    verification_reference: Mapped[str | None] = mapped_column(String(120))
    assurance_level: Mapped[str] = mapped_column(String(2), default="L0")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    supersedes_version_id: Mapped[str | None] = mapped_column(String(32))
    change_reason: Mapped[str | None] = mapped_column(Text)
    name_match: Mapped[str | None] = mapped_column(String(16))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)
    restored_from_export: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="versions")


class VerificationCheck(Base):
    __tablename__ = "verification_check"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_version.id"))
    method: Mapped[str] = mapped_column(String(32))
    source_code: Mapped[str | None] = mapped_column(String(48))
    result: Mapped[str] = mapped_column(String(24))
    resulting_assurance: Mapped[str] = mapped_column(String(2))
    matched_fields: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    next_check_due: Mapped[datetime | None] = mapped_column(DateTime)
    checked_by: Mapped[str | None] = mapped_column(String(120))


# --------------------------------------------------------------------------
# Groups and disclosure
# --------------------------------------------------------------------------
class CertificateGroup(Base, TimestampMixin):
    __tablename__ = "certificate_group"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_group_code"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    code: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    sensitivity_ceiling: Mapped[str] = mapped_column(String(2), default="R")
    nda_required: Mapped[bool] = mapped_column(Boolean, default=False)
    watermark_required: Mapped[bool] = mapped_column(Boolean, default=True)
    download_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    max_grant_days: Mapped[int] = mapped_column(Integer, default=90)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    display_order: Mapped[int] = mapped_column(Integer, default=100)


class GroupItem(Base):
    __tablename__ = "group_item"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    group_id: Mapped[str] = mapped_column(ForeignKey("certificate_group.id"))
    item_kind: Mapped[str] = mapped_column(String(16))  # document | type_rule
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id"))
    type_prefix: Mapped[str | None] = mapped_column(String(16))  # e.g. "SEC-" or "SEC-001"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ClientAccount(Base, TimestampMixin):
    __tablename__ = "client_account"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(200))
    relationship_type: Mapped[str] = mapped_column(String(24), default="customer")
    country: Mapped[str | None] = mapped_column(String(2))
    internal_reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    offboarded_at: Mapped[datetime | None] = mapped_column(DateTime)
    offboard_reason: Mapped[str | None] = mapped_column(Text)
    # Set when this client record represents a real buyer workspace on the
    # platform rather than a name the supplier typed in. It is what lets an
    # approved grant become visible inside the buyer's own account.
    buyer_tenant_id: Mapped[str | None] = mapped_column(String(32), index=True)


class ClientContact(Base, TimestampMixin):
    __tablename__ = "client_contact"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    client_id: Mapped[str] = mapped_column(ForeignKey("client_account.id"))
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200))
    job_title: Mapped[str | None] = mapped_column(String(120))
    nda_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    nda_template_hash: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class AccessGrant(Base, TimestampMixin):
    __tablename__ = "access_grant"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    client_id: Mapped[str] = mapped_column(ForeignKey("client_account.id"))
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("client_contact.id"))
    api_client_id: Mapped[str | None] = mapped_column(String(32))
    scope_kind: Mapped[str] = mapped_column(String(24))  # group | document | whole_profile
    scope_id: Mapped[str | None] = mapped_column(String(32))
    permission_level: Mapped[str] = mapped_column(String(24), default="view")
    starts_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    nda_required: Mapped[bool] = mapped_column(Boolean, default=False)
    watermark_required: Mapped[bool] = mapped_column(Boolean, default=True)
    download_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    purpose: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class ShareLink(Base, TimestampMixin):
    __tablename__ = "share_link"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    grant_id: Mapped[str] = mapped_column(ForeignKey("access_grant.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(String(160))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_views: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ShareLinkView(Base):
    __tablename__ = "share_link_view"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    share_link_id: Mapped[str] = mapped_column(ForeignKey("share_link.id"))
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    viewer_email: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    document_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(24), default="view")


class AccessRequest(Base, TimestampMixin):
    __tablename__ = "access_request"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    client_id: Mapped[str] = mapped_column(ForeignKey("client_account.id"))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    scope_kind: Mapped[str] = mapped_column(String(24), default="group")
    scope_id: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="open")
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    grant_id: Mapped[str | None] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(24), default="buyer_portal")
    requested_by_user_id: Mapped[str | None] = mapped_column(String(32))
    requested_by_tenant_id: Mapped[str | None] = mapped_column(String(32), index=True)


# --------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------
class RequirementProfile(Base, TimestampMixin):
    __tablename__ = "requirement_profile"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(160))
    audience: Mapped[str] = mapped_column(String(24), default="platform_template")
    sector: Mapped[str | None] = mapped_column(String(64))
    jurisdiction: Mapped[str | None] = mapped_column(String(2))
    description: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list["RequirementItem"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin")


class RequirementItem(Base):
    __tablename__ = "requirement_item"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("requirement_profile.id"))
    type_code: Mapped[str] = mapped_column(String(16))
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    min_assurance: Mapped[str] = mapped_column(String(2), default="L1")
    max_age_days: Mapped[int | None] = mapped_column(Integer)
    min_cover_amount: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[int] = mapped_column(Integer, default=3)


class Assessment(Base):
    __tablename__ = "assessment"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("requirement_profile.id"))
    assessed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    score: Mapped[float] = mapped_column(Float, default=0)
    met_count: Mapped[int] = mapped_column(Integer, default=0)
    gap_count: Mapped[int] = mapped_column(Integer, default=0)
    engine_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    results: Mapped[list["AssessmentResult"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin")


class AssessmentResult(Base):
    __tablename__ = "assessment_result"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("assessment.id"))
    type_code: Mapped[str] = mapped_column(String(16))
    type_name: Mapped[str] = mapped_column(String(160))
    outcome: Mapped[str] = mapped_column(String(24))
    cited_version_id: Mapped[str | None] = mapped_column(String(32))
    explanation: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Work, audit, machine access
# --------------------------------------------------------------------------
class Task(Base, TimestampMixin):
    __tablename__ = "task"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str] = mapped_column(String(32), default="renewal")
    title: Mapped[str] = mapped_column(String(255))
    document_id: Mapped[str | None] = mapped_column(String(32))
    due_at: Mapped[date | None] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    status: Mapped[str] = mapped_column(String(16), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),
                      UniqueConstraint("tenant_id", "seq", name="uq_audit_seq"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    # Per-tenant monotonic sequence. The hash chain is ordered by this, not by
    # timestamp: several events can share a second and ordering must be exact.
    seq: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    actor_kind: Mapped[str] = mapped_column(String(24), default="user")
    actor_id: Mapped[str | None] = mapped_column(String(32))
    actor_label: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(48))
    object_id: Mapped[str | None] = mapped_column(String(32))
    grant_id: Mapped[str | None] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str | None] = mapped_column(Text)
    # The chain hashes payload_hash, not payload, and actor_id, not actor_label.
    # That lets a purge scrub personal data from the log while leaving the
    # chain verifiable - otherwise erasure and evidential integrity conflict.
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), default="")


class AiInference(Base):
    """Provenance for every model or heuristic output - GICC-05 AD9. Without
    this row an AI proposal is indistinguishable from a verified fact."""
    __tablename__ = "ai_inference"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="heuristic")  # model | heuristic
    model: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    object_type: Mapped[str | None] = mapped_column(String(48))
    object_id: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ImportBatch(Base, TimestampMixin):
    """Bulk profile import - GICC-07 F26. Staged, validated, then committed;
    reversible until any profile in it is claimed."""
    __tablename__ = "import_batch"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_by: Mapped[str | None] = mapped_column(String(120))
    filename: Mapped[str | None] = mapped_column(String(255))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="parsing")
    mapping_json: Mapped[str | None] = mapped_column(Text)
    counts_json: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reversal_blocked_reason: Mapped[str | None] = mapped_column(Text)


class ImportRow(Base):
    __tablename__ = "import_row"
    __table_args__ = (Index("ix_import_row_batch", "batch_id", "classification"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    raw_json: Mapped[str | None] = mapped_column(Text)
    normalised_json: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(16), default="create")
    reason: Mapped[str | None] = mapped_column(Text)
    matched_organisation_id: Mapped[str | None] = mapped_column(String(32))
    created_organisation_id: Mapped[str | None] = mapped_column(String(32))


class ProfileClaim(Base, TimestampMixin):
    """Claiming an unclaimed profile - GICC-07 F27. This is identity proofing:
    a successful claim lets somebody publish credentials as that company."""
    __tablename__ = "profile_claim"
    __table_args__ = (Index("ix_claim_org_status", "organisation_id", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    claimant_email: Mapped[str] = mapped_column(String(255))
    claimant_name: Mapped[str | None] = mapped_column(String(255))
    claimant_tenant_id: Mapped[str | None] = mapped_column(String(32))
    evidence_file_path: Mapped[str | None] = mapped_column(String(500))
    evidence_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_filename: Mapped[str | None] = mapped_column(String(255))
    extracted_json: Mapped[str | None] = mapped_column(Text)
    signals_json: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    band: Mapped[str] = mapped_column(String(16), default="review")
    adverse_flags: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="open")
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reversal_reason: Mapped[str | None] = mapped_column(Text)


class DocumentTypeProposal(Base, TimestampMixin):
    """K1: the global catalogue is extended only by a named human. An unmatched
    document is filed under a tenant-scoped type until an operator promotes it."""
    __tablename__ = "document_type_proposal"
    __table_args__ = (Index("ix_proposal_status", "status", "occurrences"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    proposed_name: Mapped[str] = mapped_column(String(160))
    proposed_domain: Mapped[str | None] = mapped_column(String(8))
    nearest_type_code: Mapped[str | None] = mapped_column(String(16))
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="heuristic")
    sample_document_id: Mapped[str | None] = mapped_column(String(32))
    created_type_code: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    resulting_type_code: Mapped[str | None] = mapped_column(String(16))
    merged_into_code: Mapped[str | None] = mapped_column(String(16))
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(Text)


class Asset(Base, TimestampMixin):
    """The asset register - GICC-07 F29. A calibration certificate is meaningless
    without the instrument it belongs to, and auditors check the equipment list
    against the certificates."""
    __tablename__ = "asset"
    __table_args__ = (UniqueConstraint("tenant_id", "asset_tag", name="uq_asset_tag"),
                      Index("ix_asset_due", "tenant_id", "next_due_on"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    asset_tag: Mapped[str] = mapped_column(String(64))
    serial_number: Mapped[str | None] = mapped_column(String(96), index=True)
    name: Mapped[str] = mapped_column(String(160))
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="measuring_instrument")
    location: Mapped[str | None] = mapped_column(String(160))
    custodian_user_id: Mapped[str | None] = mapped_column(String(32))
    criticality: Mapped[int] = mapped_column(Integer, default=3)
    calibration_interval_months: Mapped[int | None] = mapped_column(Integer)
    interval_basis: Mapped[str | None] = mapped_column(Text)
    traceability_required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="in_service")
    last_calibrated_on: Mapped[date | None] = mapped_column(Date)
    next_due_on: Mapped[date | None] = mapped_column(Date)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    retirement_reason: Mapped[str | None] = mapped_column(Text)


class CalibrationRecord(Base):
    """Extends a document_version rather than replacing it, so versioning,
    assurance and disclosure continue to work unchanged."""
    __tablename__ = "calibration_record"
    __table_args__ = (UniqueConstraint("document_version_id",
                                       name="uq_calibration_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_version.id"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    calibration_date: Mapped[date | None] = mapped_column(Date)
    next_due_on: Mapped[date | None] = mapped_column(Date)
    interval_months: Mapped[int | None] = mapped_column(Integer)
    laboratory_name: Mapped[str | None] = mapped_column(String(200))
    accreditation_body: Mapped[str | None] = mapped_column(String(96))
    accreditation_number: Mapped[str | None] = mapped_column(String(64))
    accreditation_scope_ref: Mapped[str | None] = mapped_column(String(255))
    as_found: Mapped[str] = mapped_column(String(24), default="not_applicable")
    as_left: Mapped[str] = mapped_column(String(24), default="in_tolerance")
    tolerance_verdict: Mapped[str] = mapped_column(String(24), default="pass")
    measurement_uncertainty: Mapped[str | None] = mapped_column(String(96))
    uncertainty_unit: Mapped[str | None] = mapped_column(String(32))
    coverage_factor: Mapped[str | None] = mapped_column(String(16))
    traceability_statement: Mapped[str | None] = mapped_column(Text)
    traceability_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    standards_used_json: Mapped[str | None] = mapped_column(Text)
    ambient_conditions_json: Mapped[str | None] = mapped_column(Text)
    impact_assessment_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ImpactAssessment(Base, TimestampMixin):
    """An out-of-tolerance result means measurements since the previous
    calibration may be invalid. That reaches into product already shipped, so it
    is an assessment, not a renewal task."""
    __tablename__ = "impact_assessment"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    calibration_record_id: Mapped[str | None] = mapped_column(String(32))
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    affected_scope: Mapped[str | None] = mapped_column(Text)
    products_affected: Mapped[str | None] = mapped_column(Text)
    customers_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    disposition: Mapped[str] = mapped_column(String(24), default="under_assessment")
    assessed_by: Mapped[str | None] = mapped_column(String(120))
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    conclusion: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """Long-running work with per-item state, so a restart resumes rather than
    restarts - GICC-07 §2.2."""
    __tablename__ = "job"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)


class JobItem(Base):
    __tablename__ = "job_item"
    __table_args__ = (Index("ix_job_item", "job_id", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class SupplierLink(Base, TimestampMixin):
    """A buyer's own list of suppliers.

    The link points at an *organisation*, not a tenant, because a buyer must be
    able to track a supplier that has not joined yet. That is the acquisition
    loop: the buyer names the company, the company gets invited, and the profile
    it eventually claims is the one the buyer was already watching.
    """
    __tablename__ = "supplier_link"
    __table_args__ = (UniqueConstraint("buyer_tenant_id", "organisation_id",
                                       name="uq_supplier_link"),
                      Index("ix_supplier_link_state", "buyer_tenant_id", "state"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    buyer_tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"))
    # watching -> invited -> requested -> connected, or removed at any point.
    state: Mapped[str] = mapped_column(String(16), default="watching")
    internal_reference: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(96))
    criticality: Mapped[int] = mapped_column(Integer, default=3)
    notes: Mapped[str | None] = mapped_column(Text)
    added_by_user_id: Mapped[str | None] = mapped_column(String(32))
    last_request_id: Mapped[str | None] = mapped_column(String(32))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class DirectorySearch(Base):
    """Every buyer search is recorded.

    The directory's value to a buyer and its value to somebody harvesting a
    contact list look identical one query at a time. Recording them makes bulk
    extraction visible after the fact, and gives a supplier an honest answer to
    'who has been looking me up'.
    """
    __tablename__ = "directory_search"
    __table_args__ = (Index("ix_directory_search_tenant", "buyer_tenant_id",
                            "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    buyer_tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(32))
    query: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ProfileView(Base):
    """A buyer opening a supplier's directory entry. Shown to the supplier."""
    __tablename__ = "profile_view"
    __table_args__ = (Index("ix_profile_view_org", "organisation_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32))
    buyer_tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CertSearchLookup(Base, TimestampMixin):
    """One attempt to confirm a certificate against IAF CertSearch.

    Kept separate from VerificationCheck because a lookup and a verdict are
    different things: a lookup may return three candidate certificates, or none,
    or one that contradicts what the supplier uploaded. Only once a candidate is
    reconciled does it become a check with an assurance consequence.
    """
    __tablename__ = "certsearch_lookup"
    __table_args__ = (Index("ix_certsearch_version", "document_version_id",
                            "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_version.id"))
    transport: Mapped[str] = mapped_column(String(24), default="assisted")
    query_name: Mapped[str | None] = mapped_column(String(255))
    query_country: Mapped[str | None] = mapped_column(String(2))
    query_standard: Mapped[str | None] = mapped_column(String(64))
    search_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="open")
    raw_payload: Mapped[str | None] = mapped_column(Text)
    candidates_json: Mapped[str | None] = mapped_column(Text)
    selected_json: Mapped[str | None] = mapped_column(Text)
    comparison_json: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(24))
    assurance_ceiling: Mapped[str] = mapped_column(String(2), default="L2")
    resulting_assurance: Mapped[str | None] = mapped_column(String(2))
    verification_check_id: Mapped[str | None] = mapped_column(String(32))
    attested_by: Mapped[str | None] = mapped_column(String(120))
    attested_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class CloudConnection(Base, TimestampMixin):
    """A supplier's Google Drive or OneDrive folder, connected read-only.

    The platform never writes to the supplier's drive. It reads one folder the
    supplier chose, mirrors it into the register, and keeps a revision history
    of every file it has seen. Tokens are encrypted at rest with a key derived
    from SECRET_KEY.
    """
    __tablename__ = "cloud_connection"
    __table_args__ = (Index("ix_cloud_due", "status", "next_sync_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))     # google | microsoft | demo
    account_email: Mapped[str | None] = mapped_column(String(255))
    folder_id: Mapped[str | None] = mapped_column(String(255))
    folder_name: Mapped[str | None] = mapped_column(String(255))
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    schedule: Mapped[str] = mapped_column(String(16), default="daily")
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_result: Mapped[str | None] = mapped_column(String(24))
    auto_publish_renewals: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[str | None] = mapped_column(String(120))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncRun(Base):
    """One pass over a connected folder - scheduled or manual."""
    __tablename__ = "sync_run"
    __table_args__ = (Index("ix_sync_run_conn", "connection_id", "started_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    connection_id: Mapped[str] = mapped_column(ForeignKey("cloud_connection.id"))
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    seen: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    published: Mapped[int] = mapped_column(Integer, default=0)
    authority_links: Mapped[int] = mapped_column(Integer, default=0)
    flagged: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class SyncedFile(Base, TimestampMixin):
    """A file in the connected folder, tracked by the provider's own id so a
    rename or a move inside the folder is the same file, not a new one."""
    __tablename__ = "synced_file"
    __table_args__ = (UniqueConstraint("connection_id", "provider_file_id",
                                       name="uq_synced_file"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    connection_id: Mapped[str] = mapped_column(ForeignKey("cloud_connection.id"))
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    provider_file_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    path: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(160))
    provider_revision: Mapped[str | None] = mapped_column(String(255))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(String(32), index=True)
    current_version_id: Mapped[str | None] = mapped_column(String(32))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="active")
    last_error: Mapped[str | None] = mapped_column(Text)


class SyncedFileRevision(Base):
    """Every distinct revision of a source file the platform has seen.

    This is the source-side history. The register keeps its own version history
    of the credential; this table is what lets anyone answer 'which file in
    which folder produced v3, and when did it change?'"""
    __tablename__ = "synced_file_revision"
    __table_args__ = (Index("ix_revision_file", "synced_file_id", "seen_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    synced_file_id: Mapped[str] = mapped_column(ForeignKey("synced_file.id"))
    sync_run_id: Mapped[str | None] = mapped_column(String(32))
    change: Mapped[str] = mapped_column(String(16))      # added | updated | removed
    provider_revision: Mapped[str | None] = mapped_column(String(255))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    file_name: Mapped[str | None] = mapped_column(String(500))
    document_version_id: Mapped[str | None] = mapped_column(String(32))
    type_code: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    authority_findings_json: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OnboardingProgress(Base, TimestampMixin):
    """One guided journey per workspace - GICC-06 F24.

    Milestone stamps live here rather than being derived at query time, so
    cohort analysis stays cheap and the historical record survives later data
    changes.
    """
    __tablename__ = "onboarding_progress"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_onboarding_tenant"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    sector: Mapped[str | None] = mapped_column(String(64))
    buyer_types: Mapped[str | None] = mapped_column(String(255))
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    steps_json: Mapped[str | None] = mapped_column(Text)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Value milestones - GICC-00 Part 6.1
    m1_at: Mapped[datetime | None] = mapped_column(DateTime)   # first L3/L4 credential
    m2_at: Mapped[datetime | None] = mapped_column(DateTime)   # first live grant
    m3_at: Mapped[datetime | None] = mapped_column(DateTime)   # first buyer read


class AiProviderConfig(Base, TimestampMixin):
    """LLM provider credentials, managed from the platform console.

    Platform-level, not per-tenant: the model provider is a subprocessor for
    every tenant whose documents it reads, so the decision to use one belongs
    with whoever signed the DPA, not with each supplier.
    """
    __tablename__ = "ai_provider_config"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(80))
    base_url: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(120))
    # Fernet ciphertext. Never returned to the browser; the UI shows the mask
    # and the fingerprint so an operator can identify the key without reading it.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_mask: Mapped[str | None] = mapped_column(String(40))
    api_key_fingerprint: Mapped[str | None] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1500)
    notes: Mapped[str | None] = mapped_column(Text)
    data_boundary: Mapped[str] = mapped_column(String(24), default="third_party")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_detail: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    calls_made: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str | None] = mapped_column(String(120))


class AuthenticitySignal(Base):
    """An observation warranting a human look - GICC-05 F19. It never changes
    an assurance level in either direction."""
    __tablename__ = "authenticity_signal"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_version.id"))
    code: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(8), default="low")  # low | medium | high
    detail: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="rule")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|dismissed|confirmed
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_notification_dedupe"),
                      Index("ix_notification_inbox", "tenant_id", "read_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    recipient_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    recipient_contact_id: Mapped[str | None] = mapped_column(String(32))
    recipient_email: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(8), default="info")  # info|warn|high
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    action_label: Mapped[str | None] = mapped_column(String(64))
    action_url: Mapped[str | None] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    object_type: Mapped[str | None] = mapped_column(String(48))
    object_id: Mapped[str | None] = mapped_column(String(32))
    delivery_state: Mapped[str] = mapped_column(String(16), default="pending")
    delivery_channel: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationPreference(Base):
    __tablename__ = "notification_preference"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_pref_kind"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    channel: Mapped[str] = mapped_column(String(16), default="inbox")  # inbox|email|off
    digest: Mapped[bool] = mapped_column(Boolean, default=False)


class TrustScore(Base):
    __tablename__ = "trust_score"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    composite: Mapped[float] = mapped_column(Float, default=0.0)
    band: Mapped[str] = mapped_column(String(16), default="Weak")
    algorithm_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    modifiers_json: Mapped[str | None] = mapped_column(Text)
    withheld_reason: Mapped[str | None] = mapped_column(String(120))
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    domains: Mapped[list["TrustDomainScore"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin")


class TrustDomainScore(Base):
    __tablename__ = "trust_domain_score"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    trust_score_id: Mapped[str] = mapped_column(ForeignKey("trust_score.id"))
    domain: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    expected: Mapped[int] = mapped_column(Integer, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    impaired: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[str | None] = mapped_column(Text)


class ControlSummary(Base):
    __tablename__ = "control_summary"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="heuristic")
    model: Mapped[str | None] = mapped_column(String(64))
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Questionnaire(Base, TimestampMixin):
    __tablename__ = "questionnaire"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str | None] = mapped_column(String(32))
    source_format: Mapped[str] = mapped_column(String(16), default="csv")
    source_filename: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    answered_count: Mapped[int] = mapped_column(Integer, default=0)
    gap_count: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list["QuestionnaireItem"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin",
        order_by="QuestionnaireItem.position")


class QuestionnaireItem(Base):
    __tablename__ = "questionnaire_item"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    questionnaire_id: Mapped[str] = mapped_column(ForeignKey("questionnaire.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    reference: Mapped[str | None] = mapped_column(String(48))
    question: Mapped[str] = mapped_column(Text)
    mapped_types: Mapped[str | None] = mapped_column(String(255))
    answer: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="heuristic")
    status: Mapped[str] = mapped_column(String(16), default="unanswered")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class AnswerBank(Base):
    """Approved answers, reusable across questionnaires - GICC-05 F21.7. The
    compounding asset: the fifth questionnaire should be nearly free."""
    __tablename__ = "answer_bank"
    __table_args__ = (UniqueConstraint("tenant_id", "question_hash", name="uq_answer_hash"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    question_hash: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    citation_types: Mapped[str | None] = mapped_column(String(255))
    times_used: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ApiClient(Base, TimestampMixin):
    __tablename__ = "api_client"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(120))
    key_prefix: Mapped[str] = mapped_column(String(12), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[str] = mapped_column(String(500), default="profile:read documents:read")
    grant_id: Mapped[str | None] = mapped_column(String(32))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentCallLog(Base):
    __tablename__ = "agent_call_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(32))
    api_client_id: Mapped[str | None] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(8), default="mcp")
    mcp_method: Mapped[str | None] = mapped_column(String(48))
    mcp_name: Mapped[str | None] = mapped_column(String(64))
    scopes: Mapped[str | None] = mapped_column(String(255))
    result_status: Mapped[str] = mapped_column(String(24), default="ok")
    bytes_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)
