import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.config import get_cached_config
from app.database.db import db
from app.services.bot_notify_service import run_bot_notify_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Эта функция запускается вместе с запуском fastapi,
    корректно открывает и закрывает подключение к бд, поднимает и
    останавливает воркер доставки уведомлений в боты
    (app/services/bot_notify_service.py)"""
    config = get_cached_config()
    app.state.config = config
    await db.init(config.db.url)
    await db.create_tables()

    bot_notify_task = asyncio.create_task(run_bot_notify_loop())

    yield

    bot_notify_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await bot_notify_task
    await db.close()
