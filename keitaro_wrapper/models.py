"""ORM-модели локального кеша данных Keitaro."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utcnow() -> datetime:
    """Текущее время в UTC — значение по умолчанию для timestamp-полей."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Служебные поля, общие для всех таблиц кеша."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Campaign(TimestampMixin, Base):
    """Кампания Keitaro, сохранённая в локальной БД."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keitaro_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    state: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str | None] = mapped_column(String(64))
    group_id: Mapped[int | None] = mapped_column(Integer)
    domain_id: Mapped[int | None] = mapped_column(Integer)
    keitaro_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    streams: Mapped[list["Stream"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Stream(TimestampMixin, Base):
    """Поток (stream) кампании Keitaro."""

    __tablename__ = "streams"
    __table_args__ = (
        UniqueConstraint("campaign_id", "keitaro_id", name="uq_streams_campaign_keitaro"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keitaro_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    type: Mapped[str | None] = mapped_column(String(64))
    action_type: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(32))
    position: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    campaign: Mapped[Campaign] = relationship(back_populates="streams")