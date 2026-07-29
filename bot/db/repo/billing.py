"""Блок «Баланс»: баланс, журнал операций, инвойсы Crypto Pay, рефералка."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BalanceTx, CryptoInvoice, User


async def add_balance_tx(
    session: AsyncSession,
    user_id: int,
    amount_kopeks: int,
    kind: str,
    note: str | None = None,
) -> None:
    """ЕДИНСТВЕННАЯ точка изменения баланса: атомарный инкремент User.balance_kopeks
    + строка журнала balance_txs. kind: deposit | charge | ref | admin.
    Коммит — на вызывающем."""
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(balance_kopeks=User.balance_kopeks + amount_kopeks)
    )
    session.add(BalanceTx(
        user_id=user_id, amount_kopeks=amount_kopeks, kind=kind, note=note
    ))
    await session.flush()


async def list_balance_txs(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[BalanceTx]:
    return list((await session.execute(
        select(BalanceTx)
        .where(BalanceTx.user_id == user_id)
        .order_by(BalanceTx.id.desc())
        .limit(limit)
    )).scalars())


async def create_crypto_invoice(
    session: AsyncSession, *, user_id: int, invoice_id: int,
    amount_kopeks: int, url: str,
) -> CryptoInvoice:
    inv = CryptoInvoice(
        user_id=user_id, invoice_id=invoice_id,
        amount_kopeks=amount_kopeks, url=url,
    )
    session.add(inv)
    await session.flush()
    return inv


async def get_crypto_invoice(session: AsyncSession, row_id: int) -> CryptoInvoice | None:
    return await session.get(CryptoInvoice, row_id)


async def list_open_invoices(
    session: AsyncSession, *, max_age_days: int = 3
) -> list[CryptoInvoice]:
    """Активные инвойсы для поллинга планировщиком. Старше max_age_days не трогаем —
    Crypto Pay столько не живёт (наш expires_in час), это страховка от вечного опроса."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return list((await session.execute(
        select(CryptoInvoice)
        .where(CryptoInvoice.status == "active")
        .where(CryptoInvoice.created_at >= cutoff)
    )).scalars())


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(User).where(User.referrer_id == user_id)
    )).scalar_one()


async def sum_ref_earned(session: AsyncSession, user_id: int) -> int:
    """Сколько копеек юзер заработал на рефах за всё время (kind='ref')."""
    return (await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.user_id == user_id)
        .where(BalanceTx.kind == "ref")
    )).scalar_one()
