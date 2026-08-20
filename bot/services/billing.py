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
from bot.db.models import AuditAction, CryptoInvoice, PlategaPayment, User
from bot.services import revive as revive_svc
from bot.services.pricing import (
    DAYS_PER_MONTH,
    DEPOSIT_BONUS_PERCENT,
    DEPOSIT_METHOD_LABELS,
    TERM_DISCOUNTS,
    convert_remaining,
    tariff_ceiling,
    deposit_bonus_kopeks,
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


async def credit_deposit(
    session: AsyncSession, *, user_id: int, amount_kopeks: int,
    method: str, note: str, audit_details: str = "Пополнение баланса",
) -> DepositResult:
    """Зачисляет пополнение любым способом: баланс, бонус за способ, журнал,
    реф-награда пригласившему.

    Общая для CryptoBot и звёзд (этап D). Проверку «не зачисляли ли уже» делает
    вызывающий — она у каждого способа своя: у инвойса это его статус, у звёзд
    строка платежа по charge_id. Коммит — тоже на вызывающем.
    """
    user = await repo.get_user_by_id(session, user_id)
    await repo.add_balance_tx(session, user_id, amount_kopeks, "deposit", note=note)
    logger.info("Deposit ({}): user {} +{} kopeks", method, user_id, amount_kopeks)
    # Бонус за способ — ОТДЕЛЬНАЯ строка, а не надбавка внутри пополнения: в
    # статистике «пополнений за 30 дней» должны стоять деньги, которые сервис
    # правда получил, а не они же плюс подарок.
    bonus = deposit_bonus_kopeks(amount_kopeks, method)
    if bonus:
        await repo.add_balance_tx(
            session, user_id, bonus, "bonus",
            note=(
                f"Бонус {DEPOSIT_BONUS_PERCENT[method]}% за пополнение "
                f"{DEPOSIT_METHOD_LABELS.get(method, method)}"
            ),
        )
    # Журнал пишем здесь, а не в хендлере: сюда же приходит поллинг планировщика,
    # и оплата, увиденная им (а не кнопкой «Проверить»), обязана попасть в
    # историю. Задвоения нет — выше стоит проверка вызывающего.
    await repo.log_action(
        session, AuditAction.BALANCE_TOPUP,
        actor_tg_id=user.tg_id if user is not None else None,
        target_user_id=user_id,
        amount_kopeks=amount_kopeks,
        details=audit_details,
    )

    referrer = None
    reward = 0
    if user is not None and user.referrer_id is not None:
        reward = amount_kopeks * settings.referral_percent // 100
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
            # Событие вешаем на ПРИГЛАСИВШЕГО: спор про реф-проценты разбирается
            # по его карточке. Инициатора нет — начислил бот, а не человек.
            await repo.log_action(
                session, AuditAction.REFERRAL_REWARD,
                target_user_id=referrer.id,
                amount_kopeks=reward,
                details=f"{settings.referral_percent}% с пополнения реферала",
            )
        else:
            referrer, reward = None, 0

    if user is not None:
        await session.refresh(user)
    return DepositResult(
        credited=True, user=user, amount_kopeks=amount_kopeks,
        referrer=referrer, ref_reward_kopeks=reward,
    )


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
    return await credit_deposit(
        session, user_id=inv.user_id, amount_kopeks=inv.amount_kopeks,
        method="cryptobot", note=f"Пополнение (инвойс {inv.invoice_id})",
    )


async def apply_paid_platega(
    session: AsyncSession, row: PlategaPayment
) -> DepositResult:
    """Зачисляет ОПЛАЧЕННЫЙ счёт Platega: баланс юзеру + реф-награда пригласившему.

    Идемпотентно: повторный вызов по уже paid-строке — no-op (кнопка «Проверить»
    и поллинг планировщика могут наперегонки увидеть одну оплату).

    Юзер и сумма берутся из строки, а не из ответа провайдера: их API по id
    отдаёт и чужие транзакции, доверять ему как источнику правды нельзя."""
    if row.status == "paid":
        return DepositResult(credited=False)
    row.status = "paid"
    row.paid_at = datetime.now(timezone.utc)
    return await credit_deposit(
        session, user_id=row.user_id, amount_kopeks=row.amount_kopeks,
        method="platega", note=f"Пополнение картой (счёт {row.transaction_id})",
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


def _expiry_aware(user: User) -> datetime | None:
    """Срок окончания как aware-datetime. SQLite отдаёт naive — сравнивать
    такое с aware нельзя, а грабли эти в проекте уже случались."""
    exp = user.sub_expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp


def remaining_seconds(user: User) -> int:
    """Сколько секунд подписки осталось. Истёкшая и бессрочная — ноль.

    Бессрочная даёт ноль не потому, что её нет, а потому что «остаток
    бесконечности» пересчитать не во что: такие подписки в смену тарифа и в
    покупку не пускают выше по стеку.
    """
    exp = _expiry_aware(user)
    if exp is None:
        return 0
    return max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))


