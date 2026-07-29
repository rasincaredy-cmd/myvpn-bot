"""Подписка: единый гейт (срок, лимиты устройств/обходов) и учёт трафика периода."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Peer, User, WdttAccess


async def sum_user_traffic(session: AsyncSession, user_id: int) -> int:
    """Суммарный трафик юзера за всё время = WG-пиры + доступы обхода БС.

    Отозванные тоже считаем — трафик уже потрачен. Разница с sub_traffic_base_bytes
    даёт расход за текущий период подписки."""
    peers = (await session.execute(
        select(func.coalesce(func.sum(Peer.traffic_used_bytes), 0))
        .where(Peer.user_id == user_id)
    )).scalar() or 0
    wdtt = (await session.execute(
        select(func.coalesce(func.sum(WdttAccess.traffic_used_bytes), 0))
        .where(WdttAccess.user_id == user_id)
    )).scalar() or 0
    return peers + wdtt


async def sub_traffic_used(session: AsyncSession, user: User) -> int:
    """Расход трафика за текущий период = Σ пиров − base (не меньше нуля)."""
    total = await sum_user_traffic(session, user.id)
    return max(0, total - (user.sub_traffic_base_bytes or 0))


async def set_subscription(
    session: AsyncSession,
    user_id: int,
    *,
    max_devices: int | None = None,
    max_bypass: int | None = None,
    expires_at: datetime | None = None,
    touch_expires: bool = False,
    traffic_limit_bytes: int | None = None,
    touch_traffic_limit: bool = False,
    reset_traffic_base: bool = False,
    mark_paid: bool = False,
    mark_trial: bool = False,
    term_months: int | None = None,
) -> None:
    """Обновляет подписку юзера. expires_at/traffic_limit меняются только при
    соответствующем touch_* (иначе None трактовался бы как «снять»). При продлении
    (reset_traffic_base=True) обнуляем расход периода: base := текущая Σ трафика."""
    values: dict = {}
    if max_devices is not None:
        values["sub_max_devices"] = max_devices
    if max_bypass is not None:
        values["sub_max_bypass"] = max_bypass
    if touch_expires:
        values["sub_expires_at"] = expires_at
        values["sub_warn_flags"] = 0  # новый срок → предупреждаем заново
    if touch_traffic_limit:
        values["sub_traffic_limit_bytes"] = traffic_limit_bytes
    if reset_traffic_base:
        values["sub_traffic_base_bytes"] = await sum_user_traffic(session, user_id)
    if mark_paid:
        values["is_trial"] = False
    if mark_trial:
        # Обратная mark_paid: админ выдал триал заново (Блок «Мелочи 2»).
        values["is_trial"] = True
    if term_months is not None:
        # Купленный срок — ориентир для автопродления. Только при покупке:
        # у выданного админом срока «сколько месяцев» не существует, и затирать
        # им прежний выбор юзера нельзя.
        values["sub_term_months"] = term_months
    if values:
        await session.execute(
            update(User).where(User.id == user_id).values(**values)
        )
