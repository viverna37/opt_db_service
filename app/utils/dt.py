from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(value: datetime) -> datetime:
    """Postgres+asyncpg всегда отдаёт timezone-aware datetime для TIMESTAMPTZ,
    но подстраховываемся на случай naive-значения (например, тестовая sqlite)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
