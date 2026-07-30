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
    TERM_DISCOUNTS,
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
    missing_kopeks: int = 0        # сколько не хватило (при ok=False; при
                                   # срезанном автопродлении — до полного срока)
    months: int = 0                # на сколько месяцев продлили фактически
    # Автопродление: на сколько юзер рассчитывал (его прошлая покупка) и сколько
    # это стоило бы. months < wanted_months ⇒ срок срезан из-за нехватки денег,
    # об этом обязательно сказать юзеру.
    wanted_months: int = 0
    wanted_price_kopeks: int = 0


async def _extend(
    session: AsyncSession, user: User, months: int, devices: int, bypass: int
) -> tuple[datetime, "revive_svc.ReviveResult"]:
    """Общая механика продления для покупки и админской выдачи: срок прибавляется
    к остатку (активная подписка не сгорает), подписка становится платной, лимит
    трафика снимается, отозванные по истечению устройства оживают."""
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
        term_months=months,
    )
    await session.refresh(user)
    rv = await revive_svc.revive_devices_for_user(session, user)
    return new_expiry, rv


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
    # Последний рубеж против пустого/кривого тарифа: сюда ходят конструктор,
    # автопродление и будущие вызовы — доверять валидации хендлера нельзя.
    if devices < 0 or bypass < 0 or devices + bypass < 1:
        logger.warning(
            "Charge rejected: user {} empty tariff {}/{}", user.id, devices, bypass
        )
        return ChargeResult(ok=False)
    price = term_price_kopeks(monthly_price_kopeks(devices, bypass), months)
    if user.balance_kopeks < price:
        return ChargeResult(
            ok=False, price_kopeks=price,
            missing_kopeks=price - user.balance_kopeks,
        )

    await repo.add_balance_tx(
        session, user.id, -price, "charge",
        note=f"Подписка {months} мес (устройств: {devices}, обходов: {bypass})",
    )
    new_expiry, rv = await _extend(session, user, months, devices, bypass)
    logger.info(
        "Sub charge: user {} -{} kopeks, {} mo, until {}",
        user.id, price, months, new_expiry.isoformat(),
    )
    return ChargeResult(
        ok=True, price_kopeks=price, new_expires_at=new_expiry, revive=rv,
        months=months, wanted_months=months, wanted_price_kopeks=price,
    )


async def grant_term(
    session: AsyncSession, user: User, months: int
) -> ChargeResult:
    """Админ ВЫДАЁТ подписку на один из продаваемых сроков — без денег.

    Отличие от charge_and_extend ровно одно: ничего не списывается и в журнале
    баланса не появляется строки (это подарок/компенсация, а не покупка). Всё
    остальное как у покупки, включая запоминание срока: выдали год — дальше и
    автопродление возьмёт год.

    Срок обязан быть из прайса (TERM_DISCOUNTS): произвольные периоды остаются
    за «📅 Задать срок», который живёт своей логикой (дата/период/бессрочно)."""
    if months not in TERM_DISCOUNTS:
        raise ValueError(f"срок {months} мес не продаётся")
    new_expiry, rv = await _extend(
        session, user, months, user.sub_max_devices, user.sub_max_bypass
    )
    logger.info(
        "Sub granted by admin: user {}, {} mo, until {}",
        user.id, months, new_expiry.isoformat(),
    )
    return ChargeResult(
        ok=True, price_kopeks=0, new_expires_at=new_expiry, revive=rv,
        months=months, wanted_months=months,
    )


def plan_autopay(user: User) -> tuple[int, int, int, int] | None:
    """Что спишет автопродление, БЕЗ списания: (срок, цена, хотели, цена полного).

    Отдельно от самого продления, потому что этот же расчёт нужен предупреждению
    «подписка скоро истечёт» — юзер должен заранее знать сумму. Срок истечения
    здесь НЕ проверяется: предупреждение шлётся, пока подписка ещё жива.

    None — автопродление не сработает: выключено, подписка бессрочная, тариф
    пустой или денег не хватает даже на месяц."""
    if not user.autopay or user.sub_expires_at is None:
        return None
    # Пустой тариф (админ выставил 0/0) не автопродлеваем: списывать деньги за
    # подписку, в которой нельзя создать ни устройство, ни обход, — нечестно.
    if user.sub_max_devices + user.sub_max_bypass < 1:
        return None
    wanted = user.sub_term_months or 1
    monthly = monthly_price_kopeks(user.sub_max_devices, user.sub_max_bypass)
    wanted_price = term_price_kopeks(monthly, wanted)
    # Максимальный доступный срок, но не длиннее купленного: цена срока не
    # линейна (скидки), поэтому перебираем варианты, а не делим сумму.
    months = next(
        (m for m in sorted(TERM_DISCOUNTS, reverse=True)
         if m <= wanted and term_price_kopeks(monthly, m) <= user.balance_kopeks),
        None,
    )
    if months is None:
        return None
    return months, term_price_kopeks(monthly, months), wanted, wanted_price


async def autopay_if_expired(
    session: AsyncSession, user: User
) -> ChargeResult | None:
    """Автопродление, если подписка УЖЕ истекла: списываем текущий тариф с баланса.

    Общая точка для планировщика (тик по истечению) и мгновенного продления
    сразу после пополнения (кнопка «Проверить», ручное начисление админом) —
    чтобы юзер не ждал тика до 5 минут с деньгами на счету. None — продлевать
    не надо (подписка активна/бессрочная, autopay выключен) или не хватило
    баланса (charge_and_extend при нехватке ничего не пишет — отката не нужно).
    Crypto Pay не требуется: списание идёт с баланса, а его могли пополнить
    и руками (kind=admin за перевод на карту).

    Срок — тот же, что юзер покупал (sub_term_months), чтобы не терялась скидка
    за длинный срок. Денег на полный срок не хватило — берём максимальный, на
    который хватает: пусть подписка будет короче, чем ждали, но не оборвётся.
    Вызывающий обязан сказать юзеру про срезанный срок (months < wanted_months).
    """
    if user.sub_expires_at is None:
        return None
    exp = user.sub_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp > datetime.now(timezone.utc):
        return None

    plan = plan_autopay(user)
    if plan is None:              # выключено / пустой тариф / денег не хватает
        return None
    months, _price, wanted, wanted_price = plan
    balance_before = user.balance_kopeks

    res = await charge_and_extend(session, user, months)
    if not res.ok:
        return None
    res.wanted_months = wanted
    res.wanted_price_kopeks = wanted_price
    # Срок срезан: показываем, сколько не хватило на полный, — юзер поймёт,
    # на сколько пополнить, чтобы в следующий раз продлилось как раньше.
    res.missing_kopeks = (
        max(0, wanted_price - balance_before) if months < wanted else 0
    )
    return res
