from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

API_PREFIX = "/admin_api/v1"
DEFAULT_TIMEOUT = 30.0


class KeitaroAPIError(RuntimeError):
    """Error raised when a Keitaro API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KeitaroAPI:
    """Thin synchronous client for the Keitaro Admin API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("KEITARO_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("KEITARO_API_KEY", "")
        self.timeout = (
            timeout
            if timeout is not None
            else float(os.getenv("KEITARO_TIMEOUT", DEFAULT_TIMEOUT))
        )
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if self.api_key:
            self.session.headers.update({"Api-Key": self.api_key})

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        if not self.configured:
            raise KeitaroAPIError(
                "Не заданы KEITARO_BASE_URL и/или KEITARO_API_KEY (проверьте .env)"
            )

        url = self._url(path)
        try:
            response = self.session.request(
                method, url, params=params, json=json, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise KeitaroAPIError(
                f"Не удалось выполнить запрос к Keitaro ({method} {url}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise KeitaroAPIError(
                f"Keitaro ответил {response.status_code} на {method} {url}: "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise KeitaroAPIError(
                f"Keitaro вернул ответ не в формате JSON на {method} {url}"
            ) from exc

    # --- API Methods ---

    def ping(self) -> bool:
        """Check the tracker availability and the API key validity."""
        self._request("GET", "campaigns", params={"limit": 1})
        return True

    def get_campaigns(self) -> list[dict[str, Any]]:
        """Get the campaign list."""
        data = self._request("GET", "campaigns")
        return self._ensure_list(data, "campaigns")

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        data = self._request("GET", f"campaigns/{campaign_id}")
        if not isinstance(data, dict):
            raise KeitaroAPIError(
                f"Неожиданный формат ответа для кампании {campaign_id}"
            )
        return data

    def get_streams(self, campaign_id: int) -> list[dict[str, Any]]:
        """Get the streams of a campaign."""
        data = self._request("GET", f"campaigns/{campaign_id}/streams")
        return self._ensure_list(data, f"streams кампании {campaign_id}")

    def get_stream(self, stream_id: int) -> dict[str, Any]:
        """Get detailed information about a single stream."""
        data = self._request("GET", f"streams/{stream_id}")
        if not isinstance(data, dict):
            raise KeitaroAPIError(f"Неожиданный формат ответа для потока {stream_id}")
        return data
    
    def update_stream(self, stream_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Update stream settings (PUT /streams/{stream_id})."""
        data = self._request("PUT", f"streams/{stream_id}", json=payload)
        if not isinstance(data, dict):
            raise KeitaroAPIError(f"Неожиданный формат ответа при обновлении потока {stream_id}")
        return data
    
    def manage_offer_in_stream(
        self, stream_id: int, offer_id: int, action: str, share: int = 100
    ) -> dict[str, Any]:
        """Add or remove a single offer in a campaign stream."""
        stream_data = self.get_stream(stream_id)

        current_offers = self._extract_offers(stream_data)

        if action == "add":
            if not any(o.get("offer_id") == offer_id for o in current_offers):
                current_offers.append(
                    {"offer_id": offer_id, "share": share, "state": "active"}
                )
        elif action == "remove":
            current_offers = [o for o in current_offers if o.get("offer_id") != offer_id]
        else:
            raise KeitaroAPIError(f"Неизвестное действие: {action}. Используйте 'add' или 'remove'.")

        return self.update_stream(
            stream_id, self._stream_update_payload(stream_data, current_offers)
        )

    def replace_stream_offers(
        self, stream_id: int, offers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        stream_data = self.get_stream(stream_id)
        normalized = self._normalize_offers(offers)
        return self.update_stream(
            stream_id, self._stream_update_payload(stream_data, normalized)
        )

    @classmethod
    def _extract_offers(cls, stream_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract offers from a Keitaro response, keeping only the writable fields."""
        raw_offers = stream_data.get("offers")
        return cls._normalize_offers(raw_offers if isinstance(raw_offers, list) else [])

    @staticmethod
    def _normalize_offers(offers: list[Any]) -> list[dict[str, Any]]:
        """Normalize offers to the OfferStreamRequest format: offer_id, share, state."""
        normalized: list[dict[str, Any]] = []
        for item in offers:
            if not isinstance(item, dict) or item.get("offer_id") in (None, ""):
                continue
            normalized.append(
                {
                    "offer_id": int(item["offer_id"]),
                    "share": int(item.get("share") or 0),
                    "state": item.get("state") or "active",
                }
            )
        return normalized

    @staticmethod
    def _stream_update_payload(
        stream_data: dict[str, Any], offers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Build the stream update request body while keeping the existing settings."""
        payload: dict[str, Any] = {
            "name": stream_data.get("name"),
            "type": stream_data.get("type", "regular"),
            "action_payload": stream_data.get("action_payload", ""),
            "state": stream_data.get("state", "active"),
            "offers": offers,
        }
        if stream_data.get("action_type"):
            payload["action_type"] = stream_data["action_type"]
        if stream_data.get("schema"):
            payload["schema"] = stream_data["schema"]
        return payload

    # --- dictionaries for the frontend ---

    def get_offers(self) -> list[dict[str, Any]]:
        """Get the offer list for autocomplete/selection."""
        data = self._request("GET", "offers")
        return self._ensure_list(data, "offers")

    def get_groups(self, type_name: str = "campaigns") -> list[dict[str, Any]]:
        """Get the group list (campaigns by default)."""
        data = self._request("GET", "groups", params={"type": type_name})
        return self._ensure_list(data, "groups")

    def get_sources(self) -> list[dict[str, Any]]:
        """Get the traffic source list."""
        data = self._request("GET", "traffic_sources")
        return self._ensure_list(data, "sources")

    def get_domains(self) -> list[dict[str, Any]]:
        """Get the list of available domains."""
        data = self._request("GET", "domains")
        return self._ensure_list(data, "domains")

    def create_group(
        self,
        name: str,
        type_name: str = "campaigns",
        position: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "type": type_name}
        if position is not None:
            payload["position"] = position
        data = self._request("POST", "groups", json=payload)
        return self._ensure_dict(data, f"группы «{name}»")

    def create_domain(
        self,
        name: str,
        group_id: int | None = None,
        notes: str | None = None,
        catch_not_found: bool = False,
    ) -> dict[str, Any]:
        """Create a domain (POST /domains). Only the ``name`` field is required."""
        payload: dict[str, Any] = {"name": name, "catch_not_found": catch_not_found}
        if group_id is not None:
            payload["group_id"] = str(group_id)
        if notes:
            payload["notes"] = notes
        data = self._request("POST", "domains", json=payload)
        return self._ensure_dict(data, f"домена «{name}»")

    # --- create companies and streams (Creator) ---

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new campaign (POST /campaigns)."""
        data = self._request("POST", "campaigns", json=payload)
        if not isinstance(data, dict):
            raise KeitaroAPIError("Неожиданный формат ответа при создании кампании")
        return data

    def create_stream(self, campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
            """Create a stream in a campaign (POST /streams with campaign_id in the body)."""
            body = {**payload, "campaign_id": campaign_id}
            data = self._request("POST", "streams", json=body)
            if not isinstance(data, dict):
                raise KeitaroAPIError(
                    f"Неожиданный формат ответа при создании потока для кампании {campaign_id}"
                )
            return data

    def setup_campaign_with_two_streams(
        self,
        name: str,
        geo: str,
        offer_id: int,
        group_id: int | None = None,
        source_id: int | None = None,
        domain_id: int | None = None,
    ) -> dict[str, Any]:
        """High-level Creator method: campaign + geo stream (to google) + offer stream."""
        campaign_payload: dict[str, Any] = {
            "name": name,
            "alias": name.lower().replace(" ", "-"),
            "type": "position",
            "state": "active",
        }
        if group_id is not None:
            campaign_payload["group_id"] = group_id
        if source_id is not None:
            campaign_payload["traffic_source_id"] = source_id
        if domain_id is not None:
            campaign_payload["domain_id"] = domain_id

        campaign = self.create_campaign(campaign_payload)
        campaign_id = campaign.get("id")
        if not campaign_id:
            raise KeitaroAPIError("Keitaro не вернул ID созданной кампании")

        created_streams = []

        stream_geo_payload = {
            "name": f"Geo Filter ({geo}) -> Google",
            "type": "regular",
            "schema": "redirect",
            "action_type": "http",
            "action_payload": "https://google.com",
            "state": "active",
            "filters": [
                {
                    "name": "country",
                    "mode": "accept",
                    "payload": [geo],
                }
            ],
        }
        stream1 = self.create_stream(campaign_id, stream_geo_payload)
        created_streams.append(stream1)

        stream_offer_payload = {
            "name": f"Offer Stream (ID: {offer_id})",
            "type": "regular",
            "schema": "landings",
            "action_type": "http",
            "state": "active",
            "offers": [
                {
                    "offer_id": offer_id,
                    "share": 100,
                    "state": "active",
                }
            ],
        }
        stream2 = self.create_stream(campaign_id, stream_offer_payload)
        created_streams.append(stream2)

        return {
            "campaign": campaign,
            "streams": created_streams,
        }

    @staticmethod
    def _ensure_list(data: Any, what: str) -> list[dict[str, Any]]:
        """Coerce a Keitaro response into a list of dicts."""
        if data is None:
            return []
        if isinstance(data, dict):
            for key in ("items", "campaigns", "streams", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
            raise KeitaroAPIError(f"Неожиданный формат ответа для {what}")
        if not isinstance(data, list):
            raise KeitaroAPIError(f"Неожиданный формат ответа для {what}")
        return data

    @staticmethod
    def _ensure_dict(data: Any, what: str) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        raise KeitaroAPIError(f"Неожиданный формат ответа для {what}")


def parse_keitaro_datetime(value: Any) -> datetime | None:
    """Parse a datetime from a Keitaro response."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed