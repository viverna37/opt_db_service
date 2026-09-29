"""Расчёт цен — чистые функции app/services/pricing.py"""
from app.services.pricing import PriceTable, Tier, min_price, price_group, resolve_tier, unit_price

T1, T10, T50 = Tier(1, "от 1 шт", 1), Tier(2, "от 10 шт", 10), Tier(3, "от 50 шт", 50)
TIERS = [T50, T1, T10]  # порядок на входе не важен


def test_tier_by_total_qty_of_product():
    assert resolve_tier(TIERS, 1) == T1
    assert resolve_tier(TIERS, 9) == T1
    assert resolve_tier(TIERS, 10) == T10
    assert resolve_tier(TIERS, 500) == T50


def test_qty_below_lowest_threshold_still_gets_lowest_tier():
    assert resolve_tier([Tier(1, "от 5 шт", 5)], 2).threshold == 5
    assert resolve_tier([], 10) is None


def test_group_sums_all_variants_to_pick_tier_and_hints_next():
    table = PriceTable(product={1: 26000, 2: 25000, 3: 24000})
    group = price_group(TIERS, table, [(100, 4), (101, 3)])  # 7 шт разных вкусов
    assert group.total_qty == 7
    assert group.tier == T1
    assert group.next_tier == T10 and group.qty_to_next_tier == 3  # «ещё 3 шт до цены от 10»
    assert [line.unit_price for line in group.lines] == [26000, 26000]
    assert group.subtotal == 7 * 26000

    group = price_group(TIERS, table, [(100, 6), (101, 4)])  # ровно 10 — уже второй уровень для обоих
    assert group.tier == T10
    assert group.subtotal == 10 * 25000
    assert group.qty_to_next_tier == 40


def test_variant_override_and_fallback_to_lower_tier():
    table = PriceTable(product={1: 26000}, variants={101: {1: 30000, 2: 29000}})
    # у товара цена только на «от 1» — на уровне «от 10» берётся она же
    assert unit_price(TIERS, table, 100, T10) == 26000
    # у варианта своя цена на уровнях
    assert unit_price(TIERS, table, 101, T10) == 29000
    # на «от 50» у варианта ничего нет — спускаемся к его «от 10», а не к цене товара
    assert unit_price(TIERS, table, 101, T50) == 29000


def test_no_price_at_all_is_none():
    group = price_group(TIERS, PriceTable(), [(100, 2)])
    assert group.lines[0].unit_price is None and group.lines[0].amount is None
    assert group.subtotal == 0


def test_min_price_for_list():
    assert min_price(PriceTable(product={1: 26000, 3: 24000}, variants={5: {1: 23000}})) == 23000
    assert min_price(PriceTable()) is None


# ---------- Режим amount: уровень по сумме заявки ----------

R, W3, W10 = Tier(10, "розница", 100000), Tier(11, "от 3 000 ₽", 300000), Tier(12, "от 10 000 ₽", 1000000)
AMOUNT_TIERS = [W10, R, W3]


def test_amount_tier_uses_total_in_that_tier_prices():
    from app.services.pricing import resolve_amount_tier

    table = PriceTable(product={10: 90000, 11: 46000, 12: 45000})  # 900 / 460 / 450 ₽
    # 5 шт: в оптовых ценах 2 300 ₽ < 3 000 — уровень «розница», 4 500 ₽
    res = resolve_amount_tier(AMOUNT_TIERS, [(table, [(1, 5)])])
    assert res.tier == R and res.total == 5 * 90000
    assert res.next_tier == W3 and res.amount_to_next_tier == 300000 - 5 * 46000
    # 7 шт: 3 220 ₽ в ценах «от 3 000» — проходит, хотя в розничных было бы 6 300
    res = resolve_amount_tier(AMOUNT_TIERS, [(table, [(1, 7)])])
    assert res.tier == W3 and res.total == 7 * 46000
    # 23 шт: в ценах «от 10 000» 10 350 ₽ — старший уровень, дальше некуда
    res = resolve_amount_tier(AMOUNT_TIERS, [(table, [(1, 23)])])
    assert res.tier == W10 and res.next_tier is None and res.amount_to_next_tier is None


def test_amount_tier_counts_whole_cart_across_products():
    from app.services.pricing import resolve_amount_tier

    a = PriceTable(product={10: 50000, 11: 20000})
    b = PriceTable(product={10: 80000, 11: 40000})
    res = resolve_amount_tier(AMOUNT_TIERS, [(a, [(1, 5)]), (b, [(2, 5)])])  # 1 000 + 2 000 = 3 000 ₽ в оптовых
    assert res.tier == W3 and [g.subtotal for g in res.groups] == [100000, 200000]


def test_no_lower_price_falls_back_to_nearest_higher():
    # у жидкостей нет розничной цены — на розничном уровне берём цену «от 3 000», а не «по запросу»
    table = PriceTable(product={11: 26000, 12: 25500})
    assert unit_price(AMOUNT_TIERS, table, 1, R) == 26000
