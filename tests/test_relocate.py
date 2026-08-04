"""Переезд конфига на другой сервер (Этап C).

SSH замокан — проверяем оркестрацию и состояние БД:
  • кулдаун «раз в сутки на конфиг»;
  • отбор серверов-кандидатов (потолок, приватность, чужие локации);
  • сам переезд: новый пир создаётся ДО того, как старый помечен грейсом;
  • журнал: одна строка «конфиг переехал», без «выдан конфиг».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, AuditLog, Peer, PeerStatus, ServerStatus
from bot.handlers import configs
from bot.services import relocate
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


async def _user(session: AsyncSession, *, tg_id: int = 111, vip: bool = False):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    # tg_id 111 и 222 сидят в ADMIN_IDS тестового окружения (conftest), а админу
    # приватные серверы видны всегда — без сброса флага проверки приватности
    # прошли бы вхолостую.
    user.is_admin = False
    user.sub_max_devices = 5
    user.sub_max_bypass = 5
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user.is_vip = vip
    return user


async def _server(session: AsyncSession, *, name: str, location: str | None,
                  max_peers: int | None = None, private: bool = False):
    server = await repo.create_server(
        session, name=name, host="1.1.1.1", wg_port=585,
        owner_tg_id=1, status=ServerStatus.READY, location=location,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    server.max_peers = max_peers
    server.is_private = private
    await session.flush()
    return server


async def _peer(session: AsyncSession, *, server, user, device_id, ip="10.8.0.2"):
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device_id,
        label="phone", ip=ip, public_key=f"pk{server.id}-{ip}",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return peer


def _fake_create(calls: list):
    """Подмена configs._create_peer_for_user: без SSH, помнит сервер и log_issue."""
    async def fake(session, server, user, label, *, device_id=None, expires_at=None,
                   log_issue=True):
        calls.append((server.id, log_issue))
        peer = Peer(
            server_id=server.id, user_id=user.id, device_id=device_id,
            label=label, ip=f"10.8.{server.id}.99", public_key=f"new-pk{server.id}",
            private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
            expires_at=expires_at,
        )
        session.add(peer)
        await session.flush()
        return peer, f"conf-{server.id}"
    return fake


class TestCooldown:
    def test_never_moved_can_move_now(self) -> None:
        peer = Peer(moved_at=None)
        assert relocate.cooldown_left(peer, datetime.now(timezone.utc)) is None

    def test_just_moved_must_wait(self) -> None:
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=now - timedelta(hours=1))
        left = relocate.cooldown_left(peer, now)
        assert left is not None
        # Ждать примерно 23 часа — точную секунду не фиксируем.
        assert timedelta(hours=22) < left < timedelta(hours=23, minutes=1)

    def test_after_a_day_free_again(self) -> None:
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=now - timedelta(hours=25))
        assert relocate.cooldown_left(peer, now) is None

    def test_naive_datetime_from_sqlite_does_not_crash(self) -> None:
        """SQLite отдаёт время без таймзоны — вычитание aware-naive упало бы
        TypeError'ом (тот же капкан, что лечит utils.timefmt.as_utc)."""
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=(now - timedelta(hours=2)).replace(tzinfo=None))
        assert relocate.cooldown_left(peer, now) is not None


class TestVisiblePeers:
    def test_hides_grace_and_revoked(self) -> None:
        live = Peer(status=PeerStatus.ACTIVE, grace_until=None)
        dying = Peer(status=PeerStatus.ACTIVE,
                     grace_until=datetime.now(timezone.utc) + timedelta(hours=5))
        dead = Peer(status=PeerStatus.REVOKED, grace_until=None)

        assert relocate.visible_peers([live, dying, dead]) == [live]


