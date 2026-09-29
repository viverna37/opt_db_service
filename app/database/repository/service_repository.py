"""Служебные таблицы: очередь уведомлений и аудит-лог"""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuditLog, Notification, NotificationType


class NotificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        tenant_id: int,
        tg_user_id: int,
        notif_type: NotificationType,
        text: str,
        reply_markup: dict | None = None,
        related_order_id: int | None = None,
    ) -> Notification:
        notification = Notification(
            tenant_id=tenant_id, tg_user_id=tg_user_id, notif_type=notif_type, text=text,
            reply_markup=reply_markup, related_order_id=related_order_id,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def list_unsent(self, limit: int = 100) -> Sequence[Notification]:
        result = await self.session.execute(
            select(Notification).where(Notification.is_sent.is_(False))
            .order_by(Notification.created_at.asc(), Notification.id.asc()).limit(limit)
        )
        return result.scalars().all()

    async def mark_sent(self, notification_id: int) -> None:
        await self.session.execute(
            update(Notification).where(Notification.id == notification_id).values(is_sent=True, sent_at=func.now())
        )


class AuditRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(self, tenant_id: int, tenant_user_id: int | None, action: str, entity: str,
                  entity_id: int | None = None, data: dict | None = None) -> None:
        self.session.add(AuditLog(tenant_id=tenant_id, tenant_user_id=tenant_user_id, action=action, entity=entity,
                                  entity_id=entity_id, data=data))
        await self.session.flush()

    async def list(self, tenant_id: int, limit: int = 100, offset: int = 0) -> Sequence[AuditLog]:
        result = await self.session.execute(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all()
