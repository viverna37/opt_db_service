"""Сборка ответов API из ORM-объектов — общая для клиентских и админских роутеров"""
from typing import Sequence

from app.database.models import (
    AttributeDefinition,
    AttributeScope,
    Order,
    PriceTier,
    Product,
    Tenant,
    TenantUser,
)
from app.models.catalog_models import ProductCard, ProductListItem, TierPrice, VariantCard
from app.models.common_models import TenantPublicResponse, TenantResponse, TgUserResponse
from app.models.order_models import AdminOrderResponse, OrderClient, OrderHistoryResponse, OrderItemResponse
from app.services.catalog_service import (
    aggregate_stock,
    ask_manager_url,
    attribute_values,
    contact_url,
    to_pricing_tiers,
)
from app.services.pricing import PriceTable, find_next_tier, min_price, resolve_tier, tier_prices
from app.utils.file_storage import file_url


def tenant_public(tenant: Tenant) -> TenantPublicResponse:
    return TenantPublicResponse(
        slug=tenant.slug, name=tenant.name, logo_url=file_url(tenant.logo_key), accent_color=tenant.accent_color,
        currency=tenant.currency, welcome_text=tenant.welcome_text, bot_username=tenant.bot_username,
    )


def tenant_response(tenant: Tenant) -> TenantResponse:
    return TenantResponse(
        **tenant_public(tenant).model_dump(),
        manager_username=tenant.manager_username, access_mode=tenant.access_mode, age_gate=tenant.age_gate,
        min_order_amount=tenant.min_order_amount, low_stock_threshold=tenant.low_stock_threshold,
        catalog_updated_at=tenant.catalog_updated_at,
    )


def order_client(member: TenantUser) -> OrderClient:
    return OrderClient(
        tenant_user_id=member.id, role=member.role, status=member.status, note=member.note,
        user=TgUserResponse.model_validate(member.tg_user), contact_url=contact_url(member.tg_user),
    )


def admin_order(order: Order) -> AdminOrderResponse:
    return AdminOrderResponse(
        id=order.id, number=order.number, status=order.status, comment=order.comment, total=order.total,
        created_at=order.created_at, updated_at=order.updated_at,
        items=[OrderItemResponse.model_validate(item) for item in order.items],
        manager_note=order.manager_note, client=order_client(order.tenant_user),
        history=[OrderHistoryResponse.model_validate(h) for h in order.history],
    )


def product_list_item(
    product: Product, definitions: Sequence[AttributeDefinition], table: PriceTable, brand_names: dict[int, str],
    cart_qty: dict[int, int], include_hidden_variants: bool = False,
) -> ProductListItem:
    variants = [v for v in product.variants if v.is_visible or include_hidden_variants]
    product_defs = [d for d in definitions if d.scope == AttributeScope.product]
    return ProductListItem(
        id=product.id,
        name=product.name,
        brand=brand_names.get(product.brand_id) if product.brand_id else None,
        category_id=product.category_id,
        sku=product.sku,
        cover_url=file_url(product.photos[0].storage_key) if product.photos else None,
        meta=attribute_values(product_defs, product.attributes, only_in_list=True),
        price_from=min_price(table),
        stock_status=aggregate_stock(variants),
        variants_count=sum(1 for v in variants if not v.is_default),
        in_cart_qty=sum(cart_qty.get(v.id, 0) for v in variants),
    )


def _tier_prices(tiers: Sequence[PriceTier], table: PriceTable, variant_id: int | None) -> list[TierPrice]:
    return [
        TierPrice(tier_id=tier.id, label=tier.label, min_qty=tier.min_qty, amount=amount)
        for tier, amount in tier_prices(to_pricing_tiers(tiers), table, variant_id)
    ]


def product_card(
    tenant: Tenant, product: Product, definitions: Sequence[AttributeDefinition], tiers: Sequence[PriceTier],
    table: PriceTable, brand_names: dict[int, str], cart_qty: dict[int, int], include_hidden_variants: bool = False,
) -> ProductCard:
    variants = [v for v in product.variants if v.is_visible or include_hidden_variants]
    product_defs = [d for d in definitions if d.scope == AttributeScope.product]
    variant_defs = [d for d in definitions if d.scope == AttributeScope.variant]
    in_cart = sum(cart_qty.get(v.id, 0) for v in variants)
    pricing_tiers = to_pricing_tiers(tiers)
    current = resolve_tier(pricing_tiers, in_cart)
    upcoming = find_next_tier(pricing_tiers, in_cart)
    product_prices = _tier_prices(tiers, table, None)
    return ProductCard(
        id=product.id,
        name=product.name,
        brand=brand_names.get(product.brand_id) if product.brand_id else None,
        category_id=product.category_id,
        sku=product.sku,
        description=product.description,
        photos=[file_url(photo.storage_key) for photo in product.photos],
        attributes=attribute_values(product_defs, product.attributes),
        tiers=product_prices,
        current_tier_id=current.id if current else None,
        next_tier=next((p for p in product_prices if upcoming and p.tier_id == upcoming.id), None),
        qty_to_next_tier=(upcoming.min_qty - in_cart) if upcoming else None,
        in_cart_qty=in_cart,
        variants=[
            VariantCard(
                id=v.id, name=v.name, sku=v.sku, is_default=v.is_default,
                attributes=attribute_values(variant_defs, v.attributes),
                stock_status=v.stock_status, stock_qty=v.stock_qty,
                prices=_tier_prices(tiers, table, v.id), in_cart_qty=cart_qty.get(v.id, 0),
            )
            for v in variants
        ],
        has_variants=any(not v.is_default for v in variants),
        ask_manager_url=ask_manager_url(tenant.manager_username, product.name),
        updated_at=product.updated_at,
    )
