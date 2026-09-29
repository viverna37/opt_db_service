from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.common_models import TgUserResponse


class PlatformTenantResponse(BaseModel):
    id: int
    slug: str
    name: str
    is_active: bool
    bot_username: Optional[str] = None
    bot_configured: bool
    owner: Optional[TgUserResponse] = None
    products_count: int
    clients_count: int
    orders_count: int
    last_order_at: Optional[datetime] = None
    created_at: datetime
    catalog_url: Optional[str] = None  # мини-апп: {WEBAPP_BASE_URL}/t/{slug}
    bot_url: Optional[str] = None  # t.me/{bot_username}


class PlatformTenantSaved(PlatformTenantResponse):
    """После создания/изменения: что не получилось сделать автоматически (например, кнопку меню бота)"""
    warnings: list[str] = []


class PlatformTenantCreateRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=40)
    name: str = Field(..., min_length=1, max_length=150)
    bot_token: Optional[str] = Field(None, max_length=100)
    owner_telegram_id: Optional[int] = Field(None, gt=0)


class PlatformTenantUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    is_active: Optional[bool] = None
    bot_token: Optional[str] = Field(None, max_length=100)
    owner_telegram_id: Optional[int] = Field(None, gt=0)


class PlatformMeResponse(BaseModel):
    telegram_id: int
    webapp_configured: bool
    platform_bot_configured: bool
