"""Снятие устройств/доступов обхода с серверов + удаление из БД.

Общая машинерия для юзерских и админских хендлеров: сначала best-effort снимаем
с VPS (WG-пиры / wdtt-пароли), затем чистим записи в БД. SSH-сбой не мешает
удалению из БД — иначе бот продолжит считать доступ живым."""
from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Device, PeerStatus, WdttAccess
from bot.services import amnezia
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError


async def delete_device(session: AsyncSession, device: Device) -> None:
    """Снимает все активные пиры и доступы обхода устройства с серверов, затем
    полностью удаляет устройство из БД (пиры+обходы+запись), освобождая IP."""
    for peer in await repo.list_peers_for_device(session, device.id):
        if peer.status != PeerStatus.ACTIVE:
            continue
        server = await repo.get_server(session, peer.server_id)
        if server is None:
            continue
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
        except SSHError as exc:
            logger.warning("Teardown device {} peer {} ssh err: {}", device.id, peer.id, exc)
    for acc in await repo.list_wdtt_for_device(session, device.id):
        if acc.status != PeerStatus.ACTIVE:
            continue
        await _remove_bypass_on_server(session, acc)
    await repo.delete_device(session, device.id)


async def revoke_bypass(session: AsyncSession, access: WdttAccess) -> None:
    """Снимает пароль обхода с сервера и удаляет запись из БД (отозванный обход =
    мёртвая ссылка, ревайва нет)."""
    if access.status == PeerStatus.ACTIVE:
        await _remove_bypass_on_server(session, access)
    await repo.delete_wdtt_access(session, access.id)


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
