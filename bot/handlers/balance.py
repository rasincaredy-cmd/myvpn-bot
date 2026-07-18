"""Баланс, пополнение через Crypto Pay, рефералка, покупка подписки (Блок «Баланс»).

Деньги: копейки в БД, движение только через repo.add_balance_tx. Пополнение —
RUB-инвойс @CryptoBot (клиент bot/services/cryptopay.py), зачисление идемпотентно
(billing.apply_paid_invoice) — кнопка «Проверить» и поллинг планировщика не
задвоят депозит. Продление — с баланса, тариф выбирается в момент покупки.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
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

router = Router(name="balance")

# Кнопки быстрых сумм пополнения = цены базового тарифа за 1/3/6/12 мес.
_DEPOSIT_AMOUNTS_RUB = [90, 240, 450, 810]
_CUSTOM_MIN_RUB, _CUSTOM_MAX_RUB = 10, 100_000
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
    text = f"💰 <b>Баланс: {fmt_rub(user.balance_kopeks)}</b>"
    if not cryptopay.enabled():
        text += "\n\n<i>Пополнение временно недоступно — напиши админу.</i>"
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
        "Оплата криптой через @CryptoBot по курсу (сумма — в рублях). "
        "Выбери сумму:",
        reply_markup=deposit_amounts_kb(_DEPOSIT_AMOUNTS_RUB),
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
    rub = int(call.data.rsplit(":", 1)[-1])
    user = await _get_user(session, call)
    await _create_and_show_invoice(call.message.edit_text, session, user, rub * 100)
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
    await call.answer("Оплата пока не видна. Оплатил? Подожди пару секунд и жми ещё раз.", show_alert=True)


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
        "👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей — получай <b>{settings.referral_percent}%</b> "
        "с каждого их пополнения на свой баланс.\n\n"
        f"Твоя ссылка (нажми, чтобы скопировать):\n<code>{link}</code>\n\n"
        f"• Приглашено: <b>{invited}</b>\n"
        f"• Заработано: <b>{fmt_rub(earned)}</b>"
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


async def _render_extend(edit, user, devices: int, bypass: int) -> None:
    devices = max(1, min(_MAX_DEVICES, devices))
    bypass = max(1, min(_MAX_BYPASS, bypass))
    monthly = monthly_price_kopeks(devices, bypass)
    text = (
        "🔁 <b>Продление подписки</b>\n\n"
        f"Тариф: <b>{devices}</b> устр. + <b>{bypass}</b> обход БС "
        f"= <b>{fmt_rub(monthly)}/мес</b>\n"
        f"💰 На балансе: <b>{fmt_rub(user.balance_kopeks)}</b>\n\n"
        "Срок прибавится к текущему, тариф применится сразу. "
        "Подкрути тариф ±, выбери срок:"
    )
    await edit(text, reply_markup=extend_kb(devices, bypass, _term_price_rows(devices, bypass)))


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
    _, _, dev, byp = call.data.split(":")
    user = await _get_user(session, call)
    try:
        await _render_extend(call.message.edit_text, user, int(dev), int(byp))
    except Exception:
        pass  # «message is not modified» на упоре в границы 1..10 — не страшно
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:buy:"))
async def cb_bal_buy(call: CallbackQuery, session: AsyncSession) -> None:
    _, _, dev, byp, months = call.data.split(":")
    devices, bypass, months = int(dev), int(byp), int(months)
    if not (1 <= devices <= _MAX_DEVICES and 1 <= bypass <= _MAX_BYPASS
            and months in TERM_DISCOUNTS):
        await call.answer("Что-то не то с тарифом, начни заново.", show_alert=True)
        return
    user = await _get_user(session, call)
    res = await billing.charge_and_extend(
        session, user, months, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        await session.rollback()
        await call.answer(
            f"Не хватает {fmt_rub(res.missing_kopeks)} "
            f"(цена {fmt_rub(res.price_kopeks)}). Пополни баланс.",
            show_alert=True,
        )
        return
    await session.commit()
    until = res.new_expires_at.strftime("%d.%m.%Y %H:%M")
    text = (
        f"🎉 Подписка оплачена: <b>{months} мес</b> за <b>{fmt_rub(res.price_kopeks)}</b>.\n"
        f"Действует до <b>{until} UTC</b>.\n"
        f"💰 Остаток: <b>{fmt_rub(user.balance_kopeks)}</b>"
    )
    rv = res.revive
    if rv is not None and (rv.devices_restored or rv.bypass_restored):
        text += (
            "\n♻️ Твои устройства восстановлены — прежние конфиги и ссылки "
            "снова работают."
        )
    if rv is not None and rv.errors:
        text += "\n⚠️ Часть не восстановилась, напиши админу."
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
            reply_markup=subscription_kb(True, can_pay=cp.enabled(), autopay=user.autopay)
        )
    except Exception:
        pass
    await call.answer(
        "Автопродление включено: при истечении спишем месяц с баланса."
        if user.autopay else "Автопродление выключено."
    )
