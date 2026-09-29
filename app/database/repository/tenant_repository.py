from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Order, Product, Role, Tenant, TenantUser, TgUser
from app.utils.dt import utcnow


class TenantRepository:
    """Репозиторий оптовиков (тенантов)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, tenant_id: int) -> Tenant | None:
        return await self.session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[Tenant]:
        result = await self.session.execute(select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id.desc()))
        return result.scalars().all()

    async def platform_stats(self) -> dict[int, dict]:
        """Раздел «Платформа»: по каждому тенанту — товары, клиенты, заявки, последняя заявка, владелец"""
        stats: dict[int, dict] = {}

        def bucket(tenant_id: int) -> dict:
            return stats.setdefault(tenant_id, {
                "products_count": 0, "clients_count": 0, "orders_count": 0, "last_order_at": None, "owner": None,
            })

        rows = await self.session.execute(
            select(Product.tenant_id, func.count(Product.id)).where(Product.deleted_at.is_(None)).group_by(Product.tenant_id)
        )
        for tenant_id, count in rows.all():
            bucket(tenant_id)["products_count"] = count
        rows = await self.session.execute(
            select(TenantUser.tenant_id, func.count(TenantUser.id)).where(TenantUser.role == Role.client)
            .group_by(TenantUser.tenant_id)
        )
        for tenant_id, count in rows.all():
            bucket(tenant_id)["clients_count"] = count
        rows = await self.session.execute(
            select(Order.tenant_id, func.count(Order.id), func.max(Order.created_at)).group_by(Order.tenant_id)
        )
        for tenant_id, count, last in rows.all():
            bucket(tenant_id).update(orders_count=count, last_order_at=last)
        rows = await self.session.execute(
            select(TenantUser.tenant_id, TgUser).join(TgUser, TgUser.id == TenantUser.tg_user_id)
            .where(TenantUser.role == Role.owner)
        )
        for tenant_id, owner in rows.all():
            bucket(tenant_id)["owner"] = owner
        return stats

    async def list_active(self) -> Sequence[Tenant]:
        result = await self.session.execute(select(Tenant).where(Tenant.is_active.is_(True)).order_by(Tenant.id))
        return result.scalars().all()

    async def create(self, **fields) -> Tenant:
        tenant = Tenant(**fields)
        self.session.add(tenant)
        await self.session.flush()
        return tenant

    async def update(self, tenant_id: int, **values) -> None:
        if values:
            await self.session.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))

    async def next_order_number(self, tenant_id: int) -> int:
        """Атомарный инкремент сквозного номера заявки — две одновременные отправки не получат один номер"""
        result = await self.session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(last_order_number=Tenant.last_order_number + 1)
            .returning(Tenant.last_order_number)
        )
        return result.scalar_one()

    async def touch_catalog(self, tenant_id: int) -> None:
        """«Обновлено сегодня» на главной"""
        await self.update(tenant_id, catalog_updated_at=utcnow())
