"""Журнал действий: запись событий и выборки для админки.

Пишем в той же сессии, что и само действие, — коммит на вызывающем. Так
запись о событии не появится, если само событие откатилось.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditAction, AuditLog


async def log_action(
    session: AsyncSession,
    action: AuditAction,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    target_user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    amount_kopeks: int | None = None,
    details: str | None = None,
) -> None:
    """Записывает событие. Коммит — на вызывающем."""
    session.add(AuditLog(
        action=action,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=target_user_id,
        target_type=target_type,
        target_id=target_id,
        amount_kopeks=amount_kopeks,
        details=details,
    ))
    await session.flush()


async def list_audit(
    session: AsyncSession, *, limit: int = 10, offset: int = 0
) -> list[AuditLog]:
    """Лента журнала, свежие сверху. Вторичная сортировка по id — у SQLite
    несколько записей в одной транзакции получают одинаковый created_at."""
    res = await session.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def count_audit(session: AsyncSession) -> int:
    res = await session.execute(select(func.count(AuditLog.id)))
    return int(res.scalar_one())


async def list_audit_for_user(
    session: AsyncSession, user_id: int, *, limit: int = 10, offset: int = 0
) -> list[AuditLog]:
    res = await session.execute(
        select(AuditLog)
        .where(AuditLog.target_user_id == user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def count_audit_for_user(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.target_user_id == user_id)
    )
    return int(res.scalar_one())


async def delete_audit_older_than(session: AsyncSession, days: int) -> int:
    """Чистка старых записей. days <= 0 — ретеншн выключен, не трогаем ничего.

    synchronize_session=False обязателен: SQLite отдаёт created_at без
    таймзоны, а cutoff — aware. Со стратегией по умолчанию SQLAlchemy сверял бы
    их в Python на уже загруженных объектах и падал с TypeError (тот же капкан,
    что описан в докстринге bot.utils.timefmt.as_utc). Сравнение остаётся в SQL.
    """
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(
        delete(AuditLog)
        .where(AuditLog.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(res.rowcount or 0)
