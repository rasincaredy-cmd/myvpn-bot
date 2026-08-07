"""Гейт согласия с условиями: не пускает дальше, пока юзер не принял оферту.

Раньше проверка стояла в трёх местах внутри /start, и это было дырой: /menu,
/help и любая инлайн-кнопка вели в бот мимо экрана условий. Middleware видит
КАЖДОЕ сообщение и КАЖДОЕ нажатие, поэтому обойти его нечем.

Пропускаем мимо гейта только то, без чего экран согласия не работает: сами
кнопки «Согласен»/«Не согласен» и ссылки на документы (они url-кнопки, апдейта
не создают).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.db import repo
from bot.keyboards.inline import CB_LEGAL, consent_kb
from bot.texts import t

# Экран согласия показываем только тем, кто зарегистрировался начиная с этой
# даты. Действующих юзеров не дёргаем: они пришли до появления требования.
CONSENT_SINCE = datetime(2026, 8, 5, tzinfo=timezone.utc)

# Коллбэки самого экрана согласия — их гейт пропускает, иначе принять условия
# было бы невозможно.
_ALLOWED_PREFIX = f"{CB_LEGAL}:"
_ALLOWED = {f"{CB_LEGAL}:accept", f"{CB_LEGAL}:decline"}


def needs_consent(user) -> bool:
    """Нужно ли показать экран условий.

    user=None — юзера ещё нет в БД (написал /menu вместо /start): это заведомо
    новый, гейт нужен.
    """
    from bot.config import settings

    # Без ссылок на документы гейт выключен: юзер застрял бы на экране, где
    # нечего прочитать.
    if not (settings.legal_terms_url or settings.legal_privacy_url):
        return False
    if user is None:
        return True
    # Админ — это владелец сервиса; гейт на нём заблокировал бы управление.
    if getattr(user, "is_admin", False):
        return False
    if user.terms_accepted_at is not None:
        return False
    created = user.created_at
    if created is None:
        return False
    # SQLite отдаёт naive datetime — сравнивать с aware нельзя.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= CONSENT_SINCE


class ConsentMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if session is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and (event.data or "") in _ALLOWED:
            return await handler(event, data)

        from_user = event.from_user
        if from_user is None:
            return await handler(event, data)

        user = await repo.get_user_by_tg_id(session, from_user.id)
        if not needs_consent(user):
            return await handler(event, data)

        # Показываем экран условий вместо запрошенного действия. У юзера,
        # которого ещё нет в БД, get_or_create_user отработает в /start уже
        # после согласия — здесь запись не создаём.
        if isinstance(event, CallbackQuery):
            await event.answer()
            await event.message.answer(t.consent_intro, reply_markup=consent_kb())
        else:
            await event.answer(t.consent_intro, reply_markup=consent_kb())
        return None
