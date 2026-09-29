import logging.config
import os


def setup_logging() -> None:
    """Настраивает логирование на весь процесс. Уровень берётся из LOG_LEVEL (по умолчанию INFO)."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
            },
            "root": {
                "handlers": ["console"],
                "level": level,
            },
            "loggers": {
                "uvicorn": {"level": level, "propagate": True},
                "uvicorn.access": {"level": level, "propagate": True},
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
            },
        }
    )
