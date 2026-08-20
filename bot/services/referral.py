"""Именная реферальная ссылка (Блок «Рефка», 20.08.2026).

Была `t.me/bot?start=ref_7` — голый номер строки в базе. Влад распространяет
ссылку на форумах, и номер там читается как мусор, а не как имя. Стало
`?start=ref_vlad`.

Ссылки по номеру обязаны работать вечно: они уже разосланы, и сломать их
означало бы обнулить чужую работу по продвижению. Поэтому `resolve` понимает
оба вида.
"""
from __future__ import annotations

import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User

# Telegram в start-параметре пропускает только латиницу, цифры, `_` и `-`.
# Дефис исключаем сами: он путается с переносом строки при копировании ссылки
# из поста на форуме. Первый символ — буква, иначе код читается как номер, от
# которого мы и уходили.
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# Слова, которые нельзя занимать: ссылка «?start=ref_start» читается как баг
# сервиса, а «ref_support» — как официальный канал.
_RESERVED = frozenset({
    "admin", "administrator", "support", "help", "start", "menu", "bot",
    "moschata", "vpn", "ref", "referral", "official", "root", "null", "none",
})

_MAX_TRIES = 20


def normalize(raw: str | None) -> str | None:
    """Приводит введённое к каноническому виду или отказывает.

    Регистр не сохраняем: ссылку с форума перепишут руками как угодно, а
    найтись она обязана. «@» срезаем — люди копируют ник вместе с ним.
    """
    if not raw:
        return None
    code = raw.strip().lstrip("@").lower()
    if code in _RESERVED or not _CODE_RE.match(code):
        return None
    return code


async def _is_free(session: AsyncSession, code: str, *, owner_id: int | None = None) -> bool:
    """Свободен ли код. `owner_id` — чей код не считать занятым (свой же)."""
    stmt = select(User.id).where(func.lower(User.ref_code) == code)
    if owner_id is not None:
        stmt = stmt.where(User.id != owner_id)
    return (await session.execute(stmt)).first() is None


def _from_username(user: User) -> str | None:
    """Ник Telegram как основа кода — если он вообще годится."""
    return normalize(user.username)


def _random_code() -> str:
    """Запасной код для тех, у кого ника нет или он не годится.

    Читаемый, а не хеш: его будут диктовать голосом и переписывать руками.
    """
    return "vpn" + secrets.token_hex(3)


async def ensure_code(session: AsyncSession, user: User) -> str:
    """Код юзера; при первом обращении выдаёт его.

    Выданный код НЕ меняется сам, даже если юзер сменил ник в Telegram: ссылка
    к тому моменту уже может лежать на форуме, и молча её протухать нельзя.
    """
    if user.ref_code:
        return user.ref_code

    base = _from_username(user)
    candidates = [base] if base else []
    if base:
        # Ник занят другим — не отбираем, а даём соседний.
        candidates += [f"{base}{i}" for i in range(2, _MAX_TRIES)]
    for candidate in candidates:
        if candidate and await _is_free(session, candidate, owner_id=user.id):
            user.ref_code = candidate
            await session.flush()
            return candidate

    while True:  # случайные коды кончиться не могут
        candidate = _random_code()
        if await _is_free(session, candidate, owner_id=user.id):
            user.ref_code = candidate
            await session.flush()
            return candidate


async def set_code(session: AsyncSession, user: User, raw: str) -> str:
    """Юзер выбирает код сам. Возвращает «ok» / «invalid» / «taken».

    Строкой, а не исключением: у вызывающего на каждый исход своё сообщение
    юзеру, и три ветки читаются лучше, чем три except.
    """
    code = normalize(raw)
    if code is None:
        return "invalid"
    if not await _is_free(session, code, owner_id=user.id):
        return "taken"
    user.ref_code = code
    await session.flush()
    return "ok"


async def resolve(session: AsyncSession, token: str) -> User | None:
    """Находит пригласившего по коду ИЛИ по старому числовому id.

    Порядок важен: сперва код. Числовой id остаётся запасным путём ради уже
    разосланных ссылок, а не основным.
    """
    token = (token or "").strip().lstrip("@")
    if not token:
        return None
    by_code = (
        await session.execute(
            select(User).where(func.lower(User.ref_code) == token.lower())
        )
    ).scalar_one_or_none()
    if by_code is not None:
        return by_code
    if token.isdigit():
        return (
            await session.execute(select(User).where(User.id == int(token)))
        ).scalar_one_or_none()
    return None