class TestCandidates:
    async def test_excludes_current_and_full_servers(self, session: AsyncSession) -> None:
        user = await _user(session)
        other = await _user(session, tg_id=222)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        full = await _server(session, name="nl2", location="🇳🇱 Нидерланды", max_peers=1)
        free = await _server(session, name="nl3", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await _peer(session, server=full, user=other, device_id=None, ip="10.8.1.5")

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇳🇱 Нидерланды"]] == [free.id]

    async def test_private_server_hidden_from_plain_user(self, session: AsyncSession) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды", private=True)
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert groups == {}

    async def test_private_server_offered_to_friend(self, session: AsyncSession) -> None:
        user = await _user(session, vip=True)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        priv = await _server(session, name="nl2", location="🇳🇱 Нидерланды", private=True)
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇳🇱 Нидерланды"]] == [priv.id]

    async def test_location_where_device_already_has_config_excluded(
        self, session: AsyncSession
    ) -> None:
        """Устройство держит по конфигу на локацию. Переезд в страну, где конфиг
        уже есть, дал бы там два, а в родной — ни одного."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        de1 = await _server(session, name="de1", location="🇩🇪 Германия")
        await _server(session, name="de2", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await _peer(session, server=de1, user=user, device_id=device.id, ip="10.8.2.2")

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert "🇩🇪 Германия" not in groups

    async def test_free_foreign_location_is_offered(self, session: AsyncSession) -> None:
        """А если конфига в той стране нет — механика переезда туда работает."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        de1 = await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇩🇪 Германия"]] == [de1.id]


class TestAutoTarget:
    async def test_picks_least_loaded_in_same_location(self, session: AsyncSession) -> None:
        user = await _user(session)
        other = await _user(session, tg_id=222)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        loaded = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        empty = await _server(session, name="nl3", location="🇳🇱 Нидерланды")
        await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        for i in range(3):
            await _peer(session, server=loaded, user=other, device_id=None,
                        ip=f"10.8.1.{i + 10}")

        target = await relocate.auto_target(session, peer, owner=user)

        # Своя локация, наименее загруженный. Германию не берём: устройство
        # осталось бы без конфига в Нидерландах.
        assert target is not None and target.id == empty.id

    async def test_none_when_no_other_server_in_location(self, session: AsyncSession) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        assert await relocate.auto_target(session, peer, owner=user) is None


