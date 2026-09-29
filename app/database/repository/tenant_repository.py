from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Tenant
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
