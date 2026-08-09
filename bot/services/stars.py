"""Зачисление оплаты звёздами Telegram на баланс.

Звёзды пополняют баланс, а не покупают подписку напрямую: покупка,
автопродление, реферальные начисления и история операций уже ходят через
баланс, и второй путь пришлось бы дублировать в каждом из этих мест.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import StarPayment
from bot.services import billing


async def credit_star_payment(
    session: AsyncSession, *, user_id: int, charge_id: str,
    amount_kopeks: int, stars: int,
) -> billing.DepositResult:
    """Зачисляет оплату звёздами. credited=False — этот платёж уже был зачислен.

    Идемпотентность по `charge_id`: Telegram может доставить событие оплаты
    повторно, и без этой проверки баланс вырос бы дважды. Коммит — на
    вызывающем.

    Дальше — общая дорога с CryptoBot (billing.credit_deposit): реф-награда
    пригласившему и запись в журнал у звёзд ровно те же. Бонуса за способ у них
    нет: своя наценка 25 % уже заложена в цену звёзд, и бонус поверх неё был бы
    взаимоисключающим.
    """
    if await session.get(StarPayment, charge_id) is not None:
        logger.info("Star payment {} already credited, skipping", charge_id)
        return billing.DepositResult(credited=False)
    session.add(StarPayment(
        charge_id=charge_id, user_id=user_id,
        amount_kopeks=amount_kopeks, stars=stars,
    ))
    return await billing.credit_deposit(
        session, user_id=user_id, amount_kopeks=amount_kopeks,
        method="stars", note=f"Пополнение звёздами ({stars} ⭐)",
        audit_details=f"Пополнение баланса звёздами ({stars} ⭐)",
    )
