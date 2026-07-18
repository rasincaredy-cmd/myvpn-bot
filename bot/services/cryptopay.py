"""Клиент Crypto Pay API (@CryptoBot) — пополнение баланса (Блок «Баланс»).

Инвойсы создаём в фиате (RUB): юзер видит сумму в рублях, платит любой
криптой по курсу Crypto Pay. Вебхуков нет — статус добирается поллингом
планировщика и кнопкой «Проверить оплату» (getInvoices).

Докой: https://help.send.tg/en/articles/10279948-crypto-pay-api
"""
from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger

from bot.config import settings

_API_BASE = "https://pay.crypt.bot/api"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Сколько живёт неоплаченный инвойс на стороне Crypto Pay (сек).
INVOICE_TTL = 3600


class CryptoPayError(Exception):
    """Ошибка Crypto Pay API (сеть или ok=false)."""


def enabled() -> bool:
    return bool(settings.cryptopay_token)


async def _call(method: str, **params: Any) -> Any:
    """POST https://pay.crypt.bot/api/<method>; ok=false → CryptoPayError."""
    if not settings.cryptopay_token:
        raise CryptoPayError("CRYPTOPAY_TOKEN не задан")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.post(
                f"{_API_BASE}/{method}",
                json=params,
                headers={"Crypto-Pay-API-Token": settings.cryptopay_token},
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:  # сеть/таймаут/не-JSON
        raise CryptoPayError(f"Crypto Pay недоступен: {exc}") from exc
    if not data.get("ok"):
        # В error бывает name вроде PARAM_..._REQUIRED — секретов там нет.
        raise CryptoPayError(f"Crypto Pay: {data.get('error')}")
    return data["result"]


async def create_invoice(
    amount_kopeks: int, *, description: str, payload: str
) -> dict:
    """Создаёт RUB-инвойс. Возвращает {invoice_id, url} (url — bot_invoice_url)."""
    rub, kop = divmod(amount_kopeks, 100)
    amount = f"{rub}.{kop:02d}" if kop else str(rub)
    inv = await _call(
        "createInvoice",
        currency_type="fiat",
        fiat="RUB",
        amount=amount,
        description=description,
        payload=payload,
        expires_in=INVOICE_TTL,
    )
    logger.info("CryptoPay invoice {} created ({} RUB)", inv["invoice_id"], amount)
    return {"invoice_id": inv["invoice_id"], "url": inv["bot_invoice_url"]}


async def get_invoice_statuses(invoice_ids: list[int]) -> dict[int, str]:
    """invoice_id → status (active|paid|expired). Отсутствующие в ответе — пропущены."""
    if not invoice_ids:
        return {}
    result = await _call(
        "getInvoices", invoice_ids=",".join(str(i) for i in invoice_ids)
    )
    items = result.get("items", result) if isinstance(result, dict) else result
    return {int(i["invoice_id"]): i["status"] for i in items}
