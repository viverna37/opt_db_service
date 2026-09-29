from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MemberStatus, Order, Role, STAFF_ROLES, TenantUser, TgUser


class TenantUserRepository:
    """Участники тенанта — клиенты и сотрудники"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, tenant_id: int, tenant_user_id: int) -> TenantUser | None:
        result = await self.session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.id == tenant_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_tg_user(self, tenant_id: int, tg_user_id: int) -> TenantUser | None:
        result = await self.session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.tg_user_id == tg_user_id)
        )
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, tg_user_id: int, role: Role, status: MemberStatus) -> TenantUser:
        member = TenantUser(tenant_id=tenant_id, tg_user_id=tg_user_id, role=role, status=status)
        self.session.add(member)
        await self.session.flush()
        return await self.get(tenant_id, member.id)

    async def list_staff(self, tenant_id: int) -> Sequence[TenantUser]:
        """Активные сотрудники — адресаты уведомлений о новых заявках"""
        result = await self.session.execute(
            select(TenantUser).where(
                TenantUser.tenant_id == tenant_id,
                TenantUser.role.in_(STAFF_ROLES),
                TenantUser.status == MemberStatus.active,
            )
        )
        return result.scalars().all()

    async def list_members(
        self,
        tenant_id: int,
        q: str | None = None,
        status: MemberStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple[TenantUser, int]], int]:
        """Раздел «Клиенты»: участники + число их заявок, свежие по последнему визиту сверху"""
        orders_count = (
            select(func.count(Order.id)).where(Order.tenant_user_id == TenantUser.id).correlate(TenantUser)
            .scalar_subquery()
        )
        query = select(TenantUser, orders_count).join(TgUser, TgUser.id == TenantUser.tg_user_id).where(
            TenantUser.tenant_id == tenant_id
        )
        if status is not None:
            query = query.where(TenantUser.status == status)
        if role is not None:
            query = query.where(TenantUser.role == role)
        if q:
            pattern = f"%{q.strip().lstrip('@').lower()}%"
            query = query.where(
                or_(
                    func.lower(TgUser.username).like(pattern),
                    func.lower(TgUser.first_name).like(pattern),
                    func.lower(TgUser.last_name).like(pattern),
                    func.lower(TenantUser.note).like(pattern),
                )
            )
        total = (await self.session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        result = await self.session.execute(
            query.order_by(TenantUser.last_seen.desc(), TenantUser.id.desc()).limit(limit).offset(offset)
        )
        return [(member, count) for member, count in result.all()], total

    async def count_pending(self, tenant_id: int) -> int:
        result = await self.session.execute(
            select(func.count(TenantUser.id)).where(
                TenantUser.tenant_id == tenant_id, TenantUser.status == MemberStatus.pending
            )
        )
        return result.scalar_one()
