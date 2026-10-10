"""Tenant roles and their capabilities.

Two planes, kept apart deliberately: a role governs what a person inside the
supplier may do, a grant governs what somebody outside may see. A buyer never
gets a role; an employee never gets a grant.

Capabilities are checked, not roles, so adding a role later means editing one
table rather than hunting through routers.
"""
from __future__ import annotations

from fastapi import HTTPException

from ..models import User

# Capability vocabulary
DOCUMENTS_WRITE = "documents:write"      # add documents, publish versions, verify
GROUPS_WRITE = "groups:write"            # group membership and disclosure policy
CLIENTS_WRITE = "clients:write"          # client accounts, contacts
GRANTS_WRITE = "grants:write"            # issue, extend, revoke access
REQUIREMENTS_RUN = "requirements:run"    # run conformance evaluations
PROFILE_WRITE = "profile:write"          # company profile and ownership
SETTINGS_WRITE = "settings:write"        # users, API keys
LIFECYCLE_WRITE = "lifecycle:write"      # export, closure, purge
HOLD_WRITE = "hold:write"                # place or release a legal hold
TASKS_WRITE = "tasks:write"

ROLES: dict[str, dict] = {
    "owner": {
        "label": "Owner",
        "summary": "Full control of the workspace, including closure and API keys.",
        "capabilities": {DOCUMENTS_WRITE, GROUPS_WRITE, CLIENTS_WRITE, GRANTS_WRITE,
                         REQUIREMENTS_RUN, PROFILE_WRITE, SETTINGS_WRITE,
                         LIFECYCLE_WRITE, HOLD_WRITE, TASKS_WRITE},
    },
    "compliance": {
        "label": "Compliance manager",
        "summary": "Owns the credentials: uploads, versions, verification, groups.",
        "capabilities": {DOCUMENTS_WRITE, GROUPS_WRITE, REQUIREMENTS_RUN, PROFILE_WRITE,
                         HOLD_WRITE, TASKS_WRITE},
    },
    "sales": {
        "label": "Sales and bid",
        "summary": "Owns disclosure: clients, grants, share links, access requests.",
        "capabilities": {CLIENTS_WRITE, GRANTS_WRITE, REQUIREMENTS_RUN, TASKS_WRITE},
    },
    "viewer": {
        "label": "Viewer",
        "summary": "Read-only across the workspace.",
        "capabilities": set(),
    },
}

ROLE_ORDER = ["owner", "compliance", "sales", "viewer"]


def capabilities(user: User) -> set[str]:
    return ROLES.get(user.role, ROLES["viewer"])["capabilities"]


def can(user: User, capability: str) -> bool:
    return capability in capabilities(user)


def role_label(role: str) -> str:
    return ROLES.get(role, {}).get("label", role.title())


def require(user: User, capability: str) -> None:
    """Raises 403 with a message that names the role needed, so the person knows
    who to ask rather than just being told no."""
    if can(user, capability):
        return
    allowed = [ROLES[r]["label"] for r in ROLE_ORDER if capability in ROLES[r]["capabilities"]]
    raise HTTPException(
        403, f"Your role ({role_label(user.role)}) cannot do this. "
             f"Ask {' or a '.join(allowed) if allowed else 'an Owner'} to make the change.")


def can_read_sensitive(user: User, sensitivity: str) -> bool:
    """C and S class documents need the sensitive-access flag on top of a role."""
    if sensitivity in {"C", "S"}:
        return bool(user.sensitive_access)
    return True
