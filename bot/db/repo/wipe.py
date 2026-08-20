"""Уничтожение юзера (Блок «Ревизия»): удаление «бумажных» следов."""
from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AuditLog,
    BalanceTx,
    CryptoInvoice,
    PlategaPayment,
    StarPayment,
    SupportMsg,
    User,
)


async def purge_user_records(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Удаляет «бумажные» следы юзера: журнал действий, журнал баланса, инвойсы,
    маршруты сапорта — и отвязывает его рефералов (их referrer_id → NULL,
    реф-циклы через удаление невозможны). Пиры/обходы/устройства НЕ трогает — их
    снимает с серверов и метит REVOKED вызывающий (retention планировщика добьёт
    строки через 30 дней, повторив SSH-снятие для тех, где оно не прошло).
    Возвращает счётчики.

    Журнал действий чистить обязательно, и не только ради «сотрите мои данные»:
    users.id объявлен как Integer primary key БЕЗ AUTOINCREMENT, поэтому SQLite
    переиспользует освободившийся максимальный rowid. Оставленные строки
    достались бы следующему зарегистрировавшемуся человеку — админ открыл бы
    карточку новичка и увидел там чужие пополнения. События с target_user_id IS
    NULL (в частности сам USER_WIPED) переживают чистку намеренно: стирание
    обязано оставить след в общей ленте.
    """
    counts: dict[str, int] = {}
    counts["audit_logs"] = (await session.execute(
        delete(AuditLog).where(AuditLog.target_user_id == user_id)
    )).rowcount or 0
    counts["balance_txs"] = (await session.execute(
        delete(BalanceTx).where(BalanceTx.user_id == user_id)
    )).rowcount or 0
    counts["invoices"] = (await session.execute(
        delete(CryptoInvoice).where(CryptoInvoice.user_id == user_id)
    )).rowcount or 0
    # Звёздные платежи уходят вместе с остальными деньгами: строка нужна была
    # ради идемпотентности зачисления, а зачислять уже некому — юзера нет.
    counts["star_payments"] = (await session.execute(
        delete(StarPayment).where(StarPayment.user_id == user_id)
    )).rowcount or 0
    # Карточные платежи чистим по той же причине, что и остальные деньги, и
    # ещё по одной: `pending`-строка стёртого юзера досталась бы следующему
    # зарегистрировавшемуся (id переиспользуется, см. выше), и поллинг
    # планировщика зачислил бы ЕМУ чужую оплату. Найдено аудитом 20.08.2026.
    counts["platega_payments"] = (await session.execute(
        delete(PlategaPayment).where(PlategaPayment.user_id == user_id)
    )).rowcount or 0
    counts["support_msgs"] = (await session.execute(
        delete(SupportMsg).where(SupportMsg.user_id == user_id)
    )).rowcount or 0
    counts["referrals_unlinked"] = (await session.execute(
        update(User).where(User.referrer_id == user_id).values(referrer_id=None)
    )).rowcount or 0
    return counts
