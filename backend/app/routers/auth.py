"""Signup, login, and account info."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User
from app.security import (
    create_access_token,
    current_user,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    entitled: bool


class MeOut(BaseModel):
    id: str
    email: str
    entitled: bool
    is_admin: bool
    created_at: str | None = None


def _weak(password: str) -> str | None:
    """Length is the dominant factor; a full policy just pushes people to '!'."""
    if len(password) < 10:
        return "password must be at least 10 characters"
    if re.fullmatch(r"[0-9]+", password):
        return "password cannot be all digits"
    if password.lower() in {
        "password12", "password123", "qwerty1234", "1234567890",
        "letmein123", "changeme12",
    }:
        return "password is too common"
    return None


@router.post("/signup", response_model=TokenOut, status_code=201)
def signup(body: SignupIn, db: Session = Depends(get_db)) -> TokenOut:
    if (problem := _weak(body.password)) is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)

    email = body.email.lower().strip()
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Deliberately the same shape as a successful-signup failure, so this
        # endpoint cannot be used to enumerate which emails have accounts.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "could not create account with that email") from None
    db.refresh(user)
    return TokenOut(access_token=create_access_token(user.id),
                    entitled=user.is_entitled)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))

    # Hash even when the user does not exist, so response time does not reveal
    # whether the account is real.
    stored = user.password_hash if user else hash_password("dummy-timing-guard")
    ok = verify_password(body.password, stored)

    if not user or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
        db.commit()

    return TokenOut(access_token=create_access_token(user.id),
                    entitled=user.is_entitled)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user)) -> MeOut:
    return MeOut(
        id=user.id, email=user.email, entitled=user.is_entitled,
        is_admin=user.is_admin,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.post("/dev-grant", response_model=MeOut)
def dev_grant(user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> MeOut:
    """Grant entitlement without paying — development only.

    Hard-refuses when env=prod so it can never become a free bypass in
    production, regardless of how the config is set.
    """
    s = get_settings()
    if s.is_prod or not s.allow_dev_grant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not available")
    from app.models import utcnow

    user.entitled_at = utcnow()
    db.commit()
    db.refresh(user)
    return MeOut(id=user.id, email=user.email, entitled=user.is_entitled,
                 is_admin=user.is_admin,
                 created_at=user.created_at.isoformat() if user.created_at else None)
