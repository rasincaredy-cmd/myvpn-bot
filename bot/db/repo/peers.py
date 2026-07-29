"""Пиры (конфиги AmneziaWG): выборки, отзыв, возврат, удаление."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Peer, PeerStatus


async def list_peers_for_user(session: AsyncSession, user_id: int) -> list[Peer]:
    result = await session.execute(
        select(Peer).where(Peer.user_id == user_id).order_by(Peer.id)
    )
    return list(result.scalars())


async def list_peers_for_server(session: AsyncSession, server_id: int) -> list[Peer]:
    result = await session.execute(
        select(Peer).where(Peer.server_id == server_id).order_by(Peer.id)
    )
    return list(result.scalars())


async def get_peer(session: AsyncSession, peer_id: int) -> Peer | None:
    return await session.get(Peer, peer_id)


async def revoke_peer(session: AsyncSession, peer_id: int) -> None:
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(status=PeerStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )


async def revive_peer(session: AsyncSession, peer_id: int) -> None:
    # Пир заново добавляется на сервер → счётчик awg стартует с нуля; сбрасываем
    # накопленный трафик, чтобы прежний лимит не отозвал пира сразу же.
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(
            status=PeerStatus.ACTIVE,
            revoked_at=None,
            traffic_used_bytes=0,
            traffic_last_raw_bytes=0,
            expiry_warn_flags=0,
        )
    )


async def delete_peer(session: AsyncSession, peer_id: int) -> None:
    peer = await session.get(Peer, peer_id)
    if peer is not None:
        await session.delete(peer)
        await session.flush()
