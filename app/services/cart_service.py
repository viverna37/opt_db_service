"""
Сборка корзины с ценами: грузит варианты/товары/уровни/цены пачкой и
прогоняет через app/services/pricing.py. Используется и клиентом (экран
корзины, отправка заявки), и админкой («Живые корзины»).

Уровни цен — по режиму тенанта (Tenant.price_basis): у каждого товара свой
по количеству штук или общий на заявку по её сумме (см. pricing.py).

Позиции, которые больше нельзя заказать (вариант скрыт/нет в наличии,
товар скрыт/удалён), остаются в корзине и подсвечиваются
(problem=unavailable), но не участвуют ни в уровне цены, ни в итоге —
иначе клиент видел бы скидку за товар, который ему не отгрузят.
"""
from dataclasses import dataclass

from app.database.models import Cart, PriceBasis, Product, Tenant, Variant
from app.database.repository.main_repository import Repository
from app.models.cart_models import CartGroup, CartLine, CartResponse
from app.services.catalog_service import is_orderable, tier_price, to_pricing_tiers
from app.services.pricing import price_group, resolve_amount_tier
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
    amount_tier_id: int | None = None  # режим amount: текущий уровень заявки (подсветка на карточке)


async def build_cart_view(repo: Repository, tenant: Tenant, cart: Cart | None) -> CartView:
    items = list(cart.items) if cart else []
    variants = await repo.variant.get_many(tenant.id, [item.variant_id for item in items])
    product_ids = list(dict.fromkeys(v.product_id for v in variants.values()))
    products = await repo.product.get_many(tenant.id, product_ids)
    basis = tenant.price_basis
    tiers = to_pricing_tiers(await repo.price_tier.list(tenant.id), basis)
    tables = await repo.price.tables(product_ids)

    # группировка по товару в порядке первого добавления
    grouped: dict[int, list] = {}
    for item in items:
        variant = variants.get(item.variant_id)
        if variant is None:
            continue
        grouped.setdefault(variant.product_id, []).append((item, variant))

    orderable_by_product = {
        product_id: [(item, variant) for item, variant in rows if is_orderable(variant, products[product_id])]
        for product_id, rows in grouped.items()
    }
    pricing_inputs = [
        (tables[product_id], [(variant.id, item.qty) for item, variant in orderable_by_product[product_id]])
        for product_id in grouped
    ]

    # qty — уровень у каждого товара свой; amount — общий на заявку по её сумме
    amount_pricing = None
    if basis == PriceBasis.amount:
        amount_pricing = resolve_amount_tier(tiers, pricing_inputs)
        group_prices = amount_pricing.groups
    else:
        group_prices = [price_group(tiers, table, lines) for table, lines in pricing_inputs]

    groups: list[CartGroup] = []
    priced_lines: list[PricedLine] = []
    blockers: set[str] = set()
    total = total_qty = 0

    for (product_id, rows), pricing in zip(grouped.items(), group_prices):
        product = products[product_id]
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

        groups.append(CartGroup(
            product_id=product.id,
            product_name=product.name,
            cover_url=file_url(product.photos[0].storage_key) if product.photos else None,
            total_qty=pricing.total_qty,
            tier=tier_price(pricing.tier, basis) if pricing.tier else None,
            next_tier=tier_price(pricing.next_tier, basis) if pricing.next_tier else None,
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
        price_basis=basis,
        tier=tier_price(amount_pricing.tier, basis) if amount_pricing and amount_pricing.tier and groups else None,
        next_tier=tier_price(amount_pricing.next_tier, basis) if amount_pricing and amount_pricing.next_tier and groups else None,
        amount_to_next_tier=amount_pricing.amount_to_next_tier if amount_pricing and groups else None,
        groups=groups,
        total=total,
        total_qty=total_qty,
        positions=sum(len(g.lines) for g in groups),
        min_order_amount=tenant.min_order_amount,
        can_submit=not blockers,
        blockers=[b for b in BLOCKERS_ORDER if b in blockers],
        updated_at=cart.updated_at if cart else None,
    )
    return CartView(response=response, priced_lines=priced_lines, amount_tier_id=amount_pricing.tier.id if amount_pricing and amount_pricing.tier else None)


async def cart_qty_by_variant(repo: Repository, member_id: int) -> dict[int, int]:
    cart = await repo.cart.get_for_member(member_id)
    return {item.variant_id: item.qty for item in cart.items} if cart else {}
