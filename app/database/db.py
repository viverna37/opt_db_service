import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

Base = declarative_base()

logger = logging.getLogger(__name__)


class Database:
    """Класс для работы с базой данных"""

    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def init(self, database_url: str):
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    def session(self) -> AsyncSession:
        """Одна сессия, один апдейт"""
        return self.session_factory()

    async def create_tables(self):
        """Создать все таблицы. pg_trgm нужен до create_all — на нём trigram-индекс поиска по названию товара"""
        try:
            async with self.engine.begin() as conn:
                logger.info("Подключение к БД успешно")
                if conn.dialect.name == "postgresql":
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                await conn.run_sync(Base.metadata.create_all)
                logger.info("Таблицы созданы/проверены")
        except Exception:
            logger.exception("Ошибка при создании таблиц")
            raise

    async def close(self):
        """Закрыть подключение"""
        await self.engine.dispose()


# Глобальный экземпляр БД
db = Database()
