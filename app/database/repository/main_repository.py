from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repository.cart_repository import CartRepository
from app.database.repository.catalog_repository import (
    AttributeRepository,
    BrandRepository,
    CategoryRepository,
    PriceTierRepository,
)
from app.database.repository.order_repository import OrderRepository
from app.database.repository.product_repository import (
    PhotoRepository,
    PriceRepository,
    ProductRepository,
    VariantRepository,
)
from app.database.repository.service_repository import AuditRepository, NotificationRepository
from app.database.repository.tenant_repository import TenantRepository
from app.database.repository.tenant_user_repository import TenantUserRepository
from app.database.repository.tg_user_repository import TgUserRepository


class Repository:
    """
    Просто удобный класс, чтобы не делать 100 импортов. Собрал все в кучу.

    В отличие от такси, методы репозиториев не коммитят сами (только flush):
    отправка заявки, замена цен и т.п. трогают несколько таблиц и должны
    пройти одной транзакцией. Коммит — один раз в конце операции (commit()).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

        self.tenant = TenantRepository(session)
        self.tg_user = TgUserRepository(session)
        self.tenant_user = TenantUserRepository(session)
        self.category = CategoryRepository(session)
        self.brand = BrandRepository(session)
        self.attribute = AttributeRepository(session)
        self.price_tier = PriceTierRepository(session)
        self.product = ProductRepository(session)
        self.variant = VariantRepository(session)
        self.price = PriceRepository(session)
        self.photo = PhotoRepository(session)
        self.cart = CartRepository(session)
        self.order = OrderRepository(session)
        self.notification = NotificationRepository(session)
        self.audit = AuditRepository(session)

    async def commit(self) -> None:
        await self.session.commit()
