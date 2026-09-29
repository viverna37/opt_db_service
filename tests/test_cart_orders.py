"""Корзина-заявка: уровни цен, подсказки, отправка менеджеру (снимок + пересчёт), уведомления, «Повторить»"""
import app.database.models as m
from tests.helpers import create_product, headers, notifications, setup_shop, visible_variant_ids


def _shop(client, session_factory, **tenant_fields):
    ctx = setup_shop(client, session_factory, age_gate=False, **tenant_fields)
    product = create_product(client, ctx)
    return ctx, product, visible_variant_ids(product)


def test_default_variant_hidden_once_named_variants_exist(client, session_factory):
    ctx, product, variant_ids = _shop(client, session_factory)
    default = [v for v in product["variants"] if v["is_default"]][0]
    assert not default["is_visible"] and len(variant_ids) == 2

    plain = create_product(client, ctx, name="Картридж", variants=())
    assert [v["is_default"] for v in plain["variants"] if v["is_visible"]] == [True]
    card = client.get(f"/v1/catalog/products/{plain['id']}", headers=headers("shop", 10)).json()
    assert card["has_variants"] is False and len(card["variants"]) == 1


def test_cart_tier_by_total_qty_and_next_tier_hint(client, session_factory):
    ctx, product, (mango, cola) = _shop(client, session_factory)
    h = headers("shop", 10)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 4}, headers=h)
    cart = client.put(f"/v1/cart/items/{cola}", json={"qty": 3}, headers=h).json()
    group = cart["groups"][0]
    assert group["total_qty"] == 7 and group["tier"]["label"] == "от 1 шт"
    assert group["qty_to_next_tier"] == 3 and group["next_tier"]["min_qty"] == 10
    assert cart["total"] == 7 * 26000

    cart = client.put(f"/v1/cart/items/{cola}", json={"qty": 6}, headers=h).json()
    assert cart["groups"][0]["tier"]["label"] == "от 10 шт"
    assert cart["total"] == 10 * 25000 and cart["can_submit"]

    # карточка подсвечивает текущий уровень по количеству в корзине
    card = client.get(f"/v1/catalog/products/{product['id']}", headers=h).json()
    assert card["in_cart_qty"] == 10 and card["current_tier_id"] == ctx["tiers"][1]
    assert {v["id"]: v["in_cart_qty"] for v in card["variants"]} == {mango: 4, cola: 6}

    # qty=0 убирает позицию
    cart = client.put(f"/v1/cart/items/{mango}", json={"qty": 0}, headers=h).json()
    assert cart["positions"] == 1


def test_cart_survives_between_sessions(client, session_factory):
    ctx, product, (mango, _) = _shop(client, session_factory)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 2}, headers=headers("shop", 10))
    assert client.get("/v1/me", headers=headers("shop", 10)).json()["cart_qty"] == 2
    assert client.get("/v1/cart", headers=headers("shop", 10)).json()["total_qty"] == 2


def test_submit_creates_snapshot_clears_cart_and_notifies(client, session_factory):
    ctx, product, (mango, cola) = _shop(client, session_factory)
    h = headers("shop", 10)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 5}, headers=h)
    client.put(f"/v1/cart/items/{cola}", json={"qty": 5}, headers=h)

    r = client.post("/v1/cart/submit", json={"comment": "  после обеда  "}, headers=h)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["number"] == 1 and order["status"] == "new" and order["comment"] == "после обеда"
    assert order["total"] == 10 * 25000
    assert {(i["variant_name"], i["qty"], i["price"], i["tier_label"]) for i in order["items"]} == {
        ("Манго", 5, 25000, "от 10 шт"), ("Кола", 5, 25000, "от 10 шт"),
    }
    assert client.get("/v1/cart", headers=h).json()["positions"] == 0

    admin_msgs = notifications(session_factory, m.NotificationType.order_new_admin)
    assert len(admin_msgs) == 1  # один сотрудник — владелец
    keyboard = admin_msgs[0].reply_markup["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == f"order_take:{order['id']}"
    assert keyboard[1][0]["web_app"]["url"] == f"https://opt.example.com/t/shop/admin/orders/{order['id']}"
    client_msgs = notifications(session_factory, m.NotificationType.order_copy_client)
    assert "Заявка №1" in client_msgs[0].text and "Манго" in client_msgs[0].text
    for text in (admin_msgs[0].text, client_msgs[0].text):
        assert "купить" not in text.lower() and "оплат" not in text.lower()

    # снимок не меняется, если товар переименовали, а номер сквозной
    client.patch(f"/v1/admin/products/{product['id']}", json={"name": "Новое имя"}, headers=ctx["owner"])
    assert client.get(f"/v1/orders/{order['id']}", headers=h).json()["items"][0]["product_name"] == "Жнец 30мл"
    client.put(f"/v1/cart/items/{mango}", json={"qty": 1}, headers=h)
    assert client.post("/v1/cart/submit", json={}, headers=h).json()["number"] == 2


def test_price_is_recomputed_on_submit(client, session_factory):
    ctx, product, (mango, _) = _shop(client, session_factory)
    h = headers("shop", 10)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 1}, headers=h)
    rows = [{"tier_id": ctx["tiers"][0], "amount": 99900}]
    client.put(f"/v1/admin/products/{product['id']}/prices", json={"prices": rows}, headers=ctx["owner"])
    order = client.post("/v1/cart/submit", json={}, headers=h).json()
    assert order["items"][0]["price"] == 99900


