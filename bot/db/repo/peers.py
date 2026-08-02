"""Пиры (конфиги AmneziaWG): выборки, отзыв, возврат, удаление."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditAction, Peer, PeerStatus
from bot.db.repo.audit import log_action


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


async def revoke_peer(
    session: AsyncSession,
    peer_id: int,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    details: str | None = None,
) -> None:
    """Отзывает один пир и сразу пишет событие в журнал.

    Запись живёт здесь, а не врезкой рядом с вызовом: одиночный отзыв идёт из
    двух разных мест (карточка сервера в админке и стирание юзера), сервисной
    обёртки у них общей нет — они по-разному ведут себя с SSH. Внутри примитива
    запись обойти нельзя, а забытая врезка у нового вызывающего — ровно та
    ошибка, которую в этой ветке ловили четыре раза подряд.

    Коммит — на вызывающем: событие обязано откатиться вместе с самим отзывом.
    """
    peer = await session.get(Peer, peer_id)
    if peer is None:
        return
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(status=PeerStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )
    await log_action(
        session, AuditAction.CONFIG_REVOKED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=peer.user_id,
        target_type="peer",
        target_id=peer_id,
        details=details or f"Пир «{peer.label}» отозван",
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
