"""Системные команды для админа: статистика, проверка SSH-связи."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Invite, Peer, PeerStatus, Server, ServerStatus, User
from bot.filters.admin import AdminFilter

router = Router(name="admin")
router.message.filter(AdminFilter())


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    users_total = (await session.execute(select(func.count(User.id)))).scalar() or 0
    servers_total = (await session.execute(select(func.count(Server.id)))).scalar() or 0
    servers_ready = (
        await session.execute(
            select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
        )
    ).scalar() or 0
    peers_total = (await session.execute(select(func.count(Peer.id)))).scalar() or 0
    peers_active = (
        await session.execute(
            select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0
    invites_total = (
        await session.execute(select(func.count(Invite.id)))
    ).scalar() or 0
    invites_pending = (
        await session.execute(
            select(func.count(Invite.id)).where(Invite.used_at.is_(None))
        )
    ).scalar() or 0

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b>\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / <b>{servers_total}</b> всего\n"
        f"📄 Peers: <b>{peers_active}</b> активных / <b>{peers_total}</b> всего\n"
        f"🎟 Инвайтов: <b>{invites_pending}</b> непогашенных / <b>{invites_total}</b> всего"
    )
