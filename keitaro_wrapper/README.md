# keitaro_wrapper

Мини-сервис (FastAPI) — обёртка над Admin API трекера Keitaro: забирает кампании
и их потоки (streams) в локальную БД (SQLAlchemy + SQLite по умолчанию)
и отдаёт их через REST API.

## Структура

```
keitaro_wrapper/
├── services/
│   ├── __init__.py
│   └── keitaro_api.py      # клиент Admin API Keitaro (requests, заголовок Api-Key)
├── static/
│   └── index.html          # веб-интерфейс на Tailwind (создаватель + редактор)
├── __init__.py
├── .env                    # настройки подключения (URL, API-ключ, БД)
├── database.py             # engine, SessionLocal, Base, get_db(), init_db()
├── models.py               # ORM-модели Campaign и Stream
├── schemas.py              # Pydantic-схемы ответов
├── main.py                 # FastAPI-приложение и эндпоинты
├── requirements.txt
└── README.md
```

## Установка и запуск

```powershell
cd keitaro_wrapper
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# заполнить .env: KEITARO_BASE_URL и KEITARO_API_KEY
uvicorn main:app --reload
```

Swagger UI: http://127.0.0.1:8000/docs
Веб-интерфейс: http://127.0.0.1:8000/ (отдаётся `static/index.html`)

## Веб-интерфейс (Tailwind)

`static/index.html` — одна страница без сборки, стили подключены через
Tailwind Play CDN (`https://cdn.tailwindcss.com`). На странице:

* бейдж состояния (`GET /health`) и ссылка на Swagger;
* форма «Создаватель» — `POST /campaigns/create-wrapper`
  (название, гео, оффер + подсказки из `GET /api/offers`; группы и домены
  выбираются из селекторов, которые заполняются `GET /api/groups?type=campaigns`
  и `GET /api/domains`),
* блок «Справочники» — списки групп (с выбором типа: campaigns/offers/landings/domains)
  и доменов из `GET /api/groups` и `GET /api/domains`, создание новых
  (`POST /api/groups`, `POST /api/domains`);
* форма «Редактор» — `POST /campaigns/{keitaro_id}/streams/{stream_id}/offers`
  (тело `{"offers": [{"offer_id": 18813, "share": 100, "state": "active"}]}` —
  «PUSH TO KT» отправляет полный список офферов потока с долями; также
  поддерживается точечный формат `{"offer_id": 18813, "action": "add"|"remove"}`.
  Существующие офферы потока отдаёт `GET /campaigns/{keitaro_id}/streams`
  в поле `offers`);
* таблица кампаний из локальной БД (`GET /campaigns`, поиск по названию или ID
  через `?search=`, подсказка «Найдено: N») с кнопками
  «Обновить список», «Синхронизировать с Keitaro» (`POST /campaigns/sync`)
  и «Показать потоки» (`GET /campaigns/{keitaro_id}/streams`);
* все кнопки страницы блокируются (`disabled`) на время активных запросов к API,
  чтобы исключить повторные клики; поиск в таблице кампаний отправляется
  с задержкой 300 мс (debounce) и по нажатию Enter.

Как подключено в `main.py`:

```python
STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
def read_root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
```

## Эндпоинты

| Метод | Путь                          | Назначение                                          |
|-------|-------------------------------|-----------------------------------------------------|
| GET   | `/`                           | веб-интерфейс (`static/index.html`)                  |
| GET   | `/static/{file}`              | статика (css/js/изображения, если появятся)          |
| GET   | `/health`                     | живость сервиса, БД и наличие настроек Keitaro       |
| GET   | `/keitaro/ping`               | проверка доступности Keitaro и API-ключа             |
| GET   | `/api/offers`                 | список офферов для выбора                            |
| GET   | `/api/groups`                 | список групп Keitaro (`?type=campaigns\|offers\|landings\|domains`) |
| POST  | `/api/groups`                 | создать группу Keitaro (`name`, `type`, `position`)  |
| GET   | `/api/domains`                | список доменов Keitaro                               |
| POST  | `/api/domains`                | создать домен Keitaro (`name`, `group_id`, `notes`)  |
| POST  | `/campaigns/sync`             | синхронизация кампаний и потоков из Keitaro в БД     |
| POST  | `/campaigns/create-wrapper`   | создание кампании с двумя потоками (создаватель)     |
| POST  | `/campaigns/{keitaro_id}/streams/{stream_id}/offers` | офферы потока: полный набор `{"offers": [...]}` или точечно `{"offer_id", "action"}` |
| GET   | `/campaigns`                  | кампании из БД (`search`, `state`, `limit`, `offset`) |
| GET   | `/campaigns/{keitaro_id}`     | одна кампания                                        |
| GET   | `/campaigns/{keitaro_id}/streams` | потоки кампании                                 |

## Настройки (.env)

| Переменная          | Описание                                            |
|---------------------|-----------------------------------------------------|
| `KEITARO_BASE_URL`  | URL трекера без `/admin_api/v1`                     |
| `KEITARO_API_KEY`   | API-ключ из настроек Keitaro                        |
| `KEITARO_TIMEOUT`   | таймаут запросов, секунды (по умолчанию 30)         |
| `DATABASE_URL`      | строка подключения SQLAlchemy, по умолчанию SQLite  |

## Обработка ошибок

Любая ошибка обращения к Keitaro (`KeitaroAPIError`) возвращается клиенту
с кодом `502 Bad Gateway` и текстом причины в поле `detail`.