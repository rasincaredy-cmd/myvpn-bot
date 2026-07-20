"""Даты для юзерских текстов: московское время вместо UTC.

БД (SQLite) отдаёт datetime без таймзоны — трактуем как UTC (см. _as_utc в
scheduler). Для юзеров из РФ показываем МСК: фиксированный UTC+3, перевода
часов в РФ нет. Админские экраны остаются в UTC — там время согласовано с
валидатором ввода дат.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_MSK = timezone(timedelta(hours=3))


def fmt_msk(dt: datetime, with_time: bool = True) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_MSK)
    fmt = "%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y"
    return local.strftime(fmt)
