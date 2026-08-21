"""Общее для всех экранов админ-панели: рендер карточки юзера и мелкие хелперы.

Живёт отдельным модулем, потому что карточку юзера рисуют сразу три экрана
(открытие юзера, тумблер «друг», блокировка), а строку про триал — ещё и
карточка подписки. Держать это в одном из них значило бы импорт «вбок» между
соседними модулями и риск циклов.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.texts import ui
from bot.services import amnezia
from bot.utils.timefmt import as_utc, fmt_msk

# Размер страницы в списке юзеров.
PER_PAGE = 10

TIER_LABEL = {
    "paid": "💎 Платная подписка",
    "trial": "🎁 Триал",
    "none": "💤 Без подписки",
}


def trial_line(user) -> str:
    """Триал юзера словами (Блок «Мелочи 2»). Триал выдаётся автоматически при
    регистрации, а флаг is_trial снимается, как только админ задаёт срок или
    юзер платит, — поэтому is_trial=False читается как «триал уже позади»."""
    if not user.is_trial:
        return "использован (сейчас платная)"
    if user.sub_expires_at is None:
        return "🎁 идёт (бессрочный)"
    exp = as_utc(user.sub_expires_at)
    if exp > datetime.now(timezone.utc):
        return f"🎁 идёт, до {fmt_msk(user.sub_expires_at)} МСК"
    return f"использован, истёк {fmt_msk(user.sub_expires_at, with_time=False)}"


async def user_card_text(session: AsyncSession, user) -> str:
    devices = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    tier = repo.user_sub_tier(user)
    if user.sub_expires_at is None:
        srok = "бессрочно"
    else:
        exp = as_utc(user.sub_expires_at)
        srok = (
            f"{'до' if exp > datetime.now(timezone.utc) else 'истекла'} "
            f"{fmt_msk(user.sub_expires_at)} МСК"
        )
    trf = amnezia.fmt_traffic_line(
        await repo.sub_traffic_used(session, user), user.sub_traffic_limit_bytes,
        tier == "none",
    )
    status = (
        "🔴 Заблокирован" if user.is_blocked
        else ("👑 Админ" if user.is_admin else TIER_LABEL[tier])
    )
    if user.is_vip:
        status += " · ⭐ друг"
    from bot.services.pricing import fmt_rub
    return (
        # Имя из профиля Telegram — чужой текст. Без экранирования админ не
        # смог бы ОТКРЫТЬ карточку юзера с угловой скобкой в имени: Telegram
        # такое сообщение не принимает (аудит 20.08.2026).
        f"👤 <b>{ui.safe(user.full_name) or '—'}</b>\n"
        f"• Username: {('@' + user.username) if user.username else '—'}\n"
        f"• Telegram ID: <code>{user.tg_id}</code>\n"
        f"• Статус: {status}\n"
        f"• Устройства: <b>{devices}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{srok}</b>\n"
        f"• Триал: <b>{trial_line(user)}</b>\n"
        f"• Трафик: <b>{trf}</b>\n"
        f"• Баланс: <b>{fmt_rub(user.balance_kopeks)}</b>\n"
        f"• С нами с: {fmt_msk(user.created_at, with_time=False)}"
    )
