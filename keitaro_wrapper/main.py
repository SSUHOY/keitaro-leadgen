"""FastAPI-обёртка над трекером Keitaro.

Сервис забирает кампании и потоки из Admin API Keitaro, складывает их
в локальную БД и отдаёт наружу через REST API.

Веб-интерфейс (Tailwind) — ``GET /`` (файл ``static/index.html``),
статика — ``/static/<файл>``.

Запуск: ``uvicorn main:app --reload`` из каталога ``keitaro_wrapper``.
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

# Каталог со статикой (index.html) — путь не зависит от текущего рабочего каталога
STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("keitaro_wrapper")


class StepTimer:
    """Собирает длительности этапов запроса в миллисекундах."""

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
    description="Обёртка над Admin API Keitaro: синхронизация кампаний и потоков",
    version="1.0.0",
    lifespan=lifespan,
)

# Раздача статики: /static/<файл> (например /static/index.html)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(KeitaroAPIError)
async def keitaro_error_handler(_: Any, exc: KeitaroAPIError) -> JSONResponse:
    """Любая ошибка Keitaro отдаётся клиенту как 502 Bad Gateway."""
    logger.error("Ошибка Keitaro: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def get_keitaro_api() -> KeitaroAPI:
    """FastAPI-зависимость: клиент Keitaro, настроенный из .env."""
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
    """Собрать шаблон LIKE для поиска: спецсимволы (``%``, ``_``, ``\\``) экранируются."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped[:limit]}%"


def sync_campaigns(db: Session, api: KeitaroAPI) -> SyncResult:
    """Забрать кампании и их потоки из Keitaro и сохранить в локальную БД."""
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
            # потоки одной кампании не должны ломать всю синхронизацию
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

            stream = db.scalar(
                select(Stream).where(
                    Stream.campaign_id == campaign.id,
                    Stream.keitaro_id == stream_keitaro_id,
                )
            )
            if stream is None:
                stream = Stream(
                    campaign_id=campaign.id,
                    keitaro_id=stream_keitaro_id,
                    name=f"Stream {stream_keitaro_id}",
                )
                db.add(stream)
                stats["streams_created"] += 1
            else:
                stats["streams_updated"] += 1

            stream.name = _to_str(stream_payload.get("name")) or f"Stream {stream_keitaro_id}"
            stream.type = _to_str(stream_payload.get("type"), 64)
            stream.action_type = _to_str(stream_payload.get("action_type"), 64)
            stream.state = _to_str(stream_payload.get("state"), 32)
            stream.position = _to_int(stream_payload.get("position"))
            stream.raw = stream_payload
            stream.synced_at = utcnow()

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
    """Отдать веб-интерфейс (static/index.html)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get(
    "/health",
    response_model=HealthOut,
    tags=["service"],
    summary="Проверка живости сервиса и БД",
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
    summary="Проверка доступности Keitaro и API-ключа",
)
def keitaro_ping(api: KeitaroAPI = Depends(get_keitaro_api)) -> KeitaroPingOut:
    api.ping()
    return KeitaroPingOut(status="ok", base_url=api.base_url)


@app.post(
    "/campaigns/sync",
    response_model=SyncResult,
    tags=["campaigns"],
    summary="Синхронизировать кампании и потоки из Keitaro",
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
    summary="Список кампаний из локальной БД",
)
def list_campaigns(
    search: str | None = Query(
        default=None,
        min_length=1,
        max_length=255,
        description="Поиск по названию (частичное совпадение) или по ID кампании в Keitaro",
    ),
    state: str | None = Query(default=None, description="Фильтр по состоянию, например active"),
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
    summary="Кампания по её id в Keitaro",
)
def get_campaign(keitaro_id: int, db: Session = Depends(get_db)) -> Campaign:
    return _get_campaign_or_404(db, keitaro_id)


@app.get(
    "/campaigns/{keitaro_id}/streams",
    response_model=list[StreamOut],
    tags=["campaigns"],
    summary="Потоки кампании",
)
def list_campaign_streams(
    keitaro_id: int,
    db: Session = Depends(get_db),
) -> list[Stream]:
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
    summary="Получить список офферов для автокомплита",
)
def api_get_offers(api: KeitaroAPI = Depends(get_keitaro_api)):
    try:
        return api.get_offers()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось получить офферы: {e}")


# Допустимые типы групп в Admin API Keitaro
GROUP_TYPES = ("campaigns", "offers", "landings", "domains")


@app.get(
    "/api/groups",
    tags=["creators"],
    summary="Список групп Keitaro по типу (для селекторов)",
)
def api_get_groups(
    type_name: str = Query(
        default="campaigns",
        alias="type",
        description="Тип групп: campaigns, offers, landings или domains",
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
    summary="Создать группу в Keitaro",
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
    summary="Список доменов Keitaro (для селекторов)",
)
def api_get_domains(api: KeitaroAPI = Depends(get_keitaro_api)):
    try:
        return api.get_domains()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось получить домены: {e}")


@app.post(
    "/api/domains",
    tags=["creators"],
    summary="Создать домен в Keitaro",
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
    summary="Создать кампанию с двумя потоками (Создаватор из ТЗ)",
)
def create_campaign_wrapper(
    payload: CampaignCreateRequest,
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    """
    Создает кампанию в Keitaro:
1. Кампания с именем, группой, источником, доменом.
2. Поток 1: фильтр по стране (Geo) -> google.com.
3. Поток 2: основной поток с выбранным оффером.
    После создания автоматически запускает синхронизацию этой кампании в локальную БД.
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

        # Сразу синхронизируем изменения локально
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
    summary="Обновить офферы потока кампании (Редактор)",
)
def manage_stream_offer(
    keitaro_id: int,
    stream_id: int,
    payload: StreamOffersUpdate | OfferUpdateAction,
    db: Session = Depends(get_db),
    api: KeitaroAPI = Depends(get_keitaro_api),
):
    """
    Управляет офферами в потоке существующей кампании. Поддерживаются два формата:

- ``{"offers": [{"offer_id": 1, "share": 100, "state": "active"}]}`` —
      «PUSH TO KT»: полный набор офферов потока (доли трафика + удаление);
- ``{"offer_id": 1, "action": "add" | "remove", "share": 100}`` —
      точечная операция над одним оффером.
    """
    timer = StepTimer("manage_stream_offer")

    try:
        with timer.measure("db_lookup_campaign"):
            campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))

        if campaign is None:
            with timer.measure("kt_get_campaign"):
                api.get_campaign(keitaro_id)
            with timer.measure("db_sync_campaigns"):
                sync_campaigns(db, api)
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
    """Обновить в локальной БД только один поток по его ID в Keitaro."""
    try:
        raw_stream = api.get_stream(keitaro_stream_id)
    except KeitaroAPIError:
        # Если PUT уже прошёл, а GET почему-то вернул ошибку — берём словарь из ответа PUT
        if isinstance(fallback_stream, dict) and fallback_stream:
            raw_stream = fallback_stream
        else:
            raise

    stream_kt_id = _to_int(raw_stream.get("id")) or keitaro_stream_id
    camp_kt_id = _to_int(raw_stream.get("campaign_id")) or keitaro_campaign_id
    now = utcnow()

    stream_obj = db.scalar(select(Stream).where(Stream.keitaro_id == stream_kt_id))
    if stream_obj is None:
        stream_obj = Stream(
            keitaro_id=stream_kt_id,
            campaign_id=camp_kt_id,
            name=_to_str(raw_stream.get("name")) or f"Stream #{stream_kt_id}",
            created_at=parse_keitaro_datetime(raw_stream.get("created_at")) or now,
        )
        db.add(stream_obj)

    stream_obj.campaign_id = camp_kt_id
    stream_obj.name = _to_str(raw_stream.get("name")) or stream_obj.name
    stream_obj.type = _to_str(raw_stream.get("type"))
    stream_obj.action_type = _to_str(raw_stream.get("action_type"))
    stream_obj.state = _to_str(raw_stream.get("state"))
    stream_obj.position = _to_int(raw_stream.get("position"))
    stream_obj.raw = raw_stream
    stream_obj.synced_at = now

    campaign_obj = db.scalar(select(Campaign).where(Campaign.keitaro_id == camp_kt_id))
    if campaign_obj is not None:
        campaign_obj.synced_at = now

    db.commit()
    db.refresh(stream_obj)
    return stream_obj