def remaining_seconds_after_switch(user: User, devices: int, bypass: int) -> int:
    """Остаток подписки, пересчитанный в тариф `devices`+`bypass`.

    Триал НЕ пересчитывается: экран пробного периода обещает дословно
    «оплаченный срок прибавится к пробному, ни дня не сгорит», а пересчёт
    подаренных дней по дорогому тарифу это обещание бы нарушил. Подарок в
    trial_days эксплойтом не является — он один на юзера и короткий.

    Тариф 0+0 (админ обнулил лимиты) тоже оставляем как есть: цены у него нет,
    делить не на что, а съедать человеку время из-за админской правки нельзя.
    """
    left = remaining_seconds(user)
    if left <= 0 or user.is_trial:
        return left
    if user.sub_max_devices + user.sub_max_bypass < 1:
        return left
    old_monthly = monthly_price_kopeks(user.sub_max_devices, user.sub_max_bypass)
    new_monthly = monthly_price_kopeks(devices, bypass)
    return convert_remaining(left, old_monthly, new_monthly)


@dataclass
class TariffChangeResult:
    """Итог смены тарифа без оплаты. reason заполняется только при ok=False —
    хендлер по нему выбирает объяснение юзеру."""
    ok: bool
    reason: str = ""
    new_expires_at: datetime | None = None
    old_days: int = 0          # сколько дней было до смены
    new_days: int = 0          # сколько станет после
    used_devices: int = 0      # при reason="in_use" — сколько занято сейчас
    used_bypass: int = 0


