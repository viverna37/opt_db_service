"""
CLI для того, что не делается из мини-аппа: заведение тенанта, бот-токен,
назначение владельца, демо-данные.

    python -m app.cli create-tenant --slug amigo --name "Amigo Opt" --bot-token 123:ABC --bot-username amigo_opt_bot
    python -m app.cli set-bot --slug amigo --bot-token 123:ABC --bot-username amigo_opt_bot
    python -m app.cli set-owner --slug amigo --telegram-id 123456789
    python -m app.cli seed-demo --slug demo --owner-telegram-id 123456789
    python -m app.cli import-price --slug amigo --file "AMIGO OPT.xlsx" [--wipe] [--no-photos] [--dry-run]

В докере: docker compose exec opt_catalog_db_service python -m app.cli ...
"""
import argparse
import asyncio
import sys

from app.config.config import get_cached_config
from app.database.db import db
from app.database.models import (
    AttributeScope,
    AttributeType,
    StockStatus,
)
from app.database.repository.main_repository import Repository
from app.services.tenant_service import make_owner, slug_problem


async def _with_repo(action, *args):
    await db.init(get_cached_config().db.url)
    await db.create_tables()
    try:
        async with db.session() as session:
            repo = Repository(session)
            await action(repo, *args)
            await repo.commit()
    finally:
        await db.close()


async def _tenant(repo: Repository, slug: str):
    tenant = await repo.tenant.get_by_slug(slug)
    if not tenant:
        sys.exit(f"Тенант {slug} не найден")
    return tenant


async def create_tenant(repo: Repository, args) -> None:
    if problem := slug_problem(args.slug):
        sys.exit(f"Некорректный slug: {problem}")
    if await repo.tenant.get_by_slug(args.slug):
        sys.exit(f"Тенант {args.slug} уже есть")
    tenant = await repo.tenant.create(slug=args.slug, name=args.name, bot_token=args.bot_token,
                                      bot_username=args.bot_username)
    print(f"Создан тенант id={tenant.id} slug={tenant.slug}")


async def set_bot(repo: Repository, args) -> None:
    tenant = await _tenant(repo, args.slug)
    await repo.tenant.update(tenant.id, bot_token=args.bot_token, bot_username=args.bot_username)
    print(f"Бот тенанта {args.slug} обновлён")


async def set_owner(repo: Repository, args) -> None:
    tenant = await _tenant(repo, args.slug)
    await make_owner(repo, tenant, args.telegram_id)
    print(f"telegram_id={args.telegram_id} — владелец тенанта {args.slug}")


async def seed_demo(repo: Repository, args) -> None:
    """Демо-оптовик: дерево категорий, атрибуты, 3 уровня цен, товары с вкусами/цветами и остатками"""
    if await repo.tenant.get_by_slug(args.slug):
        sys.exit(f"Тенант {args.slug} уже есть — демо сидится только в пустой")
    tenant = await repo.tenant.create(
        slug=args.slug, name="Демо Опт", manager_username="demo_manager", min_order_amount=300000,
        welcome_text="Оптовый каталог. Соберите заявку — менеджер свяжется с вами.",
        bot_token=args.bot_token, bot_username=args.bot_username,
    )
    t = tenant.id
    tiers = [
        await repo.price_tier.create(t, label="от 1 шт", min_qty=1, sort_order=0),
        await repo.price_tier.create(t, label="от 10 шт", min_qty=10, sort_order=1),
        await repo.price_tier.create(t, label="от 50 шт", min_qty=50, sort_order=2),
    ]
    attrs = {
        "volume": await repo.attribute.create(t, key="volume", label="Объём", type=AttributeType.number, unit="мл",
                                              filterable=True, show_in_list=True, sort_order=0),
        "nicotine": await repo.attribute.create(t, key="nicotine", label="Никотин", type=AttributeType.select,
                                                unit="мг", options=["20", "50"], filterable=True, show_in_list=True,
                                                sort_order=1),
        "puffs": await repo.attribute.create(t, key="puffs", label="Затяжек", type=AttributeType.number,
                                             filterable=True, show_in_list=True, sort_order=2),
        "battery": await repo.attribute.create(t, key="battery", label="Аккумулятор", type=AttributeType.number,
                                               unit="мАч", show_in_list=True, sort_order=3),
        "resistance": await repo.attribute.create(t, key="resistance", label="Сопротивление",
                                                  type=AttributeType.number, unit="Ом",
                                                  scope=AttributeScope.variant, sort_order=4),
        "color": await repo.attribute.create(t, key="color", label="Цвет", type=AttributeType.color,
                                             scope=AttributeScope.variant, sort_order=5),
    }
    liquids = await repo.category.create(t, name="Жидкости", sort_order=0)
    salt = await repo.category.create(t, name="Солевые", parent_id=liquids.id, sort_order=0)
    disposables = await repo.category.create(t, name="Одноразки", sort_order=1)
    pods = await repo.category.create(t, name="Под-системы", sort_order=2)
    coils = await repo.category.create(t, name="Испарители", sort_order=3)
    await repo.category.set_attributes(liquids.id, [attrs["volume"].id, attrs["nicotine"].id])
    await repo.category.set_attributes(disposables.id, [attrs["puffs"].id])
    await repo.category.set_attributes(pods.id, [attrs["battery"].id, attrs["color"].id])
    await repo.category.set_attributes(coils.id, [attrs["resistance"].id])

    brands = {name: await repo.brand.create(t, name=name) for name in ("Жнец", "Lost Mary", "Smoant", "Elf Bar")}

    async def product(name, category, brand, attributes, prices, variants, sku=None):
        p = await repo.product.create(t, name=name, category_id=category.id, brand_id=brands[brand].id,
                                      attributes=attributes, sku=sku)
        await repo.variant.create(t, p.id, is_default=True, is_visible=not variants, sort_order=-1)
        for index, (v_name, v_attrs, stock_qty, status) in enumerate(variants):
            await repo.variant.create(t, p.id, name=v_name, attributes=v_attrs, stock_qty=stock_qty,
                                      stock_status=status, sort_order=index)
        await repo.price.replace_for_product(t, p.id, [(None, tiers[i].id, a) for i, a in enumerate(prices)])

    ok, low, out = StockStatus.in_stock, StockStatus.low, StockStatus.out
    await product("Жнец 30 мл 50 мг", salt, "Жнец", {"volume": 30, "nicotine": "50"}, [26000, 25500, 24500], [
        ("Мятная жвачка айс", {}, None, ok), ("Манго айс", {}, None, ok), ("Кола айс", {}, None, low),
        ("Арбуз", {}, None, out),
    ], sku="ZH-30-50")
    await product("Lost Mary 30 мл 20 мг", salt, "Lost Mary", {"volume": 30, "nicotine": "20"}, [24000, 23500, 23000], [
        ("Черника земляника лёд", {}, None, ok), ("Дайкири лёд", {}, None, ok), ("Спрайт лёд", {}, None, ok),
    ])
    await product("Elf Bar 6000", disposables, "Elf Bar", {"puffs": 6000}, [45000, 44000, 43000], [
        ("Blue Razz Ice", {}, 40, ok), ("Watermelon", {}, 3, low), ("Strawberry Kiwi", {}, 0, out),
    ])
    await product("Smoant Charon Baby", pods, "Smoant", {"battery": 750}, [75000, 73000, 70000], [
        ("Black", {"color": "#000000"}, None, ok), ("Peacock Blue", {"color": "#1F6F8B"}, None, ok),
        ("Matte Rainbow", {"color": "#B06AB3"}, None, low),
    ])
    await product("Испаритель Charon Baby (3 шт)", coils, "Smoant", {}, [45000, 44000, 43000], [
        ("0,6 Ом", {"resistance": 0.6}, None, ok), ("1,2 Ом", {"resistance": 1.2}, None, ok),
    ])
    await product("Картридж Charon Baby", coils, "Smoant", {}, [28000, 27000, 26000], [])

    if args.owner_telegram_id:
        await make_owner(repo, tenant, args.owner_telegram_id)
    print(f"Демо-тенант создан: slug={args.slug} id={tenant.id}")


