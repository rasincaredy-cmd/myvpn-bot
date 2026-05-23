from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from cachetools import TTLCache

from bot.texts import t


class ThrottleMiddleware(BaseMiddleware):
    """Простой rate-limit: один запрос в DEFAULT_RATE секунд на пользователя."""

    def __init__(self, rate: float = 0.7, maxsize: int = 10_000) -> None:
        self._cache: TTLCache[int, float] = TTLCache(maxsize=maxsize, ttl=rate)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            user_id = event.from_user.id

        if user_id is not None:
            if user_id in self._cache:
                if isinstance(event, CallbackQuery):
                    await event.answer(t.throttled, show_alert=False)
                return None
            self._cache[user_id] = 1.0

        return await handler(event, data)
