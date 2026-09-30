"""Импорт простого прайса «строка = товар» с уровнями по количеству (формат «Дымка»)"""
import io
from argparse import Namespace

from openpyxl import Workbook

import app.database.models as m
from app.importer.detect import parse_price_file
from tests.helpers import headers, run, seed_member, seed_tenant


def build_simple() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Одноразки"
    ws.append(["Одноразки"])
    ws.append(["Фото товара", "Наименование товара", "Вкусы", "от 5 штук", "от 50 штук", "от 100 штук"])
    ws.append([None, "WAKA 8000 тяг", "Свежая мята\nКислое яблоко\nМанго лед", 750, 700, None])
    ws.append([None, "ELF BAR ICE KING 30000 тяг❌❌❌", None, 800, None, None])
    cart = wb.create_sheet("Картриджииспар")
    cart.append(["Картриджи/испарители"])
    cart.append(["Фото товара", "Наименование товара", "Сопративление", "1 пачка", "Блок(10 пачек)", "10 блоков"])
    cart.append([None, "XROS", "0.4\n0.6", "1 шт - 720\n(Пачка 4 шт)", 700, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_simple_parser():
    kind, res = parse_price_file(build_simple(), "dymok.xlsx")
    assert kind == "simple" and res.tenant_defaults["price_basis"] == "qty"
    waka, elf = res.sheets[0].products
    assert waka.prices == {5: 75000, 50: 70000} and [v.name for v in waka.variants] == ["Свежая мята", "Кислое яблоко", "Манго лед"]
    assert elf.name == "ELF BAR ICE KING 30000 тяг" and elf.out
    xros = res.sheets[1].products[0]
    assert res.sheets[1].title == "Картриджи/испарители"
    assert xros.prices == {1: 288000, 10: 280000} and [v.name for v in xros.variants] == ["0,4 Ом", "0,6 Ом"]
    assert "пачками по 4 шт" in xros.description


def test_simple_import_qty_tiers(client, session_factory, tmp_path):
    from app.cli import _with_repo, import_price

    tenant_id = seed_tenant(session_factory, "dymok", age_gate=False)
    seed_member(session_factory, tenant_id, 1, role=m.Role.owner)
    owner = headers("dymok", 1)
    path = tmp_path / "dymok.xlsx"
    path.write_bytes(build_simple())
    run(_with_repo(import_price, Namespace(slug="dymok", file=str(path), wipe=False, no_photos=True, dry_run=False)))

    assert client.get("/v1/me", headers=owner).json()["tenant"]["price_basis"] == "qty"
    tiers = [(t["label"], t["min_qty"]) for t in client.get("/v1/admin/price-tiers", headers=owner).json()]
    assert tiers == [("от 1 шт", 1), ("от 5 шт", 5), ("от 10 шт", 10), ("от 50 шт", 50)]
    products = {p["name"]: p for p in client.get("/v1/admin/products", headers=owner).json()["items"]}
    waka = client.get(f"/v1/catalog/products/{products['WAKA 8000 тяг']['id']}", headers=owner).json()
    h = headers("dymok", 10)
    mint, apple = waka["variants"][0]["id"], waka["variants"][1]["id"]
    # 30 + 20 шт одного товара = 50 -> цена «от 50 шт» на оба вкуса
    client.put(f"/v1/cart/items/{mint}", json={"qty": 30}, headers=h)
    cart = client.put(f"/v1/cart/items/{apple}", json={"qty": 20}, headers=h).json()
    assert cart["total"] == 50 * 70000
    # 3 шт: младше «от 5» — цена «от 5» (нет цены ниже — берётся ближайшая старшая)
    cart = client.put(f"/v1/cart/items/{apple}", json={"qty": 0}, headers=h).json()
    cart = client.put(f"/v1/cart/items/{mint}", json={"qty": 3}, headers=h).json()
    assert cart["total"] == 3 * 75000
    assert products["ELF BAR ICE KING 30000 тяг"]["stock_status"] == "out"
