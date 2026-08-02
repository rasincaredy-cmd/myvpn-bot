"""Доставка конфига юзеру: сборка текста, права, выбор формата."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.amnezia import AmneziaParams
from bot.services.crypto import encrypt


async def _user_with_peer(session: AsyncSession, *, tg_id: int):
    """Юзер с активной подпиской, устройством и одним пиром на READY-сервере."""
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="SRVPUB", server_endpoint="1.1.1.1:585",
        # Параметры обфускации заполняет установщик; у READY-сервера они есть
        # всегда, и без них конфиг собрать нельзя — это не WireGuard.
        awg_params_json=AmneziaParams(
            Jc=5, Jmin=50, Jmax=1000, S1=50, S2=80, H1=10, H2=20, H3=30, H4=40
        ).to_json(),
    )
    server.location = "🇳🇱 Нидерланды"
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    await session.flush()
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", ip="10.8.0.2", public_key="PP",
        private_key_enc=encrypt("PRIVKEY"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return user, server, device, peer


class TestBuildConf:
    async def test_conf_has_keys_and_endpoint(self, session: AsyncSession) -> None:
        """Собранный конфиг обязан содержать приватный ключ пира, его адрес и
        endpoint сервера — без любого из трёх он не подключится."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, server, _, peer = await _user_with_peer(session, tg_id=3001)

        got = await build_conf_for_peer(session, peer)

        assert got is not None
        srv, conf = got
        assert srv.id == server.id
        assert "PRIVKEY" in conf
        assert "10.8.0.2" in conf
        assert "SRVPUB" in conf
        assert "1.1.1.1:585" in conf

    async def test_missing_server_gives_none(self, session: AsyncSession) -> None:
        """Сервер удалили, а строка пира осталась: собирать нечего, и молча
        подсунуть пустой конфиг хуже, чем честно сказать «нет»."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, _, _, peer = await _user_with_peer(session, tg_id=3002)
        peer.server_id = 999999
        await session.flush()

        assert await build_conf_for_peer(session, peer) is None
