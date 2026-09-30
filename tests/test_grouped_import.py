"""Парсер прайса «группа → строки-вкусы» (формат Alaska Trade)"""
from io import BytesIO

import openpyxl

from app.importer.detect import parse_price_file


def _workbook() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ALASKA TRADE — Прайс-лист"])
    ws.append(["Наименование", "Цена", "Ед.", "Доступно", "Заказ", "Сумма", "Код товара"])
    ws.append(["YOVO 25000 Новинка!"])
    ws.append(["YOVO 25000 - Арбуз", 1490, "шт", 7, 0, None, 601])
    ws.append(["YOVO 25000 - Клубника", 1590, "шт", 0, 0, None, 602])
    ws.append(["Комплект Podonki Глицерин + Ароматизатор"])
    for flavor in ("Вишня", "Лимон", "Манго"):
        ws.append([f"Ароматизатор Podonki Podgonki SOUR {flavor}", 195, "шт", 5, 0, None, 700])
    ws.append(["Ароматизатор Podonki Podgonki Банан", 195, "шт", 5, 0, None, 710])
    ws.append(["Жидкость VLIQ OGGO ICE"])
    ws.append(["VLIQ OGGO ICE - Вишня 20мг", 415, "шт", 3, 0, None, 801])
    ws.append(["VLIQ OGGO ICE - Мята 20мг", 415, "шт", 3, 0, None, 802])
    ws.append(["ИТОГО:"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_groups_lines_and_stock():
    kind, result = parse_price_file(_workbook(), "alaska.xlsx")
    assert kind == "grouped"
    by_name = {p.name: p for p in result.products}
    yovo = by_name["YOVO 25000"]
    assert yovo.category_path == ["Одноразки"] and yovo.description == "Новинка"
    arbuz, klubnika = yovo.variants
    assert (arbuz.name, arbuz.stock_qty, arbuz.sku, arbuz.out) == ("Арбуз", 7, "601", False)
    assert klubnika.out and klubnika.prices == {1: 159000}
    # две линейки в одной группе — два товара
    assert [v.name for v in by_name["Ароматизатор Podonki Podgonki SOUR"].variants] == ["Вишня", "Лимон", "Манго"]
    assert by_name["Ароматизатор Podonki Podgonki"].category_path == ["Самозамес"]
    liquid = by_name["VLIQ OGGO ICE"]
    assert liquid.category_path == ["Жидкости"] and [v.name for v in liquid.variants] == ["Вишня", "Мята"]
    assert liquid.description == "20мг"
