"""FastAPI wrapper around the Keitaro tracker.

The service pulls campaigns and streams from the Keitaro Admin API, stores
them in a local database and exposes them through a REST API.

Web UI (Tailwind) — ``GET /`` (the ``static/index.html`` file),
static assets — ``/static/<file>``.

Run: ``uvicorn main:app --reload`` from the ``keitaro_wrapper`` directory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Campaign, Stream, utcnow
from schemas import (
    CampaignCreateRequest,
    CampaignOut,
    DomainCreateRequest,
    GroupCreateRequest,
    HealthOut,
    KeitaroPingOut,
    OfferUpdateAction,
    StreamOffersUpdate,
    StreamOut,
    SyncResult,
)
from services.keitaro_api import KeitaroAPI, KeitaroAPIError, parse_keitaro_datetime

# Static directory (index.html); the path does not depend on the current CWD
STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("keitaro_wrapper")


class StepTimer:
    """Collect the durations of request stages in milliseconds."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.started_at = time.perf_counter()
        self.timings_ms: dict[str, float] = {}

    @contextmanager
    def measure(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.timings_ms[stage] = round(self.timings_ms.get(stage, 0.0) + elapsed_ms, 2)

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self.started_at) * 1000, 2)

    def log(self, extra: str = "") -> None:
        parts = " | ".join(f"{k}={v}ms" for k, v in self.timings_ms.items())
        suffix = f" ({extra})" if extra else ""
        logger.info("[TIMING] %s%s: total=%sms | %s", self.label, suffix, self.total_ms, parts)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Iterator[None]:
    init_db()
    logger.info("Таблицы БД готовы, сервис запущен")
    yield


