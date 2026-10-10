"""Password hashing, session and API-key handling.

Uses stdlib scrypt rather than bcrypt/passlib so there is no native build step
on Render and no dependency that can break a deploy.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiClient, BreakGlassSession, PlatformAdmin, Tenant, User, now

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, sha256_hash). The full key is shown once."""
    raw = secrets.token_urlsafe(32)
    key = f"gicc_{raw}"
    return key, key[:12], hashlib.sha256(key.encode()).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_share_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    return token, hash_token(token)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(status_code=303, detail="login required",
                            headers={"Location": "/login"})
    return user


def require_tenant(user: User = Depends(require_user), db: Session = Depends(get_db)) -> Tenant:
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    return tenant


# --------------------------------------------------------------------------
# Platform operators. A separate session key and a separate table: an admin is
# never "a user with a flag", so a stolen supplier session can never escalate.
# --------------------------------------------------------------------------
def current_admin(request: Request, db: Session = Depends(get_db)) -> PlatformAdmin | None:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    admin = db.get(PlatformAdmin, admin_id)
    return admin if admin and admin.is_active else None


def require_admin(request: Request, db: Session = Depends(get_db)) -> PlatformAdmin:
    admin = current_admin(request, db)
    if admin is None:
        raise HTTPException(status_code=303, detail="admin sign-in required",
                            headers={"Location": "/admin/login"})
    return admin


def active_break_glass(request: Request, db: Session) -> BreakGlassSession | None:
    """Returns the live break-glass session for this browser session, expiring
    it if the time box has run out."""
    session_id = request.session.get("break_glass_id")
    if not session_id:
        return None
    bg = db.get(BreakGlassSession, session_id)
    if bg is None or bg.ended_at:
        request.session.pop("break_glass_id", None)
        return None
    if bg.expires_at <= now():
        bg.ended_at = now()
        db.commit()
        request.session.pop("break_glass_id", None)
        request.session.pop("user_id", None)
        return None
    return bg


def api_client_from_request(request: Request, db: Session) -> ApiClient | None:
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if not key:
        return None
    digest = hashlib.sha256(key.encode()).hexdigest()
    client = db.scalar(
        select(ApiClient).where(ApiClient.key_prefix == key[:12],
                                ApiClient.key_hash == digest,
                                ApiClient.revoked_at.is_(None)))
    return client


def require_api_client(request: Request, db: Session = Depends(get_db)) -> ApiClient:
    client = api_client_from_request(request, db)
    if client is None:
        raise HTTPException(401, "Provide a valid API key in X-API-Key or Authorization: Bearer")
    return client


def has_scope(client: ApiClient, scope: str) -> bool:
    return scope in (client.scopes or "").split()
