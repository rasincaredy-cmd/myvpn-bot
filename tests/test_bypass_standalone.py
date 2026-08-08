"""Резервное подключение живёт отдельно от устройства.

Тариф продаёт устройства и резервные подключения отдельными позициями, и
«0 устройств + 1 подключение» — покупаемый тариф. Проверяем три вещи, из-за
которых по нему нельзя было получить ничего:

  • нет устройств → шаг «выбери устройство» пропускается, доступ выдаётся сам
    по себе;
  • удаление устройства подключение не уносит — оно теряет метку и остаётся;
  • отвязанное подключение не выпадает из жизненного цикла: гаснет при
    истечении подписки и оживает при продлении.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus, WdttAccess
from bot.handlers import wdtt as h
from bot.services.crypto import encrypt
from bot.states.install import WdttStates


# ------------------------------- заготовки ---------------------------------

class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.username = "u"
        self.full_name = "U"


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)

    async def answer(self, text: str, **kwargs) -> None:
        self.texts.append(text)


class _FakeCall:
    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class _FakeSSH:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


async def _fsm():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1)
    )


def _mute_wdtt_ssh(monkeypatch, *, password: str = "PASS1") -> list[dict]:
    """Сервер обхода замокан: тесты про оркестрацию, а не про ctl."""
    created: list[dict] = []

    async def fake_create(ssh, *, days, label, vk_hashes, ports, binary):
        created.append({"days": days, "label": label})
        return {"password": password, "link": f"wdtt://1.1.1.1:1:2:3:{password}:hx"}

    monkeypatch.setattr(h, "SSHClient", lambda creds: _FakeSSH())
    monkeypatch.setattr(h.repo, "creds_from_server", lambda s: None)
    monkeypatch.setattr(h.wdtt_svc, "create_access", fake_create)
    monkeypatch.setattr(h.settings, "wdtt_vk_hashes", ["hash1"])
    return created


async def _user(session: AsyncSession, *, tg_id: int, devices: int = 0):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 3
    user.sub_max_bypass = 3
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    for i in range(devices):
        await repo.create_device(session, user_id=user.id, label=f"Устройство {i + 1}")
    await session.flush()
    return user


async def _server(session: AsyncSession, *, tg_id: int = 1):
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    server.wdtt_enabled = True
    server.wdtt_ports = "1,2,3"
    await session.flush()
    return server


# ============ Задача 1: выдача без устройства ===============================

class TestIssueWithoutDevice:
    async def test_no_devices_skips_the_device_step(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Раньше здесь был тупик «Сначала создай устройство»: на тарифе с
        нулём устройств пройти его было физически нельзя."""
        _mute_wdtt_ssh(monkeypatch)
        await _user(session, tg_id=3101, devices=0)
        await _server(session)
        await session.commit()

        call, state = _FakeCall(f"{h.CB_WDTT}:new", 3101), await _fsm()
        await h.cb_wdtt_new(call, state, session)

        assert await state.get_state() == WdttStates.vk.state
        assert (await state.get_data())["device_id"] is None
        assert not any("Сначала создай устройство" in t for t in call.message.texts)

    async def test_devices_still_get_the_device_step(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Есть к чему привязать — экран выбора остаётся: метка устройства
        помогает юзеру понять, что это за подключение в списке."""
        _mute_wdtt_ssh(monkeypatch)
        await _user(session, tg_id=3102, devices=2)
        await _server(session)
        await session.commit()

        call, state = _FakeCall(f"{h.CB_WDTT}:new", 3102), await _fsm()
        await h.cb_wdtt_new(call, state, session)

        assert await state.get_state() == WdttStates.pick_device.state

    async def test_access_is_created_and_has_a_name(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Имя обязано быть непустым: уходит на сервер в ctl -label, стоит
        заголовком карточки и суффиксом ПК-ссылки."""
        created = _mute_wdtt_ssh(monkeypatch)
        user = await _user(session, tg_id=3103, devices=0)
        server = await _server(session)
        await session.commit()
        user_id = user.id

        state = await _fsm()
        await state.update_data(server_id=server.id, device_id=None, vk_hash=None)
        call = _FakeCall(f"{h.CB_WDTT}:plat:pc", 3103)
        await h.cb_wdtt_platform(call, state, session)

        rows = await repo.list_wdtt_for_user(session, user_id)
        assert len(rows) == 1
        assert rows[0].device_id is None
        assert rows[0].label == "Резервное подключение 1"
        assert created[0]["label"] == "Резервное подключение 1"

    async def test_names_do_not_collide(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Номер — наименьший свободный, а не «всего + 1»: иначе после удаления
        среднего подключения новое получило бы имя-двойник."""
        _mute_wdtt_ssh(monkeypatch)
        user = await _user(session, tg_id=3104, devices=0)
        server = await _server(session)
        await repo.create_wdtt_access(
            session, server_id=server.id, user_id=user.id, device_id=None,
            label="Резервное подключение 1", uri_enc=encrypt("wdtt://x"),
            password_enc=encrypt("P"), expires_at=None, platform="pc",
        )
        await session.commit()

        assert await h._standalone_label(session, user.id) == "Резервное подключение 2"


# ============ Задача 2: удаление устройства не уносит подключение ===========

async def _device_with_bypass(session: AsyncSession, *, tg_id: int):
    user = await _user(session, tg_id=tg_id)
    server = await _server(session, tg_id=tg_id)
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    session.add(Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", ip="10.8.0.2", public_key="pp",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    ))
    await session.flush()
    access = await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", uri_enc=encrypt("wdtt://1.1.1.1:1:2:3:PASS1:hx"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )
    await session.commit()
    return user, server, device, access


def _mute_teardown_ssh(monkeypatch) -> list[str]:
    """Возвращает список снятых с сервера паролей резервных подключений."""
    from bot.services import teardown

    removed: list[str] = []

    async def noop(*args, **kwargs) -> None:
        return None

    async def fake_remove(ssh, *, password, binary) -> None:
        removed.append(password)

    monkeypatch.setattr(teardown, "SSHClient", lambda creds: _FakeSSH())
    monkeypatch.setattr(teardown.repo, "creds_from_server", lambda s: None)
    monkeypatch.setattr(teardown.amnezia, "remove_peer_on_server", noop)
    monkeypatch.setattr(teardown.wdtt_svc, "remove_access", fake_remove)
    return removed


class TestDeleteDeviceKeepsBypass:
    async def test_bypass_survives_device_deletion(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Юзер убрал из списка старый телефон — оплаченная отдельной позицией
        тарифа строка резервного подключения уходила вместе с ним молча."""
        from bot.services import teardown

        removed = _mute_teardown_ssh(monkeypatch)
        user, _server_, device, access = await _device_with_bypass(session, tg_id=3201)
        user_id, access_id = user.id, access.id

        await teardown.delete_device(session, device, actor_tg_id=user.tg_id)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_wdtt_for_user(session, user_id)
        assert [r.id for r in rows] == [access_id], "подключение удалено вместе с устройством"
        assert rows[0].status == PeerStatus.ACTIVE
        assert rows[0].device_id is None, "метка устройства не снята — строка ссылается на удалённое"
        assert removed == [], "пароль сняли с сервера обхода, хотя подключение остаётся"

    async def test_device_and_its_peers_are_still_gone(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Устройство и его конфиги удаляются как раньше — иначе IP не
        освободится, а строка повиснет в списке."""
        from bot.services import teardown

        _mute_teardown_ssh(monkeypatch)
        user, _server_, device, _access = await _device_with_bypass(session, tg_id=3202)
        user_id, device_id = user.id, device.id

        await teardown.delete_device(session, device, actor_tg_id=user.tg_id)
        await session.commit()
        session.expunge_all()

        assert await repo.get_device(session, device_id) is None
        assert await repo.list_peers_for_user(session, user_id) == []

    async def test_user_is_told_where_the_bypass_went(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Молча оставленное подключение выглядит как пропавшее: в карточке
        устройства юзер видел его числом, а карточки больше нет."""
        from bot.handlers import devices as dev_h

        _mute_teardown_ssh(monkeypatch)
        user, _server_, device, _access = await _device_with_bypass(session, tg_id=3203)

        call = _FakeCall(f"{dev_h.CB_DEVICE}:revoke:{device.id}", user.tg_id)
        await dev_h.cb_dev_revoke(call, session)

        assert "Резервное подключение" in call.message.texts[-1]

    async def test_no_bypass_no_promise(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """У устройства без подключений про них не пишем — юзер решил бы, что
        где-то есть оплаченный доступ, которого нет."""
        from bot.handlers import devices as dev_h

        _mute_teardown_ssh(monkeypatch)
        user = await _user(session, tg_id=3204)
        device = await repo.create_device(session, user_id=user.id, label="Ноутбук")
        await session.commit()

        call = _FakeCall(f"{dev_h.CB_DEVICE}:revoke:{device.id}", user.tg_id)
        await dev_h.cb_dev_revoke(call, session)

        assert "Резервное подключение" not in call.message.texts[-1]


# ============ Задача 3: жизненный цикл отвязанного подключения ==============

def _mute_revive_ssh(monkeypatch) -> dict[str, list]:
    """Сервер обхода замокан: `ctl add -password` отдаёт тот же пароль (иначе
    ревайв считает бинарь старым и оставляет доступ отозванным)."""
    from bot.services import revive

    log: dict[str, list] = {"removed": [], "restored": []}

    async def noop(*args, **kwargs) -> None:
        return None

    async def fake_remove(ssh, *, password, binary) -> None:
        log["removed"].append(password)

    async def fake_create(ssh, *, days, label, vk_hashes, ports, binary, password=None):
        log["restored"].append(password)
        return {"password": password, "link": "wdtt://x"}

    monkeypatch.setattr(revive, "SSHClient", lambda creds: _FakeSSH())
    monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)
    monkeypatch.setattr(revive.amnezia, "remove_peer_on_server", noop)
    monkeypatch.setattr(revive.amnezia, "add_peer_on_server", noop)
    monkeypatch.setattr(revive.wdtt_svc, "remove_access", fake_remove)
    monkeypatch.setattr(revive.wdtt_svc, "create_access", fake_create)
    return log


async def _standalone_bypass(session: AsyncSession, *, tg_id: int):
    """Юзер без устройств, с одним отвязанным резервным подключением."""
    user = await _user(session, tg_id=tg_id)
    server = await _server(session, tg_id=tg_id)
    access = await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, device_id=None,
        label="Резервное подключение 1",
        uri_enc=encrypt("wdtt://1.1.1.1:1:2:3:PASS1:hx"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )
    await session.commit()
    return user, access


class TestStandaloneLifecycle:
    async def test_expiry_takes_the_bypass_down(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Отзыв ходил по устройствам — подключение без устройства он не видел
        вовсе и продолжал бы работать на сервере бесплатно."""
        from bot.services import revive

        log = _mute_revive_ssh(monkeypatch)
        user, access = await _standalone_bypass(session, tg_id=3301)
        user_id, access_id = user.id, access.id

        touched = await revive.revoke_devices_for_user(
            session, user_id, reason="истекла подписка"
        )
        await session.commit()
        session.expunge_all()

        assert touched is True, "отзыв доложил, что отзывать было нечего"
        assert log["removed"] == ["PASS1"], "пароль не снят с сервера"
        rows = await repo.list_wdtt_for_user(session, user_id)
        assert rows[0].status == PeerStatus.REVOKED
        history = [
            r for r in await repo.list_audit_for_user(session, user_id)
            if r.target_type == "wdtt" and r.target_id == access_id
        ]
        assert history, "в истории юзера нет следа, почему подключение встало"

    async def test_renewal_brings_the_bypass_back(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Ревайв тоже ходил по устройствам: юзер платил, а подключение без
        устройства оставалось отозванным навсегда."""
        from bot.services import revive

        log = _mute_revive_ssh(monkeypatch)
        user, _access = await _standalone_bypass(session, tg_id=3302)
        user_id = user.id

        await revive.revoke_devices_for_user(session, user_id)
        res = await revive.revive_devices_for_user(session, user)
        await session.commit()
        session.expunge_all()

        assert res.bypass_restored == 1
        assert log["restored"] == ["PASS1"], "восстановлен не прежний пароль"
        rows = await repo.list_wdtt_for_user(session, user_id)
        assert rows[0].status == PeerStatus.ACTIVE

    async def test_bypass_survives_a_device_limit_cut(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Подключение — отдельная позиция тарифа: урезанный лимит устройств
        его не касается, иначе юзер терял бы оплаченное из-за соседней позиции."""
        from bot.services import revive

        _mute_revive_ssh(monkeypatch)
        user, _server_, _device, _access = await _device_with_bypass(session, tg_id=3303)
        user_id = user.id
        await revive.revoke_devices_for_user(session, user_id)
        user.sub_max_devices = 0          # тариф урезан до нуля устройств
        user.sub_max_bypass = 1
        await session.commit()

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()
        session.expunge_all()

        assert res.devices_restored == 0, "устройство ожило вопреки лимиту"
        assert res.bypass_restored == 1, "оплаченное подключение не вернулось"
        rows = await repo.list_wdtt_for_user(session, user_id)
        assert rows[0].status == PeerStatus.ACTIVE

    async def test_bypass_limit_is_still_respected(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Лимит подключений считается по юзеру и после отвязки: два отозванных
        при лимите 1 возвращают только одно."""
        from bot.services import revive

        _mute_revive_ssh(monkeypatch)
        user, _access = await _standalone_bypass(session, tg_id=3304)
        server = (await repo.list_ready_servers(session))[0]
        await repo.create_wdtt_access(
            session, server_id=server.id, user_id=user.id, device_id=None,
            label="Резервное подключение 2",
            uri_enc=encrypt("wdtt://1.1.1.1:1:2:3:PASS2:hx"),
            password_enc=encrypt("PASS2"), expires_at=None, platform="pc",
        )
        await session.commit()

        await revive.revoke_devices_for_user(session, user.id)
        user.sub_max_bypass = 1
        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.bypass_restored == 1
        assert res.bypass_skipped_limit == 1
