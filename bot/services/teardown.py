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
    """Снимает все активные пиры и доступы обхода устройства с серверов, затем
    полностью удаляет устройство из БД (пиры+обходы+запись), освобождая IP.

    Событие журнала пишется здесь же, а не врезкой у вызывающего: удалять
    устройство умеют и юзер, и админ из карточки, и каждый новый экран — ещё
    один шанс врезку забыть. Кто именно удалил, знает только вызывающий, поэтому
    актор и текст приходят параметрами. Коммит — на вызывающем, чтобы событие
    откатилось вместе с удалением."""
    device_id, user_id, label = device.id, device.user_id, device.label
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
            logger.warning("Teardown device {} peer {} ssh err: {}", device_id, peer.id, exc)
    for acc in await repo.list_wdtt_for_device(session, device_id):
        if acc.status != PeerStatus.ACTIVE:
            continue
        await _remove_bypass_on_server(session, acc)
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
    if access.status == PeerStatus.ACTIVE:
        await _remove_bypass_on_server(session, access)
    await repo.delete_wdtt_access(session, access_id)
    await repo.log_action(
        session, AuditAction.CONFIG_REVOKED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=user_id,
        target_type="wdtt",
        target_id=access_id,
        details=details or f"Обход БС «{label}» удалён",  # wording: ok — аудит-лог админа
    )


async def _remove_bypass_on_server(session: AsyncSession, access: WdttAccess) -> None:
    server = await repo.get_server(session, access.server_id)
    if server is None:
        return
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            await wdtt_svc.remove_access(
                ssh, password=decrypt(access.password_enc),
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        logger.warning("Teardown bypass {} ssh err: {}", access.id, exc)
