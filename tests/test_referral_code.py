"""Именная реферальная ссылка (20.08.2026).

Раньше ссылка была вида `?start=ref_7` — голый номер строки в базе. Влад
распространяет её на форумах, и номер там выглядит как мусор, а не как имя.
Теперь у юзера есть код: `?start=ref_vlad`.

Старые ссылки по номеру обязаны продолжать работать вечно — они уже разосланы.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services import referral


async def _user(session: AsyncSession, tg_id: int = 601, username: str | None = "Vlad"):
    return await repo.get_or_create_user(
        session, tg_id=tg_id, username=username, full_name="U"
    )


class TestNormalize:
    def test_lowercases(self) -> None:
        """Код хранится в одном регистре: человек напечатает ссылку как угодно,
        а найтись она обязана."""
        assert referral.normalize("VladVPN") == "vladvpn"

    def test_strips_at_and_spaces(self) -> None:
        """Люди копируют ник вместе с «@» — не повод отказывать."""
        assert referral.normalize("  @vlad ") == "vlad"

    @pytest.mark.parametrize("bad", [
        "ab",                 # короче трёх
        "a" * 33,             # длиннее лимита
        "влад",               # кириллица — в deep-link Telegram её нельзя
        "vlad-vpn",           # дефис Telegram в start-параметре не пропустит
        "vlad vpn",           # пробел
        "1vlad",              # начинается с цифры
        "vlad!",              # знак
        "",
    ])
    def test_rejects_bad(self, bad: str) -> None:
        assert referral.normalize(bad) is None

    @pytest.mark.parametrize("reserved", ["admin", "support", "start", "menu", "bot"])
    def test_rejects_reserved(self, reserved: str) -> None:
        """Служебные слова заняты: ссылка «?start=ref_start» читается как баг."""
        assert referral.normalize(reserved) is None


class TestAssign:
    @pytest.mark.asyncio
    async def test_takes_telegram_username(self, session: AsyncSession) -> None:
        user = await _user(session, username="Vlad")
        assert await referral.ensure_code(session, user) == "vlad"

    @pytest.mark.asyncio
    async def test_is_stable(self, session: AsyncSession) -> None:
        """Код выдаётся один раз и не меняется сам: ссылка уже на форуме."""
        user = await _user(session, username="Vlad")
        first = await referral.ensure_code(session, user)
        user.username = "VladNew"
        await session.flush()
        assert await referral.ensure_code(session, user) == first

    @pytest.mark.asyncio
    async def test_falls_back_without_username(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=602, username=None)
        code = await referral.ensure_code(session, user)
        assert referral.normalize(code) == code, f"выдан невалидный код {code!r}"

    @pytest.mark.asyncio
    async def test_unusable_username_falls_back(self, session: AsyncSession) -> None:
        """Ник из двух букв или из цифр в код не годится — берём запасной."""
        user = await _user(session, tg_id=603, username="ab")
        code = await referral.ensure_code(session, user)
        assert referral.normalize(code) == code

    @pytest.mark.asyncio
    async def test_collision_gets_a_suffix(self, session: AsyncSession) -> None:
        first = await _user(session, tg_id=604, username="Vlad")
        await referral.ensure_code(session, first)
        await session.flush()
        second = await _user(session, tg_id=605, username="vlad")
        code = await referral.ensure_code(session, second)
        assert code != "vlad"
        assert referral.normalize(code) == code


class TestSetCustom:
    @pytest.mark.asyncio
    async def test_user_can_choose(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=606)
        assert await referral.set_code(session, user, "MoschataVlad") == "ok"
        assert user.ref_code == "moschatavlad"

    @pytest.mark.asyncio
    async def test_taken_is_refused(self, session: AsyncSession) -> None:
        first = await _user(session, tg_id=607)
        await referral.set_code(session, first, "topvpn")
        await session.flush()
        second = await _user(session, tg_id=608)
        assert await referral.set_code(session, second, "TopVPN") == "taken"

    @pytest.mark.asyncio
    async def test_keeping_your_own_is_fine(self, session: AsyncSession) -> None:
        """Ввести свой же код — не «занято», а просто ничего не меняется."""
        user = await _user(session, tg_id=609)
        await referral.set_code(session, user, "mine123")
        assert await referral.set_code(session, user, "Mine123") == "ok"

    @pytest.mark.asyncio
    async def test_bad_code_is_refused(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=610)
        assert await referral.set_code(session, user, "не годится") == "invalid"


class TestResolve:
    @pytest.mark.asyncio
    async def test_finds_by_code(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=611, username="Vlad")
        await referral.ensure_code(session, user)
        await session.flush()
        found = await referral.resolve(session, "vlad")
        assert found is not None and found.id == user.id

    @pytest.mark.asyncio
    async def test_is_case_insensitive(self, session: AsyncSession) -> None:
        """На форуме ссылку перепишут руками и регистр не сохранят."""
        user = await _user(session, tg_id=612, username="Vlad")
        await referral.ensure_code(session, user)
        await session.flush()
        assert (await referral.resolve(session, "VLAD")).id == user.id

    @pytest.mark.asyncio
    async def test_old_numeric_links_keep_working(self, session: AsyncSession) -> None:
        """Ссылки по номеру уже разосланы — ломать их нельзя никогда."""
        user = await _user(session, tg_id=613, username="Someone")
        await session.flush()
        found = await referral.resolve(session, str(user.id))
        assert found is not None and found.id == user.id

    @pytest.mark.asyncio
    async def test_unknown_is_none(self, session: AsyncSession) -> None:
        assert await referral.resolve(session, "nobodyhere") is None
        assert await referral.resolve(session, "999999") is None
