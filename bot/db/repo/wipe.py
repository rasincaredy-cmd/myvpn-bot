"""Уничтожение юзера (Блок «Ревизия»): удаление «бумажных» следов."""
from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BalanceTx, CryptoInvoice, SupportMsg, User


async def purge_user_records(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Удаляет «бумажные» следы юзера: журнал баланса, инвойсы, маршруты сапорта —
    и отвязывает его рефералов (их referrer_id → NULL, реф-циклы через удаление
    невозможны). Пиры/обходы/устройства НЕ трогает — их снимает с серверов и
    метит REVOKED вызывающий (retention планировщика добьёт строки через 30 дней,
    повторив SSH-снятие для тех, где оно не прошло). Возвращает счётчики."""
    counts: dict[str, int] = {}
    counts["balance_txs"] = (await session.execute(
        delete(BalanceTx).where(BalanceTx.user_id == user_id)
    )).rowcount or 0
    counts["invoices"] = (await session.execute(
        delete(CryptoInvoice).where(CryptoInvoice.user_id == user_id)
    )).rowcount or 0
    counts["support_msgs"] = (await session.execute(
        delete(SupportMsg).where(SupportMsg.user_id == user_id)
    )).rowcount or 0
    counts["referrals_unlinked"] = (await session.execute(
        update(User).where(User.referrer_id == user_id).values(referrer_id=None)
    )).rowcount or 0
    return counts
