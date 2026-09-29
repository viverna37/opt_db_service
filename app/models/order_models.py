from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import MemberStatus, OrderStatus, Role
from app.models.common_models import TgUserResponse


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    product_name: str
    variant_name: Optional[str] = None
    sku: Optional[str] = None
    qty: int
    price: int
    tier_label: Optional[str] = None
    amount: int


class OrderHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: Optional[OrderStatus] = None
    to_status: OrderStatus
    tenant_user_id: Optional[int] = None
    created_at: datetime


class OrderResponse(BaseModel):
    """Заявка глазами клиента — без manager_note"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: int
    status: OrderStatus
    comment: Optional[str] = None
    total: int
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemResponse]


class OrderClient(BaseModel):
    tenant_user_id: int
    role: Role
    status: MemberStatus
    note: Optional[str] = None
    user: TgUserResponse
    contact_url: str  # https://t.me/{username} или tg://user?id=


class AdminOrderResponse(OrderResponse):
    manager_note: Optional[str] = None
    client: OrderClient
    history: list[OrderHistoryResponse]


class OrderStatusUpdateRequest(BaseModel):
    status: OrderStatus


class OrderNoteUpdateRequest(BaseModel):
    manager_note: Optional[str] = Field(None, max_length=4000)


class RepeatOrderResponse(BaseModel):
    added: int
    skipped: list[str]  # названия позиций, которых больше нет/нет в наличии
