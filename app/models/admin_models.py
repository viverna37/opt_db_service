from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models import (
    AccessMode,
    AttributeScope,
    AttributeType,
    MemberStatus,
    Role,
    StockStatus,
)
from app.models.cart_models import CartResponse
from app.models.catalog_models import ProductListItem
from app.models.common_models import TgUserResponse
from app.models.order_models import OrderClient

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


# ---------- Сводка / корзины ----------

class AdminSummaryResponse(BaseModel):
    products_total: int
    new_orders: int
    in_progress_orders: int
    live_carts: int
    out_of_stock_products: int
    pending_clients: int


class LiveCartResponse(BaseModel):
    cart_id: int
    client: OrderClient
    positions: int
    total_qty: int
    total: int
    updated_at: datetime
    reminded_at: Optional[datetime] = None
    can_remind: bool


class LiveCartDetailResponse(LiveCartResponse):
    cart: CartResponse


# ---------- Клиенты ----------

class ClientResponse(BaseModel):
    tenant_user_id: int
    role: Role
    status: MemberStatus
    note: Optional[str] = None
    user: TgUserResponse
    contact_url: str
    first_seen: datetime
    last_seen: datetime
    age_confirmed_at: Optional[datetime] = None
    orders_count: int


class ClientUpdateRequest(BaseModel):
    """Заметку и статус меняет менеджер; роль — только владелец (см. admin_clients_router)"""
    note: Optional[str] = Field(None, max_length=2000)
    status: Optional[MemberStatus] = None
    role: Optional[Role] = None


# ---------- Товары ----------

class ProductWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    sku: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=10000)
    attributes: dict[str, Any] = {}
    is_visible: bool = True
    sort_order: int = 0

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название не может быть пустым")
        return value


class ProductUpdateRequest(BaseModel):
    """PATCH: только переданные поля (exclude_unset)"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    sku: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=10000)
    attributes: Optional[dict[str, Any]] = None
    is_visible: Optional[bool] = None
    sort_order: Optional[int] = None


class VariantWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    sku: Optional[str] = Field(None, max_length=100)
    attributes: dict[str, Any] = {}
    stock_qty: Optional[int] = Field(None, ge=0)
    stock_status: StockStatus = StockStatus.in_stock
    is_visible: bool = True
    sort_order: int = 0


class VariantUpdateRequest(BaseModel):
    """
    Наличие: stock_qty числом — статус считается сам по порогу «мало»
    тенанта; stock_status явно (быстрый тумблер) — ручной режим, stock_qty
    сбрасывается в null.
    """
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    sku: Optional[str] = Field(None, max_length=100)
    attributes: Optional[dict[str, Any]] = None
    stock_qty: Optional[int] = Field(None, ge=0)
    stock_status: Optional[StockStatus] = None
    is_visible: Optional[bool] = None
    sort_order: Optional[int] = None


class ProductStockRequest(BaseModel):
    """Быстрый тумблер наличия всего товара из списка — применяется ко всем видимым вариантам"""
    stock_status: StockStatus


class PriceRow(BaseModel):
    tier_id: int
    variant_id: Optional[int] = None  # None — цена товара, иначе переопределение для варианта
    amount: int = Field(..., ge=0)  # копейки


class PricesUpdateRequest(BaseModel):
    prices: list[PriceRow]


class PhotoResponse(BaseModel):
    id: int
    url: str


class AdminVariantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: Optional[str] = None
    sku: Optional[str] = None
    attributes: dict[str, Any]
    stock_qty: Optional[int] = None
    stock_status: StockStatus
    is_default: bool
    is_visible: bool
    sort_order: int


class AdminProductResponse(BaseModel):
    id: int
    name: str
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    attributes: dict[str, Any]
    is_visible: bool
    sort_order: int
    photos: list[PhotoResponse]
    variants: list[AdminVariantResponse]
    prices: list[PriceRow]
    created_at: datetime
    updated_at: datetime


class AdminProductListItem(ProductListItem):
    is_visible: bool


# ---------- Справочники ----------

class CategoryWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    parent_id: Optional[int] = None
    sort_order: int = 0
    is_visible: bool = True


class CategoryUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None


class CategoryAttributesRequest(BaseModel):
    attribute_ids: list[int]


class AttributeWriteRequest(BaseModel):
    key: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(..., min_length=1, max_length=100)
    type: AttributeType
    unit: Optional[str] = Field(None, max_length=20)
    options: Optional[list[str]] = None
    scope: AttributeScope = AttributeScope.product
    filterable: bool = False
    show_in_list: bool = False
    sort_order: int = 0


class AttributeUpdateRequest(BaseModel):
    """key и type не меняются — на них завязаны уже сохранённые значения у товаров"""
    label: Optional[str] = Field(None, min_length=1, max_length=100)
    unit: Optional[str] = Field(None, max_length=20)
    options: Optional[list[str]] = None
    filterable: Optional[bool] = None
    show_in_list: Optional[bool] = None
    sort_order: Optional[int] = None


class BrandWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    sort_order: int = 0


class PriceTierWriteRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=50)
    min_qty: int = Field(..., ge=1)
    sort_order: int = 0


class PriceTierUpdateRequest(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=50)
    min_qty: Optional[int] = Field(None, ge=1)
    sort_order: Optional[int] = None


# ---------- Настройки ----------

class TenantSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    logo_url: Optional[str] = None
    accent_color: str
    currency: str
    timezone: str
    manager_username: Optional[str] = None
    low_stock_threshold: int
    access_mode: AccessMode
    age_gate: bool
    min_order_amount: Optional[int] = None
    welcome_text: Optional[str] = None
    bot_username: Optional[str] = None
    bot_configured: bool


class TenantSettingsUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    accent_color: Optional[str] = Field(None, pattern=HEX_COLOR)
    currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")
    timezone: Optional[str] = Field(None, max_length=50)
    manager_username: Optional[str] = Field(None, max_length=100)
    low_stock_threshold: Optional[int] = Field(None, ge=0)
    access_mode: Optional[AccessMode] = None
    age_gate: Optional[bool] = None
    min_order_amount: Optional[int] = Field(None, ge=0)
    welcome_text: Optional[str] = Field(None, max_length=2000)


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_user_id: Optional[int] = None
    action: str
    entity: str
    entity_id: Optional[int] = None
    data: Optional[dict] = None
    created_at: datetime
