"""Заведение оптовиков и назначение владельца — общее для CLI и раздела «Платформа»."""
import re

from app.database.models import MemberStatus, Role, Tenant
from app.database.repository.main_repository import Repository

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# Занятые пути фронта/API — slug тенанта не может совпасть с ними
RESERVED_SLUGS = {"platform", "admin", "api", "static", "files", "t", "health", "demo-platform"}


def slug_problem(slug: str) -> str | None:
    if not SLUG_RE.match(slug):
        return "Только латиница в нижнем регистре, цифры и дефис, 2–40 символов"
    if slug in RESERVED_SLUGS:
        return "Это имя зарезервировано"
    return None


async def make_owner(repo: Repository, tenant: Tenant, telegram_id: int) -> None:
    """Владелец у тенанта один: прежний (если был) становится admin"""
    tg_user = await repo.tg_user.upsert(telegram_id, {})
    for staff in await repo.tenant_user.list_staff(tenant.id):
        if staff.role == Role.owner and staff.tg_user_id != tg_user.id:
            staff.role = Role.admin
    member = await repo.tenant_user.get_by_tg_user(tenant.id, tg_user.id)
    if member is None:
        member = await repo.tenant_user.create(tenant.id, tg_user.id, Role.owner, MemberStatus.active)
    member.role, member.status = Role.owner, MemberStatus.active