def test_unavailable_items_are_highlighted_and_block_submit(client, session_factory):
    ctx, product, (mango, cola) = _shop(client, session_factory)
    h = headers("shop", 10)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 3}, headers=h)
    client.put(f"/v1/cart/items/{cola}", json={"qty": 8}, headers=h)
    client.patch(f"/v1/admin/products/{product['id']}/variants/{cola}", json={"stock_status": "out"},
                 headers=ctx["owner"])

    cart = client.get("/v1/cart", headers=h).json()
    lines = {line["variant_id"]: line for line in cart["groups"][0]["lines"]}
    assert lines[cola]["problem"] == "unavailable" and lines[cola]["amount"] is None
    # недоступное не даёт скидку за объём: уровень считается по 3 шт
    assert cart["groups"][0]["tier"]["label"] == "от 1 шт" and cart["total"] == 3 * 26000
    assert not cart["can_submit"] and "unavailable_items" in cart["blockers"]
    r = client.post("/v1/cart/submit", json={}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "unavailable_items"

    # добавить ещё нельзя, убрать — можно
    assert client.put(f"/v1/cart/items/{cola}", json={"qty": 9}, headers=h).status_code == 409
    cart = client.put(f"/v1/cart/items/{cola}", json={"qty": 0}, headers=h).json()
    assert cart["can_submit"]


def test_min_order_amount_and_empty_cart(client, session_factory):
    ctx, product, (mango, _) = _shop(client, session_factory, min_order_amount=300000)
    h = headers("shop", 10)
    r = client.post("/v1/cart/submit", json={}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "empty"
    client.put(f"/v1/cart/items/{mango}", json={"qty": 2}, headers=h)
    assert client.get("/v1/cart", headers=h).json()["blockers"] == ["below_min_amount"]
    client.put(f"/v1/cart/items/{mango}", json={"qty": 20}, headers=h)
    assert client.post("/v1/cart/submit", json={}, headers=h).status_code == 201


def test_no_price_blocks_submit(client, session_factory):
    ctx = setup_shop(client, session_factory, age_gate=False)
    product = create_product(client, ctx, variants=(), prices=())
    h = headers("shop", 10)
    cart = client.put(f"/v1/cart/items/{visible_variant_ids(product)[0]}", json={"qty": 1}, headers=h).json()
    assert cart["groups"][0]["lines"][0]["problem"] == "no_price"
    assert client.post("/v1/cart/submit", json={}, headers=h).status_code == 409


def test_my_orders_and_repeat(client, session_factory):
    ctx, product, (mango, cola) = _shop(client, session_factory)
    h = headers("shop", 10)
    client.put(f"/v1/cart/items/{mango}", json={"qty": 2}, headers=h)
    client.put(f"/v1/cart/items/{cola}", json={"qty": 3}, headers=h)
    order = client.post("/v1/cart/submit", json={}, headers=h).json()
    assert "manager_note" not in order

    page = client.get("/v1/orders", headers=h).json()
    assert page["total"] == 1 and page["items"][0]["id"] == order["id"]
    # чужую заявку не видно
    assert client.get(f"/v1/orders/{order['id']}", headers=headers("shop", 11)).status_code == 404

    client.delete(f"/v1/admin/products/{product['id']}/variants/{cola}", headers=ctx["owner"])
    r = client.post(f"/v1/orders/{order['id']}/repeat", headers=h).json()
    assert r == {"added": 1, "skipped": ["Жнец 30мл — Кола"]}
    cart = client.get("/v1/cart", headers=h).json()
    assert [(line["variant_id"], line["qty"]) for line in cart["groups"][0]["lines"]] == [(mango, 2)]
