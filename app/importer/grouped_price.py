"""
Парсер прайсов «группа → строки-вкусы» (формат «Alaska Trade», выгрузка из их учёта):
- строка заголовков «Наименование | Цена | Ед. | Доступно | Заказ | Сумма | Код товара»;
- строка-группа (только название, объединена на всю ширину) — линейка товара
  («YOVO 25000»), строки под ней — вкусы с ценой, остатком и кодом;
- название вкуса — строка без префикса линейки («YOVO 25000 - Арбуз» → «Арбуз»); если
  в группе несколько линеек («Podgonki Extra …», «Podgonki SOUR …») — это разные товары;
- раздел определяется по названию группы («Жидкость …», «ТАБАК ДЛЯ КАЛЬЯНА …», «Набор …»),
  остальное — одноразки;
- «Доступно» — остаток (0 — нет в наличии), уровень цены один.
Чистый модуль — без БД и FastAPI.
"""
from __future__ import annotations

import re
from collections import Counter
from io import BytesIO

import openpyxl

from app.importer.block_price import ParsedProduct, ParsedSheet, ParsedVariant, ParseResult

NEW_RE = re.compile(r"\s*новинка!*\s*", re.I)
# (префикс названия группы, раздел, что оставить в названии товара)
SECTIONS = [
    (re.compile(r"^жидкость\s+", re.I), "Жидкости"),
    (re.compile(r"^комплект\s+", re.I), "Самозамес"),
    (re.compile(r"^табак для кальяна\s+", re.I), "Табак для кальяна"),
    (re.compile(r"^жевательный табак\s*", re.I), "Жевательный табак"),
    (re.compile(r"^набор\s+", re.I), "Pod-системы"),
]
DEFAULT_SECTION = "Одноразки"
TWO_WORD_BRANDS = {"lost": "Lost Mary", "funky": "Funky Lands"}


def _text(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _header(ws) -> dict | None:
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=15), start=1):
        texts = [_text(c.value).lower() for c in row]
        if "наименование" in texts and "цена" in texts and "доступно" in texts:
            return {
                "row": r,
                "name": texts.index("наименование"),
                "price": texts.index("цена"),
                "stock": texts.index("доступно"),
                "code": next((i for i, t in enumerate(texts) if t.startswith("код")), None),
            }
    return None


def is_grouped_price(content: bytes) -> bool:
    try:
        wb = openpyxl.load_workbook(BytesIO(content), read_only=True)
        return any(_header(ws) for ws in wb.worksheets[:2])
    except Exception:
        return False


def _section(title: str) -> tuple[str, str]:
    for pattern, section in SECTIONS:
        if pattern.match(title):
            rest = pattern.sub("", title).strip()
            return section, rest or title
    return DEFAULT_SECTION, title


def _brand(name: str) -> str | None:
    words = name.split()
    if not words:
        return None
    first = words[0]
    if first.lower() in TWO_WORD_BRANDS:
        return TWO_WORD_BRANDS[first.lower()]
    return first if len(first) <= 4 or not first.isupper() else first.title()


def _tokens(text: str) -> list[str]:
    return text.split(" ")


