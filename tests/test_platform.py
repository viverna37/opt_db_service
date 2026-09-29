"""Раздел «Платформа»: заведение оптовиков владельцем сервиса (PLATFORM_ADMIN_IDS)"""
import pytest

import app.routers.platform_router as platform_router
from app.services.telegram_bot import BotTokenError
from tests.helpers import headers, seed_member, seed_tenant
from tests.test_auth import build_init_data

ADMIN = {"X-Tg-User-Id": "4242"}


@pytest.fixture()
def fake_telegram(monkeypatch):
    """Bot API подменён: токен 'good:*' валиден, кнопка меню ставится и запоминается"""
    calls = {"menu": []}

    async def get_bot_username(token):
        if not token.startswith("good:"):
            raise BotTokenError("Unauthorized")
        return f"bot_{token.split(':')[1]}"

    async def set_catalog_menu_button(token, url, text="Каталог"):
        calls["menu"].append((token, url))

    monkeypatch.setattr(platform_router, "get_bot_username", get_bot_username)
    monkeypatch.setattr(platform_router, "set_catalog_menu_button", set_catalog_menu_button)
    return calls


def test_only_platform_admins(client, session_factory):
    assert client.get("/v1/platform/tenants").status_code == 401
    assert client.get("/v1/platform/tenants", headers={"X-Tg-User-Id": "1"}).status_code == 403
    assert client.get("/v1/platform/tenants", headers=ADMIN).status_code == 200
    # владелец обычного тенанта суперадмином не становится
    tenant_id = seed_tenant(session_factory, "shop")
    seed_member(session_factory, tenant_id, 77, role=__import__("app.database.models", fromlist=["Role"]).Role.owner)
    assert client.get("/v1/platform/tenants", headers=headers("shop", 77)).status_code == 403


def test_platform_admin_via_real_init_data(client, session_factory):
    seed_tenant(session_factory, "shop", bot_token="111:AAA")
    user = {"id": 4242, "first_name": "Хозяин"}
    # из бота платформы
    r = client.get("/v1/platform/me", headers={"X-Init-Data": build_init_data("999:PLATFORM", user)})
    assert r.status_code == 200 and r.json()["telegram_id"] == 4242
    # из бота любого оптовика (X-Tenant)
    r = client.get("/v1/platform/me", headers={"X-Tenant": "shop", "X-Init-Data": build_init_data("111:AAA", user)})
    assert r.status_code == 200
    # чужой подписью — нет
    r = client.get("/v1/platform/me", headers={"X-Init-Data": build_init_data("555:X", user)})
    assert r.status_code == 401
    # /v1/me внутри тенанта подсказывает фронту показать вход в «Платформу»
    me = client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": build_init_data("111:AAA", user)}).json()
    assert me["is_platform_admin"] is True


def test_create_tenant_full(client, session_factory, fake_telegram):
    r = client.post("/v1/platform/tenants", json={
        "slug": "amigo", "name": "Amigo Opt", "bot_token": "good:amigo", "owner_telegram_id": 555,
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["bot_username"] == "bot_amigo" and body["bot_url"] == "https://t.me/bot_amigo"
    assert body["catalog_url"] == "https://opt.example.com/t/amigo"
    assert body["owner"]["telegram_id"] == 555 and body["warnings"] == []
    assert fake_telegram["menu"] == [("good:amigo", "https://opt.example.com/t/amigo")]

    # владелец сразу попадает в админку своего каталога
    me = client.get("/v1/me", headers=headers("amigo", 555)).json()
    assert me["role"] == "owner" and me["is_staff"] and not me["is_platform_admin"]

    listed = client.get("/v1/platform/tenants", headers=ADMIN).json()
    assert [t["slug"] for t in listed] == ["amigo"]


def test_create_validation(client, session_factory, fake_telegram):
    seed_tenant(session_factory, "taken")
    bad = [
        ({"slug": "Amigo Opt", "name": "x"}, 422),
        ({"slug": "platform", "name": "x"}, 422),
        ({"slug": "taken", "name": "x"}, 409),
        ({"slug": "newone", "name": "x", "bot_token": "bad:token"}, 422),
    ]
    for payload, code in bad:
        r = client.post("/v1/platform/tenants", json=payload, headers=ADMIN)
        assert r.status_code == code, (payload, r.text)
    # без бота и владельца — создаётся, но с предупреждениями
    r = client.post("/v1/platform/tenants", json={"slug": "draft", "name": "Черновик"}, headers=ADMIN)
    assert r.status_code == 201 and len(r.json()["warnings"]) == 2


def test_update_deactivate_owner_and_bot(client, session_factory, fake_telegram):
    client.post("/v1/platform/tenants", json={"slug": "amigo", "name": "Amigo", "owner_telegram_id": 555}, headers=ADMIN)
    assert client.get("/v1/me", headers=headers("amigo", 10)).status_code == 200

    r = client.patch("/v1/platform/tenants/amigo", json={"is_active": False}, headers=ADMIN)
    assert r.json()["is_active"] is False
    assert client.get("/v1/me", headers=headers("amigo", 555)).status_code == 404
    client.patch("/v1/platform/tenants/amigo", json={"is_active": True}, headers=ADMIN)

    r = client.patch("/v1/platform/tenants/amigo", json={"owner_telegram_id": 777, "name": "Amigo Opt",
                                                          "bot_token": "good:new"}, headers=ADMIN).json()
    assert r["owner"]["telegram_id"] == 777 and r["name"] == "Amigo Opt" and r["bot_username"] == "bot_new"
    # прежний владелец стал админом, не потерял доступ
    assert client.get("/v1/me", headers=headers("amigo", 555)).json()["role"] == "admin"
    assert client.patch("/v1/platform/tenants/amigo", json={"bot_token": "bad:x"}, headers=ADMIN).status_code == 422
    assert client.patch("/v1/platform/tenants/nope", json={"name": "x"}, headers=ADMIN).status_code == 404


def test_stats(client, session_factory, fake_telegram):
    from tests.helpers import create_product, setup_shop, visible_variant_ids

    ctx = setup_shop(client, session_factory, age_gate=False)
    product = create_product(client, ctx)
    client.put(f"/v1/cart/items/{visible_variant_ids(product)[0]}", json={"qty": 1}, headers=headers("shop", 10))
    client.post("/v1/cart/submit", json={}, headers=headers("shop", 10))
    t = client.get("/v1/platform/tenants/shop", headers=ADMIN).json()
    assert (t["products_count"], t["clients_count"], t["orders_count"]) == (1, 1, 1)
    assert t["owner"]["telegram_id"] == 1 and t["last_order_at"]
