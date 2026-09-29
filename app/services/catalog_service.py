"""
Общая логика каталога без HTTP: дерево категорий, наследование атрибутов,
приведение значений атрибутов к типу, статус наличия, ссылки.
"""
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from app.database.models import (
    AttributeDefinition,
    AttributeScope,
    AttributeType,
    Category,
    PriceBasis,
    PriceTier,
    StockStatus,
    TgUser,
    Variant,
)
from app.models.catalog_models import AttributeValue, CategoryNode, TierPrice
from app.services.pricing import Tier
from app.utils.file_storage import file_url


class AttributeValueError(ValueError):
    pass


# ---------- Категории ----------

def descendant_ids(categories: Sequence[Category], root_id: int) -> list[int]:
    """Категория + все её потомки (товары подкатегорий видны в родителе)"""
    children: dict[int | None, list[int]] = {}
    for category in categories:
        children.setdefault(category.parent_id, []).append(category.id)
    result, stack = [], [root_id]
    while stack:
        current = stack.pop()
        if current in result:
            continue  # защита от цикла, если parent_id когда-то испортили
        result.append(current)
        stack.extend(children.get(current, []))
    return result


def ancestor_chain(categories: Sequence[Category], category_id: int) -> list[Category]:
    """От корня до самой категории включительно"""
    by_id = {category.id: category for category in categories}
    chain: list[Category] = []
    current = by_id.get(category_id)
    while current is not None and current not in chain:
        chain.append(current)
        current = by_id.get(current.parent_id) if current.parent_id else None
    return list(reversed(chain))


def would_create_cycle(categories: Sequence[Category], category_id: int, new_parent_id: int | None) -> bool:
    return new_parent_id is not None and new_parent_id in descendant_ids(categories, category_id)


def build_category_tree(
    categories: Sequence[Category], direct_counts: dict[int, int], only_visible: bool,
) -> list[CategoryNode]:
    visible = [c for c in categories if c.is_visible or not only_visible]
    visible_ids = {c.id for c in visible}
    nodes = {
        c.id: CategoryNode(
            id=c.id, parent_id=c.parent_id, name=c.name, image_url=file_url(c.image_key), sort_order=c.sort_order,
            is_visible=c.is_visible, product_count=0, children=[],
        )
        for c in visible
    }
    roots: list[CategoryNode] = []
    for c in visible:
        node = nodes[c.id]
        if c.parent_id is not None and c.parent_id in visible_ids:
            nodes[c.parent_id].children.append(node)
        elif c.parent_id is None:
            roots.append(node)
        # потомок скрытой категории скрыт вместе с ней

    def fill_counts(node: CategoryNode) -> int:
        node.children.sort(key=lambda n: (n.sort_order, n.name))
        node.product_count = direct_counts.get(node.id, 0) + sum(fill_counts(child) for child in node.children)
        return node.product_count

    roots.sort(key=lambda n: (n.sort_order, n.name))
    for root in roots:
        fill_counts(root)
    return roots


def inherited_attribute_ids(
    categories: Sequence[Category], bound: dict[int, list[int]], category_id: int,
) -> list[int]:
    """Атрибуты категории вместе с атрибутами всех её предков"""
    ids: list[int] = []
    for category in ancestor_chain(categories, category_id):
        for attribute_id in bound.get(category.id, []):
            if attribute_id not in ids:
                ids.append(attribute_id)
    return ids


# ---------- Атрибуты ----------

