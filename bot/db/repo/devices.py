"""Устройства (Блок 9): группа «пиры по локациям + обходы» под одним именем."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Device, Peer, PeerStatus, WdttAccess


async def create_device(
    session: AsyncSession, *, user_id: int, label: str
) -> Device:
    device = Device(user_id=user_id, label=label, status=PeerStatus.ACTIVE)
    session.add(device)
    await session.flush()
    return device


async def get_device(session: AsyncSession, device_id: int) -> Device | None:
    return await session.get(Device, device_id)


async def list_devices_for_user(
    session: AsyncSession, user_id: int, *, active_only: bool = False
) -> list[Device]:
    stmt = select(Device).where(Device.user_id == user_id)
    if active_only:
        stmt = stmt.where(Device.status == PeerStatus.ACTIVE)
    stmt = stmt.order_by(Device.id)
    return list((await session.execute(stmt)).scalars())


async def count_active_devices(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(Device.id))
            .where(Device.user_id == user_id)
            .where(Device.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0


async def revoke_device(session: AsyncSession, device_id: int) -> None:
    """Отзывает устройство. Пиры и доступы обхода → REVOKED: строки ждут
    возможного ревайва при продлении подписки (пир держит IP, wdtt — пароль,
    который сервер умеет восстановить через ctl add -password). Планировщик
    чистит их через retention."""
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Device).where(Device.id == device_id).values(status=PeerStatus.REVOKED)
    )
    await session.execute(
        update(Peer)
        .where(Peer.device_id == device_id)
        .where(Peer.status == PeerStatus.ACTIVE)
        .values(status=PeerStatus.REVOKED, revoked_at=now)
    )
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.device_id == device_id)
        .where(WdttAccess.status == PeerStatus.ACTIVE)
        .values(status=PeerStatus.REVOKED, revoked_at=now)
    )


async def delete_device(session: AsyncSession, device_id: int) -> None:
    """Полностью удаляет устройство из БД: его wdtt-доступы и пиры (освобождает
    их IP) + саму запись устройства. Снятие пиров с сервера по SSH — на вызывающем.
    Отозванный/удалённый девайс не оставляем мусором (в отличие от 30-дн retention
    у одиночных пиров — тут юзер явно удаляет своё устройство)."""
    await session.execute(
        delete(WdttAccess).where(WdttAccess.device_id == device_id)
    )
    await session.execute(delete(Peer).where(Peer.device_id == device_id))
    await session.execute(delete(Device).where(Device.id == device_id))


async def backfill_devices(session: AsyncSession) -> int:
    """Грандфазер (Блок 9): активные пиры без device_id заворачиваем в устройства.
    Идемпотентно — берём только device_id IS NULL. Отозванные не трогаем."""
    peers = list((await session.execute(
        select(Peer)
        .where(Peer.device_id.is_(None))
        .where(Peer.status == PeerStatus.ACTIVE)
    )).scalars())
    for p in peers:
        device = Device(user_id=p.user_id, label=p.label, status=PeerStatus.ACTIVE)
        session.add(device)
        await session.flush()
        p.device_id = device.id
    return len(peers)


async def list_peers_for_device(session: AsyncSession, device_id: int) -> list[Peer]:
    return list(
        (await session.execute(
            select(Peer).where(Peer.device_id == device_id).order_by(Peer.id)
        )).scalars()
    )


async def list_wdtt_for_device(
    session: AsyncSession, device_id: int
) -> list[WdttAccess]:
    return list(
        (await session.execute(
            select(WdttAccess)
            .where(WdttAccess.device_id == device_id)
            .order_by(WdttAccess.id)
        )).scalars()
    )
