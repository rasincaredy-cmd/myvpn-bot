"""Глобальная статистика админ-панели."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    BalanceTx,
    Device,
    Invite,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
    WdttAccess,
)
from bot.keyboards.inline import CB_PANEL, back_to_panel

router = Router(name="admin_stats")


@router.callback_query(F.data == f"{CB_PANEL}:stats")
async def cb_panel_stats(call: CallbackQuery, session: AsyncSession) -> None:
    """Статистика под подписочную модель (Блок «Ревизия»): сегменты юзеров как
    в списке (💎🎁💤🔴), устройства/обходы вместо голых пиров, деньги за 30 дней.
    Таблицы маленькие — юзеров грузим целиком и сегментируем той же логикой,
    что и список (repo.user_sub_tier), чтобы цифры не расходились с иконками."""
    users = list((await session.execute(select(User))).scalars())
    users_total = len(users)
    seg = {"paid": 0, "trial": 0, "none": 0}
    blocked = admins = 0
    for u in users:
        if u.is_blocked:
            blocked += 1
            continue
        if u.is_admin:
            admins += 1
            continue
        seg[repo.user_sub_tier(u)] += 1

    async def _cnt(stmt) -> int:
        return (await session.execute(stmt)).scalar() or 0

    servers_total = await _cnt(select(func.count(Server.id)))
    servers_ready = await _cnt(
        select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
    )
    dev_active = await _cnt(
        select(func.count(Device.id)).where(Device.status == PeerStatus.ACTIVE)
    )
    dev_total = await _cnt(select(func.count(Device.id)))
    byp_active = await _cnt(
        select(func.count(WdttAccess.id)).where(WdttAccess.status == PeerStatus.ACTIVE)
    )
    byp_total = await _cnt(select(func.count(WdttAccess.id)))
    peers_active = await _cnt(
        select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
    )
    invites_pending = await _cnt(
        select(func.count(Invite.id)).where(Invite.used_at.is_(None))
    )
    # Конверсия триал→оплата: сколько юзеров хоть раз ПЛАТИЛИ за подписку
    # (kind='charge' — покупка/автопродление; депозиты и правки админа не в счёт).
    users_paid_ever = await _cnt(
        select(func.count(func.distinct(BalanceTx.user_id)))
        .where(BalanceTx.kind == "charge")
    )
    conv_pct = round(users_paid_ever * 100 / users_total) if users_total else 0
    # Деньги за 30 дней: живые пополнения (Crypto Pay) и списания за подписку.
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    from bot.services.pricing import fmt_rub
    dep_30d = (await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "deposit")
        .where(BalanceTx.created_at >= month_ago)
    )).scalar_one()
    charge_30d = -(await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "charge")
        .where(BalanceTx.created_at >= month_ago)
    )).scalar_one()

    # «4 активных / 5 всего» путало: в карточках юзеров суммарно видно 4 —
    # пятое устройство отозвано. Теперь отозванные названы явно (Блок «Мелочи»).
    def _split(active: int, total: int) -> str:
        rev = total - active
        return f"<b>{active}</b> активных" + (f" + {rev} отозвано = {total}" if rev else "")

    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b> — "
        f"💎 {seg['paid']} · 🎁 {seg['trial']} · 💤 {seg['none']} · "
        f"🔴 {blocked} · 👑 {admins}\n"
        f"📈 Конверсия: <b>{users_paid_ever}</b> из {users_total} покупали "
        f"подписку ({conv_pct}%)\n"
        f"💰 За 30 дней: пополнений <b>{fmt_rub(dep_30d)}</b>, "
        f"оплат подписки <b>{fmt_rub(charge_30d)}</b>\n\n"
        f"📱 Устройств: {_split(dev_active, dev_total)}\n"
        f"🛡 Обходов БС: {_split(byp_active, byp_total)}\n"
        f"📄 Конфигов на серверах: <b>{peers_active}</b>\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / {servers_total} всего\n"
        f"🎟 Инвайтов не погашено: <b>{invites_pending}</b>",
        reply_markup=back_to_panel(),
    )
    await call.answer()
