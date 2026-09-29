"""Назовем это диспатчером из айограм,он помогает нам пропихивать всякое,в нашем случае сервисы в энд поинты"""
import hmac
import json
import logging
from datetime import timedelta
from typing import Any, AsyncGenerator, Optional

from fastapi import Depends, Header, HTTPException, status

from app.config.config import Config, get_cached_config
from app.database.db import db
from app.database.models import AccessMode, MemberStatus, Role, STAFF_ROLES, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.utils.dt import as_aware_utc, utcnow
from app.utils.init_data import InitDataError, validate_webapp_init_data

logger = logging.getLogger(__name__)

# last_seen пишем не на каждый запрос, а не чаще раза в минуту
LAST_SEEN_THROTTLE = timedelta(seconds=60)


def api_error(http_status: int, code: str, message: str) -> HTTPException:
    """Ошибка с машинным кодом — фронт по code решает, какой экран показать (pending, age_required...)"""
    return HTTPException(http_status, detail={"code": code, "message": message})


# ---------- DB ----------
async def get_session() -> AsyncGenerator[Any, Any]:
    async with db.session() as session:
        yield session


def get_repository(session=Depends(get_session)) -> Repository:
    return Repository(session)


def get_config() -> Config:
    return get_cached_config()


# ---------- Тенант ----------
async def get_tenant(
    x_tenant: str = Header(..., alias="X-Tenant"),
    repo: Repository = Depends(get_repository),
) -> Tenant:
    """Тенант — по slug из заголовка X-Tenant (мини-апп открыт по /t/{slug} и шлёт его в каждом запросе)"""
    tenant = await repo.tenant.get_by_slug(x_tenant)
    if not tenant or not tenant.is_active:
        raise api_error(status.HTTP_404_NOT_FOUND, "tenant_not_found", "Каталог не найден")
    return tenant


# ---------- Идентификация ----------
def _validate_account(init_data: str, bot_token: str, config: Config) -> dict:
    """Проверенная подписью initData -> аккаунт Telegram (id, имя, username, фото)"""
    fields = validate_webapp_init_data(init_data, bot_token, config.auth.init_data_max_age_sec)
    account = json.loads(fields["user"])
    account["id"] = int(account["id"])
    return account


def _account_from_headers(
    tenant: Tenant, x_init_data: Optional[str], x_tg_user_id: Optional[int], config: Config,
) -> dict:
    """
    Два режима:
    - X-Init-Data — настоящая initData из Telegram.WebApp, подпись проверяется
      bot token'ом ЭТОГО тенанта (initData от бота другого оптовика не пройдёт).
    - DEV_AUTH=true в конфиге — X-Tg-User-Id без проверки, для работы в
      браузере. На проде выключен: в отличие от такси, dev-режим не
      включается сам по отсутствию токена, только явным флагом.
    """
    if x_init_data:
        if not tenant.bot_token:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "bot_not_configured", "У каталога не настроен бот")
        try:
            return _validate_account(x_init_data, tenant.bot_token, config)
        except (InitDataError, KeyError, ValueError, TypeError) as exc:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_init_data", f"Невалидная initData: {exc}")
    if config.auth.dev_auth and x_tg_user_id:
        logger.warning("dev-авторизация без проверки подписи: tenant=%s tg_user_id=%s", tenant.slug, x_tg_user_id)
        return {"id": x_tg_user_id, "first_name": f"Dev {x_tg_user_id}"}
    raise api_error(status.HTTP_401_UNAUTHORIZED, "auth_required", "Нужен заголовок X-Init-Data")


