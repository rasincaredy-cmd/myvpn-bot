"""Инвайты: разовые ссылки на получение конфига с сервера."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Invite


async def get_invite(session: AsyncSession, token: str) -> Invite | None:
    return (
        await session.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()


async def mark_invite_used(session: AsyncSession, invite: Invite, tg_id: int) -> None:
    invite.used_by_tg_id = tg_id
    invite.used_at = datetime.now(timezone.utc)
    await session.flush()


async def list_invites_for_server(
    session: AsyncSession, server_id: int
) -> list[Invite]:
    result = await session.execute(
        select(Invite)
        .where(Invite.server_id == server_id)
        .order_by(Invite.created_at.desc())
    )
    return list(result.scalars())


async def delete_invite(session: AsyncSession, invite_id: int) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is not None:
        await session.delete(invite)
        await session.flush()
