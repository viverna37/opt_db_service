from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Order, OrderItem, OrderStatus, OrderStatusHistory


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _with_items(self, query):
        return query.options(selectinload(Order.items), selectinload(Order.history))

    async def create(self, order: Order, items: list[OrderItem], actor_id: int | None) -> Order:
        self.session.add(order)
        await self.session.flush()
        for item in items:
            item.order_id = order.id
            self.session.add(item)
        self.session.add(OrderStatusHistory(order_id=order.id, from_status=None, to_status=order.status,
                                            tenant_user_id=actor_id))
        await self.session.flush()
        return await self.get(order.tenant_id, order.id)

    async def get(self, tenant_id: int, order_id: int) -> Order | None:
        result = await self.session.execute(
            self._with_items(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id))
            # история/позиции могли пополниться в этой же сессии мимо загруженного объекта
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        tenant_id: int,
        tenant_user_id: int | None = None,
        status: OrderStatus | None = None,
        number: int | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Order], int]:
        query = select(Order).where(Order.tenant_id == tenant_id)
        if tenant_user_id is not None:
            query = query.where(Order.tenant_user_id == tenant_user_id)
        if status is not None:
            query = query.where(Order.status == status)
        if number is not None:
            query = query.where(Order.number == number)
        if created_from is not None:
            query = query.where(Order.created_at >= created_from)
        if created_to is not None:
            query = query.where(Order.created_at < created_to)
        total = (await self.session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        result = await self.session.execute(
            self._with_items(query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset))
        )
        return result.scalars().unique().all(), total

    async def count_by_status(self, tenant_id: int) -> dict[OrderStatus, int]:
        result = await self.session.execute(
            select(Order.status, func.count(Order.id)).where(Order.tenant_id == tenant_id).group_by(Order.status)
        )
        counts = {status: 0 for status in OrderStatus}
        counts.update({status: count for status, count in result.all()})
        return counts

    async def count_for_member(self, tenant_user_id: int) -> int:
        result = await self.session.execute(select(func.count(Order.id)).where(Order.tenant_user_id == tenant_user_id))
        return result.scalar_one()

    async def add_history(self, order_id: int, from_status: OrderStatus, to_status: OrderStatus,
                          actor_id: int | None) -> None:
        self.session.add(OrderStatusHistory(order_id=order_id, from_status=from_status, to_status=to_status,
                                            tenant_user_id=actor_id))
        await self.session.flush()
