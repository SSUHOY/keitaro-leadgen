"""Pydantic-схемы запросов и ответов API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    """Базовая схема для ответов, собираемых из ORM-объектов."""

    model_config = ConfigDict(from_attributes=True)


class HealthOut(BaseModel):
    status: str = Field(description="Состояние сервиса", examples=["ok"])
    database: str = Field(description="Состояние БД", examples=["ok"])
    keitaro_configured: bool = Field(
        description="Заданы ли KEITARO_BASE_URL и KEITARO_API_KEY в .env"
    )


class KeitaroPingOut(BaseModel):
    status: str = Field(examples=["ok"])
    base_url: str = Field(description="Базовый URL трекера, к которому было обращение")


class CampaignOut(ORMModel):
    id: int
    keitaro_id: int
    name: str
    state: str | None = None
    type: str | None = None
    group_id: int | None = None
    domain_id: int | None = None
    keitaro_updated_at: datetime | None = None
    created_at: datetime
    synced_at: datetime


class StreamOfferItem(BaseModel):
    """Оффер внутри потока кампании."""

    offer_id: int = Field(description="ID оффера в Keitaro")
    share: int = Field(default=100, description="Доля трафика, %")
    state: str = Field(default="active", description="Состояние оффера: active или disabled")


class StreamOffersUpdate(BaseModel):
    """Полный набор офферов потока — «PUSH TO KT» из редактора.

    Keitaro принимает список офферов целиком, поэтому фронтенд присылает
    итоговый список (с долями и без удалённых офферов).
    """

    offers: list[StreamOfferItem] = Field(description="Итоговый список офферов потока")


class StreamOut(ORMModel):
    id: int
    keitaro_id: int
    campaign_id: int
    name: str
    type: str | None = None
    action_type: str | None = None
    state: str | None = None
    position: int | None = None
    created_at: datetime
    synced_at: datetime
    offers: list[StreamOfferItem] = Field(
        default_factory=list,
        description="Офферы потока, сохранённые в локальном кеше",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
        description="Исходный ответ Keitaro — используется только для разбора офферов",
    )

    @model_validator(mode="after")
    def _fill_offers_from_raw(self) -> "StreamOut":
        """Достать офферы из сохранённого ответа Keitaro, если они не переданы явно."""
        if self.offers or not isinstance(self.raw, dict):
            return self

        raw_offers = self.raw.get("offers")
        if not isinstance(raw_offers, list):
            return self

        offers: list[StreamOfferItem] = []
        for item in raw_offers:
            if isinstance(item, dict) and item.get("offer_id") is not None:
                offers.append(
                    StreamOfferItem(
                        offer_id=int(item["offer_id"]),
                        share=int(item.get("share") or 0),
                        state=str(item.get("state") or "active"),
                    )
                )
        self.offers = offers
        return self


class SyncResult(BaseModel):
    """Итоги синхронизации с Keitaro."""

    campaigns_total: int = 0
    campaigns_created: int = 0
    campaigns_updated: int = 0
    streams_total: int = 0
    streams_created: int = 0
    streams_updated: int = 0

class CampaignCreateRequest(BaseModel):
    """Данные для создания кампании через 'создаватор'."""
    name: str = Field(description="Название кампании")
    geo: str = Field(description="Гео-код страны, например AU, MX, RU")
    offer_id: int = Field(description="ID оффера в Keitaro")
    group_id: int | None = Field(default=None, description="ID группы кампаний")
    source_id: int | None = Field(default=None, description="ID источника трафика")
    domain_id: int | None = Field(default=None, description="ID домена")

class GroupCreateRequest(BaseModel):
    """Данные для создания группы Keitaro."""

    name: str = Field(description="Название группы")
    type: str = Field(
        default="campaigns",
        description="Тип группы: campaigns, offers, landings или domains",
    )
    position: int | None = Field(default=None, description="Позиция группы в списке")


class DomainCreateRequest(BaseModel):
    """Данные для создания домена Keitaro."""

    name: str = Field(description="Имя домена, например track.example.com")
    group_id: int | None = Field(default=None, description="ID группы доменов")
    notes: str | None = Field(default=None, description="Заметки к домену")
    catch_not_found: bool = Field(
        default=False, description="Ловить трафик несуществующих URL"
    )


class OfferUpdateAction(BaseModel):
    offer_id: int = Field(description="ID оффера в Keitaro")
    action: str = Field(description="Действие: 'add' или 'remove'")
    share: int = Field(default=100, description="Доля трафика для оффера (по умолчанию 100)")