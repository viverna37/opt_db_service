"""
Парсер табличных прайсов-выгрузок из учётных систем (МойСклад и похожие,
формат прайса «Галактики»). Чистый модуль — без БД и FastAPI.

Как устроен файл:
- строка заголовков: «Тип», «Код товара модификации», «Наименование»,
  «Ед.изм.», уровни цен «От 20 тыс» / «От 50 тыс» / «От 1 млн» (по сумме
  заявки);
- строка-группа: пустой «Тип», в «Наименовании» путь
  «Товары/1. ЖЕЛЕЗО (Наборы)/1. GEEKVAPE/Aegis Force» -> категории, бренд
  (первое латинское звено пути), модель;
- «Модификация» — вариант товара: товары с одинаковым кодом склеиваются,
  вариант — текст в последних скобках «Набор … Kit (Carbon Black)»;
- «Товар» — самостоятельный товар; позиции одной группы, отличающиеся
  только сопротивлением («GTX 0,15 Ом Mesh Coil» / «GTX 0,2 Ом …»),
  склеиваются в товар с вариантами по сопротивлению;
- нулевые цены (промо, «не для продажи») пропускаются;
- шапка над таблицей: условия (минимальная сумма заказа и т.п.).
"""
from __future__ import annotations

import re
from io import BytesIO

from app.importer.block_price import ParsedProduct, ParsedSheet, ParsedVariant, ParseResult

TYPE_HEADERS = {"тип"}
NAME_HEADERS = {"наименование"}
CODE_HEADERS = {"код товара модификации", "код", "артикул", "код товара"}
TIER_HEADER_RE = re.compile(r"^от\s*([\d\s.,]+)\s*(тыс|млн|₽|руб|р)?\.?$", re.IGNORECASE)
HOT_RE = re.compile(r"\s*[-–—]\s*горячее предложение!?\s*$", re.IGNORECASE)
NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\.\s*")
NEW_MARK_RE = re.compile(r"\s*\((?:новинка|new)[^()]*\)\s*", re.IGNORECASE)
LATIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &.\-+']*$")
RESISTANCE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(Ω|Ом|ом|ohm|om)(?![A-Za-zА-Яа-я])", re.IGNORECASE)
SKIP_ROOTS = {"промо (мерч)", "промо"}
MIN_ORDER_RE = re.compile(r"минимальн\w*\s+сумм\w*\s+заказа\s*[—\-:]*\s*([\d\s]+)", re.IGNORECASE)
TG_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{4,})")
MOYSKLAD_RE = re.compile(r"https?://b2b\.moysklad\.ru/public/([A-Za-z0-9_\-]+)")


# ---------- чтение файла в «сетку» значений ----------

