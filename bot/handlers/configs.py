"""Выдача peer-конфигов: своим, по инвайту, отзыв."""
from __future__ import annotations

from datetime import datetime, timezone

import asyncio
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    AuditAction,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
)
from bot.filters.admin import AdminFilter
from bot.handlers.config_delivery import ask_config_format, build_conf_for_peer
from bot.keyboards.inline import (
    back_to_menu,
    cancel_only,
    pick_server,
    to_server,
)
from bot.loader import bot
from bot.services import amnezia, amnezia_native
from bot.services.crypto import encrypt
from bot.services.ssh import SSHClient, SSHError
from bot.texts import t, ui
from bot.utils.timefmt import fmt_msk
from bot.utils.validators import is_valid_label

router = Router(name="configs")


# Блокировки на каждый сервер: сериализуют аллокацию IP, чтобы два параллельных
# создания пира (устройство юзера и redeem инвайта) не выбрали один и тот же IP.
_server_ip_locks: dict[int, asyncio.Lock] = {}


def _server_ip_lock(server_id: int) -> asyncio.Lock:
    lock = _server_ip_locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _server_ip_locks[server_id] = lock
    return lock


async def _create_peer_for_user(
    session: AsyncSession,
    server: Server,
    user: User,
    label: str,
    *,
    device_id: int | None = None,
    expires_at: "datetime | None" = None,
    log_issue: bool = True,
) -> tuple[Peer, str]:
    """Создаёт peer на сервере и в БД. Возвращает (peer, conf).

    Пир возвращается целиком, а не его поля: вызывающему нужен `peer.id`, чтобы
    предложить юзеру выбрать формат конфига — экран выбора пересобирает конфиг
    по id уже в момент нажатия кнопки.

    Критическая секция под per-server Lock: пока держим лок, читаем занятые IP
    с сервера (`awg show`), выбираем свободный и добавляем peer. Так два
    параллельных создания на один сервер не займут один IP — второй увидит
    первый уже в выводе `awg show`.
    """
    async with _server_ip_lock(server.id):
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            used = await amnezia.list_used_ips(ssh, server.wg_subnet)
            # Резервируем IP ВСЕХ пиров из БД, включая отозванных: их строка
            # остаётся в БД, а UNIQUE(server_id, ip) не даст переиспользовать IP —
            # иначе INSERT нового пира падает с ошибкой. Отозванный пир держит свой
            # IP, пока его не удалят из БД.
            for p in await repo.list_peers_for_server(session, server.id):
                used.add(p.ip)
            ip = amnezia.next_free_ip(server.wg_subnet, used)
            keys = await amnezia.generate_peer_keys(ssh)

            # Сначала пишем в БД (UniqueConstraint поймает дубль IP), и только
            # потом трогаем сервер — иначе при коллизии остался бы «сирота» на VPS.
            peer = Peer(
                server_id=server.id,
                user_id=user.id,
                device_id=device_id,
                label=label,
                ip=ip,
                public_key=keys.public_key,
                private_key_enc=encrypt(keys.private_key),
                status=PeerStatus.ACTIVE,
                expires_at=expires_at,
            )
            session.add(peer)
            await session.flush()

            await amnezia.add_peer_on_server(ssh, public_key=keys.public_key, peer_ip=ip)

    # Пишем в журнал уже ВНЕ лока: запись в БД не участвует в аллокации IP,
    # держать из-за неё сериализацию сервера незачем.
    #
    # log_issue=False зовёт переезд (services/relocate): он пишет своё событие
    # «конфиг переехал». Иначе на один переезд в истории вышли бы две строки —
    # «выдан конфиг» и «переехал», — и админ, разбирая жалобу, не отличил бы
    # переезд от выдачи нового устройства.
    if log_issue:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=user.tg_id,
            target_user_id=user.id,
            target_type="peer",
            target_id=peer.id,
            details=f"{label} на сервере «{server.name}»",
        )

    # Сервер только что читали из БД в этой же функции — None тут невозможен,
    # проверка на него была бы мёртвым кодом.
    _server, conf = await build_conf_for_peer(session, peer)  # type: ignore[misc]
    return peer, conf


