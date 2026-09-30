"""Выбор парсера по содержимому файла: табличная выгрузка (МойСклад) или блочный прайс (Amigo)."""
from app.importer.block_price import ParseResult, parse_workbook
from app.importer.table_price import is_table_price, parse_table


def parse_price_file(content: bytes, filename: str, with_images: bool = True) -> tuple[str, ParseResult]:
    if filename.lower().endswith(".xls") or is_table_price(content, filename):
        return "table", parse_table(content, filename)
    return "blocks", parse_workbook(content, with_images=with_images)
