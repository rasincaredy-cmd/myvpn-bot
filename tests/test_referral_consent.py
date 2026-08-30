"""Реф-ссылка и экран условий: код приглашающего обязан пережить гейт.

Живой случай 29–30.08.2026: Влад разложил свою ссылку по форумам, шесть человек
дошли до бота и завели конфиги — а «Приглашено» осталось нулём.

Причина: гейт согласия (05.08.2026) перехватывает ЛЮБОЕ сообщение нового
юзера, включая самый первый `/start ref_код`, и возвращает экран условий вместо
хендлера. До разбора реф-токена дело не доходило никогда, а после «Согласен»
токена уже нет — он остался в перехваченном сообщении.

Поэтому код едет на кнопке «Согласен», а не в FSM: storage у бота memory-only,
и рестарт между «пришёл по ссылке» и «согласился» обнулил бы привязку молча.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import CB_LEGAL
from bot.middlewares import consent as gate_mod

_TG_USER = TgUser(id=909, is_bot=False, first_name="Н")
_CHAT = Chat(id=909, type="private")


def _msg(text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=_CHAT,
        from_user=_TG_USER,
        text=text,
    )


def _cb(data: str) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id="1", from_user=_TG_USER, chat_instance="x", data=data, message=_msg("/start")
    )


# --- гейт: код доезжает до кнопки --------------------------------------------

@pytest.fixture
def gate(monkeypatch):
    """Гейт с подменённым repo и перехваченной отправкой. Отдаёт middleware и
    список кодов, с которыми собиралась клавиатура согласия."""
    from bot.config import settings

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")

    codes: list[str | None] = []

    async def _answer(self, text=None, **kw):
        return None

    monkeypatch.setattr(Message, "answer", _answer, raising=False)
    monkeypatch.setattr(CallbackQuery, "answer", _answer, raising=False)
    monkeypatch.setattr(
        gate_mod, "consent_kb", lambda ref=None: codes.append(ref) or None
    )

    async def _no_user(session, tg_id):
        return None

    monkeypatch.setattr(gate_mod.repo, "get_user_by_tg_id", _no_user)
    return gate_mod.ConsentMiddleware(), codes


async def _run(mw, event) -> bool:
    """Прогоняет событие через гейт. True — если хендлер всё-таки вызван."""
    called = False

    async def handler(e, d):
        nonlocal called
        called = True

    await mw(handler, event, {"session": object()})
    return called


@pytest.mark.asyncio
async def test_start_by_link_never_reaches_handler(gate) -> None:
    """Тот самый корень: первый /start по реф-ссылке до хендлера не доходит."""
    mw, _ = gate
    assert not await _run(mw, _msg("/start ref_vladvpn"))


@pytest.mark.asyncio
async def test_consent_button_carries_ref_code(gate) -> None:
    """Раз хендлер пропущен, код обязан уехать на кнопке «Согласен»."""
    mw, codes = gate
    await _run(mw, _msg("/start ref_vladvpn"))
    assert codes == ["vladvpn"]


@pytest.mark.asyncio
async def test_plain_start_carries_nothing(gate) -> None:
    mw, codes = gate
    await _run(mw, _msg("/start"))
    assert codes == [None]


@pytest.mark.asyncio
async def test_garbage_token_carries_nothing(gate) -> None:
    """Мусор в кнопку не пускаем: callback_data Telegram ограничен 64 байтами,
    а кириллица и пробелы в ней ломают разбор."""
    mw, codes = gate
    await _run(mw, _msg("/start ref_" + "я" * 40))
    assert codes == [None]


@pytest.mark.asyncio
async def test_gate_lets_accept_with_code_through(gate) -> None:
    """Кнопка согласия с кодом обязана проходить гейт — иначе принять нечем."""
    mw, _ = gate
    assert await _run(mw, _cb(f"{CB_LEGAL}:accept:vladvpn"))


# --- согласие: привязка происходит ------------------------------------------

@pytest.fixture
def accept(monkeypatch):
    """Обработчик «Согласен» без сети: удаление экрана и меню замолчали."""
    from bot.handlers import common, legal

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(Message, "delete", _noop, raising=False)
    monkeypatch.setattr(CallbackQuery, "answer", _noop, raising=False)
    monkeypatch.setattr(common, "send_start_screens", _noop)
    return legal.cb_accept


async def _referrer(session: AsyncSession, code: str = "vladvpn"):
    user = await repo.get_or_create_user(
        session, tg_id=1001, username="Vlad", full_name="V"
    )
    user.ref_code = code
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_accept_binds_referrer(accept, session: AsyncSession) -> None:
    """Человек пришёл по ссылке, принял условия — приглашение засчитано."""
    inviter = await _referrer(session)
    await accept(_cb(f"{CB_LEGAL}:accept:vladvpn"), session)

    guest = await repo.get_user_by_tg_id(session, _TG_USER.id)
    assert guest.referrer_id == inviter.id
    assert await repo.count_referrals(session, inviter.id) == 1


@pytest.mark.asyncio
async def test_accept_binds_by_old_numeric_link(accept, session: AsyncSession) -> None:
    """Ссылки вида ?start=ref_1 уже разосланы — они обязаны работать."""
    inviter = await _referrer(session)
    await accept(_cb(f"{CB_LEGAL}:accept:{inviter.id}"), session)

    guest = await repo.get_user_by_tg_id(session, _TG_USER.id)
    assert guest.referrer_id == inviter.id


@pytest.mark.asyncio
async def test_accept_without_code_works(accept, session: AsyncSession) -> None:
    """Обычный вход без ссылки согласие не ломает."""
    await accept(_cb(f"{CB_LEGAL}:accept"), session)

    guest = await repo.get_user_by_tg_id(session, _TG_USER.id)
    assert guest is not None and guest.referrer_id is None
    assert guest.terms_accepted_at is not None


@pytest.mark.asyncio
async def test_unknown_code_is_ignored(accept, session: AsyncSession) -> None:
    """Чужой или протухший код не мешает человеку войти."""
    await accept(_cb(f"{CB_LEGAL}:accept:nosuchcode"), session)

    guest = await repo.get_user_by_tg_id(session, _TG_USER.id)
    assert guest is not None and guest.referrer_id is None


@pytest.mark.asyncio
async def test_self_invite_is_ignored(accept, session: AsyncSession) -> None:
    """Своей же ссылкой себя не пригласить."""
    me = await repo.get_or_create_user(
        session, tg_id=_TG_USER.id, username="Self", full_name="S"
    )
    me.ref_code = "selfcode"
    await session.commit()

    await accept(_cb(f"{CB_LEGAL}:accept:selfcode"), session)
    guest = await repo.get_user_by_tg_id(session, _TG_USER.id)
    assert guest.referrer_id is None
