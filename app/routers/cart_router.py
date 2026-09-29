from fastapi import APIRouter, Depends, status

from app.database.models import Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_client, get_repository, get_tenant
from app.models.cart_models import CartItemUpdateRequest, CartResponse, CartSubmitRequest
from app.models.order_models import OrderResponse
from app.services.cart_service import build_cart_view
from app.services.catalog_service import is_orderable
from app.services.notify import notify_new_order
from app.services.order_service import OrderError, submit_order

router = APIRouter(prefix="/v1/cart", tags=["cart"])


@router.get("", response_model=CartResponse)
async def get_cart(
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """Корзина с ценами по уровням, подсказками «ещё N шт до…», итогом и причинами, почему нельзя отправить"""
    cart = await repo.cart.get_for_member(member.id)
    return (await build_cart_view(repo, tenant, cart)).response


@router.put("/items/{variant_id}", response_model=CartResponse)
async def set_cart_item(
    variant_id: int,
    data: CartItemUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """
    Установить количество варианта (0 — убрать). Идемпотентно: фронт шлёт
    итоговое число после debounce, а не +1/-1. Увеличивать можно только
    то, что можно заказать; уменьшать/убирать — всегда (в т.ч. то, чего
    уже нет в наличии).
    """
    variant = await repo.variant.get(tenant.id, variant_id)
    if variant is None:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Позиция не найдена")
    cart = await repo.cart.get_or_create(tenant.id, member.id)
    current = next((item.qty for item in cart.items if item.variant_id == variant_id), 0)
    if data.qty > current and not is_orderable(variant, variant.product):
        raise api_error(status.HTTP_409_CONFLICT, "unavailable", "Этой позиции сейчас нет в наличии")
    await repo.cart.set_qty(cart, variant_id, data.qty)
    await repo.commit()
    return (await build_cart_view(repo, tenant, cart)).response


@router.delete("", response_model=CartResponse)
async def clear_cart(
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    cart = await repo.cart.get_or_create(tenant.id, member.id)
    await repo.cart.clear(cart)
    await repo.commit()
    return (await build_cart_view(repo, tenant, cart)).response


@router.post("/submit", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def submit_cart(
    data: CartSubmitRequest,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """
    «Отправить менеджеру» (MainButton): заявка со снимком позиций и цен,
    пересчитанных здесь же, корзина очищается, админам и клиенту уходят
    сообщения в бот. Всё одной транзакцией.
    """
    try:
        order = await submit_order(repo, tenant, member, data.comment)
    except OrderError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc.code, exc.message)
    await notify_new_order(repo, tenant, order, member)
    await repo.commit()
    return order
