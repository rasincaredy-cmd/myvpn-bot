"""Блок «Тариф»: пересчёт остатка при смене тарифа.

Модель (согласована с Владом 20.08.2026): неиспользованное время не сгорает и
не возвращается деньгами — оно пересчитывается в время нового тарифа по
БАЗОВЫМ ценам (без скидки за срок), а купленный срок прибавляется сверху
целиком. Округление ВНИЗ, поэтому качели между тарифами только теряют время.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services import billing
from bot.services.pricing import DAYS_PER_MONTH, convert_remaining, monthly_price_kopeks

DAY = 86400


async def _make_user(session: AsyncSession, tg_id: int = 501, **kw):
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    for k, v in kw.items():
        setattr(user, k, v)
    await session.flush()
    return user


class TestConvertRemaining:
    def test_same_tariff_keeps_time(self) -> None:
        """Тариф не менялся — коэффициент 1. На этом стоит совместимость:
        обычное продление и автопродление обязаны вести себя как раньше."""
        assert convert_remaining(300 * DAY, 120_00, 120_00) == 300 * DAY

    def test_upgrade_shrinks_time(self) -> None:
        """365 дней на 90 ₽/мес → тариф 160 ₽/мес. 365 × 90 / 160 = 205.3 → 205."""
        got = convert_remaining(365 * DAY, 90_00, 160_00)
        assert got // DAY == 205

    def test_downgrade_stretches_time(self) -> None:
        """100 дней на 230 ₽/мес → 90 ₽/мес: 100 × 230 / 90 = 255.5 → 255."""
        got = convert_remaining(100 * DAY, 230_00, 90_00)
        assert got // DAY == 255

    def test_rounds_down(self) -> None:
        """Округление вниз, а не к ближайшему: лишней секунды юзеру не дарим."""
        assert convert_remaining(10, 100_00, 300_00) == 3  # 3.33 → 3

    def test_round_trip_never_gains(self) -> None:
        """Качели «повысил — понизил» не наращивают время НИ на одном тарифе.

        Это единственная защита от арбитража: если бы пересчёт округлял вверх,
        юзер дёргал бы тариф туда-сюда и печатал себе дни.
        """
        start = 365 * DAY
        for dev_a, byp_a in [(1, 0), (1, 1), (3, 2), (10, 10)]:
            for dev_b, byp_b in [(1, 0), (2, 1), (5, 5), (10, 0)]:
                a = monthly_price_kopeks(dev_a, byp_a)
                b = monthly_price_kopeks(dev_b, byp_b)
                there = convert_remaining(start, a, b)
                back = convert_remaining(there, b, a)
                assert back <= start, f"{dev_a}+{byp_a} ↔ {dev_b}+{byp_b} напечатал время"

    def test_zero_remaining_stays_zero(self) -> None:
        assert convert_remaining(0, 90_00, 500_00) == 0

    def test_negative_remaining_is_zero(self) -> None:
        """Истёкшая подписка — остаток ноль, а не отрицательное время."""
        assert convert_remaining(-5 * DAY, 90_00, 120_00) == 0


class TestPurchaseConvertsRemainder:
    """Дыра, найденная 20.08.2026: покупка перезаписывала лимиты на весь
    оставшийся срок, и месяц дорогого тарифа поднимал лимиты на весь
    прошлогодний остаток."""

    @pytest.mark.asyncio
    async def test_upgrade_purchase_does_not_gift_old_remainder(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session,
            balance_kopeks=100_000_00,
            sub_max_devices=1, sub_max_bypass=0,   # 90 ₽/мес
            sub_expires_at=now + timedelta(days=365),
            is_trial=False,
        )
        # Покупает месяц тарифа 10 устройств (90 + 9×40 = 450 ₽/мес).
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=10, max_bypass=0
        )
        assert res.ok
        left_days = (res.new_expires_at - now).days
        # Остаток 365 дней по 90 ₽ = 73 дня по 450 ₽, плюс купленные 30.
        assert left_days == pytest.approx(73 + DAYS_PER_MONTH, abs=1)
        assert left_days < 200, "старый остаток подарен по новому тарифу"

    @pytest.mark.asyncio
    async def test_same_tariff_purchase_unchanged(self, session: AsyncSession) -> None:
        """Обычное продление тем же тарифом — как раньше: срок просто
        прибавляется к остатку, ни дня не теряется."""
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=502,
            balance_kopeks=100_000_00,
            sub_max_devices=2, sub_max_bypass=1,
            sub_expires_at=now + timedelta(days=100),
            is_trial=False,
        )
        res = await billing.charge_and_extend(
            session, user, 3, max_devices=2, max_bypass=1
        )
        assert res.ok
        assert (res.new_expires_at - now).days == pytest.approx(
            100 + 3 * DAYS_PER_MONTH, abs=1
        )

    @pytest.mark.asyncio
    async def test_trial_days_are_never_converted(self, session: AsyncSession) -> None:
        """Триальные дни прибавляются как есть.

        Экран триала обещает дословно: «оплаченный срок прибавится к пробному,
        ни дня не сгорит». Пересчёт триала по дорогому тарифу съедал бы дни и
        делал это обещание враньём — а подарок в 7 дней не эксплойт.
        """
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=503,
            balance_kopeks=100_000_00,
            sub_max_devices=1, sub_max_bypass=1,
            sub_expires_at=now + timedelta(days=7),
            is_trial=True,
        )
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=10, max_bypass=10
        )
        assert res.ok
        assert (res.new_expires_at - now).days == pytest.approx(
            7 + DAYS_PER_MONTH, abs=1
        )

    @pytest.mark.asyncio
    async def test_expired_subscription_starts_from_now(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=504,
            balance_kopeks=100_000_00,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=now - timedelta(days=30),
            is_trial=False,
        )
        res = await billing.charge_and_extend(session, user, 1, max_devices=1, max_bypass=0)
        assert res.ok
        assert (res.new_expires_at - now).days == pytest.approx(DAYS_PER_MONTH, abs=1)


class TestChangeTariff:
    """Смена тарифа БЕЗ оплаты: только пересчёт остатка."""

    @pytest.mark.asyncio
    async def test_upgrade_without_payment_shortens_term(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=511,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=now + timedelta(days=365),
            is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=2, max_bypass=0)
        assert res.ok
        assert user.sub_max_devices == 2
        assert (res.new_expires_at - now).days == pytest.approx(252, abs=1)  # 365×90/130
        assert user.balance_kopeks == 0, "смена тарифа не трогает деньги"

    @pytest.mark.asyncio
    async def test_downgrade_without_payment_lengthens_term(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=512,
            sub_max_devices=3, sub_max_bypass=1,   # 200 ₽/мес
            sub_expires_at=now + timedelta(days=100),
            is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=1, max_bypass=0)
        assert res.ok
        assert (res.new_expires_at - now).days == pytest.approx(222, abs=1)  # 100×200/90

    @pytest.mark.asyncio
    async def test_term_months_untouched(self, session: AsyncSession) -> None:
        """`sub_term_months` — это то, что юзер ПОКУПАЛ, ориентир автопродления.
        Смена тарифа покупкой не является и затирать его не должна."""
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=513,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=now + timedelta(days=365),
            sub_term_months=12, is_trial=False,
        )
        await billing.change_tariff(session, user, max_devices=2, max_bypass=0)
        assert user.sub_term_months == 12

    @pytest.mark.asyncio
    async def test_trial_cannot_change_tariff(self, session: AsyncSession) -> None:
        """Триальные дни не куплены — обменивать их не на что."""
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=514,
            sub_max_devices=1, sub_max_bypass=1,
            sub_expires_at=now + timedelta(days=7),
            is_trial=True,
        )
        res = await billing.change_tariff(session, user, max_devices=2, max_bypass=1)
        assert not res.ok
        assert res.reason == "trial"
        assert user.sub_max_devices == 1, "тариф изменён вопреки отказу"

    @pytest.mark.asyncio
    async def test_perpetual_cannot_change_tariff(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, tg_id=515,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=None, is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=2, max_bypass=0)
        assert not res.ok
        assert res.reason == "perpetual"

    @pytest.mark.asyncio
    async def test_expired_cannot_change_tariff(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=516,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=now - timedelta(days=1), is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=2, max_bypass=0)
        assert not res.ok
        assert res.reason == "expired"

    @pytest.mark.asyncio
    async def test_refuses_when_less_than_a_day_left(self, session: AsyncSession) -> None:
        """Апгрейд, после которого остаются часы, — это обнуление подписки
        одним тапом. Такую смену не проводим и говорим почему."""
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=517,
            sub_max_devices=1, sub_max_bypass=0,     # 90 ₽/мес
            sub_expires_at=now + timedelta(days=2),
            is_trial=False,
        )
        # 2 дня по 90 ₽ на тарифе 10+10 (90+9×40+10×30 = 750 ₽) — это 5 часов.
        res = await billing.change_tariff(session, user, max_devices=10, max_bypass=10)
        assert not res.ok
        assert res.reason == "too_short"
        assert user.sub_max_devices == 1

    @pytest.mark.asyncio
    async def test_empty_tariff_rejected(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=518,
            sub_max_devices=2, sub_max_bypass=1,
            sub_expires_at=now + timedelta(days=100), is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=0, max_bypass=0)
        assert not res.ok
        assert res.reason == "empty"

    @pytest.mark.asyncio
    async def test_no_change_is_rejected(self, session: AsyncSession) -> None:
        """Тот же самый тариф — менять нечего, лишний журнальный след не нужен."""
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=519,
            sub_max_devices=2, sub_max_bypass=1,
            sub_expires_at=now + timedelta(days=100), is_trial=False,
        )
        res = await billing.change_tariff(session, user, max_devices=2, max_bypass=1)
        assert not res.ok
        assert res.reason == "same"

    @pytest.mark.asyncio
    async def test_logs_to_audit(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=520,
            sub_max_devices=1, sub_max_bypass=0,
            sub_expires_at=now + timedelta(days=365), is_trial=False,
        )
        await billing.change_tariff(session, user, max_devices=2, max_bypass=0)
        await session.commit()
        rows = await repo.list_audit(session, limit=10)
        assert any("тариф" in (r.details or "").lower() for r in rows), \
            "смена тарифа не попала в журнал"

    @pytest.mark.asyncio
    async def test_cannot_go_below_what_is_in_use(self, session: AsyncSession) -> None:
        """Понижение ниже фактически занятого — способ получить больше за
        меньше: лишние устройства проверяются только при ДОБАВЛЕНИИ и до конца
        срока продолжали бы работать."""
        from bot.db import models

        now = datetime.now(timezone.utc)
        user = await _make_user(
            session, tg_id=521,
            sub_max_devices=3, sub_max_bypass=0,
            sub_expires_at=now + timedelta(days=100), is_trial=False,
        )
        for i in range(2):
            session.add(
                models.Device(
                    user_id=user.id, label=f"D{i}",
                    status=models.PeerStatus.ACTIVE,
                )
            )
        await session.flush()

        res = await billing.change_tariff(session, user, max_devices=1, max_bypass=0)
        assert not res.ok
        assert res.reason == "in_use"
        assert res.used_devices == 2
        assert user.sub_max_devices == 3
