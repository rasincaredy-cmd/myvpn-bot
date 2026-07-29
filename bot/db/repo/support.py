"""Блок «Сапорт-чат»: маршрутизация вопрос↔ответ."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import SupportMsg


async def add_support_route(
    session: AsyncSession, *, user_id: int, user_tg_id: int, user_msg_id: int,
    admin_tg_id: int, admin_msg_id: int,
) -> None:
    """Запоминает пару (сообщение у юзера ↔ сообщение у админа). Пишется при
    копировании вопроса админу И при доставке ответа юзеру — реплай на любую
    сторону продолжает переписку. Коммит — на вызывающем."""
    session.add(SupportMsg(
        user_id=user_id, user_tg_id=user_tg_id, user_msg_id=user_msg_id,
        admin_tg_id=admin_tg_id, admin_msg_id=admin_msg_id,
    ))
    await session.flush()


async def find_support_route_by_admin_msg(
    session: AsyncSession, admin_tg_id: int, admin_msg_id: int
) -> SupportMsg | None:
    return (await session.execute(
        select(SupportMsg)
        .where(SupportMsg.admin_tg_id == admin_tg_id)
        .where(SupportMsg.admin_msg_id == admin_msg_id)
    )).scalars().first()


async def is_support_reply_from_user(
    session: AsyncSession, user_tg_id: int, user_msg_id: int
) -> bool:
    """True, если юзер реплаит на сообщение, доставленное ему сапорт-чатом."""
    return (await session.execute(
        select(SupportMsg.id)
        .where(SupportMsg.user_tg_id == user_tg_id)
        .where(SupportMsg.user_msg_id == user_msg_id)
    )).scalars().first() is not None


async def purge_old_support_routes(session: AsyncSession, days: int = 30) -> int:
    """Удаляет маршруты старше days дней (реплай на них перестанет доставляться,
    но живой переписке 30 дней хватает с запасом). Возвращает число удалённых."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        delete(SupportMsg).where(SupportMsg.created_at < cutoff)
    )
    return result.rowcount or 0
