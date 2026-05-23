from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from bot.config import settings


class AdminFilter(Filter):
    """Пропускает только пользователей из ADMIN_IDS."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None:
            return False
        return user.id in settings.admin_ids
