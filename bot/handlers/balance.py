"""Баланс, пополнение через Crypto Pay, рефералка, покупка подписки (Блок «Баланс»).

Деньги: копейки в БД, движение только через repo.add_balance_tx. Пополнение —
RUB-инвойс @CryptoBot (клиент bot/services/cryptopay.py), зачисление идемпотентно
(billing.apply_paid_invoice) — кнопка «Проверить» и поллинг планировщика не
задвоят депозит. Продление — с баланса, тариф выбирается в момент покупки.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.inline import (
    CB_BAL,
    balance_kb,
    back_to_menu,
    cancel_only,
    deposit_amounts_kb,
    extend_kb,
    invoice_kb,
)
from bot.loader import bot
from bot.services import billing, cryptopay
from bot.services.pricing import (
    TERM_DISCOUNTS,
    fmt_rub,
    monthly_price_kopeks,
    term_price_kopeks,
)
from bot.states.install import BalanceStates
from bot.texts import t
from bot.utils.timefmt import fmt_msk

router = Router(name="balance")

# Сроки для кнопок быстрых сумм: цена базового тарифа за 1/3/6/12 мес.
# Суммы считаются из прайсинга, а не хардкодятся — при смене цены кнопки
# не разъедутся с реальной стоимостью.
_DEPOSIT_TERMS = [(1, "месяц"), (3, "3 мес"), (6, "полгода"), (12, "год")]
_CUSTOM_MIN_RUB, _CUSTOM_MAX_RUB = 10, 100_000


def _deposit_amounts() -> list[tuple[int, str]]:
    monthly = monthly_price_kopeks(1, 1)
    return [
        (
            term_price_kopeks(monthly, months) // 100,
            f"{fmt_rub(term_price_kopeks(monthly, months))} — {word}",
        )
        for months, word in _DEPOSIT_TERMS
    ]
# Пределы тарифа на экране продления.
_MAX_DEVICES, _MAX_BYPASS = 10, 10

_bot_username: str | None = None  # кеш для реф-ссылки


async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return _bot_username


async def _get_user(session: AsyncSession, call_or_msg) -> "object":
    u = call_or_msg.from_user
    return await repo.get_or_create_user(
        session, tg_id=u.id, username=u.username, full_name=u.full_name
    )


# ── Экран баланса ────────────────────────────────────────────────────────────

async def _render_balance(edit_or_answer, session: AsyncSession, user) -> None:
    text = (
        f"💰 <b>Баланс: {fmt_rub(user.balance_kopeks)}</b>\n\n"
        "С баланса оплачивается подписка: пополни здесь, а продлить можно "
        "в разделе «🎫 Моя подписка».\n"
        f"Приглашай друзей — {settings.referral_percent}% с их пополнений "
        "тоже падают сюда."
    )
    if not cryptopay.enabled():
        text += "\n\n<i>Пополнение временно недоступно — напиши в поддержку.</i>"
    await edit_or_answer(text, reply_markup=balance_kb(cryptopay.enabled()))


@router.callback_query(F.data == f"{CB_BAL}:my")
async def cb_bal_my(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()  # сюда же ведут «Отмена»/«К балансу» из подпотоков
    user = await _get_user(session, call)
    await _render_balance(call.message.edit_text, session, user)
    await call.answer()


# ── Пополнение ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == f"{CB_BAL}:dep")
async def cb_bal_deposit(call: CallbackQuery, session: AsyncSession) -> None:
    if not cryptopay.enabled():
        await call.answer("Пополнение временно недоступно.", show_alert=True)
        return
    await call.message.edit_text(
        "➕ <b>Пополнение баланса</b>\n\n"
        "Платёж проходит через @CryptoBot — платёжный бот прямо в Telegram. "
        "Сумма — в обычных рублях.\n\n"
        "<i>Крипты нет? Не страшно: её можно купить с банковской карты прямо "
        "в @CryptoBot за пару минут (раздел «Купить») и сразу оплатить счёт.</i>\n\n"
        "Выбери сумму:\n"
        "<i>Суммы на кнопках — стоимость базового тарифа (1 устройство + "
        "1 обход БС) на месяц, 3 месяца, полгода и год.</i>",
        reply_markup=deposit_amounts_kb(_deposit_amounts()),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:dep:custom")
async def cb_bal_deposit_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BalanceStates.custom_amount)
    await state.update_data(cancel_to="bal")
    await call.message.edit_text(
        f"✏️ Введи сумму пополнения в рублях "
        f"({_CUSTOM_MIN_RUB}–{_CUSTOM_MAX_RUB}):",
        reply_markup=cancel_only(),
    )
    await call.answer()


@router.message(BalanceStates.custom_amount, F.text)
async def step_bal_custom_amount(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip().replace("₽", "").strip()
    if not raw.isdigit() or not (_CUSTOM_MIN_RUB <= int(raw) <= _CUSTOM_MAX_RUB):
        await message.answer(
            f"Сумма — целое число {_CUSTOM_MIN_RUB}–{_CUSTOM_MAX_RUB} ₽. Ещё раз:"
        )
        return
    await state.clear()
    user = await _get_user(session, message)
    await _create_and_show_invoice(message.answer, session, user, int(raw) * 100)


@router.callback_query(F.data.startswith(f"{CB_BAL}:dep:"))
async def cb_bal_deposit_amount(call: CallbackQuery, session: AsyncSession) -> None:
    # Сюда падают только "dep:<число>" — dep и dep:custom перехвачены выше.
    raw = call.data.rsplit(":", 1)[-1]
    # callback_data приходит от клиента и может быть подделана (кастомные
    # клиенты Telegram) — держим сумму в тех же рамках, что и ручной ввод.
    if not raw.isdigit() or not (_CUSTOM_MIN_RUB <= int(raw) <= _CUSTOM_MAX_RUB):
        await call.answer("Некорректная сумма.", show_alert=True)
        return
    user = await _get_user(session, call)
    await _create_and_show_invoice(call.message.edit_text, session, user, int(raw) * 100)
    await call.answer()


async def _create_and_show_invoice(
    send, session: AsyncSession, user, amount_kopeks: int
) -> None:
    try:
        inv = await cryptopay.create_invoice(
            amount_kopeks,
            description=f"Пополнение баланса VPN на {fmt_rub(amount_kopeks)}",
            payload=f"user:{user.id}",
        )
    except cryptopay.CryptoPayError as exc:
        logger.warning("CryptoPay create_invoice failed: {}", exc)
        await send(
            "❌ Не получилось создать счёт — попробуй позже.",
            reply_markup=balance_kb(cryptopay.enabled()),
        )
        return
    row = await repo.create_crypto_invoice(
        session, user_id=user.id, invoice_id=inv["invoice_id"],
        amount_kopeks=amount_kopeks, url=inv["url"],
    )
    await session.commit()
    await send(
        f"💳 Счёт на <b>{fmt_rub(amount_kopeks)}</b> создан (действует 1 час).\n\n"
        "<i>Крипты нет? Купи её с карты прямо в @CryptoBot (раздел «Купить») — "
        "и оплати счёт.</i>\n\n"
        "Оплати в @CryptoBot и жми «Проверить» — обычно баланс зачисляется "
        "за пару секунд. Если закроешь экран — не страшно, бот сам увидит "
        "оплату в течение ~5 минут.",
        reply_markup=invoice_kb(inv["url"], row.id),
    )


@router.callback_query(F.data.startswith(f"{CB_BAL}:check:"))
async def cb_bal_check(call: CallbackQuery, session: AsyncSession) -> None:
    row_id = int(call.data.rsplit(":", 1)[-1])
    inv = await repo.get_crypto_invoice(session, row_id)
    user = await _get_user(session, call)
    if inv is None or inv.user_id != user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return
    if inv.status == "paid":
        await _render_balance(call.message.edit_text, session, user)
        await call.answer("Уже зачислено ✅")
        return
    try:
        statuses = await cryptopay.get_invoice_statuses([inv.invoice_id])
    except cryptopay.CryptoPayError as exc:
        logger.warning("CryptoPay check failed: {}", exc)
        await call.answer("Crypto Pay не отвечает, попробуй чуть позже.", show_alert=True)
        return
    status = statuses.get(inv.invoice_id)
    if status == "paid":
        dep = await billing.apply_paid_invoice(session, inv)
        await session.commit()
        await notify_deposit(dep)
        await session.refresh(user)
        # Подписка уже истекла, автопродление включено? Продлеваем сразу на
        # свежие деньги — не заставляем ждать тика планировщика (до 5 минут).
        ap = await billing.autopay_if_expired(session, user)
        if ap is not None:
            await session.commit()
            await notify_autopay(user, ap)
            await session.refresh(user)
        await _render_balance(call.message.edit_text, session, user)
        await call.answer("Зачислено ✅")
        return
    if status == "expired":
        inv.status = "expired"
        await session.commit()
        await call.message.edit_text(
            "⌛ Счёт истёк (не оплачен за час). Создай новый.",
            reply_markup=balance_kb(cryptopay.enabled()),
        )
        await call.answer()
        return
    await call.answer(
        "Оплата пока не видна. Если платёж уже отправлен — подожди пару секунд "
        "и жми ещё раз.",
        show_alert=True,
    )


async def notify_deposit(dep: billing.DepositResult) -> None:
    """Уведомления о зачислении: юзеру и (если есть) пригласившему. Общая для
    кнопки «Проверить» и поллинга планировщика; ошибки Telegram глотаем."""
    if not dep.credited or dep.user is None:
        return
    try:
        await bot.send_message(
            dep.user.tg_id,
            f"✅ Баланс пополнен на <b>{fmt_rub(dep.amount_kopeks)}</b>. "
            f"Сейчас на счету: <b>{fmt_rub(dep.user.balance_kopeks)}</b>.",
        )
    except Exception:
        pass
    if dep.referrer is not None and dep.ref_reward_kopeks > 0:
        try:
            await bot.send_message(
                dep.referrer.tg_id,
                f"🎁 Твой реферал пополнил баланс — тебе начислено "
                f"<b>{fmt_rub(dep.ref_reward_kopeks)}</b> ({settings.referral_percent}%).",
            )
        except Exception:
            pass


async def notify_autopay(user, res: billing.ChargeResult) -> None:
    """Уведомление об автопродлении с баланса. Общая для планировщика и
    мгновенного продления после пополнения; ошибки Telegram глотаем."""
    text = (
        f"♻️ Подписка автоматически продлена на месяц за "
        f"{fmt_rub(res.price_kopeks)} с баланса "
        f"(до {fmt_msk(res.new_expires_at)} МСК).\n"
        f"Остаток: {fmt_rub(user.balance_kopeks)}. "
        "Отключить автопродление можно в «🎫 Моя подписка»."
    )
    rv = res.revive
    if rv is not None and (rv.devices_restored or rv.bypass_restored):
        text += (
            "\n📱 Устройства восстановлены — прежние конфиги и ссылки "
            "снова работают."
        )
    if rv is not None and rv.errors:
        text += "\n⚠️ Часть устройств не восстановилась — напиши в поддержку, починим."
    try:
        await bot.send_message(user.tg_id, text)
    except Exception:
        pass


# ── История ──────────────────────────────────────────────────────────────────

_KIND_ICONS = {"deposit": "➕", "charge": "🎫", "ref": "🎁", "admin": "🛠"}


@router.callback_query(F.data == f"{CB_BAL}:hist")
async def cb_bal_history(call: CallbackQuery, session: AsyncSession) -> None:
    user = await _get_user(session, call)
    txs = await repo.list_balance_txs(session, user.id, limit=10)
    if not txs:
        lines = ["📜 <b>История операций</b>\n", "Пока пусто."]
    else:
        lines = ["📜 <b>История операций</b> (последние 10)\n"]
        for tx in txs:
            icon = _KIND_ICONS.get(tx.kind, "•")
            when = tx.created_at.strftime("%d.%m %H:%M") if tx.created_at else "—"
            note = f" — {tx.note}" if tx.note else ""
            lines.append(f"{icon} {when}  <b>{fmt_rub(tx.amount_kopeks)}</b>{note}")
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await call.answer()


# ── Рефералка ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == f"{CB_BAL}:ref")
async def cb_bal_ref(call: CallbackQuery, session: AsyncSession) -> None:
    user = await _get_user(session, call)
    username = await _get_bot_username()
    link = f"https://t.me/{username}?start=ref_{user.id}"
    invited = await repo.count_referrals(session, user.id)
    earned = await repo.sum_ref_earned(session, user.id)
    text = (
        f"👥 <b>Приведи друга — получай {settings.referral_percent}% "
        "с каждого его пополнения</b>\n\n"
        "Отправь другу свою ссылку. Каждый раз, когда он пополняет баланс, "
        f"тебе приходит <b>{settings.referral_percent}%</b> от суммы — "
        "настоящими деньгами на твой баланс, ими можно оплачивать свою "
        "подписку. Не разово, а с каждого пополнения, всегда.\n\n"
        f"Твоя ссылка (нажми, чтобы скопировать):\n<code>{link}</code>\n\n"
        f"• Приглашено: <b>{invited}</b>\n"
        f"• Заработано: <b>{fmt_rub(earned)}</b>\n\n"
        "<i>Можно просто переслать другу: «Держи VPN, который работает: "
        f"{link} — первые {settings.trial_days} дней бесплатно»</i>"
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


# ── Продление / покупка подписки ─────────────────────────────────────────────

def _term_price_rows(devices: int, bypass: int) -> list[tuple[int, str]]:
    monthly = monthly_price_kopeks(devices, bypass)
    rows: list[tuple[int, str]] = []
    for months, discount in TERM_DISCOUNTS.items():
        price = term_price_kopeks(monthly, months)
        label = f"{months} мес — {fmt_rub(price)}"
        if discount:
            label += f" (−{discount}%)"
        rows.append((months, label))
    return rows


def _tariff_bounds(user) -> tuple[int, int]:
    """Потолки конструктора. Обычно 10/10, но если админ выставил юзеру больше —
    показываем честно, а не срезаем молча (иначе покупка тихо даунгрейдила бы
    тариф до 10 и лишние устройства переставали бы оживать)."""
    return (
        max(_MAX_DEVICES, user.sub_max_devices),
        max(_MAX_BYPASS, user.sub_max_bypass),
    )


def _clamp_tariff(user, devices: int, bypass: int) -> tuple[int, int]:
    """0..потолок по каждому типу; тариф «совсем без всего» не существует —
    пустую пару поднимаем до 1+1 (стартовый экран у юзера с лимитами 0/0)."""
    max_dev, max_byp = _tariff_bounds(user)
    devices = max(0, min(max_dev, devices))
    bypass = max(0, min(max_byp, bypass))
    if devices + bypass == 0:
        devices = bypass = 1
    return devices, bypass


async def _render_extend(edit, user, devices: int, bypass: int) -> None:
    devices, bypass = _clamp_tariff(user, devices, bypass)
    max_dev, max_byp = _tariff_bounds(user)
    monthly = monthly_price_kopeks(devices, bypass)
    first_rub = (
        settings.price_base_rub
        - settings.price_extra_device_rub  # цена тарифа из одной позиции (1+0/0+1)
    )
    text = (
        "🔁 <b>Продление подписки</b>\n\n"
        f"Считаем просто: первая позиция (устройство или обход БС) — "
        f"<b>{first_rub} ₽/мес</b>, каждая следующая — "
        f"<b>+{settings.price_extra_device_rub} ₽/мес</b>. Не нужны устройства "
        "или обходы — смело ставь 0.\n\n"
        "Твой тариф:\n"
        f"📱 Устройств: <b>{devices}</b>\n"
        f"🛡 Обходов БС: <b>{bypass}</b>\n"
        f"Цена: <b>{fmt_rub(monthly)}/мес</b>\n"
        f"💰 На балансе: <b>{fmt_rub(user.balance_kopeks)}</b>\n\n"
        "Настрой количество кнопками − и +, потом выбери срок — чем дольше, "
        "тем дешевле. Оплаченные дни прибавятся к текущей подписке, новый "
        "тариф заработает сразу."
    )
    await edit(text, reply_markup=extend_kb(
        devices, bypass, _term_price_rows(devices, bypass), max_dev, max_byp
    ))


@router.callback_query(F.data == f"{CB_BAL}:extend")
async def cb_bal_extend(call: CallbackQuery, session: AsyncSession) -> None:
    user = await _get_user(session, call)
    if user.sub_expires_at is None and not user.is_trial:
        await call.answer("У тебя бессрочная подписка — продлевать нечего 🙂", show_alert=True)
        return
    await _render_extend(call.message.edit_text, user, user.sub_max_devices, user.sub_max_bypass)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:ext:"))
async def cb_bal_extend_adjust(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    if len(parts) != 4 or not parts[2].lstrip("-").isdigit() or not parts[3].lstrip("-").isdigit():
        await call.answer()  # кривой callback (форжат только руками) — молча игнор
        return
    user = await _get_user(session, call)
    try:
        await _render_extend(call.message.edit_text, user, int(parts[2]), int(parts[3]))
    except TelegramBadRequest as exc:
        # На границах CB_NOP-заглушки перерисовку не дёргают, но старые
        # сообщения с прежней клавиатурой могут прислать то же состояние.
        if "message is not modified" not in str(exc):
            raise
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:buy:"))
async def cb_bal_buy(call: CallbackQuery, session: AsyncSession) -> None:
    try:
        _, _, dev, byp, months = call.data.split(":")
        devices, bypass, months = int(dev), int(byp), int(months)
    except ValueError:  # форжённый callback (кастомный клиент) — не роняем хендлер
        await call.answer("Что-то не то с тарифом, начни заново.", show_alert=True)
        return
    user = await _get_user(session, call)
    max_dev, max_byp = _tariff_bounds(user)
    if not (0 <= devices <= max_dev and 0 <= bypass <= max_byp
            and devices + bypass >= 1 and months in TERM_DISCOUNTS):
        await call.answer("Что-то не то с тарифом, начни заново.", show_alert=True)
        return
    # Анти-эксплойт: тариф ниже текущего ПОТРЕБЛЕНИЯ не продаём — активные
    # устройства сверх нового лимита продолжили бы работать весь срок (лимит
    # проверяется только при добавлении), выходило бы дешёвое продление.
    used_dev = await repo.count_active_devices(session, user.id)
    used_byp = await repo.count_active_wdtt_for_user(session, user.id)
    if devices < used_dev or bypass < used_byp:
        await call.answer(
            f"У тебя сейчас активно {used_dev} устр. и {used_byp} обход(а) — "
            "тариф не может быть меньше. Сначала удали лишнее в «📱 Мои "
            "устройства» / «🛡 Обход БС».",
            show_alert=True,
        )
        return
    res = await billing.charge_and_extend(
        session, user, months, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        await session.rollback()
        await call.answer(
            f"Не хватает {fmt_rub(res.missing_kopeks)}: цена "
            f"{fmt_rub(res.price_kopeks)}, на балансе "
            f"{fmt_rub(user.balance_kopeks)}. "
            "Жми «➕ Пополнить баланс» под сообщением 👇",
            show_alert=True,
        )
        return
    await session.commit()
    text = (
        f"🎉 Подписка оплачена: <b>{months} мес</b> за <b>{fmt_rub(res.price_kopeks)}</b>.\n"
        f"Действует до <b>{fmt_msk(res.new_expires_at)} МСК</b>.\n"
        f"💰 Остаток: <b>{fmt_rub(user.balance_kopeks)}</b>"
    )
    rv = res.revive
    if rv is not None and (rv.devices_restored or rv.bypass_restored):
        text += (
            "\n♻️ Твои устройства восстановлены — прежние конфиги и ссылки "
            "снова работают."
        )
    if rv is not None and rv.errors:
        text += "\n⚠️ Часть устройств не восстановилась — напиши в поддержку, починим."
    await call.message.edit_text(text, reply_markup=back_to_menu())
    await call.answer("Оплачено 🎉")


# ── Автопродление ────────────────────────────────────────────────────────────

@router.callback_query(F.data == f"{CB_BAL}:autopay")
async def cb_bal_autopay(call: CallbackQuery, session: AsyncSession) -> None:
    user = await _get_user(session, call)
    user.autopay = not user.autopay
    await session.commit()
    # Экран подписки перерисовывает devices.cb_sub_my — дергаем его логику руками
    # нельзя (циклический импорт), поэтому просто подтверждаем и обновляем кнопки.
    from bot.keyboards.inline import subscription_kb
    from bot.services import cryptopay as cp
    try:
        await call.message.edit_reply_markup(
            reply_markup=subscription_kb(can_pay=cp.enabled(), autopay=user.autopay)
        )
    except Exception:
        pass
    await call.answer(
        "Автопродление включено: при истечении спишем месяц с баланса."
        if user.autopay else "Автопродление выключено."
    )
