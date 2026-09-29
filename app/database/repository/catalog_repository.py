"""Справочники каталога: категории, бренды, атрибуты, уровни цен"""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AttributeDefinition,
    Brand,
    Category,
    CategoryAttribute,
    PriceTier,
    Product,
)


class CategoryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, tenant_id: int, only_visible: bool = False) -> Sequence[Category]:
        query = select(Category).where(Category.tenant_id == tenant_id)
        if only_visible:
            query = query.where(Category.is_visible.is_(True))
        result = await self.session.execute(query.order_by(Category.sort_order, Category.name))
        return result.scalars().all()

    async def get(self, tenant_id: int, category_id: int) -> Category | None:
        result = await self.session.execute(
            select(Category).where(Category.tenant_id == tenant_id, Category.id == category_id)
        )
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, **fields) -> Category:
        category = Category(tenant_id=tenant_id, **fields)
        self.session.add(category)
        await self.session.flush()
        return category

    async def has_children(self, category_id: int) -> bool:
        result = await self.session.execute(select(func.count(Category.id)).where(Category.parent_id == category_id))
        return result.scalar_one() > 0

    async def has_products(self, category_id: int) -> bool:
        result = await self.session.execute(
            select(func.count(Product.id)).where(Product.category_id == category_id, Product.deleted_at.is_(None))
        )
        return result.scalar_one() > 0

    async def delete(self, category: Category) -> None:
        await self.session.delete(category)
        await self.session.flush()

    async def attribute_ids(self, category_ids: list[int]) -> dict[int, list[int]]:
        if not category_ids:
            return {}
        result = await self.session.execute(
            select(CategoryAttribute).where(CategoryAttribute.category_id.in_(category_ids))
        )
        bound: dict[int, list[int]] = {}
        for row in result.scalars().all():
            bound.setdefault(row.category_id, []).append(row.attribute_id)
        return bound

    async def set_attributes(self, category_id: int, attribute_ids: list[int]) -> None:
        await self.session.execute(delete(CategoryAttribute).where(CategoryAttribute.category_id == category_id))
        for attribute_id in dict.fromkeys(attribute_ids):
            self.session.add(CategoryAttribute(category_id=category_id, attribute_id=attribute_id))
        await self.session.flush()


class BrandRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, tenant_id: int) -> Sequence[Brand]:
        result = await self.session.execute(
            select(Brand).where(Brand.tenant_id == tenant_id).order_by(Brand.sort_order, Brand.name)
        )
        return result.scalars().all()

    async def get(self, tenant_id: int, brand_id: int) -> Brand | None:
        result = await self.session.execute(select(Brand).where(Brand.tenant_id == tenant_id, Brand.id == brand_id))
        return result.scalar_one_or_none()

    async def get_by_name(self, tenant_id: int, name: str) -> Brand | None:
        result = await self.session.execute(select(Brand).where(Brand.tenant_id == tenant_id, Brand.name == name))
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, **fields) -> Brand:
        brand = Brand(tenant_id=tenant_id, **fields)
        self.session.add(brand)
        await self.session.flush()
        return brand

    async def is_used(self, brand_id: int) -> bool:
        result = await self.session.execute(
            select(func.count(Product.id)).where(Product.brand_id == brand_id, Product.deleted_at.is_(None))
        )
        return result.scalar_one() > 0

    async def delete(self, brand: Brand) -> None:
        await self.session.delete(brand)
        await self.session.flush()


class AttributeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, tenant_id: int) -> Sequence[AttributeDefinition]:
        result = await self.session.execute(
            select(AttributeDefinition)
            .where(AttributeDefinition.tenant_id == tenant_id)
            .order_by(AttributeDefinition.sort_order, AttributeDefinition.id)
        )
        return result.scalars().all()

    async def get(self, tenant_id: int, attribute_id: int) -> AttributeDefinition | None:
        result = await self.session.execute(
            select(AttributeDefinition).where(
                AttributeDefinition.tenant_id == tenant_id, AttributeDefinition.id == attribute_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_key(self, tenant_id: int, key: str) -> AttributeDefinition | None:
        result = await self.session.execute(
            select(AttributeDefinition).where(AttributeDefinition.tenant_id == tenant_id, AttributeDefinition.key == key)
        )
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, **fields) -> AttributeDefinition:
        attribute = AttributeDefinition(tenant_id=tenant_id, **fields)
        self.session.add(attribute)
        await self.session.flush()
        return attribute

    async def delete(self, attribute: AttributeDefinition) -> None:
        await self.session.delete(attribute)
        await self.session.flush()


class PriceTierRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, tenant_id: int) -> Sequence[PriceTier]:
        result = await self.session.execute(
            select(PriceTier).where(PriceTier.tenant_id == tenant_id)
            .order_by(func.coalesce(PriceTier.min_qty, PriceTier.min_amount), PriceTier.id)
        )
        return result.scalars().all()

    async def get(self, tenant_id: int, tier_id: int) -> PriceTier | None:
        result = await self.session.execute(
            select(PriceTier).where(PriceTier.tenant_id == tenant_id, PriceTier.id == tier_id)
        )
        return result.scalar_one_or_none()

    async def get_by_min_qty(self, tenant_id: int, min_qty: int) -> PriceTier | None:
        result = await self.session.execute(
            select(PriceTier).where(PriceTier.tenant_id == tenant_id, PriceTier.min_qty == min_qty)
        )
        return result.scalar_one_or_none()

    async def get_by_min_amount(self, tenant_id: int, min_amount: int) -> PriceTier | None:
        result = await self.session.execute(
            select(PriceTier).where(PriceTier.tenant_id == tenant_id, PriceTier.min_amount == min_amount)
        )
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, **fields) -> PriceTier:
        tier = PriceTier(tenant_id=tenant_id, **fields)
        self.session.add(tier)
        await self.session.flush()
        return tier

    async def delete(self, tier: PriceTier) -> None:
        """Цены этого уровня уходят вместе с ним (ondelete=CASCADE + явное удаление для sqlite)"""
        from app.database.models import Price

        await self.session.execute(delete(Price).where(Price.tier_id == tier.id))
        await self.session.delete(tier)
        await self.session.flush()