class TestMovePeer:
    async def test_creates_new_peer_and_graces_old(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        calls: list[tuple[int, bool]] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create(calls))
        before = datetime.now(timezone.utc)
        new = await relocate.move_peer(
            session, old, target, owner=user,
            actor_tg_id=user.tg_id, reason="по просьбе юзера",
        )
        await session.commit()

        assert calls == [(target.id, False)]      # событие пишет сам переезд
        assert new.server_id == target.id
        assert new.device_id == device.id
        assert new.label == old.label
        assert new.moved_at is not None           # кулдаун поехал с новым пиром
        # Старый конфиг ОСТАЁТСЯ рабочим — просто с датой смерти.
        assert old.status == PeerStatus.ACTIVE
        assert old.grace_until is not None
        left = relocate.as_utc(old.grace_until) - before
        assert timedelta(hours=23, minutes=59) < left < timedelta(hours=24, minutes=1)

    async def test_writes_one_moved_event(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create([]))
        new = await relocate.move_peer(
            session, old, target, owner=user,
            actor_tg_id=999, actor_is_admin=True, reason="отвязал админ",
        )
        await session.commit()

        rows = list((await session.execute(select(AuditLog))).scalars())
        assert [r.action for r in rows] == [AuditAction.CONFIG_MOVED]
        row = rows[0]
        assert row.target_user_id == user.id     # User.id, не tg_id
        assert row.target_type == "peer" and row.target_id == new.id
        assert row.actor_tg_id == 999 and row.actor_is_admin is True
        # В строке видно и «откуда → куда», и почему.
        assert "🇳🇱 Нидерланды 1" in row.details
        assert "🇳🇱 Нидерланды 2" in row.details
        assert "отвязал админ" in row.details

    async def test_failed_creation_leaves_old_peer_untouched(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Порядок «сначала создать» и существует ради этого случая: сеть упала —
        юзер остался со работающим конфигом."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        async def boom(*a, **kw):
            raise SSHError("сервер лёг")

        monkeypatch.setattr(configs, "_create_peer_for_user", boom)

        with pytest.raises(SSHError):
            await relocate.move_peer(
                session, old, target, owner=user,
                actor_tg_id=user.tg_id, reason="по просьбе юзера",
            )

        assert old.status == PeerStatus.ACTIVE and old.grace_until is None


class FakeSSH:
    """Асинхронный контекст-менеджер вместо SSHClient — соединения нет."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "FakeSSH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FailingSSH(FakeSSH):
    """Коннект не поднимается — как упавший сервер."""

    async def __aenter__(self):
        raise SSHError("нет коннекта")


def _patch_ssh(monkeypatch, cls=FakeSSH) -> None:
    monkeypatch.setattr(relocate, "SSHClient", cls)
    monkeypatch.setattr(relocate.repo, "creds_from_server", lambda s: None)


class TestExpireGrace:
    async def test_peer_alive_until_grace_ends(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) + timedelta(hours=5)
        await session.flush()

        _patch_ssh(monkeypatch)
        removed: list[str] = []
        monkeypatch.setattr(
            relocate.amnezia, "remove_peer_on_server",
            lambda ssh, *, public_key: removed.append(public_key),
        )

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))

        assert done == [] and removed == []
        assert peer.status == PeerStatus.ACTIVE

    async def test_revokes_after_grace_and_logs_event(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        _patch_ssh(monkeypatch)
        removed: list[str] = []

        async def fake_remove(ssh, *, public_key):
            removed.append(public_key)

        monkeypatch.setattr(relocate.amnezia, "remove_peer_on_server", fake_remove)

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))
        await session.commit()

        assert [p.id for p in done] == [peer.id]
        assert removed == [peer.public_key]      # реально сняли с сервера
        assert peer.status == PeerStatus.REVOKED
        assert peer.revoked_at is not None       # дальше его подберёт ретеншн
        # grace_until НЕ обнуляем: по нему ревайв узнаёт переехавший конфиг.
        assert peer.grace_until is not None
        rows = list((await session.execute(select(AuditLog))).scalars())
        assert [r.action for r in rows] == [AuditAction.CONFIG_REVOKED]
        assert rows[0].actor_tg_id is None       # снял бот, не человек

    async def test_ssh_connect_failure_keeps_row_for_next_tick(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """В строке пира единственные ключи, которыми его снимают с VPS. Не
        поднялся коннект — не трогаем: повторим на следующем тике."""
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        _patch_ssh(monkeypatch, FailingSSH)

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))

        assert done == []
        assert peer.status == PeerStatus.ACTIVE and peer.grace_until is not None


class TestReviveSkipsMoved:
    async def test_moved_peer_not_resurrected(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Подписка кончилась во время грейса, юзер продлил. Переехавший конфиг
        поднимать нельзя: в приложении у юзера уже новый."""
        from bot.services import revive

        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        moved = await _peer(session, server=srv, user=user, device_id=device.id)
        normal = await _peer(session, server=srv, user=user, device_id=device.id,
                             ip="10.8.0.3")
        moved.grace_until = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.revoke_device(session, device.id)
        await session.flush()

        monkeypatch.setattr(revive, "SSHClient", FakeSSH)
        monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)

        async def fake_add(ssh, *, public_key, peer_ip):
            return None

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fake_add)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.peers_restored == 1
        assert normal.status == PeerStatus.ACTIVE
        assert moved.status == PeerStatus.REVOKED


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text: str, reply_markup=None, **kw) -> None:
        self.texts.append(text)
        self.markups.append(reply_markup)


class _FakeCall:
    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = type("_U", (), {"id": uid})()
        self.message = _FakeMessage()
        self.answers: list[str] = []
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        (self.alerts if show_alert else self.answers).append(text)


class _FakeState:
    """FSM-заглушка: карточка пира чистит состояние на входе."""

    async def clear(self) -> None:
        return None


def _cbs(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestAdminPeerCardMoveState:
    """Карточка пира: когда показывать кнопку «Переселить» и надпись о грейсе."""

    async def test_card_offers_move_when_there_is_a_free_neighbour(
        self, session: AsyncSession
    ) -> None:
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=338)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await session.flush()

        call = _FakeCall(f"adm:peer:{peer.id}", 999)
        await h.cb_admin_peer_open(call, session, _FakeState())

        assert f"adm:move:{peer.id}" in _cbs(call.message.markups[0])

    async def test_card_hides_move_when_alone_in_location(
        self, session: AsyncSession
    ) -> None:
        """Соседа нет — кнопки быть не должно: нажатие всё равно ответит
        «некуда», а админ уже потратил на неё клик."""
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=339)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await session.flush()

        call = _FakeCall(f"adm:peer:{peer.id}", 999)
        await h.cb_admin_peer_open(call, session, _FakeState())

        assert f"adm:move:{peer.id}" not in _cbs(call.message.markups[0])

    async def test_card_explains_why_moved_peer_is_still_alive(
        self, session: AsyncSession
    ) -> None:
        """Переехавший пир жив, но в карточке устройства у юзера его нет —
        админ должен видеть причину, а не гадать."""
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=340)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) + timedelta(hours=5)
        await session.flush()

        call = _FakeCall(f"adm:peer:{peer.id}", 999)
        await h.cb_admin_peer_open(call, session, _FakeState())

        assert "Переехал, работает до" in call.message.texts[0]
        # Второй раз переселять доживающий конфиг нечего — кнопки нет.
        assert f"adm:move:{peer.id}" not in _cbs(call.message.markups[0])


