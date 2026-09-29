"""Хелперы: сидинг тенантов/сотрудников напрямую в БД, товары — через админское API (заодно его проверяем)."""
import asyncio

from sqlalchemy import select

import app.database.models as m


def run(coro):
    return asyncio.run(coro)


def headers(slug: str, tg_id: int) -> dict:
    """dev-авторизация (DEV_AUTH=true в conftest.py)"""
    return {"X-Tenant": slug, "X-Tg-User-Id": str(tg_id)}


def seed_tenant(session_factory, slug: str = "shop", **fields) -> int:
    async def _seed():
        async with session_factory() as s:
            tenant = m.Tenant(slug=slug, name=fields.pop("name", slug.title()), **fields)
            s.add(tenant)
            await s.commit()
            return tenant.id

    return run(_seed())


def seed_member(session_factory, tenant_id: int, tg_id: int, role: m.Role = m.Role.client,
                status: m.MemberStatus = m.MemberStatus.active, age_confirmed: bool = True) -> int:
    async def _seed():
        async with session_factory() as s:
            user = (await s.execute(select(m.TgUser).where(m.TgUser.telegram_id == tg_id))).scalar_one_or_none()
            if user is None:
                user = m.TgUser(telegram_id=tg_id, first_name=f"User{tg_id}", username=f"user{tg_id}")
                s.add(user)
                await s.flush()
            member = m.TenantUser(tenant_id=tenant_id, tg_user_id=user.id, role=role, status=status,
                                  age_confirmed_at=m.utcnow() if age_confirmed else None)
            s.add(member)
            await s.commit()
            return member.id

    return run(_seed())


def setup_shop(client, session_factory, slug: str = "shop", owner_tg: int = 1, **tenant_fields) -> dict:
    """Тенант + владелец + уровни цен «от 1 / от 10 / от 50» через API. Возвращает контекст"""
    tenant_id = seed_tenant(session_factory, slug, **tenant_fields)
    seed_member(session_factory, tenant_id, owner_tg, role=m.Role.owner)
    owner = headers(slug, owner_tg)
    tiers = []
    for label, min_qty in (("от 1 шт", 1), ("от 10 шт", 10), ("от 50 шт", 50)):
        r = client.post("/v1/admin/price-tiers", json={"label": label, "min_qty": min_qty}, headers=owner)
        assert r.status_code == 201, r.text
        tiers.append(r.json()["id"])
    return {"slug": slug, "tenant_id": tenant_id, "owner": owner, "tiers": tiers}


def create_product(client, ctx: dict, name: str = "Жнец 30мл", variants=("Манго", "Кола"),
                   prices=(26000, 25000, 24000), **fields) -> dict:
    owner = ctx["owner"]
    r = client.post("/v1/admin/products", json={"name": name, **fields}, headers=owner)
    assert r.status_code == 201, r.text
    product = r.json()
    for variant in variants:
        r = client.post(f"/v1/admin/products/{product['id']}/variants", json={"name": variant}, headers=owner)
        assert r.status_code == 201, r.text
        product = r.json()
    rows = [{"tier_id": tier_id, "amount": amount} for tier_id, amount in zip(ctx["tiers"], prices)]
    r = client.put(f"/v1/admin/products/{product['id']}/prices", json={"prices": rows}, headers=owner)
    assert r.status_code == 200, r.text
    return r.json()


def visible_variant_ids(product: dict) -> list[int]:
    return [v["id"] for v in product["variants"] if v["is_visible"]]


def notifications(session_factory, notif_type: m.NotificationType | None = None) -> list[m.Notification]:
    async def _get():
        async with session_factory() as s:
            query = select(m.Notification).order_by(m.Notification.id)
            if notif_type is not None:
                query = query.where(m.Notification.notif_type == notif_type)
            return list((await s.execute(query)).scalars().all())

    return run(_get())
