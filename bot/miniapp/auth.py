"""Проверка подлинности мини-приложения: кто именно открыл страницу.

Мини-приложение — это обычная веб-страница, и «кто ты» она сообщает сама.
Верить ей на слово нельзя: подделать запрос к API может кто угодно. Поэтому
Telegram при открытии страницы кладёт в неё строку `initData`, подписанную
ключом, который выводится из токена бота. Подпись проверяется здесь, и только
после этого запрос получает пользователя.

Ключ подписи — HMAC(«WebAppData», токен_бота), а не сам токен: так Telegram
отделяет подпись мини-приложения от всего остального, что подписывается тем же
токеном.

Отдельно проверяем возраст подписи. Без этого украденная один раз строка
работала бы вечно: она не привязана ни к сессии, ни к устройству.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from bot.config import settings


class AuthError(Exception):
    """Подпись не сошлась, протухла или в данных нет пользователя."""


@dataclass(frozen=True)
class WebAppUser:
    """Кто открыл мини-приложение. Ровно то, что подписал Telegram."""

    tg_id: int
    username: str | None
    full_name: str


# Сколько живёт одна подпись. Сутки — компромисс: страницу держат открытой
# часами (Telegram не перезагружает её при сворачивании), а вечная подпись
# превращает случайно утёкший лог в постоянный ключ от аккаунта.
MAX_AGE_SECONDS = 24 * 60 * 60


def _secret_key(token: str) -> bytes:
    return hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()


def _check_string(pairs: list[tuple[str, str]], *, drop: set[str]) -> str:
    return "\n".join(
        f"{k}={v}" for k, v in sorted(pairs, key=lambda p: p[0]) if k not in drop
    )


def check(
    init_data: str,
    *,
    token: str | None = None,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> WebAppUser:
    """Проверяет подпись `initData` и возвращает пользователя. Иначе AuthError.

    Считаем подпись ДВАЖДЫ: сначала по всем полям кроме `hash`, потом — кроме
    `hash` и `signature`. Причина: `signature` (отдельная подпись Telegram для
    сторонних сервисов) появилась позже самого механизма, и в документации она
    исключается из строки проверки только в разделе про сторонние сервисы.
    Один вариант из двух совпадёт на любом клиенте. Безопасность от этого не
    страдает: `signature` мы не используем вовсе, подделать ею нечего.
    """
    token = token or settings.bot_token
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    got = data.get("hash", "")
    if not got:
        raise AuthError("нет подписи")

    key = _secret_key(token)
    for drop in ({"hash"}, {"hash", "signature"}):
        want = hmac.new(
            key, _check_string(pairs, drop=drop).encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(want, got):
            break
    else:
        raise AuthError("подпись не сошлась")

    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError:
        raise AuthError("нет времени подписи") from None
    age = (now if now is not None else time.time()) - auth_date
    if max_age and age > max_age:
        raise AuthError("подпись просрочена")

    try:
        raw_user = json.loads(data.get("user", ""))
        tg_id = int(raw_user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("в данных нет пользователя") from None

    full_name = " ".join(
        part for part in (raw_user.get("first_name"), raw_user.get("last_name")) if part
    )
    return WebAppUser(
        tg_id=tg_id,
        username=raw_user.get("username") or None,
        full_name=full_name or (raw_user.get("username") or str(tg_id)),
    )