class TestAdminMoveHandler:
    """Кнопка «Переселить» в карточке пира на сервере."""

    async def test_ssh_failure_keeps_old_peer_and_tells_admin(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Целевой сервер не ответил: откат сессии гасит загруженные объекты, и
        хендлер обязан пережить это, а не упасть на чтении метки пира."""
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=333)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)
        await session.commit()
        # id забираем ДО вызова: хендлер откатит сессию, а откат гасит объект —
        # `old.id` после него полез бы в БД и уронил уже сам тест.
        old_id = old.id

        async def boom(*a, **kw):
            raise SSHError("сервер лёг")

        monkeypatch.setattr(configs, "_create_peer_for_user", boom)

        call = _FakeCall(f"adm:move:{old_id}", 999)
        await h.cb_admin_peer_move(call, session)

        assert call.message.texts, "админу должно прийти объяснение"
        assert "не отвечает" in call.message.texts[0]
        # Сырой текст исключения админу не показываем — он выдаёт host сервера.
        assert "сервер лёг" not in call.message.texts[0]
        fresh = await repo.get_peer(session, old_id)
        assert fresh.status == PeerStatus.ACTIVE and fresh.grace_until is None

    async def test_nowhere_to_go_answers_alert(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=334)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)
        await session.flush()

        call = _FakeCall(f"adm:move:{old.id}", 999)
        await h.cb_admin_peer_move(call, session)

        assert call.alerts and "некуда" in call.alerts[0]
        assert old.grace_until is None

    async def test_already_moved_peer_is_refused(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Второй раз переселять доживающий конфиг нельзя: он уже заменён."""
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=335)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)
        old.grace_until = datetime.now(timezone.utc) + timedelta(hours=5)
        await session.flush()

        call = _FakeCall(f"adm:move:{old.id}", 999)
        await h.cb_admin_peer_move(call, session)

        assert call.alerts and "переехал" in call.alerts[0]

    async def test_moves_and_notifies_user(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=336)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)
        await session.commit()

        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create([]))
        sent: list[tuple[int, str]] = []

        class _Bot:
            async def send_message(self, chat_id, text, **kw):
                sent.append((chat_id, text))

        monkeypatch.setattr(h, "bot", _Bot())

        async def fake_ask(chat_id, session_, peer):
            sent.append((chat_id, "формат"))

        import bot.handlers.config_delivery as cd
        monkeypatch.setattr(cd, "ask_config_format", fake_ask)

        call = _FakeCall(f"adm:move:{old.id}", 999)
        await h.cb_admin_peer_move(call, session)

        assert [c for c, _ in sent] == [user.tg_id, user.tg_id]
        assert "сменили сервер" in sent[0][1]
        fresh = await repo.get_peer(session, old.id)
        assert fresh.grace_until is not None      # старый доживает сутки
        new = [p for p in await repo.list_peers_for_device(session, device.id)
               if p.server_id == target.id]
        assert len(new) == 1

    async def test_blocked_user_does_not_undo_the_move(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Юзер заблокировал бота — переезд всё равно состоялся: он уже в БД и
        на серверах, и откатывать его из-за недоставленного сообщения нельзя."""
        from bot.handlers.servers import peers as h

        user = await _user(session, tg_id=337)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)
        await session.commit()

        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create([]))

        class _DeadBot:
            async def send_message(self, *a, **kw):
                raise RuntimeError("bot was blocked by the user")

        monkeypatch.setattr(h, "bot", _DeadBot())

        call = _FakeCall(f"adm:move:{old.id}", 999)
        await h.cb_admin_peer_move(call, session)

        fresh = await repo.get_peer(session, old.id)
        assert fresh.grace_until is not None
        assert call.message.texts and "переехал" in call.message.texts[0]
