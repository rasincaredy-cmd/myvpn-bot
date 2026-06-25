from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Invite, Peer, PeerStatus, Server, ServerStatus, User
from bot.services.crypto import decrypt
from bot.services.ssh import SSHCredentials


def creds_from_server(server: Server) -> SSHCredentials:
    """Распаковывает зашифрованные SSH-креды из БД в SSHCredentials."""
    return SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        password=decrypt(server.ssh_password_enc),
        private_key=decrypt(server.ssh_key_enc),
        key_passphrase=decrypt(server.ssh_key_passphrase_enc),
    )


# --- Users -----------------------------------------------------------------

async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    full_name: str | None,
) -> User:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            is_admin=tg_id in settings.admin_ids,
        )
        session.add(user)
        await session.flush()
    else:
        # Поддерживаем username/full_name в актуальном состоянии.
        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        # Админы могут добавляться/убираться через .env, синхронизируем флаг.
        is_admin = tg_id in settings.admin_ids
        if user.is_admin != is_admin:
            user.is_admin = is_admin
            changed = True
        if changed:
            await session.flush()
    return user


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Получить юзера по внутреннему id (PK), а не tg_id.
    Нужно для admin-панели: пир может принадлежать чужому юзеру (инвайт).
    """
    return await session.get(User, user_id)


# --- Servers ------------------------------------------------------------------

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


async def list_ready_servers(session: AsyncSession) -> list[Server]:
    result = await session.execute(
        select(Server).where(Server.status == ServerStatus.READY).order_by(Server.id)
    )
    return list(result.scalars())


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


# --- Peers --------------------------------------------------------------------

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
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(status=PeerStatus.ACTIVE, revoked_at=None)
    )


async def delete_peer(session: AsyncSession, peer_id: int) -> None:
    peer = await session.get(Peer, peer_id)
    if peer is not None:
        await session.delete(peer)
        await session.flush()

# --- Invites ------------------------------------------------------------------

async def get_invite(session: AsyncSession, token: str) -> Invite | None:
    return (
        await session.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()


async def mark_invite_used(session: AsyncSession, invite: Invite, tg_id: int) -> None:
    invite.used_by_tg_id = tg_id
    invite.used_at = datetime.now(timezone.utc)
    await session.flush()
