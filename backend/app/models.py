"""Database models.

Two decisions that answer "scalable, and can add new fields":

1. **Every table carries a `data` JSON column.** New attributes — a detector
   field, a user preference, a purchase flag — land there with no migration and
   no downtime. Promote a field to a real column later, once it has proven it
   needs indexing or constraints. This is what lets the detector evolve
   `ticker.alert.v1` without the backend blocking the release.

2. **The full detector payload is stored verbatim** in `Event.payload`. Even if
   the backend does not understand a field yet, it is retained and served, so
   clients can start using it before the backend is updated. Nothing is lost in
   translation.

Scaling shape: the app servers are stateless (JWT auth, no server-side
sessions), so throughput scales by adding replicas. All shared state is in the
database, with Redis only as a fanout bus.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres (indexable, binary), plain JSON on SQLite. Same Python API.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    # Non-null = has lifetime access. A timestamp rather than a boolean so we
    # can always answer "since when", which billing disputes require.
    entitled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                           index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Future: notification preferences, quiet hours, auto-invest settings.
    data: Mapped[dict] = mapped_column(JSONType, default=dict)

    devices: Mapped[list[Device]] = relationship(back_populates="user",
                                                 cascade="all, delete-orphan")
    purchases: Mapped[list[Purchase]] = relationship(back_populates="user",
                                                     cascade="all, delete-orphan")

    @property
    def is_entitled(self) -> bool:
        return self.entitled_at is not None


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    # Unique so a replayed Stripe webhook cannot double-record a purchase.
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), unique=True,
                                                          nullable=True)
    stripe_payment_intent: Mapped[str | None] = mapped_column(String(255),
                                                              nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                     nullable=True)
    data: Mapped[dict] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="purchases")


class Event(Base):
    """A detection published by the VPS detector."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # The detector's id. (event_id, state) is the natural key: a CONFIRMED and
    # its later RETRACTED are two rows for one story.
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(24), index=True)
    target: Mapped[str] = mapped_column(String(255), index=True)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    detect_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), default="ticker.alert.v1")
    # THE extensibility hatch: the complete detector payload, verbatim. Unknown
    # fields survive here and are served to clients untouched.
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)

    __table_args__ = (
        UniqueConstraint("event_id", "state", name="uq_event_state"),
        Index("ix_events_feed", "received_at", "state"),
    )


class Device(Base):
    """A push destination. `kind` lets APNs/FCM join later without a migration."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    kind: Mapped[str] = mapped_column(String(16), default="webpush")  # webpush|apns|fcm
    # Web Push endpoint, or an APNs/FCM device token.
    token: Mapped[str] = mapped_column(Text)
    keys: Mapped[dict] = mapped_column(JSONType, default=dict)
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="devices")

    __table_args__ = (
        UniqueConstraint("kind", "token", name="uq_device_token"),
    )


class Delivery(Base):
    """Per-user, per-event delivery record.

    Exists so "did this user actually get told?" is answerable after the fact —
    the question that matters when someone says they missed the alert they paid
    for.
    """

    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    event_row_id: Mapped[str] = mapped_column(ForeignKey("events.id",
                                                         ondelete="CASCADE"),
                                              index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         index=True)
    channel: Mapped[str] = mapped_column(String(16))     # webpush|sse|apns|fcm
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)
    data: Mapped[dict] = mapped_column(JSONType, default=dict)

    __table_args__ = (
        UniqueConstraint("event_row_id", "user_id", "channel", name="uq_delivery"),
    )
