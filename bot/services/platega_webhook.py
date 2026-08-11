"""Приём уведомлений Platega об оплате (callback/вебхук).

Provider шлёт POST на наш адрес из личного кабинета и ждёт 200 в течение 60
секунд, иначе повторяет: через 5 минут и через 10. Тело: id транзакции, сумма,
валюта, статус, метод оплаты, наш payload; в заголовках — пара
x-merchantid/x-secret, по которой мы узнаём, что запрос от них.

Телу верим ТОЛЬКО в части «какая транзакция и что с ней стало». Кому и сколько
зачислять — берём из своей строки в базе: по id их API отдаёт и чужие
транзакции, а тело запроса вообще может прислать кто угодно, кто узнал адрес.

Сервер поднимается в bot/services/webserver.py; сюда вынесены проверка запроса
и обработка, чтобы их можно было проверить тестами без сети.
"""
from __future__ import annotations

import hmac
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import PlategaPayment
from bot.services import billing


def headers_ok(headers: Any) -> bool:
    """Пришёл ли запрос от Platega: пара ключей должна совпасть с нашей.

    Имена заголовков сверяем без учёта регистра (в документации они то
    `X-MerchantId`, то `x-merchantid`), значения — постоянным по времени
    сравнением: подбирать секрет по скорости ответа не дадим."""
    if not settings.platega_merchant_id or not settings.platega_secret:
        return False
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    merchant = str(lowered.get("x-merchantid", ""))
    secret = str(lowered.get("x-secret", ""))
    # Сравниваем БАЙТЫ: compare_digest на строках с не-ASCII бросает TypeError,
    # и один запрос с кириллицей в заголовке ронял бы приём уведомлений.
    return (
        hmac.compare_digest(merchant.encode(), settings.platega_merchant_id.encode())
        and hmac.compare_digest(secret.encode(), settings.platega_secret.encode())
    )


def _field(body: dict, *names: str) -> str | None:
    """Значение поля по любому из написаний (Id/id, Status/status)."""
    lowered = {str(k).lower(): v for k, v in body.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return str(value)
    return None


async def handle_payload(
    session: AsyncSession, body: dict
) -> "billing.DepositResult | None":
    """Обрабатывает тело уведомления. Коммит — на вызывающем.

    None — уведомление не про наш платёж (или платёж уже закрыт): отвечать
    провайдеру всё равно надо 200, иначе он будет ломиться ещё дважды.
    """
    tx_id = _field(body, "id", "transactionId")
    status = _field(body, "status")
    if not tx_id or not status:
        logger.warning("Platega webhook без id/статуса: {}", body)
        return None

    row = await repo.get_platega_payment_by_tx(session, tx_id)
    if row is None:
        # Чужая или очень старая транзакция. Денег это коснуться не может.
        logger.warning("Platega webhook по неизвестной транзакции {}", tx_id)
        return None

    if status == "CONFIRMED":
        dep = await billing.apply_paid_platega(session, row)
        if dep.credited:
            logger.info("Platega webhook: платёж {} зачислен", tx_id)
        return dep
    if status == "CANCELED":
        row.status = "canceled"
        await session.flush()
        return None
    if status == "CHARGEBACKED":
        # Деньги вернули плательщику. Баланс не трогаем (юзер мог их потратить),
        # разбирает админ — тревогу шлёт вызывающий, у него есть бот.
        row.status = "canceled"
        await session.flush()
        logger.error(
            "Platega webhook: возврат по платежу {} (юзер {}, {} копеек)",
            tx_id, row.user_id, row.amount_kopeks,
        )
        return None
    return None


def is_chargeback(body: dict) -> bool:
    """Нужна ли тревога админам по этому уведомлению."""
    return _field(body, "status") == "CHARGEBACKED"


async def find_payment(session: AsyncSession, body: dict) -> PlategaPayment | None:
    """Строка платежа по телу уведомления — для текста тревоги о возврате."""
    tx_id = _field(body, "id", "transactionId")
    if not tx_id:
        return None
    return await repo.get_platega_payment_by_tx(session, tx_id)
