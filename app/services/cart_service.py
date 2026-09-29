"""
Сборка корзины с ценами: грузит варианты/товары/уровни/цены пачкой и
прогоняет через app/services/pricing.py. Используется и клиентом (экран
корзины, отправка заявки), и админкой («Живые корзины»).

Позиции, которые больше нельзя заказать (вариант скрыт/нет в наличии,
товар скрыт/удалён), остаются в корзине и подсвечиваются
(problem=unavailable), но не участвуют ни в уровне цены, ни в итоге —
иначе клиент видел бы скидку за товар, который ему не отгрузят.
"""
from dataclasses import dataclass

from app.database.models import Cart, Product, Tenant, Variant
from app.database.repository.main_repository import Repository
from app.models.cart_models import CartGroup, CartLine, CartResponse
from app.models.catalog_models import TierPrice
from app.services.catalog_service import is_orderable, to_pricing_tiers
from app.services.pricing import Tier, price_group
from app.utils.file_storage import file_url


# Первый блокер — то, что фронт покажет клиенту в первую очередь
BLOCKERS_ORDER = ("empty", "unavailable_items", "no_price_items", "below_min_amount")


@dataclass
class PricedLine:
    """Для снимка в заявку — то, что CartLine не несёт (сам товар/вариант)"""
    product: Product
    variant: Variant
    qty: int
    unit_price: int
    amount: int
    tier_label: str | None


@dataclass
class CartView:
    response: CartResponse
    priced_lines: list[PricedLine]


def _tier_price(tier: Tier | None) -> TierPrice | None:
    if tier is None:
        return None
    return TierPrice(tier_id=tier.id, label=tier.label, min_qty=tier.min_qty)


async def build_cart_view(repo: Repository, tenant: Tenant, cart: Cart | None) -> CartView:
    items = list(cart.items) if cart else []
    variants = await repo.variant.get_many(tenant.id, [item.variant_id for item in items])
    product_ids = list(dict.fromkeys(v.product_id for v in variants.values()))
    products = await repo.product.get_many(tenant.id, product_ids)
    tiers = to_pricing_tiers(await repo.price_tier.list(tenant.id))
    tables = await repo.price.tables(product_ids)

    # группировка по товару в порядке первого добавления
    grouped: dict[int, list] = {}
    for item in items:
        variant = variants.get(item.variant_id)
        if variant is None:
            continue
        grouped.setdefault(variant.product_id, []).append((item, variant))

    groups: list[CartGroup] = []
    priced_lines: list[PricedLine] = []
    blockers: set[str] = set()
    total = total_qty = 0

    for product_id, rows in grouped.items():
        product = products[product_id]
        orderable = [(item, variant) for item, variant in rows if is_orderable(variant, product)]
        pricing = price_group(tiers, tables[product_id], [(variant.id, item.qty) for item, variant in orderable])
        by_variant = {line.variant_id: line for line in pricing.lines}

        lines: list[CartLine] = []
        for item, variant in rows:
            priced = by_variant.get(variant.id)
            problem = None
            if priced is None:
                problem = "unavailable"
                blockers.add("unavailable_items")
            elif priced.unit_price is None:
                problem = "no_price"
                blockers.add("no_price_items")
            else:
                priced_lines.append(PricedLine(
                    product=product, variant=variant, qty=item.qty, unit_price=priced.unit_price,
                    amount=priced.amount, tier_label=pricing.tier.label if pricing.tier else None,
                ))
            lines.append(CartLine(
                variant_id=variant.id,
                variant_name=None if variant.is_default else variant.name,
                sku=variant.sku or product.sku,
                qty=item.qty,
                unit_price=priced.unit_price if priced else None,
                amount=priced.amount if priced else None,
                stock_status=variant.stock_status,
                stock_qty=variant.stock_qty,
                problem=problem,
            ))

        next_tier = _tier_price(pricing.next_tier)
        groups.append(CartGroup(
            product_id=product.id,
            product_name=product.name,
            cover_url=file_url(product.photos[0].storage_key) if product.photos else None,
            total_qty=pricing.total_qty,
            tier=_tier_price(pricing.tier),
            next_tier=next_tier,
            qty_to_next_tier=pricing.qty_to_next_tier,
            subtotal=pricing.subtotal,
            lines=lines,
        ))
        total += pricing.subtotal
        total_qty += pricing.total_qty

    if not priced_lines:
        blockers.add("empty")
    elif tenant.min_order_amount and total < tenant.min_order_amount:
        blockers.add("below_min_amount")

    response = CartResponse(
        groups=groups,
        total=total,
        total_qty=total_qty,
        positions=sum(len(g.lines) for g in groups),
        min_order_amount=tenant.min_order_amount,
        can_submit=not blockers,
        blockers=[b for b in BLOCKERS_ORDER if b in blockers],
        updated_at=cart.updated_at if cart else None,
    )
    return CartView(response=response, priced_lines=priced_lines)


async def cart_qty_by_variant(repo: Repository, member_id: int) -> dict[int, int]:
    cart = await repo.cart.get_for_member(member_id)
    return {item.variant_id: item.qty for item in cart.items} if cart else {}
