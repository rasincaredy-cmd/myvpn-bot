"""Серверы: создание, выборки (включая гейт приватных), статус установки."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Server, ServerStatus, User  # noqa: F401  (User — в аннотации)


async def create_server(session: AsyncSession, **fields: object) -> Server:
    server = Server(**fields)
    session.add(server)
    await session.flush()
    return server


async def get_server(session: AsyncSession, server_id: int) -> Server | None:
    return await session.get(Server, server_id)


async def list_servers_for_owner(session: AsyncSession, owner_tg_id: int) -> list[Server]:
    result = await session.execute(
        select(Server).where(Server.owner_tg_id == owner_tg_id).order_by(Server.id)
    )
    return list(result.scalars())


async def list_all_servers(session: AsyncSession) -> list[Server]:
    """Все серверы сервиса (Блок 8: общий пул, не «личные»). Любой админ управляет
    всеми — owner_tg_id остаётся лишь пометкой «кем установлен»."""
    # Сортировка по локациям (Блок «Мелочи»): серверы одной страны идут подряд,
    # безлокационные — в конец. Внутри локации — по id (порядок установки).
    result = await session.execute(
        select(Server).order_by(Server.location.is_(None), Server.location, Server.id)
    )
    return list(result.scalars())


async def list_ready_servers(
    session: AsyncSession, for_user: "User | None" = None
) -> list[Server]:
    """READY-серверы. for_user — гейт приватных серверов (Блок «Ревизия»):
    обычному юзеру приватные не видны и не выдаются; None (админ-контекст,
    планировщик), админу и «другу» (is_vip) — весь пул."""
    result = await session.execute(
        select(Server).where(Server.status == ServerStatus.READY).order_by(Server.id)
    )
    servers = list(result.scalars())
    if for_user is not None and not (for_user.is_admin or for_user.is_vip):
        servers = [s for s in servers if not s.is_private]
    return servers


async def set_server_status(
    session: AsyncSession,
    server_id: int,
    status: ServerStatus,
    last_error: str | None = None,
) -> None:
    await session.execute(
        update(Server)
        .where(Server.id == server_id)
        .values(status=status, last_error=last_error)
    )
