"""Тесты Platega: конвертация сумм, выключенность без ключей, счета и зачисление.

Сеть не трогаем: сам HTTP-клиент проверяется живыми запросами руками (см.
спеку), а здесь — денежная логика и то, что бот не сломается от чужих ответов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services import platega


async def _user(session: AsyncSession, tg_id: int = 501):
    return await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )


class TestAmountConversion:
    def test_whole_rubles(self) -> None:
        assert platega.amount_to_rub(300_00) == 300.0

    def test_kopeks_survive(self) -> None:
        """90.50 ₽ обязаны уехать как 90.5, а не как 90 или 9050."""
        assert platega.amount_to_rub(90_50) == 90.5

    def test_no_float_drift(self) -> None:
        """Копейки считаем целыми и делим один раз — накопленной ошибки быть не может."""
        assert platega.amount_to_rub(10_01) == 10.01
        assert platega.amount_to_rub(1_000_000_00) == 1_000_000.0


class TestEnabled:
    def test_disabled_without_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_needs_both_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Один ключ без второго — это не настроенная платёжка, а опечатка в .env."""
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_enabled_with_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "sec")
        assert platega.enabled() is True


class TestPaymentRows:
    @pytest.mark.asyncio
    async def test_created_row_is_pending(self, session: AsyncSession) -> None:
        user = await _user(session)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-1",
            amount_kopeks=300_00, url="https://pay.platega.io?id=tx-1",
        )
        assert row.status == "pending"
        assert row.paid_at is None
        assert (await repo.get_platega_payment(session, row.id)).transaction_id == "tx-1"

    @pytest.mark.asyncio
    async def test_only_pending_are_polled(self, session: AsyncSession) -> None:
        """Оплаченные и отменённые счета опрашивать незачем — они финальны."""
        user = await _user(session)
        pending = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-open",
            amount_kopeks=100_00, url="u",
        )
        paid = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-paid",
            amount_kopeks=100_00, url="u",
        )
        paid.status = "paid"
        await session.flush()
        open_rows = await repo.list_open_platega_payments(session)
        assert [r.id for r in open_rows] == [pending.id]

    @pytest.mark.asyncio
    async def test_stale_rows_are_dropped(self, session: AsyncSession) -> None:
        """Счёт живёт 30 минут: вчерашние строки провайдер уже отменил сам,
        и гонять по ним запросы вечно не нужно."""
        user = await _user(session)
        old = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-old",
            amount_kopeks=100_00, url="u",
        )
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
        await session.flush()
        assert await repo.list_open_platega_payments(session) == []
