"""Баланс не должен уходить в минус (аудит 20.08.2026).

Проверка «хватает ли денег» и списание были ДВУМЯ шагами, а между ними покупка
успевает сходить по SSH оживлять устройства — это секунды. Второй тап «Купить»
в это окно читал ещё не изменённый баланс, проходил проверку и списывал второй
раз: два срока подписки по цене одного и минус на балансе.

Троттлинг (0.7 с) окно не закрывает: SSH-оживление длится дольше.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo


async def _user(session: AsyncSession, kopeks: int, tg_id: int = 701):
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    user.balance_kopeks = kopeks
    await session.flush()
    return user


class TestAtomicCharge:
    @pytest.mark.asyncio
    async def test_charges_when_enough(self, session: AsyncSession) -> None:
        user = await _user(session, 1000_00)
        assert await repo.charge_balance(session, user.id, 300_00, note="тест") is True
        await session.refresh(user)
        assert user.balance_kopeks == 700_00

    @pytest.mark.asyncio
    async def test_refuses_when_not_enough(self, session: AsyncSession) -> None:
        user = await _user(session, 100_00, tg_id=702)
        assert await repo.charge_balance(session, user.id, 300_00, note="тест") is False
        await session.refresh(user)
        assert user.balance_kopeks == 100_00, "деньги списались при отказе"

    @pytest.mark.asyncio
    async def test_exact_amount_passes(self, session: AsyncSession) -> None:
        """Ровно столько, сколько есть, — это «хватает», а не «не хватает»."""
        user = await _user(session, 300_00, tg_id=703)
        assert await repo.charge_balance(session, user.id, 300_00, note="тест") is True
        await session.refresh(user)
        assert user.balance_kopeks == 0

    @pytest.mark.asyncio
    async def test_second_charge_on_stale_balance_is_refused(
        self, session: AsyncSession
    ) -> None:
        """Ядро бага: второе списание по УСТАРЕВШЕМУ представлению о балансе.

        Имитируем два тапа: код обоих раз «увидел» 300 ₽ и решил, что денег
        хватает. Второе списание обязано провалиться на уровне базы, а не
        полагаться на то, что кто-то раньше прочитал свежее значение.
        """
        user = await _user(session, 300_00, tg_id=704)
        seen_balance = user.balance_kopeks          # оба «тапа» видели это
        assert seen_balance >= 300_00

        assert await repo.charge_balance(session, user.id, 300_00, note="тап 1") is True
        assert await repo.charge_balance(session, user.id, 300_00, note="тап 2") is False

        await session.refresh(user)
        assert user.balance_kopeks == 0, "баланс ушёл в минус"

    @pytest.mark.asyncio
    async def test_refusal_leaves_no_journal_row(self, session: AsyncSession) -> None:
        """Отказ не должен оставлять строку в истории операций: юзер увидел бы
        списание, которого не было."""
        user = await _user(session, 100_00, tg_id=705)
        await repo.charge_balance(session, user.id, 300_00, note="не должно попасть")
        rows = await repo.list_balance_txs(session, user.id, limit=10)
        assert not [r for r in rows if r.note == "не должно попасть"]


class TestPurchaseCannotOverdraw:
    @pytest.mark.asyncio
    async def test_double_purchase_charges_once(self, session: AsyncSession) -> None:
        """Покупка целиком: второй заход с тем же (устаревшим) объектом юзера
        не должен увести баланс в минус."""
        from datetime import datetime, timedelta, timezone

        from bot.services import billing
        from bot.services.pricing import monthly_price_kopeks, term_price_kopeks

        price = term_price_kopeks(monthly_price_kopeks(1, 0), 1)
        user = await _user(session, price, tg_id=706)
        user.sub_max_devices, user.sub_max_bypass = 1, 0
        user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=10)
        user.is_trial = False
        await session.flush()

        first = await billing.charge_and_extend(session, user, 1, max_devices=1, max_bypass=0)
        second = await billing.charge_and_extend(session, user, 1, max_devices=1, max_bypass=0)

        assert first.ok
        assert not second.ok, "вторая покупка прошла на пустом балансе"
        await session.refresh(user)
        assert user.balance_kopeks >= 0, "баланс ушёл в минус"
