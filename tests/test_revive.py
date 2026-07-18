"""Тесты Блока «Ревайв»: ретеншен wdtt при отзыве + восстановление при продлении.

SSH и серверные вызовы замоканы — проверяем оркестрацию и состояние БД:
  • revoke_device теперь ХРАНИТ wdtt-строки (REVOKED), а не удаляет;
  • revive_devices_for_user оживляет пиры/обходы с теми же ключами/паролями,
    уважает лимиты подписки и не верит серверу, вернувшему чужой пароль.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services import revive
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


async def _make_user_with_device(
    session: AsyncSession, *, tg_id: int = 111, max_devices: int = 2, max_bypass: int = 2
):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = max_devices
    user.sub_max_bypass = max_bypass
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


def _patch_ssh(monkeypatch) -> None:
    monkeypatch.setattr(revive, "SSHClient", FakeSSH)
    monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)


class TestRevokeKeepsWdtt:
    async def test_revoke_device_marks_wdtt_revoked(self, session: AsyncSession) -> None:
        user, server, device, peer, access = await _make_user_with_device(session)
        await repo.revoke_device(session, device.id)
        await session.commit()
        await session.refresh(access)
        await session.refresh(peer)
        await session.refresh(device)
        assert device.status == PeerStatus.REVOKED
        assert peer.status == PeerStatus.REVOKED
        # Ключевое: wdtt-строка не удалена, а ждёт ревайва.
        assert access.status == PeerStatus.REVOKED
        assert access.revoked_at is not None


class TestRevokeAll:
    """revoke_devices_for_user — общий отзыв (планировщик + мгновенное
    отключение из админки): снимает с серверов и метит REVOKED."""

    async def test_revokes_everything_and_reports(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user, server, device, peer, access = await _make_user_with_device(session)
        _patch_ssh(monkeypatch)
        removed_peers: list[str] = []
        removed_pw: list[str] = []

        async def fake_remove_peer(ssh, *, public_key: str) -> None:
            removed_peers.append(public_key)

        async def fake_remove_access(ssh, *, password: str, binary: str) -> None:
            removed_pw.append(password)

        monkeypatch.setattr(revive.amnezia, "remove_peer_on_server", fake_remove_peer)
        monkeypatch.setattr(revive.wdtt_svc, "remove_access", fake_remove_access)

        assert await revive.revoke_devices_for_user(session, user.id) is True
        await session.commit()
        await session.refresh(device)
        await session.refresh(peer)
        await session.refresh(access)
        assert device.status == PeerStatus.REVOKED
        assert peer.status == PeerStatus.REVOKED
        assert access.status == PeerStatus.REVOKED
        assert removed_peers == ["pp"]
        assert removed_pw == ["PASS1"]

    async def test_false_when_nothing_active(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=999, username="e", full_name="E"
        )
        assert await revive.revoke_devices_for_user(session, user.id) is False

    async def test_ssh_error_still_marks_revoked(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Сервер недоступен — доступ на нём и так не работает; в БД всё равно
        REVOKED, чтобы ревайв при продлении вернул как надо."""
        user, server, device, peer, access = await _make_user_with_device(session)
        _patch_ssh(monkeypatch)

        async def boom(*a, **kw):
            raise SSHError("down")

        monkeypatch.setattr(revive.amnezia, "remove_peer_on_server", boom)
        monkeypatch.setattr(revive.wdtt_svc, "remove_access", boom)

        assert await revive.revoke_devices_for_user(session, user.id) is True
        await session.commit()
        await session.refresh(device)
        assert device.status == PeerStatus.REVOKED


class TestParseUri:
    def test_parse_ok(self) -> None:
        assert revive._parse_wdtt_uri(
            "wdtt://1.2.3.4:56000:56001:9000:SECRET:hashA,hashB"
        ) == ("56000,56001,9000", "hashA,hashB")

    def test_parse_pc_fragment(self) -> None:
        # PC-платформа дописывает #label — не должен мешать.
        assert revive._parse_wdtt_uri(
            "wdtt://1.2.3.4:56000:56001:9000:SECRET:h#Ноут"
        ) == ("56000,56001,9000", "h")

    def test_parse_garbage(self) -> None:
        assert revive._parse_wdtt_uri("https://example.com") is None


