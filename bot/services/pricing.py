"""Цены подписки (Блок «Баланс»). Все суммы — в КОПЕЙКАХ (никаких float у денег).

Модель: первая позиция (устройство ИЛИ резервное подключение) стоит
`price_first_rub` — это пол тарифа. Каждая следующая позиция прибавляется по
своей цене. Чем длиннее срок — тем больше скидка. Рубли из конфига, скидки и
округление — здесь.
"""
from __future__ import annotations

from bot.config import settings

# Срок (мес) → скидка в %. Итог округляется ВНИЗ до 10 ₽ — в пользу юзера,
# чтобы цены были «круглыми» (база 90₽: 3 мес 240, 6 мес 450, 12 мес 810).
TERM_DISCOUNTS: dict[int, int] = {1: 0, 3: 10, 6: 15, 12: 25}

# Срок словами. Живёт рядом со скидками, чтобы кнопки покупки, выдача админом
# и уведомления автопродления называли срок ОДИНАКОВО: юзер должен узнавать
# в списании ровно то, что выбирал.
TERM_LABELS: dict[int, str] = {1: "месяц", 3: "3 мес", 6: "полгода", 12: "год"}

# Дни, прибавляемые за «месяц» подписки.
DAYS_PER_MONTH = 30

_ROUND_TO = 10 * 100  # 10 ₽ в копейках

# Бонус за способ пополнения, % к зачисляемой сумме (этап D). Смысл — вести
# юзера к способу, который дешевле обходится сервису: карта 9 % и СБП 8 % самые
# дорогие, доплачивать за них нельзя. У звёзд своя наценка 25 %, и бонус поверх
# неё был бы взаимоисключающим.
DEPOSIT_BONUS_PERCENT: dict[str, int] = {
    "cryptobot": 4,
    "platega": 0,
    "stars": 0,
}


def deposit_bonus_kopeks(amount_kopeks: int, method: str) -> int:
    """Надбавка к зачислению за способ пополнения.

    Неизвестный способ — ноль: новый провайдер не должен начать раздавать
    бонусы просто потому, что его забыли внести в таблицу.
    """
    return amount_kopeks * DEPOSIT_BONUS_PERCENT.get(method, 0) // 100


def monthly_price_kopeks(max_devices: int, max_bypass: int) -> int:
    """₽/мес тарифа. Первая позиция — `price_first_rub`, каждая следующая
    прибавляется: устройство +`price_extra_device_rub`, резервное подключение
    +`price_extra_bypass_rub`.

    Первой считается устройство, если оно есть; если устройств ноль — первое
    подключение. Тариф без единой позиции (0+0) не существует — за ним стоит
    ошибка вызывающего.

    Формула только складывает, поэтому дешевле пола тариф быть не может. У
    прежней база покрывала позиции, а отказ от них вычитался, и при
    base < сумма доплат тариф уходил в минус — за этим приходилось следить
    отдельным правилом. Правило исчезло вместе с вычитанием.
    """
    if max_devices + max_bypass < 1:
        raise ValueError("тариф без устройств и обходов не продаётся")  # wording: ok
    if max_devices >= 1:
        rub = (
            settings.price_first_rub
            + (max_devices - 1) * settings.price_extra_device_rub
            + max_bypass * settings.price_extra_bypass_rub
        )
    else:
        rub = (
            settings.price_first_rub
            + (max_bypass - 1) * settings.price_extra_bypass_rub
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
