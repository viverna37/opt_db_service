"""
Админка: справочники (категории, атрибуты, бренды, уровни цен), настройки
тенанта, аудит-лог. Изменения — только owner / admin; читать справочники
могут все сотрудники (нужны менеджеру в форме товара).
"""
from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config.config import Config
from app.database.models import (
    AttributeType,
    PriceBasis,
    PriceTier,
    Tenant,
    TenantUser,
    Variant,
)
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_admin, get_config, get_repository, get_staff, get_tenant
from app.models.admin_models import (
    AttributeUpdateRequest,
    AttributeWriteRequest,
    AuditLogResponse,
    BrandWriteRequest,
    CategoryAttributesRequest,
    CategoryUpdateRequest,
    CategoryWriteRequest,
    PriceTierUpdateRequest,
    PriceTierWriteRequest,
    TenantSettingsResponse,
    TenantSettingsUpdateRequest,
)
from app.models.catalog_models import (
    AttributeDefinitionResponse,
    BrandResponse,
    CategoryNode,
    PriceTierResponse,
)
from app.services.catalog_service import (
    ancestor_chain,
    build_category_tree,
    inherited_attribute_ids,
    stock_status_from_qty,
    would_create_cycle,
)
from app.utils.file_storage import InvalidImage, LocalStorage, build_image_key, file_url, process_image
from app.utils.money import safe_zone

router = APIRouter(prefix="/v1/admin", tags=["admin: settings"])


async def _commit_unique(repo: Repository, message: str) -> None:
    """Уникальные ключи (key атрибута, имя бренда, min_qty уровня) — 409 вместо 500"""
    try:
        await repo.commit()
    except IntegrityError:
        await repo.session.rollback()
        raise api_error(status.HTTP_409_CONFLICT, "duplicate", message)


# ---------- Категории ----------

