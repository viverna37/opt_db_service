from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Cart, CartItem, TenantUser
from app.utils.dt import utcnow


class CartRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_for_member(self, tenant_user_id: int) -> Cart | None:
        result = await self.session.execute(
            select(Cart).where(Cart.tenant_user_id == tenant_user_id).options(selectinload(Cart.items))
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, tenant_id: int, tenant_user_id: int) -> Cart:
        cart = await self.get_for_member(tenant_user_id)
        if cart:
            return cart
        cart = Cart(tenant_id=tenant_id, tenant_user_id=tenant_user_id)
        self.session.add(cart)
        await self.session.flush()
        return await self.get_for_member(tenant_user_id)

    async def set_qty(self, cart: Cart, variant_id: int, qty: int) -> None:
        """qty=0 — убрать позицию"""
        existing = next((item for item in cart.items if item.variant_id == variant_id), None)
        if qty <= 0:
            if existing:
                await self.session.delete(existing)
        elif existing:
            existing.qty = qty
        else:
            self.session.add(CartItem(cart_id=cart.id, variant_id=variant_id, qty=qty))
        cart.updated_at = utcnow()
        await self.session.flush()
        await self.session.refresh(cart, attribute_names=["items"])

    async def clear(self, cart: Cart) -> None:
        await self.session.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
        cart.updated_at = utcnow()
        await self.session.flush()
        await self.session.refresh(cart, attribute_names=["items"])

    async def mark_reminded(self, cart: Cart, at: datetime) -> None:
        cart.reminded_at = at
        await self.session.flush()

    async def list_live(self, tenant_id: int, limit: int = 100) -> Sequence[Cart]:
        """«Живые корзины»: непустые, свежие сверху"""
        non_empty = select(CartItem.cart_id).distinct()
        result = await self.session.execute(
            select(Cart)
            .where(Cart.tenant_id == tenant_id, Cart.id.in_(non_empty))
            .options(selectinload(Cart.items))
            .order_by(Cart.updated_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def count_live(self, tenant_id: int) -> int:
        result = await self.session.execute(
            select(func.count(func.distinct(Cart.id)))
            .join(CartItem, CartItem.cart_id == Cart.id)
            .where(Cart.tenant_id == tenant_id)
        )
        return result.scalar_one()

    async def get(self, tenant_id: int, cart_id: int) -> Cart | None:
        result = await self.session.execute(
            select(Cart).where(Cart.tenant_id == tenant_id, Cart.id == cart_id).options(selectinload(Cart.items))
        )
        return result.scalar_one_or_none()

    async def members(self, member_ids: list[int]) -> dict[int, TenantUser]:
        if not member_ids:
            return {}
        result = await self.session.execute(select(TenantUser).where(TenantUser.id.in_(member_ids)))
        return {member.id: member for member in result.scalars().all()}