class TestRevive:
    async def test_full_revive(self, session: AsyncSession, monkeypatch) -> None:
        user, server, device, peer, access = await _make_user_with_device(session)
        await repo.revoke_device(session, device.id)
        await session.flush()

        _patch_ssh(monkeypatch)
        added_peers: list[tuple[str, str]] = []

        async def fake_add_peer(ssh, *, public_key: str, peer_ip: str) -> None:
            added_peers.append((public_key, peer_ip))

        created: list[dict] = []

        async def fake_create_access(ssh, **kw) -> dict:
            created.append(kw)
            return {"password": kw["password"], "link": "wdtt://restored", "expires_at": 0}

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fake_add_peer)
        monkeypatch.setattr(revive.wdtt_svc, "create_access", fake_create_access)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.devices_restored == 1
        assert res.peers_restored == 1
        assert res.bypass_restored == 1
        assert not res.errors
        # Пир вернулся с ТЕМИ ЖЕ ключом и IP — старый конфиг юзера жив.
        assert added_peers == [("pp", "10.8.0.2")]
        # Обход восстановлен ТЕМ ЖЕ паролем и параметрами из старой ссылки.
        assert created[0]["password"] == "PASS1"
        assert created[0]["ports"] == "56000,56001,9000"
        assert created[0]["vk_hashes"] == "hashX"

        await session.refresh(device)
        await session.refresh(peer)
        await session.refresh(access)
        assert device.status == PeerStatus.ACTIVE
        assert peer.status == PeerStatus.ACTIVE
        assert access.status == PeerStatus.ACTIVE
        assert access.revoked_at is None

    async def test_device_limit_respected(self, session: AsyncSession, monkeypatch) -> None:
        user, server, device, peer, access = await _make_user_with_device(
            session, max_devices=1
        )
        device2 = await repo.create_device(session, user_id=user.id, label="tablet")
        peer2 = Peer(
            server_id=server.id, user_id=user.id, device_id=device2.id,
            label="tablet", ip="10.8.0.3", public_key="pp2",
            private_key_enc=encrypt("priv2"), status=PeerStatus.ACTIVE,
        )
        session.add(peer2)
        await session.flush()
        await repo.revoke_device(session, device.id)
        await repo.revoke_device(session, device2.id)
        await session.flush()

        _patch_ssh(monkeypatch)

        async def fake_add_peer(ssh, **kw) -> None:
            pass

        async def fake_create_access(ssh, **kw) -> dict:
            return {"password": kw["password"], "link": "l", "expires_at": 0}

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fake_add_peer)
        monkeypatch.setattr(revive.wdtt_svc, "create_access", fake_create_access)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        # Лимит 1: старейшее устройство ожило, второе осталось ждать retention.
        assert res.devices_restored == 1
        assert res.devices_skipped_limit == 1
        await session.refresh(device)
        await session.refresh(device2)
        assert device.status == PeerStatus.ACTIVE
        assert device2.status == PeerStatus.REVOKED

    async def test_wrong_password_from_server_keeps_revoked(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Старый бинарь wdtt-сервера игнорирует -password: доступ не считаем
        восстановленным (ссылка юзера была бы мертва), лишний пароль откатываем."""
        user, server, device, peer, access = await _make_user_with_device(session)
        await repo.revoke_device(session, device.id)
        await session.flush()

        _patch_ssh(monkeypatch)
        removed: list[str] = []

        async def fake_add_peer(ssh, **kw) -> None:
            pass

        async def fake_create_access(ssh, **kw) -> dict:
            return {"password": "GENERATED_OTHER", "link": "l", "expires_at": 0}

        async def fake_remove_access(ssh, *, password: str, binary: str) -> bool:
            removed.append(password)
            return True

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fake_add_peer)
        monkeypatch.setattr(revive.wdtt_svc, "create_access", fake_create_access)
        monkeypatch.setattr(revive.wdtt_svc, "remove_access", fake_remove_access)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.bypass_restored == 0
        assert res.errors  # понятная ошибка, а не тихий провал
        assert removed == ["GENERATED_OTHER"]  # мусорный пароль снят с сервера
        await session.refresh(access)
        assert access.status == PeerStatus.REVOKED
        # Пир при этом ожил — устройство активно (VPN важнее обхода).
        await session.refresh(device)
        assert device.status == PeerStatus.ACTIVE

    async def test_ssh_error_leaves_revoked(self, session: AsyncSession, monkeypatch) -> None:
        user, server, device, peer, access = await _make_user_with_device(session)
        await repo.revoke_device(session, device.id)
        await session.flush()

        _patch_ssh(monkeypatch)

        async def fail_add_peer(ssh, **kw) -> None:
            raise SSHError("boom")

        async def fail_create_access(ssh, **kw) -> dict:
            raise SSHError("boom")

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fail_add_peer)
        monkeypatch.setattr(revive.wdtt_svc, "create_access", fail_create_access)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.devices_restored == 0
        assert len(res.errors) == 2
        await session.refresh(device)
        await session.refresh(peer)
        await session.refresh(access)
        assert device.status == PeerStatus.REVOKED
        assert peer.status == PeerStatus.REVOKED
        assert access.status == PeerStatus.REVOKED

    async def test_no_revoked_devices_noop(self, session: AsyncSession) -> None:
        user, *_ = await _make_user_with_device(session)
        res = await revive.revive_devices_for_user(session, user)
        assert not res.touched
