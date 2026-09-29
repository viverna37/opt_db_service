from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.database.models import StockStatus
from app.models.catalog_models import TierPrice


class CartLine(BaseModel):
    variant_id: int
    variant_name: Optional[str] = None
    sku: Optional[str] = None
    qty: int
    unit_price: Optional[int] = None
    amount: Optional[int] = None
    stock_status: StockStatus
    stock_qty: Optional[int] = None
    # None — всё хорошо; unavailable — нет в наличии/скрыт (подсветить); no_price — цена не задана
    problem: Optional[str] = None


class CartGroup(BaseModel):
    product_id: int
    product_name: str
    cover_url: Optional[str] = None
    total_qty: int
    tier: Optional[TierPrice] = None
    next_tier: Optional[TierPrice] = None
    qty_to_next_tier: Optional[int] = None  # «ещё 3 шт до цены от 10»
    subtotal: int
    lines: list[CartLine]


class CartResponse(BaseModel):
    groups: list[CartGroup]
    total: int
    total_qty: int
    positions: int
    min_order_amount: Optional[int] = None
    can_submit: bool
    blockers: list[str]  # empty | unavailable_items | no_price_items | below_min_amount
    updated_at: Optional[datetime] = None


class CartItemUpdateRequest(BaseModel):
    qty: int = Field(..., ge=0, le=100000)


class CartSubmitRequest(BaseModel):
    comment: Optional[str] = Field(None, max_length=1000)
