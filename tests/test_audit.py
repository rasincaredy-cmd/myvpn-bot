"""Тесты журнала действий."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