app = FastAPI(
    title="Keitaro Wrapper API",
    description="Wrapper around the Keitaro Admin API: campaign and stream sync",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(KeitaroAPIError)
async def keitaro_error_handler(_: Any, exc: KeitaroAPIError) -> JSONResponse:
    """Any Keitaro error is returned to the client as 502 Bad Gateway."""
    logger.error("Ошибка Keitaro: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def get_keitaro_api() -> KeitaroAPI:
    """FastAPI dependency: a Keitaro client configured from .env."""
    return KeitaroAPI()


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_str(value: Any, limit: int = 255) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result[:limit] or None


def _like_pattern(term: str, limit: int = 255) -> str:
    """Build a LIKE pattern for search: special chars (``%``, ``_``, ``\\``) are escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped[:limit]}%"


def _get_or_create_local_campaign(
    db: Session, keitaro_id: int, name: str | None = None
) -> Campaign:
    """Find a local campaign by its Keitaro id, creating the row if it is missing."""
    campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))
    if campaign is None:
        campaign = Campaign(keitaro_id=keitaro_id, name=name or f"Campaign {keitaro_id}")
        db.add(campaign)
        db.flush()
    return campaign


def _merge_offers_into_raw(new_raw: dict[str, Any], previous_raw: Any) -> dict[str, Any]:
    if "offers" in new_raw or not isinstance(previous_raw, dict):
        return new_raw
    previous_offers = previous_raw.get("offers")
    if isinstance(previous_offers, list) and previous_offers:
        return {**new_raw, "offers": previous_offers}
    return new_raw


def _drop_other_stream_rows(db: Session, stream_obj: Stream | None, keitaro_id: int) -> None:
    """Delete redundant local stream rows: a stream id in Keitaro is unique.

    This removes the "orphaned" rows produced by an old bug
    (``Stream.campaign_id`` held the Keitaro campaign id instead of the local PK)
    as well as any other duplicates of the same stream.
    """
    obsolete = [
        row
        for row in db.scalars(select(Stream).where(Stream.keitaro_id == keitaro_id))
        if row is not stream_obj
    ]
    if not obsolete:
        return
    for row in obsolete:
        db.delete(row)
    # Persist the deletions right away: otherwise the INSERT/UPDATE of the new
    # row may collide with the unique index (campaign_id, keitaro_id)
    db.flush()


def _persist_stream_snapshot(
    db: Session,
    raw_stream: dict[str, Any],
    campaign: Campaign,
    fallback_stream_id: int | None = None,
) -> tuple[Stream, bool]:
    """Save a Keitaro stream snapshot into the local cache (no API calls).

    ``Stream.campaign_id`` is the PK of the ``campaigns`` table, not the Keitaro campaign id.
    Returns a pair "stream object — whether it was created during this sync".
    """
    stream_kt_id = _to_int(raw_stream.get("id")) or fallback_stream_id
    if stream_kt_id is None:
        raise KeitaroAPIError("Keitaro не вернул id потока")

    now = utcnow()
    stream_obj = db.scalar(select(Stream).where(Stream.keitaro_id == stream_kt_id))
    created = stream_obj is None
    if stream_obj is None:
        stream_obj = Stream(
            keitaro_id=stream_kt_id,
            campaign_id=campaign.id,
            name=_to_str(raw_stream.get("name")) or f"Stream {stream_kt_id}",
            created_at=parse_keitaro_datetime(raw_stream.get("created_at")) or now,
        )

    _drop_other_stream_rows(db, stream_obj, stream_kt_id)
    if created:
        db.add(stream_obj)
        # autoflush is disabled, so persist the insert: otherwise the next
        # snapshot in the same session won't find the stream and will create a duplicate
        db.flush()
    elif stream_obj.campaign_id != campaign.id:
        stream_obj.campaign_id = campaign.id

    stream_obj.name = _to_str(raw_stream.get("name")) or stream_obj.name
    stream_obj.type = _to_str(raw_stream.get("type"))
    stream_obj.action_type = _to_str(raw_stream.get("action_type"))
    stream_obj.state = _to_str(raw_stream.get("state"))
    stream_obj.position = _to_int(raw_stream.get("position"))
    stream_obj.raw = _merge_offers_into_raw(raw_stream, stream_obj.raw)
    stream_obj.synced_at = now
    campaign.synced_at = now
    return stream_obj, created


def sync_campaigns(db: Session, api: KeitaroAPI) -> SyncResult:
    """Fetch campaigns and their streams from Keitaro and store them in the local DB."""
    payloads = api.get_campaigns()
    stats: dict[str, int] = {
        "campaigns_total": len(payloads),
        "campaigns_created": 0,
        "campaigns_updated": 0,
        "streams_total": 0,
        "streams_created": 0,
        "streams_updated": 0,
    }

    for payload in payloads:
        keitaro_id = _to_int(payload.get("id"))
        if keitaro_id is None:
            logger.warning("Пропущена кампания без id: %s", payload)
            continue

        campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))
        if campaign is None:
            campaign = Campaign(keitaro_id=keitaro_id, name=f"Campaign {keitaro_id}")
            db.add(campaign)
            stats["campaigns_created"] += 1
        else:
            stats["campaigns_updated"] += 1

        campaign.name = _to_str(payload.get("name")) or f"Campaign {keitaro_id}"
        campaign.state = _to_str(payload.get("state"), 32)
        campaign.type = _to_str(payload.get("type"), 64)
        campaign.group_id = _to_int(payload.get("group_id"))
        campaign.domain_id = _to_int(payload.get("domain_id"))
        campaign.keitaro_updated_at = parse_keitaro_datetime(payload.get("updated_at"))
        campaign.raw = payload
        campaign.synced_at = utcnow()
        db.flush()

        try:
            stream_payloads = api.get_streams(keitaro_id)
        except KeitaroAPIError as exc:
            # streams of a single campaign must not break the whole sync
            logger.warning("Не удалось получить потоки кампании %s: %s", keitaro_id, exc)
            continue

        stats["streams_total"] += len(stream_payloads)
        for stream_payload in stream_payloads:
            stream_keitaro_id = _to_int(stream_payload.get("id"))
            if stream_keitaro_id is None:
                logger.warning(
                    "Пропущен поток без id в кампании %s: %s", keitaro_id, stream_payload
                )
                continue

            _, created = _persist_stream_snapshot(db, stream_payload, campaign)
            stats["streams_created" if created else "streams_updated"] += 1

    db.commit()
    logger.info("Синхронизация завершена: %s", stats)
    return SyncResult(**stats)


def _get_campaign_or_404(db: Session, keitaro_id: int) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))
    if campaign is None:
        msg = (
            f"Кампания {keitaro_id} не найдена в локальной БД — "
            "выполните POST /campaigns/sync"
        )
        raise HTTPException(status_code=404, detail=msg)
    return campaign


@app.get("/", include_in_schema=False)
def read_root() -> FileResponse:
    """Serve the web UI (static/index.html)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get(
    "/health",
    response_model=HealthOut,
    tags=["service"],
    summary="Service and DB liveness check",
)
def health(
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
) -> HealthOut:
    db.execute(text("SELECT 1"))
    return HealthOut(status="ok", database="ok", keitaro_configured=api.configured)


@app.get(
    "/keitaro/ping",
    response_model=KeitaroPingOut,
    tags=["keitaro"],
    summary="Check Keitaro availability and the API key",
)
def keitaro_ping(api: KeitaroAPI = Depends(get_keitaro_api)) -> KeitaroPingOut:
    api.ping()
    return KeitaroPingOut(status="ok", base_url=api.base_url)


@app.post(
    "/campaigns/sync",
    response_model=SyncResult,
    tags=["campaigns"],
    summary="Sync campaigns and streams from Keitaro",
)
def campaigns_sync(
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
) -> SyncResult:
    return sync_campaigns(db, api)


@app.get(
    "/campaigns",
    response_model=list[CampaignOut],
    tags=["campaigns"],
    summary="Campaign list from the local DB",
)
def list_campaigns(
    search: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        description="Search by name (partial match) or by the Keitaro campaign ID",
    ),
    state: str | None = Query(default=None, description="Filter by state, e.g. active"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Campaign]:
    stmt = select(Campaign)
    if state:
        stmt = stmt.where(Campaign.state == state)
    if search and search.strip():
        term = search.strip()
        conditions = [Campaign.name.ilike(_like_pattern(term), escape="\\")]
        keitaro_id = _to_int(term)
        if keitaro_id is not None:
            conditions.append(Campaign.keitaro_id == keitaro_id)
        stmt = stmt.where(or_(*conditions))
    stmt = stmt.order_by(Campaign.keitaro_id).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@app.get(
    "/campaigns/{keitaro_id}",
    response_model=CampaignOut,
    tags=["campaigns"],
    summary="Campaign by its Keitaro id",
)
def get_campaign(keitaro_id: int, db: Session = Depends(get_db)) -> Campaign:
    return _get_campaign_or_404(db, keitaro_id)


@app.get(
    "/campaigns/{keitaro_id}/streams",
    response_model=list[StreamOut],
    tags=["campaigns"],
    summary="Campaign streams",
)
def list_campaign_streams(
    keitaro_id: int,
    source: str = Query(
        default="local",
        pattern="^(local|keitaro)$",
        description=(
            "Local cache or Keitaro"
        ),
    ),
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
) -> list[Stream]:
    if source == "keitaro":
        campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))
        if campaign is None:
            campaign = _get_or_create_local_campaign(db, keitaro_id)
        for raw_stream in api.get_streams(keitaro_id):
            _persist_stream_snapshot(db, raw_stream, campaign)
        db.commit()
    else:
        campaign = _get_campaign_or_404(db, keitaro_id)

    stmt = (
        select(Stream)
        .where(Stream.campaign_id == campaign.id)
        .order_by(Stream.position, Stream.keitaro_id)
    )
    return list(db.scalars(stmt))


