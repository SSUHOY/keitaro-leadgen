# keitaro_wrapper

Тестовое задание: сервис на **FastAPI** поверх Admin API трекера **Keitaro**.

* **Создаватель кампаний** — создаёт кампанию по шаблону: geo-поток (редирект на Google) + поток с оффером.
* **Редактор офферов** — добавляет/удаляет офферы в потоках существующих кампаний, меняет доли.
* Локальный кеш — **SQLite** (SQLAlchemy 2.0), интерфейс — одна HTML-страница (`static/index.html`, Tailwind Play CDN, сборка не нужна).

---

## Требования

* Python 3.10+ (проверено на 3.11+)
* Доступный инстанс Keitaro и API-ключ к нему

---

## Запуск

### Windows (PowerShell)

```powershell
git clone https://github.com/SSUHOY/keitaro-leadgen.git
cd keitaro-leadgen\keitaro_wrapper

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

Copy-Item .env.example .env

uvicorn main:app --reload
```

### Linux / macOS

```bash
git clone https://github.com/SSUHOY/keitaro-leadgen.git
cd keitaro-leadgen/keitaro_wrapper

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env

uvicorn main:app --reload
```

> Запускать `uvicorn` нужно **из каталога `keitaro_wrapper`**: импорты в проекте плоские
> (`from database import ...`), а `DATABASE_URL=sqlite:///./keitaro.db` создаёт файл БД относительно текущего каталога.

После старта:

* `http://127.0.0.1:8000/` — веб-интерфейс
* `http://127.0.0.1:8000/docs` — Swagger UI
* `http://127.0.0.1:8000/health` — живость сервиса и настройки Keitaro

---

## Настройки (`.env`)

Скопируйте `keitaro_wrapper/.env.example` в `keitaro_wrapper/.env` и заполните:

| Переменная | Обязательна | Описание |
|---|---|---|
| `KEITARO_BASE_URL` | да | URL трекера **без** `/admin_api/v1`, например `https://tracker.example.com` |
| `KEITARO_API_KEY` | да | API-ключ из админки Keitaro (Администрирование → API) |
| `KEITARO_TIMEOUT` | нет | Таймаут HTTP-запросов к Keitaro, секунды (по умолчанию `30`) |
| `DATABASE_URL` | нет | Строка подключения SQLAlchemy (по умолчанию `sqlite:///./keitaro.db`) |

`.env` добавлен в `.gitignore` и в репозиторий не попадает.

---

## База данных и миграции

Alembic в проекте **не используется** — для двух таблиц (`campaigns`, `streams`) схема создаётся автоматически
при старте приложения: lifespan вызывает `init_db()` → `Base.metadata.create_all(bind=engine)`.

* **Первый запуск** — таблицы и файл `keitaro_wrapper/keitaro.db` создаются сами, дополнительных команд не нужно.
* **Сброс схемы и кеша** — остановить сервис, удалить БД, запустить снова:
  `Remove-Item keitaro.db` (PowerShell) или `rm keitaro.db` (bash).
* **Другая СУБД** — задать `DATABASE_URL`, например
  `postgresql+psycopg2://user:pass@localhost:5432/keitaro` (+ `pip install psycopg2-binary`); таблицы создаст тот же `create_all()`.
* **Изменение моделей** — `create_all()` не перестраивает существующие таблицы: пересоздайте БД либо подключите Alembic (`alembic init` + правка `env.py`).

Кеш наполняется по требованию: при создании кампании (`POST /campaigns/create-wrapper`) и при чтении потоков живьём
(`GET /campaigns/{keitaro_id}/streams?source=keitaro`).

---

## Основные эндпоинты

Полный список — в Swagger UI (`/docs`).

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | веб-интерфейс (`static/index.html`) |
| GET | `/health` | живость сервиса, доступность БД, заданы ли настройки Keitaro |
| GET | `/keitaro/ping` | проверка доступности Keitaro и API-ключа |
| GET | `/api/offers` | список офферов (заполняет datalist в UI) |
| GET | `/api/groups?type=campaigns` | группы Keitaro (`campaigns\|offers\|landings\|domains`) |
| GET | `/api/domains` | домены Keitaro |
| GET | `/api/sources` | источники трафика Keitaro |
| POST | `/campaigns/create-wrapper` | **создаватель**: кампания + geo-поток на Google + поток с оффером |
| GET | `/campaigns/{keitaro_id}/streams` | потоки кампании: `?source=local` (кеш) или `?source=keitaro` (живые данные) |
| POST | `/campaigns/{keitaro_id}/streams/{stream_id}/offers` | **редактор**: полный набор офферов `{"offers": [...]}` или точечно `{"offer_id", "action"}` |

Примеры запросов:

```bash
# Создание кампании
curl -X POST http://127.0.0.1:8000/campaigns/create-wrapper \
  -H "Content-Type: application/json" \
  -d '{"name": "My Campaign", "geo": "AU", "offer_id": 18813,
       "group_id": 9799, "domain_id": 1813, "source_id": 836}'

# Публикация офферов в потоке ("PUSH TO KT")
curl -X POST http://127.0.0.1:8000/campaigns/216386/streams/1330428/offers \
  -H "Content-Type: application/json" \
  -d '{"offers": [{"offer_id": 18813, "share": 70, "state": "active"},
                  {"offer_id": 12992, "share": 30, "state": "active"}]}'
```
