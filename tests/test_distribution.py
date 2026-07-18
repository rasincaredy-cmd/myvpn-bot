"""Тесты Блока «Распределение и локации».

SSH замокан — проверяем выбор сервера и состояние БД:
  • provision_device_peers: один пир на ЛОКАЦИЮ, сервер — наименее загруженный,
    упавший сервер не хоронит локацию (пробуем следующий);
  • обход БС: заполненные сервера (wdtt_max_accesses) не предлагаются;
  • имена конфигов: локация без номера сервера, файл — без эмодзи;
  • list_known_locations: уникальные локации для кнопок.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus, WdttAccess
from bot.handlers import configs
from bot.handlers.wdtt import _wdtt_location_groups, _least_loaded
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


async def _make_user(session: AsyncSession, tg_id: int = 111):
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    user.sub_max_devices = 5
    user.sub_max_bypass = 5
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    return user


async def _make_server(
    session: AsyncSession, *, name: str, location: str | None,
    wdtt_enabled: bool = False, wdtt_max: int | None = None,
):
    server = await repo.create_server(
        session, name=name, host="1.1.1.1", wg_port=585,
        owner_tg_id=1, status=ServerStatus.READY, location=location,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
        wdtt_enabled=wdtt_enabled, wdtt_max_accesses=wdtt_max,
    )
    return server


async def _add_active_peers(session: AsyncSession, server, user, n: int) -> None:
    for i in range(n):
        session.add(Peer(
            server_id=server.id, user_id=user.id, device_id=None,
            label=f"x{server.id}-{i}", ip=f"10.8.{server.id}.{i + 2}",
            public_key=f"pk{server.id}-{i}", private_key_enc=encrypt("priv"),
            status=PeerStatus.ACTIVE,
        ))
    await session.flush()


async def _add_active_wdtt(session: AsyncSession, server, user, n: int) -> None:
    for i in range(n):
        session.add(WdttAccess(
            server_id=server.id, user_id=user.id, device_id=None,
            label=f"w{server.id}-{i}", uri_enc=encrypt("wdtt://x"),
            password_enc=encrypt("p"), status=PeerStatus.ACTIVE,
        ))
    await session.flush()


def _fake_create_peer(calls: list):
    """Подменяет configs._create_peer_for_user: без SSH, записывает выбранный сервер."""
    async def fake(session, server, user, label, *, device_id=None, expires_at=None):
        calls.append(server.id)
        return f"conf-{server.id}", "10.8.0.2", label
    return fake


class TestGroupByLocation:
    def _srv(self, sid: int, location: str | None):
        class S:  # лёгкая заглушка вместо ORM-объекта
            pass
        s = S()
        s.id, s.location = sid, location
        return s

    def test_groups_same_location(self) -> None:
        s1, s2, s3 = self._srv(1, "🇩🇪 Германия"), self._srv(2, "🇩🇪 Германия"), self._srv(3, "🇳🇱 Нидерланды")
        groups = repo.group_by_location([s1, s2, s3])
        assert [s.id for s in groups["🇩🇪 Германия"]] == [1, 2]
        assert [s.id for s in groups["🇳🇱 Нидерланды"]] == [3]

    def test_no_location_is_own_group(self) -> None:
        s1, s2 = self._srv(1, None), self._srv(2, None)
        groups = repo.group_by_location([s1, s2])
        assert set(groups) == {"#1", "#2"}  # не слиплись в одну псевдо-локацию


class TestProvisionDistribution:
    async def test_picks_least_loaded_in_location(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _make_user(session)
        other = await _make_user(session, tg_id=222)
        s1 = await _make_server(session, name="de1", location="🇩🇪 Германия")
        s2 = await _make_server(session, name="de2", location="🇩🇪 Германия")
        await _add_active_peers(session, s1, other, 2)  # s1 загружен, s2 пуст
        device = await repo.create_device(session, user_id=user.id, label="phone")

        calls: list[int] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create_peer(calls))
        made = await configs.provision_device_peers(session, user, device)

        assert calls == [s2.id]           # наименее загруженный
        assert len(made) == 1             # одна локация → один конфиг
        assert made[0][0].id == s2.id

    async def test_skips_location_where_device_has_peer(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _make_user(session)
        s1 = await _make_server(session, name="de1", location="🇩🇪 Германия")
        s2 = await _make_server(session, name="de2", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        session.add(Peer(
            server_id=s1.id, user_id=user.id, device_id=device.id,
            label="phone", ip="10.8.0.2", public_key="pk",
            private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
        ))
        await session.flush()

        calls: list[int] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create_peer(calls))
        made = await configs.provision_device_peers(session, user, device)

        assert calls == [] and made == []  # конфиг в локации уже есть — s2 не трогаем

    async def test_falls_back_to_next_server_on_ssh_error(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _make_user(session)
        s1 = await _make_server(session, name="de1", location="🇩🇪 Германия")
        s2 = await _make_server(session, name="de2", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")

        calls: list[int] = []

        async def fake(session_, server, user_, label, *, device_id=None, expires_at=None):
            calls.append(server.id)
            if server.id == s1.id:
                raise SSHError("сервер лёг")
            return f"conf-{server.id}", "10.8.0.2", label

        monkeypatch.setattr(configs, "_create_peer_for_user", fake)
        made = await configs.provision_device_peers(session, user, device)

        assert calls == [s1.id, s2.id]    # упал первый — взяли второй
        assert [srv.id for srv, _ in made] == [s2.id]


class TestWdttCapacity:
    async def test_full_server_excluded(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        s1 = await _make_server(session, name="nl1", location="🇳🇱 Нидерланды",
                                wdtt_enabled=True, wdtt_max=1)
        s2 = await _make_server(session, name="nl2", location="🇳🇱 Нидерланды",
                                wdtt_enabled=True, wdtt_max=None)  # безлимит
        await _add_active_wdtt(session, s1, user, 1)  # s1 заполнен

        groups, load, any_wdtt = await _wdtt_location_groups(session)
        assert any_wdtt
        ids = [s.id for s in groups["🇳🇱 Нидерланды"]]
        assert ids == [s2.id]

    async def test_zero_limit_closes_new_issuance(self, session: AsyncSession) -> None:
        await _make_server(session, name="nl1", location="🇳🇱 Нидерланды",
                           wdtt_enabled=True, wdtt_max=0)
        groups, _load, any_wdtt = await _wdtt_location_groups(session)
        assert any_wdtt          # сервера с обходом есть...
        assert groups == {}      # ...но слотов нет — юзеру скажем «попробуй позже»

    async def test_least_loaded_within_location(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        s1 = await _make_server(session, name="nl1", location="🇳🇱 Нидерланды",
                                wdtt_enabled=True)
        s2 = await _make_server(session, name="nl2", location="🇳🇱 Нидерланды",
                                wdtt_enabled=True)
        await _add_active_wdtt(session, s1, user, 3)
        groups, load, _ = await _wdtt_location_groups(session)
        assert _least_loaded(groups["🇳🇱 Нидерланды"], load).id == s2.id


class TestConfigNames:
    def _srv(self, location: str | None, name: str = "srv-1"):
        class S:
            pass
        s = S()
        s.location, s.name = location, name
        return s

    def test_display_base_is_location_without_index(self) -> None:
        assert configs.config_display_base(self._srv("🇳🇱 Нидерланды")) == "🇳🇱 Нидерланды"

    def test_display_base_falls_back_to_server_name(self) -> None:
        assert configs.config_display_base(self._srv(None, name="kl-1")) == "kl-1"

    def test_filename_strips_emoji(self) -> None:
        assert configs._safe_filename_base("🇳🇱 Нидерланды") == "Нидерланды"
        assert configs._safe_filename_base("🇩🇪 Германия") == "Германия"
        assert configs._safe_filename_base("🏴‍☠️💀") == "config"  # всё вырезали — фолбэк


class TestKnownLocations:
    async def test_distinct_and_sorted(self, session: AsyncSession) -> None:
        await _make_server(session, name="a", location="🇩🇪 Германия")
        await _make_server(session, name="b", location="🇩🇪 Германия")
        await _make_server(session, name="c", location="🇳🇱 Нидерланды")
        await _make_server(session, name="d", location=None)
        locs = await repo.list_known_locations(session)
        assert locs == ["🇩🇪 Германия", "🇳🇱 Нидерланды"]
