"""
Применение распарсенного блочного прайса (app/importer/block_price.py) к
каталогу тенанта. Это синхронизация, а не заливка «с нуля»: следующий прайс
того же оптовика обновляет существующие товары.

- категория = лист прайса (по названию, создаётся при отсутствии);
- товар ищется по (категория, нормализованное название), иначе создаётся;
- варианты — по нормализованному названию; пропавшие из прайса скрываются
  (не удаляются: на них ссылаются корзины), новые создаются;
- наличие: зачёркнутое = out, остальное = in_stock (если остаток не ведётся
  числом вручную в админке — тогда не трогаем);
- цены товара заменяются целиком ценами из прайса по уровням «по сумме
  заявки» (Tenant.price_basis = amount), уровни создаются по порогам прайса;
- фото из прайса ставится только товару без фото (загруженные вручную не
  перетираются);
- товары тенанта, которых нет в прайсе, скрываются (is_visible=False).

wipe=True — перед импортом удалить весь каталог тенанта (товары, категории,
бренды, атрибуты, уровни цен). Заявки не трогаются: в них снимок позиций.
"""
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select, update

from app.database.models import (
    AttributeDefinition,
    Brand,
    Cart,
    CartItem,
    Category,
    CategoryAttribute,
    Order,
    OrderItem,
    PriceBasis,
    PriceTier,
    Product,
    ProductPhoto,
    Price,
    StockStatus,
    Tenant,
    Variant,
)
from app.database.repository.main_repository import Repository
from app.importer.block_price import RETAIL_THRESHOLD, ParseResult, ParsedProduct
from app.utils.dt import utcnow
from app.utils.file_storage import InvalidImage, LocalStorage, build_image_key, process_image
from app.utils.text import normalize_name
from app.services.catalog_service import stock_status_from_qty

logger = logging.getLogger(__name__)


class ImportError_(Exception):
    pass


@dataclass
class ImportStats:
    categories_created: int = 0
    brands_created: int = 0
    tiers_created: int = 0
    products_created: int = 0
    products_updated: int = 0
    products_hidden: int = 0
    variants_created: int = 0
    variants_hidden: int = 0
    photos_added: int = 0
    photo_errors: int = 0
    warnings: list[str] = field(default_factory=list)


def _category_title(sheet: str) -> str:
    """«ЖИДКОСТИ» -> «Жидкости», «ПОД-СИСТЕМЫ» -> «Под-системы»; смешанный регистр оставляем"""
    title = sheet.strip()
    return title[:1] + title[1:].lower() if title.isupper() else title


def _tier_label(threshold_rub: int) -> str:
    amount = f"{threshold_rub:,}".replace(",", " ")
    return f"Розница от {amount} ₽" if threshold_rub == RETAIL_THRESHOLD else f"от {amount} ₽"


async def wipe_catalog(repo: Repository, tenant: Tenant) -> None:
    s = repo.session
    t = tenant.id
    order_ids = select(Order.id).where(Order.tenant_id == t)
    await s.execute(update(OrderItem).where(OrderItem.order_id.in_(order_ids)).values(product_id=None, variant_id=None))
    await s.execute(delete(CartItem).where(CartItem.cart_id.in_(select(Cart.id).where(Cart.tenant_id == t))))
    await s.execute(delete(Price).where(Price.tenant_id == t))
    await s.execute(delete(ProductPhoto).where(ProductPhoto.product_id.in_(select(Product.id).where(Product.tenant_id == t))))
    await s.execute(delete(Variant).where(Variant.tenant_id == t))
    await s.execute(delete(Product).where(Product.tenant_id == t))
    await s.execute(delete(CategoryAttribute).where(CategoryAttribute.category_id.in_(select(Category.id).where(Category.tenant_id == t))))
    # дерево: сначала обнуляем родителей, чтобы удалить без оглядки на порядок
    await s.execute(update(Category).where(Category.tenant_id == t).values(parent_id=None))
    await s.execute(delete(Category).where(Category.tenant_id == t))
    await s.execute(delete(Brand).where(Brand.tenant_id == t))
    await s.execute(delete(AttributeDefinition).where(AttributeDefinition.tenant_id == t))
    await s.execute(delete(PriceTier).where(PriceTier.tenant_id == t))
    await s.flush()


