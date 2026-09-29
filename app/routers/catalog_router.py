from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.database.models import PriceBasis, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.database.repository.product_repository import SORTS, ProductFilter
from app.deps import api_error, get_client, get_repository, get_tenant
from app.models.catalog_models import (
    AttributeDefinitionResponse,
    BrandResponse,
    CategoryDetailResponse,
    CategoryNode,
    FilterOption,
    ProductCard,
    ProductListItem,
)
from app.models.common_models import Page
from app.services.cart_service import build_cart_view, cart_qty_by_variant
from app.services.catalog_service import (
    ancestor_chain,
    build_category_tree,
    descendant_ids,
    inherited_attribute_ids,
)
from app.services.serializers import product_card, product_list_item

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

ATTR_PARAM_PREFIX = "attr."


@router.get("/categories", response_model=list[CategoryNode])
async def list_categories(
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """Главная: дерево видимых категорий с числом товаров (вместе с подкатегориями)"""
    categories = await repo.category.list(tenant.id)
    counts = await repo.product.count_per_category(tenant.id)
    return build_category_tree(categories, counts, only_visible=True)


@router.get("/categories/{category_id}", response_model=CategoryDetailResponse)
async def get_category(
    category_id: int,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """Экран категории: подкатегории, хлебные крошки и чипы-фильтры из filterable-атрибутов"""
    categories = await repo.category.list(tenant.id)
    chain = ancestor_chain(categories, category_id)
    if not chain or not all(c.is_visible for c in chain):
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Категория не найдена")
    category = chain[-1]
    subtree = descendant_ids(categories, category_id)

    bound = await repo.category.attribute_ids([c.id for c in chain])
    attribute_ids = inherited_attribute_ids(categories, bound, category_id)
    all_definitions = {d.id: d for d in await repo.attribute.list(tenant.id)}
    definitions = [all_definitions[i] for i in attribute_ids if i in all_definitions]
    definitions.sort(key=lambda d: (d.sort_order, d.id))
    filterable = [d for d in definitions if d.filterable]
    values = await repo.product.attribute_values(tenant.id, subtree, filterable)

    counts = await repo.product.count_per_category(tenant.id)
    tree = {n.id: n for n in _flatten(build_category_tree(categories, counts, only_visible=True))}
    children = tree[category_id].children if category_id in tree else []

    return CategoryDetailResponse(
        id=category.id,
        parent_id=category.parent_id,
        name=category.name,
        breadcrumbs=[{"id": c.id, "name": c.name} for c in chain],
        children=children,
        attributes=[AttributeDefinitionResponse.model_validate(d) for d in definitions],
        filters=[
            FilterOption(
                attribute=AttributeDefinitionResponse.model_validate(d),
                values=d.options if d.options else values.get(d.key, []),
            )
            for d in filterable
            if d.options or values.get(d.key)
        ],
        brands=[BrandResponse.model_validate(b) for b in await repo.brand.list(tenant.id)],
    )


def _flatten(nodes: list[CategoryNode]) -> list[CategoryNode]:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten(node.children))
    return result


async def parse_attribute_filters(request: Request, repo: Repository, tenant_id: int) -> list:
    """?attr.volume=30,60&attr.nicotine=20 -> [(определение, ['30','60']), ...]; неизвестные ключи игнорируются"""
    raw = {
        key[len(ATTR_PARAM_PREFIX):]: value
        for key, value in request.query_params.items()
        if key.startswith(ATTR_PARAM_PREFIX) and value != ""
    }
    if not raw:
        return []
    definitions = {d.key: d for d in await repo.attribute.list(tenant_id)}
    return [
        (definitions[key], [v.strip() for v in value.split(",") if v.strip()])
        for key, value in raw.items()
        if key in definitions
    ]


@router.get("/products", response_model=Page[ProductListItem])
async def list_products(
    request: Request,
    category_id: Optional[int] = None,
    q: Optional[str] = Query(None, max_length=100),
    brand_id: list[int] = Query(default=[]),
    in_stock: bool = False,
    sort: str = Query("default", pattern="|".join(SORTS)),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """
    Список/поиск товаров. Фильтры по атрибутам — query-параметры вида
    attr.<key>=v1,v2 (значения через запятую — «или»).
    """
    category_ids = None
    if category_id is not None:
        category_ids = descendant_ids(await repo.category.list(tenant.id), category_id)
    filters = ProductFilter(
        category_ids=category_ids, q=q, brand_ids=brand_id or None, in_stock_only=in_stock,
        attributes=await parse_attribute_filters(request, repo, tenant.id),
    )
    products, total = await repo.product.search(tenant.id, filters, sort=sort, limit=limit, offset=offset)
    definitions = await repo.attribute.list(tenant.id)
    tables = await repo.price.tables([p.id for p in products])
    brand_names = {b.id: b.name for b in await repo.brand.list(tenant.id)}
    cart_qty = await cart_qty_by_variant(repo, member.id)
    return Page(
        items=[product_list_item(p, definitions, tables[p.id], brand_names, cart_qty) for p in products],
        total=total, limit=limit, offset=offset,
    )


@router.get("/products/{product_id}", response_model=ProductCard)
async def get_product(
    product_id: int,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """Карточка: фото, атрибуты, плитки уровней цен (текущий — по количеству в корзине), варианты со степперами"""
    product = await repo.product.get(tenant.id, product_id)
    if not product or not product.is_visible:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Товар не найден")
    tables = await repo.price.tables([product.id])
    amount_tier_id = None
    if tenant.price_basis == PriceBasis.amount:
        # уровень общий на заявку — нужен расчёт всей корзины
        amount_tier_id = (await build_cart_view(repo, tenant, await repo.cart.get_for_member(member.id))).amount_tier_id
    return product_card(
        tenant, product, await repo.attribute.list(tenant.id), await repo.price_tier.list(tenant.id),
        tables[product.id], {b.id: b.name for b in await repo.brand.list(tenant.id)},
        await cart_qty_by_variant(repo, member.id), amount_tier_id=amount_tier_id,
    )
