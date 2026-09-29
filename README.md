# opt_catalog_db_service

API «ОптКаталога» — Telegram Mini App каталога для оптовиков. FastAPI +
SQLAlchemy 2 (async) + Postgres, мультитенантный. Контекст и правила —
`CLAUDE.md`, ТЗ — `opt_promt.md`.

## Запуск

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp app/.env.example app/.env          # заполнить POSTGRES_*, для браузера DEV_AUTH=true
.venv/bin/python -m app.cli seed-demo --owner-telegram-id <ваш telegram id>
.venv/bin/uvicorn app.main:app --reload --port 9002
```

Docker (как такси — в общей сети `backend_net` с контейнером `postgres`):

```bash
docker compose up -d --build
docker compose exec opt_catalog_db_service python -m app.cli seed-demo --owner-telegram-id <id>
```

Свежий Postgres: `pg_trgm` сервис включит сам (`CREATE EXTENSION IF NOT EXISTS`),
пользователю БД нужны на это права.

Тесты: `.venv/bin/pytest` (файловая sqlite на каждый тест, Postgres не нужен).

## CLI

```bash
python -m app.cli create-tenant --slug amigo --name "Amigo Opt" --bot-token 123:ABC --bot-username amigo_opt_bot
python -m app.cli set-bot       --slug amigo --bot-token 123:ABC --bot-username amigo_opt_bot
python -m app.cli set-owner     --slug amigo --telegram-id 123456789
python -m app.cli seed-demo     [--slug demo] [--owner-telegram-id 123] [--bot-token ...]
```

## Авторизация (для фронта и бота)

Каждый запрос (кроме публичных):

| Заголовок | Значение |
|---|---|
| `X-Tenant` | slug оптовика (мини-апп открыт по `/t/{slug}`) |
| `X-Init-Data` | сырая `window.Telegram.WebApp.initData` |
| `X-Tg-User-Id` | только при `DEV_AUTH=true` — вместо initData, для браузера |

`GET /v1/me` → `access`: `ok` | `age_required` (экран 18+ → `POST /v1/me/age-confirm`) |
`pending` (ожидайте подтверждения) | `blocked`; `is_staff` — показывать ли админку.
Ошибки: `{"detail": {"code": "...", "message": "..."}}`.

## API

Деньги — копейки (int). Полная схема — `/docs`.

**Публичное**: `GET /v1/tenants/{slug}/public` (брендинг), `GET /v1/files/{key}` (картинки).

**Клиент**
- `GET /v1/catalog/categories` — дерево с числом товаров
- `GET /v1/catalog/categories/{id}` — крошки, подкатегории, фильтры (`filterable` атрибуты со значениями)
- `GET /v1/catalog/products?category_id&q&brand_id&in_stock&sort=default|name|price_asc|price_desc|new&limit&offset&attr.<key>=v1,v2`
- `GET /v1/catalog/products/{id}` — карточка: уровни цен, `current_tier_id` по корзине, варианты с `in_cart_qty`
- `GET /v1/cart`, `PUT /v1/cart/items/{variant_id}` `{qty}` (итоговое число, 0 — убрать), `DELETE /v1/cart`
- `POST /v1/cart/submit` `{comment}` — «Отправить менеджеру»; 409 с `code` из `blockers`
- `GET /v1/orders`, `GET /v1/orders/{id}`, `POST /v1/orders/{id}/repeat`

**Админка** (`/v1/admin`, owner/admin/manager; справочники и настройки — owner/admin)
- `GET /summary`; `GET /orders?status&number&date_from&date_to`, `GET /orders/{id}`,
  `PATCH /orders/{id}/status`, `PATCH /orders/{id}/note`, `GET /orders/{id}/text`,
  `GET /orders/{id}/export.xlsx`, `GET /orders/export.xlsx`
- `GET /carts`, `GET /carts/{id}`, `POST /carts/{id}/remind` (раз в сутки, иначе 429)
- `GET /clients?q&status&role`, `PATCH /clients/{id}` `{note, status, role}`
- `GET|POST /products`, `GET|PATCH|DELETE /products/{id}`, `POST /products/{id}/stock`,
  `PUT /products/{id}/prices`, `POST|PATCH|DELETE /products/{id}/variants[/{vid}]`,
  `POST /products/{id}/photos`, `DELETE /products/{id}/photos/{pid}`, `PUT /products/{id}/photos/order`
- `categories` (+ `/{id}/attributes`, `/{id}/image`), `attributes`, `brands`, `price-tiers` — CRUD
- `GET|PATCH /settings`, `POST /settings/logo`, `GET /audit`

**Бот-сервис**: `GET /v1/service/tenants` (`X-Service-Key`) — тенанты с токенами для вебхуков.

## Прод

Сервер `201.34.158.94`, домен `https://opt.bozdyrevdev.ru` (фронт), API — `/api`.
Всё в `/opt/opt_catalog`: `docker-compose.yml` (Postgres 16 + API + Caddy с HTTPS),
`Caddyfile`, `.env` (пароли, создан `deploy/server-init.sh`, в git не хранится).

```bash
# первичная настройка (один раз)
ssh root@201.34.158.94 'bash -s' < deploy/server-init.sh opt.bozdyrevdev.ru <telegram id админов>
# выкатка (сборка фронта + rsync + docker compose up --build)
DEPLOY_HOST=root@201.34.158.94 deploy/deploy.sh
# CLI на проде
ssh root@201.34.158.94 'cd /opt/opt_catalog && docker compose exec api python -m app.cli ...'
```

## Платформа (владелец сервиса)

Оптовики заводятся в мини-аппе, раздел `/platform`: список, создание (slug, название,
токен бота, Telegram id владельца), смена бота/владельца, отключение. Доступ — только
Telegram id из `PLATFORM_ADMIN_IDS` в `.env`. Токен бота проверяется через `getMe`,
кнопка меню бота ставится на каталог автоматически (нужен `WEBAPP_BASE_URL`).
Вход: «Настройки → Платформа» в любом каталоге, где вы сотрудник, иконка в шапке
каталога или отдельный бот платформы (`PLATFORM_BOT_TOKEN`, кнопка меню на `/platform`).
