"""Файл конфига"""
from dataclasses import dataclass
from functools import lru_cache

from environs import Env


@dataclass
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def url(self) -> str:
        """Для удобства прописан метод, чтобы ручками не собирать url"""
        return (
            f"postgresql+asyncpg://"
            f"{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


@dataclass
class AuthConfig:
    """
    Токены ботов лежат не здесь, а в БД у каждого тенанта (Tenant.bot_token) —
    у каждого оптовика свой бот, initData проверяется ключом его бота.

    dev_auth — режим для работы в браузере без Telegram: вместо X-Init-Data
    принимается X-Tg-User-Id без всякой проверки. На проде ОБЯЗАН быть
    выключен, иначе любой может представиться кем угодно.
    """
    init_data_max_age_sec: int
    dev_auth: bool


@dataclass
class TelegramConfig:
    """
    api_base_url — у прода прямой доступ к api.telegram.org закрыт (как и в
    такси), поэтому адрес Bot API настраивается: можно указать
    прокси-воркер. webapp_base_url — домен мини-аппа, из него строятся
    кнопки "Открыть" под уведомлениями (/t/{slug}/...).
    """
    api_base_url: str
    webapp_base_url: str


@dataclass
class PlatformConfig:
    """
    Владелец платформы (тот, кто заводит оптовиков). admin_ids — Telegram id
    суперадминов: задаются только здесь, через API их не назначить.
    bot_token — необязательный «бот платформы»: из его мини-аппа открывается
    /platform. Без него раздел открывается из бота любого оптовика.
    """
    admin_ids: frozenset[int]
    bot_token: str


@dataclass
class Config:
    """Просто сбор в кучу"""
    db: DbConfig
    auth: AuthConfig
    telegram: TelegramConfig
    platform: PlatformConfig
    uploads_dir: str
    # Ключ для server-to-server вызовов бот-сервиса (/v1/service/*). Пусто — ручки выключены.
    service_api_key: str


def load_config(path: str | None = None) -> Config:
    env = Env()
    env.read_env(path)
    return Config(
        db=DbConfig(
            host=env.str("POSTGRES_HOST"),
            port=env.int("POSTGRES_PORT"),
            user=env.str("POSTGRES_USER"),
            password=env.str("POSTGRES_PASSWORD"),
            database=env.str("POSTGRES_DB"),
        ),
        auth=AuthConfig(
            init_data_max_age_sec=env.int("INIT_DATA_MAX_AGE_SEC", 86400),
            dev_auth=env.bool("DEV_AUTH", False),
        ),
        telegram=TelegramConfig(
            api_base_url=env.str("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/"),
            webapp_base_url=env.str("WEBAPP_BASE_URL", "").rstrip("/"),
        ),
        platform=PlatformConfig(
            admin_ids=frozenset(int(x) for x in env.list("PLATFORM_ADMIN_IDS", []) if str(x).strip()),
            bot_token=env.str("PLATFORM_BOT_TOKEN", ""),
        ),
        uploads_dir=env.str("UPLOADS_DIR", "/app/uploads"),
        service_api_key=env.str("SERVICE_API_KEY", ""),
    )


@lru_cache
def get_cached_config() -> Config:
    """Конфиг читается из .env один раз за жизнь процесса, а не на каждый запрос"""
    return load_config()
