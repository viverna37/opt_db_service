from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TgUser
from app.utils.dt import utcnow

PROFILE_FIELDS = ("first_name", "last_name", "username", "photo_url", "language_code")


class TgUserRepository:
    """Глобальные Telegram-аккаунты"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, tg_user_id: int) -> TgUser | None:
        return await self.session.get(TgUser, tg_user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> TgUser | None:
        result = await self.session.execute(select(TgUser).where(TgUser.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def upsert(self, telegram_id: int, profile: dict) -> TgUser:
        """
        Регистрации нет — профиль берётся из initData при каждом входе.
        В отличие от такси (где имя вводил сам человек), здесь имя/username/
        фото — единственный источник данных о клиенте, поэтому обновляем их
        при каждом входе, если что-то поменялось.
        """
        values = {key: profile.get(key) for key in PROFILE_FIELDS}
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = TgUser(telegram_id=telegram_id, **values)
            self.session.add(user)
            await self.session.flush()
            return user
        changed = False
        for key, value in values.items():
            if value is not None and getattr(user, key) != value:
                setattr(user, key, value)
                changed = True
        if changed:
            user.updated_at = utcnow()
            await self.session.flush()
        return user