async def get_member(
    tenant: Tenant = Depends(get_tenant),
    x_init_data: Optional[str] = Header(None, alias="X-Init-Data"),
    x_tg_user_id: Optional[int] = Header(None, alias="X-Tg-User-Id"),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
) -> TenantUser:
    """
    Регистрации нет: TgUser и TenantUser создаются/обновляются при каждом
    входе. Новый участник в режиме approval — pending, иначе active.
    Здесь НЕ отсекаются blocked/pending — /v1/me должен отдать им статус,
    чтобы фронт показал нужный экран. Отсекает get_client.
    """
    account = _account_from_headers(tenant, x_init_data, x_tg_user_id, config)
    tg_user = await repo.tg_user.upsert(account["id"], account)
    member = await repo.tenant_user.get_by_tg_user(tenant.id, tg_user.id)
    now = utcnow()
    if member is None:
        initial_status = MemberStatus.pending if tenant.access_mode == AccessMode.approval else MemberStatus.active
        member = await repo.tenant_user.create(tenant.id, tg_user.id, Role.client, initial_status)
    elif now - as_aware_utc(member.last_seen) > LAST_SEEN_THROTTLE:
        member.last_seen = now
    await repo.commit()
    return member


def is_staff(member: TenantUser) -> bool:
    return member.role in STAFF_ROLES


async def get_client(
    member: TenantUser = Depends(get_member),
    tenant: Tenant = Depends(get_tenant),
) -> TenantUser:
    """Доступ к товарам, ценам, корзине и заявкам. Сотрудники проходят без 18+ и одобрения."""
    if member.status == MemberStatus.blocked:
        raise api_error(status.HTTP_403_FORBIDDEN, "blocked", "Доступ к каталогу закрыт")
    if is_staff(member):
        return member
    if member.status == MemberStatus.pending:
        raise api_error(status.HTTP_403_FORBIDDEN, "pending", "Ожидайте подтверждения менеджера")
    if tenant.age_gate and member.age_confirmed_at is None:
        raise api_error(status.HTTP_403_FORBIDDEN, "age_required", "Подтвердите, что вам есть 18 лет")
    return member


async def get_staff(member: TenantUser = Depends(get_member)) -> TenantUser:
    """Админка: owner / admin / manager"""
    if member.status == MemberStatus.blocked or not is_staff(member):
        raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Нет доступа к админке")
    return member


async def get_admin(member: TenantUser = Depends(get_staff)) -> TenantUser:
    """Настройки каталога (категории, атрибуты, уровни цен, брендинг, режим доступа): owner / admin"""
    if member.role not in (Role.owner, Role.admin):
        raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Действие доступно только администратору")
    return member


# ---------- Владелец платформы ----------
async def get_platform_admin(
    x_tenant: Optional[str] = Header(None, alias="X-Tenant"),
    x_init_data: Optional[str] = Header(None, alias="X-Init-Data"),
    x_tg_user_id: Optional[int] = Header(None, alias="X-Tg-User-Id"),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
) -> int:
    """
    Суперадмин платформы — Telegram id из PLATFORM_ADMIN_IDS (только конфиг).
    initData принимается от «бота платформы» (PLATFORM_BOT_TOKEN) или, если
    раздел открыт из каталога оптовика (X-Tenant), — от бота этого оптовика.
    Возвращает telegram_id.
    """
    account: dict | None = None
    if x_init_data:
        tokens = [config.platform.bot_token] if config.platform.bot_token else []
        if x_tenant:
            tenant = await repo.tenant.get_by_slug(x_tenant)
            if tenant and tenant.bot_token:
                tokens.append(tenant.bot_token)
        for token in tokens:
            try:
                account = _validate_account(x_init_data, token, config)
                break
            except (InitDataError, KeyError, ValueError, TypeError):
                continue
        if account is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_init_data", "Невалидная initData")
    elif config.auth.dev_auth and x_tg_user_id:
        account = {"id": x_tg_user_id}
    else:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "auth_required", "Нужен заголовок X-Init-Data")

    if account["id"] not in config.platform.admin_ids:
        raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Раздел доступен только владельцу платформы")
    await repo.tg_user.upsert(account["id"], account)
    await repo.commit()
    return account["id"]


# ---------- Server-to-server ----------
async def require_service_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    config: Config = Depends(get_config),
) -> None:
    """
    Бот-сервис (один процесс на ботов всех тенантов) берёт отсюда список
    тенантов с токенами для вебхуков. В отличие от выпиленного в такси
    X-Admin-Id, тут есть секрет и сравнение constant-time; без
    SERVICE_API_KEY в конфиге ручки просто выключены.
    """
    if not config.service_api_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not x_service_key or not hmac.compare_digest(x_service_key, config.service_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный X-Service-Key")