async def import_price(repo: Repository, args) -> None:
    """Блочный прайс (формат Amigo Opt) -> каталог тенанта, см. app/services/price_import.py"""
    from app.importer.block_price import parse_workbook
    from app.services.price_import import ImportError_, import_block_price
    from app.utils.file_storage import LocalStorage

    tenant = await _tenant(repo, args.slug)
    with open(args.file, "rb") as f:
        parsed = parse_workbook(f.read(), with_images=not args.no_photos)
    products = parsed.products
    print(f"Разобрано: листов {len(parsed.sheets)}, товаров {len(products)}, "
          f"вариантов {sum(len(p.variants) for p in products)}, фото {sum(1 for p in products if p.image)}")
    for warning in parsed.warnings:
        print(f"  ! {warning}")
    if args.dry_run:
        print("--dry-run: в базу ничего не записано")
        return
    storage = None if args.no_photos else LocalStorage(get_cached_config().uploads_dir)
    try:
        stats = await import_block_price(repo, tenant, parsed, storage, wipe=args.wipe)
    except ImportError_ as exc:
        sys.exit(str(exc))
    print(
        f"Готово: категорий +{stats.categories_created}, брендов +{stats.brands_created}, уровней +{stats.tiers_created}; "
        f"товаров +{stats.products_created} / обновлено {stats.products_updated} / скрыто {stats.products_hidden}; "
        f"вариантов +{stats.variants_created} / скрыто {stats.variants_hidden}; фото +{stats.photos_added}"
        + (f" (ошибок {stats.photo_errors})" if stats.photo_errors else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-tenant")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--bot-token")
    p.add_argument("--bot-username")
    p.set_defaults(action=create_tenant)

    p = sub.add_parser("set-bot")
    p.add_argument("--slug", required=True)
    p.add_argument("--bot-token", required=True)
    p.add_argument("--bot-username")
    p.set_defaults(action=set_bot)

    p = sub.add_parser("set-owner")
    p.add_argument("--slug", required=True)
    p.add_argument("--telegram-id", type=int, required=True)
    p.set_defaults(action=set_owner)

    p = sub.add_parser("seed-demo")
    p.add_argument("--slug", default="demo")
    p.add_argument("--owner-telegram-id", type=int)
    p.add_argument("--bot-token")
    p.add_argument("--bot-username")
    p.set_defaults(action=seed_demo)

    p = sub.add_parser("import-price")
    p.add_argument("--slug", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--wipe", action="store_true", help="удалить весь каталог тенанта перед импортом")
    p.add_argument("--no-photos", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="только разобрать и показать итог")
    p.set_defaults(action=import_price)

    args = parser.parse_args()
    asyncio.run(_with_repo(args.action, args))


if __name__ == "__main__":
    main()
