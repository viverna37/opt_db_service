"""Авторизация через initData (без регистрации), статусы доступа, 18+, режим approval"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import app.database.models as m
from app.config.config import get_cached_config
from app.deps import _account_from_headers
from tests.helpers import headers, seed_member, seed_tenant, setup_shop


def build_init_data(bot_token: str, user: dict, auth_date: int | None = None) -> str:
    """Зеркало проверки — так же подписывает initData Telegram (и бот-сервис в такси)"""
    data = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    launch_params = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, launch_params.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


USER = {"id": 777, "first_name": "Иван", "username": "ivan_shop", "photo_url": "https://t.me/i/ivan.jpg"}


def test_valid_init_data_creates_user_without_registration(client, session_factory):
    seed_tenant(session_factory, "shop", bot_token="111:AAA")
    r = client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": build_init_data("111:AAA", USER)})
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["user"]["username"] == "ivan_shop" and me["user"]["photo_url"].endswith("ivan.jpg")
    assert me["role"] == "client" and me["status"] == "active"
    assert me["access"] == "age_required"  # age_gate включён по умолчанию

    # имя/username обновляются при следующем входе
    renamed = {**USER, "username": "ivan_new"}
    r = client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": build_init_data("111:AAA", renamed)})
    assert r.json()["user"]["username"] == "ivan_new"
    assert r.json()["id"] == me["id"]


def test_init_data_signed_by_other_tenants_bot_is_rejected(client, session_factory):
    seed_tenant(session_factory, "shop", bot_token="111:AAA")
    seed_tenant(session_factory, "other", bot_token="222:BBB")
    foreign = build_init_data("222:BBB", USER)
    r = client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": foreign})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_init_data"


def test_expired_and_tampered_init_data_rejected(client, session_factory):
    seed_tenant(session_factory, "shop", bot_token="111:AAA")
    old = build_init_data("111:AAA", USER, auth_date=int(time.time()) - 3 * 86400)
    assert client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": old}).status_code == 401
    tampered = build_init_data("111:AAA", USER).replace("ivan_shop", "admin")
    assert client.get("/v1/me", headers={"X-Tenant": "shop", "X-Init-Data": tampered}).status_code == 401


def test_dev_header_ignored_when_dev_auth_disabled(session_factory):
    from dataclasses import replace

    from fastapi import HTTPException

    config = get_cached_config()
    prod = replace(config, auth=replace(config.auth, dev_auth=False))
    tenant = m.Tenant(slug="shop", name="Shop", bot_token="111:AAA")
    with pytest.raises(HTTPException) as exc:
        _account_from_headers(tenant, None, 42, prod)
    assert exc.value.status_code == 401


def test_unknown_tenant_404(client):
    assert client.get("/v1/me", headers=headers("nope", 1)).status_code == 404


def test_age_gate_blocks_catalog_until_confirmed(client, session_factory):
    setup_shop(client, session_factory)
    h = headers("shop", 50)
    r = client.get("/v1/catalog/categories", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "age_required"
    r = client.post("/v1/me/age-confirm", headers=h)
    assert r.json()["access"] == "ok"
    assert client.get("/v1/catalog/categories", headers=h).status_code == 200


def test_age_gate_off(client, session_factory):
    setup_shop(client, session_factory, age_gate=False)
    assert client.get("/v1/catalog/categories", headers=headers("shop", 50)).status_code == 200


def test_approval_mode_pending_until_admin_approves(client, session_factory):
    ctx = setup_shop(client, session_factory, access_mode=m.AccessMode.approval, age_gate=False)
    h = headers("shop", 60)
    me = client.get("/v1/me", headers=h).json()
    assert me["status"] == "pending" and me["access"] == "pending"
    r = client.get("/v1/catalog/products", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "pending"

    pending = client.get("/v1/admin/clients?status=pending", headers=ctx["owner"]).json()["items"]
    assert [c["tenant_user_id"] for c in pending] == [me["id"]]
    r = client.patch(f"/v1/admin/clients/{me['id']}", json={"status": "active"}, headers=ctx["owner"])
    assert r.status_code == 200, r.text
    assert client.get("/v1/catalog/products", headers=h).status_code == 200


def test_blocked_client_sees_no_prices_and_staff_cannot_self_block(client, session_factory):
    ctx = setup_shop(client, session_factory)
    member_id = seed_member(session_factory, ctx["tenant_id"], 70)
    r = client.patch(f"/v1/admin/clients/{member_id}", json={"status": "blocked", "note": "магазин Пар, Бузулук"},
                     headers=ctx["owner"])
    assert r.status_code == 200 and r.json()["note"] == "магазин Пар, Бузулук"
    h = headers("shop", 70)
    assert client.get("/v1/me", headers=h).json()["access"] == "blocked"
    assert client.get("/v1/catalog/products", headers=h).status_code == 403
    assert client.get("/v1/cart", headers=h).status_code == 403

    owner_member = client.get("/v1/me", headers=ctx["owner"]).json()["id"]
    r = client.patch(f"/v1/admin/clients/{owner_member}", json={"status": "blocked"}, headers=ctx["owner"])
    assert r.status_code == 403


def test_roles(client, session_factory):
    ctx = setup_shop(client, session_factory)
    seed_member(session_factory, ctx["tenant_id"], 80, role=m.Role.manager)
    seed_member(session_factory, ctx["tenant_id"], 81)
    manager, plain = headers("shop", 80), headers("shop", 81)

    assert client.get("/v1/admin/orders", headers=plain).status_code == 403
    assert client.get("/v1/admin/orders", headers=manager).status_code == 200
    # менеджер ведёт товары, но не настройки/справочники
    assert client.post("/v1/admin/products", json={"name": "X"}, headers=manager).status_code == 201
    assert client.patch("/v1/admin/settings", json={"name": "Y"}, headers=manager).status_code == 403
    assert client.post("/v1/admin/brands", json={"name": "Z"}, headers=manager).status_code == 403
    # назначать сотрудников — только владелец
    client_id = client.get("/v1/me", headers=plain).json()["id"]
    assert client.patch(f"/v1/admin/clients/{client_id}", json={"role": "manager"}, headers=manager).status_code == 403
    r = client.patch(f"/v1/admin/clients/{client_id}", json={"role": "admin"}, headers=ctx["owner"])
    assert r.status_code == 200 and r.json()["role"] == "admin"


def test_service_endpoint_requires_key(client, session_factory):
    seed_tenant(session_factory, "shop", bot_token="111:AAA", bot_username="shop_bot")
    seed_tenant(session_factory, "nobot")
    assert client.get("/v1/service/tenants").status_code == 401
    assert client.get("/v1/service/tenants", headers={"X-Service-Key": "wrong"}).status_code == 401
    r = client.get("/v1/service/tenants", headers={"X-Service-Key": "test-service-key"})
    assert [t["slug"] for t in r.json()] == ["shop"]


def test_public_branding_without_auth(client, session_factory):
    seed_tenant(session_factory, "shop", name="Amigo Opt", accent_color="#FF9900")
    r = client.get("/v1/tenants/shop/public")
    assert r.status_code == 200 and r.json()["name"] == "Amigo Opt" and r.json()["accent_color"] == "#FF9900"
    assert "min_order_amount" not in r.json()
