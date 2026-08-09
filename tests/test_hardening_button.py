"""Кнопка защиты в карточке сервера."""
from bot.keyboards.inline.servers import server_card


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_has_protection_button() -> None:
    markup = server_card(1)
    assert any("Защита" in t for t in _texts(markup))


def test_protection_button_leads_to_check_not_apply() -> None:
    # Первое нажатие обязано только ПОКАЗАТЬ состояние. Применение —
    # отдельным подтверждением: случайный тык не должен трогать сервер.
    markup = server_card(1)
    datas = [d for d in _datas(markup) if d and "harden" in d]
    assert datas, "нет кнопки защиты"
    assert all("hardenrun" not in d for d in datas), (
        "из карточки нельзя сразу применять — только проверка"
    )


# --- Important-4: обработчик не должен умирать от ошибок asyncssh ----------
#
# `asyncssh.Error` наследуется напрямую от Exception, а SFTPError — от неё,
# и НЕ от OSError. Проверка первым делом кладёт сценарий на сервер через
# SFTP: кончилось место в /root или файловая система только для чтения —
# и SFTPError летел мимо перехвата `except (SSHError, OSError)`. Обработчик
# умирал с трейсбеком, а админ навсегда оставался с экраном «Проверяю...».
# То же при обрыве связи посреди `harden`, а он идёт минуты.

import asyncssh

import bot.handlers.servers.card as card
from bot.db import repo
from bot.services import hardening
from bot.services.ssh import SSHCredentials


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, reply_markup=None):
        self.texts.append(text)
        return self


class _FakeCall:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)


class _FakeServer:
    id = 1
    host = "203.0.113.9"
    ssh_port = 2222
    ssh_user = "root"
    wg_port = 51820


def _patch_server(monkeypatch) -> None:
    async def fake_get_server(_session, _server_id):
        return _FakeServer()

    monkeypatch.setattr(repo, "get_server", fake_get_server)
    monkeypatch.setattr(
        repo, "creds_from_server", lambda s: SSHCredentials(host=s.host, port=s.ssh_port)
    )


class _SSHRaising:
    """Подключение, падающее ошибкой asyncssh на входе в контекст."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, _creds):
        return self

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _SSHOk:
    def __init__(self, _creds) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_check_survives_sftp_error(monkeypatch) -> None:
    """Нет места в /root — проверка обязана сказать об этом админу, а не
    оставить его с «Проверяю...» навсегда."""
    _patch_server(monkeypatch)
    monkeypatch.setattr(
        card, "SSHClient", _SSHRaising(asyncssh.SFTPError(4, "нет места на диске"))
    )

    call = _FakeCall("servers:harden:1")
    await card.cb_server_harden(call, session=None)

    assert call.message.texts, "обработчик не ответил вовсе"
    assert "нет места на диске" in call.message.texts[-1]


async def test_harden_run_survives_connection_lost(monkeypatch) -> None:
    """Обрыв связи посреди приведения к эталону (минуты работы) — обычное
    дело; экран админа обязан завершиться сообщением, а не зависнуть."""
    _patch_server(monkeypatch)
    monkeypatch.setattr(card, "SSHClient", _SSHOk)

    async def boom(*args, **kwargs):
        raise asyncssh.ConnectionLost("связь потеряна")

    monkeypatch.setattr(hardening, "harden", boom)

    call = _FakeCall("servers:hardenrun:1")
    await card.cb_server_harden_run(call, session=None)

    assert call.message.texts, "обработчик не ответил вовсе"
    assert "связь потеряна" in call.message.texts[-1]