async def import_block_price(
    repo: Repository,
    tenant: Tenant,
    parsed: ParseResult,
    storage: LocalStorage | None,
    wipe: bool = False,
    hide_missing: bool = True,
) -> ImportStats:
    stats = ImportStats(warnings=list(parsed.warnings))
    if wipe:
        await wipe_catalog(repo, tenant)

    # --- режим уровней и сами уровни ---
    # Блочные/табличные прайсы — уровни по сумме заявки (пороги в рублях),
    # простые «строка = товар» — по количеству штук (пороги в штуках)
    basis = PriceBasis(parsed.tenant_defaults.get("price_basis", "amount"))
    existing_tiers = list(await repo.price_tier.list(tenant.id))
    if tenant.price_basis != basis:
        if existing_tiers:
            raise ImportError_("У тенанта уровни цен в другом режиме — удалите их или запустите с --wipe")
        tenant.price_basis = basis
    thresholds = sorted({t for p in parsed.products for t in p.prices}
                        | {t for p in parsed.products for v in p.variants for t in (v.prices or {})})
    if basis == PriceBasis.amount:
        tier_by_threshold = {t.min_amount // 100: t.id for t in existing_tiers if t.min_amount is not None}
    else:
        tier_by_threshold = {t.min_qty: t.id for t in existing_tiers if t.min_qty is not None}
    for index, threshold in enumerate(thresholds):
        if threshold not in tier_by_threshold:
            if basis == PriceBasis.amount:
                tier = await repo.price_tier.create(
                    tenant.id, label=_tier_label(threshold), min_amount=threshold * 100, sort_order=index,
                )
            else:
                tier = await repo.price_tier.create(
                    tenant.id, label=f"от {threshold} шт", min_qty=threshold, sort_order=index,
                )
            tier_by_threshold[threshold] = tier.id
            stats.tiers_created += 1
    # Пустые настройки тенанта — из шапки прайса (минимальная сумма, менеджер, условия)
    defaults = parsed.tenant_defaults
    if not tenant.min_order_amount:
        if defaults.get("min_order_amount"):
            tenant.min_order_amount = defaults["min_order_amount"] * 100
        elif basis == PriceBasis.amount and RETAIL_THRESHOLD in thresholds:
            tenant.min_order_amount = RETAIL_THRESHOLD * 100
    if not tenant.manager_username and defaults.get("manager_username"):
        tenant.manager_username = defaults["manager_username"]
    if not tenant.welcome_text and defaults.get("welcome_text"):
        tenant.welcome_text = defaults["welcome_text"]

    # --- категории: лист прайса или путь из группы (вложенные), и бренды ---
    by_parent: dict[tuple[int | None, str], Category] = {
        (c.parent_id, normalize_name(c.name)): c for c in await repo.category.list(tenant.id)
    }
    sort_counter: dict[int | None, int] = {}

    async def category_for(path: list[str]) -> Category:
        parent: Category | None = None
        for name in path:
            key = (parent.id if parent else None, normalize_name(name))
            category = by_parent.get(key)
            if category is None:
                order = sort_counter.get(key[0], 0)
                category = await repo.category.create(
                    tenant.id, name=name, parent_id=key[0], sort_order=order,
                )
                by_parent[key] = category
                stats.categories_created += 1
            sort_counter[key[0]] = sort_counter.get(key[0], 0) + 1
            parent = category
        return parent

    brands = {normalize_name(b.name): b for b in await repo.brand.list(tenant.id)}

    async def brand_id(name: str | None) -> int | None:
        if not name:
            return None
        key = normalize_name(name)
        if key not in brands:
            brands[key] = await repo.brand.create(tenant.id, name=name)
            stats.brands_created += 1
        return brands[key].id

    # --- товары ---
    result = await repo.session.execute(
        select(Product).where(Product.tenant_id == tenant.id, Product.deleted_at.is_(None))
    )
    existing = {(p.category_id, p.name_normalized): p for p in result.scalars().all()}
    seen_ids: set[int] = set()

    sheet_categories = {sheet.title: await category_for([_category_title(sheet.title)]) for sheet in parsed.sheets
                        if any(p.category_path is None for p in sheet.products)}
    for sheet in parsed.sheets:
        for index, parsed_product in enumerate(sheet.products):
            category = (
                await category_for(parsed_product.category_path) if parsed_product.category_path
                else sheet_categories[sheet.title]
            )
            product = existing.get((category.id, normalize_name(parsed_product.name)))
            fields = dict(
                category_id=category.id,
                brand_id=await brand_id(parsed_product.brand),
                description=parsed_product.description,
                sku=parsed_product.sku,
                is_visible=True,
                sort_order=index,
            )
            if product is None:
                product = await repo.product.create(tenant.id, name=parsed_product.name, **fields)
                stats.products_created += 1
            else:
                for key, value in fields.items():
                    setattr(product, key, value)
                product.updated_at = utcnow()
                stats.products_updated += 1
            seen_ids.add(product.id)
            variant_ids = await _sync_variants(repo, tenant, product, parsed_product, stats)
            rows = [(None, tier_by_threshold[t], amount) for t, amount in parsed_product.prices.items()]
            for parsed_variant, variant_id in zip(parsed_product.variants, variant_ids):
                for t, amount in (parsed_variant.prices or {}).items():
                    rows.append((variant_id, tier_by_threshold[t], amount))
            await repo.price.replace_for_product(tenant.id, product.id, rows)
            if storage is not None and parsed_product.image:
                await _maybe_add_photo(repo, tenant, product, [parsed_product.image, *parsed_product.images], storage, stats)

    if hide_missing:
        for product in existing.values():
            if product.id not in seen_ids and product.is_visible:
                product.is_visible = False
                stats.products_hidden += 1

    await repo.tenant.touch_catalog(tenant.id)
    await repo.audit.log(tenant.id, None, "import", "price", None, {
        "products_created": stats.products_created, "products_updated": stats.products_updated,
        "products_hidden": stats.products_hidden, "wipe": wipe,
    })
    return stats


async def _sync_variants(repo: Repository, tenant: Tenant, product: Product, parsed: ParsedProduct,
                         stats: ImportStats) -> list[int]:
    """Возвращает id вариантов в порядке parsed.variants (для своих цен вариантов)"""
    result = await repo.session.execute(select(Variant).where(Variant.product_id == product.id))
    variants = list(result.scalars().all())
    default = next((v for v in variants if v.is_default), None)
    named = {normalize_name(v.name): v for v in variants if not v.is_default and v.name}

    def set_stock(variant: Variant, out: bool, stock_qty: int | None = None) -> None:
        if stock_qty is not None:  # остаток из учётной системы — статус по порогу «мало»
            variant.stock_qty = stock_qty
            variant.stock_status = stock_status_from_qty(stock_qty, tenant.low_stock_threshold)
        elif variant.stock_qty is None:  # остаток числом ведут вручную — наличие из прайса не трогаем
            variant.stock_status = StockStatus.out if out else StockStatus.in_stock

    if default is None:
        default = await repo.variant.create(tenant.id, product.id, is_default=True, sort_order=-1)

    if not parsed.variants:
        default.is_visible = True
        set_stock(default, parsed.out, parsed.stock_qty)
        for variant in named.values():
            if variant.is_visible:
                variant.is_visible = False
                stats.variants_hidden += 1
        return []

    default.is_visible = False
    keep: set[int] = set()
    ordered_ids: list[int] = []
    for index, parsed_variant in enumerate(parsed.variants):
        key = normalize_name(parsed_variant.name)
        variant = named.get(key)
        if variant is None:
            variant = await repo.variant.create(tenant.id, product.id, name=parsed_variant.name, sort_order=index)
            named[key] = variant
            stats.variants_created += 1
        variant.is_visible = True
        variant.sort_order = index
        if parsed_variant.sku:
            variant.sku = parsed_variant.sku
        set_stock(variant, parsed_variant.out or parsed.out, parsed_variant.stock_qty)
        keep.add(variant.id)
        ordered_ids.append(variant.id)
    for variant in named.values():
        if variant.id not in keep and variant.is_visible:
            variant.is_visible = False
            stats.variants_hidden += 1
    return ordered_ids


async def _maybe_add_photo(repo: Repository, tenant: Tenant, product: Product, images: list[bytes],
                           storage: LocalStorage, stats: ImportStats) -> None:
    """Фото из прайса — только товару без фото (загруженные вручную не трогаем), до 5 штук"""
    count = (await repo.session.execute(select(ProductPhoto.id).where(ProductPhoto.product_id == product.id))).all()
    if count:
        return
    for order, image in enumerate(images[:5]):
        try:
            content = process_image(image)
        except InvalidImage:
            stats.photo_errors += 1
            continue
        key = build_image_key(tenant.id, f"products/{product.id}")
        storage.save(key, content)
        await repo.photo.create(product.id, key, order)
        stats.photos_added += 1
