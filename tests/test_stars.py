"""Оплата звёздами: пересчёт и защита от двойного зачисления."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import StarPayment
from bot.services.pricing import monthly_price_kopeks, stars_for_kopeks


class TestStarPrice:
    def test_markup_is_twentyfive_percent(self) -> None:
        assert stars_for_kopeks(120_00) == 150
        assert stars_for_kopeks(160_00) == 200
        assert stars_for_kopeks(1080_00) == 1350

    def test_fraction_rounds_up(self) -> None:
        """Дробной звезды не бывает. Округление вниз означало бы, что сервис
        дарит долю звезды на каждой покупке."""
        assert stars_for_kopeks(90_00) == 113        # 112,5 → 113

    def test_typical_tariff_in_stars(self) -> None:
        assert stars_for_kopeks(monthly_price_kopeks(1, 1)) == 150


class TestStarCredit:
    async def test_credits_balance_once(self, session: AsyncSession) -> None:
        """Повторно доставленный платёж не должен зачислиться дважды."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4301, username="u", full_name="U"
        )
        await session.commit()

        first = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()
        second = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()

        assert first.credited is True
        assert second.credited is False, "повторная доставка зачислила деньги второй раз"
        assert user.balance_kopeks == 120_00

    async def test_no_bonus_on_stars(self, session: AsyncSession) -> None:
        """У звёзд своя наценка 25 % — бонус поверх неё был бы
        взаимоисключающим."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4302, username="u", full_name="U"
        )
        await session.commit()

        await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-2",
            amount_kopeks=100_00, stars=125,
        )
        await session.commit()

        rows = await repo.list_balance_txs(session, user.id, limit=10)
        assert {r.kind for r in rows} == {"deposit"}
        assert user.balance_kopeks == 100_00

    async def test_referrer_gets_his_percent(self, session: AsyncSession) -> None:
        """Пополнение звёздами — такое же пополнение: пригласивший получает
        свои проценты, иначе способ оплаты молча отменял бы рефералку."""
        from bot.services import stars as stars_svc

        boss = await repo.get_or_create_user(
            session, tg_id=4303, username="boss", full_name="B"
        )
        await session.commit()
        friend = await repo.get_or_create_user(
            session, tg_id=4304, username="f", full_name="F"
        )
        friend.referrer_id = boss.id
        await session.commit()

        await stars_svc.credit_star_payment(
            session, user_id=friend.id, charge_id="ch-3",
            amount_kopeks=200_00, stars=250,
        )
        await session.commit()

        assert boss.balance_kopeks == 200_00 * settings.referral_percent // 100

    async def test_wipe_takes_star_payments_too(self, session: AsyncSession) -> None:
        """«Сотрите мои данные» обязано унести и звёздные платежи: это такая же
        история оплат, как инвойсы."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4305, username="u", full_name="U"
        )
        await session.commit()
        await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-4",
            amount_kopeks=100_00, stars=125,
        )
        await session.commit()

        purged = await repo.purge_user_records(session, user.id)
        await session.commit()

        assert purged["star_payments"] == 1
        assert await session.get(StarPayment, "ch-4") is None
