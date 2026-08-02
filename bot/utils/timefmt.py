"""Время: в базе UTC, у людей — московское.

БД (SQLite) отдаёт datetime без таймзоны — трактуем как UTC (`as_utc`). Всё,
что видит человек (и юзер, и админ), показываем в МСК: фиксированный UTC+3,
перевода часов в РФ нет. Ввод дат админом (`validators.parse_expiry`) тоже
читается как МСК — иначе «до 23:00» на экране админа и у юзера разъезжались
бы на три часа.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def as_utc(dt: datetime) -> datetime:
    """SQLite отдаёт datetime без таймзоны — считаем такие значения UTC.

    Без этого арифметика `expires_at - now` (aware) падает с TypeError.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def msk_to_utc(naive_msk: datetime) -> datetime:
    """Дата/время, введённые человеком по Москве → UTC для записи в базу."""
    return naive_msk.replace(tzinfo=MSK).astimezone(timezone.utc)


def fmt_msk(dt: datetime, with_time: bool = True, *, fmt: str | None = None) -> str:
    """Дата/время в МСК. fmt — явный формат для мест, где нужен свой (например,
    список операций: там год только мешает)."""
    local = as_utc(dt).astimezone(MSK)
    return local.strftime(fmt or ("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y"))


def fmt_ago(moment: datetime | None) -> str:
    """«12 мин назад» для карточек. None — события не было ни разу.

    Шкала та же, что у хендшейка пира в метриках сервера (сек/мин/ч/д), чтобы
    админ читал обе карточки одинаково.
    """
    if moment is None:
        return "не видели ни разу"
    delta = int((datetime.now(timezone.utc) - as_utc(moment)).total_seconds())
    if delta < 60:
        return f"{max(delta, 0)} сек назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} д назад"