@app.get(
    "/api/offers",
    tags=["creators"],
    summary="Get offers list",
)
def api_get_offers(api: KeitaroAPI = Depends(get_keitaro_api)):
    try:
        return api.get_offers()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось получить офферы: {e}")


GROUP_TYPES = ("campaigns", "offers", "landings", "domains")

@app.get(
    "/api/groups",
    tags=["creators"],
    summary="Keitaro group list by type (for selectors)",
)
def api_get_groups(
    type_name: str = Query(
        default="campaigns",
        alias="type",
        description="Group type: campaigns, offers, landings or domains",
    ),
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    if type_name not in GROUP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип групп «{type_name}». Доступны: {', '.join(GROUP_TYPES)}",
        )
    try:
        return api.get_groups(type_name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось получить группы: {e}")


@app.post(
    "/api/groups",
    tags=["creators"],
    summary="Create group in Keitaro",
)
def api_create_group(
    payload: GroupCreateRequest,
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    if payload.type not in GROUP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип групп «{payload.type}». Доступны: {', '.join(GROUP_TYPES)}",
        )
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название группы не может быть пустым")
    try:
        group = api.create_group(name, type_name=payload.type, position=payload.position)
        return {
            "status": "success",
            "message": f"Группа «{name}» ({payload.type}) создана",
            "group": group,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось создать группу: {e}")


@app.get(
    "/api/domains",
    tags=["creators"],
    summary="Keitaro domain list (for selectors)",
)
def api_get_domains(api: KeitaroAPI = Depends(get_keitaro_api)):
    try:
        return api.get_domains()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось получить домены: {e}")


@app.post(
    "/api/domains",
    tags=["creators"],
    summary="Create a domain in Keitaro",
)
def api_create_domain(
    payload: DomainCreateRequest,
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя домена не может быть пустым")
    try:
        domain = api.create_domain(
            name,
            group_id=payload.group_id,
            notes=payload.notes,
            catch_not_found=payload.catch_not_found,
        )
        return {
            "status": "success",
            "message": f"Домен «{name}» создан",
            "domain": domain,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось создать домен: {e}")


@app.post(
    "/campaigns/create-wrapper",
    tags=["campaigns"],
    summary="Create a campaign with two streams (Creator from the spec)",
)
def create_campaign_wrapper(
    payload: CampaignCreateRequest,
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    """
    Create a campaign in Keitaro:
1. A campaign with a name, group, traffic source and domain.
2. Stream 1: a country filter (Geo) -> google.com.
3. Stream 2: the main stream with the selected offer.
    After creation it automatically syncs this campaign into the local DB.
    """
    try:
        result = api.setup_campaign_with_two_streams(
            name=payload.name,
            geo=payload.geo,
            offer_id=payload.offer_id,
            group_id=payload.group_id,
            source_id=payload.source_id,
            domain_id=payload.domain_id,
        )

        sync_campaigns(db, api)

        created_campaign = result.get("campaign") if isinstance(result, dict) else None
        created_id = created_campaign.get("id") if isinstance(created_campaign, dict) else None

        return {
            "status": "success",
            "message": "Кампания успешно создана в Keitaro и синхронизирована",
            "data": result,
            "campaign_id": created_id,
        }
    except KeitaroAPIError as e:
        raise HTTPException(status_code=e.status_code or 500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка создания кампании: {e}")


@app.post(
    "/campaigns/{keitaro_id}/streams/{stream_id}/offers",
    tags=["campaigns"],
    summary="Update campaign stream offers (Editor)",
)
def manage_stream_offer(
    keitaro_id: int,
    stream_id: int,
    payload: StreamOffersUpdate | OfferUpdateAction,
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    """
    Manage offers of an existing campaign stream. Two formats are supported:

- ``{"offers": [{"offer_id": 1, "share": 100, "state": "active"}]}`` —
      "PUSH TO KT": the full set of stream offers (traffic shares + removals);
- ``{"offer_id": 1, "action": "add" | "remove", "share": 100}`` —
      a point operation on a single offer.
    """
    timer = StepTimer("manage_stream_offer")

    try:
        with timer.measure("db_lookup_campaign"):
            campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))

        if campaign is None:
            with timer.measure("kt_sync_campaign"):
                campaign = _get_or_create_local_campaign(db, keitaro_id)
                for raw_stream in api.get_streams(keitaro_id):
                    _persist_stream_snapshot(db, raw_stream, campaign)
                db.commit()
            with timer.measure("db_verify_campaign"):
                campaign = _get_campaign_or_404(db, keitaro_id)

        with timer.measure("kt_update_offers"):
            if isinstance(payload, StreamOffersUpdate):
                updated_stream = api.replace_stream_offers(
                    stream_id=stream_id,
                    offers=[offer.model_dump() for offer in payload.offers],
                )
                message = (
                    f"Список офферов потока {stream_id} обновлён: "
                    f"{len(payload.offers)} шт."
                )
            else:
                updated_stream = api.manage_offer_in_stream(
                    stream_id=stream_id,
                    offer_id=payload.offer_id,
                    action=payload.action,
                    share=payload.share,
                )
                action_text = "добавлен в" if payload.action == "add" else "удалён из"
                message = f"Оффер #{payload.offer_id} успешно {action_text} поток {stream_id}"

        with timer.measure("db_upsert_stream"):
            persisted_stream = upsert_stream_from_keitaro(
                db=db,
                api=api,
                keitaro_campaign_id=keitaro_id,
                keitaro_stream_id=stream_id,
                fallback_stream=updated_stream,
            )

        timer.log(f"campaign={keitaro_id}, stream={stream_id}")

        return {
            "status": "success",
            "message": message,
            "stream": updated_stream,
            "local_stream": StreamOut.model_validate(persisted_stream),
            "timings_ms": {
                **timer.timings_ms,
                "total": timer.total_ms,
            },
        }
    except HTTPException:
        timer.log(f"campaign={keitaro_id}, stream={stream_id}, error=HTTPException")
        raise
    except KeitaroAPIError as e:
        timer.log(f"campaign={keitaro_id}, stream={stream_id}, error=KeitaroAPIError")
        raise HTTPException(status_code=e.status_code or 500, detail=str(e))
    except Exception as e:
        timer.log(f"campaign={keitaro_id}, stream={stream_id}, error=Exception")
        raise HTTPException(status_code=500, detail=f"Ошибка при изменении офферов потока: {e}")


def upsert_stream_from_keitaro(
    db: Session,
    api: KeitaroAPI,
    keitaro_campaign_id: int,
    keitaro_stream_id: int,
    fallback_stream: dict[str, Any] | None = None,
) -> Stream:
    """Update a single stream in the local DB by its Keitaro id."""
    try:
        raw_stream = api.get_stream(keitaro_stream_id)
    except KeitaroAPIError:
        # If PUT already succeeded but GET unexpectedly failed, reuse the dict from the PUT response
        if isinstance(fallback_stream, dict) and fallback_stream:
            raw_stream = fallback_stream
        else:
            raise

    camp_kt_id = _to_int(raw_stream.get("campaign_id")) or keitaro_campaign_id
    campaign_obj = _get_or_create_local_campaign(db, camp_kt_id)

    stream_obj, _ = _persist_stream_snapshot(
        db, raw_stream, campaign_obj, fallback_stream_id=keitaro_stream_id
    )

    db.commit()
    db.refresh(stream_obj)
    return stream_obj