from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.database.models import OrderStatus, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.deps import api_error, get_client, get_repository, get_tenant
from app.models.common_models import Page
from app.models.order_models import OrderResponse, RepeatOrderResponse
from app.services.order_service import repeat_order

router = APIRouter(prefix="/v1/orders", tags=["orders"])


@router.get("", response_model=Page[OrderResponse])
async def list_my_orders(
    status_filter: Optional[OrderStatus] = Query(None, alias="status"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """«Мои заявки» — только свои, в пределах текущего тенанта"""
    orders, total = await repo.order.list(tenant.id, tenant_user_id=member.id, status=status_filter,
                                          limit=limit, offset=offset)
    return Page(items=[OrderResponse.model_validate(o) for o in orders], total=total, limit=limit, offset=offset)


async def _my_order(repo: Repository, tenant: Tenant, member: TenantUser, order_id: int):
    order = await repo.order.get(tenant.id, order_id)
    if not order or order.tenant_user_id != member.id:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", "Заявка не найдена")
    return order


@router.get("/{order_id}", response_model=OrderResponse)
async def get_my_order(
    order_id: int,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    return await _my_order(repo, tenant, member, order_id)


@router.post("/{order_id}/repeat", response_model=RepeatOrderResponse)
async def repeat_my_order(
    order_id: int,
    tenant: Tenant = Depends(get_tenant),
    member: TenantUser = Depends(get_client),
    repo: Repository = Depends(get_repository),
):
    """«Повторить»: позиции снова в корзину; то, чего больше нет/нет в наличии, вернётся в skipped"""
    order = await _my_order(repo, tenant, member, order_id)
    added, skipped = await repeat_order(repo, tenant, member, order)
    await repo.commit()
    return RepeatOrderResponse(added=added, skipped=skipped)
