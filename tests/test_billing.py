"""Тесты Блока «Баланс»: цены, зачисление инвойсов, рефералка, покупка подписки.

Crypto Pay замокан на уровне строк БД (инвойс уже создан) — проверяем денежную
логику: идемпотентность зачисления, реф-процент, списание с продлением срока,
недостаток средств, автопродление-подобный сценарий.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.services import billing
from bot.services.pricing import monthly_price_kopeks, term_price_kopeks, fmt_rub


async def _make_user(session: AsyncSession, tg_id: int = 111, **kw):
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    for k, v in kw.items():
        setattr(user, k, v)
    await session.flush()
    return user


async def _make_invoice(session: AsyncSession, user, kopeks: int):
    return await repo.create_crypto_invoice(
        session, user_id=user.id, invoice_id=1000 + user.id,
        amount_kopeks=kopeks, url="https://t.me/CryptoBot?start=x",
    )


class TestPricing:
    def test_monthly_base_and_extras(self) -> None:
        assert monthly_price_kopeks(1, 1) == 90_00       # база: 1 устр + 1 обход
        assert monthly_price_kopeks(2, 1) == 120_00      # +30₽ за устройство
        assert monthly_price_kopeks(1, 3) == 150_00      # +30₽ за каждый обход
        assert monthly_price_kopeks(3, 2) == 180_00

    def test_monthly_zero_positions(self) -> None:
        # Блок «Ревизия»: отказ от позиции вычитает её доп. цену от базы —
        # «первая позиция 60₽, каждая следующая +30₽».
        assert monthly_price_kopeks(0, 1) == 60_00
        assert monthly_price_kopeks(1, 0) == 60_00
        assert monthly_price_kopeks(0, 2) == 90_00
        assert monthly_price_kopeks(2, 0) == 90_00

    def test_monthly_empty_tariff_rejected(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            monthly_price_kopeks(0, 0)

    def test_term_discounts_round_down_to_10(self) -> None:
        m = monthly_price_kopeks(1, 1)  # 90₽
        assert term_price_kopeks(m, 1) == 90_00
        assert term_price_kopeks(m, 3) == 240_00   # 270 −10% = 243 → вниз до 240
        assert term_price_kopeks(m, 6) == 450_00   # 540 −15% = 459 → 450
        assert term_price_kopeks(m, 12) == 810_00  # 1080 −25% = 810

    def test_fmt_rub(self) -> None:
        assert fmt_rub(90_00) == "90 ₽"
        assert fmt_rub(90_50) == "90.50 ₽"
        assert fmt_rub(-30_00) == "−30 ₽"


class TestDeposit:
    async def test_deposit_credits_balance(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        inv = await _make_invoice(session, user, 90_00)
        dep = await billing.apply_paid_invoice(session, inv)
        await session.commit()
        assert dep.credited and dep.user.id == user.id
        assert user.balance_kopeks == 90_00
        assert inv.status == "paid" and inv.paid_at is not None
        txs = await repo.list_balance_txs(session, user.id)
        assert [tx.kind for tx in txs] == ["deposit"]

    async def test_deposit_is_idempotent(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и поллинг наперегонки не задваивают зачисление."""
        user = await _make_user(session)
        inv = await _make_invoice(session, user, 90_00)
        await billing.apply_paid_invoice(session, inv)
        dep2 = await billing.apply_paid_invoice(session, inv)
        await session.commit()
        assert not dep2.credited
        assert user.balance_kopeks == 90_00
        assert len(await repo.list_balance_txs(session, user.id)) == 1

    async def test_referral_reward(self, session: AsyncSession) -> None:
        referrer = await _make_user(session, tg_id=100)
        user = await _make_user(session, tg_id=200, referrer_id=referrer.id)
        inv = await _make_invoice(session, user, 100_00)
        dep = await billing.apply_paid_invoice(session, inv)
        await session.commit()
        expected = 100_00 * settings.referral_percent // 100
        assert dep.referrer.id == referrer.id
        assert dep.ref_reward_kopeks == expected
        await session.refresh(referrer)
        assert referrer.balance_kopeks == expected
        assert await repo.sum_ref_earned(session, referrer.id) == expected
        assert await repo.count_referrals(session, referrer.id) == 1

    async def test_no_reward_without_referrer(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        inv = await _make_invoice(session, user, 100_00)
        dep = await billing.apply_paid_invoice(session, inv)
        assert dep.referrer is None and dep.ref_reward_kopeks == 0


class TestCharge:
    async def test_not_enough_balance(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=50_00, sub_max_devices=1, sub_max_bypass=1
        )
        res = await billing.charge_and_extend(session, user, 1)
        assert not res.ok
        assert res.price_kopeks == 90_00 and res.missing_kopeks == 40_00
        assert user.balance_kopeks == 50_00  # ничего не списано

    async def test_charge_extends_from_now_when_expired(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=5)
        user = await _make_user(
            session, balance_kopeks=100_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(session, user, 1)
        await session.commit()
        assert res.ok and res.price_kopeks == 90_00
        assert user.balance_kopeks == 10_00
        assert user.is_trial is False
        assert user.sub_traffic_limit_bytes is None  # платным — безлимит трафика
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=29) < left < timedelta(days=31)  # от now, не от past

    async def test_charge_stacks_on_active_sub(self, session: AsyncSession) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=10)
        user = await _make_user(
            session, balance_kopeks=500_00, sub_expires_at=future,
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(session, user, 3)
        await session.commit()
        assert res.ok and res.price_kopeks == 240_00
        # 10 оставшихся дней не сгорели: срок = старый + 90 дней.
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=99) < left < timedelta(days=101)

    async def test_charge_zero_device_tariff(self, session: AsyncSession) -> None:
        """Блок «Ревизия»: тариф «0 устройств + 1 обход» продаётся за 60₽."""
        user = await _make_user(
            session, balance_kopeks=100_00,
            sub_expires_at=datetime.now(timezone.utc),
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=0, max_bypass=1
        )
        await session.commit()
        assert res.ok and res.price_kopeks == 60_00
        assert user.sub_max_devices == 0 and user.sub_max_bypass == 1

    async def test_charge_rejects_empty_tariff(self, session: AsyncSession) -> None:
        """0/0 отбивается гардом ДО прайсинга — деньги не двигаются."""
        user = await _make_user(
            session, balance_kopeks=500_00,
            sub_expires_at=datetime.now(timezone.utc),
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=0, max_bypass=0
        )
        assert not res.ok
        assert user.balance_kopeks == 500_00
        assert user.sub_max_devices == 1  # тариф не тронут

    async def test_charge_with_tariff_change(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=200_00,
            sub_expires_at=datetime.now(timezone.utc),
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=2, max_bypass=2
        )
        await session.commit()
        assert res.ok and res.price_kopeks == 150_00  # 90+30+30
        assert user.sub_max_devices == 2 and user.sub_max_bypass == 2
        txs = await repo.list_balance_txs(session, user.id)
        assert txs[0].kind == "charge" and txs[0].amount_kopeks == -150_00


