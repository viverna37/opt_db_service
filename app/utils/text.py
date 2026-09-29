import re

_SPACES = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    """Нормализованное имя для поиска и сопоставления: lower, ё→е, схлопнутые пробелы"""
    return _SPACES.sub(" ", value.lower().replace("ё", "е")).strip()