async def provision_device_peers(
    session: AsyncSession, user: User, device: "object"
) -> list[tuple[Server, Peer]]:
    """Создаёт по одному WG-пиру на КАЖДОЙ READY-локации, где у устройства ещё нет
    активного пира (Блок 8: устройство = группа конфигов по странам). Если в локации
    несколько серверов — берём наименее загруженный по активным пирам (Блок
    «Распределение»); упавший сервер не хороним локацию — пробуем следующий.
    Существующие пиры не переезжают: конфиг на руках у клиента привязан к серверу.
    Приватные серверы (Блок «Ревизия») обычным юзерам не выдаются — гейт в
    list_ready_servers(for_user=...). Заполненные по `Server.max_peers` серверы
    пропускаются — потолок действует и здесь, иначе его обходил бы кто угодно
    кнопкой «добавить устройство». Возвращает [(server, peer), ...]."""
    servers = await repo.list_ready_servers(session, for_user=user)
    existing = {
        p.server_id
        for p in await repo.list_peers_for_device(session, device.id)
        if p.status == PeerStatus.ACTIVE
    }
    load = await repo.count_active_peers_by_server(session)
    made: list[tuple[Server, Peer]] = []
    for group in repo.group_by_location(servers).values():
        if any(s.id in existing for s in group):
            continue  # в этой локации у устройства уже есть конфиг
        for server in sorted(group, key=lambda s: load.get(s.id, 0)):
            if not repo.has_free_wg_slot(server, load):
                continue  # сервер упёрся в потолок — юзеру его не предлагаем
            try:
                peer, _conf = await _create_peer_for_user(
                    session, server, user, device.label,
                    device_id=device.id, expires_at=None,
                )
            except SSHError as exc:
                logger.warning("Device {} provision on server {} failed: {}", device.id, server.id, exc)
                continue
            except Exception:
                logger.exception("Device {} provision on server {} crashed", device.id, server.id)
                continue
            load[server.id] = load.get(server.id, 0) + 1
            made.append((server, peer))
            break
    return made


def _split_dns(dns: str | None) -> tuple[str, str]:
    parts = [p.strip() for p in (dns or "1.1.1.1, 1.0.0.1").split(",") if p.strip()]
    return (parts[0] if parts else "1.1.1.1"), (parts[1] if len(parts) > 1 else "")


def config_display_base(server: Server) -> str:
    """Имя конфига для юзера: локация БЕЗ номера сервера («🇳🇱 Нидерланды», а не
    «🇳🇱 Нидерланды 2») — юзеру не важно, какой именно сервер локации ему достался.
    В интерфейсе бота нумерация «Локация N» остаётся (server_labels_map).
    Фолбэк — имя сервера, если локация не задана."""
    return server.location or server.name


async def make_vpn_link(session: AsyncSession, server: Server, label: str, conf: str) -> str:
    """Строит `vpn://`-ссылку с человекочитаемым именем «Локация · метка»."""
    name = f"{config_display_base(server)} · {label}"
    d1, d2 = _split_dns(server.dns)
    return amnezia_native.build_vpn_link(
        conf=conf, name=name, host=server.host, port=server.wg_port, dns1=d1, dns2=d2,
    )


# --- Создание peer админом --------------------------------------------------

router_admin = Router(name="peer_admin")
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())


# Инвайты (одноразовые ссылки для друзей) удалены 20.08.2026 по решению
# Влада: за всё время их выдали шесть штук, последний — 9 июля, и все
# погашены. Раздача доступов теперь идёт через реферальные ссылки, которые
# ещё и приводят деньги. Таблица `invites` в боевой базе НЕ удалена — там
# лежит история, кто по какому инвайту пришёл.

router.include_router(router_admin)
