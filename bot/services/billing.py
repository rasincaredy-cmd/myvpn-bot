"""Биллинг (Блок «Баланс»): зачисление оплат и покупка подписки с баланса.

Деньги двигаются ТОЛЬКО через repo.add_balance_tx (журнал). Уведомления в
Telegram — на вызывающем (хендлер/планировщик), как в revive.py: сервис не
знает контекста. Коммит — тоже на вызывающем.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import CryptoInvoice, User
from bot.services import revive as revive_svc
from bot.services.pricing import (
    DAYS_PER_MONTH,
    monthly_price_kopeks,
    term_price_kopeks,
)


@dataclass
class DepositResult:
    """Итог зачисления инвойса — для уведомлений на вызывающем."""
    credited: bool                 # False — инвойс уже был зачислен (идемпотентность)
    user: User | None = None
    amount_kopeks: int = 0
    referrer: User | None = None   # кому упала реф-награда (None — рефа нет)
    ref_reward_kopeks: int = 0


async def apply_paid_invoice(
    session: AsyncSession, inv: CryptoInvoice
) -> DepositResult:
    """Зачисляет ОПЛАЧЕННЫЙ инвойс: баланс юзеру + реф-награда пригласившему.

    Идемпотентно: повторный вызов по уже paid-строке — no-op (кнопка «Проверить»
    и поллинг планировщика могут наперегонки увидеть одну оплату)."""
    if inv.status == "paid":
        return DepositResult(credited=False)
    inv.status = "paid"
    inv.paid_at = datetime.now(timezone.utc)

    user = await repo.get_user_by_id(session, inv.user_id)
    await repo.add_balance_tx(
        session, inv.user_id, inv.amount_kopeks, "deposit",
        note=f"Пополнение (инвойс {inv.invoice_id})",
    )
    logger.info(
        "Deposit: user {} +{} kopeks (invoice {})",
        inv.user_id, inv.amount_kopeks, inv.invoice_id,
    )

    referrer = None
    reward = 0
    if user is not None and user.referrer_id is not None:
        reward = inv.amount_kopeks * settings.referral_percent // 100
        referrer = await repo.get_user_by_id(session, user.referrer_id)
        if referrer is not None and reward > 0:
            await repo.add_balance_tx(
                session, referrer.id, reward, "ref",
                note=f"{settings.referral_percent}% с пополнения реферала",
            )
            logger.info(
                "Ref reward: user {} +{} kopeks (referral {})",
                referrer.id, reward, user.id,
            )
        else:
            referrer, reward = None, 0

    if user is not None:
        await session.refresh(user)
    return DepositResult(
        credited=True, user=user, amount_kopeks=inv.amount_kopeks,
        referrer=referrer, ref_reward_kopeks=reward,
    )


@dataclass
class ChargeResult:
    ok: bool                       # False — не хватило баланса
    price_kopeks: int = 0
    new_expires_at: datetime | None = None
    revive: "revive_svc.ReviveResult | None" = None
    missing_kopeks: int = 0        # сколько не хватило (при ok=False)


async def charge_and_extend(
    session: AsyncSession, user: User, months: int,
    *, max_devices: int | None = None, max_bypass: int | None = None,
) -> ChargeResult:
    """Покупка/продление подписки с баланса на `months` месяцев. Тариф — текущий
    у юзера или явный (max_devices/max_bypass): смена тарифа происходит ТОЛЬКО
    в момент покупки, иначе апгрейд лимитов был бы бесплатным до конца срока.

    Срок прибавляется к остатку (активная подписка не сгорает), у платной
    подписки лимит трафика снимается (продаём устройства, не гигабайты),
    отозванные по истечению устройства оживают (ревайв)."""
    devices = max_devices if max_devices is not None else user.sub_max_devices
    bypass = max_bypass if max_bypass is not None else user.sub_max_bypass
    price = term_price_kopeks(monthly_price_kopeks(devices, bypass), months)
    if user.balance_kopeks < price:
        return ChargeResult(
            ok=False, price_kopeks=price,
            missing_kopeks=price - user.balance_kopeks,
        )

    await repo.add_balance_tx(
        session, user.id, -price, "charge",
        note=f"Подписка {months} мес ({devices} устр., {bypass} обход)",
    )
    now = datetime.now(timezone.utc)
    base = user.sub_expires_at
    if base is not None and base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    start = base if base is not None and base > now else now
    new_expiry = start + timedelta(days=DAYS_PER_MONTH * months)
    await repo.set_subscription(
        session, user.id,
        max_devices=devices, max_bypass=bypass,
        expires_at=new_expiry, touch_expires=True,
        reset_traffic_base=True, mark_paid=True,
        traffic_limit_bytes=None, touch_traffic_limit=True,
    )
    await session.refresh(user)
    logger.info(
        "Sub charge: user {} -{} kopeks, {} mo, until {}",
        user.id, price, months, new_expiry.isoformat(),
    )

    rv = await revive_svc.revive_devices_for_user(session, user)
    return ChargeResult(
        ok=True, price_kopeks=price, new_expires_at=new_expiry, revive=rv
    )


async def autopay_if_expired(
    session: AsyncSession, user: User
) -> ChargeResult | None:
    """Автопродление, если подписка УЖЕ истекла: месяц текущего тарифа с баланса.

    Общая точка для планировщика (тик по истечению) и мгновенного продления
    сразу после пополнения (кнопка «Проверить», ручное начисление админом) —
    чтобы юзер не ждал тика до 5 минут с деньгами на счету. None — продлевать
    не надо (подписка активна/бессрочная, autopay выключен) или не хватило
    баланса (charge_and_extend при нехватке ничего не пишет — отката не нужно).
    Crypto Pay не требуется: списание идёт с баланса, а его могли пополнить
    и руками (kind=admin за перевод на карту)."""
    if not user.autopay or user.sub_expires_at is None:
        return None
    exp = user.sub_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp > datetime.now(timezone.utc):
        return None
    res = await charge_and_extend(session, user, 1)
    return res if res.ok else None
