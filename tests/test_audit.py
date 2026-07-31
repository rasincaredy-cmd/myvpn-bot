"""Тесты журнала действий."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, AuditLog


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
        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].action == AuditAction.CONFIG_ISSUED
        assert rows[0].details == "Телефон"

    async def test_list_is_newest_first(self, session: AsyncSession) -> None:
        for i in range(3):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        rows = await repo.list_audit(session)
        assert [r.details for r in rows] == ["#2", "#1", "#0"]

    async def test_paging(self, session: AsyncSession) -> None:
        for i in range(5):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        page2 = await repo.list_audit(session, limit=2, offset=2)
        assert [r.details for r in page2] == ["#2", "#1"]
        assert await repo.count_audit(session) == 5

    async def test_history_filters_by_user(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=1)
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=2)
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
        await session.flush()

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 1
        left = await repo.list_audit(session)
        assert [r.details for r in left] == ["свежая"]

    async def test_retention_zero_days_keeps_everything(self, session: AsyncSession) -> None:
        """0 = ретеншн выключен: журнал не чистится вообще."""
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            created_at=datetime.now(timezone.utc) - timedelta(days=999),
        )
        session.add(old)
        await session.flush()

        assert await repo.delete_audit_older_than(session, days=0) == 0
        assert len(await repo.list_audit(session)) == 1
