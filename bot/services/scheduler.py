"""Фоновый планировщик: автоотзыв пиров по истечению срока и лимиту трафика."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from bot.db import repo
from bot.db.base import session_scope
from bot.db.models import Peer, PeerStatus, Server
from bot.loader import bot
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError


# Пороги предупреждений о скором истечении (часов до отзыва). Порядок = номер
# бита в Peer.expiry_warn_flags. v1 — фиксированные; позже можно сделать настройку.
WARN_OFFSETS_HOURS = (24, 1)


async def _notify(tg_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_id, text)
    except Exception:
        pass


def _humanize_left(delta: timedelta) -> str:
    """Грубое «сколько осталось» для текста предупреждения."""
    minutes = int(delta.total_seconds() // 60)
    if minutes >= 1440:
        return f"{minutes // 1440} дн"
    if minutes >= 60:
        return f"{minutes // 60} ч"
    return f"{max(minutes, 1)} мин"


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

        # ── 1b. Предупреждения о скором истечении ───────────────────────────
        soon = list((await session.execute(
            select(Peer)
            .where(Peer.status == PeerStatus.ACTIVE)
            .where(Peer.expires_at.isnot(None))
            .where(Peer.expires_at > now)
        )).scalars())

        for peer in soon:
            remaining = peer.expires_at - now
            # Пороги, которые уже пора слать и которые ещё не отправляли.
            fireable = [
                i for i, hours in enumerate(WARN_OFFSETS_HOURS)
                if not (peer.expiry_warn_flags & (1 << i))
                and remaining <= timedelta(hours=hours)
            ]
            if not fireable:
                continue

            user = await repo.get_user_by_id(session, peer.user_id)
            # Помечаем сработавшие пороги ВСЕГДА (даже если юзер выключил
            # предупреждения) — чтобы не копить «долги» и не слать протухшее
            # «истекает через 24ч», когда осталось 3. Само сообщение шлём только
            # при включённых предупреждениях. Одно сообщение за тик — по факту.
            for i in fireable:
                peer.expiry_warn_flags |= (1 << i)
            if user and user.expiry_warn_enabled:
                await _notify(
                    user.tg_id,
                    f"⏳ Конфиг <b>{peer.label}</b> истекает примерно через "
                    f"{_humanize_left(remaining)} и будет автоматически отозван.",
                )

        # ── 2. Учёт трафика и лимиты ────────────────────────────────────────
        # Накапливаем трафик для ВСЕХ активных пиров (не только с лимитом), чтобы
        # счётчик пережил ребут сервера и был готов, когда лимит поставят позже.
        active = list((await session.execute(
            select(Peer).where(Peer.status == PeerStatus.ACTIVE)
        )).scalars())

        if not active:
            return

        # Группируем по серверу — один SSH на сервер для получения трафика
        by_server: dict[int, list[Peer]] = {}
        for p in active:
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

            # Обновляем накопленный трафик и собираем превысивших лимит
            to_revoke: list[Peer] = []
            for peer in peers:
                ti = traffic_map.get(peer.public_key)
                if ti is None:
                    continue
                raw = ti.rx_bytes + ti.tx_bytes
                peer.traffic_used_bytes, peer.traffic_last_raw_bytes = (
                    amnezia.accumulate_traffic(
                        peer.traffic_used_bytes, peer.traffic_last_raw_bytes, raw
                    )
                )
                if (peer.traffic_limit_bytes is not None
                        and peer.traffic_used_bytes >= peer.traffic_limit_bytes):
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
                await repo.revoke_peer(session, peer.id)
                logger.info("Auto-revoked traffic-exceeded peer {} ({})", peer.id, peer.label)
                if user:
                    used  = amnezia.fmt_bytes(peer.traffic_used_bytes)
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
