"""Цены подписки (Блок «Баланс»). Все суммы — в КОПЕЙКАХ (никаких float у денег).

Модель: база (1 устройство + 1 обход БС) + доплата за каждое следующее
устройство/обход; чем длиннее срок — тем больше скидка. Рубли из конфига,
скидки и округление — здесь.
"""
from __future__ import annotations

from bot.config import settings

# Срок (мес) → скидка в %. Итог округляется ВНИЗ до 10 ₽ — в пользу юзера,
# чтобы цены были «круглыми» (база 90₽: 3 мес 240, 6 мес 450, 12 мес 810).
TERM_DISCOUNTS: dict[int, int] = {1: 0, 3: 10, 6: 15, 12: 25}

# Дни, прибавляемые за «месяц» подписки.
DAYS_PER_MONTH = 30

_ROUND_TO = 10 * 100  # 10 ₽ в копейках


def monthly_price_kopeks(max_devices: int, max_bypass: int) -> int:
    """₽/мес тарифа: база покрывает 1 устройство и 1 обход, дальше — доплата."""
    extra_dev = max(0, max_devices - 1)
    extra_byp = max(0, max_bypass - 1)
    rub = (
        settings.price_base_rub
        + extra_dev * settings.price_extra_device_rub
        + extra_byp * settings.price_extra_bypass_rub
    )
    return rub * 100


def term_price_kopeks(monthly_kopeks: int, months: int) -> int:
    """Цена за срок со скидкой TERM_DISCOUNTS, округление вниз до 10 ₽."""
    discount = TERM_DISCOUNTS.get(months, 0)
    raw = monthly_kopeks * months * (100 - discount) // 100
    return max(_ROUND_TO, raw // _ROUND_TO * _ROUND_TO)


def fmt_rub(kopeks: int) -> str:
    """Копейки → строка «90 ₽» / «−90.50 ₽» (копейки видны, только если есть)."""
    sign = "−" if kopeks < 0 else ""
    kopeks = abs(kopeks)
    rub, kop = divmod(kopeks, 100)
    return f"{sign}{rub}.{kop:02d} ₽" if kop else f"{sign}{rub} ₽"
