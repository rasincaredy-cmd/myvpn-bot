"""Удаление устройства не должно оставлять живой конфиг на сервере.

Найдено аудитом 20.08.2026. При удалении устройства бот снимал пиры с VPS, а
потом удалял строки из базы — но SSH-сбой удаление из базы НЕ отменял. В строке
лежит единственный ключ, которым пир снимается; после удаления строки пир
остаётся на сервере, и закрыть его больше нечем.

Это опаснее той же дыры в ретеншне, потому что удаление жмёт САМ ЮЗЕР:
1. Упирается в лимит устройств, конфиг работает.
2. Ловит момент, когда нода недоступна (перезагрузка, работы, авария).
3. Жмёт «Удалить устройство» — снятие падает, строка исчезает.
4. Лимит освободился, добавляет новое устройство.
5. Два рабочих конфига по цене одного. Повторяемо.

Сверки сервера с базой в боте нет, так что сирота остаётся навсегда и невидимо.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Device, Peer, PeerStatus, ServerStatus
from bot.services import teardown
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


class FakeSSH:
    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a) -> None:
        return None


async def _device_with_peer(session: AsyncSession, tg_id: int):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    server = await repo.create_server(
        session, name=f"s{tg_id}", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.2", public_key=f"pk{tg_id}",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return user, device, peer


def _patch(monkeypatch, remove) -> None:
    monkeypatch.setattr(teardown, "SSHClient", FakeSSH)
    monkeypatch.setattr(teardown.repo, "creds_from_server", lambda s: None)
    monkeypatch.setattr(teardown.amnezia, "remove_peer_on_server", remove)


class TestDeleteDevice:
    @pytest.mark.asyncio
    async def test_clean_removal_deletes_everything(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user, device, peer = await _device_with_peer(session, 901)

        async def ok(ssh, *, public_key):
            return None

        _patch(monkeypatch, ok)
        await teardown.delete_device(session, device)
        await session.flush()

        assert await session.get(Device, device.id) is None
        assert await repo.get_peer(session, peer.id) is None, \
            "снятый пир должен уйти из базы вместе с устройством"

    @pytest.mark.asyncio
    async def test_failed_removal_keeps_the_keys(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Ядро бага: снятие упало — ключи выбрасывать нельзя, иначе пир на
        сервере становится вечным."""
        user, device, peer = await _device_with_peer(session, 902)

        async def boom(ssh, *, public_key):
            raise SSHError("нода недоступна")

        _patch(monkeypatch, boom)
        await teardown.delete_device(session, device)
        await session.flush()

        kept = await repo.get_peer(session, peer.id)
        assert kept is not None, "ключи от живого пира удалены — снять его больше нечем"
        assert kept.status == PeerStatus.REVOKED

    @pytest.mark.asyncio
    async def test_device_still_leaves_the_list(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Юзер нажал «удалить» — устройство обязано пропасть из списка и
        освободить лимит, даже если пир пока не снят. Иначе он будет жать
        кнопку снова и снова."""
        user, device, peer = await _device_with_peer(session, 903)

        async def boom(ssh, *, public_key):
            raise SSHError("нода недоступна")

        _patch(monkeypatch, boom)
        await teardown.delete_device(session, device)
        await session.flush()

        assert await session.get(Device, device.id) is None
        assert await repo.count_active_devices(session, user.id) == 0

    @pytest.mark.asyncio
    async def test_kept_peer_is_due_for_retention_now(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Оставленный пир должен попасть под уборку СРАЗУ, а не через 30 дней.

        Обычные отозванные строки ждут месяц ради оживления при продлении. Здесь
        ждать нечего: устройства больше нет, оживлять нечего — а каждый день
        ожидания это день бесплатного VPN.
        """
        from bot.services.scheduler import REVOKED_RETENTION_DAYS

        user, device, peer = await _device_with_peer(session, 904)

        async def boom(ssh, *, public_key):
            raise SSHError("нода недоступна")

        _patch(monkeypatch, boom)
        await teardown.delete_device(session, device)
        await session.flush()

        kept = await repo.get_peer(session, peer.id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=REVOKED_RETENTION_DAYS)
        revoked_at = kept.revoked_at
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=timezone.utc)
        assert revoked_at < cutoff, "уборка подберёт пир только через месяц"

    @pytest.mark.asyncio
    async def test_kept_peer_cannot_be_revived(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Оставленный пир не должен воскреснуть при продлении подписки: юзер
        это устройство удалил, возвращать его — сюрприз и лишняя позиция в лимите.

        Держится на том, что оживление ходит по УСТРОЙСТВАМ, а устройства уже
        нет. Тест стережёт: если оживление однажды начнут делать по пирам,
        здесь станет красно.
        """
        from bot.services import revive as revive_svc

        user, device, peer = await _device_with_peer(session, 905)

        async def boom(ssh, *, public_key):
            raise SSHError("нода недоступна")

        _patch(monkeypatch, boom)
        await teardown.delete_device(session, device)
        await session.flush()

        user.sub_max_devices = 5
        user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await session.flush()

        revived = [
            d for d in await repo.list_devices_for_user(session, user.id)
            if d.status == PeerStatus.REVOKED
        ]
        assert not revived, "удалённое устройство вернулось в кандидаты на оживление"
        res = await revive_svc.revive_devices_for_user(session, user)
        assert res.devices_restored == 0


class TestPeersWithoutDevice:
    @pytest.mark.asyncio
    async def test_legacy_peer_is_untouched(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Одиночные пиры без устройства (легаси) удалением устройства не
        затрагиваются — проверка на всякий случай, чтобы правка не задела их."""
        user, device, peer = await _device_with_peer(session, 906)
        loose = Peer(
            server_id=peer.server_id, user_id=user.id, device_id=None,
            label="legacy", ip="10.8.0.9", public_key="pk-legacy",
            private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
        )
        session.add(loose)
        await session.flush()

        async def ok(ssh, *, public_key):
            return None

        _patch(monkeypatch, ok)
        await teardown.delete_device(session, device)
        await session.flush()

        still = (await session.execute(
            select(Peer).where(Peer.id == loose.id)
        )).scalar_one_or_none()
        assert still is not None and still.status == PeerStatus.ACTIVE
