"""
Расчёт цен — единственное место, где считаются деньги (фронт только
отображает). Чистые функции без БД — данные подгружает
app/services/cart_service.py.

Два режима уровней цен (Tenant.price_basis), порог уровня — Tier.threshold:
- qty: уровень определяется суммарным количеством ВСЕХ вариантов товара в
  корзине, у каждого товара свой уровень (threshold — штуки).
- amount: уровень общий на всю заявку и определяется её суммой (threshold —
  копейки), как у оптовиков «от 3 000 / от 10 000 ₽». Сумма зависит от
  уровня, поэтому уровень выбирается сверху вниз: самый старший, для которого
  сумма заявки В ЕГО ЦЕНАХ не меньше его порога (resolve_amount_tier).

Правила цены варианта на уровне:
- переопределение варианта на этом уровне, иначе цена товара на этом уровне;
- нет на этом уровне — ближайший младший уровень, где цена есть (у оптовика
  может быть заполнен, например, только «от 1 шт»);
- нет и ниже — ближайший старший (у товара нет розничной цены, а заявка на
  розничном уровне: берём минимальную оптовую, а не «цена по запросу»);
- нет вообще — None («цена по запросу»): позиция отображается, но в заявку
  такую не отправить (см. cart_service.CartView).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tier:
    id: int
    label: str
    threshold: int  # штуки (qty) или копейки (amount)


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


@dataclass
class AmountPricing:
    """Режим amount: общий уровень заявки и цены по каждому товару на нём"""
    tier: Tier | None
    next_tier: Tier | None
    amount_to_next_tier: int | None  # копейки: сколько добавить до следующего уровня
    groups: list[GroupPrice]
    total: int


def sort_tiers(tiers: list[Tier]) -> list[Tier]:
    return sorted(tiers, key=lambda t: t.threshold)


def resolve_tier(tiers: list[Tier], value: int) -> Tier | None:
    """Старший уровень с порогом <= value; меньше младшего порога — всё равно младший (порог — не минимальная партия)"""
    ordered = sort_tiers(tiers)
    if not ordered:
        return None
    current = ordered[0]
    for tier in ordered:
        if tier.threshold <= value:
            current = tier
    return current


def find_next_tier(tiers: list[Tier], value: int) -> Tier | None:
    for tier in sort_tiers(tiers):
        if tier.threshold > value:
            return tier
    return None


def unit_price(tiers: list[Tier], table: PriceTable, variant_id: int | None, tier: Tier | None) -> int | None:
    if tier is None:
        return None
    ordered = sort_tiers(tiers)
    overrides = table.variants.get(variant_id, {}) if variant_id is not None else {}

    def price_at(candidate: Tier) -> int | None:
        if candidate.id in overrides:
            return overrides[candidate.id]
        return table.product.get(candidate.id)

    # от текущего уровня вниз к младшим, затем вверх к старшим
    below = [t for t in ordered if t.threshold <= tier.threshold]
    above = [t for t in ordered if t.threshold > tier.threshold]
    for candidate in [*reversed(below), *above]:
        price = price_at(candidate)
        if price is not None:
            return price
    return None


def tier_prices(tiers: list[Tier], table: PriceTable, variant_id: int | None = None) -> list[tuple[Tier, int | None]]:
    """Плитки уровней на карточке: цена на каждом уровне (с тем же откатом)."""
    return [(tier, unit_price(tiers, table, variant_id, tier)) for tier in sort_tiers(tiers)]


def min_price(table: PriceTable) -> int | None:
    """Цена «от» в списке товаров — минимальная из всех заведённых цен товара и его вариантов."""
    amounts = list(table.product.values())
    for overrides in table.variants.values():
        amounts.extend(overrides.values())
    return min(amounts) if amounts else None


def price_lines_at(tiers: list[Tier], table: PriceTable, lines: list[tuple[int, int]], tier: Tier | None) -> GroupPrice:
    """Строки одного товара по ценам заданного уровня (next_tier/qty_to_next_tier не заполняются)"""
    priced: list[LinePrice] = []
    subtotal = 0
    for variant_id, qty in lines:
        price = unit_price(tiers, table, variant_id, tier)
        amount = price * qty if price is not None else None
        if amount is not None:
            subtotal += amount
        priced.append(LinePrice(variant_id=variant_id, qty=qty, unit_price=price, amount=amount))
    return GroupPrice(
        total_qty=sum(qty for _, qty in lines), tier=tier, next_tier=None, qty_to_next_tier=None,
        lines=priced, subtotal=subtotal,
    )


def price_group(tiers: list[Tier], table: PriceTable, lines: list[tuple[int, int]]) -> GroupPrice:
    """
    Режим qty. lines — [(variant_id, qty)] одного товара. Возвращает текущий
    уровень, следующий уровень с подсказкой «ещё N шт» и цены по строкам.
    """
    total_qty = sum(qty for _, qty in lines)
    tier = resolve_tier(tiers, total_qty)
    next_tier = find_next_tier(tiers, total_qty)
    group = price_lines_at(tiers, table, lines, tier)
    group.next_tier = next_tier
    group.qty_to_next_tier = (next_tier.threshold - total_qty) if next_tier else None
    return group


def resolve_amount_tier(tiers: list[Tier], groups: list[tuple[PriceTable, list[tuple[int, int]]]]) -> AmountPricing:
    """
    Режим amount. groups — [(цены товара, [(variant_id, qty)])] всей заявки.
    Уровень — самый старший, для которого сумма заявки в его ценах >= его
    порога; если ни один не проходит — младший. Следующий уровень и
    «сколько добавить» считаются в ценах следующего уровня.
    """
    ordered = sort_tiers(tiers)

    def at(tier: Tier | None) -> tuple[list[GroupPrice], int]:
        priced = [price_lines_at(tiers, table, lines, tier) for table, lines in groups]
        return priced, sum(g.subtotal for g in priced)

    if not ordered:
        priced, total = at(None)
        return AmountPricing(tier=None, next_tier=None, amount_to_next_tier=None, groups=priced, total=total)

    chosen = ordered[0]
    for tier in reversed(ordered):
        _, total = at(tier)
        if total >= tier.threshold:
            chosen = tier
            break
    priced, total = at(chosen)

    next_tier = next((t for t in ordered if t.threshold > chosen.threshold), None)
    to_next = None
    if next_tier is not None and total > 0:
        _, total_next = at(next_tier)
        to_next = max(next_tier.threshold - total_next, 1)
    return AmountPricing(tier=chosen, next_tier=next_tier, amount_to_next_tier=to_next, groups=priced, total=total)
