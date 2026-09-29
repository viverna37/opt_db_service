"""
Вызовы Bot API от имени бота оптовика при его подключении к платформе:
проверить токен (getMe) и повесить на бота кнопку меню, открывающую каталог
(setChatMenuButton) — чтобы владельцу платформы не ходить в BotFather руками.
"""
import httpx

from app.config.config import get_cached_config

API_TIMEOUT_SEC = 10.0


class BotTokenError(Exception):
    pass


async def _call(token: str, method: str, payload: dict | None = None) -> dict:
    base = get_cached_config().telegram.api_base_url
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SEC) as client:
            response = await client.post(f"{base}/bot{token}/{method}", json=payload or {})
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BotTokenError(f"Telegram недоступен: {exc}") from exc
    if not data.get("ok"):
        raise BotTokenError(data.get("description") or "Telegram отклонил запрос")
    return data["result"]


async def get_bot_username(token: str) -> str:
    """Проверка токена: вернёт @username бота без «@» или BotTokenError"""
    result = await _call(token, "getMe")
    return result["username"]


async def set_catalog_menu_button(token: str, url: str, text: str = "Каталог") -> None:
    """Кнопка меню бота (слева от поля ввода) открывает мини-апп каталога"""
    await _call(token, "setChatMenuButton", {"menu_button": {"type": "web_app", "text": text, "web_app": {"url": url}}})
