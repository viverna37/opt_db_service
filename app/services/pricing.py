"""
Расчёт цен — единственное место, где считаются деньги (фронт только
отображает). Чистые функции без БД — данные подгружает
app/services/cart_service.py.

Правила:
- Уровень цены товара определяется суммарным количеством ВСЕХ вариантов
  этого товара в корзине: берётся уровень с максимальным min_qty <= qty.
  Если qty меньше порога самого младшего уровня — всё равно применяется
  самый младший (порог уровня — не минимальная партия).
- Цена варианта на уровне: переопределение варианта на этом уровне, иначе
  цена товара на этом уровне. Если на этом уровне цены нет ни там, ни там —
  спускаемся к ближайшему младшему уровню, где она есть (у оптовика может
  быть заполнен, например, только «от 1 шт»).
- Нет цены вообще — None («цена по запросу»): позиция отображается, но в
  заявку такую не отправить (см. cart_service.CartView.problems).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tier:
    id: int
    label: str
    min_qty: int


@dataclass
class PriceTable:
    """Цены одного товара: product[tier_id] и переопределения variants[variant_id][tier_id], всё в копейках."""
    product: dict[int, int] = field(default_factory=dict)
    variants: dict[int, dict[int, int]] = field(default_factory=dict)


@dataclass
class LinePrice:
    variant_id: int
    qty: int
    unit_price: int | None
    amount: int | None


@dataclass
class GroupPrice:
    total_qty: int
    tier: Tier | None
    next_tier: Tier | None
    qty_to_next_tier: int | None
    lines: list[LinePrice]
    subtotal: int  # сумма позиций, у которых есть цена


def sort_tiers(tiers: list[Tier]) -> list[Tier]:
    return sorted(tiers, key=lambda t: t.min_qty)


def resolve_tier(tiers: list[Tier], qty: int) -> Tier | None:
    ordered = sort_tiers(tiers)
    if not ordered:
        return None
    current = ordered[0]
    for tier in ordered:
        if tier.min_qty <= qty:
            current = tier
    return current


def find_next_tier(tiers: list[Tier], qty: int) -> Tier | None:
    for tier in sort_tiers(tiers):
        if tier.min_qty > qty:
            return tier
    return None


def unit_price(tiers: list[Tier], table: PriceTable, variant_id: int | None, tier: Tier | None) -> int | None:
    if tier is None:
        return None
    ordered = sort_tiers(tiers)
    overrides = table.variants.get(variant_id, {}) if variant_id is not None else {}
    # от текущего уровня вниз к младшим
    for candidate in reversed([t for t in ordered if t.min_qty <= tier.min_qty]):
        if candidate.id in overrides:
            return overrides[candidate.id]
        if candidate.id in table.product:
            return table.product[candidate.id]
    return None


def tier_prices(tiers: list[Tier], table: PriceTable, variant_id: int | None = None) -> list[tuple[Tier, int | None]]:
    """Плитки уровней на карточке: цена на каждом уровне (с тем же откатом к младшему)."""
    return [(tier, unit_price(tiers, table, variant_id, tier)) for tier in sort_tiers(tiers)]


def min_price(table: PriceTable) -> int | None:
    """Цена «от» в списке товаров — минимальная из всех заведённых цен товара и его вариантов."""
    amounts = list(table.product.values())
    for overrides in table.variants.values():
        amounts.extend(overrides.values())
    return min(amounts) if amounts else None


def price_group(tiers: list[Tier], table: PriceTable, lines: list[tuple[int, int]]) -> GroupPrice:
    """
    lines — [(variant_id, qty)] одного товара. Возвращает текущий уровень,
    следующий уровень с подсказкой «ещё N шт» и цены по строкам.
    """
    total_qty = sum(qty for _, qty in lines)
    tier = resolve_tier(tiers, total_qty)
    next_tier = find_next_tier(tiers, total_qty)
    priced: list[LinePrice] = []
    subtotal = 0
    for variant_id, qty in lines:
        price = unit_price(tiers, table, variant_id, tier)
        amount = price * qty if price is not None else None
        if amount is not None:
            subtotal += amount
        priced.append(LinePrice(variant_id=variant_id, qty=qty, unit_price=price, amount=amount))
    return GroupPrice(
        total_qty=total_qty,
        tier=tier,
        next_tier=next_tier,
        qty_to_next_tier=(next_tier.min_qty - total_qty) if next_tier else None,
        lines=priced,
        subtotal=subtotal,
    )