class TestInstantAutopay:
    """billing.autopay_if_expired — мгновенное автопродление после пополнения
    (кнопка «Проверить», начисление админом) и тик планировщика."""

    async def test_extends_expired_sub(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=2)
        user = await _make_user(
            session, balance_kopeks=90_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok and res.price_kopeks == 90_00
        assert user.balance_kopeks == 0
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=29) < left < timedelta(days=31)

    async def test_noop_when_sub_active(self, session: AsyncSession) -> None:
        """Пополнение при ЖИВОЙ подписке ничего не списывает — юзер сам решает,
        когда продлить."""
        future = datetime.now(timezone.utc) + timedelta(days=10)
        user = await _make_user(
            session, balance_kopeks=500_00, sub_expires_at=future, autopay=True,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 500_00

    async def test_noop_when_autopay_off(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=2)
        user = await _make_user(
            session, balance_kopeks=500_00, sub_expires_at=past, autopay=False,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 500_00

    async def test_noop_when_perpetual(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=500_00, sub_expires_at=None, autopay=True,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 500_00

    async def test_noop_when_empty_tariff(self, session: AsyncSession) -> None:
        """Админ выставил 0/0 → автопродление НЕ списывает деньги за пустоту
        (раньше списало бы 90₽ — тариф клампился к 1+1)."""
        past = datetime.now(timezone.utc) - timedelta(days=2)
        user = await _make_user(
            session, balance_kopeks=500_00, sub_expires_at=past,
            sub_max_devices=0, sub_max_bypass=0, autopay=True,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 500_00

    async def test_noop_when_not_enough_money(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=2)
        user = await _make_user(
            session, balance_kopeks=10_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 10_00  # ничего не списано

    async def test_deposit_then_autopay_full_flow(self, session: AsyncSession) -> None:
        """Сценарий кнопки «Проверить»: зачисление инвойса → мгновенное продление."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True,
        )
        inv = await _make_invoice(session, user, 90_00)
        dep = await billing.apply_paid_invoice(session, inv)
        assert dep.credited and user.balance_kopeks == 90_00
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok
        assert user.balance_kopeks == 0
        txs = await repo.list_balance_txs(session, user.id)
        assert [tx.kind for tx in txs] == ["charge", "deposit"]


class TestAdminAdjust:
    async def test_add_balance_tx_updates_and_journals(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        await repo.add_balance_tx(session, user.id, 90_00, "admin", note="перевод на карту")
        await repo.add_balance_tx(session, user.id, -20_00, "admin")
        await session.commit()
        await session.refresh(user)
        assert user.balance_kopeks == 70_00
        txs = await repo.list_balance_txs(session, user.id)
        assert len(txs) == 2 and txs[0].amount_kopeks == -20_00
