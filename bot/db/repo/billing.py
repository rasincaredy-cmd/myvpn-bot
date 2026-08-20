"""Блок «Баланс»: баланс, журнал операций, инвойсы Crypto Pay, рефералка."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BalanceTx, CryptoInvoice, PlategaPayment, User


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


async def charge_balance(
    session: AsyncSession, user_id: int, price_kopeks: int, note: str | None = None
) -> bool:
    """Списывает деньги ТОЛЬКО если их хватает. True — списали, False — нет.

    Условие «хватает» стоит внутри самого UPDATE, а не отдельным чтением до
    него. Иначе между проверкой и списанием остаётся окно: покупка успевает
    сходить по SSH оживлять устройства (секунды), второй тап «Купить» читает
    ещё не изменённый баланс, проходит проверку — и списывает второй раз.
    Троттлинг в 0.7 с это окно не закрывает.

    Строка журнала пишется только при успехе: отказ, оставивший запись о
    списании, юзер прочитал бы как «деньги сняли».

    Коммит — на вызывающем.
    """
    res = await session.execute(
        update(User)
        .where(User.id == user_id, User.balance_kopeks >= price_kopeks)
        .values(balance_kopeks=User.balance_kopeks - price_kopeks)
    )
    if res.rowcount == 0:
        return False
    session.add(BalanceTx(
        user_id=user_id, amount_kopeks=-price_kopeks, kind="charge", note=note
    ))
    await session.flush()
    return True


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


async def create_platega_payment(
    session: AsyncSession, *, user_id: int, transaction_id: str,
    amount_kopeks: int, url: str,
) -> PlategaPayment:
    row = PlategaPayment(
        user_id=user_id, transaction_id=transaction_id,
        amount_kopeks=amount_kopeks, url=url,
    )
    session.add(row)
    await session.flush()
    return row


async def get_platega_payment(
    session: AsyncSession, row_id: int
) -> PlategaPayment | None:
    return await session.get(PlategaPayment, row_id)


async def get_platega_payment_by_tx(
    session: AsyncSession, transaction_id: str
) -> PlategaPayment | None:
    """Строка платежа по id транзакции провайдера — так приходит уведомление."""
    return (await session.execute(
        select(PlategaPayment).where(PlategaPayment.transaction_id == transaction_id)
    )).scalar_one_or_none()


async def list_open_platega_payments(
    session: AsyncSession, *, max_age_hours: int = 24
) -> list[PlategaPayment]:
    """Неоплаченные счета для поллинга планировщиком. Счёт Platega живёт 30
    минут, поэтому суток с запасом хватает: всё старше провайдер уже отменил."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return list((await session.execute(
        select(PlategaPayment)
        .where(PlategaPayment.status == "pending")
        .where(PlategaPayment.created_at >= cutoff)
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
