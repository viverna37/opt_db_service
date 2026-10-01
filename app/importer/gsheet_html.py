"""
Импорт прайса прямо по ссылке на Google-таблицу (через публичный htmlview).

Зачем: xlsx-экспорт больших таблиц с картинками в ячейках Google отдаёт очень медленно
и обрывает, а htmlview приходит за секунды и содержит ссылки на картинки ячеек
(ссылки живут несколько минут — скачиваем сразу).

Формат прайса («BESTSALE.OPT»):
- лист = раздел; строка заголовков «Картинка | Полное наименование | Мелкий опт 5-20 тыс |
  Средний опт 20-50 тыс | Крупный опт от 50 тыс | Вкусы» — уровни по сумме заявки;
- строка без цен с одним текстом — подгруппа: латиница — бренд («MRAZZ»), кириллица —
  подкатегория («Снюс»); контакты/условия («ВАЖНО», «whatsapp») пропускаются;
- «ЦЕНЫ УКАЗАНЫ ЗА ПАЧКУ» — пометка в описание товаров листа;
- текст в колонке «Вкусы», кроме заглушки «Вкусы», — описание (состав боксов).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from html.parser import HTMLParser

import httpx

from app.importer.block_price import ParsedProduct, ParsedSheet, ParseResult

log = logging.getLogger(__name__)

GSHEET_RE = re.compile(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_\-]+)")
SHEET_LIST_RE = re.compile(r'name: "((?:[^"\\]|\\.)*)", pageUrl: "[^"]*", gid: "(\d+)"')
RANGE_TIER_RE = re.compile(r"(\d+)\s*[-–]\s*\d+\s*(тыс|к)", re.I)
FROM_TIER_RE = re.compile(r"от\s*(\d+)\s*(тыс|к)", re.I)
PRICE_RE = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|р\.?|руб\.?)?\s*$")
MIN_ORDER_RE = re.compile(r"минимальн\w*\s+заказ\w*\s*(?:от\s*)?([\d\s]+)", re.I)
TG_RE = re.compile(r"t\.me/([A-Za-z0-9_]{4,})")
INFO_RE = re.compile(
    r"whatsapp|telegram|написать|важно|bestsale|по вопросам|детали|оптов\w+ компания|минимальн|"
    r"предоплат|отправляем|условие",
    re.I,
)
PER_PACK_RE = re.compile(r"цены указаны за пачку", re.I)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
PLACEHOLDERS = {"вкусы", "-", "—", ""}
IMAGE_SIZE = "=w800-h800"


class _Grid(HTMLParser):
    """Таблица htmlview -> {номер строки: {колонка: {"text", "img", "href"}}} с учётом colspan/rowspan"""

    def __init__(self):
        super().__init__()
        self.rows: dict[int, dict[int, dict]] = {}
        self._row: int | None = None
        self._col = 0
        self._cell: dict | None = None
        self._span: dict[tuple[int, int], bool] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "th" and (m := re.search(r"R(\d+)$", a.get("id", ""))):
            self._row = int(m.group(1)) + 1
            self._col = 0
        elif tag == "td" and self._row is not None:
            while (self._row, self._col) in self._span:
                self._col += 1
            colspan, rowspan = int(a.get("colspan", 1)), int(a.get("rowspan", 1))
            self._cell = {"text": "", "img": None, "href": None, "col": self._col}
            for dr in range(rowspan):
                for dc in range(colspan):
                    if dr or dc:
                        self._span[(self._row + dr, self._col + dc)] = True
            self._next_col = self._col + colspan
        elif self._cell is not None:
            if tag == "br":
                self._cell["text"] += "\n"
            elif tag == "img" and a.get("src", "").startswith("https://docs.google.com/sheets-images-rt/"):
                self._cell["img"] = a["src"]
            elif tag == "a" and a.get("href"):
                self._cell["href"] = a["href"]

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None and self._row is not None:
            self._cell["text"] = self._cell["text"].strip()
            self.rows.setdefault(self._row, {})[self._cell["col"]] = self._cell
            self._col = self._next_col
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_grid(page: str) -> dict[int, dict[int, dict]]:
    grid = _Grid()
    grid.feed(page)
    return grid.rows


def _threshold(label: str) -> int | None:
    """«Мелкий опт 5-20 тыс» -> 5000, «Крупный опт от 50 тыс» -> 50000"""
    if m := RANGE_TIER_RE.search(label) or FROM_TIER_RE.search(label):
        return int(m.group(1)) * 1000
    return None


def _price(text: str) -> int | None:
    if m := PRICE_RE.match(text.replace(" ", " ")):
        value = float(re.sub(r"\s", "", m.group(1)).replace(",", "."))
        return round(value * 100) if value > 0 else None
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", EMOJI_RE.sub("", text)).strip(" .")


def _unescape_js(text: str) -> str:
    """JS-строка из htmlview: «\\x3d», «\\u0026» -> символы (кириллица там как есть)"""
    text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return html.unescape(text.replace('\\"', '"').replace("\\/", "/"))


def _sheet_title(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return {"Эл.сигареты": "Электронные сигареты", "Жижи": "Жидкости"}.get(name, name)


FROM_BRAND_RE = re.compile(r"^[А-Яа-яЁё ]+\s+от\s+([A-Za-z][A-Za-z0-9 &.\-]*)$")


def _group_name(text: str) -> str:
    """«ПЛАСТИНКИ LOST MARY» -> «Пластинки Lost Mary»"""
    if text.isupper():
        text = " ".join(w.title() if _is_latin(w) else w.lower() for w in text.split())
    return text[:1].upper() + text[1:]


def _is_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all("a" <= c.lower() <= "z" for c in letters)


def parse_sheets(pages: list[tuple[str, str]], images: dict[str, bytes] | None = None) -> ParseResult:
    images = images or {}
    sheets: list[ParsedSheet] = []
    warnings: list[str] = []
    defaults: dict = {}
    for name, page in pages:
        rows = parse_grid(page)
        header_row = tiers = None
        name_col = flavor_col = None
        for r in sorted(rows):
            cells = rows[r]
            texts = {c: cell["text"].lower() for c, cell in cells.items()}
            if any("наименование" in t for t in texts.values()):
                found = {c: _threshold(cells[c]["text"]) for c in cells if _threshold(cells[c]["text"])}
                if found:
                    header_row, tiers = r, found
                    name_col = next(c for c, t in texts.items() if "наименование" in t)
                    flavor_col = next((c for c, t in texts.items() if t.startswith("вкус")), None)
                    break
        if header_row is None:
            warnings.append(f"лист «{name}»: нет строки заголовков с уровнями цен — пропущен")
            continue
        title = _sheet_title(name)
        sub: str | None = None
        brand: str | None = None
        per_pack = False
        products: list[ParsedProduct] = []
        for r in sorted(rows):
            cells = rows[r]
            all_text = " ".join(c["text"] for c in cells.values())
            if m := MIN_ORDER_RE.search(all_text):
                defaults.setdefault("min_order_amount", int(re.sub(r"\s", "", m.group(1))))
            for cell in cells.values():
                if cell["href"] and (m := TG_RE.search(html.unescape(cell["href"]))):
                    defaults.setdefault("manager_username", m.group(1))
            if r <= header_row:
                continue
            prices = {t: p for c, t in tiers.items() if c in cells and (p := _price(cells[c]["text"]))}
            product_name = _clean(cells.get(name_col, {}).get("text", ""))
            if prices and product_name:
                extra = _clean(cells[flavor_col]["text"]) if flavor_col in cells else ""
                notes = []
                if per_pack:
                    notes.append("Цена за пачку")
                if extra.lower() not in PLACEHOLDERS:
                    notes.append(cells[flavor_col]["text"].replace("▪️", "•").strip())
                image_url = next((c["img"] for c in cells.values() if c["img"]), None)
                product = ParsedProduct(
                    sheet=title, row=r, name=product_name, brand=brand, prices=prices,
                    description="\n".join(notes) or None,
                    category_path=[title] + ([sub] if sub else []),
                )
                if image_url:
                    product.image = images.get(image_url)
                    product.source_name = image_url  # для дозагрузки фото
                products.append(product)
                continue
            texts = [_clean(c["text"]) for c in cells.values() if _clean(c["text"])]
            if prices or len(texts) != 1:
                continue
            text = texts[0]
            if PER_PACK_RE.search(text):
                per_pack = True
                continue
            if INFO_RE.search(text) or len(text) > 60:
                continue
            if m := FROM_BRAND_RE.match(text):
                # «Жидкости от Brusko» — бренд в разделе листа
                sub, brand = None, m.group(1)
            elif _is_latin(text.replace("!", "")):
                # подгруппа-линейка («НИКОБУСТЕР»: все товары называются «Никобустер …») кончилась
                in_sub = [p for p in products if sub and p.category_path[-1] == sub]
                if in_sub and all(sub.split()[0].lower() in p.name.lower() for p in in_sub):
                    sub = None
                brand = text
            elif text.lower() not in title.lower():
                sub, brand = _group_name(text), None
        # одна подкатегория на весь лист — не нужна
        subs = {tuple(p.category_path) for p in products}
        if len(subs) == 1:
            for p in products:
                p.category_path = [title]
        if products:
            sheets.append(ParsedSheet(title=title, products=products))
    defaults["price_basis"] = "amount"
    return ParseResult(sheets=sheets, warnings=warnings, tenant_defaults=defaults)


async def load_gsheet(url: str, with_images: bool = True) -> ParseResult:
    """Скачать все листы таблицы через htmlview и сразу — картинки ячеек"""
    m = GSHEET_RE.search(url)
    if not m:
        raise ValueError("Это не ссылка на Google-таблицу")
    base = f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        index = (await client.get(f"{base}/htmlview")).text
        sheet_list = [(_unescape_js(n), gid) for n, gid in SHEET_LIST_RE.findall(index)]
        seen: set[str] = set()
        pages: list[tuple[str, str]] = []
        for name, gid in sheet_list:
            if gid in seen:
                continue
            seen.add(gid)
            page = (await client.get(f"{base}/htmlview/sheet", params={"headers": "true", "gid": gid})).text
            pages.append((name, page))
        images: dict[str, bytes] = {}
        if with_images:
            urls = {c["img"] for _, page in pages for row in parse_grid(page).values() for c in row.values() if c["img"]}
            semaphore = asyncio.Semaphore(6)

            async def fetch(src: str) -> None:
                async with semaphore:
                    for attempt in range(3):
                        try:
                            resp = await client.get(src.rsplit("=", 1)[0] + IMAGE_SIZE)
                            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                                images[src] = resp.content
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(1 + attempt)
                    log.warning("картинка не скачалась: %s", src[:80])

            await asyncio.gather(*(fetch(u) for u in urls))
    result = parse_sheets(pages, images)
    if with_images:
        missing = sum(1 for p in result.products if p.source_name and not p.image)
        if missing:
            result.warnings.append(f"не скачалось фото: {missing}")
    for p in result.products:
        p.source_name = None
    return result
