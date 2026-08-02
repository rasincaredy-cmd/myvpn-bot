"""«Последний трафик» обхода БС: выводится из прироста счётчиков.

Сервер обхода времени последнего контакта не отдаёт — только накопленные
байты. Планировщик опрашивает их каждые 5 минут, и рост между опросами и есть
единственный доступный признак «им пользовались».
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import ServerStatus, WdttAccess
from bot.services.crypto import encrypt


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
        uri_enc=encrypt("wdtt://x"), password_enc=encrypt("PASS"),
        expires_at=None, platform="android",
    )


class TestLastSeen:
    async def test_growth_marks_seen(self, session: AsyncSession) -> None:
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4001)
        assert acc.last_seen_at is None

        apply_wdtt_traffic(acc, raw=1000)

        assert acc.last_seen_at is not None
        assert acc.traffic_used_bytes == 1000

    async def test_no_growth_keeps_old_time(self, session: AsyncSession) -> None:
        """Тик без прироста не должен освежать время: иначе «последний трафик»
        всегда показывал бы «только что» и не значил бы ничего."""
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4002)
        apply_wdtt_traffic(acc, raw=1000)
        was = acc.last_seen_at
        acc.last_seen_at = was - timedelta(hours=3)
        stale = acc.last_seen_at

        apply_wdtt_traffic(acc, raw=1000)   # счётчик не изменился

        assert acc.last_seen_at == stale

    async def test_counter_reset_counts_as_traffic(
        self, session: AsyncSession
    ) -> None:
        """Сервер обхода перезапустили, счётчик обнулился. Накопление это уже
        умеет пережить; время последнего трафика тоже обязано обновиться —
        байты после сброса реальны."""
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4003)
        apply_wdtt_traffic(acc, raw=5000)
        acc.last_seen_at = acc.last_seen_at - timedelta(hours=3)
        stale = acc.last_seen_at

        apply_wdtt_traffic(acc, raw=10)     # сброс: raw меньше прошлого

        assert acc.last_seen_at != stale
