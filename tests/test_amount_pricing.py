"""Режим «уровень цены по сумме заявки» через API — как у Amigo (от 3 000 / 10 000 ₽, розница от 1 000)"""
import app.database.models as m
from tests.helpers import headers, seed_member, seed_tenant


def _amount_shop(client, session_factory):
    tenant_id = seed_tenant(session_factory, "amigo", age_gate=False, price_basis=m.PriceBasis.amount, min_order_amount=100000)
    seed_member(session_factory, tenant_id, 1, role=m.Role.owner)
    owner = headers("amigo", 1)
    tiers = []
    for label, amount in (("Розница от 1 000 ₽", 100000), ("от 3 000 ₽", 300000), ("от 10 000 ₽", 1000000)):
        r = client.post("/v1/admin/price-tiers", json={"label": label, "min_amount": amount}, headers=owner)
        assert r.status_code == 201, r.text
        assert r.json()["min_amount"] == amount and r.json()["min_qty"] is None
        tiers.append(r.json()["id"])
    products = []
    for name, prices in (("ICON 40000", (110000, 51000, 50000)), ("Жнец 30мл", (None, 26000, 25500))):
        p = client.post("/v1/admin/products", json={"name": name}, headers=owner).json()
        rows = [{"tier_id": t, "amount": a} for t, a in zip(tiers, prices) if a is not None]
        p = client.put(f"/v1/admin/products/{p['id']}/prices", json={"prices": rows}, headers=owner).json()
        products.append((p["id"], [v["id"] for v in p["variants"] if v["is_visible"]][0]))
    return owner, tiers, products


def test_amount_tiers_in_cart_and_card(client, session_factory):
    owner, tiers, [(icon, icon_v), (liquid, liquid_v)] = _amount_shop(client, session_factory)
    h = headers("amigo", 10)

    # 1 шт ICON: розница 1 100 ₽; жидкость без розничной цены берёт оптовую 260 ₽
    client.put(f"/v1/cart/items/{icon_v}", json={"qty": 1}, headers=h)
    cart = client.put(f"/v1/cart/items/{liquid_v}", json={"qty": 1}, headers=h).json()
    assert cart["price_basis"] == "amount" and cart["tier"]["tier_id"] == tiers[0]
    assert cart["total"] == 110000 + 26000
    assert cart["next_tier"]["min_amount"] == 300000 and cart["amount_to_next_tier"] == 300000 - (51000 + 26000)
    assert cart["can_submit"]  # 1 360 ₽ >= минимальной 1 000 ₽

    # 6 шт ICON: 6×510 = 3 060 ₽ в оптовых ценах — весь заказ по «от 3 000»
    cart = client.put(f"/v1/cart/items/{icon_v}", json={"qty": 6}, headers=h).json()
    assert cart["tier"]["tier_id"] == tiers[1]
    assert {line["unit_price"] for g in cart["groups"] for line in g["lines"]} == {51000, 26000}
    assert all(g["tier"]["tier_id"] == tiers[1] for g in cart["groups"])

    # карточка подсвечивает уровень всей заявки
    card = client.get(f"/v1/catalog/products/{liquid}", headers=h).json()
    assert card["current_tier_id"] == tiers[1] and card["qty_to_next_tier"] is None
    assert [t["min_amount"] for t in card["tiers"]] == [100000, 300000, 1000000]

    order = client.post("/v1/cart/submit", json={}, headers=h).json()
    assert order["total"] == 6 * 51000 + 26000
    assert {i["tier_label"] for i in order["items"]} == {"от 3 000 ₽"}


def test_below_min_order(client, session_factory):
    owner, tiers, [(icon, icon_v), (liquid, liquid_v)] = _amount_shop(client, session_factory)
    h = headers("amigo", 10)
    cart = client.put(f"/v1/cart/items/{liquid_v}", json={"qty": 2}, headers=h).json()
    assert cart["total"] == 52000 and cart["blockers"] == ["below_min_amount"]


def test_tier_threshold_validation_and_basis_switch(client, session_factory):
    owner, tiers, _ = _amount_shop(client, session_factory)
    assert client.post("/v1/admin/price-tiers", json={"label": "x", "min_qty": 5}, headers=owner).status_code == 422
    assert client.post("/v1/admin/price-tiers", json={"label": "x", "min_amount": 300000}, headers=owner).status_code == 409
    # режим не меняется, пока есть уровни в старом формате
    r = client.patch("/v1/admin/settings", json={"price_basis": "qty"}, headers=owner)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tiers_exist"
    for t in tiers:
        client.delete(f"/v1/admin/price-tiers/{t}", headers=owner)
    r = client.patch("/v1/admin/settings", json={"price_basis": "qty"}, headers=owner)
    assert r.status_code == 200 and r.json()["price_basis"] == "qty"
    assert client.get("/v1/me", headers=owner).json()["tenant"]["price_basis"] == "qty"
