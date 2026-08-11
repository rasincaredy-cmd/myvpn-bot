"""Тесты Platega: конвертация сумм, выключенность без ключей, счета и зачисление.

Сеть не трогаем: сам HTTP-клиент проверяется живыми запросами руками (см.
спеку), а здесь — денежная логика и то, что бот не сломается от чужих ответов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.services import billing, platega


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


class TestCrediting:
    @pytest.mark.asyncio
    async def test_payment_credits_balance(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=601)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-pay",
            amount_kopeks=300_00, url="u",
        )
        dep = await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert dep.credited is True
        assert user.balance_kopeks == 300_00
        assert row.status == "paid"
        assert row.paid_at is not None

    @pytest.mark.asyncio
    async def test_no_bonus_for_card(self, session: AsyncSession) -> None:
        """Карта и СБП — самый дорогой для сервиса способ, бонуса за него нет
        (решение 8.08). Зачисляем ровно сумму счёта."""
        user = await _user(session, tg_id=602)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-nobonus",
            amount_kopeks=100_00, url="u",
        )
        await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert user.balance_kopeks == 100_00

    @pytest.mark.asyncio
    async def test_double_credit_impossible(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и тик планировщика могут увидеть оплату
        одновременно — баланс обязан вырасти один раз."""
        user = await _user(session, tg_id=603)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-twice",
            amount_kopeks=250_00, url="u",
        )
        first = await billing.apply_paid_platega(session, row)
        second = await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert first.credited is True
        assert second.credited is False
        assert user.balance_kopeks == 250_00

    @pytest.mark.asyncio
    async def test_referrer_gets_percent(self, session: AsyncSession) -> None:
        inviter = await _user(session, tg_id=604)
        buyer = await _user(session, tg_id=605)
        buyer.referrer_id = inviter.id
        await session.flush()
        row = await repo.create_platega_payment(
            session, user_id=buyer.id, transaction_id="tx-ref",
            amount_kopeks=1000_00, url="u",
        )
        dep = await billing.apply_paid_platega(session, row)
        await session.refresh(inviter)
        assert dep.ref_reward_kopeks == 1000_00 * settings.referral_percent // 100
        assert inviter.balance_kopeks == dep.ref_reward_kopeks


class TestScreens:
    def test_method_button_present(self) -> None:
        """Кнопка карты/СБП есть на экране выбора способа."""
        from bot.keyboards.inline import deposit_methods_kb

        kb = deposit_methods_kb(4, cryptobot=True, platega=True)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Карта" in t for t in texts)

    def test_method_button_hidden_without_keys(self) -> None:
        """Ключей нет — кнопки нет: счёт всё равно не создать."""
        from bot.keyboards.inline import deposit_methods_kb

        kb = deposit_methods_kb(4, cryptobot=True, platega=False)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("Карта" in t for t in texts)

    def test_amounts_keyboard_routes_to_platega(self) -> None:
        from bot.keyboards.inline import platega_amounts_kb

        kb = platega_amounts_kb([(90, "90 ₽ — месяц"), (240, "240 ₽ — 3 мес")])
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "bal:pg:90" in data
        assert "bal:pg:custom" in data

    def test_invoice_keyboard_has_pay_and_check(self) -> None:
        from bot.keyboards.inline import platega_invoice_kb

        kb = platega_invoice_kb("https://pay.platega.io?id=x", 7)
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert any(b.url == "https://pay.platega.io?id=x" for b in buttons)
        assert any(b.callback_data == "bal:pgchk:7" for b in buttons)

    def test_ttl_named_in_minutes(self) -> None:
        """На экране счёта обязан стоять реальный срок жизни (30 минут), иначе
        юзер уйдёт пить чай и вернётся к мёртвому счёту."""
        assert platega.INVOICE_TTL_MINUTES == 30
