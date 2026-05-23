"""Тесты репозиториев — поднимаем настоящую SQLite (in-memory) и гоняем CRUD."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    Invite,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
)
from bot.services.crypto import encrypt


class TestUsers:
    async def test_get_or_create_creates_new(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=999, username="alice", full_name="Alice"
        )
        assert user.id is not None
        assert user.tg_id == 999
        assert user.username == "alice"
        assert user.is_admin is False

    async def test_get_or_create_is_idempotent(self, session: AsyncSession) -> None:
        a = await repo.get_or_create_user(session, tg_id=1, username="x", full_name="X")
        b = await repo.get_or_create_user(session, tg_id=1, username="x", full_name="X")
        assert a.id == b.id

    async def test_admin_flag_set_from_settings(self, session: AsyncSession) -> None:
        # 111 в ADMIN_IDS (см. conftest.py)
        user = await repo.get_or_create_user(
            session, tg_id=111, username="adm", full_name="Admin"
        )
        assert user.is_admin is True

    async def test_username_updates_on_subsequent_calls(
        self, session: AsyncSession
    ) -> None:
        await repo.get_or_create_user(session, tg_id=42, username="old", full_name="Old")
        u = await repo.get_or_create_user(session, tg_id=42, username="new", full_name="New")
        assert u.username == "new"
        assert u.full_name == "New"


class TestServers:
    async def test_create_and_list(self, session: AsyncSession) -> None:
        s = await repo.create_server(
            session,
            name="srv1",
            host="1.2.3.4",
            ssh_user="root",
            ssh_password_enc=encrypt("hunter2"),
            wg_port=585,
            owner_tg_id=111,
            status=ServerStatus.PENDING,
        )
        assert s.id is not None

        servers = await repo.list_servers_for_owner(session, owner_tg_id=111)
        assert len(servers) == 1
        assert servers[0].name == "srv1"

    async def test_list_isolated_by_owner(self, session: AsyncSession) -> None:
        await repo.create_server(
            session, name="a", host="1.1.1.1", wg_port=585, owner_tg_id=111
        )
        await repo.create_server(
            session, name="b", host="2.2.2.2", wg_port=585, owner_tg_id=222
        )
        a_list = await repo.list_servers_for_owner(session, 111)
        b_list = await repo.list_servers_for_owner(session, 222)
        assert {s.name for s in a_list} == {"a"}
        assert {s.name for s in b_list} == {"b"}

    async def test_list_ready_only_returns_ready(self, session: AsyncSession) -> None:
        await repo.create_server(
            session, name="ready1", host="1.1.1.1", wg_port=585,
            owner_tg_id=111, status=ServerStatus.READY,
        )
        await repo.create_server(
            session, name="fail1", host="2.2.2.2", wg_port=585,
            owner_tg_id=111, status=ServerStatus.FAILED,
        )
        ready = await repo.list_ready_servers(session)
        assert [s.name for s in ready] == ["ready1"]

    async def test_set_server_status_persists_error(
        self, session: AsyncSession
    ) -> None:
        s = await repo.create_server(
            session, name="s", host="1.1.1.1", wg_port=585,
            owner_tg_id=111, status=ServerStatus.INSTALLING,
        )
        await repo.set_server_status(
            session, s.id, ServerStatus.FAILED, last_error="boom"
        )
        await session.commit()
        await session.refresh(s)
        assert s.status == ServerStatus.FAILED
        assert s.last_error == "boom"


class TestPeers:
    async def test_create_list_revoke(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=111, username="me", full_name="Me"
        )
        server = await repo.create_server(
            session, name="s", host="1.1.1.1", wg_port=585,
            owner_tg_id=111, status=ServerStatus.READY,
            server_public_key="pub", server_endpoint="1.1.1.1:585",
        )
        peer = Peer(
            server_id=server.id,
            user_id=user.id,
            label="phone",
            ip="10.8.0.2",
            public_key="pp",
            private_key_enc=encrypt("priv"),
            status=PeerStatus.ACTIVE,
        )
        session.add(peer)
        await session.flush()

        # list_peers_for_user
        peers = await repo.list_peers_for_user(session, user.id)
        assert len(peers) == 1

        # list_peers_for_server
        srv_peers = await repo.list_peers_for_server(session, server.id)
        assert len(srv_peers) == 1

        # revoke_peer
        await repo.revoke_peer(session, peer.id)
        await session.commit()
        await session.refresh(peer)
        assert peer.status == PeerStatus.REVOKED
        assert peer.revoked_at is not None


class TestInvites:
    async def test_get_invite_and_mark_used(self, session: AsyncSession) -> None:
        server = await repo.create_server(
            session, name="s", host="1.1.1.1", wg_port=585,
            owner_tg_id=111, status=ServerStatus.READY,
        )
        invite = Invite(
            token="tok-123",
            server_id=server.id,
            issued_by_tg_id=111,
            label="vasya",
        )
        session.add(invite)
        await session.flush()

        found = await repo.get_invite(session, "tok-123")
        assert found is not None
        assert found.used_at is None

        await repo.mark_invite_used(session, found, tg_id=555)
        assert found.used_by_tg_id == 555
        assert found.used_at is not None

    async def test_get_invite_returns_none_for_unknown_token(
        self, session: AsyncSession
    ) -> None:
        assert await repo.get_invite(session, "nope") is None
