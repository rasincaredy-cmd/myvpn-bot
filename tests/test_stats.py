"""Статистика показывает чужих людей, а не своих.

Повод: 8.08.2026 конверсия показывала «1 из 7 покупали подписку», и этот один
был сам владелец — тестовая подписка на 12 месяцев за 27 270 ₽ на 50 устройств,
оплаченная деньгами, которые он сам себе начислил кнопкой за одиннадцать минут
до этого. Та же сумма стояла в строке «за 30 дней».
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.handlers.admin.stats import collect_money_stats


async def _user(session: AsyncSession, *, tg_id: int, admin=False, staff=False):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.is_admin = admin
    user.is_staff = staff
    await session.flush()
    return user


class TestStatsExcludesOwnPeople:
    async def test_admin_purchase_is_not_conversion(
        self, session: AsyncSession
    ) -> None:
        admin = await _user(session, tg_id=4201, admin=True)
        await repo.add_balance_tx(session, admin.id, -2727000, "charge", note="тест")
        await _user(session, tg_id=4202)
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_counted == 1, "админ попал в знаменатель конверсии"
        assert st.users_paid == 0, "тестовая покупка админа засчитана как продажа"
        assert st.charged_30d == 0

    async def test_staff_flag_excludes_too(self, session: AsyncSession) -> None:
        """Друзья платят вне бота, проверяющий из платёжки не купит никогда —
        обоим не место в знаменателе."""
        await _user(session, tg_id=4203, staff=True)
        await _user(session, tg_id=4204)
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_counted == 1
        assert st.staff_counted == 1

    async def test_staff_deposit_is_not_revenue(self, session: AsyncSession) -> None:
        """Пополнение служебного аккаунта — не выручка, а перекладывание из
        кармана в карман."""
        staff = await _user(session, tg_id=4205, staff=True)
        await repo.add_balance_tx(session, staff.id, 500_00, "deposit", note="тест")
        await session.commit()

        st = await collect_money_stats(session)

        assert st.deposited_30d == 0

    async def test_real_purchase_is_counted(self, session: AsyncSession) -> None:
        buyer = await _user(session, tg_id=4206)
        await repo.add_balance_tx(session, buyer.id, -120_00, "charge", note="покупка")
        await repo.add_balance_tx(session, buyer.id, 200_00, "deposit", note="пополнение")
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_counted == 1
        assert st.users_paid == 1
        assert st.charged_30d == 120_00
        assert st.deposited_30d == 200_00
