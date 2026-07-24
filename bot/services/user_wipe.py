"""Уничтожение юзера из БД (Блок «Ревизия»).

Порядок продиктован двумя ловушками:

1. SQLite не enforce'ит FK (PRAGMA foreign_keys нигде не включён), поэтому
   ondelete=CASCADE в моделях — мёртвый DDL: детей чистим явно по user_id.
2. Пир/wdtt-пароль, удалённый из БД до снятия с сервера, остаётся на VPS
   навсегда (снять больше нечем — ключи были только в строке). Поэтому конфиги
   НЕ удаляем из БД, а отзываем (SSH best-effort + статус REVOKED): retention
   планировщика через 30 дней удалит строки сам и ПОВТОРИТ SSH-снятие для тех,
   где оно сейчас не прошло. Чистка ретеншна идёт по статусу/дате, не по юзеру —
   работает и для строк с уже несуществующим user_id.

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
    purged: dict[str, int] = field(default_factory=dict)


async def wipe_user(session: AsyncSession, user: User) -> WipeResult:
    """Стирает юзера: отзыв конфигов → чистка записей → удаление строки users.

    Коммит — на вызывающем. Защита от удаления админа — тоже на вызывающем
    (хендлер знает актуальный settings.admin_ids)."""
    res = WipeResult(tg_id=user.tg_id)

    # 1. Активные конфиги: SSH-снятие best-effort + пометка REVOKED (общий
    #    примитив с истечением подписки). До удаления строк — см. докстринг.
    peers = await repo.list_peers_for_user(session, user.id)
    accesses = await repo.list_wdtt_for_user(session, user.id)
    res.revoked_items = (
        sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
        + sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)
    )
    await revive_svc.revoke_devices_for_user(session, user.id)
    # Легаси-строки без device_id (revoke идёт по устройствам) — отзываем адресно.
    for p in peers:
        if p.device_id is None and p.status == PeerStatus.ACTIVE:
            await repo.revoke_peer(session, p.id)
    for a in accesses:
        if a.device_id is None and a.status == PeerStatus.ACTIVE:
            await repo.revoke_wdtt_access(session, a.id)

    # 2. «Бумага»: журнал баланса, инвойсы (открытые гаснут вместе со строками —
    #    поллинг их больше не увидит), сапорт-маршруты; отвязка рефералов.
    res.purged = await repo.purge_user_records(session, user.id)

    # 3. Сама строка users — Core DELETE, не session.delete(user): ORM-удаление
    #    попыталось бы занулить peers.user_id через relationship и упало бы на
    #    NOT NULL. Оставшиеся REVOKED пиры/обходы/устройства переживут юзера до
    #    ретеншна (30 дн) — их user_id повиснет, это ожидаемо: чистка и повторное
    #    SSH-снятие в scheduler идут по статусу, владелец не нужен.
    user_id, tg_id = user.id, user.tg_id
    session.expunge(user)
    await session.execute(delete(User).where(User.id == user_id))
    logger.info(
        "User {} (tg {}) wiped: {} revoked, purged {}",
        user_id, tg_id, res.revoked_items, res.purged,
    )
    return res
