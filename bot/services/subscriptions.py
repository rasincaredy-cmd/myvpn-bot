"""Генерация персонализированных подписок (Subscription Feed).

Поддерживает форматы:
1. JSON для Amnezia Clean и Moschata VPN Android (единый JSON с серверами AWG и профилями qWDTT).
2. Raw multiline / Base64 (список URI: awg://, qwdtt://, wdtt://, vless://).
3. HTTP-заголовки Subscription-Userinfo и Profile-Update-Interval.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.db import repo
from bot.db.models import Peer, PeerStatus, Server, ServerStatus, User, WdttAccess
from bot.services import amnezia
from bot.services.amnezia import AmneziaParams, build_peer_conf
from bot.services.crypto import decrypt
from bot.services.wdtt import link_with_host
from bot.utils.timefmt import fmt_msk


async def build_subscription_data(
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Формирует структурированные данные подписки пользователя со всеми активными конфигурациями."""
    now = datetime.now(timezone.utc)
    is_expired = bool(user.sub_expires_at and user.sub_expires_at <= now)
    is_active = (not user.is_blocked) and (not is_expired)

    used_bytes = await repo.sub_traffic_used(session, user)
    limit_bytes = user.sub_traffic_limit_bytes
    used_mb = round(used_bytes / (1024 * 1024), 2)
    limit_mb = round(limit_bytes / (1024 * 1024), 2) if limit_bytes else 0.0

    sub_name = "Moschata VPN"
    if user.is_blocked:
        status_desc = "⛔ Аккаунт заблокирован"
    elif is_expired:
        status_desc = f"⚠️ Подписка истекла {fmt_msk(user.sub_expires_at)}"
    elif user.sub_expires_at:
        status_desc = f"Действует до {fmt_msk(user.sub_expires_at)}"
    else:
        status_desc = "Бессрочная подписка"

    servers_json: list[dict[str, str]] = []
    profiles_json: list[dict[str, Any]] = []
    uris: list[str] = []

    if is_active:
        # 1. Загружаем активные AmneziaWG пиры
        peers_stmt = (
            select(Peer)
            .join(Server, Peer.server_id == Server.id)
            .where(
                Peer.user_id == user.id,
                Peer.status == PeerStatus.ACTIVE,
                Server.status == ServerStatus.READY,
            )
            .options(selectinload(Peer.server))
            .order_by(Peer.id)
        )
        peers = list((await session.execute(peers_stmt)).scalars())

        for p in peers:
            srv = p.server
            if not srv or not srv.server_public_key or not srv.server_endpoint:
                continue
            try:
                priv_key = decrypt(p.private_key_enc)
                params = (
                    AmneziaParams.from_json(srv.awg_params_json)
                    if srv.awg_params_json
                    else AmneziaParams()
                )
                conf_text = build_peer_conf(
                    peer_private_key=priv_key,
                    peer_ip=p.ip,
                    server_public_key=srv.server_public_key,
                    endpoint=srv.server_endpoint,
                    params=params,
                    dns=srv.dns,
                )
                loc_name = srv.location or srv.name
                peer_title = f"{loc_name} ({p.label})" if p.label else loc_name
                servers_json.append({
                    "name": peer_title,
                    "config": conf_text,
                })
            except Exception:
                continue

        # 2. Загружаем активные резервные подключения (qWDTT / Яндекс.Документы)
        wdtt_stmt = (
            select(WdttAccess, Server)
            .join(Server, WdttAccess.server_id == Server.id)
            .where(
                WdttAccess.user_id == user.id,
                WdttAccess.status == PeerStatus.ACTIVE,
                Server.status == ServerStatus.READY,
                Server.wdtt_enabled.is_(True),
            )
            .order_by(WdttAccess.id)
        )
        wdtt_rows = list((await session.execute(wdtt_stmt)).all())

        for acc, srv in wdtt_rows:
            if not srv:
                continue
            try:
                raw_uri = decrypt(acc.uri_enc)
                password = decrypt(acc.password_enc)
                full_uri = link_with_host(raw_uri, srv.host)

                transport = getattr(acc, "transport", "vk") or "vk"
                loc_name = srv.location or srv.name

                if "openflux://" in full_uri or transport == "yandex":
                    title = f"📄 {loc_name} Резервное подключение Яндекс ({acc.label})"
                    uris.append(full_uri)
                    profiles_json.append({
                        "name": title,
                        "type": "yandex_docs",
                        "transport": "wss",
                        "peer": "docs.yandex.ru:443",
                        "password": password,
                        "server": srv.host,
                        "uri": full_uri,
                        "trafficMb": round(acc.traffic_used_bytes / (1024 * 1024), 2),
                    })
                else:
                    # Разбираем wdtt://host:dtls:wg:tun:pass:hash#label
                    vk_hash = ""
                    if "://" in full_uri:
                        body_part = full_uri.split("://", 1)[1].partition("#")[0]
                        parts = body_part.split(":")
                        if len(parts) >= 6:
                            vk_hash = parts[5]

                    title = f"⚡ {loc_name} Резервное подключение ({acc.label})"
                    uris.append(full_uri)

                    # qWDTT-порт: по умолчанию используем RAW 56003 или DTLS 56000
                    peer_endpoint = f"{srv.host}:56003"
                    profiles_json.append({
                        "name": title,
                        "type": "qwdtt",
                        "peer": peer_endpoint,
                        "hashes": vk_hash or settings.wdtt_vk_hashes,
                        "workers": 9,
                        "port": 9000,
                        "password": password,
                        "trafficMb": round(acc.traffic_used_bytes / (1024 * 1024), 2),
                    })
            except Exception:
                continue

    return {
        "subscriptionName": sub_name,
        "description": status_desc,
        "is_active": is_active,
        "is_expired": is_expired,
        "is_blocked": user.is_blocked,
        "trafficUsedMb": used_mb,
        "trafficLimitMb": limit_mb,
        "trafficUsedBytes": used_bytes,
        "trafficLimitBytes": limit_bytes or 0,
        "expiresAt": user.sub_expires_at.isoformat() if user.sub_expires_at else None,
        "expiresTimestamp": int(user.sub_expires_at.timestamp()) if user.sub_expires_at else 0,
        "updatedAt": now.isoformat(),
        "version": 1,
        "servers": servers_json,
        "profiles": profiles_json,
        "uris": uris,
    }


def format_subscription_response(
    data: dict[str, Any],
    requested_format: str = "json",
) -> tuple[str, str, dict[str, str]]:
    """Возвращает (body, content_type, headers)."""
    headers = {
        "Subscription-Userinfo": (
            f"upload=0; download={data['trafficUsedBytes']}; "
            f"total={data['trafficLimitBytes']}; expire={data['expiresTimestamp']}"
        ),
        "Profile-Update-Interval": "6",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }

    if requested_format in ("raw", "txt", "text"):
        lines = []
        for s in data["servers"]:
            lines.append(f"# {s['name']}\n{s['config']}")
        for u in data["uris"]:
            lines.append(u)
        body = "\n\n".join(lines)
        headers["Content-Disposition"] = 'inline; filename="moschata.txt"'
        return body, "text/plain; charset=utf-8", headers

    if requested_format == "base64":
        lines = data["uris"]
        text_payload = "\n".join(lines)
        body = base64.b64encode(text_payload.encode("utf-8")).decode("ascii")
        headers["Content-Disposition"] = 'inline; filename="moschata_sub.txt"'
        return body, "text/plain; charset=utf-8", headers

    # По умолчанию JSON:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    headers["Content-Disposition"] = 'inline; filename="moschata.json"'
    return body, "application/json; charset=utf-8", headers
