"""
Идемпотентные правки схемы для уже существующей базы — как в такси, миграций
Alembic нет: create_all создаёт новые таблицы, а изменения старых таблиц
(новые колонки и т.п.) догоняются здесь при каждом старте. Каждая команда
должна быть безопасна при повторном выполнении. Только Postgres (тесты на
sqlite создают схему с нуля).
"""
from sqlalchemy import text

STATEMENTS = [
    # 2026-09: уровни цен по сумме заявки (Tenant.price_basis, PriceTier.min_amount)
    """DO $$ BEGIN CREATE TYPE price_basis AS ENUM ('qty', 'amount');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS price_basis price_basis NOT NULL DEFAULT 'qty'",
    "ALTER TABLE price_tiers ADD COLUMN IF NOT EXISTS min_amount INTEGER",
    "ALTER TABLE price_tiers ALTER COLUMN min_qty DROP NOT NULL",
    """DO $$ BEGIN ALTER TABLE price_tiers ADD CONSTRAINT uq_price_tiers_tenant_min_amount UNIQUE (tenant_id, min_amount);
       EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$""",
]


async def apply(conn) -> None:
    for statement in STATEMENTS:
        await conn.execute(text(statement))
