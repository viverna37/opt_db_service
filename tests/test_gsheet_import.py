"""Прайс из Google-таблицы через htmlview (формат BESTSALE.OPT)"""
from app.importer.gsheet_html import parse_sheets


def _row(n: int, *cells: str) -> str:
    return f'<tr><th id="1R{n}"><div>{n + 1}</div></th>' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


PAGE = "<table>" + "".join([
    _row(0, "Картинка", "Полное наименование", "Мелкий опт 5-20 тыс", "Средний опт 20-50 тыс", "Крупный опт от 50 тыс", "Вкусы"),
    '<tr><th id="1R1"><div>2</div></th><td colspan="6">Минимальный заказ 5000 рублей. Работаем по полной предоплате.</td></tr>',
    '<tr><th id="1R2"><div>3</div></th><td colspan="6"><a href="https://www.google.com/url?q=https://t.me/bestsale_opt&amp;sa=D">написать в TG</a></td></tr>',
    _row(3, "НИКОБУСТЕР", "", "", "", "", ""),
    _row(4, '<img src="https://docs.google.com/sheets-images-rt/abc=w120-h84">', "Никобустер 5%", "35", "31", "28", "-"),
    _row(5, "", "", "MRAZZ", "", "", ""),
    _row(6, "", "MRAZZ! 70mg", "305", "285", "275", "Вкусы"),
    _row(7, "Жидкости от Brusko", "", "", "", "", ""),
    _row(8, "", "SKALA 2% 5%", "250", "230", "220", "Вкусы"),
    _row(9, "", "Бокс жидкостей (100 шт)", "6 500₽", "", "", "Микс"),
]) + "</table>"


def test_parse_sheet():
    result = parse_sheets([("Жижи", PAGE)], images={"https://docs.google.com/sheets-images-rt/abc=w120-h84": b"jpeg"})
    assert result.tenant_defaults == {"min_order_amount": 5000, "manager_username": "bestsale_opt", "price_basis": "amount"}
    booster, mrazz, skala, box = result.products
    assert booster.category_path == ["Жидкости", "Никобустер"] and booster.image == b"jpeg"
    assert booster.prices == {5000: 3500, 20000: 3100, 50000: 2800}
    assert mrazz.brand == "MRAZZ" and mrazz.category_path == ["Жидкости"] and mrazz.description is None
    assert skala.brand == "Brusko" and skala.category_path == ["Жидкости"]
    assert box.prices == {5000: 650000} and box.description == "Микс"
