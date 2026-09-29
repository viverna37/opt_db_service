"""Изоляция тенантов: всё, что создано у одного оптовика, не видно и не трогается из другого"""
import app.database.models as m
from tests.helpers import create_product, headers, seed_member, setup_shop, visible_variant_ids


def test_catalog_cart_orders_and_admin_are_isolated(client, session_factory):
    a = setup_shop(client, session_factory, slug="a", owner_tg=1, age_gate=False)
    b = setup_shop(client, session_factory, slug="b", owner_tg=2, age_gate=False)
    product_a = create_product(client, a, name="Товар A")
    variant_a = visible_variant_ids(product_a)[0]

    # клиент 10 — в обоих каталогах, но это разные участники
    ha, hb = headers("a", 10), headers("b", 10)
    assert client.get("/v1/me", headers=ha).json()["id"] != client.get("/v1/me", headers=hb).json()["id"]

    assert client.get("/v1/catalog/products", headers=hb).json()["total"] == 0
    assert client.get(f"/v1/catalog/products/{product_a['id']}", headers=hb).status_code == 404
    assert client.put(f"/v1/cart/items/{variant_a}", json={"qty": 1}, headers=hb).status_code == 404

    client.put(f"/v1/cart/items/{variant_a}", json={"qty": 1}, headers=ha)
    order = client.post("/v1/cart/submit", json={}, headers=ha).json()
    assert client.get("/v1/orders", headers=hb).json()["total"] == 0
    assert client.get(f"/v1/orders/{order['id']}", headers=hb).status_code == 404

    # владелец B — не админ в A и не видит заявок A через свой тенант
    assert client.get("/v1/admin/orders", headers=headers("a", 2)).status_code == 403
    assert client.get(f"/v1/admin/orders/{order['id']}", headers=b["owner"]).status_code == 404
    assert client.get(f"/v1/admin/products/{product_a['id']}", headers=b["owner"]).status_code == 404
    r = client.patch(f"/v1/admin/products/{product_a['id']}", json={"name": "взлом"}, headers=b["owner"])
    assert r.status_code == 404
    assert client.get("/v1/admin/carts", headers=b["owner"]).json() == []

    # цены товара A нельзя повесить на уровни B, а категорию B — на товар A
    rows = [{"tier_id": b["tiers"][0], "amount": 1}]
    assert client.put(f"/v1/admin/products/{product_a['id']}/prices", json={"prices": rows},
                      headers=a["owner"]).status_code == 422
    cat_b = client.post("/v1/admin/categories", json={"name": "B"}, headers=b["owner"]).json()
    r = client.patch(f"/v1/admin/products/{product_a['id']}", json={"category_id": cat_b["id"]}, headers=a["owner"])
    assert r.status_code == 422

    # номера заявок сквозные внутри тенанта
    seed_member(session_factory, b["tenant_id"], 20)
    product_b = create_product(client, b, name="Товар B")
    client.put(f"/v1/cart/items/{visible_variant_ids(product_b)[0]}", json={"qty": 1}, headers=headers("b", 20))
    assert client.post("/v1/cart/submit", json={}, headers=headers("b", 20)).json()["number"] == 1

    # клиента, который есть только у B, владелец A не видит (владелец B, заходивший в A, для A — обычный клиент)
    clients_a = {c["user"]["telegram_id"]: c["role"] for c in
                 client.get("/v1/admin/clients", headers=a["owner"]).json()["items"]}
    assert 20 not in clients_a
    assert clients_a[1] == m.Role.owner.value and clients_a[2] == m.Role.client.value
