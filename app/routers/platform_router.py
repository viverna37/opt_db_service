"""
Раздел «Платформа» — владелец сервиса заводит оптовиков (тенантов),
подключает их ботов, назначает владельцев, отключает. Доступ — только
Telegram id из PLATFORM_ADMIN_IDS (app/deps.py::get_platform_admin).

Оптовики не удаляются — только отключаются (is_active=False): у них заявки,
клиенты и история. Отключённый каталог отдаёт 404 всем, включая его сотрудников.
"""
from fastapi import APIRouter, Depends, status

from app.config.config import Config
from app.database.models import Tenant
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_config, get_platform_admin, get_repository
from app.models.common_models import TgUserResponse
from app.models.platform_models import (
    PlatformMeResponse,
    PlatformTenantCreateRequest,
    PlatformTenantResponse,
    PlatformTenantSaved,
    PlatformTenantUpdateRequest,
)
from app.services.telegram_bot import BotTokenError, get_bot_username, set_catalog_menu_button
from app.services.tenant_service import make_owner, slug_problem

router = APIRouter(prefix="/v1/platform", tags=["platform"])


def _catalog_url(config: Config, slug: str) -> str | None:
    base = config.telegram.webapp_base_url
    return f"{base}/t/{slug}" if base else None


def _response(tenant: Tenant, stats: dict, config: Config, cls=PlatformTenantResponse, **extra):
    s = stats.get(tenant.id, {})
    owner = s.get("owner")
    return cls(
        id=tenant.id, slug=tenant.slug, name=tenant.name, is_active=tenant.is_active,
        bot_username=tenant.bot_username, bot_configured=bool(tenant.bot_token),
        owner=TgUserResponse.model_validate(owner) if owner else None,
        products_count=s.get("products_count", 0), clients_count=s.get("clients_count", 0),
        orders_count=s.get("orders_count", 0), last_order_at=s.get("last_order_at"),
        created_at=tenant.created_at, catalog_url=_catalog_url(config, tenant.slug),
        bot_url=f"https://t.me/{tenant.bot_username}" if tenant.bot_username else None,
        **extra,
    )


async def _connect_bot(token: str, slug: str, config: Config, warnings: list[str]) -> str:
    """Проверяет токен (getMe -> username) и вешает кнопку меню на каталог. Ошибка токена — 422"""
    try:
        username = await get_bot_username(token)
    except BotTokenError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_bot_token", f"Токен бота не подошёл: {exc}")
    url = _catalog_url(config, slug)
    if not url:
        warnings.append("Не задан WEBAPP_BASE_URL — кнопку меню бота настройте вручную в BotFather")
    else:
        try:
            await set_catalog_menu_button(token, url)
        except BotTokenError as exc:
            warnings.append(f"Кнопку меню бота поставить не удалось: {exc}")
    return username


async def _tenant(repo: Repository, slug: str) -> Tenant:
    tenant = await repo.tenant.get_by_slug(slug)
    if not tenant:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Оптовик не найден")
    return tenant


@router.get("/me", response_model=PlatformMeResponse)
async def platform_me(telegram_id: int = Depends(get_platform_admin), config: Config = Depends(get_config)):
    return PlatformMeResponse(
        telegram_id=telegram_id, webapp_configured=bool(config.telegram.webapp_base_url),
        platform_bot_configured=bool(config.platform.bot_token),
    )


@router.get("/tenants", response_model=list[PlatformTenantResponse])
async def list_tenants(
    _: int = Depends(get_platform_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    stats = await repo.tenant.platform_stats()
    return [_response(t, stats, config) for t in await repo.tenant.list_all()]


@router.get("/tenants/{slug}", response_model=PlatformTenantResponse)
async def get_tenant(
    slug: str,
    _: int = Depends(get_platform_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    tenant = await _tenant(repo, slug)
    return _response(tenant, await repo.tenant.platform_stats(), config)


@router.post("/tenants", response_model=PlatformTenantSaved, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    data: PlatformTenantCreateRequest,
    _: int = Depends(get_platform_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    """Новый оптовик: проверяем slug и токен бота, ставим кнопку меню, назначаем владельца"""
    slug = data.slug.strip().lower()
    if problem := slug_problem(slug):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_slug", problem)
    if await repo.tenant.get_by_slug(slug):
        raise api_error(status.HTTP_409_CONFLICT, "slug_taken", "Такой адрес уже занят")
    warnings: list[str] = []
    token = (data.bot_token or "").strip() or None
    username = await _connect_bot(token, slug, config, warnings) if token else None
    if not token:
        warnings.append("Бот не подключён — клиенты не смогут войти, пока не добавите токен")
    tenant = await repo.tenant.create(slug=slug, name=data.name.strip(), bot_token=token, bot_username=username)
    if data.owner_telegram_id:
        await make_owner(repo, tenant, data.owner_telegram_id)
    else:
        warnings.append("Владелец не назначен — в админку оптовика пока никто не попадёт")
    await repo.commit()
    return _response(tenant, await repo.tenant.platform_stats(), config, PlatformTenantSaved, warnings=warnings)


@router.patch("/tenants/{slug}", response_model=PlatformTenantSaved)
async def update_tenant(
    slug: str,
    data: PlatformTenantUpdateRequest,
    _: int = Depends(get_platform_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    """Переименовать, сменить бота или владельца, отключить/включить"""
    tenant = await _tenant(repo, slug)
    fields = data.model_dump(exclude_unset=True)
    warnings: list[str] = []
    if fields.get("name"):
        tenant.name = fields["name"].strip()
    if fields.get("is_active") is not None:
        tenant.is_active = fields["is_active"]
    if fields.get("bot_token"):
        token = fields["bot_token"].strip()
        tenant.bot_username = await _connect_bot(token, tenant.slug, config, warnings)
        tenant.bot_token = token
    if fields.get("owner_telegram_id"):
        await make_owner(repo, tenant, fields["owner_telegram_id"])
    await repo.commit()
    return _response(tenant, await repo.tenant.platform_stats(), config, PlatformTenantSaved, warnings=warnings)
