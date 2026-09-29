"""
Модели домена «ОптКаталог». Контекст и бизнес-правила — в CLAUDE.md.

Мультитенантность: каждый оптовик — Tenant, все бизнес-таблицы несут
tenant_id и каждый запрос фильтруется по нему (см. app/deps.py —
тенант определяется по заголовку X-Tenant, а не доверяется телу запроса).

Политика удаления: товары и варианты физически не удаляются — на варианты
ссылаются корзины, а заявки хранят снимок позиций (OrderItem), поэтому
товар «удаляется» через deleted_at, вариант — скрывается (is_visible=False).

Деньги — integer в копейках. Все даты — timestamptz, UTC.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.db import Base
from app.utils.dt import utcnow

# JSONB на Postgres (GIN-индекс по атрибутам), обычный JSON на тестовой sqlite
JsonB = JSON().with_variant(JSONB(), "postgresql")


# ---------- Enums ----------

class Role(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    manager = "manager"
    client = "client"


STAFF_ROLES = (Role.owner, Role.admin, Role.manager)


class MemberStatus(str, enum.Enum):
    active = "active"
    pending = "pending"  # access_mode=approval: ждёт одобрения админом
    blocked = "blocked"


class AccessMode(str, enum.Enum):
    open = "open"
    approval = "approval"


class AttributeType(str, enum.Enum):
    text = "text"
    number = "number"
    select = "select"
    bool = "bool"
    color = "color"


class AttributeScope(str, enum.Enum):
    product = "product"
    variant = "variant"


class StockStatus(str, enum.Enum):
    in_stock = "in_stock"
    low = "low"
    out = "out"


class OrderStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    done = "done"
    cancelled = "cancelled"


class NotificationType(str, enum.Enum):
    order_new_admin = "order_new_admin"  # админам: новая заявка
    order_copy_client = "order_copy_client"  # клиенту: копия его заявки
    cart_reminder = "cart_reminder"  # клиенту: напоминание о корзине


# ---------- Тенант ----------

class Tenant(Base):
    """
    Оптовик. Свой бот (bot_token — ключ проверки initData и отправки
    уведомлений), свой slug (мини-апп открывается по /t/{slug}) и настройки
    витрины.
    """
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    bot_token: Mapped[Optional[str]] = mapped_column(String(100))
    bot_username: Mapped[Optional[str]] = mapped_column(String(100))

    logo_key: Mapped[Optional[str]] = mapped_column(String(255))
    accent_color: Mapped[str] = mapped_column(String(9), default="#8BE0B4", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    # Только для отображения дат в текстах (копия заявки, «Скопировать текстом») — хранится всё в UTC
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Moscow", nullable=False)
    manager_username: Mapped[Optional[str]] = mapped_column(String(100))
    # stock_qty <= порога -> stock_status=low (если остаток задан числом)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    access_mode: Mapped[AccessMode] = mapped_column(
        SAEnum(AccessMode, name="access_mode"), default=AccessMode.open, nullable=False
    )
    age_gate: Mapped[bool] = mapped_column(default=True, nullable=False)
    min_order_amount: Mapped[Optional[int]] = mapped_column(Integer)  # копейки
    welcome_text: Mapped[Optional[str]] = mapped_column(Text)

    # Сквозной номер заявки внутри тенанта — инкрементится атомарным UPDATE ... RETURNING
    last_order_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # «Обновлено сегодня» на главной — трогается при любом изменении товаров/цен/наличия
    catalog_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------- Пользователи ----------

class TgUser(Base):
    """
    Глобальный Telegram-аккаунт (один на все тенанты). Регистрации нет —
    создаётся/обновляется из initData при каждом входе (app/deps.py).
    """
    __tablename__ = "tg_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    last_name: Mapped[Optional[str]] = mapped_column(String(100))
    username: Mapped[Optional[str]] = mapped_column(String(100))
    photo_url: Mapped[Optional[str]] = mapped_column(String(500))
    language_code: Mapped[Optional[str]] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TenantUser(Base):
    """
    Участник конкретного тенанта: клиент-магазин или сотрудник оптовика.
    Один TgUser может быть клиентом у нескольких оптовиков — это разные
    TenantUser со своими корзинами, заявками, заметками и блокировками.
    """
    __tablename__ = "tenant_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tg_user_id", name="uq_tenant_users_member"),
        Index("ix_tenant_users_tenant_role", "tenant_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    tg_user_id: Mapped[int] = mapped_column(ForeignKey("tg_users.id"), nullable=False)
    role: Mapped[Role] = mapped_column(SAEnum(Role, name="role"), default=Role.client, nullable=False)
    status: Mapped[MemberStatus] = mapped_column(
        SAEnum(MemberStatus, name="member_status"), default=MemberStatus.active, nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(Text)  # заметка менеджера, клиенту не видна
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    age_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    tg_user: Mapped["TgUser"] = relationship(lazy="joined")


# ---------- Справочники каталога ----------

class Brand(Base):
    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_brands_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Category(Base):
    """Дерево категорий (parent_id). Атрибуты привязываются через CategoryAttribute и наследуются потомками."""
    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_tenant_parent", "tenant_id", "parent_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    image_key: Mapped[Optional[str]] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_visible: Mapped[bool] = mapped_column(default=True, nullable=False)


class AttributeDefinition(Base):
    """
    Динамическая характеристика (EAV-lite). Значения лежат в JSON
    attributes у Product/Variant под ключом key, тип значения — по type
    (number -> число, bool -> true/false, остальное -> строка).
    """
    __tablename__ = "attribute_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_attribute_definitions_tenant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[AttributeType] = mapped_column(SAEnum(AttributeType, name="attribute_type"), nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    options: Mapped[Optional[list]] = mapped_column(JSON)  # для select
    scope: Mapped[AttributeScope] = mapped_column(
        SAEnum(AttributeScope, name="attribute_scope"), default=AttributeScope.product, nullable=False
    )
    filterable: Mapped[bool] = mapped_column(default=False, nullable=False)
    show_in_list: Mapped[bool] = mapped_column(default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CategoryAttribute(Base):
    __tablename__ = "category_attributes"

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True)
    attribute_id: Mapped[int] = mapped_column(
        ForeignKey("attribute_definitions.id", ondelete="CASCADE"), primary_key=True
    )


class PriceTier(Base):
    """Уровень цены «от N шт». Уровень определяется суммарным количеством всех вариантов товара в корзине."""
    __tablename__ = "price_tiers"
    __table_args__ = (UniqueConstraint("tenant_id", "min_qty", name="uq_price_tiers_tenant_min_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    min_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------- Товары ----------

class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_tenant_category", "tenant_id", "category_id"),
        Index("ix_products_attributes", "attributes", postgresql_using="gin"),
        Index(
            "ix_products_name_trgm", "name_normalized",
            postgresql_using="gin", postgresql_ops={"name_normalized": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"))
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brands.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    is_visible: Mapped[bool] = mapped_column(default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    variants: Mapped[List["Variant"]] = relationship(
        back_populates="product", order_by="Variant.sort_order, Variant.id"
    )
    photos: Mapped[List["ProductPhoto"]] = relationship(order_by="ProductPhoto.sort_order, ProductPhoto.id")


class ProductPhoto(Base):
    """До 5 фото на товар, первое по sort_order — обложка."""
    __tablename__ = "product_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Variant(Base):
    """
    Вариант товара (вкус/цвет/сопротивление). В корзину всегда кладётся
    вариант. Товар без вариантов = один вариант is_default=True (name пустое,
    в UI не показывается).
    """
    __tablename__ = "variants"
    __table_args__ = (
        Index("ix_variants_product", "product_id"),
        Index("ix_variants_attributes", "attributes", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    sku: Mapped[Optional[str]] = mapped_column(String(100))
    attributes: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    stock_qty: Mapped[Optional[int]] = mapped_column(Integer)
    stock_status: Mapped[StockStatus] = mapped_column(
        SAEnum(StockStatus, name="stock_status"), default=StockStatus.in_stock, nullable=False
    )
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_visible: Mapped[bool] = mapped_column(default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="variants")


class Price(Base):
    """
    Цена товара на уровне. variant_id=None — цена товара; с variant_id —
    переопределение для конкретного варианта. Уникальность
    (product, variant, tier) держит код (app/database/repository/price_repository.py),
    т.к. NULL в variant_id не участвует в UNIQUE на Postgres < 15.
    """
    __tablename__ = "prices"
    __table_args__ = (Index("ix_prices_product", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    variant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("variants.id"))
    tier_id: Mapped[int] = mapped_column(ForeignKey("price_tiers.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки


# ---------- Корзина ----------

class Cart(Base):
    """Серверная корзина — переживает закрытие приложения и смену устройства. Одна на TenantUser."""
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    tenant_user_id: Mapped[int] = mapped_column(ForeignKey("tenant_users.id"), unique=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Последнее «Напомнить» от менеджера — не чаще раза в сутки
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    items: Mapped[List["CartItem"]] = relationship(order_by="CartItem.id")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "variant_id", name="uq_cart_items_cart_variant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------- Заявки ----------

class Order(Base):
    """
    Заявка менеджеру (не покупка: без оплаты и доставки). Позиции — снимок
    на момент отправки, цена всегда пересчитана на бэкенде.
    """
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_orders_tenant_number"),
        Index("ix_orders_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_orders_tenant_user", "tenant_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_user_id: Mapped[int] = mapped_column(ForeignKey("tenant_users.id"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), default=OrderStatus.new, nullable=False
    )
    comment: Mapped[Optional[str]] = mapped_column(Text)  # от клиента
    manager_note: Mapped[Optional[str]] = mapped_column(Text)  # внутренняя, клиенту не видна
    total: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    items: Mapped[List["OrderItem"]] = relationship(order_by="OrderItem.id")
    history: Mapped[List["OrderStatusHistory"]] = relationship(order_by="OrderStatusHistory.id")
    tenant_user: Mapped["TenantUser"] = relationship(lazy="joined")


class OrderItem(Base):
    """Снимок позиции: названия/артикул/цена копируются, FK — только для «Повторить» и аналитики."""
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    variant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("variants.id"))
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    variant_name: Mapped[Optional[str]] = mapped_column(String(255))
    sku: Mapped[Optional[str]] = mapped_column(String(100))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки за штуку
    tier_label: Mapped[Optional[str]] = mapped_column(String(50))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки, price * qty


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status: Mapped[Optional[OrderStatus]] = mapped_column(SAEnum(OrderStatus, name="order_status"))
    to_status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus, name="order_status"), nullable=False)
    tenant_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tenant_users.id"))  # кто сменил
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------- Служебное ----------

class AuditLog(Base):
    """Кто из сотрудников что поменял в админке."""
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    tenant_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tenant_users.id"))
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # create/update/delete/...
    entity: Mapped[str] = mapped_column(String(50), nullable=False)  # product/variant/order/...
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    data: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Notification(Base):
    """
    Очередь сообщений в чат бота тенанта. Доставляет push-воркер внутри
    сервиса (app/services/bot_notify_service.py) — как в такси, один
    процесс, дублей по конструкции нет. reply_markup — готовая
    inline-клавиатура Bot API (кнопки «Взять в работу» / «Открыть»).
    """
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_unsent", "is_sent", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    tg_user_id: Mapped[int] = mapped_column(ForeignKey("tg_users.id"), nullable=False)
    notif_type: Mapped[NotificationType] = mapped_column(
        SAEnum(NotificationType, name="notification_type"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_markup: Mapped[Optional[dict]] = mapped_column(JSON)
    related_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"))
    is_sent: Mapped[bool] = mapped_column(default=False, nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
