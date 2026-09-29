from datetime import datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

from app.database.models import AccessMode, MemberStatus, PriceBasis, Role

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class TgUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    telegram_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None


class TenantPublicResponse(BaseModel):
    """Брендинг витрины — без цен, отдаётся без авторизации"""
    slug: str
    name: str
    logo_url: Optional[str] = None
    accent_color: str
    currency: str
    welcome_text: Optional[str] = None
    bot_username: Optional[str] = None


class TenantResponse(TenantPublicResponse):
    """Настройки тенанта, нужные клиентскому фронту"""
    manager_username: Optional[str] = None
    access_mode: AccessMode
    age_gate: bool
    price_basis: PriceBasis
    min_order_amount: Optional[int] = None
    low_stock_threshold: int
    catalog_updated_at: datetime


class MeResponse(BaseModel):
    """
    Открытие мини-аппа. access говорит фронту, какой экран показать:
    ok | age_required | pending | blocked (для сотрудников всегда ok).
    """
    id: int
    role: Role
    status: MemberStatus
    is_staff: bool
    is_platform_admin: bool  # показывать ли вход в раздел «Платформа»
    access: str
    age_confirmed_at: Optional[datetime] = None
    user: TgUserResponse
    tenant: TenantResponse
    cart_qty: int
