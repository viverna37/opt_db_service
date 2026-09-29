from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AttributeDefinition,
    AttributeScope,
    AttributeType,
    Price,
    Product,
    ProductPhoto,
    StockStatus,
    Variant,
)
from app.services.pricing import PriceTable
from app.utils.text import normalize_name


@dataclass
class ProductFilter:
    """Фильтры списка товаров — общие для каталога клиента и списка в админке"""
    category_ids: list[int] | None = None  # категория вместе с поддеревом
    q: str | None = None
    brand_ids: list[int] | None = None
    in_stock_only: bool = False
    # [(определение атрибута, выбранные значения строками из query-string)]
    attributes: list[tuple[AttributeDefinition, list[str]]] = field(default_factory=list)
    only_visible: bool = True  # клиенту — только видимые; админ видит и скрытые
    stock_status: StockStatus | None = None  # админка: «нет в наличии» и т.п.


SORTS = ("default", "name", "price_asc", "price_desc", "new")


def _attribute_condition(column, definition: AttributeDefinition, values: list[str]):
    """Сравнение значения из JSON attributes по типу атрибута (number хранится числом, bool — true/false)"""
    element = column[definition.key]
    if definition.type == AttributeType.number:
        numbers = []
        for value in values:
            try:
                numbers.append(float(value.replace(",", ".")))
            except ValueError:
                continue
        return element.as_float().in_(numbers) if numbers else None
    if definition.type == AttributeType.bool:
        flags = {value.lower() in ("1", "true", "yes", "да") for value in values}
        return element.as_boolean().in_(list(flags))
    return element.as_string().in_(values)


def _visible_variant_clause():
    return and_(Variant.product_id == Product.id, Variant.is_visible.is_(True))


class ProductRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _base_query(self, tenant_id: int, filters: ProductFilter):
        query = select(Product).where(Product.tenant_id == tenant_id, Product.deleted_at.is_(None))
        if filters.only_visible:
            query = query.where(Product.is_visible.is_(True))
        if filters.category_ids is not None:
            query = query.where(Product.category_id.in_(filters.category_ids))
        if filters.brand_ids:
            query = query.where(Product.brand_id.in_(filters.brand_ids))
        if filters.q and filters.q.strip():
            needle = normalize_name(filters.q)
            pattern = f"%{needle}%"
            query = query.where(
                or_(
                    Product.name_normalized.like(pattern),
                    func.lower(Product.sku).like(pattern),
                    exists().where(
                        _visible_variant_clause(),
                        or_(func.lower(Variant.name).like(pattern), func.lower(Variant.sku).like(pattern)),
                    ),
                )
            )
        if filters.in_stock_only:
            query = query.where(exists().where(_visible_variant_clause(), Variant.stock_status != StockStatus.out))
        if filters.stock_status is not None:
            query = query.where(
                exists().where(_visible_variant_clause(), Variant.stock_status == filters.stock_status)
            )
        for definition, values in filters.attributes:
            if definition.scope == AttributeScope.product:
                condition = _attribute_condition(Product.attributes, definition, values)
                if condition is not None:
                    query = query.where(condition)
            else:
                condition = _attribute_condition(Variant.attributes, definition, values)
                if condition is not None:
                    query = query.where(exists().where(_visible_variant_clause(), condition))
        return query

    async def search(
        self, tenant_id: int, filters: ProductFilter, sort: str = "default", limit: int = 30, offset: int = 0,
    ) -> tuple[Sequence[Product], int]:
        query = self._base_query(tenant_id, filters)
        total = (await self.session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

        if sort in ("price_asc", "price_desc"):
            min_price = (
                select(func.min(Price.amount)).where(Price.product_id == Product.id).correlate(Product)
                .scalar_subquery()
            )
            order = min_price.asc() if sort == "price_asc" else min_price.desc()
            query = query.order_by(order.nulls_last(), Product.id)
        elif sort == "name":
            query = query.order_by(Product.name_normalized, Product.id)
        elif sort == "new":
            query = query.order_by(Product.created_at.desc(), Product.id.desc())
        else:
            query = query.order_by(Product.sort_order, Product.name_normalized, Product.id)

        result = await self.session.execute(
            query.options(selectinload(Product.variants), selectinload(Product.photos)).limit(limit).offset(offset)
        )
        return result.scalars().unique().all(), total

    async def get(self, tenant_id: int, product_id: int, include_deleted: bool = False) -> Product | None:
        query = (
            select(Product)
            .where(Product.tenant_id == tenant_id, Product.id == product_id)
            .options(selectinload(Product.variants), selectinload(Product.photos))
            # коллекции variants/photos могли измениться в этой же сессии мимо загруженного объекта
            .execution_options(populate_existing=True)
        )
        if not include_deleted:
            query = query.where(Product.deleted_at.is_(None))
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_many(self, tenant_id: int, product_ids: list[int]) -> dict[int, Product]:
        if not product_ids:
            return {}
        result = await self.session.execute(
            select(Product)
            .where(Product.tenant_id == tenant_id, Product.id.in_(product_ids))
            .options(selectinload(Product.variants), selectinload(Product.photos))
        )
        return {product.id: product for product in result.scalars().unique().all()}

    async def create(self, tenant_id: int, **fields) -> Product:
        product = Product(tenant_id=tenant_id, name_normalized=normalize_name(fields["name"]), **fields)
        self.session.add(product)
        await self.session.flush()
        return product

    async def count_out_of_stock(self, tenant_id: int) -> int:
        """Сводка админки: видимые товары, у которых нет ни одного варианта в наличии"""
        in_stock = exists().where(_visible_variant_clause(), Variant.stock_status != StockStatus.out)
        result = await self.session.execute(
            select(func.count(Product.id)).where(
                Product.tenant_id == tenant_id,
                Product.deleted_at.is_(None),
                Product.is_visible.is_(True),
                ~in_stock,
            )
        )
        return result.scalar_one()

    async def count_per_category(self, tenant_id: int, only_visible: bool = True) -> dict[int, int]:
        query = select(Product.category_id, func.count(Product.id)).where(
            Product.tenant_id == tenant_id, Product.deleted_at.is_(None), Product.category_id.is_not(None)
        )
        if only_visible:
            query = query.where(Product.is_visible.is_(True))
        result = await self.session.execute(query.group_by(Product.category_id))
        return {category_id: count for category_id, count in result.all()}

    async def attribute_values(
        self, tenant_id: int, category_ids: list[int], definitions: list[AttributeDefinition]
    ) -> dict[str, list]:
        """Значения для чипов-фильтров категории — собираются из того, что реально есть у товаров"""
        product_keys = [d.key for d in definitions if d.scope == AttributeScope.product]
        variant_keys = [d.key for d in definitions if d.scope == AttributeScope.variant]
        values: dict[str, set] = {d.key: set() for d in definitions}
        base = [
            Product.tenant_id == tenant_id,
            Product.deleted_at.is_(None),
            Product.is_visible.is_(True),
            Product.category_id.in_(category_ids),
        ]
        if product_keys:
            for (attrs,) in (await self.session.execute(select(Product.attributes).where(*base))).all():
                for key in product_keys:
                    if attrs and attrs.get(key) not in (None, ""):
                        values[key].add(attrs[key])
        if variant_keys:
            rows = await self.session.execute(
                select(Variant.attributes).join(Product, Product.id == Variant.product_id)
                .where(*base, Variant.is_visible.is_(True))
            )
            for (attrs,) in rows.all():
                for key in variant_keys:
                    if attrs and attrs.get(key) not in (None, ""):
                        values[key].add(attrs[key])
        return {key: sorted(found, key=lambda v: (isinstance(v, str), v)) for key, found in values.items()}


class VariantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, tenant_id: int, variant_id: int) -> Variant | None:
        result = await self.session.execute(
            select(Variant).where(Variant.tenant_id == tenant_id, Variant.id == variant_id)
            .options(selectinload(Variant.product))
        )
        return result.scalar_one_or_none()

    async def get_many(self, tenant_id: int, variant_ids: list[int]) -> dict[int, Variant]:
        if not variant_ids:
            return {}
        result = await self.session.execute(
            select(Variant).where(Variant.tenant_id == tenant_id, Variant.id.in_(variant_ids))
        )
        return {variant.id: variant for variant in result.scalars().all()}

    async def create(self, tenant_id: int, product_id: int, **fields) -> Variant:
        variant = Variant(tenant_id=tenant_id, product_id=product_id, **fields)
        self.session.add(variant)
        await self.session.flush()
        return variant


class PriceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def tables(self, product_ids: list[int]) -> dict[int, PriceTable]:
        """Цены пачки товаров в виде таблиц для app/services/pricing.py"""
        tables: dict[int, PriceTable] = {product_id: PriceTable() for product_id in product_ids}
        if not product_ids:
            return tables
        result = await self.session.execute(select(Price).where(Price.product_id.in_(product_ids)))
        for price in result.scalars().all():
            table = tables[price.product_id]
            if price.variant_id is None:
                table.product[price.tier_id] = price.amount
            else:
                table.variants.setdefault(price.variant_id, {})[price.tier_id] = price.amount
        return tables

    async def replace_for_product(self, tenant_id: int, product_id: int, rows: list[tuple[int | None, int, int]]) -> None:
        """Полная замена цен товара: rows = [(variant_id | None, tier_id, amount)]"""
        await self.session.execute(delete(Price).where(Price.product_id == product_id))
        seen: set[tuple[int | None, int]] = set()
        for variant_id, tier_id, amount in rows:
            if (variant_id, tier_id) in seen:
                continue
            seen.add((variant_id, tier_id))
            self.session.add(
                Price(tenant_id=tenant_id, product_id=product_id, variant_id=variant_id, tier_id=tier_id, amount=amount)
            )
        await self.session.flush()


class PhotoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, product_id: int, storage_key: str, sort_order: int) -> ProductPhoto:
        photo = ProductPhoto(product_id=product_id, storage_key=storage_key, sort_order=sort_order)
        self.session.add(photo)
        await self.session.flush()
        return photo

    async def delete(self, photo: ProductPhoto) -> None:
        await self.session.delete(photo)
        await self.session.flush()
