from zoneinfo import ZoneInfo

CURRENCY_SIGNS = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "BYN": "Br", "UZS": "сум"}


def format_money(kopecks: int, currency: str = "RUB") -> str:
    """450000 -> '4 500 ₽', 450050 -> '4 500,50 ₽'"""
    rubles, rest = divmod(abs(kopecks), 100)
    whole = f"{rubles:,}".replace(",", " ")
    sign = "-" if kopecks < 0 else ""
    text = f"{sign}{whole},{rest:02d}" if rest else f"{sign}{whole}"
    return f"{text} {CURRENCY_SIGNS.get(currency, currency)}"


def safe_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")
