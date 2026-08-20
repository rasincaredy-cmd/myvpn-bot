"""Глобальная статистика админ-панели."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    BalanceTx,
    Device,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
    WdttAccess,
)
from bot.keyboards.inline import CB_PANEL, back_to_panel

router = Router(name="admin_stats")


@dataclass
class MoneyStats:
    """Деньги и конверсия по ЧУЖИМ людям."""

    users_counted: int      # знаменатель конверсии: все, кроме своих
    staff_counted: int      # свои: админы + помеченные служебными
    users_paid: int         # из чужих — сколько хоть раз платили
    deposited_30d: int
    charged_30d: int


async def collect_money_stats(session: AsyncSession) -> MoneyStats:
    """Считает деньги и конверсию, не видя своих.

    Отдельной функцией, а не строчками внутри хендлера: экран статистики в
    тесте не поднять, а «кого считаем» — ровно то, что надо проверять. Свои —
    это админы (автоматически) и аккаунты с пометкой «служебный»: друзья,
    платящие вне бота, и проверяющие от платёжного провайдера. Их покупки и
    пополнения — перекладывание из кармана в карман, а не выручка.
    """
    own = select(User.id).where(or_(User.is_admin.is_(True), User.is_staff.is_(True)))
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)

    async def _one(stmt) -> int:
        return (await session.execute(stmt)).scalar_one()

    async def _sum(kind: str) -> int:
        return await _one(
            select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
            .where(BalanceTx.kind == kind)
            .where(BalanceTx.created_at >= month_ago)
            .where(BalanceTx.user_id.not_in(own))
        )

    return MoneyStats(
        users_counted=await _one(
            select(func.count(User.id)).where(User.id.not_in(own))
        ),
        staff_counted=await _one(
            select(func.count(User.id)).where(User.id.in_(own))
        ),
        users_paid=await _one(
            select(func.count(func.distinct(BalanceTx.user_id)))
            .where(BalanceTx.kind == "charge")
            .where(BalanceTx.user_id.not_in(own))
        ),
        deposited_30d=await _sum("deposit"),
        # Списания хранятся отрицательными — на экран идёт положительная сумма.
        charged_30d=-await _sum("charge"),
    )


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
    # Конверсия и деньги — только по чужим людям: свои тесты и бесплатные
    # друзья не должны выдавать себя за продажи (см. collect_money_stats).
    from bot.services.pricing import fmt_rub
    money = await collect_money_stats(session)
    conv_pct = (
        round(money.users_paid * 100 / money.users_counted)
        if money.users_counted else 0
    )

    # «4 активных / 5 всего» путало: в карточках юзеров суммарно видно 4 —
    # пятое устройство отозвано. Теперь отозванные названы явно (Блок «Мелочи»).
    def _split(active: int, total: int) -> str:
        rev = total - active
        return f"<b>{active}</b> активных" + (f" + {rev} отозвано = {total}" if rev else "")

    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b>"
        + (f" (служебных {money.staff_counted})" if money.staff_counted else "")
        + " — "
        f"💎 {seg['paid']} · 🎁 {seg['trial']} · 💤 {seg['none']} · "
        f"🔴 {blocked} · 👑 {admins}\n"
        f"📈 Конверсия: <b>{money.users_paid}</b> из {money.users_counted} "
        f"покупали подписку ({conv_pct}%)\n"
        f"💰 За 30 дней: пополнений <b>{fmt_rub(money.deposited_30d)}</b>, "
        f"оплат подписки <b>{fmt_rub(money.charged_30d)}</b>\n\n"
        f"📱 Устройств: {_split(dev_active, dev_total)}\n"
        f"🛡 Обходов БС: {_split(byp_active, byp_total)}\n"
        f"📄 Конфигов на серверах: <b>{peers_active}</b>\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / {servers_total} всего",
        reply_markup=back_to_panel(),
    )
    await call.answer()
