"""Тесты маршрутизации сапорт-чата (Блок «Сапорт-чат»).

Проверяем repo-слой: маршрут «сообщение юзера ↔ сообщение админа» находится в
обе стороны, чистка по возрасту работает и не задевает свежие переписки.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.db import repo
from bot.db.models import SupportMsg


async def _mk_user(session, tg_id: int = 1001):
    return await repo.get_or_create_user(
        session, tg_id=tg_id, username="vasya", full_name="Вася"
    )


@pytest.mark.asyncio
async def test_route_found_by_admin_msg(session) -> None:
    """Реплай админа на копию вопроса находит юзера и его исходное сообщение."""
    user = await _mk_user(session)
    await repo.add_support_route(
        session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=42,
        admin_tg_id=111, admin_msg_id=500,
    )
    route = await repo.find_support_route_by_admin_msg(session, 111, 500)
    assert route is not None
    assert route.user_tg_id == user.tg_id
    assert route.user_msg_id == 42


@pytest.mark.asyncio
async def test_route_not_found_for_wrong_admin(session) -> None:
    """Маршрут привязан к конкретному админу: у другого админа тот же message_id —
    другое сообщение (id локальны для чата)."""
    user = await _mk_user(session)
    await repo.add_support_route(
        session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=42,
        admin_tg_id=111, admin_msg_id=500,
    )
    assert await repo.find_support_route_by_admin_msg(session, 222, 500) is None
    assert await repo.find_support_route_by_admin_msg(session, 111, 501) is None


@pytest.mark.asyncio
async def test_user_reply_recognized(session) -> None:
    """Реплай юзера на доставленный ему ответ поддержки распознаётся как
    продолжение переписки (обратный маршрут)."""
    user = await _mk_user(session)
    # Ответ поддержки доставлен юзеру как сообщение 77 в его чате.
    await repo.add_support_route(
        session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=77,
        admin_tg_id=111, admin_msg_id=600,
    )
    assert await repo.is_support_reply_from_user(session, user.tg_id, 77) is True
    # Реплай на постороннее сообщение / от постороннего юзера — не наш.
    assert await repo.is_support_reply_from_user(session, user.tg_id, 78) is False
    assert await repo.is_support_reply_from_user(session, 9999, 77) is False


@pytest.mark.asyncio
async def test_multi_admin_routes(session) -> None:
    """Вопрос копируется каждому админу отдельным сообщением — каждый может
    ответить со своей копии, оба маршрута ведут к одному юзеру."""
    user = await _mk_user(session)
    for admin_id, msg_id in [(111, 500), (222, 900)]:
        await repo.add_support_route(
            session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=42,
            admin_tg_id=admin_id, admin_msg_id=msg_id,
        )
    r1 = await repo.find_support_route_by_admin_msg(session, 111, 500)
    r2 = await repo.find_support_route_by_admin_msg(session, 222, 900)
    assert r1 is not None and r2 is not None
    assert r1.user_tg_id == r2.user_tg_id == user.tg_id


@pytest.mark.asyncio
async def test_purge_old_routes(session) -> None:
    """Чистка удаляет только маршруты старше порога."""
    user = await _mk_user(session)
    await repo.add_support_route(
        session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=1,
        admin_tg_id=111, admin_msg_id=10,
    )
    await repo.add_support_route(
        session, user_id=user.id, user_tg_id=user.tg_id, user_msg_id=2,
        admin_tg_id=111, admin_msg_id=20,
    )
    # Состариваем первый маршрут руками (created_at ставит БД).
    old = await repo.find_support_route_by_admin_msg(session, 111, 10)
    old.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    await session.flush()

    purged = await repo.purge_old_support_routes(session, days=30)
    assert purged == 1
    assert await repo.find_support_route_by_admin_msg(session, 111, 10) is None
    assert await repo.find_support_route_by_admin_msg(session, 111, 20) is not None
