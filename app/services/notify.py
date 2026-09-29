"""
Постановка сообщений в очередь Notification (доставляет
app/services/bot_notify_service.py). Тексты и кнопки собираются здесь.

Формулировки — только «заявка» / «отправить менеджеру»: это не покупка,
никаких «купить/оплатить/оформить заказ».
"""
from app.config.config import get_cached_config
from app.database.models import NotificationType, Order, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.services.order_service import order_text

# callback_data кнопки «Взять в работу» — разбирает бот-сервис и дёргает
# PATCH /v1/admin/orders/{id}/status от имени нажавшего сотрудника.
TAKE_ORDER_CALLBACK = "order_take:{order_id}"


def webapp_url(tenant: Tenant, path: str = "") -> str | None:
    base = get_cached_config().telegram.webapp_base_url
    return f"{base}/t/{tenant.slug}{path}" if base else None


def _web_app_button(text: str, url: str | None) -> list[dict]:
    return [{"text": text, "web_app": {"url": url}}] if url else []


async def notify_new_order(repo: Repository, tenant: Tenant, order: Order, client: TenantUser) -> None:
    text = "🆕 Новая заявка\n\n" + order_text(tenant, order, client)
    keyboard = [[{"text": "✅ Взять в работу", "callback_data": TAKE_ORDER_CALLBACK.format(order_id=order.id)}]]
    open_button = _web_app_button("Открыть в админке", webapp_url(tenant, f"/admin/orders/{order.id}"))
    if open_button:
        keyboard.append(open_button)
    for staff in await repo.tenant_user.list_staff(tenant.id):
        await repo.notification.create(
            tenant_id=tenant.id, tg_user_id=staff.tg_user_id, notif_type=NotificationType.order_new_admin,
            text=text, reply_markup={"inline_keyboard": keyboard}, related_order_id=order.id,
        )

    client_text = (
        f"Заявка №{order.number} отправлена менеджеру — он свяжется с вами.\n\n"
        + order_text(tenant, order, client, include_client=False)
    )
    button = _web_app_button("Мои заявки", webapp_url(tenant, "/orders"))
    await repo.notification.create(
        tenant_id=tenant.id, tg_user_id=client.tg_user_id, notif_type=NotificationType.order_copy_client,
        text=client_text, reply_markup={"inline_keyboard": [button]} if button else None, related_order_id=order.id,
    )


async def notify_cart_reminder(repo: Repository, tenant: Tenant, client: TenantUser, positions: int) -> None:
    text = f"У вас в корзине {positions} {_plural(positions, 'позиция', 'позиции', 'позиций')}. " \
           f"Отправьте заявку менеджеру, когда будете готовы."
    button = _web_app_button("Открыть корзину", webapp_url(tenant, "/cart"))
    await repo.notification.create(
        tenant_id=tenant.id, tg_user_id=client.tg_user_id, notif_type=NotificationType.cart_reminder,
        text=text, reply_markup={"inline_keyboard": [button]} if button else None,
    )


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
