from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.database.models import MemberStatus, Role, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_repository, get_staff, get_tenant
from app.models.admin_models import ClientResponse, ClientUpdateRequest
from app.models.common_models import Page, TgUserResponse
from app.services.catalog_service import contact_url

router = APIRouter(prefix="/v1/admin/clients", tags=["admin: clients"])


def _client(member: TenantUser, orders_count: int) -> ClientResponse:
    return ClientResponse(
        tenant_user_id=member.id, role=member.role, status=member.status, note=member.note,
        user=TgUserResponse.model_validate(member.tg_user), contact_url=contact_url(member.tg_user),
        first_seen=member.first_seen, last_seen=member.last_seen, age_confirmed_at=member.age_confirmed_at,
        orders_count=orders_count,
    )


@router.get("", response_model=Page[ClientResponse])
async def list_clients(
    q: Optional[str] = Query(None, max_length=100),
    status_filter: Optional[MemberStatus] = Query(None, alias="status"),
    role: Optional[Role] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """Все, кто открывал каталог. status=pending — очередь на одобрение в режиме approval"""
    rows, total = await repo.tenant_user.list_members(tenant.id, q=q, status=status_filter, role=role,
                                                      limit=limit, offset=offset)
    return Page(items=[_client(m, c) for m, c in rows], total=total, limit=limit, offset=offset)


@router.patch("/{tenant_user_id}", response_model=ClientResponse)
async def update_client(
    tenant_user_id: int,
    data: ClientUpdateRequest,
    tenant: Tenant = Depends(get_tenant),
    staff: TenantUser = Depends(get_staff),
    repo: Repository = Depends(get_repository),
):
    """
    Заметка («магазин Пар, Бузулук»), одобрение (status=active),
    блокировка (status=blocked) — любой сотрудник, но только клиентов.
    Роли и статусы сотрудников — только владелец; владельца не трогает никто
    (передача владения — через CLI).
    """
    member = await repo.tenant_user.get(tenant.id, tenant_user_id)
    if not member:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Клиент не найден")
    fields = data.model_dump(exclude_unset=True)

    touches_access = "status" in fields or "role" in fields
    if touches_access:
        if member.role == Role.owner or fields.get("role") == Role.owner:
            raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Владелец меняется только через CLI")
        if member.id == staff.id:
            raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Нельзя менять собственный доступ")
        is_staff_change = member.role != Role.client or fields.get("role", Role.client) != Role.client
        if is_staff_change and staff.role != Role.owner:
            raise api_error(status.HTTP_403_FORBIDDEN, "forbidden", "Сотрудников назначает только владелец")

    if "note" in fields:
        member.note = (fields["note"] or "").strip() or None
    if fields.get("status") is not None:
        member.status = fields["status"]
    if fields.get("role") is not None:
        member.role = fields["role"]
    await repo.audit.log(tenant.id, staff.id, "update", "client", member.id,
                         {k: (v.value if hasattr(v, "value") else v) for k, v in fields.items() if k != "note"})
    await repo.commit()
    return _client(member, await repo.order.count_for_member(member.id))
