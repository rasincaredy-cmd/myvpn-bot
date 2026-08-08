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
    def test_first_position_costs_the_minimum(self) -> None:
        """Любая одна позиция стоит 90 ₽ — это пол тарифа (решение Влада 8.08)."""
        assert monthly_price_kopeks(1, 0) == 90_00
        assert monthly_price_kopeks(0, 1) == 90_00

    def test_next_positions_add_up(self) -> None:
        assert monthly_price_kopeks(1, 1) == 120_00   # +30 за подключение
        assert monthly_price_kopeks(2, 1) == 160_00   # +40 за устройство
        assert monthly_price_kopeks(3, 1) == 200_00
        assert monthly_price_kopeks(2, 0) == 130_00
        assert monthly_price_kopeks(0, 2) == 120_00
        assert monthly_price_kopeks(1, 3) == 180_00

    def test_nothing_costs_less_than_the_floor(self) -> None:
        """Формула только складывает — уйти ниже 90 ₽ неоткуда.

        Прежняя вычитала из базы неиспользуемые позиции и при неудачной
        правке цен могла уйти в минус; тест стережёт, что это не вернётся.
        """
        for devices in range(0, 11):
            for bypass in range(0, 11):
                if devices + bypass == 0:
                    continue
                assert monthly_price_kopeks(devices, bypass) >= 90_00

    def test_monthly_empty_tariff_rejected(self) -> None:
        with pytest.raises(ValueError):
            monthly_price_kopeks(0, 0)

    def test_term_discounts_round_down_to_10(self) -> None:
        m = monthly_price_kopeks(1, 1)  # 120 ₽
        assert term_price_kopeks(m, 1) == 120_00
        assert term_price_kopeks(m, 3) == 320_00    # 360 −10% = 324 → вниз до 320
        assert term_price_kopeks(m, 6) == 610_00    # 720 −15% = 612 → 610
        assert term_price_kopeks(m, 12) == 1080_00  # 1440 −25% = 1080

    def test_term_discounts_on_the_floor_tariff(self) -> None:
        m = monthly_price_kopeks(1, 0)  # 90 ₽
        assert term_price_kopeks(m, 3) == 240_00
        assert term_price_kopeks(m, 6) == 450_00
        assert term_price_kopeks(m, 12) == 810_00

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
        assert user.balance_kopeks == 93_60      # 90 ₽ + бонус 4 % за CryptoBot
        assert inv.status == "paid" and inv.paid_at is not None
        txs = await repo.list_balance_txs(session, user.id)
        assert [tx.kind for tx in txs] == ["bonus", "deposit"]

    async def test_deposit_is_idempotent(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и поллинг наперегонки не задваивают зачисление."""
        user = await _make_user(session)
        inv = await _make_invoice(session, user, 90_00)
        await billing.apply_paid_invoice(session, inv)
        dep2 = await billing.apply_paid_invoice(session, inv)
        await session.commit()
        assert not dep2.credited
        assert user.balance_kopeks == 93_60          # 90 ₽ + бонус, ровно один раз
        assert len(await repo.list_balance_txs(session, user.id)) == 2

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


class TestDepositBonus:
    """Бонус за способ пополнения (этап D): надбавка к зачислению, ведущая
    юзера к способу, который дешевле обходится сервису."""

    def test_cryptobot_gives_four_percent(self) -> None:
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "cryptobot") == 4_00
        assert deposit_bonus_kopeks(1000_00, "cryptobot") == 40_00

    def test_expensive_methods_give_nothing(self) -> None:
        """Карта и СБП обходятся сервису в 9 и 8 % — доплачивать юзеру за
        самый невыгодный способ нельзя. У звёзд своя наценка 25 %, бонус
        поверх неё был бы взаимоисключающим."""
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "platega") == 0
        assert deposit_bonus_kopeks(100_00, "stars") == 0

    def test_unknown_method_gives_nothing(self) -> None:
        """Новый провайдер не должен начать раздавать бонусы по умолчанию."""
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "нет такого") == 0

    async def test_bonus_lands_as_its_own_row(self, session: AsyncSession) -> None:
        """Бонус — отдельная строка, а не надбавка внутри пополнения.

        Иначе статистика «пополнений за 30 дней» показывала бы сумму, которой
        сервис никогда не получал.
        """
        user = await _make_user(session, tg_id=4101)
        inv = await _make_invoice(session, user, 100_00)

        res = await billing.apply_paid_invoice(session, inv)
        await session.commit()

        assert res.credited
        assert user.balance_kopeks == 104_00
        rows = await repo.list_balance_txs(session, user.id)
        kinds = {r.kind: r.amount_kopeks for r in rows}
        assert kinds["deposit"] == 100_00, "пополнение раздуто бонусом"
        assert kinds["bonus"] == 4_00


class TestCharge:
    async def test_not_enough_balance(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=50_00, sub_max_devices=1, sub_max_bypass=1
        )
        res = await billing.charge_and_extend(session, user, 1)
        assert not res.ok
        assert res.price_kopeks == 120_00 and res.missing_kopeks == 70_00
        assert user.balance_kopeks == 50_00  # ничего не списано

    async def test_charge_extends_from_now_when_expired(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=5)
        user = await _make_user(
            session, balance_kopeks=150_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(session, user, 1)
        await session.commit()
        assert res.ok and res.price_kopeks == 120_00
        assert user.balance_kopeks == 30_00
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
        assert res.ok and res.price_kopeks == 320_00
        # 10 оставшихся дней не сгорели: срок = старый + 90 дней.
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=99) < left < timedelta(days=101)

    async def test_charge_zero_device_tariff(self, session: AsyncSession) -> None:
        """Тариф «0 устройств + 1 подключение» — одна позиция, то есть пол
        тарифа: 90 ₽ (Блок «Ревизия», цена пересмотрена 8.08)."""
        user = await _make_user(
            session, balance_kopeks=100_00,
            sub_expires_at=datetime.now(timezone.utc),
            sub_max_devices=1, sub_max_bypass=1,
        )
        res = await billing.charge_and_extend(
            session, user, 1, max_devices=0, max_bypass=1
        )
        await session.commit()
        assert res.ok and res.price_kopeks == 90_00
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
        assert res.ok and res.price_kopeks == 190_00  # 90 + 40 + 30 + 30
        assert user.sub_max_devices == 2 and user.sub_max_bypass == 2
        txs = await repo.list_balance_txs(session, user.id)
        assert txs[0].kind == "charge" and txs[0].amount_kopeks == -190_00


class TestInstantAutopay:
    """billing.autopay_if_expired — мгновенное автопродление после пополнения
    (кнопка «Проверить», начисление админом) и тик планировщика."""

    async def test_extends_expired_sub(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=2)
        user = await _make_user(
            session, balance_kopeks=120_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok and res.price_kopeks == 120_00
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
        inv = await _make_invoice(session, user, 120_00)
        dep = await billing.apply_paid_invoice(session, inv)
        assert dep.credited and user.balance_kopeks == 124_80  # 120 ₽ + бонус 4 %
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok
        assert user.balance_kopeks == 4_80        # бонус остался на балансе
        txs = await repo.list_balance_txs(session, user.id)
        assert [tx.kind for tx in txs] == ["charge", "bonus", "deposit"]


class TestAutopayTerm:
    """Автопродление на ТОТ ЖЕ срок, что юзер покупал, с откатом на меньший при
    нехватке баланса. Раньше продлевали всегда на месяц — купивший год со
    скидкой 25% незаметно начинал платить помесячно по полной цене."""

    async def test_purchase_remembers_term(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=1080_00, sub_max_devices=1, sub_max_bypass=1
        )
        await billing.charge_and_extend(session, user, 12)
        await session.commit()
        assert user.sub_term_months == 12

    async def test_autopay_renews_for_bought_term(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=1080_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=12,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok
        assert res.months == 12 and res.price_kopeks == 1080_00  # со скидкой 25%
        assert user.balance_kopeks == 0
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=359) < left < timedelta(days=361)

    async def test_autopay_falls_back_to_affordable_term(self, session: AsyncSession) -> None:
        """Купил год, на балансе 700 ₽: вместо паузы продлеваем на 6 мес
        (610 ₽) — максимальный срок, на который хватает."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=700_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=12,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res is not None and res.ok
        assert res.months == 6 and res.price_kopeks == 610_00
        assert res.wanted_months == 12 and res.wanted_price_kopeks == 1080_00
        assert res.missing_kopeks == 380_00       # сколько не хватило до года
        assert user.balance_kopeks == 90_00

    async def test_full_term_is_not_marked_shortened(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=1080_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=12,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res.months == res.wanted_months and res.missing_kopeks == 0

    async def test_unknown_term_renews_monthly(self, session: AsyncSession) -> None:
        """Старые юзеры (покупали до этой правки) — месяц, как и раньше."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=1080_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=None,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res.months == 1 and res.price_kopeks == 120_00
        assert user.balance_kopeks == 960_00

    async def test_never_renews_longer_than_bought(self, session: AsyncSession) -> None:
        """Денег хватает на год, но покупали месяц — списываем месяц."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=1000_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=1,
        )
        res = await billing.autopay_if_expired(session, user)
        await session.commit()
        assert res.months == 1 and res.price_kopeks == 120_00

    async def test_waits_when_not_enough_even_for_month(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = await _make_user(
            session, balance_kopeks=10_00, sub_expires_at=past,
            sub_max_devices=1, sub_max_bypass=1, autopay=True, sub_term_months=12,
        )
        assert await billing.autopay_if_expired(session, user) is None
        assert user.balance_kopeks == 10_00  # ничего не списано


class TestAdminGrantTerm:
    """Админ выдаёт подписку на один из тех же сроков, что продаются юзеру
    (1/3/6/12 мес) — без списания денег, но со всеми свойствами покупки."""

    async def test_grant_extends_and_charges_nothing(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, balance_kopeks=500_00, sub_max_devices=1, sub_max_bypass=1
        )
        res = await billing.grant_term(session, user, 3)
        await session.commit()
        assert res.ok and res.months == 3
        assert res.price_kopeks == 0            # выдача бесплатна
        assert user.balance_kopeks == 500_00    # баланс не тронут
        txs = await repo.list_balance_txs(session, user.id)
        assert txs == []                        # в журнале денег ничего нет

    async def test_grant_sets_term_for_autopay(self, session: AsyncSession) -> None:
        """Выдали год → автопродление дальше берёт год, а не месяц."""
        user = await _make_user(session, sub_max_devices=1, sub_max_bypass=1)
        await billing.grant_term(session, user, 12)
        await session.commit()
        assert user.sub_term_months == 12

    async def test_grant_stacks_on_active_sub(self, session: AsyncSession) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=10)
        user = await _make_user(session, sub_expires_at=future)
        res = await billing.grant_term(session, user, 1)
        await session.commit()
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=39) < left < timedelta(days=41)  # 10 дней не сгорели

    async def test_grant_from_now_when_expired(self, session: AsyncSession) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=100)
        user = await _make_user(session, sub_expires_at=past)
        res = await billing.grant_term(session, user, 1)
        await session.commit()
        left = res.new_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=29) < left < timedelta(days=31)

    async def test_grant_makes_subscription_paid(self, session: AsyncSession) -> None:
        user = await _make_user(
            session, is_trial=True, sub_traffic_limit_bytes=10 * 1024**3
        )
        await billing.grant_term(session, user, 1)
        await session.commit()
        assert user.is_trial is False
        assert user.sub_traffic_limit_bytes is None  # как у покупки: безлимит

    async def test_grant_rejects_unknown_term(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        with pytest.raises(ValueError):
            await billing.grant_term(session, user, 7)


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
