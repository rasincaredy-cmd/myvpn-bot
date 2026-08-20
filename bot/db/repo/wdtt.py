"""Доступы обхода БС (WdttAccess): выдача, выборки, отзыв, возврат, удаление."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PeerStatus, WdttAccess


async def create_wdtt_access(
    session: AsyncSession,
    *,
    server_id: int,
    user_id: int,
    label: str,
    uri_enc: bytes,
    password_enc: bytes,
    expires_at: datetime | None,
    device_id: int | None = None,
    platform: str | None = None,
    vk_own: bool | None = None,
) -> WdttAccess:
    access = WdttAccess(
        server_id=server_id,
        user_id=user_id,
        device_id=device_id,
        label=label,
        uri_enc=uri_enc,
        password_enc=password_enc,
        status=PeerStatus.ACTIVE,
        expires_at=expires_at,
        platform=platform,
        vk_own=vk_own,
    )
    session.add(access)
    await session.flush()
    return access


async def count_active_wdtt_for_user(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(WdttAccess.id))
            .where(WdttAccess.user_id == user_id)
            .where(WdttAccess.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0


async def get_wdtt_access(session: AsyncSession, access_id: int) -> WdttAccess | None:
    return await session.get(WdttAccess, access_id)


async def list_wdtt_for_user(session: AsyncSession, user_id: int) -> list[WdttAccess]:
    result = await session.execute(
        select(WdttAccess).where(WdttAccess.user_id == user_id).order_by(WdttAccess.id)
    )
    return list(result.scalars())


async def list_wdtt_for_server(
    session: AsyncSession, server_id: int
) -> list[WdttAccess]:
    result = await session.execute(
        select(WdttAccess)
        .where(WdttAccess.server_id == server_id)
        .order_by(WdttAccess.id)
    )
    return list(result.scalars())


async def revoke_wdtt_access(session: AsyncSession, access_id: int) -> None:
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.id == access_id)
        .values(status=PeerStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )


# Раньше лежала в секции устройств, рядом с revoke_device; здесь — по сущности.
async def revive_wdtt_access(session: AsyncSession, access_id: int) -> None:
    # Пароль заново добавлен на сервер → его счётчики Up/Down стартуют с нуля;
    # сбрасываем накопитель, чтобы защита от сброса не насчитала лишнего.
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.id == access_id)
        .values(
            status=PeerStatus.ACTIVE,
            revoked_at=None,
            traffic_used_bytes=0,
            traffic_last_raw_bytes=0,
            expiry_warn_flags=0,
        )
    )


async def delete_wdtt_access(session: AsyncSession, access_id: int) -> None:
    access = await session.get(WdttAccess, access_id)
    if access is not None:
        await session.delete(access)
        await session.flush()


async def strand_wdtt_access(session: AsyncSession, access_id: int) -> None:
    """Помечает доступ, который не удалось снять с сервера, как ждущий уборки.

    Дата отзыва в прошлом — чтобы уборка планировщика взяла строку на ближайшем
    тике, а не через месяц: месяц ожидания это месяц бесплатного подключения.
    """
    from bot.services.scheduler import REVOKED_RETENTION_DAYS

    stale_ts = datetime.now(timezone.utc) - timedelta(days=REVOKED_RETENTION_DAYS + 1)
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.id == access_id)
        .values(status=PeerStatus.REVOKED, revoked_at=stale_ts)
    )
    await session.flush()
