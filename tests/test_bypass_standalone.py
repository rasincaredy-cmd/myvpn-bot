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
