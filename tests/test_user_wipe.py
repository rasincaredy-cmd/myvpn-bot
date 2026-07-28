"""Тесты стирания юзера (services/user_wipe.py).

Главный инвариант, который тут защищается: после стирания строка `users`
исчезает, а REVOKED-пиры и wdtt-доступы ОСТАЮТСЯ висеть с несуществующим
user_id. Это не недосмотр — в этих строках лежат ключи, по которым ретеншн
планировщика повторяет SSH-снятие, если при отзыве сервер был недоступен.
Включить `PRAGMA foreign_keys` (каскад) = снести их = оставить живой пир
удалённого юзера на VPS навсегда. Тест ловит такую «оптимизацию».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Device, Peer, PeerStatus, ServerStatus, User, WdttAccess
from bot.services import revive, user_wipe
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


class FakeSSH:
    """Асинхронный контекст-менеджер вместо SSHClient — соединения нет."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "FakeSSH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class DeadSSH:
    """SSH, который не поднимается: сервер недоступен в момент стирания."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "DeadSSH":
        raise SSHError("host unreachable")

    async def __aexit__(self, *exc) -> None:
        return None


async def _make_user(session: AsyncSession, *, tg_id: int = 555):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 2
    user.sub_max_bypass = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.2", public_key="pp",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    access = await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", uri_enc=encrypt("wdtt://1.1.1.1:56000:56001:9000:PASS1:hashX"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )
    return user, server, device, peer, access


def _patch_ssh(monkeypatch, ssh_cls=FakeSSH) -> None:
    monkeypatch.setattr(revive, "SSHClient", ssh_cls)
    monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)

    async def ok_remove_peer(ssh, *, public_key: str) -> None:
        return None

    async def ok_remove_access(ssh, *, password: str, binary: str) -> bool:
        return True

    monkeypatch.setattr(revive.amnezia, "remove_peer_on_server", ok_remove_peer)
    monkeypatch.setattr(revive.wdtt_svc, "remove_access", ok_remove_access)


class TestWipeRemovesUser:
    @pytest.mark.asyncio
    async def test_user_row_is_gone(self, session: AsyncSession, monkeypatch) -> None:
        _patch_ssh(monkeypatch)
        user, *_ = await _make_user(session)
        user_id = user.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left = (await session.execute(
            select(func.count()).select_from(User).where(User.id == user_id)
        )).scalar_one()
        assert left == 0

    @pytest.mark.asyncio
    async def test_counts_revoked_items(self, session: AsyncSession, monkeypatch) -> None:
        _patch_ssh(monkeypatch)
        user, *_ = await _make_user(session)

        res = await user_wipe.wipe_user(session, user)

        # один активный пир + один активный обход
        assert res.revoked_items == 2


class TestWipeKeepsConfigRowsForRetention:
    """Ключевой инвариант: ключи переживают юзера, иначе пир не снять."""

    @pytest.mark.asyncio
    async def test_peer_and_wdtt_rows_survive_as_revoked(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        _patch_ssh(monkeypatch)
        user, _server, _device, peer, access = await _make_user(session)
        peer_id, access_id = peer.id, access.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left_peer = (await session.execute(
            select(Peer).where(Peer.id == peer_id)
        )).scalar_one_or_none()
        left_acc = (await session.execute(
            select(WdttAccess).where(WdttAccess.id == access_id)
        )).scalar_one_or_none()

        assert left_peer is not None, "пир снесён — ретеншну нечем снять его с VPS"
        assert left_acc is not None, "wdtt-строка снесена — пароль не отозвать"
        assert left_peer.status == PeerStatus.REVOKED
        assert left_acc.status == PeerStatus.REVOKED
        # Ключи на месте — именно ими ретеншн добьёт пир на сервере.
        assert left_peer.public_key == "pp"
        assert left_acc.password_enc

    @pytest.mark.asyncio
    async def test_rows_survive_even_when_ssh_was_down(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Сервер недоступен → снять не удалось → строки тем более нужны."""
        _patch_ssh(monkeypatch, DeadSSH)
        user, _server, _device, peer, access = await _make_user(session)
        peer_id, access_id = peer.id, access.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left_peer = (await session.execute(
            select(Peer).where(Peer.id == peer_id)
        )).scalar_one_or_none()
        left_acc = (await session.execute(
            select(WdttAccess).where(WdttAccess.id == access_id)
        )).scalar_one_or_none()

        assert left_peer is not None and left_peer.status == PeerStatus.REVOKED
        assert left_acc is not None and left_acc.status == PeerStatus.REVOKED
        assert left_peer.revoked_at is not None, "без revoked_at ретеншн строку не найдёт"
        assert left_acc.revoked_at is not None

    @pytest.mark.asyncio
    async def test_device_row_survives_for_zombie_cleanup(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        _patch_ssh(monkeypatch)
        user, _server, device, _peer, _access = await _make_user(session)
        device_id = device.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left_dev = (await session.execute(
            select(Device).where(Device.id == device_id)
        )).scalar_one_or_none()
        assert left_dev is not None
        assert left_dev.status == PeerStatus.REVOKED
