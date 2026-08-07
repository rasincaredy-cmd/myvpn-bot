"""Гейт согласия перехватывает ЛЮБОЙ вход, а не только /start.

Проверка в хендлерах была дырявой: /menu, /help и любая кнопка вели в обход
экрана условий. Поэтому решение принимает middleware — через него проходит
каждое сообщение и каждое нажатие.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.middlewares.consent import needs_consent


def _user(**kw):
    """Заглушка юзера: настоящая модель тянет БД, а решение принимается
    по трём полям."""

    class _U:
        terms_accepted_at = None
        created_at = datetime.now(timezone.utc)
        is_admin = False

    u = _U()
    for k, v in kw.items():
        setattr(u, k, v)
    return u


@pytest.fixture(autouse=True)
def _urls(monkeypatch):
    """Гейт включается только при заданных ссылках на документы."""
    from bot.config import settings

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")


def test_new_user_needs_consent() -> None:
    assert needs_consent(_user())


def test_accepted_user_passes() -> None:
    assert not needs_consent(_user(terms_accepted_at=datetime.now(timezone.utc)))


def test_old_user_not_bothered() -> None:
    """Действующих юзеров гейт не трогает: они пришли до появления требования."""
    old = datetime.now(timezone.utc) - timedelta(days=365)
    assert not needs_consent(_user(created_at=old))


def test_unknown_user_needs_consent() -> None:
    """Юзера ещё нет в БД (первый /menu вместо /start) — это точно новый."""
    assert needs_consent(None)


def test_gate_off_without_documents(monkeypatch) -> None:
    """Без ссылок гейт выключен: юзер застрял бы на экране, где нечего читать."""
    from bot.config import settings

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")
    assert not needs_consent(_user())


def test_admin_not_gated() -> None:
    """Админ — это Влад: гейт на нём заблокировал бы управление сервисом."""
    assert not needs_consent(_user(is_admin=True))


# --- сам middleware ----------------------------------------------------------
# Дыра была именно здесь: решение принималось верно, но до хендлера всё равно
# доходило. Поэтому проверяем не функцию, а факт «хендлер не вызван».


from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

# Настоящие типы aiogram, собранные без валидации: middleware проверяет их
# через isinstance, и на самодельных заглушках тест был бы бесполезен —
# проверено, с заглушками гейт «пропускал» всё.
_TG_USER = TgUser(id=777, is_bot=False, first_name="Т")
_CHAT = Chat(id=777, type="private")


def _msg() -> Message:
    return Message.model_construct(
        message_id=1, date=datetime.now(timezone.utc), chat=_CHAT, from_user=_TG_USER
    )


def _cb(data: str) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id="1", from_user=_TG_USER, chat_instance="x", data=data, message=_msg()
    )


@pytest.fixture
def gate(monkeypatch):
    """Middleware с подменённым repo и перехваченной отправкой: БД и сеть
    для проверки решения не нужны. shown копит показанные экраны."""
    from bot.middlewares import consent as mod

    shown: list[str] = []

    async def _answer(self, text=None, **kw):
        shown.append(text or "")

    monkeypatch.setattr(Message, "answer", _answer, raising=False)
    monkeypatch.setattr(CallbackQuery, "answer", _answer, raising=False)
    monkeypatch.setattr(mod, "consent_kb", lambda: None)

    def _with_user(user):
        async def _get(session, tg_id):
            return user

        monkeypatch.setattr(mod.repo, "get_user_by_tg_id", _get)
        return mod.ConsentMiddleware(), shown

    return _with_user


async def _run(mw, event):
    """Прогоняет событие через middleware. Возвращает True, если хендлер вызван."""
    called = False

    async def handler(e, d):
        nonlocal called
        called = True

    await mw(handler, event, {"session": object()})
    return called


@pytest.mark.asyncio
async def test_middleware_blocks_menu_command(gate) -> None:
    """Влад проверил на живом боте: через /menu можно было создать устройство."""
    mw, shown = gate(_user())
    called = await _run(mw, _msg())
    assert not called, "гейт пропустил юзера без согласия"
    assert shown, "экран условий не показан"


@pytest.mark.asyncio
async def test_middleware_blocks_any_button(gate) -> None:
    """Любая инлайн-кнопка — тоже вход в бота."""
    mw, _ = gate(_user())
    assert not await _run(mw, _cb("dev:list")), "гейт пропустил нажатие кнопки"


@pytest.mark.asyncio
async def test_middleware_lets_accept_through(gate) -> None:
    """Кнопки самого экрана согласия обязаны работать, иначе принять нечем."""
    mw, _ = gate(_user())
    for data in ("leg:accept", "leg:decline"):
        assert await _run(mw, _cb(data)), f"{data} не прошёл гейт"


@pytest.mark.asyncio
async def test_middleware_passes_accepted_user(gate) -> None:
    mw, _ = gate(_user(terms_accepted_at=datetime.now(timezone.utc)))
    assert await _run(mw, _msg()), "согласившийся юзер заблокирован"
