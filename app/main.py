from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging import setup_logging
from app.lifespan import lifespan
from app.routers.admin_clients_router import router as admin_clients_router
from app.routers.admin_orders_router import router as admin_orders_router
from app.routers.admin_products_router import router as admin_products_router
from app.routers.admin_settings_router import router as admin_settings_router
from app.routers.cart_router import router as cart_router
from app.routers.catalog_router import router as catalog_router
from app.routers.health import router as health_router
from app.routers.me_router import router as me_router
from app.routers.order_router import router as order_router
from app.routers.public_router import router as public_router
from app.routers.service_router import router as service_router


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(
        title="OptCatalog Database API",
        version="1.0.0",
        lifespan=lifespan,
    )
    # Мини-апп открывается на другом origin (в Telegram) — разрешаем всем,
    # сузить до конкретного origin фронта, когда он определится.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(public_router)
    app.include_router(me_router)
    app.include_router(catalog_router)
    app.include_router(cart_router)
    app.include_router(order_router)
    app.include_router(admin_orders_router)
    app.include_router(admin_products_router)
    app.include_router(admin_clients_router)
    app.include_router(admin_settings_router)
    app.include_router(service_router)
    return app


app = create_app()
