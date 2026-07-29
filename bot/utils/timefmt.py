"""Время: приведение к UTC и даты для юзерских текстов в МСК.

БД (SQLite) отдаёт datetime без таймзоны — трактуем как UTC (`as_utc`). Для
юзеров из РФ показываем МСК: фиксированный UTC+3, перевода часов в РФ нет.
Админские экраны остаются в UTC — там время согласовано с валидатором ввода дат.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_MSK = timezone(timedelta(hours=3))


def as_utc(dt: datetime) -> datetime:
    """SQLite отдаёт datetime без таймзоны — считаем такие значения UTC.

    Без этого арифметика `expires_at - now` (aware) падает с TypeError.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def fmt_msk(dt: datetime, with_time: bool = True) -> str:
    local = as_utc(dt).astimezone(_MSK)
    fmt = "%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y"
    return local.strftime(fmt)
