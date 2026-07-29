"""Юзеры: регистрация с авто-триалом, выборки для панели и рассылки, блокировка."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    full_name: str | None,
) -> User:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None:
        # Авто-триал новым юзерам (Блок 9): лимит устройств + срок из конфига.
        user = User(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            is_admin=tg_id in settings.admin_ids,
            sub_max_devices=settings.trial_devices,
            sub_expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.trial_days),
            sub_traffic_limit_bytes=(
                settings.trial_traffic_gb * 1024**3
                if settings.trial_traffic_gb else None
            ),
        )
        session.add(user)
        await session.flush()
    else:
        # Поддерживаем username/full_name в актуальном состоянии.
        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        # Админы могут добавляться/убираться через .env, синхронизируем флаг.
        is_admin = tg_id in settings.admin_ids
        if user.is_admin != is_admin:
            user.is_admin = is_admin
            changed = True
        if changed:
            await session.flush()
    return user


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Получить юзера по внутреннему id (PK), а не tg_id.
    Нужно для admin-панели: пир может принадлежать чужому юзеру (инвайт).
    """
    return await session.get(User, user_id)


async def count_users(session: AsyncSession) -> int:
    from sqlalchemy import func
    return (await session.execute(select(func.count(User.id)))).scalar() or 0


async def list_all_users(
    session: AsyncSession, offset: int = 0, limit: int = 10
) -> list[User]:
    # Сортировка сегментами: активная платная → активный триал → без подписки,
    # заблокированные — в самый низ. Внутри сегмента — по id.
    now = datetime.now(timezone.utc)
    active = (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now)
    paid = (User.is_trial.is_(False)) | (User.sub_expires_at.is_(None))
    tier = case(
        (active & paid, 0),
        (active, 1),
        else_=2,
    )
    result = await session.execute(
        select(User)
        .order_by(User.is_blocked.asc(), tier, User.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars())


async def list_all_users_for_broadcast(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.is_blocked.is_(False)).order_by(User.id)
    )
    return list(result.scalars())


async def list_users_by_ids(session: AsyncSession, ids: list[int]) -> list[User]:
    """Юзеры по списку внутренних id (для ручной рассылки), кроме заблокированных."""
    if not ids:
        return []
    return list((await session.execute(
        select(User).where(User.id.in_(ids)).where(User.is_blocked.is_(False)).order_by(User.id)
    )).scalars())


async def list_broadcast_targets(session: AsyncSession, target: str) -> list[User]:
    """Аудитория рассылки: all | active (активная подписка) | inactive (истёкшая/нет).
    Заблокированных не берём никогда."""
    now = datetime.now(timezone.utc)
    stmt = select(User).where(User.is_blocked.is_(False))
    if target == "active":
        stmt = stmt.where(
            (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now)
        )
    elif target == "inactive":
        stmt = stmt.where(User.sub_expires_at.isnot(None)).where(
            User.sub_expires_at <= now
        )
    return list((await session.execute(stmt.order_by(User.id))).scalars())


async def set_user_blocked(
    session: AsyncSession, user_id: int, blocked: bool
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(is_blocked=blocked)
    )
