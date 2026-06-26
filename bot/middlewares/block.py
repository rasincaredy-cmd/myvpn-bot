"""Отклоняет запросы от заблокированных пользователей."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.db import repo


class BlockMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if session is None:
            return await handler(event, data)

        from_user = None
        if isinstance(event, (Message, CallbackQuery)):
            from_user = event.from_user

        if from_user:
            user = await repo.get_user_by_tg_id(session, from_user.id)
            if user and user.is_blocked:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔️ Доступ заблокирован.", show_alert=True)
                else:
                    await event.answer("⛔️ Доступ заблокирован.")
                return None

        return await handler(event, data)
