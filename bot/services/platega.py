"""Клиент Platega (app.platega.io) — пополнение баланса картой, СБП и криптой.

Счёт создаём БЕЗ указания способа оплаты: провайдер отдаёт форму, где юзер сам
выбирает СБП, карту или крипту. Это умеет только v2-эндпоинт; у старого
`/transaction/process` способ оплаты обязателен.

Вебхуков нет: статус добираем поллингом планировщика и кнопкой «Проверить»
(как у Crypto Pay). Приём вебхуков требует домена с валидным сертификатом —
отдельная работа.

Дока: https://docs.platega.io/ — примеры там неполные, эндпоинты и формат
ответов проверены живыми запросами 11.08.2026 (см. спеку
docs/superpowers/specs/2026-08-11-platega-integraciya-design.md).
"""
from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger

from bot.config import settings

_API_BASE = "https://app.platega.io"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Сколько живёт неоплаченный счёт на стороне Platega (минуты). Провайдер
# возвращает это в expiresIn ("00:30:00"); держим константой, чтобы текст на
# экране не зависел от разбора чужой строки.
INVOICE_TTL_MINUTES = 30


class PlategaError(Exception):
    """Ошибка Platega: сеть, таймаут или ответ с кодом ошибки."""


def enabled() -> bool:
    """Настроена ли платёжка. Нужны ОБА ключа: с одним запрос получит 401."""
    return bool(settings.platega_merchant_id and settings.platega_secret)


def amount_to_rub(amount_kopeks: int) -> float:
    """Копейки → рубли для тела запроса. Единственное место, где деньги
    становятся дробными: внутри бота они всегда целые копейки."""
    return amount_kopeks / 100


def _headers() -> dict[str, str]:
    return {
        "X-MerchantId": settings.platega_merchant_id,
        "X-Secret": settings.platega_secret,
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, payload: dict | None = None) -> Any:
    """Запрос к API. Не-2xx или не-JSON → PlategaError."""
    if not enabled():
        raise PlategaError("PLATEGA_MERCHANT_ID/PLATEGA_SECRET не заданы")
    url = f"{_API_BASE}{path}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.request(
                method, url, json=payload, headers=_headers()
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    # message/code — служебные поля провайдера, секретов там нет.
                    message = data.get("message") if isinstance(data, dict) else data
                    raise PlategaError(f"Platega {resp.status}: {message or data}")
    except PlategaError:
        raise
    except Exception as exc:  # сеть/таймаут/не-JSON
        raise PlategaError(f"Platega недоступна: {exc}") from exc
    return data


async def create_payment(
    amount_kopeks: int, *, description: str, payload: str, return_url: str
) -> dict:
    """Создаёт счёт на сумму в копейках. Возвращает {transaction_id, url}.

    Способ оплаты НЕ передаём — юзер выберет его на форме провайдера.
    """
    data = await _request(
        "POST",
        "/v2/transaction/process",
        {
            "paymentDetails": {
                "amount": amount_to_rub(amount_kopeks),
                "currency": "RUB",
            },
            "description": description,
            "return": return_url,
            "failedUrl": return_url,
            "payload": payload,
        },
    )
    tx_id = data.get("transactionId") if isinstance(data, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    if not tx_id or not url:
        raise PlategaError(f"Platega вернула ответ без счёта: {data}")
    logger.info("Platega payment {} created ({} kopeks)", tx_id, amount_kopeks)
    return {"transaction_id": str(tx_id), "url": str(url)}


async def get_status(transaction_id: str) -> str:
    """Статус счёта: PENDING | CONFIRMED | CANCELED | CHARGEBACKED."""
    data = await _request("GET", f"/transaction/{transaction_id}")
    status = data.get("status") if isinstance(data, dict) else None
    if not status:
        raise PlategaError(f"Platega вернула ответ без статуса: {data}")
    return str(status)
