"""Passwords, tokens, and verification of signed detector requests."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

# Argon2id: memory-hard, so a leaked database is expensive to crack offline.
# Defaults are tuned for a small box; raise time_cost on bigger hardware.
_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return False


# --- tokens ----------------------------------------------------------------


def create_access_token(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.access_token_ttl_s)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> str | None:
    s = get_settings()
    try:
        data = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    return data.get("sub")


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # EventSource cannot set headers, so the SSE endpoint accepts ?token=.
    return request.query_params.get("token")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


def current_user_optional(request: Request,
                          db: Session = Depends(get_db)) -> User | None:
    try:
        return current_user(request, db)
    except HTTPException:
        return None


def require_entitled(user: User = Depends(current_user)) -> User:
    """Gate for paid features. 402 so the client knows to show checkout."""
    if not user.is_entitled:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "purchase required to access alerts",
        )
    return user


# --- detector request verification ----------------------------------------


def verify_detector_signature(header: str, body: bytes) -> bool:
    """Mirror of ticker/notify/webhook.py:sign() on the detector side.

    HMAC-SHA256 over "{timestamp}.{body}". The timestamp is inside the signed
    material and checked against a tolerance, so a captured request cannot be
    replayed later.
    """
    s = get_settings()
    if not s.ingest_secret:
        return False          # fail closed: unconfigured means untrusted
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        got = parts["v1"]
    except (ValueError, KeyError, AttributeError):
        return False
    if abs(time.time() - ts) > s.ingest_tolerance_s:
        return False
    expected = hmac.new(
        s.ingest_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, got)
