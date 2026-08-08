"""Уничтожение юзера из БД (Блок «Ревизия»).

Порядок продиктован двумя ловушками:

1. SQLite не enforce'ит FK (PRAGMA foreign_keys нигде не включён), поэтому
   ondelete=CASCADE в моделях — мёртвый DDL: детей чистим явно по user_id.
2. Пир/wdtt-пароль, удалённый из БД до снятия с сервера, остаётся на VPS
   навсегда (снять больше нечем — ключи были только в строке). Поэтому строку
   удаляем ТОЛЬКО если снятие с сервера прошло; если сервер был недоступен,
   строка остаётся REVOKED и ретеншн планировщика через 30 дней ПОВТОРИТ
   SSH-снятие и удалит её сам. Чистка ретеншна идёт по статусу/дате, не по
   юзеру — работает и для строк с уже несуществующим user_id.

   Раньше здесь оставляли REVOKED-строки всегда. Это давало дыру: Влад стёр
   аккаунт, зашёл заново, SQLite отдал новому юзеру тот же id — и к нему
   «прилипли» чужие отозванные устройства, которые ревайв при продлении мог
   поднять. Стирание должно стирать: что снято — удаляем сразу.

Повторный /start удалённого юзера создаёт его заново С НОВЫМ ТРИАЛОМ — это
осознанный компромисс (фича для мусорных аккаунтов и «сотрите мои данные»);
наказание — is_blocked, не удаление. Админ предупреждается на подтверждении.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus, User
from bot.services import revive as revive_svc


@dataclass
class WipeResult:
    tg_id: int
    revoked_items: int = 0            # активных пиров+обходов снято при отзыве
    deleted_configs: int = 0          # строк, удалённых из БД после снятия с сервера
    history_rows: int = 0             # записей журнала БЫЛО у юзера до стирания
    purged: dict[str, int] = field(default_factory=dict)


async def _delete_cleared(
    session: AsyncSession,
    user_id: int,
    peers: list,
    accesses: list,
    cleared: "revive_svc.ClearedFromServer",
    pre_revoked_peers: set[int],
    pre_revoked_wdtt: set[int],
) -> int:
    """Удаляет пиры и wdtt-строки, чей конфиг снят с сервера.

    «Снят» значит одно из двух: отзыв только что прошёл успешно (id в `cleared`)
    ИЛИ строка была отозвана ДО стирания (id в `pre_revoked_*` — снятие
    случилось при том отзыве). Неснятые строки не трогаем: в них единственные
    ключи, которыми ретеншн повторит снятие.

    Статусы обязаны быть снятыми ДО отзыва, а не прочитанными здесь: отзыв
    метит строку REVOKED независимо от того, ответил сервер или нет. Первая
    версия читала статус на месте и удаляла строку с недоступного сервера —
    ровно тот случай, от которого предостерегает докстринг модуля.
    """
    removable_peers = pre_revoked_peers | cleared.peers
    removable_wdtt = pre_revoked_wdtt | cleared.wdtt

    deleted = 0
    for p in peers:
        if p.id in removable_peers:
            await repo.delete_peer(session, p.id)
            deleted += 1
    for a in accesses:
        if a.id in removable_wdtt:
            await repo.delete_wdtt_access(session, a.id)
            deleted += 1

    # Устройства-пустышки: всё их содержимое удалено выше, восстанавливать нечем.
    # Оставленное устройство прилипло бы к новому юзеру с тем же id и висело бы
    # в его списке. Устройства, где что-то осталось (SSH не прошёл), не трогаем —
    # их подберёт зомби-чистка планировщика после ретеншна.
    for device in await repo.list_devices_for_user(session, user_id):
        left_peers = await repo.list_peers_for_device(session, device.id)
        left_wdtt = await repo.list_wdtt_for_device(session, device.id)
        if not left_peers and not left_wdtt:
            await repo.delete_device(session, device.id)
    return deleted


async def wipe_user(session: AsyncSession, user: User) -> WipeResult:
    """Стирает юзера: отзыв конфигов → чистка записей → удаление строки users.

    Коммит — на вызывающем. Защита от удаления админа — тоже на вызывающем
    (хендлер знает актуальный settings.admin_ids)."""
    res = WipeResult(tg_id=user.tg_id)
    # Считаем ДО отзыва: шаг 1 сам пишет в журнал по строке на устройство, и
    # эти строки тут же уносит чистка на шаге 2. Показывать админу их вместе с
    # настоящей историей значит завышать цифру тем сильнее, чем больше у юзера
    # было устройств.
    res.history_rows = await repo.count_audit_for_user(session, user.id)

    # 1. Активные конфиги: SSH-снятие best-effort + пометка REVOKED (общий
    #    примитив с истечением подписки). До удаления строк — см. докстринг.
    peers = await repo.list_peers_for_user(session, user.id)
    accesses = await repo.list_wdtt_for_user(session, user.id)
    res.revoked_items = (
        sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
        + sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)
    )
    cleared = revive_svc.ClearedFromServer()
    # Кто был отозван ЗАРАНЕЕ — снимаем до отзыва: он пометит REVOKED всех,
    # включая тех, кого не смог снять с сервера, и отличить их станет нельзя.
    pre_revoked_peers = {
        p.id for p in peers
        if p.status == PeerStatus.REVOKED and p.revoked_at is not None
    }
    pre_revoked_wdtt = {
        a.id for a in accesses
        if a.status == PeerStatus.REVOKED and a.revoked_at is not None
    }
    await revive_svc.revoke_devices_for_user(session, user.id, cleared=cleared)
    # Легаси-пиры без device_id (отзыв пиров идёт по устройствам) — адресно.
    # Резервные подключения без устройства отзыв берёт на себя сам: он ходит по
    # юзеру, снимает их с сервера и кладёт в `cleared`, — повторять не нужно.
    for p in peers:
        if p.device_id is None and p.status == PeerStatus.ACTIVE:
            await repo.revoke_peer(session, p.id)

    # 1a. Удаляем строки, чей конфиг снят с сервера: стирание должно стирать, а
    #     оставленные REVOKED-строки прилипали к новому юзеру с тем же id (см.
    #     докстринг). Уже-REVOKED строки (отозваны раньше, до стирания) сняты с
    #     сервера при том отзыве — их тоже удаляем. Остаются только те, где
    #     SSH сейчас не прошёл: в них ключи для повтора ретеншном.
    res.deleted_configs = await _delete_cleared(
        session, user.id, peers, accesses, cleared,
        pre_revoked_peers, pre_revoked_wdtt,
    )

    # 2. «Бумага»: журнал баланса, инвойсы (открытые гаснут вместе со строками —
    #    поллинг их больше не увидит), сапорт-маршруты; отвязка рефералов.
    res.purged = await repo.purge_user_records(session, user.id)

    # 3. Сама строка users — Core DELETE, не session.delete(user): ORM-удаление
    #    попыталось бы занулить peers.user_id через relationship и упало бы на
    #    NOT NULL. Оставшиеся REVOKED пиры/обходы — только те, что не сняты с
    #    сервера (SSH не прошёл): ретеншн повторит снятие по ключам из строки.
    user_id, tg_id = user.id, user.tg_id
    session.expunge(user)
    await session.execute(delete(User).where(User.id == user_id))
    logger.info(
        "User {} (tg {}) wiped: {} revoked, {} configs deleted, purged {}",
        user_id, tg_id, res.revoked_items, res.deleted_configs, res.purged,
    )
    return res
