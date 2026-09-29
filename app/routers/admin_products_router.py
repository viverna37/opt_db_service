"""
Админка: товары, варианты, цены, фото. Доступно всем сотрудникам (owner /
admin / manager) — менеджер ведёт наличие и карточки; справочники и
настройки — в admin_settings_router (только owner / admin).
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from app.config.config import Config
from app.database.models import (
    AttributeScope,
    Product,
    StockStatus,
    Tenant,
    TenantUser,
    Variant,
)
from app.database.repository.main_repository import Repository
from app.database.repository.product_repository import SORTS, ProductFilter
from app.deps import api_error, get_config, get_repository, get_staff, get_tenant
from app.models.admin_models import (
    AdminProductListItem,
    AdminProductResponse,
    AdminVariantResponse,
    PhotoResponse,
    PricesUpdateRequest,
    PriceRow,
    ProductStockRequest,
    ProductUpdateRequest,
    ProductWriteRequest,
    VariantUpdateRequest,
    VariantWriteRequest,
)
from app.models.common_models import Page
from app.routers.catalog_router import parse_attribute_filters
from app.services.catalog_service import (
    AttributeValueError,
    coerce_attributes,
    descendant_ids,
    stock_status_from_qty,
)
from app.services.serializers import product_list_item
from app.utils.dt import utcnow
from app.utils.file_storage import InvalidImage, LocalStorage, build_image_key, file_url, process_image
from app.utils.text import normalize_name

router = APIRouter(prefix="/v1/admin/products", tags=["admin: products"])

MAX_PHOTOS = 5


# ---------- helpers ----------

async def _product(repo: Repository, tenant: Tenant, product_id: int) -> Product:
    product = await repo.product.get(tenant.id, product_id)
    if not product:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Товар не найден")
    return product


async def _variant(repo: Repository, tenant: Tenant, product: Product, variant_id: int) -> Variant:
    variant = next((v for v in product.variants if v.id == variant_id), None)
    if not variant:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Вариант не найден")
    return variant


async def _response(repo: Repository, tenant: Tenant, product_id: int) -> AdminProductResponse:
    product = await _product(repo, tenant, product_id)
    table = (await repo.price.tables([product.id]))[product.id]
    prices = [PriceRow(tier_id=t, amount=a) for t, a in table.product.items()]
    for variant_id, overrides in table.variants.items():
        prices.extend(PriceRow(tier_id=t, variant_id=variant_id, amount=a) for t, a in overrides.items())
    return AdminProductResponse(
        id=product.id, name=product.name, category_id=product.category_id, brand_id=product.brand_id,
        sku=product.sku, description=product.description, attributes=product.attributes or {},
        is_visible=product.is_visible, sort_order=product.sort_order,
        photos=[PhotoResponse(id=p.id, url=file_url(p.storage_key)) for p in product.photos],
        variants=[AdminVariantResponse.model_validate(v) for v in product.variants],
        prices=prices, created_at=product.created_at, updated_at=product.updated_at,
    )


async def _check_refs(repo: Repository, tenant: Tenant, category_id: Optional[int], brand_id: Optional[int]) -> None:
    """Категория и бренд должны принадлежать этому же тенанту"""
    if category_id is not None and not await repo.category.get(tenant.id, category_id):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_category", "Категория не найдена")
    if brand_id is not None and not await repo.brand.get(tenant.id, brand_id):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_brand", "Бренд не найден")


async def _coerce(repo: Repository, tenant: Tenant, raw: dict, scope: AttributeScope) -> dict:
    try:
        return coerce_attributes(await repo.attribute.list(tenant.id), raw, scope)
    except AttributeValueError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_attribute", str(exc))


def _apply_stock(variant: Variant, fields: dict, threshold: int) -> None:
    """
    stock_qty числом -> статус по порогу «мало»; явный stock_status без
    stock_qty -> ручной режим, остаток сбрасывается (см. VariantUpdateRequest).
    """
    if "stock_qty" in fields and fields["stock_qty"] is not None:
        variant.stock_qty = fields["stock_qty"]
        variant.stock_status = stock_status_from_qty(variant.stock_qty, threshold)
    elif fields.get("stock_status") is not None:
        variant.stock_status = fields["stock_status"]
        variant.stock_qty = None
    elif "stock_qty" in fields:  # явный null — перейти в ручной режим, статус оставить
        variant.stock_qty = None


def _sync_default_variant(product: Product) -> None:
    """
    Товар без вариантов = один скрытый в UI вариант по умолчанию. Появился
    хоть один видимый именованный вариант — дефолтный прячем; пропали все —
    возвращаем дефолтный, чтобы товар можно было положить в корзину.
    """
    default = next((v for v in product.variants if v.is_default), None)
    if default is None:
        return
    has_named = any(v.is_visible and not v.is_default for v in product.variants)
    default.is_visible = not has_named


async def _touch(repo: Repository, tenant: Tenant, product: Product | None = None) -> None:
    if product is not None:
        product.updated_at = utcnow()
    await repo.tenant.touch_catalog(tenant.id)


# ---------- Товары ----------

@router.get("", response_model=Page[AdminProductListItem])
async def list_products(
    request: Request,
    category_id: Optional[int] = None,
    q: Optional[str] = Query(None, max_length=100),
    brand_id: list[int] = Query(default=[]),
    stock_status: Optional[StockStatus] = None,
    sort: str = Query("default", pattern="|".join(SORTS)),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Список товаров админки — вместе со скрытыми. stock_status=out — «нет в наличии»"""
    category_ids = descendant_ids(await repo.category.list(tenant.id), category_id) if category_id else None
    filters = ProductFilter(
        category_ids=category_ids, q=q, brand_ids=brand_id or None, only_visible=False, stock_status=stock_status,
        attributes=await parse_attribute_filters(request, repo, tenant.id),
    )
    products, total = await repo.product.search(tenant.id, filters, sort=sort, limit=limit, offset=offset)
    definitions = await repo.attribute.list(tenant.id)
    tables = await repo.price.tables([p.id for p in products])
    brand_names = {b.id: b.name for b in await repo.brand.list(tenant.id)}
    items = [
        AdminProductListItem(
            **product_list_item(p, definitions, tables[p.id], brand_names, {}).model_dump(), is_visible=p.is_visible,
        )
        for p in products
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=AdminProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    data: ProductWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Создаёт товар сразу с вариантом по умолчанию — положить в корзину можно только вариант"""
    await _check_refs(repo, tenant, data.category_id, data.brand_id)
    fields = data.model_dump()
    fields["attributes"] = await _coerce(repo, tenant, data.attributes, AttributeScope.product)
    product = await repo.product.create(tenant.id, **fields)
    await repo.variant.create(tenant.id, product.id, is_default=True, sort_order=-1)
    await repo.audit.log(tenant.id, staff.id, "create", "product", product.id, {"name": product.name})
    await _touch(repo, tenant)
    await repo.commit()
    return await _response(repo, tenant, product.id)


@router.get("/{product_id}", response_model=AdminProductResponse)
async def get_product(
    product_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    return await _response(repo, tenant, product_id)


@router.patch("/{product_id}", response_model=AdminProductResponse)
async def update_product(
    product_id: int,
    data: ProductUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    product = await _product(repo, tenant, product_id)
    fields = data.model_dump(exclude_unset=True)
    await _check_refs(repo, tenant, fields.get("category_id"), fields.get("brand_id"))
    if "attributes" in fields:
        fields["attributes"] = await _coerce(repo, tenant, fields["attributes"] or {}, AttributeScope.product)
    if fields.get("name"):
        fields["name"] = fields["name"].strip()
        product.name_normalized = normalize_name(fields["name"])
    for key, value in fields.items():
        setattr(product, key, value)
    await repo.audit.log(tenant.id, staff.id, "update", "product", product.id, {"fields": sorted(fields)})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Мягкое удаление: на варианты ссылаются корзины и заявки. В корзинах позиция станет «недоступна»"""
    product = await _product(repo, tenant, product_id)
    product.deleted_at = utcnow()
    product.is_visible = False
    await repo.audit.log(tenant.id, staff.id, "delete", "product", product.id, {"name": product.name})
    await _touch(repo, tenant, product)
    await repo.commit()


@router.post("/{product_id}/stock", response_model=AdminProductResponse)
async def set_product_stock(
    product_id: int,
    data: ProductStockRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Быстрый тумблер наличия из списка товаров — для всех видимых вариантов сразу"""
    product = await _product(repo, tenant, product_id)
    for variant in product.variants:
        if variant.is_visible:
            _apply_stock(variant, {"stock_status": data.stock_status}, tenant.low_stock_threshold)
            variant.updated_at = utcnow()
    await repo.audit.log(tenant.id, staff.id, "stock", "product", product.id, {"stock_status": data.stock_status.value})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


# ---------- Цены ----------

@router.put("/{product_id}/prices", response_model=AdminProductResponse)
async def replace_prices(
    product_id: int,
    data: PricesUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """
    Полная замена цен товара. variant_id=null — цена товара на уровне,
    с variant_id — переопределение для варианта (если у вкуса своя цена).
    """
    product = await _product(repo, tenant, product_id)
    tier_ids = {t.id for t in await repo.price_tier.list(tenant.id)}
    variant_ids = {v.id for v in product.variants}
    for row in data.prices:
        if row.tier_id not in tier_ids:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_tier", f"Уровень {row.tier_id} не найден")
        if row.variant_id is not None and row.variant_id not in variant_ids:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_variant",
                            f"Вариант {row.variant_id} не относится к товару")
    await repo.price.replace_for_product(
        tenant.id, product.id, [(row.variant_id, row.tier_id, row.amount) for row in data.prices]
    )
    await repo.audit.log(tenant.id, staff.id, "prices", "product", product.id, {"rows": len(data.prices)})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


# ---------- Варианты ----------

@router.post("/{product_id}/variants", response_model=AdminProductResponse, status_code=status.HTTP_201_CREATED)
async def create_variant(
    product_id: int,
    data: VariantWriteRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    product = await _product(repo, tenant, product_id)
    attributes = await _coerce(repo, tenant, data.attributes, AttributeScope.variant)
    variant = await repo.variant.create(
        tenant.id, product.id, name=data.name.strip(), sku=data.sku, attributes=attributes,
        is_visible=data.is_visible, sort_order=data.sort_order, stock_status=data.stock_status,
    )
    _apply_stock(variant, {"stock_qty": data.stock_qty}, tenant.low_stock_threshold)
    await repo.session.refresh(product, attribute_names=["variants"])
    _sync_default_variant(product)
    await repo.audit.log(tenant.id, staff.id, "create", "variant", variant.id, {"name": variant.name})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


@router.patch("/{product_id}/variants/{variant_id}", response_model=AdminProductResponse)
async def update_variant(
    product_id: int,
    variant_id: int,
    data: VariantUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    product = await _product(repo, tenant, product_id)
    variant = await _variant(repo, tenant, product, variant_id)
    fields = data.model_dump(exclude_unset=True)
    if "attributes" in fields:
        variant.attributes = await _coerce(repo, tenant, fields["attributes"] or {}, AttributeScope.variant)
    for key in ("name", "sku", "sort_order"):
        if key in fields:
            setattr(variant, key, fields[key].strip() if key == "name" and fields[key] else fields[key])
    if "is_visible" in fields and not variant.is_default:
        variant.is_visible = fields["is_visible"]
    _apply_stock(variant, fields, tenant.low_stock_threshold)
    variant.updated_at = utcnow()
    _sync_default_variant(product)
    await repo.audit.log(tenant.id, staff.id, "update", "variant", variant.id, {"fields": sorted(fields)})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


@router.delete("/{product_id}/variants/{variant_id}", response_model=AdminProductResponse)
async def delete_variant(
    product_id: int,
    variant_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Варианты физически не удаляются (на них ссылаются корзины) — только скрываются"""
    product = await _product(repo, tenant, product_id)
    variant = await _variant(repo, tenant, product, variant_id)
    if variant.is_default:
        raise api_error(status.HTTP_409_CONFLICT, "default_variant", "Вариант по умолчанию не удаляется")
    variant.is_visible = False
    variant.updated_at = utcnow()
    _sync_default_variant(product)
    await repo.audit.log(tenant.id, staff.id, "delete", "variant", variant.id, {"name": variant.name})
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


# ---------- Фото ----------

@router.post("/{product_id}/photos", response_model=AdminProductResponse, status_code=status.HTTP_201_CREATED)
async def upload_photo(
    product_id: int,
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    """До 5 фото на товар, первое — обложка. Пережимается в webp"""
    product = await _product(repo, tenant, product_id)
    if len(product.photos) >= MAX_PHOTOS:
        raise api_error(status.HTTP_409_CONFLICT, "too_many_photos", f"Не больше {MAX_PHOTOS} фото на товар")
    try:
        content = process_image(await file.read())
    except InvalidImage as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_image", str(exc))
    key = build_image_key(tenant.id, f"products/{product.id}")
    LocalStorage(config.uploads_dir).save(key, content)
    next_order = max((p.sort_order for p in product.photos), default=-1) + 1
    await repo.photo.create(product.id, key, next_order)
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)


@router.delete("/{product_id}/photos/{photo_id}", response_model=AdminProductResponse)
async def delete_photo(
    product_id: int,
    photo_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
    config: Config = Depends(get_config),
):
    product = await _product(repo, tenant, product_id)
    photo = next((p for p in product.photos if p.id == photo_id), None)
    if not photo:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Фото не найдено")
    key = photo.storage_key
    await repo.photo.delete(photo)
    await _touch(repo, tenant, product)
    await repo.commit()
    LocalStorage(config.uploads_dir).delete(key)
    return await _response(repo, tenant, product_id)


@router.put("/{product_id}/photos/order", response_model=AdminProductResponse)
async def reorder_photos(
    product_id: int,
    photo_ids: list[int],
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Новый порядок фото — полный список id; первый станет обложкой"""
    product = await _product(repo, tenant, product_id)
    by_id = {p.id: p for p in product.photos}
    if sorted(photo_ids) != sorted(by_id):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_order", "Нужен полный список id фото товара")
    for index, photo_id in enumerate(photo_ids):
        by_id[photo_id].sort_order = index
    await _touch(repo, tenant, product)
    await repo.commit()
    return await _response(repo, tenant, product_id)