@router.get("/categories", response_model=list[CategoryNode])
async def list_categories(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Всё дерево, включая скрытые; product_count — вместе со скрытыми товарами"""
    categories = await repo.category.list(tenant.id)
    counts = await repo.product.count_per_category(tenant.id, only_visible=False)
    return build_category_tree(categories, counts, only_visible=False)


@router.get("/categories/{category_id}/attributes", response_model=list[int])
async def get_category_attributes(
    category_id: int,
    inherited: bool = False,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """
    Атрибуты категории. inherited=false — привязанные именно к ней (для
    экрана привязки), inherited=true — вместе с унаследованными от предков
    (какие поля показать в форме товара).
    """
    await _category(repo, tenant, category_id)
    if not inherited:
        return (await repo.category.attribute_ids([category_id])).get(category_id, [])
    categories = await repo.category.list(tenant.id)
    chain = ancestor_chain(categories, category_id)
    bound = await repo.category.attribute_ids([c.id for c in chain])
    return inherited_attribute_ids(categories, bound, category_id)


async def _category(repo: Repository, tenant: Tenant, category_id: int):
    category = await repo.category.get(tenant.id, category_id)
    if not category:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Категория не найдена")
    return category


@router.post("/categories", response_model=CategoryNode, status_code=status.HTTP_201_CREATED)
async def create_category(
    data: CategoryWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    if data.parent_id is not None:
        await _category(repo, tenant, data.parent_id)
    category = await repo.category.create(tenant.id, **data.model_dump())
    await repo.audit.log(tenant.id, admin.id, "create", "category", category.id, {"name": category.name})
    await repo.commit()
    return CategoryNode(id=category.id, parent_id=category.parent_id, name=category.name,
                        sort_order=category.sort_order, is_visible=category.is_visible, product_count=0)


@router.patch("/categories/{category_id}", response_model=CategoryNode)
async def update_category(
    category_id: int,
    data: CategoryUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    category = await _category(repo, tenant, category_id)
    fields = data.model_dump(exclude_unset=True)
    if "parent_id" in fields and fields["parent_id"] is not None:
        await _category(repo, tenant, fields["parent_id"])
        if would_create_cycle(await repo.category.list(tenant.id), category_id, fields["parent_id"]):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "cycle", "Нельзя вложить категорию саму в себя")
    for key, value in fields.items():
        setattr(category, key, value)
    await repo.audit.log(tenant.id, admin.id, "update", "category", category.id, {"fields": sorted(fields)})
    await repo.commit()
    return CategoryNode(id=category.id, parent_id=category.parent_id, name=category.name,
                        image_url=file_url(category.image_key), sort_order=category.sort_order,
                        is_visible=category.is_visible, product_count=0)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    """Только пустую: без подкатегорий и товаров (иначе — скрыть через is_visible)"""
    category = await _category(repo, tenant, category_id)
    if await repo.category.has_children(category_id) or await repo.category.has_products(category_id):
        raise api_error(status.HTTP_409_CONFLICT, "not_empty", "В категории есть подкатегории или товары")
    await repo.category.set_attributes(category_id, [])
    await repo.category.delete(category)
    await repo.audit.log(tenant.id, admin.id, "delete", "category", category_id, {"name": category.name})
    await repo.commit()


@router.put("/categories/{category_id}/attributes", response_model=list[int])
async def set_category_attributes(
    category_id: int,
    data: CategoryAttributesRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    """Привязка атрибутов к категории (потомки наследуют)"""
    await _category(repo, tenant, category_id)
    own = {a.id for a in await repo.attribute.list(tenant.id)}
    unknown = [i for i in data.attribute_ids if i not in own]
    if unknown:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_attribute", f"Атрибуты не найдены: {unknown}")
    await repo.category.set_attributes(category_id, data.attribute_ids)
    await repo.commit()
    return list(dict.fromkeys(data.attribute_ids))


@router.post("/categories/{category_id}/image", response_model=CategoryNode)
async def upload_category_image(
    category_id: int,
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    """Картинка для плитки категории на главной"""
    category = await _category(repo, tenant, category_id)
    key = _save_image(config, tenant, "categories", await file.read())
    old_key, category.image_key = category.image_key, key
    await repo.commit()
    if old_key:
        LocalStorage(config.uploads_dir).delete(old_key)
    return CategoryNode(id=category.id, parent_id=category.parent_id, name=category.name,
                        image_url=file_url(category.image_key), sort_order=category.sort_order,
                        is_visible=category.is_visible, product_count=0)


# ---------- Атрибуты ----------

@router.get("/attributes", response_model=list[AttributeDefinitionResponse])
async def list_attributes(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    return await repo.attribute.list(tenant.id)


def _check_options(attr_type: AttributeType, options) -> None:
    if attr_type == AttributeType.select and not options:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "options_required", "Для списка нужны варианты значений")


@router.post("/attributes", response_model=AttributeDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_attribute(
    data: AttributeWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    _check_options(data.type, data.options)
    if await repo.attribute.get_by_key(tenant.id, data.key):
        raise api_error(status.HTTP_409_CONFLICT, "duplicate", "Атрибут с таким ключом уже есть")
    attribute = await repo.attribute.create(tenant.id, **data.model_dump())
    await repo.audit.log(tenant.id, admin.id, "create", "attribute", attribute.id, {"key": attribute.key})
    await _commit_unique(repo, "Атрибут с таким ключом уже есть")
    return attribute


async def _attribute(repo: Repository, tenant: Tenant, attribute_id: int):
    attribute = await repo.attribute.get(tenant.id, attribute_id)
    if not attribute:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Атрибут не найден")
    return attribute


@router.patch("/attributes/{attribute_id}", response_model=AttributeDefinitionResponse)
async def update_attribute(
    attribute_id: int,
    data: AttributeUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    attribute = await _attribute(repo, tenant, attribute_id)
    fields = data.model_dump(exclude_unset=True)
    if "options" in fields:
        _check_options(attribute.type, fields["options"])
    for key, value in fields.items():
        setattr(attribute, key, value)
    await repo.audit.log(tenant.id, admin.id, "update", "attribute", attribute.id, {"fields": sorted(fields)})
    await repo.commit()
    return attribute


@router.delete("/attributes/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attribute(
    attribute_id: int,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    """Значения у товаров остаются в JSON, но больше не показываются и не фильтруются"""
    attribute = await _attribute(repo, tenant, attribute_id)
    await repo.attribute.delete(attribute)
    await repo.audit.log(tenant.id, admin.id, "delete", "attribute", attribute_id, {"key": attribute.key})
    await repo.commit()


# ---------- Бренды ----------

@router.get("/brands", response_model=list[BrandResponse])
async def list_brands(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    return await repo.brand.list(tenant.id)


@router.post("/brands", response_model=BrandResponse, status_code=status.HTTP_201_CREATED)
async def create_brand(
    data: BrandWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    if await repo.brand.get_by_name(tenant.id, data.name.strip()):
        raise api_error(status.HTTP_409_CONFLICT, "duplicate", "Такой бренд уже есть")
    brand = await repo.brand.create(tenant.id, name=data.name.strip(), sort_order=data.sort_order)
    await _commit_unique(repo, "Такой бренд уже есть")
    return brand


@router.patch("/brands/{brand_id}", response_model=BrandResponse)
async def update_brand(
    brand_id: int,
    data: BrandWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    brand = await repo.brand.get(tenant.id, brand_id)
    if not brand:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Бренд не найден")
    brand.name, brand.sort_order = data.name.strip(), data.sort_order
    await _commit_unique(repo, "Такой бренд уже есть")
    return brand


@router.delete("/brands/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(
    brand_id: int,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    brand = await repo.brand.get(tenant.id, brand_id)
    if not brand:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Бренд не найден")
    if await repo.brand.is_used(brand_id):
        raise api_error(status.HTTP_409_CONFLICT, "in_use", "Бренд указан у товаров")
    await repo.brand.delete(brand)
    await repo.commit()


# ---------- Уровни цен ----------

@router.get("/price-tiers", response_model=list[PriceTierResponse])
async def list_price_tiers(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    return await repo.price_tier.list(tenant.id)


@router.post("/price-tiers", response_model=PriceTierResponse, status_code=status.HTTP_201_CREATED)
async def create_price_tier(
    data: PriceTierWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    fields = _tier_threshold(tenant, data.model_dump())
    duplicate = (
        await repo.price_tier.get_by_min_amount(tenant.id, fields["min_amount"])
        if tenant.price_basis == PriceBasis.amount
        else await repo.price_tier.get_by_min_qty(tenant.id, fields["min_qty"])
    )
    if duplicate:
        raise api_error(status.HTTP_409_CONFLICT, "duplicate", "Уровень с таким порогом уже есть")
    tier = await repo.price_tier.create(tenant.id, **fields)
    await repo.audit.log(tenant.id, admin.id, "create", "price_tier", tier.id, {"label": tier.label})
    await repo.tenant.touch_catalog(tenant.id)
    await _commit_unique(repo, "Уровень с таким порогом уже есть")
    return tier


def _tier_threshold(tenant: Tenant, fields: dict, partial: bool = False) -> dict:
    """Порог уровня — в поле своего режима, чужое поле обнуляется"""
    if tenant.price_basis == PriceBasis.amount:
        if fields.get("min_amount") is None and not partial:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "threshold_required", "Укажите сумму заявки, от которой действует уровень")
        fields.pop("min_qty", None)
        if not partial or "min_amount" in fields:
            fields["min_qty"] = None
    else:
        if fields.get("min_qty") is None and not partial:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "threshold_required", "Укажите количество, от которого действует уровень")
        fields.pop("min_amount", None)
        if not partial or "min_qty" in fields:
            fields["min_amount"] = None
    return fields


async def _tier(repo: Repository, tenant: Tenant, tier_id: int) -> PriceTier:
    tier = await repo.price_tier.get(tenant.id, tier_id)
    if not tier:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Уровень цены не найден")
    return tier


@router.patch("/price-tiers/{tier_id}", response_model=PriceTierResponse)
async def update_price_tier(
    tier_id: int,
    data: PriceTierUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    tier = await _tier(repo, tenant, tier_id)
    fields = _tier_threshold(tenant, data.model_dump(exclude_unset=True), partial=True)
    for key, value in fields.items():
        setattr(tier, key, value)
    await repo.audit.log(tenant.id, admin.id, "update", "price_tier", tier.id, {"fields": sorted(fields)})
    await repo.tenant.touch_catalog(tenant.id)
    await _commit_unique(repo, "Уровень с таким порогом уже есть")
    return tier


@router.delete("/price-tiers/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_price_tier(
    tier_id: int,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    """Удаляет уровень вместе со всеми ценами на нём (фронт должен переспросить)"""
    tier = await _tier(repo, tenant, tier_id)
    await repo.price_tier.delete(tier)
    await repo.audit.log(tenant.id, admin.id, "delete", "price_tier", tier_id, {"label": tier.label})
    await repo.tenant.touch_catalog(tenant.id)
    await repo.commit()


# ---------- Настройки тенанта ----------

def _settings(tenant: Tenant) -> TenantSettingsResponse:
    return TenantSettingsResponse(
        slug=tenant.slug, name=tenant.name, logo_url=file_url(tenant.logo_key), accent_color=tenant.accent_color,
        currency=tenant.currency, timezone=tenant.timezone, manager_username=tenant.manager_username,
        low_stock_threshold=tenant.low_stock_threshold, access_mode=tenant.access_mode, age_gate=tenant.age_gate,
        price_basis=tenant.price_basis,
        min_order_amount=tenant.min_order_amount, welcome_text=tenant.welcome_text,
        bot_username=tenant.bot_username, bot_configured=bool(tenant.bot_token),
    )


@router.get("/settings", response_model=TenantSettingsResponse)
async def get_settings(tenant: Tenant = Depends(get_tenant), staff: TenantUser = Depends(get_staff)):
    return _settings(tenant)


@router.patch("/settings", response_model=TenantSettingsResponse)
async def update_settings(
    data: TenantSettingsUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    """Бот-токен и slug здесь не меняются — только через CLI (python -m app.cli)"""
    fields = data.model_dump(exclude_unset=True)
    if "timezone" in fields and safe_zone(fields["timezone"]).key != fields["timezone"]:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_timezone", "Неизвестный часовой пояс")
    if fields.get("price_basis") and fields["price_basis"] != tenant.price_basis and await repo.price_tier.list(tenant.id):
        raise api_error(
            status.HTTP_409_CONFLICT, "tiers_exist",
            "Чтобы сменить режим уровней цен, сначала удалите текущие уровни — их пороги в другом формате",
        )
    if "manager_username" in fields and fields["manager_username"]:
        fields["manager_username"] = fields["manager_username"].strip().lstrip("@")
    for key, value in fields.items():
        setattr(tenant, key, value)
    if "low_stock_threshold" in fields:
        await _recompute_stock(repo, tenant)
    await repo.audit.log(tenant.id, admin.id, "update", "settings", tenant.id, {"fields": sorted(fields)})
    await repo.commit()
    return _settings(tenant)


async def _recompute_stock(repo: Repository, tenant: Tenant) -> None:
    """Порог «мало» поменялся — пересчитать статусы вариантов, у которых остаток ведётся числом"""
    result = await repo.session.execute(
        select(Variant).where(Variant.tenant_id == tenant.id, Variant.stock_qty.is_not(None))
    )
    for variant in result.scalars().all():
        variant.stock_status = stock_status_from_qty(variant.stock_qty, tenant.low_stock_threshold)
    await repo.tenant.touch_catalog(tenant.id)


@router.post("/settings/logo", response_model=TenantSettingsResponse)
async def upload_logo(
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    key = _save_image(config, tenant, "branding", await file.read())
    old_key, tenant.logo_key = tenant.logo_key, key
    await repo.commit()
    if old_key:
        LocalStorage(config.uploads_dir).delete(old_key)
    return _settings(tenant)


def _save_image(config: Config, tenant: Tenant, folder: str, raw: bytes) -> str:
    try:
        content = process_image(raw)
    except InvalidImage as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_image", str(exc))
    key = build_image_key(tenant.id, folder)
    LocalStorage(config.uploads_dir).save(key, content)
    return key


# ---------- Аудит ----------

@router.get("/audit", response_model=list[AuditLogResponse])
async def list_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    admin: TenantUser = Depends(get_admin),
    repo: Repository = Depends(get_repository),
):
    return await repo.audit.list(tenant.id, limit=limit, offset=offset)