def coerce_attributes(
    definitions: Iterable[AttributeDefinition], raw: dict[str, Any], scope: AttributeScope,
) -> dict[str, Any]:
    """
    Проверка значений из админки: ключ должен быть заведён у тенанта с нужным
    scope, значение приводится к типу (number хранится числом — иначе
    фильтры по JSON не сработают). None/"" — значит «не задано», ключ выкидывается.
    """
    by_key = {d.key: d for d in definitions}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        definition = by_key.get(key)
        if definition is None:
            raise AttributeValueError(f"Неизвестный атрибут: {key}")
        if definition.scope != scope:
            raise AttributeValueError(f"Атрибут {key} задаётся у {definition.scope.value}, а не у {scope.value}")
        if value is None or value == "":
            continue
        if definition.type == AttributeType.number:
            try:
                number = float(str(value).replace(",", "."))
            except ValueError:
                raise AttributeValueError(f"{definition.label}: нужно число")
            result[key] = int(number) if number.is_integer() else number
        elif definition.type == AttributeType.bool:
            if isinstance(value, bool):
                result[key] = value
            elif str(value).lower() in ("1", "true", "yes", "да"):
                result[key] = True
            elif str(value).lower() in ("0", "false", "no", "нет"):
                result[key] = False
            else:
                raise AttributeValueError(f"{definition.label}: нужно да/нет")
        elif definition.type == AttributeType.select:
            text = str(value).strip()
            if definition.options and text not in definition.options:
                raise AttributeValueError(f"{definition.label}: значения «{text}» нет в списке")
            result[key] = text
        else:
            result[key] = str(value).strip()
    return result


def attribute_values(
    definitions: Sequence[AttributeDefinition], attrs: dict | None, only_in_list: bool = False,
) -> list[AttributeValue]:
    """Значения для отображения в порядке sort_order определений"""
    if not attrs:
        return []
    values = []
    for definition in sorted(definitions, key=lambda d: (d.sort_order, d.id)):
        if only_in_list and not definition.show_in_list:
            continue
        if definition.key in attrs and attrs[definition.key] not in (None, ""):
            values.append(AttributeValue(
                key=definition.key, label=definition.label, value=attrs[definition.key], unit=definition.unit,
                type=definition.type,
            ))
    return values


# ---------- Наличие ----------

def stock_status_from_qty(qty: int, low_threshold: int) -> StockStatus:
    if qty <= 0:
        return StockStatus.out
    if qty <= low_threshold:
        return StockStatus.low
    return StockStatus.in_stock


def aggregate_stock(variants: Iterable[Variant]) -> StockStatus:
    """Сводный статус товара в списке: есть хоть что-то в наличии -> in_stock, только «мало» -> low"""
    statuses = {v.stock_status for v in variants if v.is_visible}
    if StockStatus.in_stock in statuses:
        return StockStatus.in_stock
    if StockStatus.low in statuses:
        return StockStatus.low
    return StockStatus.out


def is_orderable(variant: Variant, product) -> bool:
    return (
        variant.is_visible
        and variant.stock_status != StockStatus.out
        and product is not None
        and product.is_visible
        and product.deleted_at is None
    )


# ---------- Прочее ----------

def tier_price(tier: Tier, basis: PriceBasis, amount: int | None = None) -> TierPrice:
    """Уровень для ответа API: порог кладётся в min_qty или min_amount по режиму тенанта"""
    return TierPrice(
        tier_id=tier.id, label=tier.label, amount=amount,
        min_qty=tier.threshold if basis == PriceBasis.qty else None,
        min_amount=tier.threshold if basis == PriceBasis.amount else None,
    )


def to_pricing_tiers(tiers: Sequence[PriceTier], basis: PriceBasis = PriceBasis.qty) -> list[Tier]:
    """Порог уровня по режиму тенанта; уровни без порога этого режима пропускаются"""
    result = []
    for t in tiers:
        threshold = t.min_amount if basis == PriceBasis.amount else t.min_qty
        if threshold is not None:
            result.append(Tier(id=t.id, label=t.label, threshold=threshold))
    return result


def contact_url(user: TgUser) -> str:
    """Ссылка «Написать» клиенту: по username, а если его нет — по id"""
    if user.username:
        return f"https://t.me/{user.username}"
    return f"tg://user?id={user.telegram_id}"


def ask_manager_url(manager_username: str | None, product_name: str, variant_name: str | None = None) -> str | None:
    """«Спросить менеджера» — deep link в чат менеджера с готовым текстом о позиции"""
    if not manager_username:
        return None
    subject = f"{product_name} — {variant_name}" if variant_name else product_name
    text = f"Здравствуйте! Вопрос по позиции: {subject}"
    return f"https://t.me/{manager_username.lstrip('@')}?text={quote(text)}"
