"""Хелперы, не привязанные к одной сущности: локации, загрузка серверов, SSH-креды."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Peer, PeerStatus, Server, User, WdttAccess
from bot.services.crypto import decrypt
from bot.services.ssh import SSHCredentials
from bot.utils.timefmt import as_utc


def user_sub_tier(user: "User") -> str:
    """Уровень юзера для сегментации: 'paid' | 'trial' | 'none'.
    Бессрочная (NULL) подписка считается платной/активной, триал — только с конечным сроком."""
    exp = user.sub_expires_at
    active = exp is None or as_utc(exp) > datetime.now(timezone.utc)
    if not active:
        return "none"
    if user.is_trial and exp is not None:
        return "trial"
    return "paid"


async def server_labels_map(session: AsyncSession) -> dict[int, str]:
    """id сервера → человекочитаемое имя «Локация N» (номер = порядок сервера
    в своей локации по id). Если локация не задана — имя сервера."""
    servers = list((await session.execute(select(Server).order_by(Server.id))).scalars())
    idx: dict[str, int] = {}
    result: dict[int, str] = {}
    for s in servers:
        if s.location:
            idx[s.location] = idx.get(s.location, 0) + 1
            result[s.id] = f"{s.location} {idx[s.location]}"
        else:
            result[s.id] = s.name
    return result


def group_by_location(servers: list[Server]) -> dict[str, list[Server]]:
    """Группирует сервера по локации (Блок «Распределение»). Сервер без локации —
    сам себе группа (ключ `#id`), чтобы не слипались в одну псевдо-локацию."""
    groups: dict[str, list[Server]] = {}
    for s in servers:
        groups.setdefault(s.location or f"#{s.id}", []).append(s)
    return groups


async def count_active_peers_by_server(session: AsyncSession) -> dict[int, int]:
    """id сервера → число АКТИВНЫХ пиров. Метрика загрузки для распределения
    новых устройств внутри локации."""
    rows = await session.execute(
        select(Peer.server_id, func.count())
        .where(Peer.status == PeerStatus.ACTIVE)
        .group_by(Peer.server_id)
    )
    return {sid: n for sid, n in rows.all()}


def has_free_wg_slot(server: Server, load: dict[int, int]) -> bool:
    """Есть ли на сервере место под ещё один WG-конфиг (`Server.max_peers`:
    NULL — безлимит, 0 — выдача закрыта).

    Функция чистая, а нагрузку вызывающий считает одним запросом
    (`count_active_peers_by_server`): подбор сервера идёт по списку, и запрос
    на каждый сервер дал бы N+1. Ровно так же устроена ёмкость обхода БС в
    `handlers/wdtt._wdtt_location_groups`.
    """
    if server.max_peers is None:
        return True
    return load.get(server.id, 0) < server.max_peers


async def count_active_wdtt_by_server(session: AsyncSession) -> dict[int, int]:
    """id сервера → число АКТИВНЫХ wdtt-доступов. Для ёмкости обхода
    (Server.wdtt_max_accesses) и распределения внутри локации."""
    rows = await session.execute(
        select(WdttAccess.server_id, func.count())
        .where(WdttAccess.status == PeerStatus.ACTIVE)
        .group_by(WdttAccess.server_id)
    )
    return {sid: n for sid, n in rows.all()}


async def list_known_locations(session: AsyncSession) -> list[str]:
    """Уникальные локации всех серверов — для выбора кнопками (защита от опечаток:
    «🇩🇪 Германия» и «🇩🇪  Германия» стали бы двумя разными локациями)."""
    rows = await session.execute(
        select(Server.location)
        .where(Server.location.is_not(None))
        .distinct()
        .order_by(Server.location)
    )
    return [loc for (loc,) in rows.all()]


def creds_from_server(server: Server) -> SSHCredentials:
    """Распаковывает зашифрованные SSH-креды из БД в SSHCredentials."""
    return SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        password=decrypt(server.ssh_password_enc),
        private_key=decrypt(server.ssh_key_enc),
        key_passphrase=decrypt(server.ssh_key_passphrase_enc),
    )
