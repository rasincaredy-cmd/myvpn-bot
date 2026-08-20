"""Ретеншн отозванных конфигов: не удалять строку, пока пир жив на сервере.

Правило важнее экономии строк: в строке лежат ЕДИНСТВЕННЫЕ ключи, которыми пир
можно снять с VPS. Удалив её раньше снятия, мы оставляем на сервере живой
конфиг, который больше нечем закрыть, — бесплатный VPN навсегда, находимый
только ручной сверкой.

До 20.08.2026 правило соблюдалось лишь для сбоя КОННЕКТА. Если коннект
поднимался, а снятие конкретного пира падало — ошибку писали в лог и строку всё
равно удаляли.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services import scheduler
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


class FakeSSH:
    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a) -> None:
        return None


class FailingConnect(FakeSSH):
    async def __aenter__(self):
        raise SSHError("сервер недоступен")


async def _stale_peer(session: AsyncSession, *, pk: str, tg_id: int = 801):
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    server = await repo.create_server(
        session, name=f"s{tg_id}", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    peer = Peer(
        server_id=server.id, user_id=user.id, label="phone", ip="10.8.0.2",
        public_key=pk, private_key_enc=encrypt("priv"),
        status=PeerStatus.REVOKED,
        revoked_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    session.add(peer)
    await session.flush()
    return peer


def _patch(monkeypatch, ssh_cls, remove) -> None:
    monkeypatch.setattr(scheduler, "SSHClient", ssh_cls)
    monkeypatch.setattr(scheduler.repo, "creds_from_server", lambda s: None)
    monkeypatch.setattr(scheduler.amnezia, "remove_peer_on_server", remove)


class TestPurgeStalePeers:
    @pytest.mark.asyncio
    async def test_deletes_when_removal_succeeded(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        peer = await _stale_peer(session, pk="ok1")

        async def ok(ssh, *, public_key):
            return None

        _patch(monkeypatch, FakeSSH, ok)
        assert await scheduler.purge_stale_peers(session, [peer]) == 1
        assert await repo.get_peer(session, peer.id) is None

    @pytest.mark.asyncio
    async def test_keeps_row_when_removal_failed(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Ядро бага: коннект поднялся, снятие упало — строку удалять НЕЛЬЗЯ."""
        peer = await _stale_peer(session, pk="bad1", tg_id=802)

        async def boom(ssh, *, public_key):
            raise SSHError("awg set remove упал")

        _patch(monkeypatch, FakeSSH, boom)
        assert await scheduler.purge_stale_peers(session, [peer]) == 0
        assert await repo.get_peer(session, peer.id) is not None, \
            "ключи удалены, пир остался на сервере навсегда"

    @pytest.mark.asyncio
    async def test_keeps_row_when_connect_failed(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        peer = await _stale_peer(session, pk="bad2", tg_id=803)

        async def never(ssh, *, public_key):
            raise AssertionError("снятие не должно вызываться без коннекта")

        _patch(monkeypatch, FailingConnect, never)
        assert await scheduler.purge_stale_peers(session, [peer]) == 0
        assert await repo.get_peer(session, peer.id) is not None

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_the_others(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Упавший пир не должен утаскивать за собой соседей по тому же серверу:
        иначе один битый конфиг замораживает всю уборку навсегда."""
        good = await _stale_peer(session, pk="good", tg_id=804)
        bad = Peer(
            server_id=good.server_id, user_id=good.user_id, label="second",
            ip="10.8.0.3", public_key="bad3", private_key_enc=encrypt("priv"),
            status=PeerStatus.REVOKED,
            revoked_at=datetime.now(timezone.utc) - timedelta(days=40),
        )
        session.add(bad)
        await session.flush()

        async def selective(ssh, *, public_key):
            if public_key == "bad3":
                raise SSHError("этот не снялся")

        _patch(monkeypatch, FakeSSH, selective)
        assert await scheduler.purge_stale_peers(session, [good, bad]) == 1
        assert await repo.get_peer(session, good.id) is None
        assert await repo.get_peer(session, bad.id) is not None
