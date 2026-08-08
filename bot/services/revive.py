"""Восстановление устройств после продления подписки (Блок «Ревайв»).

Зеркало отзыва в scheduler._revoke_all_devices_for_user: при истечении подписки
устройства/пиры/wdtt-доступы помечаются REVOKED и ждут retention-срок. Когда
админ продлевает подписку, этот сервис возвращает всё как было:

  • WG-пиры заново добавляются на сервер с ТЕМИ ЖЕ ключами и IP — старый конфиг
    на устройстве юзера просто оживает, ничего перенастраивать не нужно;
  • wdtt-пароли восстанавливаются на сервере через `ctl add -password` — прежняя
    wdtt://-ссылка снова работает.

Лимиты подписки уважаем: восстанавливаем не больше sub_max_devices устройств и
sub_max_bypass обходов (старейшие первыми); лишние остаются REVOKED до retention.
SSH-сбой по одному пиру/доступу не валит остальные: что не ожило — остаётся
REVOKED и попадёт под retention (или ручной ревайв админом).

Переехавшие пиры (Этап C, `Peer.grace_until`) не воскрешаются вообще."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction, PeerStatus, User
from bot.services import amnezia
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError


@dataclass
class ClearedFromServer:
    """Что удалось снять с сервера при отзыве — id пиров и wdtt-доступов.

    Отзыв снимает конфиги best-effort: сервер может быть недоступен. Строку с
    ключами удалять можно ТОЛЬКО если снятие прошло, иначе пир останется на VPS
    навсегда — снять его будет нечем. Этот набор и отделяет одни от других.
    """

    peers: set[int] = field(default_factory=set)
    wdtt: set[int] = field(default_factory=set)


@dataclass
class ReviveResult:
    devices_restored: int = 0
    peers_restored: int = 0
    bypass_restored: int = 0
    devices_skipped_limit: int = 0
    bypass_skipped_limit: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.devices_restored or self.bypass_restored or self.errors)


def _sub_days_left(user: User) -> int:
    """Дней до конца подписки для ctl -days; 0 = бессрочно (как в handlers/wdtt)."""
    if user.sub_expires_at is None:
        return 0
    exp = user.sub_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    delta = exp - datetime.now(timezone.utc)
    return max(1, math.ceil(delta.total_seconds() / 86400))


def _parse_wdtt_uri(uri: str) -> tuple[str, str] | None:
    """Достаёт (ports 'dtls,wg,tun', vk-хеши) из wdtt://ip:p1:p2:p3:pass:hash[#label].

    Восстанавливаем ровно то, что зашито в ссылке у клиента, — если юзер
    создавал доступ со своей VK-ссылкой, она и вернётся."""
    body = uri.split("://", 1)[-1].split("#", 1)[0]
    parts = body.split(":")
    if len(parts) != 6:
        return None
    return ",".join(parts[1:4]), parts[5]


async def revoke_devices_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    reason: str | None = None,
    cleared: ClearedFromServer | None = None,
) -> bool:
    """Отзывает ВСЕ активные устройства юзера: снимает WG-пиры и wdtt-доступы с
    серверов (best-effort), затем метит устройства/пиры/wdtt-строки REVOKED
    (см. repo.revoke_device) — они ждут ревайва при продлении retention-срок.

    Зеркало revive_devices_for_user. Зовётся планировщиком (истечение срока,
    лимит трафика) и админкой (мгновенное отключение подписки — конфиги гаснут
    сразу, без ожидания тика). Коммит и уведомление — на вызывающем.
    Возвращает True, если что-то отозвали.

    Событие журнала пишется здесь, а не врезкой у вызывающего: путей отзыва уже
    четыре, и каждый новый — ещё один шанс врезку забыть. Почему погасло, знает
    только вызывающий, поэтому актор и `reason` приходят параметрами. `reason` —
    именно причина («истекла подписка»), а не готовая строка события: текст
    собирается здесь из метки устройства, чтобы строки отличались друг от друга.

    Строка пишется на КАЖДОЕ устройство — как и оживление ниже пишется на каждый
    пир: в карточке юзера должно быть видно, что именно встало. Поэтому счётчику
    устройств в тексте не место — сколько погасло, видно по числу строк, а
    повторённый в каждой из них счётчик читался бы как задвоение.

    `cleared` (если передан) заполняется id тех пиров и обходов, которые
    ФАКТИЧЕСКИ сняты с сервера. Нужен стиранию юзера: только такие строки можно
    удалять из БД сразу — в остальных лежат единственные ключи, которыми
    ретеншн повторит снятие."""
    devices = await repo.list_devices_for_user(session, user_id, active_only=True)
    if not devices:
        return False
    for device in devices:
        for peer in await repo.list_peers_for_device(session, device.id):
            if peer.status != PeerStatus.ACTIVE:
                continue
            server = await repo.get_server(session, peer.server_id)
            if server:
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
                    if cleared is not None:
                        cleared.peers.add(peer.id)
                except SSHError as exc:
                    logger.warning("Revoke-all peer remove err {}: {}", peer.id, exc)
        for acc in await repo.list_wdtt_for_device(session, device.id):
            if acc.status != PeerStatus.ACTIVE:
                continue
            server = await repo.get_server(session, acc.server_id)
            if server:
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await wdtt_svc.remove_access(
                            ssh, password=decrypt(acc.password_enc),
                            binary=settings.wdtt_binary_path,
                        )
                    if cleared is not None:
                        cleared.wdtt.add(acc.id)
                except SSHError as exc:
                    logger.warning("Revoke-all wdtt remove err {}: {}", acc.id, exc)
        await repo.revoke_device(session, device.id)
        await repo.log_action(
            session, AuditAction.CONFIG_REVOKED,
            actor_tg_id=actor_tg_id,
            actor_is_admin=actor_is_admin,
            target_user_id=user_id,
            target_type="device",
            target_id=device.id,
            details=(
                f"Устройство «{device.label}» отозвано: {reason}" if reason
                else f"Устройство «{device.label}» отозвано"
            ),
        )
        logger.info("Revoked device {} (user {})", device.id, user_id)
    return True


async def revive_devices_for_user(session: AsyncSession, user: User) -> ReviveResult:
    """Восстанавливает все REVOKED-устройства юзера (в пределах лимитов подписки).

    Коммит — на вызывающем. Уведомление юзеру — тоже на вызывающем (сервис
    не знает контекста: продление из админки, будущие платежи и т.д.).

    События журнала здесь несимметричны отзыву, и это намеренно: отзыв гасит
    устройство целиком и пишет строку на устройство, а оживление возвращает по
    отдельности пир и обход БС — каждый со своим SSH, каждый может не вернуться
    (см. res.errors ниже). Одна строка на устройство скрыла бы случай «пир ожил,
    обход не смог», ради которого админ в историю и полезет."""
    res = ReviveResult()

    devices = [
        d for d in await repo.list_devices_for_user(session, user.id)
        if d.status == PeerStatus.REVOKED
    ]
    if not devices:
        return res

    device_budget = max(0, user.sub_max_devices - await repo.count_active_devices(session, user.id))
    bypass_budget = max(0, user.sub_max_bypass - await repo.count_active_wdtt_for_user(session, user.id))
    res.devices_skipped_limit = max(0, len(devices) - device_budget)

    for device in devices[:device_budget]:
        revived_any = False

        # --- WG-пиры: тот же ключ + тот же IP → старый конфиг оживает --------
        for peer in await repo.list_peers_for_device(session, device.id):
            if peer.status != PeerStatus.REVOKED:
                continue
            # Переехавший конфиг (Этап C) обратно не поднимаем: его заменил новый
            # пир на другом сервере, а файла от старого у юзера в приложении
            # уже нет — «оживший» конфиг просто занимал бы IP и место под
            # потолком сервера. Метку переезда держит grace_until.
            if peer.grace_until is not None:
                continue
            server = await repo.get_server(session, peer.server_id)
            if server is None:
                res.errors.append(f"пир {peer.label}: сервер удалён")
                continue
            try:
                async with SSHClient(repo.creds_from_server(server)) as ssh:
                    await amnezia.add_peer_on_server(
                        ssh, public_key=peer.public_key, peer_ip=peer.ip
                    )
            except SSHError as exc:
                logger.warning("Revive peer {} ssh err: {}", peer.id, exc)
                res.errors.append(f"пир {peer.label} ({server.name}): SSH-ошибка")
                continue
            await repo.revive_peer(session, peer.id)
            # Инициатора-человека нет: конфиг возвращает бот после оплаты,
            # поэтому actor_tg_id=None (юзер лишь заплатил, оживление — наше).
            await repo.log_action(
                session, AuditAction.CONFIG_REVIVED,
                actor_tg_id=None,
                target_user_id=user.id,
                target_type="peer",
                target_id=peer.id,
                details=f"{peer.label} на сервере «{server.name}» ожил после оплаты",
            )
            res.peers_restored += 1
            revived_any = True

        # --- Обходы БС: восстанавливаем прежний пароль на wdtt-сервере -------
        for acc in await repo.list_wdtt_for_device(session, device.id):
            if acc.status != PeerStatus.REVOKED:
                continue
            if bypass_budget <= 0:
                res.bypass_skipped_limit += 1
                continue
            server = await repo.get_server(session, acc.server_id)
            if server is None:
                res.errors.append(f"обход {acc.label}: сервер удалён")  # wording: ok
                continue
            password = decrypt(acc.password_enc)
            parsed = _parse_wdtt_uri(decrypt(acc.uri_enc))
            ports, vk_hashes = parsed if parsed else (server.wdtt_ports, settings.wdtt_vk_hashes)
            try:
                async with SSHClient(repo.creds_from_server(server)) as ssh:
                    got = await wdtt_svc.create_access(
                        ssh,
                        days=_sub_days_left(user),
                        label=acc.label,
                        vk_hashes=vk_hashes,
                        ports=ports,
                        binary=settings.wdtt_binary_path,
                        password=password,
                    )
            except SSHError as exc:
                logger.warning("Revive wdtt {} ssh err: {}", acc.id, exc)
                res.errors.append(f"обход {acc.label}: SSH-ошибка")  # wording: ok
                continue
            if got["password"] != password:
                # Старый бинарь wdtt-сервера проигнорировал -password и сгенерил
                # новый — прежняя ссылка юзера мертва. Откатываем лишний пароль,
                # доступ оставляем REVOKED: чинить надо деплоем сервера.
                logger.error("Revive wdtt {}: сервер вернул другой пароль (старый бинарь?)", acc.id)
                res.errors.append(f"обход {acc.label}: wdtt-сервер не поддерживает restore")  # wording: ok
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await wdtt_svc.remove_access(
                            ssh, password=got["password"], binary=settings.wdtt_binary_path
                        )
                except SSHError:
                    pass
                continue
            await repo.revive_wdtt_access(session, acc.id)
            # Симметрично оживлению пира выше: юзер платит один раз, а обратно
            # у него включаются две разные вещи, и в истории должно быть видно
            # обе. Актора-человека нет — возвращает бот после оплаты.
            await repo.log_action(
                session, AuditAction.CONFIG_REVIVED,
                actor_tg_id=None,
                target_user_id=user.id,
                target_type="wdtt",
                target_id=acc.id,
                details=f"Обход БС «{acc.label}» на сервере «{server.name}» ожил после оплаты",  # wording: ok
            )
            bypass_budget -= 1
            res.bypass_restored += 1
            revived_any = True

        if revived_any:
            device.status = PeerStatus.ACTIVE
            res.devices_restored += 1
            logger.info("Revived device {} (user {})", device.id, user.id)

    return res
