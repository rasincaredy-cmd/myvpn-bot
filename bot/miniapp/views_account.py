"""Мини-приложение: подписка, тариф, баланс, друзья.

Экраны страницы собираются из этих ответов, но правила живут не здесь: цену
считает `services/pricing`, покупку и смену тарифа — `services/billing`, счета
— клиенты платёжек. Мини-приложение обязано быть ещё одной витриной тех же
правил, а не вторым их набором: разойдись они, юзер получил бы разные ответы в
боте и в приложении на один и тот же вопрос.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web
from loguru import logger

from bot.config import settings
from bot.db import repo
from bot.miniapp.http import ApiError, Ctx, authorized, body, int_arg
from bot.services import amnezia, billing, cryptopay, platega, referral
from bot.services.pricing import (
    DEPOSIT_BONUS_PERCENT,
    TERM_DISCOUNTS,
    TERM_LABELS,
    fmt_rub,
    monthly_price_kopeks,
    stars_for_kopeks,
    tariff_ceiling,
    term_price_kopeks,
)
from bot.utils.timefmt import as_utc, fmt_msk

# Границы ручной суммы пополнения — те же, что в боте: правило одно, а экранов
# два, и разъехавшиеся пределы юзер поймает первым.
DEPOSIT_MIN_RUB, DEPOSIT_MAX_RUB = 10, 100_000


def sub_active(user) -> bool:
    if user.sub_expires_at is None:
        return True
    return as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


def _days_left(user) -> int:
    return billing.remaining_seconds(user) // 86400


async def _subscription(session, user) -> dict:
    used_dev = await repo.count_active_devices(session, user.id)
    used_byp = await repo.count_active_wdtt_for_user(session, user.id)
    active = sub_active(user)
    perpetual = user.sub_expires_at is None and not user.is_trial
    return {
        "active": active,
        "perpetual": perpetual,
        "trial": bool(user.is_trial and active and user.sub_expires_at is not None),
        "expires_at": fmt_msk(user.sub_expires_at) if user.sub_expires_at else None,
        "days_left": _days_left(user),
        "devices_used": used_dev,
        "devices_max": user.sub_max_devices,
        "bypass_used": used_byp,
        "bypass_max": user.sub_max_bypass,
        "traffic": amnezia.fmt_traffic_line(
            await repo.sub_traffic_used(session, user),
            user.sub_traffic_limit_bytes,
            expired=not active,
        ),
        "autopay": bool(user.autopay),
        # Менять тариф есть смысл только тому, кому есть что пересчитывать:
        # у пробного дни подарены, у бессрочного срока нет, у истёкшего
        # пересчитывать нечего.
        "can_switch": bool(active and not user.is_trial and not perpetual),
    }


def _deposit_amounts() -> list[dict]:
    monthly = monthly_price_kopeks(1, 1)
    out = []
    for months, word in sorted(TERM_LABELS.items()):
        kopeks = term_price_kopeks(monthly, months)
        out.append({
            "rub": kopeks // 100,
            "label": fmt_rub(kopeks),
            "hint": word,
            "stars": stars_for_kopeks(kopeks),
        })
    return out


@authorized()
async def state(request: web.Request, ctx: Ctx) -> dict:
    """Всё, что нужно первому экрану. Один запрос вместо пяти: приложение
    открывают с телефона в дороге, и каждый лишний round-trip — это пустой
    экран на секунду дольше."""
    session, user = ctx.session, ctx.user
    return {
        "user": {
            "name": user.full_name or ctx.tg.full_name,
            "is_admin": bool(user.is_admin),
        },
        "sub": await _subscription(session, user),
        "balance": {
            "kopeks": user.balance_kopeks,
            "text": fmt_rub(user.balance_kopeks),
        },
        "pay": {
            "cryptobot": cryptopay.enabled(),
            "card": platega.enabled(),
            "stars": True,
            "amounts": _deposit_amounts(),
            "bonus": DEPOSIT_BONUS_PERCENT,
            "min_rub": DEPOSIT_MIN_RUB,
            "max_rub": DEPOSIT_MAX_RUB,
        },
        "prices": {
            "first": settings.price_first_rub,
            "extra_device": settings.price_extra_device_rub,
            "extra_bypass": settings.price_extra_bypass_rub,
            "terms": [
                {"months": m, "label": TERM_LABELS[m], "discount": TERM_DISCOUNTS[m]}
                for m in sorted(TERM_LABELS)
            ],
        },
        "flags": {
            "bypass_enabled": bool(settings.wdtt_vk_hashes),
            "trial_days": settings.trial_days,
            "referral_percent": settings.referral_percent,
            "privacy_url": settings.legal_privacy_url,
            "terms_url": settings.legal_terms_url,
        },
    }


# ── Тариф ────────────────────────────────────────────────────────────────────

@authorized()
async def tariff(request: web.Request, ctx: Ctx) -> dict:
    """Предпросмотр тарифа: цена за все сроки и что будет со сроком при смене.

    Число дней берём из `change_tariff(dry_run=True)` — того же расчёта, что
    выполнит кнопка. Отдельная «функция предпросмотра» разошлась бы с ним на
    первой же правке правил.
    """
    user = ctx.user
    ceil_dev, ceil_byp = tariff_ceiling(user.sub_max_devices, user.sub_max_bypass)
    devices = int_arg(dict(request.query), "devices", lo=0, hi=ceil_dev)
    bypass = int_arg(dict(request.query), "bypass", lo=0, hi=ceil_byp)
    if devices + bypass < 1:
        raise ApiError("empty", "Хотя бы одна позиция должна остаться.")

    monthly = monthly_price_kopeks(devices, bypass)
    terms = [
        {
            "months": m,
            "label": TERM_LABELS[m],
            "discount": TERM_DISCOUNTS[m],
            "kopeks": term_price_kopeks(monthly, m),
            "price": fmt_rub(term_price_kopeks(monthly, m)),
            "affordable": user.balance_kopeks >= term_price_kopeks(monthly, m),
        }
        for m in sorted(TERM_LABELS)
    ]
    switch = await billing.change_tariff(
        ctx.session, user, max_devices=devices, max_bypass=bypass, dry_run=True
    )
    return {
        "devices": devices,
        "bypass": bypass,
        "ceiling": {"devices": ceil_dev, "bypass": ceil_byp},
        "monthly": fmt_rub(monthly),
        "terms": terms,
        "switch": {
            "ok": switch.ok,
            "reason": switch.reason,
            "old_days": switch.old_days,
            "new_days": switch.new_days,
            "used_devices": switch.used_devices,
            "used_bypass": switch.used_bypass,
        },
    }


async def _receipt(ctx: Ctx, text: str) -> None:
    """Короткая запись о движении денег — в чат с ботом.

    Экран приложения закрывается и следов не оставляет, а списание с баланса
    след оставить обязано: через неделю «куда делись 810 ₽» разбирается по
    переписке, а не по памяти. Коммит делаем ДО отправки: деньги уже списаны, и
    падение Telegram не должно откатывать покупку.
    """
    from bot.loader import bot

    await ctx.session.commit()
    try:
        await bot.send_message(ctx.user.tg_id, text)
    except Exception:
        logger.warning("Мини-приложение: чек юзеру {} не ушёл", ctx.user.id)


_SWITCH_REFUSAL = {
    "same": "Это твой текущий тариф.",
    "trial": "Идёт пробный период — тариф меняется после первой покупки.",
    "perpetual": "У бессрочной подписки тариф меняет только поддержка.",
    "expired": "Подписка закончилась — сначала продли её.",
    "empty": "Хотя бы одна позиция должна остаться.",
    "too_big": "Столько позиций сразу не продаём.",
}


@authorized(action=True)
async def tariff_change(request: web.Request, ctx: Ctx) -> dict:
    data = await body(request)
    user = ctx.user
    ceil_dev, ceil_byp = tariff_ceiling(user.sub_max_devices, user.sub_max_bypass)
    devices = int_arg(data, "devices", lo=0, hi=ceil_dev)
    bypass = int_arg(data, "bypass", lo=0, hi=ceil_byp)
    res = await billing.change_tariff(
        ctx.session, user, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        if res.reason == "in_use":
            raise ApiError(
                "in_use",
                f"Сейчас занято: {res.used_devices} устр. и {res.used_bypass} "
                "рез. подключ. Освободи лишнее — и меняй.",
            )
        if res.reason == "too_short":
            raise ApiError(
                "too_short",
                f"После пересчёта осталось бы меньше суток ({res.old_days} дн. "
                "на прежнем тарифе). Сначала продли подписку.",
            )
        raise ApiError(res.reason or "no", _SWITCH_REFUSAL.get(res.reason, "Не вышло."))
    await _receipt(
        ctx,
        f"⚙️ <b>Тариф изменён</b> — {devices} устр. + {bypass} рез. подключ.\n"
        f"Срок пересчитан: {res.old_days} → {res.new_days} дн., "
        f"до {fmt_msk(res.new_expires_at)} (МСК).",
    )
    return {
        "days": res.new_days,
        "expires_at": fmt_msk(res.new_expires_at) if res.new_expires_at else None,
        "message": f"Тариф изменён. Срок пересчитан: {res.old_days} → {res.new_days} дн.",
    }


@authorized(action=True)
async def tariff_buy(request: web.Request, ctx: Ctx) -> dict:
    data = await body(request)
    user = ctx.user
    ceil_dev, ceil_byp = tariff_ceiling(user.sub_max_devices, user.sub_max_bypass)
    devices = int_arg(data, "devices", lo=0, hi=ceil_dev)
    bypass = int_arg(data, "bypass", lo=0, hi=ceil_byp)
    months = int_arg(data, "months", lo=1, hi=12)
    if months not in TERM_LABELS:
        raise ApiError("bad_body", "Такого срока нет.")
    if devices + bypass < 1:
        raise ApiError("empty", "Хотя бы одна позиция должна остаться.")
    used_dev = await repo.count_active_devices(ctx.session, user.id)
    used_byp = await repo.count_active_wdtt_for_user(ctx.session, user.id)
    if devices < used_dev or bypass < used_byp:
        raise ApiError(
            "in_use",
            f"Сейчас занято: {used_dev} устр. и {used_byp} рез. подключ. "
            "Тариф ниже этого не продаётся.",
        )
    res = await billing.charge_and_extend(
        ctx.session, user, months, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        raise ApiError(
            "no_money",
            f"На балансе не хватает {fmt_rub(res.missing_kopeks)}. Пополни — "
            "и покупка пройдёт.",
        )
    revived = res.revive.devices_restored if res.revive else 0
    await _receipt(
        ctx,
        f"✅ <b>Подписка продлена</b> на {TERM_LABELS[months]}.\n"
        f"Списано {fmt_rub(res.price_kopeks)}, тариф — {devices} устр. + "
        f"{bypass} рез. подключ.\n"
        f"Действует до {fmt_msk(res.new_expires_at)} (МСК)."
        + (f"\nУстройств вернулось в строй: {revived}." if revived else ""),
    )
    return {
        "message": (
            f"Оплачено {fmt_rub(res.price_kopeks)}. Подписка действует до "
            f"{fmt_msk(res.new_expires_at)} (МСК)."
        ),
        "revived": revived,
        "expires_at": fmt_msk(res.new_expires_at),
    }


@authorized(action=True)
async def autopay(request: web.Request, ctx: Ctx) -> dict:
    data = await body(request)
    ctx.user.autopay = bool(data.get("on"))
    return {"autopay": ctx.user.autopay}


# ── Деньги ───────────────────────────────────────────────────────────────────

@authorized()
async def history(request: web.Request, ctx: Ctx) -> dict:
    rows = await repo.list_balance_txs(ctx.session, ctx.user.id, limit=30)
    return {
        "rows": [
            {
                "amount": fmt_rub(row.amount_kopeks),
                "positive": row.amount_kopeks > 0,
                "note": row.note or "",
                "at": fmt_msk(row.created_at, fmt="%d.%m %H:%M"),
            }
            for row in rows
        ]
    }


@authorized(action=True)
async def deposit(request: web.Request, ctx: Ctx) -> dict:
    """Создаёт счёт на пополнение и отдаёт ссылку на оплату.

    Строку в базе пишем ДО ответа: если юзер оплатит, а строки не окажется,
    зачислять будет нечего — деньги провайдер уже возьмёт.
    """
    data = await body(request)
    rub = int_arg(data, "rub", lo=DEPOSIT_MIN_RUB, hi=DEPOSIT_MAX_RUB)
    method = str(data.get("method") or "")
    amount = rub * 100
    user = ctx.user

    if method == "card":
        if not platega.enabled():
            raise ApiError("off", "Оплата картой сейчас недоступна.")
        try:
            pay = await platega.create_payment(
                amount,
                description=f"Пополнение баланса VPN на {fmt_rub(amount)}",
                payload=f"user:{user.id}",
                return_url=await _bot_link(),
            )
        except platega.PlategaError as exc:
            logger.warning("Мини-приложение: счёт Platega не создан: {}", exc)
            raise ApiError("provider", "Платёжка не отвечает — попробуй позже.") from None
        row = await repo.create_platega_payment(
            ctx.session, user_id=user.id, transaction_id=pay["transaction_id"],
            amount_kopeks=amount, url=pay["url"],
        )
        return {
            "url": pay["url"], "id": row.id, "kind": "web",
            "ttl": platega.INVOICE_TTL_MINUTES,
        }

    if method == "cryptobot":
        if not cryptopay.enabled():
            raise ApiError("off", "Этот способ сейчас недоступен.")
        try:
            inv = await cryptopay.create_invoice(
                amount,
                description=f"Пополнение баланса VPN на {fmt_rub(amount)}",
                payload=f"user:{user.id}",
            )
        except cryptopay.CryptoPayError as exc:
            logger.warning("Мини-приложение: счёт CryptoBot не создан: {}", exc)
            raise ApiError("provider", "Платёжка не отвечает — попробуй позже.") from None
        row = await repo.create_crypto_invoice(
            ctx.session, user_id=user.id, invoice_id=inv["invoice_id"],
            amount_kopeks=amount, url=inv["url"],
        )
        return {"url": inv["url"], "id": row.id, "kind": "telegram"}

    if method == "stars":
        # Счёт звёздами — это сообщение в чате: оплата звёздами живёт внутри
        # Telegram, страница её открыть не может. Приложение закроется, юзер
        # увидит счёт в переписке с ботом.
        from bot.handlers.stars import send_star_invoice

        await ctx.session.commit()
        await send_star_invoice(_ChatTarget(user.tg_id), user, amount)
        return {"kind": "chat", "message": "Счёт на оплату звёздами — в чате с ботом."}

    raise ApiError("bad_body", "Неизвестный способ оплаты.")


class _ChatTarget:
    """Заглушка «сообщения», от которого `send_star_invoice` берёт чат.

    Счёт звёздами умеет отправлять только код бота, и он написан под объект
    сообщения. Городить ради этого второй путь отправки — значит завести второе
    место, где формируется цена в звёздах.
    """

    def __init__(self, chat_id: int) -> None:
        self.chat = type("Chat", (), {"id": chat_id})()

    async def answer_invoice(self, **kwargs):
        from bot.loader import bot

        return await bot.send_invoice(chat_id=self.chat.id, **kwargs)


async def _bot_link() -> str:
    from bot.loader import bot

    me = await bot.get_me()
    return f"https://t.me/{me.username}"


# ── Друзья ───────────────────────────────────────────────────────────────────

@authorized()
async def referral_info(request: web.Request, ctx: Ctx) -> dict:
    from bot.loader import bot

    code = await referral.ensure_code(ctx.session, ctx.user)
    me = await bot.get_me()
    return {
        "link": f"https://t.me/{me.username}?start=ref_{code}",
        "code": code,
        "count": await repo.count_referrals(ctx.session, ctx.user.id),
        "earned": fmt_rub(await repo.sum_ref_earned(ctx.session, ctx.user.id)),
        "percent": settings.referral_percent,
    }
