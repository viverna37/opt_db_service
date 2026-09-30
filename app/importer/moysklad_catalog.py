"""
Обогащение табличного прайса из публичного B2B-каталога МойСклада
(ссылка https://b2b.moysklad.ru/public/{id}/catalog в шапке прайса):
остатки на складе и фото. Позиции сопоставляются по полному названию —
оно в прайсе и в каталоге одинаковое («Набор … Kit (Carbon Black)»).

API публичной витрины (то же, что использует её фронт):
- GET /desktop-api/public/{id}/products.json?limit&offset — позиции со
  stock/available и миниатюрой 150px;
- GET /desktop-api/public/{id}/product/{itemId} — то же + imageURLOriginal
  (подписанная ссылка на оригинал, живёт ~60 с — качаем сразу).
"""
import asyncio
import logging

import httpx

from app.importer.block_price import ParseResult
from app.utils.text import normalize_name

logger = logging.getLogger(__name__)

BASE = "https://b2b.moysklad.ru/desktop-api/public"
PAGE = 100
MAX_PHOTOS = 5
CONCURRENCY = 4


async def fetch_catalog(client: httpx.AsyncClient, public_id: str) -> dict[str, dict]:
    items: dict[str, dict] = {}
    offset = 0
    while True:
        r = await client.get(f"{BASE}/{public_id}/products.json", params={"limit": PAGE, "offset": offset})
        r.raise_for_status()
        data = r.json()
        for item in data.get("products", []):
            items.setdefault(normalize_name(item["name"]), item)
        offset += PAGE
        if offset >= data.get("size", 0) or not data.get("products"):
            return items


async def _original_image(client: httpx.AsyncClient, public_id: str, item_id: str, sem: asyncio.Semaphore) -> bytes | None:
    """Карточка -> подписанная ссылка на оригинал -> картинка. Витрина иногда рвёт соединение — 3 попытки."""
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(f"{BASE}/{public_id}/product/{item_id}")
                r.raise_for_status()
                detail = r.json()
                url = detail.get("imageURLOriginal") or detail.get("imageURLMiniature")
                if not url:
                    return None
                img = await client.get(url)  # подписанная ссылка живёт ~60 с — сразу же
                img.raise_for_status()
                return img.content
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    logger.warning("Фото %s не скачалось: %s", item_id, exc)
                    return None
                await asyncio.sleep(1 + attempt)
    return None


async def enrich_from_moysklad(parsed: ParseResult, public_id: str, with_photos: bool = True) -> dict:
    """Проставляет stock_qty (и out, если позиция недоступна) и фото. Возвращает статистику."""
    stats = {"matched": 0, "unmatched": 0, "photos": 0}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        catalog = await fetch_catalog(client, public_id)
        photo_jobs: list[tuple[object, list[str]]] = []
        for product in parsed.products:
            units = product.variants or [product]
            item_ids: list[str] = []
            for unit in units:
                item = catalog.get(normalize_name(unit.source_name or ""))
                if item is None:
                    stats["unmatched"] += 1
                    continue
                stats["matched"] += 1
                stock = int(item.get("stock") or 0)
                unit.stock_qty = stock if item.get("available", True) else 0
                if item.get("imageURLMiniature") and item["id"] not in item_ids:
                    item_ids.append(item["id"])
            if with_photos and item_ids:
                photo_jobs.append((product, item_ids[:MAX_PHOTOS]))

        async def load(product, ids):
            images = await asyncio.gather(*[_original_image(client, public_id, i, sem) for i in ids])
            images = [img for img in images if img]
            if images:
                product.image, product.images = images[0], images[1:]
                stats["photos"] += len(images)

        await asyncio.gather(*[load(p, ids) for p, ids in photo_jobs])
    return stats