async def change_tariff(
    session: AsyncSession, user: User, *, max_devices: int, max_bypass: int,
    dry_run: bool = False,
) -> TariffChangeResult:
    """Смена тарифа БЕЗ оплаты: остаток пересчитывается в новый тариф.

    `dry_run=True` — посчитать и проверить всё то же самое, но ничего не
    записывать. Экран тарифа рисуется именно так: и текст, и подпись кнопки
    берут число дней из одного расчёта, поэтому разойтись в цифрах они не
    могут. Отдельная «функция предпросмотра» рядом с этой разошлась бы с ней
    на первой же правке правил.

    Дороже тариф — срок сокращается, дешевле — растягивается. Денег не
    трогаем совсем: ни списаний, ни возвратов на баланс. Возврат деньгами тут
    был бы вторым денежным потоком, который пришлось бы отдельно защищать от
    накрутки, — а пересчёт времени защищать не нужно, он симметричен и
    округляется вниз.

    Не трогаем и `sub_term_months`: это то, что юзер ПОКУПАЛ, ориентир
    автопродления. Смена тарифа покупкой не является.

    Коммит — на вызывающем, как и у остальных функций сервиса.
    """
    if max_devices < 0 or max_bypass < 0 or max_devices + max_bypass < 1:
        return TariffChangeResult(ok=False, reason="empty")
    # Потолок проверяем и здесь: callback_data подделывается руками, а до
    # 20.08.2026 предел 10/10 жил только в хендлере покупки.
    ceil_dev, ceil_byp = tariff_ceiling(user.sub_max_devices, user.sub_max_bypass)
    if max_devices > ceil_dev or max_bypass > ceil_byp:
        return TariffChangeResult(ok=False, reason="too_big")
    if max_devices == user.sub_max_devices and max_bypass == user.sub_max_bypass:
        return TariffChangeResult(ok=False, reason="same")
    if user.is_trial:
        return TariffChangeResult(ok=False, reason="trial")
    if _expiry_aware(user) is None:
        return TariffChangeResult(ok=False, reason="perpetual")

    left = remaining_seconds(user)
    if left <= 0:
        return TariffChangeResult(ok=False, reason="expired")

    # Тариф ниже текущего ПОТРЕБЛЕНИЯ не продаём: активные устройства сверх
    # нового лимита продолжили бы работать (лимит проверяется только при
    # добавлении), и понижение стало бы способом получить больше за меньше.
    # Та же проверка стоит на покупке — здесь она не дубль, а свой рубеж:
    # хендлеру доверять нельзя, сюда ходят два разных экрана.
    used_dev = await repo.count_active_devices(session, user.id)
    used_byp = await repo.count_active_wdtt_for_user(session, user.id)
    if max_devices < used_dev or max_bypass < used_byp:
        return TariffChangeResult(
            ok=False, reason="in_use", used_devices=used_dev, used_bypass=used_byp
        )

    kept = remaining_seconds_after_switch(user, max_devices, max_bypass)
    if kept < 86400:
        # Апгрейд, после которого остаются часы, — это обнуление подписки одним
        # тапом. Лучше отказать и назвать причину, чем оставить человека без VPN
        # с формально выполненной просьбой.
        return TariffChangeResult(
            ok=False, reason="too_short",
            old_days=left // 86400, new_days=kept // 86400,
        )

    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=kept)
    if dry_run:
        return TariffChangeResult(
            ok=True, new_expires_at=new_expiry,
            old_days=left // 86400, new_days=kept // 86400,
        )

    # Прежний тариф запоминаем ДО записи: после set_subscription + refresh в
    # полях юзера уже новые числа, и лог показывал бы «2+0 -> 2+0».
    was = f"{user.sub_max_devices}+{user.sub_max_bypass}"
    await repo.set_subscription(
        session, user.id,
        max_devices=max_devices, max_bypass=max_bypass,
        expires_at=new_expiry, touch_expires=True,
    )
    await session.refresh(user)
    logger.info(
        "Tariff changed by user {}: {} -> {}+{}, until {}",
        user.id, was, max_devices, max_bypass, new_expiry.isoformat(),
    )
    await repo.log_action(
        session, AuditAction.TARIFF_CHANGED,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        details=(
            f"Юзер сменил тариф на {max_devices} устр. + {max_bypass} "
            f"рез. подключ., срок пересчитан: {left // 86400} → {kept // 86400} дн."
        ),
    )
    return TariffChangeResult(
        ok=True, new_expires_at=new_expiry,
        old_days=left // 86400, new_days=kept // 86400,
    )


