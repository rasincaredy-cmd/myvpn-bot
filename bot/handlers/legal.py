"""Юридические экраны: тарифы, документы, согласие с условиями.

Отдельный роутер, потому что это требование платёжного провайдера, а не часть
продуктовой логики: тарифы и документы должны быть доступны из главного меню
всегда, без подписки и без оплаты.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.inline import (
    CB_LEGAL,
    back_to_menu,
    consent_kb,
    origin_of,
    tariffs_kb,
)
from bot.services.pricing import (
    PRESETS,
    TERM_DISCOUNTS,
    TERM_LABELS,
    best_value_key,
    fmt_rub,
    monthly_price_kopeks,
    per_device_kopeks,
    term_price_kopeks,
)
from bot.texts import t

router = Router(name="legal")


def build_tariffs_text() -> str:
    """Экран тарифов. Все суммы считаются из pricing: поменяются цены в .env —
    экран обновится сам, без правки текстов."""
    base = monthly_price_kopeks(1, 1)   # типовой тариф: устройство + подключение
    first = monthly_price_kopeks(1, 0)  # пол тарифа: одна позиция

    best = best_value_key()
    presets = []
    for preset in PRESETS:
        monthly = monthly_price_kopeks(preset.devices, preset.bypass)
        row = f"{preset.emoji} {preset.name} — <b>{fmt_rub(monthly)}/мес</b>"
        if preset.devices > 1:
            row += f", по {fmt_rub(per_device_kopeks(monthly, preset.devices))} за устройство"
        if preset.key == best:
            row += " 🔥"
        presets.append(row)

    lines = []
    for months in (3, 6, 12):
        price = term_price_kopeks(base, months)
        discount = TERM_DISCOUNTS.get(months, 0)
        lines.append(
            f"• {TERM_LABELS[months]} — <b>{fmt_rub(price)}</b> (−{discount}%)"
        )

    year = term_price_kopeks(base, 12)
    return t.tariffs.format(
        presets="\n".join(presets),
        year=fmt_rub(year),
        year_full=fmt_rub(base * 12),
        year_discount=TERM_DISCOUNTS[12],
        first=fmt_rub(first),
        extra_device=fmt_rub(settings.price_extra_device_rub * 100),
        extra_bypass=fmt_rub(settings.price_extra_bypass_rub * 100),
        base=fmt_rub(base),
        terms="\n".join(lines),
        trial_days=settings.trial_days,
        trial_devices=settings.trial_devices,
        trial_bypass=settings.trial_bypass,
        trial_gb=settings.trial_traffic_gb,
    )


@router.callback_query(F.data.in_({f"{CB_LEGAL}:tariffs", f"{CB_LEGAL}:tariffs:more"}))
async def cb_tariffs(call: CallbackQuery) -> None:
    """Витрина сервиса: цены доступны всегда, даже без подписки (требование
    платёжного провайдера).

    С 22.08.2026 у неё есть выход к покупке: до этого человек читал цены и
    оставался на экране с одной кнопкой «назад» — искать, где платят, ему
    приходилось самому.
    """
    await call.message.edit_text(
        build_tariffs_text(), reply_markup=tariffs_kb(origin_of(call.data))
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_LEGAL}:accept")
async def cb_accept(call: CallbackQuery, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await repo.accept_terms(session, user)
    await call.message.delete()
    # Импорт внутри функции: common.py импортирует клавиатуры, а те — конфиг;
    # на уровне модуля получился бы цикл.
    from bot.handlers.common import send_start_screens

    await send_start_screens(call.message, user, is_new=True, session=session)
    await call.answer()


@router.callback_query(F.data == f"{CB_LEGAL}:decline")
async def cb_decline(call: CallbackQuery) -> None:
    await call.message.edit_text(t.consent_declined)
    await call.answer()