def _pretty(flavor: str) -> str:
    flavor = flavor.strip(" -–,.")
    letters = [c for c in flavor if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 3:
        flavor = flavor.lower()
    return flavor[:1].upper() + flavor[1:]


def _split_rows(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Строки группы -> [(префикс линейки, строки с полем flavor)]"""
    dashed = [r for r in rows if " - " in r["name"]]
    if len(dashed) == len(rows):
        lines: dict[str, list[dict]] = {}
        for r in rows:
            prefix, flavor = r["name"].split(" - ", 1)
            lines.setdefault(prefix.strip().lower(), []).append({**r, "flavor": flavor, "prefix": prefix.strip()})
        return [(items[0]["prefix"], items) for items in lines.values()]

    # без « - »: общий префикс слов; повторяющееся следующее латинское слово
    # («Extra», «SOUR») — отдельная линейка
    token_rows = [_tokens(r["name"]) for r in rows]
    common = 0
    while all(len(t) > common + 1 for t in token_rows) and len({t[common].lower() for t in token_rows}) == 1:
        common += 1
    next_counts = Counter(t[common] for t in token_rows)
    lines = {}
    for r, tokens in zip(rows, token_rows):
        size = common
        word = tokens[common]
        if re.fullmatch(r"[A-Za-z]+", word) and next_counts[word] >= 3 and len(tokens) > common + 1:
            size += 1
        prefix = " ".join(tokens[:size])
        lines.setdefault(prefix.lower(), []).append({**r, "flavor": " ".join(tokens[size:]), "prefix": prefix})
    return [(items[0]["prefix"], items) for items in lines.values()]


def _strip_common_suffix(items: list[dict]) -> str | None:
    """«АБРИКОС, 25 гр.» / «ЛИМОН, 25 гр.» -> суффикс «25 гр.» уходит в описание"""
    if len(items) < 2:
        return None
    tails = [re.split(r",\s*|\s+", i["flavor"].strip()) for i in items]
    suffix: list[str] = []
    while all(len(t) > len(suffix) + 1 for t in tails):
        candidates = {t[-(len(suffix) + 1)].lower() for t in tails}
        if len(candidates) != 1:
            break
        suffix.insert(0, tails[0][-(len(suffix) + 1)])
    if not suffix:
        return None
    pattern = re.compile(r"[,\s]*" + r"[,\s]+".join(re.escape(s) for s in suffix) + r"\s*$", re.I)
    for i in items:
        i["flavor"] = pattern.sub("", i["flavor"])
    return " ".join(suffix).strip(" .,") + ("." if suffix[-1].endswith(".") else "")


def parse_grouped(content: bytes) -> ParseResult:
    wb = openpyxl.load_workbook(BytesIO(content))
    sections: dict[str, list[ParsedProduct]] = {}
    warnings: list[str] = []
    notes: list[str] = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        header = _header(ws)
        if header is None:
            continue
        notes += [_text(ws.cell(r, 1).value) for r in range(1, header["row"])]
        groups: list[tuple[int, str, list[dict]]] = []
        for r, row in enumerate(ws.iter_rows(min_row=header["row"] + 1), start=header["row"] + 1):
            name = _text(row[header["name"]].value)
            if not name:
                continue
            price = row[header["price"]].value
            if not isinstance(price, (int, float)):
                if name.lower().startswith("итого"):
                    break
                groups.append((r, name, []))
                continue
            if not groups:
                groups.append((r, "", []))
            stock = row[header["stock"]].value
            code = row[header["code"]].value if header["code"] is not None else None
            groups[-1][2].append({
                "r": r, "name": name, "price": round(price * 100),
                "stock": int(stock) if isinstance(stock, (int, float)) else None,
                "code": str(int(code)) if isinstance(code, (int, float)) else (_text(code) or None),
            })
        for r, title, rows in groups:
            if not rows:
                continue
            is_new = bool(NEW_RE.search(title))
            title = NEW_RE.sub(" ", title).strip()
            section, base = _section(title)
            lines = _split_rows(rows)
            for prefix, items in lines:
                suffix = _strip_common_suffix(items)
                if len(lines) == 1 and section != "Самозамес":
                    name = f"Табак {base}" if section == "Табак для кальяна" else base
                else:
                    name = _section(prefix)[1]
                    if section == "Жевательный табак" and (brand_word := base.split()[0]).lower() not in name.lower():
                        name = f"{brand_word} {name}"
                price = Counter(i["price"] for i in items).most_common(1)[0][0]
                variants = []
                seen: set[str] = set()
                for i in items:
                    flavor = _pretty(i["flavor"]) or i["name"]
                    if flavor.lower() in seen:
                        warnings.append(f"строка {i['r']}: повтор вкуса «{flavor}» в «{name}» — пропущен")
                        continue
                    seen.add(flavor.lower())
                    variants.append(ParsedVariant(
                        name=flavor, sku=i["code"], stock_qty=i["stock"], out=i["stock"] == 0,
                        prices={1: i["price"]} if i["price"] != price else None, source_name=i["name"],
                    ))
                description = []
                if is_new:
                    description.append("Новинка")
                if suffix:
                    description.append(suffix)
                sections.setdefault(section, []).append(ParsedProduct(
                    sheet=section, row=r, name=name, brand=_brand(base), prices={1: price}, variants=variants,
                    description=" · ".join(description) or None, out=all(v.out for v in variants),
                    category_path=[section], source_name=title,
                ))
    sheets = [ParsedSheet(title=title, products=products) for title, products in sections.items()]
    return ParseResult(sheets=sheets, warnings=warnings, tenant_defaults={"price_basis": "qty"})
