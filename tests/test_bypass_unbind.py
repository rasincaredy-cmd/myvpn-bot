"""Отвязка резервного подключения от устройства.

Сервер обхода запоминает первое устройство, которое подключилось по ссылке, и
всем остальным отвечает отказом; приложение показывает этот отказ как «неверный
пароль». То есть смена телефона, переустановка или переход на другой клиент без
отвязки — тупик: ссылка верная, а человек уверен, что его обманули, и уходит
молча. Проверяем, что отвязка есть у юзера и у поддержки, что три исхода
(отвязали / нечего отвязывать / сервер молчит) не путаются между собой и что
падение сервера не превращается в бодрое «готово».
"""
from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, ServerStatus, WdttAccess
from bot.services.crypto import encrypt


class _FakeSSH:
    """SSH, который вместо ctl отдаёт заранее заданную строку JSON."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.commands: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def run(self, cmd, check=False, timeout=None):
        self.commands.append(cmd)

        class _Res:
            stdout = self.out
            stderr = ""
            exit_code = 0

        return _Res()


async def _access(session: AsyncSession, *, tg_id: int) -> WdttAccess:
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    await session.flush()
    return await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, label="Телефон",
        uri_enc=encrypt("wdtt://1.1.1.1:1:2:3:PASS1:hx"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )


class TestCtlCall:
    """Уровень сервиса: какая команда уходит на сервер и как читается ответ."""

    async def test_sends_unbind_with_password(self) -> None:
        from bot.services import wdtt as wdtt_svc

        ssh = _FakeSSH(json.dumps({"ok": True, "unbound": True}))
        result = await wdtt_svc.unbind_device(
            ssh, password="PASS1", binary="/usr/local/bin/wdtt-server"
        )

        assert result is True
        assert "ctl -op unbind -password PASS1" in ssh.commands[0]

    async def test_free_access_reports_no_binding(self) -> None:
        """Привязки не было: ответ без поля unbound — не ошибка, а «и так
        свободно». Юзеру про это отдельный текст, иначе он ждёт, что «теперь
        точно заработает», а причина была другая."""
        from bot.services import wdtt as wdtt_svc

        ssh = _FakeSSH(json.dumps({"ok": True}))
        assert await wdtt_svc.unbind_device(
            ssh, password="PASS1", binary="/b"
        ) is False

    async def test_unknown_password_raises(self) -> None:
        """Пароля нет на сервере — расхождение базы бота с сервером; молчать
        нельзя, иначе поддержка будет искать проблему в приложении."""
        import pytest

        from bot.services import wdtt as wdtt_svc
        from bot.services.ssh import SSHError

        ssh = _FakeSSH(json.dumps({"ok": False, "error": "password not found"}))
        with pytest.raises(SSHError):
            await wdtt_svc.unbind_device(ssh, password="NOPE", binary="/b")


class TestHandlerOutcomes:
    """Три исхода отвязки не должны выглядеть для юзера одинаково."""

    async def test_unbound_writes_audit_and_says_reconnect(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import wdtt as h
        from bot.texts import t

        access = await _access(session, tg_id=5101)
        monkeypatch.setattr(
            h, "SSHClient", lambda creds: _FakeSSH(json.dumps({"ok": True, "unbound": True}))
        )
        monkeypatch.setattr(h.repo, "creds_from_server", lambda s: None)

        was_bound = await h._unbind_access(session, access, actor_tg_id=5101)
        await session.flush()

        assert was_bound is True
        assert h._unbind_result_text(was_bound) == t.wdtt_unbound
        assert "той же ссылкой" in t.wdtt_unbound

        rows = await repo.list_audit(session, limit=10)
        assert any(
            r.action == AuditAction.WDTT_UNBOUND and r.target_id == access.id
            for r in rows
        )

    async def test_nothing_to_unbind_gets_its_own_text(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import wdtt as h
        from bot.texts import t

        access = await _access(session, tg_id=5102)
        monkeypatch.setattr(
            h, "SSHClient", lambda creds: _FakeSSH(json.dumps({"ok": True}))
        )
        monkeypatch.setattr(h.repo, "creds_from_server", lambda s: None)

        was_bound = await h._unbind_access(session, access, actor_tg_id=5102)

        assert was_bound is False
        assert h._unbind_result_text(was_bound) == t.wdtt_unbound_already

    async def test_server_silence_is_not_reported_as_success(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import wdtt as h
        from bot.services.ssh import SSHError
        from bot.texts import t

        access = await _access(session, tg_id=5103)

        def boom(creds):
            raise SSHError("нет связи")

        monkeypatch.setattr(h, "SSHClient", boom)
        monkeypatch.setattr(h.repo, "creds_from_server", lambda s: None)

        was_bound = await h._unbind_access(session, access, actor_tg_id=5103)

        assert was_bound is None
        assert h._unbind_result_text(was_bound) == t.wdtt_unbind_failed
        # Записи в журнал быть не должно: ничего не произошло.
        rows = await repo.list_audit(session, limit=10)
        assert not [r for r in rows if r.action == AuditAction.WDTT_UNBOUND]


class TestButtons:
    def test_user_card_has_the_button_while_access_lives(self) -> None:
        from bot.keyboards.inline import wdtt_user_card_kb

        data = [
            b.callback_data
            for row in wdtt_user_card_kb(7, can_get=True).inline_keyboard
            for b in row
        ]
        assert any(":myunbind:7" in d for d in data)

    def test_revoked_access_has_no_unbind_button(self) -> None:
        """У отозванного доступа отвязывать нечего — пароля на сервере уже нет,
        и кнопка вела бы в ошибку."""
        from bot.keyboards.inline import wdtt_user_card_kb

        data = [
            b.callback_data
            for row in wdtt_user_card_kb(7, can_get=False).inline_keyboard
            for b in row
        ]
        assert not any(":myunbind:" in d for d in data)

    def test_support_has_the_same_button(self) -> None:
        from bot.keyboards.inline import admin_user_bypass_card_kb

        data = [
            b.callback_data
            for row in admin_user_bypass_card_kb(7, 3, 0, is_active=True).inline_keyboard
            for b in row
        ]
        assert any(":ubpu:7:3:0" in d for d in data)

    def test_hint_speaks_the_words_the_app_shows(self) -> None:
        """Человек придёт со словами приложения, а не с нашими: подсказка обязана
        содержать именно «неверный пароль», иначе он её не свяжет со своей
        проблемой."""
        from bot.texts import t

        assert "неверный пароль" in t.wdtt_unbind_hint
