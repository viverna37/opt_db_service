"""
Выбор парсера по содержимому файла:
- table  — табличная выгрузка МойСклада (группы-пути, «Модификация»/«Товар», .xls);
- simple — «строка = товар» с уровнями по количеству («Дымок», Google Sheets);
- blocks — блочный прайс-«карточки» (Amigo Opt).
"""
from app.importer.block_price import ParseResult, parse_workbook
from app.importer.simple_price import is_simple_price, parse_simple
from app.importer.table_price import is_table_price, parse_table


def parse_price_file(content: bytes, filename: str, with_images: bool = True) -> tuple[str, ParseResult]:
    if filename.lower().endswith(".xls") or is_table_price(content, filename):
        return "table", parse_table(content, filename)
    if is_simple_price(content):
        return "simple", parse_simple(content, with_images=with_images)
    return "blocks", parse_workbook(content, with_images=with_images)
