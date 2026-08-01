"""Тесты журнала действий."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction, AuditLog
from bot.services import billing


class TestAuditModel:
    async def test_row_is_stored_with_all_fields(self, session: AsyncSession) -> None:
        session.add(AuditLog(
            actor_tg_id=111,
            actor_is_admin=True,
            action=AuditAction.ADMIN_CREDIT,
            target_user_id=7,
            target_type="user",
            target_id=7,
            amount_kopeks=13000,
            details="Начислено вручную",
        ))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.id is not None
        assert row.created_at is not None
        assert row.action == AuditAction.ADMIN_CREDIT
        assert row.amount_kopeks == 13000
        assert row.target_user_id == 7

    async def test_system_event_has_no_actor(self, session: AsyncSession) -> None:
        """События планировщика пишутся без человека-инициатора."""
        session.add(AuditLog(action=AuditAction.SERVER_DOWN, target_type="server", target_id=1))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.actor_tg_id is None
        assert row.actor_is_admin is False
        assert row.amount_kopeks is None

    async def test_survives_real_roundtrip(self, session: AsyncSession) -> None:
        """Читаем из базы, а не из identity map: после commit+expunge_all
        объект собирается заново из строк таблицы."""
        session.add(AuditLog(
            action=AuditAction.BALANCE_CHARGE,
            target_user_id=5,
            amount_kopeks=13000,
            details="Подписка 3 мес",
        ))
        await session.commit()
        session.expunge_all()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.action == AuditAction.BALANCE_CHARGE
        assert row.amount_kopeks == 13000
        assert row.target_user_id == 5
        assert row.details == "Подписка 3 мес"


class TestAuditRepo:
    async def test_log_action_writes_row(self, session: AsyncSession) -> None:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=555, target_user_id=3, target_type="peer", target_id=9,
            details="Телефон",
        )
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].action == AuditAction.CONFIG_ISSUED
        assert rows[0].details == "Телефон"
        assert rows[0].actor_tg_id == 555
        assert rows[0].target_type == "peer"
        assert rows[0].target_id == 9

    async def test_list_is_newest_first(self, session: AsyncSession) -> None:
        for i in range(3):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert [r.details for r in rows] == ["#2", "#1", "#0"]

    async def test_paging(self, session: AsyncSession) -> None:
        for i in range(5):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        await session.commit()
        session.expunge_all()

        page2 = await repo.list_audit(session, limit=2, offset=2)
        assert [r.details for r in page2] == ["#2", "#1"]
        assert await repo.count_audit(session) == 5

    async def test_history_filters_by_user(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=1)
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=2)
        await session.commit()
        session.expunge_all()

        mine = await repo.list_audit_for_user(session, 1)
        assert len(mine) == 1
        assert mine[0].target_user_id == 1
        assert await repo.count_audit_for_user(session, 1) == 1

    async def test_retention_deletes_only_old(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, details="свежая")
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            details="старая",
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
        )
        session.add(old)
        await session.commit()
        session.expunge_all()

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 1
        left = await repo.list_audit(session)
        assert [r.details for r in left] == ["свежая"]

    async def test_retention_after_reading_feed_from_db(self, session: AsyncSession) -> None:
        """Главный краевой случай: created_at проставлен базой (server_default),
        то есть без таймзоны. Сначала читаем ленту — записи попадают в identity
        map с naive created_at, — и только потом чистим. Если чистка сравнивает
        даты в Python, а не в SQL, здесь ловится TypeError."""
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, details="свежая")
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].created_at.tzinfo is None  # база отдала без таймзоны

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 0
        assert len(await repo.list_audit(session)) == 1

    async def test_retention_zero_days_keeps_everything(self, session: AsyncSession) -> None:
        """0 = ретеншн выключен: журнал не чистится вообще."""
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            created_at=datetime.now(timezone.utc) - timedelta(days=999),
        )
        session.add(old)
        await session.commit()
        session.expunge_all()

        assert await repo.delete_audit_older_than(session, days=0) == 0
        assert len(await repo.list_audit(session)) == 1


class TestAuditMoney:
    """Денежные события попадают в журнал сами, из тех же функций, что двигают
    деньги. Везде читаем после commit+expunge_all — иначе проверялась бы память
    сессии, а не строки таблицы."""

    async def test_purchase_is_logged(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1001, username="buyer", full_name="Buyer"
        )
        user.balance_kopeks = 100_000
        user.sub_max_devices = 2
        user.sub_max_bypass = 1
        await session.flush()
        user_id, tg_id = user.id, user.tg_id

        res = await billing.charge_and_extend(session, user, months=1)
        assert res.ok
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        charges = [r for r in rows if r.action == AuditAction.BALANCE_CHARGE]
        assert len(charges) == 1
        assert charges[0].amount_kopeks == res.price_kopeks
        assert charges[0].actor_tg_id == tg_id
        assert charges[0].actor_is_admin is False
        assert charges[0].details == "Подписка 1 мес (устройств: 2, обходов: 1)"

    async def test_failed_purchase_is_not_logged(self, session: AsyncSession) -> None:
        """Не хватило денег — списания не было, значит и события быть не должно."""
        user = await repo.get_or_create_user(
            session, tg_id=1005, username="poor", full_name="Poor"
        )
        user.balance_kopeks = 10_00
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id = user.id

        res = await billing.charge_and_extend(session, user, months=1)
        assert not res.ok
        await session.commit()
        session.expunge_all()

        assert await repo.list_audit_for_user(session, user_id) == []

    async def test_admin_grant_is_logged_as_admin_action(
        self, session: AsyncSession
    ) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1002, username="gifted", full_name="Gifted"
        )
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id = user.id

        await billing.grant_term(session, user, months=3, actor_tg_id=111)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        grants = [r for r in rows if r.action == AuditAction.SUB_GRANTED]
        assert len(grants) == 1
        assert grants[0].actor_is_admin is True
        assert grants[0].actor_tg_id == 111
        # Подарок — не списание: денежной суммы у события нет.
        assert grants[0].amount_kopeks is None

    async def test_deposit_is_logged(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1003, username="payer", full_name="Payer"
        )
        await session.flush()
        user_id, tg_id = user.id, user.tg_id
        inv = await repo.create_crypto_invoice(
            session, user_id=user_id, invoice_id=9001,
            amount_kopeks=300_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        tops = [r for r in rows if r.action == AuditAction.BALANCE_TOPUP]
        assert len(tops) == 1
        assert tops[0].amount_kopeks == 300_00
        assert tops[0].actor_tg_id == tg_id
        assert tops[0].details == "Пополнение баланса"

    async def test_repeated_deposit_is_logged_once(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и поллинг наперегонки не задваивают ни деньги,
        ни запись в журнале."""
        user = await repo.get_or_create_user(
            session, tg_id=1006, username="payer2", full_name="Payer2"
        )
        await session.flush()
        user_id = user.id
        inv = await repo.create_crypto_invoice(
            session, user_id=user_id, invoice_id=9003,
            amount_kopeks=100_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        assert len([r for r in rows if r.action == AuditAction.BALANCE_TOPUP]) == 1

    async def test_referral_reward_is_logged_on_referrer(
        self, session: AsyncSession
    ) -> None:
        """Награда записывается пригласившему: спор разбирается по его карточке."""
        referrer = await repo.get_or_create_user(
            session, tg_id=1004, username="ref", full_name="Ref"
        )
        await session.flush()
        user = await repo.get_or_create_user(
            session, tg_id=1105, username="son", full_name="Son"
        )
        user.referrer_id = referrer.id
        await session.flush()
        referrer_id = referrer.id
        inv = await repo.create_crypto_invoice(
            session, user_id=user.id, invoice_id=9002,
            amount_kopeks=200_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, referrer_id)
        rewards = [r for r in rows if r.action == AuditAction.REFERRAL_REWARD]
        assert len(rewards) == 1
        assert rewards[0].amount_kopeks == 200_00 * settings.referral_percent // 100
        # Награду начислил бот, а не человек — инициатора у события нет.
        assert rewards[0].actor_tg_id is None
        assert rewards[0].details == f"{settings.referral_percent}% с пополнения реферала"
