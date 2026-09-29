from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from app.database.models import AttributeScope, AttributeType, StockStatus


class AttributeDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    label: str
    type: AttributeType
    unit: Optional[str] = None
    options: Optional[list[str]] = None
    scope: AttributeScope
    filterable: bool
    show_in_list: bool
    sort_order: int


class AttributeValue(BaseModel):
    """Значение атрибута для отображения (чипы на карточке, мета в списке)"""
    key: str
    label: str
    value: Any
    unit: Optional[str] = None
    type: AttributeType


class CategoryNode(BaseModel):
    id: int
    parent_id: Optional[int] = None
    name: str
    image_url: Optional[str] = None
    sort_order: int
    is_visible: bool
    product_count: int  # вместе с подкатегориями
    children: list["CategoryNode"] = []


class FilterOption(BaseModel):
    attribute: AttributeDefinitionResponse
    values: list[Any]


class BrandResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int


class CategoryDetailResponse(BaseModel):
    id: int
    parent_id: Optional[int] = None
    name: str
    breadcrumbs: list[dict]
    children: list[CategoryNode]
    attributes: list[AttributeDefinitionResponse]  # унаследованные от родителей тоже
    filters: list[FilterOption]  # filterable-атрибуты со значениями, реально встречающимися у товаров
    brands: list[BrandResponse]


class PriceTierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    min_qty: int
    sort_order: int


class TierPrice(BaseModel):
    tier_id: int
    label: str
    min_qty: int
    amount: Optional[int] = None


class ProductListItem(BaseModel):
    id: int
    name: str
    brand: Optional[str] = None
    category_id: Optional[int] = None
    sku: Optional[str] = None
    cover_url: Optional[str] = None
    meta: list[AttributeValue]  # show_in_list атрибуты
    price_from: Optional[int] = None
    stock_status: StockStatus  # сводный: in_stock если есть хоть что-то в наличии
    variants_count: int
    in_cart_qty: int


class VariantCard(BaseModel):
    id: int
    name: Optional[str] = None
    sku: Optional[str] = None
    is_default: bool
    attributes: list[AttributeValue]
    stock_status: StockStatus
    stock_qty: Optional[int] = None
    prices: list[TierPrice]
    in_cart_qty: int


class ProductCard(BaseModel):
    id: int
    name: str
    brand: Optional[str] = None
    category_id: Optional[int] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    photos: list[str]
    attributes: list[AttributeValue]
    tiers: list[TierPrice]  # цены товара по уровням
    current_tier_id: Optional[int] = None  # по количеству этого товара в корзине
    next_tier: Optional[TierPrice] = None
    qty_to_next_tier: Optional[int] = None
    in_cart_qty: int
    variants: list[VariantCard]
    has_variants: bool  # false — единственный скрытый вариант по умолчанию
    ask_manager_url: Optional[str] = None
    updated_at: datetime
