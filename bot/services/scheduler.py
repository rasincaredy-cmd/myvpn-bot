"""Фоновый планировщик: автоотзыв пиров по истечению срока и лимиту трафика."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from bot.db import repo
from bot.db.base import session_scope
from bot.db.models import Peer, PeerStatus, Server
from bot.loader import bot
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError


async def _notify(tg_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_id, text)
    except Exception:
        pass


async def _run_checks() -> None:
    now = datetime.now(timezone.utc)

    async with session_scope() as session:

        # ── 1. Истечение срока ──────────────────────────────────────────────
        expired = list((await session.execute(
            select(Peer)
            .where(Peer.status == PeerStatus.ACTIVE)
            .where(Peer.expires_at.isnot(None))
            .where(Peer.expires_at <= now)
        )).scalars())

        for peer in expired:
            server = await repo.get_server(session, peer.server_id)
            user   = await repo.get_user_by_id(session, peer.user_id)
            if server:
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
                except SSHError as exc:
                    logger.warning("Expiry revoke SSH error peer {}: {}", peer.id, exc)
            await repo.revoke_peer(session, peer.id)
            logger.info("Auto-revoked expired peer {} ({})", peer.id, peer.label)
            if user:
                await _notify(
                    user.tg_id,
                    f"⏱ Конфиг <b>{peer.label}</b> истёк и был автоматически отозван.",
                )

        # ── 2. Лимит трафика ────────────────────────────────────────────────
        limited = list((await session.execute(
            select(Peer)
            .where(Peer.status == PeerStatus.ACTIVE)
            .where(Peer.traffic_limit_bytes.isnot(None))
        )).scalars())

        if not limited:
            return

        # Группируем по серверу — один SSH на сервер для получения трафика
        by_server: dict[int, list[Peer]] = {}
        for p in limited:
            by_server.setdefault(p.server_id, []).append(p)

        for server_id, peers in by_server.items():
            server = await repo.get_server(session, server_id)
            if not server:
                continue
            try:
                async with SSHClient(repo.creds_from_server(server)) as ssh:
                    traffic_list = await amnezia.get_peer_traffic(ssh)
            except SSHError as exc:
                logger.warning("Traffic check SSH error server {}: {}", server_id, exc)
                continue

            traffic_map = {ti.public_key: ti for ti in traffic_list}

            # Собираем пиры под отзыв, потом один SSH на отзыв
            to_revoke: list[Peer] = []
            for peer in peers:
                ti = traffic_map.get(peer.public_key)
                if ti and (ti.rx_bytes + ti.tx_bytes) >= peer.traffic_limit_bytes:
                    to_revoke.append(peer)

            if not to_revoke:
                continue

            try:
                async with SSHClient(repo.creds_from_server(server)) as ssh:
                    for peer in to_revoke:
                        try:
                            await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
                        except SSHError as exc:
                            logger.warning("Traffic revoke SSH error peer {}: {}", peer.id, exc)
            except SSHError as exc:
                logger.warning("Traffic revoke SSH connect error server {}: {}", server_id, exc)

            for peer in to_revoke:
                user = await repo.get_user_by_id(session, peer.user_id)
                ti = traffic_map[peer.public_key]
                await repo.revoke_peer(session, peer.id)
                logger.info("Auto-revoked traffic-exceeded peer {} ({})", peer.id, peer.label)
                if user:
                    used  = amnezia.fmt_bytes(ti.rx_bytes + ti.tx_bytes)
                    limit = amnezia.fmt_bytes(peer.traffic_limit_bytes)
                    await _notify(
                        user.tg_id,
                        f"📊 Конфиг <b>{peer.label}</b> достиг лимита трафика "
                        f"({used} из {limit}) и был автоматически отозван.",
                    )


async def run() -> None:
    """Запускать как asyncio.create_task() при старте бота."""
    logger.info("Peer limit scheduler started (interval: 5 min)")
    while True:
        await asyncio.sleep(300)
        try:
            await _run_checks()
        except Exception:
            logger.exception("Scheduler _run_checks crashed")
