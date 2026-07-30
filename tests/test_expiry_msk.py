"""Ввод срока подписки админом — в МОСКОВСКОМ времени.

Раньше дата трактовалась как UTC, а юзеру срок показывался в МСК: админ
вводил «31.12.2026 23:00», юзер видел «01.01.2027 02:00». Теперь и ввод,
и показ — МСК; в базе по-прежнему UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.utils.timefmt import MSK, fmt_msk
from bot.utils.validators import parse_expiry


class TestParseExpiryMsk:
    def test_date_without_time_is_end_of_msk_day(self) -> None:
        got = parse_expiry("31.12.2026")
        # 23:59:59 по Москве = 20:59:59 UTC того же дня
        assert got == datetime(2026, 12, 31, 20, 59, 59, tzinfo=timezone.utc)
        assert fmt_msk(got) == "31.12.2026 23:59"

    def test_date_with_time_is_msk(self) -> None:
        got = parse_expiry("31.12.2026 09:30")
        assert got == datetime(2026, 12, 31, 6, 30, tzinfo=timezone.utc)
        assert fmt_msk(got) == "31.12.2026 09:30"

    def test_period_with_time_sets_msk_hour(self) -> None:
        got = parse_expiry("30д 18:00")
        assert got.astimezone(MSK).hour == 18
        left = got - datetime.now(timezone.utc)
        assert timedelta(days=29) < left < timedelta(days=31)

    def test_period_without_time_keeps_current_moment(self) -> None:
        got = parse_expiry("7д")
        left = got - datetime.now(timezone.utc)
        assert timedelta(days=6, hours=23) < left < timedelta(days=7)

    def test_dash_clears(self) -> None:
        assert parse_expiry("-") is None

    def test_garbage_is_invalid(self) -> None:
        assert parse_expiry("завтра") == "invalid"
        assert parse_expiry("31.13.2026") == "invalid"
        assert parse_expiry("31.12.2026 25:00") == "invalid"
