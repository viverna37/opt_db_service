from fastapi import APIRouter, Depends

from app.database.models import MemberStatus, Tenant, TenantUser
from app.database.repository.main_repository import Repository
from app.config.config import get_cached_config
from app.deps import get_member, get_repository, get_tenant, is_staff
from app.models.common_models import MeResponse, TgUserResponse
from app.services.cart_service import cart_qty_by_variant
from app.services.serializers import tenant_response
from app.utils.dt import utcnow

router = APIRouter(prefix="/v1/me", tags=["me"])


def _access(tenant: Tenant, member: TenantUser) -> str:
    if member.status == MemberStatus.blocked:
        return "blocked"
    if is_staff(member):
        return "ok"
    if member.status == MemberStatus.pending:
        return "pending"
    if tenant.age_gate and member.age_confirmed_at is None:
        return "age_required"
    return "ok"


async def _me(repo: Repository, tenant: Tenant, member: TenantUser) -> MeResponse:
    access = _access(tenant, member)
    cart_qty = sum((await cart_qty_by_variant(repo, member.id)).values()) if access == "ok" else 0
    return MeResponse(
        id=member.id, role=member.role, status=member.status, is_staff=is_staff(member),
        is_platform_admin=member.tg_user.telegram_id in get_cached_config().platform.admin_ids, access=access,
        age_confirmed_at=member.age_confirmed_at, user=TgUserResponse.model_validate(member.tg_user),
        tenant=tenant_response(tenant), cart_qty=cart_qty,
    )


@router.get("", response_model=MeResponse)
async def get_me(
    member: TenantUser = Depends(get_member),
    tenant: Tenant = Depends(get_tenant),
    repo: Repository = Depends(get_repository),
):
    """
    Открытие мини-аппа: заводит пользователя при первом обращении (см.
    get_member) и говорит фронту, какой экран показать (access) и есть ли
    доступ к админке (is_staff).
    """
    return await _me(repo, tenant, member)


@router.post("/age-confirm", response_model=MeResponse)
async def confirm_age(
    member: TenantUser = Depends(get_member),
    tenant: Tenant = Depends(get_tenant),
    repo: Repository = Depends(get_repository),
):
    """Экран «Мне есть 18 лет, закупаю для перепродажи» — одна кнопка, без полей"""
    if member.age_confirmed_at is None:
        member.age_confirmed_at = utcnow()
        await repo.commit()
    return await _me(repo, tenant, member)
