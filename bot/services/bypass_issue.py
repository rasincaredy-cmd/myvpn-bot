"""Выдача резервного подключения: выбор локации и создание доступа.

Логика вынесена из хендлера 22.08.2026, когда выдавать подключение научилось и
мини-приложение. Второй экземпляр этих правил означал бы, что ёмкость сервера,
срок доступа и запись в журнал живут в двух местах и однажды разойдутся, — а
расходятся такие пары всегда в худшую сторону: доступ создан на сервере, а в
базе его нет.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction, Server, WdttAccess
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt, encrypt
from bot.services.ssh import SSHClient
from bot.utils.timefmt import as_utc


class NoCapacity(Exception):
    """Свободные места на сервере кончились, пока юзер шёл по шагам."""


async def location_groups(session: AsyncSession, user=None):
    """Локация → READY-сервера с включённым резервным подключением и СВОБОДНОЙ
    ёмкостью (`wdtt_max_accesses`; NULL — безлимит). Заполненные юзеру не
    предлагаются, приватные — только админам и «друзьям» (гейт в
    list_ready_servers). Возвращает (группы, загрузка, есть_ли_такие_серверы)."""
    servers = [
        s for s in await repo.list_ready_servers(session, for_user=user)
        if s.wdtt_enabled
    ]
    load = await repo.count_active_wdtt_by_server(session)
    free = [
        s for s in servers
        if s.wdtt_max_accesses is None or load.get(s.id, 0) < s.wdtt_max_accesses
    ]
    return repo.group_by_location(free), load, bool(servers)


async def link_for(session: AsyncSession, access) -> str:
    """Ссылка доступа с АКТУАЛЬНЫМ адресом сервера из его карточки.

    Ссылка сохраняется один раз при выдаче и после смены IP у хостера держит
    мёртвый адрес. Конфиг VPN такой болезни не знает — он каждый раз собирается
    заново из server.host; здесь делаем то же самое. Одна точка на бота,
    админку и мини-приложение: поддержка обязана видеть ровно ту ссылку, что
    ушла юзеру."""
    uri = decrypt(access.uri_enc)
    server = await repo.get_server(session, access.server_id)
    return wdtt_svc.link_with_host(uri, server.host) if server else uri


def least_loaded(group, load: dict[int, int]) -> Server:
    """Наименее загруженный сервер локации — равномерное распределение внутри неё."""
    return min(group, key=lambda s: load.get(s.id, 0))


def sub_days_left(user) -> int:
    """Дней до конца подписки для `ctl -days`; 0 = бессрочно."""
    if user.sub_expires_at is None:
        return 0
    delta = as_utc(user.sub_expires_at) - datetime.now(timezone.utc)
    return max(1, math.ceil(delta.total_seconds() / 86400))


async def standalone_label(session: AsyncSession, user_id: int) -> str:
    """Имя подключения, выданного без устройства.

    Пустым оно быть не может: уходит на сервер в `ctl add -label`, стоит
    заголовком карточки и подставляется в суффикс ПК-ссылки. Номер берём
    наименьший свободный, а не «сколько всего + 1», — иначе после удаления
    второго из трёх новый снова назвался бы вторым.
    """
    taken = {a.label for a in await repo.list_wdtt_for_user(session, user_id)}
    n = 1
    while f"Резервное подключение {n}" in taken:
        n += 1
    return f"Резервное подключение {n}"


async def issue(
    session: AsyncSession,
    user,
    *,
    server: Server,
    device,
    platform: str,
    vk_hashes: str | None,
) -> tuple[WdttAccess, str]:
    """Создаёт доступ на сервере и запись в базе. Возвращает (запись, ссылка).

    Порядок важен: сначала сервер, потом база. Наоборот — и запись указывала бы
    на пароль, которого на сервере нет.

    Ёмкость перепроверяем прямо здесь: пока юзер выбирал платформу, последнее
    свободное место мог занять другой человек.
    """
    if server.wdtt_max_accesses is not None:
        load = await repo.count_active_wdtt_by_server(session)
        if load.get(server.id, 0) >= server.wdtt_max_accesses:
            raise NoCapacity

    label = (
        device.label if device is not None
        else await standalone_label(session, user.id)
    )
    async with SSHClient(repo.creds_from_server(server)) as ssh:
        res = await wdtt_svc.create_access(
            ssh,
            days=sub_days_left(user),
            label=label,
            vk_hashes=vk_hashes or settings.wdtt_vk_hashes,
            ports=server.wdtt_ports,
            binary=settings.wdtt_binary_path,
        )

    # Адрес в ссылку ставим свой: сервер мог запомнить прежний IP и отдавать
    # его до перезапуска демона (см. wdtt_svc.link_with_host).
    link = wdtt_svc.link_with_host(res["link"], server.host)
    if platform == "pc":
        link = f"{link}#{label}"

    access = await repo.create_wdtt_access(
        session,
        server_id=server.id,
        user_id=user.id,
        device_id=device.id if device is not None else None,
        label=label,
        uri_enc=encrypt(link),
        password_enc=encrypt(res["password"]),
        expires_at=None,  # срок гейтит подписка на уровне устройства
        platform=platform,
        # Своя ссылка юзера или сервисная — поддержке это первый вопрос при
        # разборе «у меня не работает».
        vk_own=bool(vk_hashes),
    )
    # Выдача — такое же событие доступа, как выдача конфига VPN: без него в
    # истории юзера подключение появляется из ниоткуда и исчезает при отзыве.
    await repo.log_action(
        session, AuditAction.CONFIG_ISSUED,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        target_type="wdtt",
        target_id=access.id,
        details=f"Обход БС «{label}» на сервере «{server.name}» ({platform})",  # wording: ok — аудит-лог админа
    )
    return access, link
