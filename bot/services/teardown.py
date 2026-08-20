"""Снятие устройств/доступов обхода с серверов + удаление из БД.

Общая машинерия для юзерских и админских хендлеров: сначала best-effort снимаем
с VPS (WG-пиры / wdtt-пароли), затем чистим записи в БД. SSH-сбой не мешает
удалению из БД — иначе бот продолжит считать доступ живым."""
from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction, Device, PeerStatus, WdttAccess
from bot.services import amnezia
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError


async def delete_device(
    session: AsyncSession,
    device: Device,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    details: str | None = None,
) -> None:
    """Снимает все активные пиры устройства с серверов, затем удаляет устройство
    из БД (пиры + запись), освобождая IP.

    Резервные подключения устройства остаются работать: они продаются отдельной
    позицией тарифа, и удаление телефона из списка не должно уносить оплаченное
    подключение. Устройство для них — метка (решение Влада 4.08), её снимает
    `repo.delete_device`; на сервере обхода снимать нечего.

    Событие журнала пишется здесь же, а не врезкой у вызывающего: удалять
    устройство умеют и юзер, и админ из карточки, и каждый новый экран — ещё
    один шанс врезку забыть. Кто именно удалил, знает только вызывающий, поэтому
    актор и текст приходят параметрами. Коммит — на вызывающем, чтобы событие
    откатилось вместе с удалением."""
    device_id, user_id, label = device.id, device.user_id, device.label
    stranded: list[int] = []          # пиры, которые снять с сервера не вышло
    for peer in await repo.list_peers_for_device(session, device_id):
        if peer.status != PeerStatus.ACTIVE:
            continue
        server = await repo.get_server(session, peer.server_id)
        if server is None:
            continue
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
        except SSHError as exc:
            logger.warning(
                "Teardown device {} peer {} ssh err: {} — ключи оставляем",
                device_id, peer.id, exc,
            )
            stranded.append(peer.id)
    # Не снятые пиры НЕ удаляем: в строке лежит единственный ключ, которым пир
    # снимается с VPS. Удалив её, мы оставляем на сервере вечный рабочий конфиг
    # (аудит 20.08.2026). Опаснее, чем та же дыра в ретеншне: удаление жмёт сам
    # юзер, и поймав момент недоступности ноды он освобождал лимит, не теряя
    # работающий конфиг, — и добавлял ещё одно устройство.
    #
    # Строку переводим в REVOKED и отвязываем от устройства: устройство сейчас
    # исчезнет, лимит освободится, а уборка планировщика повторит снятие и
    # удалит строку сама. Дату отзыва ставим в прошлое, чтобы уборка взяла её на
    # БЛИЖАЙШЕМ тике: обычные отозванные ждут месяц ради оживления при
    # продлении, а здесь оживлять нечего — устройства больше нет, и каждый день
    # ожидания это день бесплатного VPN.
    if stranded:
        await repo.strand_peers(session, stranded)
    await repo.delete_device(session, device_id)
    await repo.log_action(
        session, AuditAction.CONFIG_REVOKED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=user_id,
        target_type="device",
        target_id=device_id,
        details=details or f"Устройство «{label}» удалено",
    )


async def revoke_bypass(
    session: AsyncSession,
    access: WdttAccess,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    details: str | None = None,
) -> None:
    """Юзер/админ явно удаляет обход: снимаем пароль с сервера и удаляем запись
    из БД насовсем. Не путать с отзывом по истечению подписки (repo.revoke_device)
    — там строка остаётся REVOKED и ждёт ревайва при продлении.

    Событие журнала — здесь же и по той же причине, что у delete_device: путей
    удаления обхода три (юзер, карточка юзера в админке, карточка сервера)."""
    access_id, user_id, label = access.id, access.user_id, access.label
    removed = True
    if access.status == PeerStatus.ACTIVE:
        removed = await _remove_bypass_on_server(session, access)
    if removed:
        await repo.delete_wdtt_access(session, access_id)
    else:
        # Симметрично устройствам: не снятый доступ оставляем со своим паролем,
        # помечаем отозванным и датой в прошлом — уборка планировщика повторит
        # снятие на ближайшем тике и удалит строку сама.
        await repo.strand_wdtt_access(session, access_id)
    await repo.log_action(
        session, AuditAction.CONFIG_REVOKED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=user_id,
        target_type="wdtt",
        target_id=access_id,
        details=details or f"Обход БС «{label}» удалён",  # wording: ok — аудит-лог админа
    )


async def _remove_bypass_on_server(session: AsyncSession, access: WdttAccess) -> bool:
    """Снимает пароль доступа с wdtt-сервера. False — снять не удалось.

    Ответ важен вызывающему: в строке лежит единственный пароль, которым доступ
    закрывается. Удалив её после неудачного снятия, мы оставляем рабочее
    подключение навсегда (аудит 20.08.2026)."""
    server = await repo.get_server(session, access.server_id)
    if server is None:
        return True     # сервера нет — снимать нечего и не с чего
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            await wdtt_svc.remove_access(
                ssh, password=decrypt(access.password_enc),
                binary=settings.wdtt_binary_path,
            )
        return True
    except SSHError as exc:
        logger.warning("Teardown bypass {} ssh err: {} — ключи оставляем", access.id, exc)
        return False
