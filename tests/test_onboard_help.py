"""Секция 1c планировщика: рука помощи тем, кто забрал конфиг и не подключился.

Дырка, ради которой это сделано, видна в проде: человек добавляет устройство,
получает конфиг — и передаёт ровно ноль байт. Он уже сказал «да» и упёрся в
установку приложения, но в поддержку не пишет, а молча уходит.

Здесь проверяется ровно одно и самое дорогое: КОМУ уйдёт сообщение. Ошибка в
эту сторону — рассылка живым людям, которые ни о чём не просили, поэтому
каждое условие отбора закрыто отдельным тестом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.crypto import encrypt
from bot.services.scheduler import ONBOARD_STUCK_HOURS, find_onboard_stuck_users

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(hours=ONBOARD_STUCK_HOURS + 1)
JUST_NOW = NOW - timedelta(hours=1)


async def _stuck_user(
    session: AsyncSession,
    *,
    tg_id: int = 900,
    created_at: datetime = LONG_AGO,
    status: PeerStatus = PeerStatus.ACTIVE,
    traffic: int = 0,
):
    """Юзер, который забрал конфиг и (по умолчанию) не подключился."""
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    session.add(Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.2", public_key=f"pk{tg_id}",
        private_key_enc=encrypt("priv"), status=status,
        created_at=created_at, traffic_used_bytes=traffic,
    ))
    await session.flush()
    return user, server, device


async def _ids(session: AsyncSession) -> set[int]:
    return {u.id for u in await find_onboard_stuck_users(session, NOW)}


class TestFindsStuck:
    async def test_config_taken_a_day_ago_and_zero_traffic(
        self, session: AsyncSession
    ) -> None:
        user, _, _ = await _stuck_user(session)
        assert await _ids(session) == {user.id}


class TestSkips:
    """Кого трогать нельзя — по одному условию отбора на тест."""

    async def test_fresh_config_still_has_time(self, session: AsyncSession) -> None:
        await _stuck_user(session, created_at=JUST_NOW)
        assert await _ids(session) == set()

    async def test_user_who_actually_connected(self, session: AsyncSession) -> None:
        await _stuck_user(session, traffic=1)
        assert await _ids(session) == set()

    async def test_traffic_only_through_bypass_counts_too(
        self, session: AsyncSession
    ) -> None:
        """Трафик мог пройти только через обход — человек всё равно разобрался."""
        user, server, device = await _stuck_user(session)
        access = await repo.create_wdtt_access(
            session, server_id=server.id, user_id=user.id, device_id=device.id,
            label="phone", uri_enc=encrypt("wdtt://x"), password_enc=encrypt("P"),
            expires_at=None, platform="android",
        )
        access.traffic_used_bytes = 4096
        await session.flush()
        assert await _ids(session) == set()

    async def test_revoked_config_has_nowhere_to_connect(
        self, session: AsyncSession
    ) -> None:
        await _stuck_user(session, status=PeerStatus.REVOKED)
        assert await _ids(session) == set()

    async def test_help_already_sent_once(self, session: AsyncSession) -> None:
        user, _, _ = await _stuck_user(session)
        user.onboard_help_sent_at = NOW - timedelta(days=3)
        await session.flush()
        assert await _ids(session) == set()

    async def test_admin(self, session: AsyncSession) -> None:
        user, _, _ = await _stuck_user(session)
        user.is_admin = True
        await session.flush()
        assert await _ids(session) == set()

    async def test_staff(self, session: AsyncSession) -> None:
        user, _, _ = await _stuck_user(session)
        user.is_staff = True
        await session.flush()
        assert await _ids(session) == set()

    async def test_blocked(self, session: AsyncSession) -> None:
        user, _, _ = await _stuck_user(session)
        user.is_blocked = True
        await session.flush()
        assert await _ids(session) == set()


class TestManyUsers:
    async def test_picks_only_the_stuck_one(self, session: AsyncSession) -> None:
        stuck, _, _ = await _stuck_user(session, tg_id=901)
        await _stuck_user(session, tg_id=902, traffic=10)
        await _stuck_user(session, tg_id=903, created_at=JUST_NOW)
        assert await _ids(session) == {stuck.id}