def _grid_xls(content: bytes) -> tuple[list[list], list[str]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    grid = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
    links = [link.url_or_path for link in getattr(sheet, "hyperlink_list", [])]
    return grid, links


def _grid_xlsx(content: bytes) -> tuple[list[list], list[str]]:
    import openpyxl

    wb = openpyxl.load_workbook(BytesIO(content), read_only=False)
    ws = wb.worksheets[0]
    grid = [[c.value if c.value is not None else "" for c in row] for row in ws.iter_rows()]
    links = [c.hyperlink.target for row in ws.iter_rows() for c in row if c.hyperlink and c.hyperlink.target]
    return grid, links


def read_grid(content: bytes, filename: str) -> tuple[list[list], list[str]]:
    return _grid_xls(content) if filename.lower().endswith(".xls") else _grid_xlsx(content)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(_text(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _threshold(label: str) -> int | None:
    """«От 20 тыс» -> 20000, «От 1 млн» -> 1000000, «От 3 000 ₽» -> 3000"""
    m = TIER_HEADER_RE.match(label.strip())
    if not m:
        return None
    number = float(m.group(1).replace(" ", "").replace(",", "."))
    unit = (m.group(2) or "").lower()
    return int(number * (1000 if unit == "тыс" else 1_000_000 if unit == "млн" else 1))


def find_header(grid: list[list]) -> dict | None:
    """Строка заголовков таблицы и номера колонок; None — это не табличный прайс"""
    for r, row in enumerate(grid[:40]):
        texts = [_text(v).lower() for v in row]
        name_cols = [c for c, t in enumerate(texts) if t in NAME_HEADERS]
        tiers = {c: _threshold(_text(v)) for c, v in enumerate(row) if _threshold(_text(v))}
        if name_cols and tiers:
            # колонка «Тип» и код — в строке заголовков экспорта (обычно первая строка файла)
            type_col = code_col = None
            for rr in range(0, r + 1):
                for c, t in enumerate(_text(v).lower() for v in grid[rr]):
                    if t in TYPE_HEADERS and type_col is None:
                        type_col = c
                    if t in CODE_HEADERS and code_col is None:
                        code_col = c
            return {"row": r, "name": name_cols[-1], "tiers": tiers, "type": type_col, "code": code_col}
    return None


def is_table_price(content: bytes, filename: str) -> bool:
    try:
        grid, _ = read_grid(content, filename)
    except Exception:
        return False
    return find_header(grid) is not None


# ---------- разбор ----------

def _clean_segment(text: str) -> tuple[str, bool]:
    hot = bool(HOT_RE.search(text))
    text = HOT_RE.sub("", NUMBER_PREFIX_RE.sub("", text)).strip()
    return text, hot


def _category_name(segment: str) -> str:
    """«ЖЕЛЕЗО (Наборы)» -> «Железо (наборы)», «СМЕННЫЙ КАРТРИДЖ» -> «Сменный картридж»"""
    letters = [ch for ch in segment if ch.isalpha()]
    upper = sum(1 for ch in letters if ch.isupper())
    if letters and upper / len(letters) >= 0.5:
        return segment[:1].upper() + segment[1:].lower()
    return segment


def _split_last_parens(name: str) -> tuple[str, str | None]:
    """«Набор X Kit (Lightning Yellow (Новинка 06.26))» -> («Набор X Kit», «Lightning Yellow (Новинка 06.26)»)"""
    text = name.rstrip()
    if not text.endswith(")"):
        return text, None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return text[:i].rstrip(), text[i + 1:-1].strip()
    return text, None


def _clean_name(text: str) -> tuple[str, bool]:
    """Убирает «(новинка 07.26)» из названия; второй элемент — была ли пометка"""
    is_new = bool(NEW_MARK_RE.search(text))
    return re.sub(r"\s+", " ", NEW_MARK_RE.sub(" ", text)).strip(), is_new


def parse_table(content: bytes, filename: str) -> ParseResult:
    grid, links = read_grid(content, filename)
    header = find_header(grid)
    if header is None:
        raise ValueError("Не нашёл строку заголовков с «Наименование» и уровнями цен «От …»")
    warnings: list[str] = []
    name_col, type_col, code_col, tier_cols = header["name"], header["type"], header["code"], header["tiers"]

    # Шапка над таблицей: условия и контакты
    head_text = "\n".join(_text(v) for row in grid[: header["row"]] for v in row if _text(v))
    defaults: dict = {}
    if m := MIN_ORDER_RE.search(head_text):
        defaults["min_order_amount"] = int(re.sub(r"\s", "", m.group(1)))
    tg = [m.group(1) for link in links if (m := TG_RE.search(link or ""))]
    if tg:
        defaults["manager_username"] = tg[0]
    moysklad = [m.group(1) for link in links if (m := MOYSKLAD_RE.search(link or ""))]
    if moysklad:
        defaults["moysklad_public_id"] = moysklad[0]  # публичный B2B-каталог: фото и остатки
    conditions = [line.strip(" -•\n") for line in head_text.splitlines() if line.strip().startswith("-")]
    if conditions:
        defaults["welcome_text"] = "\n".join(f"• {c}" for c in conditions[:6])

    groups: list[dict] = []  # {path, brand, hot, rows}
    current: dict | None = None
    for r in range(header["row"] + 1, len(grid)):
        row = grid[r]
        name = _text(row[name_col]) if name_col < len(row) else ""
        row_type = _text(row[type_col]) if type_col is not None and type_col < len(row) else ""
        if not name:
            continue
        if not row_type and "/" in name:
            segments = [s for s in name.split("/") if s.strip()]
            if segments and segments[0].strip().lower() in {"товары", "products"}:
                segments = segments[1:]
            cleaned = [_clean_segment(s) for s in segments]
            current = {"segments": [c[0] for c in cleaned], "hot": any(c[1] for c in cleaned), "rows": []}
            groups.append(current)
            continue
        if current is None:
            current = {"segments": [], "hot": False, "rows": []}
            groups.append(current)
        prices = {}
        for col, threshold in tier_cols.items():
            value = _number(row[col]) if col < len(row) else None
            if value and value > 0:
                prices[threshold] = round(value * 100)
        code = _text(row[code_col]) if code_col is not None and code_col < len(row) else ""
        current["rows"].append({"r": r + 1, "type": row_type.lower(), "name": name, "code": code or None, "prices": prices})

    brand_names: dict[str, str] = {}
    sheets: dict[str, ParsedSheet] = {}
    for group in groups:
        segments = group["segments"]
        if segments and segments[0].lower() in SKIP_ROOTS:
            continue
        brand_idx = next((i for i, s in enumerate(segments) if i > 0 and LATIN_RE.match(s)), None)
        brand = None
        if brand_idx is not None:
            key = segments[brand_idx].lower().replace(" ", "")
            brand = brand_names.setdefault(key, segments[brand_idx])
        category_path = [_category_name(s) for s in (segments[:brand_idx] if brand_idx is not None else segments)]
        if not category_path:
            category_path = ["Каталог"]
        sheet = sheets.setdefault(category_path[0], ParsedSheet(title=category_path[0], products=[]))
        for product in _group_products(group, brand, category_path, warnings):
            sheet.products.append(product)

    # одинаковые названия в одной категории — разводим номером
    for sheet in sheets.values():
        seen: dict[tuple, int] = {}
        for p in sheet.products:
            key = (tuple(p.category_path or []), p.name.lower())
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                p.name = f"{p.name} #{seen[key]}"
    return ParseResult(sheets=[s for s in sheets.values() if s.products], warnings=warnings, tenant_defaults=defaults)


def _group_products(group: dict, brand: str | None, category_path: list[str], warnings: list[str]) -> list[ParsedProduct]:
    products: list[ParsedProduct] = []
    rows = [row for row in group["rows"] if row["prices"]]
    for row in group["rows"]:
        if not row["prices"]:
            warnings.append(f"строка {row['r']}: «{row['name']}» — нулевая цена, пропущена")

    def make(name: str, sku: str | None, row: dict) -> ParsedProduct:
        clean, is_new = _clean_name(name)
        notes = []
        if is_new:
            notes.append("Новинка")
        if group["hot"]:
            notes.append("Горячее предложение")
        return ParsedProduct(
            sheet=category_path[0], row=row["r"], name=clean, brand=brand, prices=dict(row["prices"]),
            description=" · ".join(notes) or None, sku=sku, category_path=category_path, source_name=row["name"],
        )

    # «Модификация»: склейка по коду (или по названию без скобок)
    by_key: dict[str, ParsedProduct] = {}
    for row in [r for r in rows if r["type"].startswith("модиф")]:
        base, variant = _split_last_parens(row["name"])
        key = row["code"] or base.lower()
        product = by_key.get(key)
        if product is None:
            product = make(base, row["code"], row)
            by_key[key] = product
            products.append(product)
        if variant:
            vname, _ = _clean_name(variant)
            vname = re.sub(r"^new colou?r\s+", "", vname, flags=re.IGNORECASE)
            product.variants.append(ParsedVariant(name=vname or variant, prices=dict(row["prices"]), source_name=row["name"]))

    # «Товар»: склейка позиций, отличающихся только сопротивлением
    singles = [r for r in rows if not r["type"].startswith("модиф")]
    buckets: dict[str, list[tuple[dict, str]]] = {}
    order: list[str] = []
    for row in singles:
        m = RESISTANCE_RE.search(row["name"])
        if m:
            base = re.sub(r"\s+", " ", (row["name"][: m.start()] + " " + row["name"][m.end():])).strip()
            key = base.lower()
            token = m.group(0).strip()
        else:
            key, token = f"__single_{row['r']}", ""
        if key not in buckets:
            order.append(key)
        buckets.setdefault(key, []).append((row, token))
    for key in order:
        items = buckets[key]
        if len(items) == 1:
            products.append(make(items[0][0]["name"], items[0][0]["code"], items[0][0]))
            continue
        first_row = items[0][0]
        base = re.sub(r"\s+", " ", (first_row["name"][: RESISTANCE_RE.search(first_row["name"]).start()]
                                   + " " + first_row["name"][RESISTANCE_RE.search(first_row["name"]).end():])).strip()
        base = re.sub(r"\s+([,)])", r"\1", base)
        product = make(base, None, first_row)
        for row, token in items:
            product.variants.append(ParsedVariant(name=token, prices=dict(row["prices"]), source_name=row["name"]))
        products.append(product)

    # цена товара — цена первого варианта; варианты с такой же ценой не храним отдельно
    for product in products:
        if product.variants:
            product.prices = dict(product.variants[0].prices or product.prices)
            for v in product.variants:
                if v.prices == product.prices:
                    v.prices = None
    return products
