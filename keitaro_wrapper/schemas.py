"""Pydantic schemas for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    """Base schema for responses built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class HealthOut(BaseModel):
    status: str = Field(description="Service status", examples=["ok"])
    database: str = Field(description="Database status", examples=["ok"])
    keitaro_configured: bool = Field(
        description="Whether KEITARO_BASE_URL and KEITARO_API_KEY are set in .env"
    )


class KeitaroPingOut(BaseModel):
    status: str = Field(examples=["ok"])
    base_url: str = Field(description="Base URL of the tracker that was called")


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
    """An offer inside a campaign stream."""

    offer_id: int = Field(description="Keitaro offer ID")
    share: int = Field(default=100, description="Traffic share, %")
    state: str = Field(default="active", description="Offer state: active or disabled")


class StreamOffersUpdate(BaseModel):
    """The full set of stream offers — "PUSH TO KT" from the editor.

    Keitaro accepts the offer list as a whole, so the frontend sends the
    final list (with shares and without the removed offers).
    """

    offers: list[StreamOfferItem] = Field(description="Final list of stream offers")


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
        description="Stream offers stored in the local cache",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
        description="Raw Keitaro response — used only to parse offers",
    )

    @model_validator(mode="after")
    def _fill_offers_from_raw(self) -> "StreamOut":
        """Pull offers from the stored Keitaro response if none were passed explicitly."""
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
    """Keitaro synchronization summary."""

    campaigns_total: int = 0
    campaigns_created: int = 0
    campaigns_updated: int = 0
    streams_total: int = 0
    streams_created: int = 0
    streams_updated: int = 0

class CampaignCreateRequest(BaseModel):
    """Data for creating a campaign through the 'creator'."""
    name: str = Field(description="Campaign name")
    geo: str = Field(description="Country geo code, e.g. AU, MX, RU")
    offer_id: int = Field(description="Keitaro offer ID")
    group_id: int | None = Field(default=None, description="Campaign group ID")
    source_id: int | None = Field(default=None, description="Traffic source ID")
    domain_id: int | None = Field(default=None, description="Domain ID")

class GroupCreateRequest(BaseModel):
    """Data for creating a Keitaro group."""

    name: str = Field(description="Group name")
    type: str = Field(
        default="campaigns",
        description="Group type: campaigns, offers, landings or domains",
    )
    position: int | None = Field(default=None, description="Group position in the list")


class DomainCreateRequest(BaseModel):
    """Data for creating a Keitaro domain."""

    name: str = Field(description="Domain name, e.g. track.example.com")
    group_id: int | None = Field(default=None, description="Domain group ID")
    notes: str | None = Field(default=None, description="Notes for the domain")
    catch_not_found: bool = Field(
        default=False, description="Catch traffic of non-existent URLs"
    )


class OfferUpdateAction(BaseModel):
    offer_id: int = Field(description="Keitaro offer ID")
    action: str = Field(description="Action: 'add' or 'remove'")
    share: int = Field(default=100, description="Traffic share for the offer (defaults to 100)")