async def _extend(
    session: AsyncSession, user: User, months: int, devices: int, bypass: int
) -> tuple[datetime, "revive_svc.ReviveResult"]:
    """Общая механика продления для покупки и админской выдачи: купленный срок
    прибавляется к остатку (активная подписка не сгорает), подписка становится
    платной, лимит трафика снимается, отозванные по истечению устройства оживают.

    Остаток перед сложением пересчитывается в новый тариф
    (`remaining_seconds_after_switch`). При неизменном тарифе коэффициент 1 и
    поведение прежнее — так ходят и автопродление, и админская выдача. А вот
    покупка с ДРУГИМ тарифом до 20.08.2026 поднимала лимиты на весь прошлый
    остаток бесплатно: год на одном устройстве плюс месяц на десяти давал год
    на десяти. Теперь старое время честно дешевеет."""
    now = datetime.now(timezone.utc)
    kept = remaining_seconds_after_switch(user, devices, bypass)
    new_expiry = now + timedelta(seconds=kept, days=DAYS_PER_MONTH * months)
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
    by_autopay: bool = False,
) -> ChargeResult:
    """Покупка/продление подписки с баланса на `months` месяцев. Тариф — текущий
    у юзера или явный (max_devices/max_bypass): смена тарифа происходит ТОЛЬКО
    в момент покупки, иначе апгрейд лимитов был бы бесплатным до конца срока.

    Срок прибавляется к остатку (активная подписка не сгорает), у платной
    подписки лимит трафика снимается (продаём устройства, не гигабайты),
    отозванные по истечению устройства оживают (ревайв).

    by_autopay — списал планировщик, а не человек: в журнале такое событие
    остаётся без инициатора. Иначе на жалобу «я ничего не покупал» админ увидел
    бы в истории самого юзера и решил бы, что тот покупку сделал сам."""
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
    # Списываем УСЛОВНО: проверка «хватает ли» стоит внутри самого UPDATE.
    # Раздельные «прочитал баланс → списал» оставляли окно на время SSH-оживления
    # устройств (секунды): второй тап «Купить» читал ещё не изменённый баланс,
    # проходил проверку и списывал второй раз (аудит 20.08.2026).
    charged = await repo.charge_balance(
        session, user.id, price,
        note=(
            f"Подписка {months} мес (устройств: {devices}, "
            f"рез. подключений: {bypass})"
        ),
    )
    if not charged:
        # Свежий баланс читаем ПОСЛЕ отказа: тот, что в объекте юзера, мог
        # устареть — из-за этого «не хватает» и называлось бы неверной цифрой.
        await session.refresh(user)
        return ChargeResult(
            ok=False, price_kopeks=price,
            missing_kopeks=max(0, price - user.balance_kopeks),
        )

    new_expiry, rv = await _extend(session, user, months, devices, bypass)
    logger.info(
        "Sub charge: user {} -{} kopeks, {} mo, until {}",
        user.id, price, months, new_expiry.isoformat(),
    )
    await repo.log_action(
        session, AuditAction.BALANCE_CHARGE,
        actor_tg_id=None if by_autopay else user.tg_id,
        target_user_id=user.id,
        amount_kopeks=price,
        details=(
            f"{'Автопродление' if by_autopay else 'Подписка'} {months} мес "
            f"(устройств: {devices}, обходов: {bypass})"  # wording: ok — аудит-лог админа
        ),
    )
    return ChargeResult(
        ok=True, price_kopeks=price, new_expires_at=new_expiry, revive=rv,
        months=months, wanted_months=months, wanted_price_kopeks=price,
    )


async def grant_term(
    session: AsyncSession, user: User, months: int, *, actor_tg_id: int | None = None
) -> ChargeResult:
    """Админ ВЫДАЁТ подписку на один из продаваемых сроков — без денег.

    Отличие от charge_and_extend ровно одно: ничего не списывается и в журнале
    баланса не появляется строки (это подарок/компенсация, а не покупка). Всё
    остальное как у покупки, включая запоминание срока: выдали год — дальше и
    автопродление возьмёт год.

    Срок обязан быть из прайса (TERM_DISCOUNTS): произвольные периоды остаются
    за «📅 Задать срок», который живёт своей логикой (дата/период/бессрочно).

    actor_tg_id — кто из админов выдал: в журнале подарок без имени выдавшего
    бесполезен, а необязательным параметр оставлен, чтобы старые вызовы (и
    будущие автоматические выдачи) не ломались."""
    if months not in TERM_DISCOUNTS:
        raise ValueError(f"срок {months} мес не продаётся")
    new_expiry, rv = await _extend(
        session, user, months, user.sub_max_devices, user.sub_max_bypass
    )
    logger.info(
        "Sub granted by admin: user {}, {} mo, until {}",
        user.id, months, new_expiry.isoformat(),
    )
    await repo.log_action(
        session, AuditAction.SUB_GRANTED,
        actor_tg_id=actor_tg_id,
        # Без актора выдачу сделал бот: подписать её админом значило бы оставить
        # в ленте «выдал админ, неизвестно какой».
        actor_is_admin=actor_tg_id is not None,
        target_user_id=user.id,
        details=f"Подписка на {months} мес выдана админом",
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

    res = await charge_and_extend(session, user, months, by_autopay=True)
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
