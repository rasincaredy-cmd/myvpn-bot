from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import (
    Invite,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
    WdttAccess,
)
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


async def count_users(session: AsyncSession) -> int:
    from sqlalchemy import func
    return (await session.execute(select(func.count(User.id)))).scalar() or 0


async def list_all_users(
    session: AsyncSession, offset: int = 0, limit: int = 10
) -> list[User]:
    result = await session.execute(
        select(User).order_by(User.id).offset(offset).limit(limit)
    )
    return list(result.scalars())


async def list_all_users_for_broadcast(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.is_blocked.is_(False)).order_by(User.id)
    )
    return list(result.scalars())


async def set_user_blocked(
    session: AsyncSession, user_id: int, blocked: bool
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(is_blocked=blocked)
    )
    

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

# --- Invites ------------------------------------------------------------------

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


# --- WdttAccess (обход белых списков) -----------------------------------------

async def create_wdtt_access(
    session: AsyncSession,
    *,
    server_id: int,
    user_id: int,
    label: str,
    uri_enc: bytes,
    password_enc: bytes,
    expires_at: datetime | None,
) -> WdttAccess:
    access = WdttAccess(
        server_id=server_id,
        user_id=user_id,
        label=label,
        uri_enc=uri_enc,
        password_enc=password_enc,
        status=PeerStatus.ACTIVE,
        expires_at=expires_at,
    )
    session.add(access)
    await session.flush()
    return access


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


async def delete_wdtt_access(session: AsyncSession, access_id: int) -> None:
    access = await session.get(WdttAccess, access_id)
    if access is not None:
        await session.delete(access)
        await session.flush()
