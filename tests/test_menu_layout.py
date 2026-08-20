"""Строка статуса подписки в шапке главного меню.

Раскладка самих кнопок переехала в test_menu_adaptive.py вместе с адаптивным
меню (Блок «Облик», 20.08.2026): проверять «во всю ширину / парами» на функции,
которая теперь собирает три разных набора, стало бессмысленно.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def test_sub_status_line_active() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) + timedelta(days=5)
    )
    line = build_sub_status_line(user)
    assert "5" in line


def test_sub_status_line_expired() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert "не активна" in build_sub_status_line(user).lower()


def test_sub_status_line_rounds_down_to_full_days() -> None:
    """5 суток и 1 час — это «5 дней», а не 6.

    Влад поймал на живом юзере: подписка выдана 06.08, истекает 13.08, а бот
    8-го писал 6 дней. Округление вверх обещало день, которого нет.
    """
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    exp = datetime.now(timezone.utc) + timedelta(days=5, hours=1)
    assert "<b>5</b>" in build_sub_status_line(User(tg_id=1, sub_expires_at=exp))


def test_sub_status_line_last_day_is_not_zero() -> None:
    """Меньше суток — всё ещё «1 день»: сервис работает, «0» читалось бы как
    «уже отключено». Ради этого случая округление вверх и делалось."""
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    exp = datetime.now(timezone.utc) + timedelta(hours=12)
    assert "<b>1</b>" in build_sub_status_line(User(tg_id=1, sub_expires_at=exp))


def test_sub_status_line_perpetual() -> None:
    """NULL в sub_expires_at — БЕССРОЧНАЯ подписка, а не отсутствие её.

    Так заведено во всём коде (devices._sub_active, wdtt, billing), и у Влада
    в проде стоит именно NULL. Первая версия строки статуса читала NULL как
    «нет подписки» и показывала админу «не активна» при работающем VPN.
    """
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    line = build_sub_status_line(User(tg_id=1, sub_expires_at=None)).lower()
    assert "не активна" not in line, "бессрочная подписка показана как отсутствующая"
    assert "бессрочно" in line
