"""
Server-to-server ручки для бот-сервиса (один процесс на ботов всех
тенантов). Защищены X-Service-Key (см. app/deps.py::require_service_key).
Действия от имени пользователей бот делает обычными ручками, подписывая
initData токеном бота тенанта — как в такси (taxi_tg_service/api_client).
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.database.repository.main_repository import Repository
from app.deps import get_repository, require_service_key

router = APIRouter(prefix="/v1/service", tags=["service"], dependencies=[Depends(require_service_key)])


class ServiceTenantResponse(BaseModel):
    slug: str
    name: str
    bot_token: str
    bot_username: str | None = None
    welcome_text: str | None = None


@router.get("/tenants", response_model=list[ServiceTenantResponse])
async def list_bot_tenants(repo: Repository = Depends(get_repository)):
    """Активные тенанты с настроенным ботом — для регистрации вебхуков /bot/{slug}"""
    return [
        ServiceTenantResponse(slug=t.slug, name=t.name, bot_token=t.bot_token, bot_username=t.bot_username,
                              welcome_text=t.welcome_text)
        for t in await repo.tenant.list_active()
        if t.bot_token
    ]
