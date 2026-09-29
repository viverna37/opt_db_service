from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.config.config import Config
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_config, get_repository
from app.models.common_models import TenantPublicResponse
from app.services.serializers import tenant_public
from app.utils.file_storage import LocalStorage

router = APIRouter(prefix="/v1", tags=["public"])


@router.get("/tenants/{slug}/public", response_model=TenantPublicResponse)
async def get_tenant_public(slug: str, repo: Repository = Depends(get_repository)):
    """Брендинг витрины до авторизации (название, логотип, цвет) — без цен и товаров"""
    tenant = await repo.tenant.get_by_slug(slug)
    if not tenant or not tenant.is_active:
        raise api_error(status.HTTP_404_NOT_FOUND, "tenant_not_found", "Каталог не найден")
    return tenant_public(tenant)


@router.get("/files/{key:path}")
async def get_file(key: str, config: Config = Depends(get_config)):
    """
    Картинки отдаются без авторизации — <img src> не умеет слать заголовки.
    Ключи содержат случайный uuid, перебором их не найти. Контент не меняется
    (новая картинка = новый ключ), поэтому кэш надолго.
    """
    try:
        content = LocalStorage(config.uploads_dir).read(key)
    except (FileNotFoundError, IsADirectoryError):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(content, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})
