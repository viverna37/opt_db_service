from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import PlainTextResponse

from app.database.models import OrderStatus, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_repository, get_staff, get_tenant
from app.models.admin_models import AdminSummaryResponse, LiveCartDetailResponse, LiveCartResponse
from app.models.common_models import Page
from app.models.order_models import AdminOrderResponse, OrderNoteUpdateRequest, OrderStatusUpdateRequest
from app.services.cart_service import build_cart_view
from app.services.notify import notify_cart_reminder
from app.services.order_service import change_status, order_text, orders_xlsx
from app.services.serializers import admin_order, order_client
from app.utils.dt import as_aware_utc, utcnow
from app.utils.money import safe_zone

router = APIRouter(prefix="/v1/admin", tags=["admin: orders"])

REMIND_INTERVAL = timedelta(hours=24)
EXPORT_LIMIT = 5000


@router.get("/summary", response_model=AdminSummaryResponse)
async def get_summary(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Сводка вверху раздела «Заявки»"""
    counts = await repo.order.count_by_status(tenant.id)
    return AdminSummaryResponse(
        products_total=sum((await repo.product.count_per_category(tenant.id, only_visible=False)).values()),
        new_orders=counts[OrderStatus.new],
        in_progress_orders=counts[OrderStatus.in_progress],
        live_carts=await repo.cart.count_live(tenant.id),
        out_of_stock_products=await repo.product.count_out_of_stock(tenant.id),
        pending_clients=await repo.tenant_user.count_pending(tenant.id),
    )


# ---------- Заявки ----------

def _date_range(tenant: Tenant, date_from: Optional[date], date_to: Optional[date]):
    """Даты фильтра — в часовом поясе тенанта, в БД сравниваем в UTC"""
    zone = safe_zone(tenant.timezone)
    start = datetime.combine(date_from, time.min, zone) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min, zone) if date_to else None
    return start, end


@router.get("/orders", response_model=Page[AdminOrderResponse])
async def list_orders(
    status_filter: Optional[OrderStatus] = Query(None, alias="status"),
    number: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    start, end = _date_range(tenant, date_from, date_to)
    orders, total = await repo.order.list(tenant.id, status=status_filter, number=number, created_from=start,
                                          created_to=end, limit=limit, offset=offset)
    return Page(items=[admin_order(o) for o in orders], total=total, limit=limit, offset=offset)


@router.get("/orders/export.xlsx")
async def export_orders(
    status_filter: Optional[OrderStatus] = Query(None, alias="status"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    start, end = _date_range(tenant, date_from, date_to)
    orders, _ = await repo.order.list(tenant.id, status=status_filter, created_from=start, created_to=end,
                                      limit=EXPORT_LIMIT)
    return _xlsx_response(orders_xlsx(tenant, list(orders)), "orders.xlsx")


async def _order(repo: Repository, tenant: Tenant, order_id: int):
    order = await repo.order.get(tenant.id, order_id)
    if not order:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Заявка не найдена")
    return order


@router.get("/orders/{order_id}", response_model=AdminOrderResponse)
async def get_order(
    order_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    return admin_order(await _order(repo, tenant, order_id))


@router.patch("/orders/{order_id}/status", response_model=AdminOrderResponse)
async def update_order_status(
    order_id: int,
    data: OrderStatusUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Смена статуса (в т.ч. кнопка «Взять в работу» под уведомлением в боте — её дёргает бот-сервис)"""
    order = await _order(repo, tenant, order_id)
    previous = order.status
    if await change_status(repo, order, data.status, staff):
        await repo.audit.log(tenant.id, staff.id, "status", "order", order.id,
                             {"from": previous.value, "to": data.status.value})
        await repo.commit()
    return admin_order(await _order(repo, tenant, order_id))


@router.patch("/orders/{order_id}/note", response_model=AdminOrderResponse)
async def update_order_note(
    order_id: int,
    data: OrderNoteUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Внутренняя заметка менеджера — клиенту не видна"""
    order = await _order(repo, tenant, order_id)
    order.manager_note = (data.manager_note or "").strip() or None
    order.updated_at = utcnow()
    await repo.commit()
    return admin_order(order)


@router.get("/orders/{order_id}/text", response_class=PlainTextResponse)
async def get_order_text(
    order_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """«Скопировать текстом» — для 1С/накладной"""
    order = await _order(repo, tenant, order_id)
    return order_text(tenant, order, order.tenant_user)


@router.get("/orders/{order_id}/export.xlsx")
async def export_order(
    order_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    order = await _order(repo, tenant, order_id)
    return _xlsx_response(orders_xlsx(tenant, [order]), f"order-{order.number}.xlsx")


def _xlsx_response(content: bytes, filename: str) -> Response:
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- Живые корзины ----------

def _can_remind(reminded_at: Optional[datetime]) -> bool:
    return reminded_at is None or utcnow() - as_aware_utc(reminded_at) >= REMIND_INTERVAL


async def _live_cart(repo: Repository, tenant: Tenant, cart, member: TenantUser, detailed: bool):
    view = (await build_cart_view(repo, tenant, cart)).response
    summary = dict(
        cart_id=cart.id, client=order_client(member), positions=view.positions, total_qty=view.total_qty,
        total=view.total, updated_at=cart.updated_at, reminded_at=cart.reminded_at,
        can_remind=_can_remind(cart.reminded_at),
    )
    return LiveCartDetailResponse(**summary, cart=view) if detailed else LiveCartResponse(**summary)


@router.get("/carts", response_model=list[LiveCartResponse])
async def list_live_carts(
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Кто что набрал, но не отправил — чтобы менеджер мог напомнить"""
    carts = await repo.cart.list_live(tenant.id)
    members = await repo.cart.members([c.tenant_user_id for c in carts])
    return [await _live_cart(repo, tenant, cart, members[cart.tenant_user_id], detailed=False) for cart in carts]


async def _cart(repo: Repository, tenant: Tenant, cart_id: int):
    cart = await repo.cart.get(tenant.id, cart_id)
    if not cart:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Корзина не найдена")
    member = await repo.tenant_user.get(tenant.id, cart.tenant_user_id)
    return cart, member


@router.get("/carts/{cart_id}", response_model=LiveCartDetailResponse)
async def get_live_cart(
    cart_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    cart, member = await _cart(repo, tenant, cart_id)
    return await _live_cart(repo, tenant, cart, member, detailed=True)


@router.post("/carts/{cart_id}/remind", response_model=LiveCartResponse)
async def remind_cart(
    cart_id: int,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """«Напомнить»: бот пишет клиенту «У вас в корзине N позиций» — не чаще раза в сутки на клиента"""
    cart, member = await _cart(repo, tenant, cart_id)
    if not cart.items:
        raise api_error(status.HTTP_409_CONFLICT, "cart_empty", "Корзина уже пуста")
    if not _can_remind(cart.reminded_at):
        raise api_error(status.HTTP_429_TOO_MANY_REQUESTS, "already_reminded", "Напоминали меньше суток назад")
    await notify_cart_reminder(repo, tenant, member, positions=len(cart.items))
    await repo.cart.mark_reminded(cart, utcnow())
    await repo.audit.log(tenant.id, staff.id, "remind", "cart", cart.id)
    await repo.commit()
    return await _live_cart(repo, tenant, cart, member, detailed=False)
