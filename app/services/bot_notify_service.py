"""
Фоновый воркер доставки очереди Notification в чаты ботов — как в такси:
asyncio-задача из app/lifespan.py, один процесс, одна транзакция на
уведомление, дублей нет по конструкции.

Отличие от такси — ботов много: у каждого тенанта свой bot_token, поэтому
сообщение уходит через бота того оптовика, к которому относится.
"""
import asyncio
import logging

import httpx

from app.config.config import get_cached_config
from app.database.db import db
from app.database.repository.main_repository import Repository

logger = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 5
API_TIMEOUT_SEC = 10.0
NOTIFICATIONS_BATCH_LIMIT = 100


async def _send_message(client: httpx.AsyncClient, api_base_url: str, bot_token: str, chat_id: int, text: str,
                        reply_markup: dict | None) -> None:
    # У личных чатов chat_id совпадает с telegram user id
    body: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    response = await client.post(f"{api_base_url}/bot{bot_token}/sendMessage", json=body)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage вернул ошибку: {data}")


async def _delivery_tick(client: httpx.AsyncClient) -> None:
    api_base_url = get_cached_config().telegram.api_base_url
    async with db.session() as session:
        repo = Repository(session)
        for notification in await repo.notification.list_unsent(limit=NOTIFICATIONS_BATCH_LIMIT):
            tenant = await repo.tenant.get_by_id(notification.tenant_id)
            user = await repo.tg_user.get_by_id(notification.tg_user_id)
            if tenant is None or user is None or not tenant.bot_token:
                logger.warning("Notification id=%s: нет тенанта/пользователя/бота — пропускаем", notification.id)
            else:
                try:
                    await _send_message(client, api_base_url, tenant.bot_token, user.telegram_id,
                                        notification.text, notification.reply_markup)
                except Exception:
                    # Заблокировал бота, чат не найден и т.п. — логируем и всё равно помечаем
                    # отправленным, иначе воркер вечно долбил бы одно и то же.
                    logger.exception("Не удалось доставить Notification id=%s telegram_id=%s",
                                     notification.id, user.telegram_id)
            await repo.notification.mark_sent(notification.id)
            await repo.commit()


async def run_bot_notify_loop() -> None:
    """Бесконечный цикл — вызывается один раз из lifespan, живёт всё время приложения"""
    logger.info("Воркер доставки уведомлений запущен, тик каждые %s сек", TICK_INTERVAL_SEC)
    async with httpx.AsyncClient(timeout=API_TIMEOUT_SEC) as client:
        while True:
            try:
                await _delivery_tick(client)
            except Exception:
                logger.exception("Ошибка тика доставки уведомлений")
            await asyncio.sleep(TICK_INTERVAL_SEC)
