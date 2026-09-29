"""
Общая инфраструктура для тестов — как в taxi_db_service: файловая sqlite
вместо боевого Postgres через monkeypatch Database.init/session. Каждый
тест получает свою изолированную БД (файл в tmp_path).
"""
import asyncio
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# get_cached_config кэширует Config через lru_cache на процесс — всё
# выставляется ДО первого обращения. DEV_AUTH=true: большинство тестов ходят
# через X-Tg-User-Id; настоящая initData проверяется в test_auth.py.
for key, value in {
    "POSTGRES_HOST": "unused", "POSTGRES_PORT": "5432", "POSTGRES_USER": "unused",
    "POSTGRES_PASSWORD": "unused", "POSTGRES_DB": "unused",
}.items():
    os.environ.setdefault(key, value)
os.environ["DEV_AUTH"] = "true"
os.environ["WEBAPP_BASE_URL"] = "https://opt.example.com"
os.environ["SERVICE_API_KEY"] = "test-service-key"
os.environ["UPLOADS_DIR"] = tempfile.mkdtemp(prefix="opt_test_uploads_")

import app.database.db as db_module  # noqa: E402
import app.lifespan as lifespan_module  # noqa: E402


async def _no_worker():
    """Воркер доставки в тестах не нужен — очередь проверяем по таблице notifications"""
    return None


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def fake_init(self, url=None):
        self.engine = engine
        self.session_factory = factory

    def fake_session(self):
        return factory()

    monkeypatch.setattr(db_module.Database, "init", fake_init)
    monkeypatch.setattr(db_module.Database, "session", fake_session)
    monkeypatch.setattr(lifespan_module, "run_bot_notify_loop", _no_worker)
    yield factory
    asyncio.run(engine.dispose())


@pytest.fixture()
def client(session_factory):
    """TestClient поверх патченной БД — таблицы создаёт сам lifespan"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        yield c
