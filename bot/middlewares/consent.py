"""Гейт согласия с условиями: не пускает дальше, пока юзер не принял оферту.

Раньше проверка стояла в трёх местах внутри /start, и это было дырой: /menu,
/help и любая инлайн-кнопка вели в бот мимо экрана условий. Middleware видит
КАЖДОЕ сообщение и КАЖДОЕ нажатие, поэтому обойти его нечем.

Пропускаем мимо гейта только то, без чего экран согласия не работает: сами
кнопки «Согласен»/«Не согласен» и ссылки на документы (они url-кнопки, апдейта
не создают).
"""
from __future__ import annotations

import re
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
# было бы невозможно. «Согласен» проверяем по началу: с 30.08.2026 на кнопке
# едет ещё и код пригласившего.
_ACCEPT = f"{CB_LEGAL}:accept"
_ALLOWED = {_ACCEPT, f"{CB_LEGAL}:decline"}

# Токен из реф-ссылки: латиница, цифры и подчёркивание. Всё остальное в
# callback_data не пускаем — её лимит 64 байта, а кириллица съедает по два.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def ref_token(event: TelegramObject) -> str | None:
    """Код пригласившего из `/start ref_код` — или None, если его там нет.

    Нужен здесь, а не в хендлере /start: у нового юзера гейт перехватывает
    самое первое сообщение, и хендлер этот код никогда не увидит. Живой случай
    29.08.2026 — люди с форумов дошли до бота, а «Приглашено» осталось нулём.
    """
    if not isinstance(event, Message):
        return None
    parts = (event.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0].split("@")[0] != "/start":
        return None
    arg = parts[1].strip()
    if not arg.startswith("ref_"):
        return None
    token = arg[4:]
    return token if _TOKEN_RE.match(token) else None


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

        if isinstance(event, CallbackQuery):
            payload = event.data or ""
            if payload in _ALLOWED or payload.startswith(f"{_ACCEPT}:"):
                return await handler(event, data)

        from_user = event.from_user
        if from_user is None:
            return await handler(event, data)

        user = await repo.get_user_by_tg_id(session, from_user.id)
        if not needs_consent(user):
            return await handler(event, data)

        # Показываем экран условий вместо запрошенного действия. У юзера,
        # которого ещё нет в БД, get_or_create_user отработает в /start уже
        # после согласия — здесь запись не создаём. Код пригласившего забираем
        # из перехваченного сообщения и вешаем на кнопку «Согласен»: другого
        # места, где он доживёт до создания юзера, нет.
        kb = consent_kb(ref_token(event))
        if isinstance(event, CallbackQuery):
            await event.answer()
            await event.message.answer(t.consent_intro, reply_markup=kb)
        else:
            await event.answer(t.consent_intro, reply_markup=kb)
        return None
