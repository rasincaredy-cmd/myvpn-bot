"""Тесты глобальной ловушки ошибок (handlers/errors.py).

Смысл ловушки — чтобы юзер не остался с «вечно крутящейся» кнопкой, а админ
узнал о баге. Поэтому проверяем ровно две вещи: юзеру ответили, апдейт помечен
обработанным (иначе aiogram ретраит его и спамит той же ошибкой). Отдельно —
что сама ловушка не падает, если Telegram не принял ответ: падение обработчика
ошибок означало бы полную тишину в интерфейсе.
"""
from __future__ import annotations

import pytest

from bot.handlers import errors


class FakeUser:
    def __init__(self, uid: int = 42) -> None:
        self.id = uid


class FakeCallback:
    def __init__(self, *, boom: bool = False) -> None:
        self.from_user = FakeUser()
        self.answers: list[tuple[str, bool]] = []
        self._boom = boom

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        if self._boom:
            raise RuntimeError("query is too old")
        self.answers.append((text, show_alert))


class FakeMessage:
    def __init__(self) -> None:
        self.from_user = FakeUser()
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeUpdate:
    update_id = 777

    def __init__(self, *, callback=None, message=None) -> None:
        self.callback_query = callback
        self.message = message


class FakeEvent:
    def __init__(self, update, exc: Exception) -> None:
        self.update = update
        self.exception = exc


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


class TestCallbackErrors:
    @pytest.mark.asyncio
    async def test_answers_callback_and_marks_handled(self) -> None:
        cb = FakeCallback()
        event = FakeEvent(FakeUpdate(callback=cb), ValueError("boom"))
        bot = FakeBot()

        handled = await errors.on_unhandled_error(event, bot)

        assert handled is True, "апдейт должен считаться обработанным, иначе будет ретрай"
        assert cb.answers, "юзеру не ответили — кнопка осталась бы крутиться"
        assert cb.answers[0][1] is True  # show_alert

    @pytest.mark.asyncio
    async def test_admins_get_alert_with_exception_type(self) -> None:
        cb = FakeCallback()
        event = FakeEvent(FakeUpdate(callback=cb), KeyError("user_id"))
        bot = FakeBot()

        await errors.on_unhandled_error(event, bot)

        assert bot.sent, "админам не ушёл сигнал об ошибке"
        assert "KeyError" in bot.sent[0][1]


class TestMessageErrors:
    @pytest.mark.asyncio
    async def test_replies_to_message(self) -> None:
        msg = FakeMessage()
        event = FakeEvent(FakeUpdate(message=msg), RuntimeError("boom"))

        handled = await errors.on_unhandled_error(event, FakeBot())

        assert handled is True
        assert msg.answers and "Что-то пошло не так" in msg.answers[0]


class TestTrapNeverCrashes:
    @pytest.mark.asyncio
    async def test_survives_failed_user_notification(self) -> None:
        """Telegram может отклонить ответ (протухший callback) — ловушка обязана
        дожить до алерта админам, а не упасть сама."""
        cb = FakeCallback(boom=True)
        event = FakeEvent(FakeUpdate(callback=cb), ValueError("boom"))
        bot = FakeBot()

        handled = await errors.on_unhandled_error(event, bot)

        assert handled is True
        assert bot.sent, "админы должны узнать даже когда юзеру ответить не вышло"

    @pytest.mark.asyncio
    async def test_survives_empty_update(self) -> None:
        """Апдейт без message и callback (например, my_chat_member)."""
        event = FakeEvent(FakeUpdate(), ValueError("boom"))

        handled = await errors.on_unhandled_error(event, FakeBot())

        assert handled is True
