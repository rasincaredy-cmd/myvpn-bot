"""Юридические экраны: тарифы, документы, согласие с условиями.

Отдельный роутер, потому что это требование платёжного провайдера, а не часть
продуктовой логики: тарифы и документы должны быть доступны из главного меню
всегда, без подписки и без оплаты.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.keyboards.inline import CB_LEGAL, back_to_menu
from bot.services.pricing import (
    TERM_DISCOUNTS,
    TERM_LABELS,
    fmt_rub,
    monthly_price_kopeks,
    term_price_kopeks,
)
from bot.texts import t

router = Router(name="legal")


def build_tariffs_text() -> str:
    """Экран тарифов. Все суммы считаются из pricing: поменяются цены в .env —
    экран обновится сам, без правки текстов."""
    base = monthly_price_kopeks(1, 1)
    first = monthly_price_kopeks(1, 0)

    lines = []
    for months in (3, 6, 12):
        price = term_price_kopeks(base, months)
        discount = TERM_DISCOUNTS.get(months, 0)
        lines.append(
            f"• {TERM_LABELS[months]} — <b>{fmt_rub(price)}</b> (−{discount}%)"
        )

    return t.tariffs.format(
        first=fmt_rub(first),
        extra=fmt_rub(settings.price_extra_device_rub * 100),
        base=fmt_rub(base),
        terms="\n".join(lines),
        trial_days=settings.trial_days,
        trial_devices=settings.trial_devices,
        trial_gb=settings.trial_traffic_gb,
    )


@router.callback_query(F.data == f"{CB_LEGAL}:tariffs")
async def cb_tariffs(call: CallbackQuery) -> None:
    await call.message.edit_text(build_tariffs_text(), reply_markup=back_to_menu())
    await call.answer()
