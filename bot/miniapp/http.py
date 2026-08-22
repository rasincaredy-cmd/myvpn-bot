"""Общая машинерия HTTP-слоя мини-приложения: вход, ошибки, частота запросов.

Каждый обработчик получает уже проверенного пользователя и открытую сессию БД —
как хендлеры бота получают их от middleware. Смысл тот же: правило безопасности,
которое нужно повторять в каждом обработчике, однажды забудут.
"""
from __future__ import annotations

import functools
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web
from cachetools import TTLCache
from loguru import logger

from bot.db import repo
from bot.db.base import SessionMaker
from bot.middlewares.consent import needs_consent
from bot.miniapp import auth


class ApiError(Exception):
    """Ошибка, которую можно показать человеку. Код — для логики страницы."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Ctx:
    """Что известно об авторе запроса."""

    session: Any          # AsyncSession
    user: Any             # bot.db.models.User
    tg: auth.WebAppUser


def json_response(payload: dict, *, status: int = 200) -> web.Response:
    # ensure_ascii=False: русский текст в ответах не должен превращаться в
    # \uXXXX — ответы читает и человек, когда разбирает жалобу по логам.
    return web.Response(
        status=status,
        text=json.dumps(payload, ensure_ascii=False, default=str),
        content_type="application/json",
        charset="utf-8",
    )


# Частота запросов. Чтения дешёвые, поэтому лимит стоит только на действиях:
# создание устройства и резервного подключения ходят по SSH на сервер, и
# десяток нажатий подряд превращается в десяток параллельных SSH-сессий.
_ACTION_INTERVAL = 2.0
_recent: TTLCache = TTLCache(maxsize=10_000, ttl=_ACTION_INTERVAL)


def _rate_limit(tg_id: int) -> None:
    if tg_id in _recent:
        raise ApiError(
            "too_fast", "Слишком часто — подожди пару секунд.", status=429
        )
    _recent[tg_id] = time.monotonic()


def _init_data(request: web.Request) -> str:
    raw = request.headers.get("Authorization", "")
    if raw.startswith("tma "):
        return raw[4:]
    return request.headers.get("X-Telegram-Init-Data", "")


def authorized(
    *, action: bool = False
) -> Callable:
    """Декоратор обработчика: проверяет подпись, находит юзера, открывает сессию.

    `action=True` — обработчик что-то меняет: добавляем ограничение частоты и
    коммитим сессию по успеху.
    """

    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def handler(request: web.Request) -> web.Response:
            try:
                tg = auth.check(_init_data(request))
            except auth.AuthError as exc:
                # Логируем без самих данных: там подпись и профиль человека.
                logger.warning("Мини-приложение: вход отклонён ({})", exc)
                return json_response(
                    {"ok": False, "error": "auth", "message": "Открой приложение заново из бота."},
                    status=401,
                )
            if action:
                try:
                    _rate_limit(tg.tg_id)
                except ApiError as exc:
                    return json_response(
                        {"ok": False, "error": exc.code, "message": exc.message},
                        status=exc.status,
                    )

            async with SessionMaker() as session:
                try:
                    # Согласие проверяем ДО создания записи — так же, как гейт
                    # в боте: иначе человек, открывший приложение раньше
                    # первого /start, молча получал бы пробный период, который
                    # начал бы тикать, пока он читает условия.
                    user = await repo.get_user_by_tg_id(session, tg.tg_id)
                    if needs_consent(user):
                        raise ApiError(
                            "consent",
                            "Сначала прими условия — они на первом экране бота.",
                            status=403,
                        )
                    user = await repo.get_or_create_user(
                        session, tg_id=tg.tg_id, username=tg.username,
                        full_name=tg.full_name,
                    )
                    if user.is_blocked:
                        raise ApiError(
                            "blocked", "Доступ закрыт. Напиши в поддержку.",
                            status=403,
                        )
                    result = await fn(request, Ctx(session=session, user=user, tg=tg))
                    await session.commit()
                except ApiError as exc:
                    await session.rollback()
                    return json_response(
                        {"ok": False, "error": exc.code, "message": exc.message},
                        status=exc.status,
                    )
                except Exception:
                    await session.rollback()
                    logger.exception("Мини-приложение: сбой в {}", fn.__name__)
                    return json_response(
                        {
                            "ok": False,
                            "error": "internal",
                            "message": "Что-то пошло не так. Попробуй ещё раз.",
                        },
                        status=500,
                    )
            if isinstance(result, web.StreamResponse):
                return result
            payload = {"ok": True}
            payload.update(result or {})
            return json_response(payload)

        return handler

    return wrap


async def body(request: web.Request) -> dict:
    """Тело запроса как словарь. Кривое тело — понятная ошибка, а не 500."""
    try:
        data = await request.json()
    except Exception:
        raise ApiError("bad_body", "Некорректный запрос.") from None
    if not isinstance(data, dict):
        raise ApiError("bad_body", "Некорректный запрос.")
    return data


def int_arg(data: dict, name: str, *, lo: int, hi: int) -> int:
    """Целое из тела запроса с проверкой границ.

    Границы проверяем ВСЕГДА и здесь: тело запроса приходит со страницы, а её
    javascript можно переписать в отладчике браузера — ровно как подделывается
    callback_data у кнопок бота.
    """
    raw = data.get(name)
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ApiError("bad_body", "Некорректный запрос.")
    try:
        value = int(raw)
    except ValueError:
        raise ApiError("bad_body", "Некорректный запрос.") from None
    if not (lo <= value <= hi):
        raise ApiError("bad_body", "Некорректное значение.")
    return value
