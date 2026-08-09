"""Текст уведомления об автопродлении.

Главное, что здесь проверяется, — юзеру ЯВНО сказано, если подписку продлили
на срок короче того, что он покупал, названа нехватка в рублях и предложено
пополнение. Молчаливое урезание срока = жалоба в поддержку.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.handlers.balance import autopay_forecast_line, autopay_notice
from bot.services import billing


def _res(months: int, price: int, wanted: int, wanted_price: int, missing: int):
    return billing.ChargeResult(
        ok=True,
        price_kopeks=price,
        new_expires_at=datetime.now(timezone.utc) + timedelta(days=30 * months),
        months=months,
        wanted_months=wanted,
        wanted_price_kopeks=wanted_price,
        missing_kopeks=missing,
    )


class _User:
    balance_kopeks = 50_00


def _user(**kw):
    """Юзер-заглушка для чистых функций (сессия им не нужна)."""
    defaults = dict(
        autopay=True, sub_expires_at=datetime.now(timezone.utc),
        sub_max_devices=1, sub_max_bypass=1, sub_term_months=12,
        balance_kopeks=1080_00,
    )
    return type("U", (), {**defaults, **kw})()


class TestPlanAutopay:
    """billing.plan_autopay — что именно спишется, БЕЗ списания. Нужна и для
    самого автопродления, и для предупреждения «скоро истечёт»."""

    def test_full_term_when_money_enough(self) -> None:
        assert billing.plan_autopay(_user()) == (12, 1080_00, 12, 1080_00)

    def test_largest_affordable_term(self) -> None:
        assert billing.plan_autopay(_user(balance_kopeks=700_00)) == (6, 610_00, 12, 1080_00)

    def test_none_when_not_even_month(self) -> None:
        assert billing.plan_autopay(_user(balance_kopeks=10_00)) is None

    def test_none_when_autopay_off(self) -> None:
        assert billing.plan_autopay(_user(autopay=False)) is None

    def test_none_when_perpetual(self) -> None:
        assert billing.plan_autopay(_user(sub_expires_at=None)) is None

    def test_none_when_empty_tariff(self) -> None:
        assert billing.plan_autopay(_user(sub_max_devices=0, sub_max_bypass=0)) is None

    def test_unknown_term_plans_month(self) -> None:
        assert billing.plan_autopay(_user(sub_term_months=None)) == (1, 120_00, 1, 120_00)


class TestExpiryForecast:
    """Строка в предупреждении «подписка скоро истечёт»: сколько и за что
    спишется, хватает ли денег."""

    def test_names_sum_and_term(self) -> None:
        line = autopay_forecast_line(_user())
        assert "1080 ₽" in line and "год" in line

    def test_warns_about_shorter_term_and_shortfall(self) -> None:
        line = autopay_forecast_line(_user(balance_kopeks=700_00))
        assert "полгода" in line and "1080 ₽" in line and "380 ₽" in line

    def test_warns_when_money_not_enough_at_all(self) -> None:
        line = autopay_forecast_line(_user(balance_kopeks=10_00))
        assert "120 ₽" in line and "пауз" in line.lower()

    def test_silent_when_autopay_off(self) -> None:
        assert autopay_forecast_line(_user(autopay=False)) is None


class TestFullTerm:
    def test_names_actual_term_not_always_month(self) -> None:
        text, topup = autopay_notice(_User(), _res(12, 1080_00, 12, 1080_00, 0))
        assert "год" in text and "1080 ₽" in text
        assert "месяц" not in text          # раньше писало «на месяц» всегда
        assert topup is False

    def test_monthly_renewal_still_says_month(self) -> None:
        text, topup = autopay_notice(_User(), _res(1, 120_00, 1, 120_00, 0))
        assert "месяц" in text and "120 ₽" in text
        assert topup is False


class TestShortenedTerm:
    def test_says_term_is_shorter_than_bought(self) -> None:
        text, topup = autopay_notice(_User(), _res(6, 610_00, 12, 1080_00, 380_00))
        # Юзер должен понять три вещи: срок НЕ тот, какой был, какой стал,
        # и сколько денег не хватило.
        assert "меньш" in text.lower()
        assert "полгода" in text        # на сколько продлили на самом деле
        assert "1080 ₽" in text         # сколько стоил бы полный срок
        assert "380 ₽" in text          # сколько не хватило
        assert topup is True

    def test_offers_topup_button(self) -> None:
        _, topup = autopay_notice(_User(), _res(1, 120_00, 12, 1080_00, 960_00))
        assert topup is True
