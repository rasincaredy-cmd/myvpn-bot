"""Оплата звёздами Telegram: счёт, подтверждение, зачисление (этап D).

Экраны выбора способа и суммы живут в balance.py вместе с остальным
пополнением — здесь только сам платёжный протокол Telegram: выставить счёт,
успеть ответить на pre_checkout и зачислить деньги по факту оплаты.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.keyboards.inline import star_invoice_kb
from bot.services import billing, stars as stars_svc
from bot.services.pricing import fmt_rub, stars_for_kopeks

router = Router(name="stars")

# По этой метке узнаём СВОЙ платёж в successful_payment: у бота может появиться
# и другая оплата звёздами, а зачислять на баланс нужно только пополнения.
_PAYLOAD_PREFIX = "stars"


async def send_star_invoice(message: Message, user, amount_kopeks: int) -> None:
    """Выставляет счёт в звёздах на пополнение баланса.

    Счёт — ОТДЕЛЬНОЕ сообщение, а не правка экрана: инвойс Telegram нельзя
    вклеить в существующее сообщение.

    Сумму зачисления кладём в payload и берём при оплате оттуда, а не считаем
    заново из звёзд: курс и наценка могут поменяться между выставлением счёта
    и оплатой, и юзер должен получить ровно то, что было написано на экране.
    """
    stars = stars_for_kopeks(amount_kopeks)
    await message.answer_invoice(
        title="Пополнение баланса",
        description=(
            f"На баланс зачислим {fmt_rub(amount_kopeks)}. "
            f"Наценка за оплату звёздами — {settings.star_markup_percent}%."
        ),
        payload=f"{_PAYLOAD_PREFIX}:{user.id}:{amount_kopeks}",
        currency="XTR",
        prices=[LabeledPrice(label=fmt_rub(amount_kopeks), amount=stars)],
        reply_markup=star_invoice_kb(stars),
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    # Ответить обязательно и в течение 10 секунд, иначе Telegram отменит платёж.
    # Проверять нечего: и сумма, и получатель заданы нами при выставлении счёта.
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, session: AsyncSession) -> None:
    sp = message.successful_payment
    parts = (sp.invoice_payload or "").split(":")
    if len(parts) != 3 or parts[0] != _PAYLOAD_PREFIX:
        logger.error("Unexpected successful_payment payload: {}", sp.invoice_payload)
        return
    user_id, kopeks = int(parts[1]), int(parts[2])
    dep = await stars_svc.credit_star_payment(
        session, user_id=user_id, charge_id=sp.telegram_payment_charge_id,
        amount_kopeks=kopeks, stars=sp.total_amount,
    )
    # Коммитим ДО ответа юзеру: деньги с него Telegram уже взял, и падение
    # отправки сообщения не должно откатить зачисление (сессию откатывает
    # middleware по исключению).
    await session.commit()
    if not dep.credited or dep.user is None:
        return
    # Импорт внутри функции: balance.py тянет отсюда выставление счёта, и на
    # уровне модуля вышло бы кольцо.
    from bot.handlers.balance import notify_autopay, notify_deposit

    await notify_deposit(dep)
    logger.info(
        "Star payment {}: user {} +{} kopeks ({} stars)",
        sp.telegram_payment_charge_id, user_id, kopeks, sp.total_amount,
    )
    # Подписка истекла, автопродление включено — продлеваем сразу на свежие
    # деньги, как после оплаты через CryptoBot: ждать тика планировщика (до
    # 5 минут) после успешной оплаты юзер не обязан.
    ap = await billing.autopay_if_expired(session, dep.user)
    if ap is not None:
        await session.commit()
        await notify_autopay(dep.user, ap)
