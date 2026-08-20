"""Баланс, пополнение, рефералка, покупка подписки (Блок «Баланс»).

Деньги: копейки в БД, движение только через repo.add_balance_tx. Пополнять
можно двумя способами (этап D): RUB-инвойс @CryptoBot (клиент
bot/services/cryptopay.py) и звёзды Telegram (bot/handlers/stars.py) — экраны
выбора и сумм для обоих живут здесь, зачисление у обоих идёт через
billing.credit_deposit и идемпотентно (кнопка «Проверить», поллинг планировщика
и повторно доставленный платёж не задвоят депозит). Продление — с баланса,
тариф выбирается в момент покупки.
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
from bot.handlers.stars import send_star_invoice
from bot.keyboards.inline import (
    CB_BAL,
    balance_kb,
    back_to_menu,
    cancel_only,
    deposit_amounts_kb,
    deposit_methods_kb,
    tariff_confirm_kb,
    tariff_kb,
    invoice_kb,
    platega_amounts_kb,
    platega_invoice_kb,
    star_amounts_kb,
    topup_kb,
)
from bot.loader import bot
from bot.services import billing, cryptopay, platega
from bot.services.pricing import (
    DEPOSIT_BONUS_PERCENT,
    TERM_DISCOUNTS,
    TERM_LABELS,
    fmt_rub,
    monthly_price_kopeks,
    stars_for_kopeks,
    term_price_kopeks,
)
from bot.states.install import BalanceStates
from bot.texts import t, ui
from bot.utils.timefmt import fmt_msk

router = Router(name="balance")

# Сроки для кнопок быстрых сумм: цена базового тарифа за 1/3/6/12 мес.
# Суммы считаются из прайсинга, а не хардкодятся — при смене цены кнопки
# не разъедутся с реальной стоимостью.
_DEPOSIT_TERMS = sorted(TERM_LABELS.items())
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


def _star_amounts() -> list[tuple[int, str]]:
    """Те же суммы, что и у CryptoBot, но подпись — «сколько звёзд = сколько
    рублей»: юзер платит в одной валюте, а на баланс получает другую."""
    return [
        (rub, f"{stars_for_kopeks(rub * 100)} ⭐ = {rub} ₽")
        for rub, _label in _deposit_amounts()
    ]


# Пределы тарифа на экране продления.
_MAX_DEVICES, _MAX_BYPASS = 10, 10

# Стартовое положение конструктора — типовой тариф, а не витринный. Два
# устройства с подключением стоят 160 ₽, это выше рыночной медианы ~150 ₽, и
# первым числом юзеру его показывать не стоит.
_START_DEVICES, _START_BYPASS = 1, 1


def _extend_intro() -> str:
    """Как считается цена — словами. Отдельной функцией, чтобы тест сверял
    названные цифры с настройками, а не с копией текста: доплаты за устройство
    и за подключение теперь разные, и одна цифра на обе была бы враньём."""
    return (
        f"Считаем просто: первая позиция (устройство или резервное "
        f"подключение) — <b>{settings.price_first_rub} ₽/мес</b>. Каждое "
        f"следующее устройство — <b>+{settings.price_extra_device_rub} ₽/мес</b>, "
        f"каждое следующее подключение — "
        f"<b>+{settings.price_extra_bypass_rub} ₽/мес</b>. Что-то из этого не "
        "нужно — смело ставь 0."
    )

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
        text += "\n\n<i>Оплата через @CryptoBot временно недоступна — остаются звёзды.</i>"
    # Кнопка пополнения теперь есть всегда: звёздам не нужен ни токен, ни
    # настройка, поэтому раздел не исчезает даже с выключенным CryptoBot.
    await edit_or_answer(text, reply_markup=balance_kb(True))


@router.callback_query(F.data == f"{CB_BAL}:my")
async def cb_bal_my(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()  # сюда же ведут «Отмена»/«К балансу» из подпотоков
    user = await _get_user(session, call)
    await _render_balance(call.message.edit_text, session, user)
    await call.answer()


# ── Пополнение ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == f"{CB_BAL}:dep")
async def cb_bal_deposit(call: CallbackQuery, session: AsyncSession) -> None:
    """Экран выбора способа (этап D). Раньше здесь сразу были суммы: способ был
    ровно один. Про долю Apple сказано прямо — юзер, купивший звёзды в iPhone,
    иначе решит, что лишние проценты забрал сервис."""
    await call.message.edit_text(
        "➕ <b>Пополнение баланса</b>\n\n"
        "💳 <b>Карта или СБП</b> — оплата рублями с карты или по QR через "
        "приложение банка. Зачислим ровно ту сумму, которую выберешь.\n\n"
        f"💎 <b>@CryptoBot</b> — оплата в рублях, крипту можно купить с карты "
        f"прямо там. Начислим <b>+{DEPOSIT_BONUS_PERCENT['cryptobot']}%</b> "
        "сверху.\n\n"
        f"⭐ <b>Звёзды Telegram</b> — оплата в два касания, не выходя из "
        f"Telegram. Дороже на {settings.star_markup_percent}%: звёзды доходят "
        "до нас через вывод с комиссиями и трёхнедельной задержкой, наценка "
        "это и покрывает.\n"
        "<i>Отдельно: Apple и Google берут свою долю при покупке самих звёзд "
        "в приложении — это не наша комиссия, мы её не получаем. Дешевле "
        "покупать звёзды не через приложение.</i>",
        reply_markup=deposit_methods_kb(
            DEPOSIT_BONUS_PERCENT["cryptobot"],
            cryptobot=cryptopay.enabled(),
            platega=platega.enabled(),
        ),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:dep:cb")
async def cb_bal_deposit_cryptobot(call: CallbackQuery) -> None:
    if not cryptopay.enabled():
        await call.answer("Этот способ временно недоступен.", show_alert=True)
        return
    await call.message.edit_text(
        "💎 <b>Пополнение через @CryptoBot</b>\n\n"
        "Платёж проходит через @CryptoBot — платёжный бот прямо в Telegram. "
        "Сумма — в обычных рублях.\n\n"
        f"✨ Начислим <b>+{DEPOSIT_BONUS_PERCENT['cryptobot']}%</b> сверху: "
        "этот способ дешевле для нас, чем остальные, и разницу мы возвращаем "
        "тебе.\n\n"
        "<i>Крипты нет? Не страшно: её можно купить с банковской карты прямо "
        "в @CryptoBot за пару минут (раздел «Купить») и сразу оплатить счёт.</i>\n\n"
        "Выбери сумму:\n"
        "<i>Суммы на кнопках — стоимость базового тарифа (1 устройство + "
        "1 резервное подключение) на месяц, 3 месяца, полгода и год.</i>",
        reply_markup=deposit_amounts_kb(_deposit_amounts()),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:dep:stars")
async def cb_bal_deposit_stars(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "⭐ <b>Пополнение звёздами</b>\n\n"
        f"Курс: <b>{stars_for_kopeks(100_00)} ⭐ = 100 ₽</b> на балансе "
        f"(наценка {settings.star_markup_percent}% за способ уже в цене).\n"
        "Звёзд не хватит — Telegram предложит докупить прямо на экране оплаты.\n\n"
        "Выбери сумму:\n"
        "<i>Рубли на кнопках — стоимость базового тарифа (1 устройство + "
        "1 резервное подключение) на месяц, 3 месяца, полгода и год.</i>",
        reply_markup=star_amounts_kb(_star_amounts()),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:dep:pg")
async def cb_bal_deposit_platega(call: CallbackQuery) -> None:
    """Экран сумм для оплаты картой/СБП. Способ оплаты юзер выбирает уже на
    форме провайдера — здесь только сумма."""
    if not platega.enabled():
        await call.answer("Этот способ временно недоступен.", show_alert=True)
        return
    await call.message.edit_text(
        "💳 <b>Оплата картой или через СБП</b>\n\n"
        "Открой ссылку, выбери удобный способ — банковская карта, СБП по QR "
        "или криптовалюта — и оплати. На баланс придёт ровно та сумма, "
        "которую выберешь: комиссию платим мы.\n\n"
        "Выбери сумму:\n"
        "<i>Суммы на кнопках — стоимость базового тарифа (1 устройство + "
        "1 резервное подключение) на месяц, 3 месяца, полгода и год.</i>",
        reply_markup=platega_amounts_kb(_deposit_amounts()),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:dep:custom")
async def cb_bal_deposit_custom(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_custom_amount(call, state, method="cryptobot")


@router.callback_query(F.data == f"{CB_BAL}:star:custom")
async def cb_bal_star_custom(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_custom_amount(call, state, method="stars")


@router.callback_query(F.data == f"{CB_BAL}:pg:custom")
async def cb_bal_platega_custom(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_custom_amount(call, state, method="platega")


async def _ask_custom_amount(
    call: CallbackQuery, state: FSMContext, *, method: str
) -> None:
    """Своя сумма — общий экран для обоих способов. Способ едет в state: без
    него ввод «300» после выбора звёзд молча выставил бы счёт в @CryptoBot."""
    await state.set_state(BalanceStates.custom_amount)
    await state.update_data(cancel_to="bal", method=method)
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
    method = (await state.get_data()).get("method", "cryptobot")
    await state.clear()
    user = await _get_user(session, message)
    if method == "stars":
        await send_star_invoice(message, user, int(raw) * 100)
        return
    if method == "platega":
        await _create_and_show_platega(message.answer, session, user, int(raw) * 100)
        return
    await _create_and_show_invoice(message.answer, session, user, int(raw) * 100)


@router.callback_query(F.data == f"{CB_BAL}:starx")
async def cb_bal_star_cancel(call: CallbackQuery, session: AsyncSession) -> None:
    """Отмена счёта в звёздах.

    Счёт УДАЛЯЕМ, а не перерисовываем: сообщение-счёт Telegram редактировать
    не даёт, и обычный «« К балансу» здесь свалился бы с ошибкой. Баланс
    поэтому уходит новым сообщением.
    """
    user = await _get_user(session, call)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        # Счёт старше 48 часов или уже удалён — экран баланса всё равно нужен.
        pass
    await _render_balance(call.message.answer, session, user)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:star:"))
async def cb_bal_star_amount(call: CallbackQuery, session: AsyncSession) -> None:
    # Сюда падают только "star:<число>" — star:custom перехвачен выше.
    raw = call.data.rsplit(":", 1)[-1]
    if not raw.isdigit() or not (_CUSTOM_MIN_RUB <= int(raw) <= _CUSTOM_MAX_RUB):
        await call.answer("Некорректная сумма.", show_alert=True)
        return
    user = await _get_user(session, call)
    await session.commit()
    await send_star_invoice(call.message, user, int(raw) * 100)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:pg:"))
async def cb_bal_platega_amount(call: CallbackQuery, session: AsyncSession) -> None:
    # Сюда падают только "pg:<число>" — pg:custom перехвачен выше.
    raw = call.data.rsplit(":", 1)[-1]
    # callback_data приходит от клиента и может быть подделана — держим сумму
    # в тех же рамках, что и ручной ввод.
    if not raw.isdigit() or not (_CUSTOM_MIN_RUB <= int(raw) <= _CUSTOM_MAX_RUB):
        await call.answer("Некорректная сумма.", show_alert=True)
        return
    user = await _get_user(session, call)
    await _create_and_show_platega(
        call.message.edit_text, session, user, int(raw) * 100
    )
    await call.answer()


async def _create_and_show_platega(
    send, session: AsyncSession, user, amount_kopeks: int
) -> None:
    """Создаёт счёт Platega и показывает его юзеру.

    Строку в базе пишем ДО показа: если юзер оплатит, а строки не окажется,
    зачислять будет нечего — деньги провайдер уже возьмёт. Способ оплаты не
    задаём: на форме юзер выберет карту, СБП или крипту сам."""
    bot_username = await _get_bot_username()
    try:
        pay = await platega.create_payment(
            amount_kopeks,
            description=f"Пополнение баланса VPN на {fmt_rub(amount_kopeks)}",
            payload=f"user:{user.id}",
            return_url=f"https://t.me/{bot_username}",
        )
    except platega.PlategaError as exc:
        logger.warning("Platega create_payment failed: {}", exc)
        await send(
            "❌ Не получилось создать счёт — попробуй позже или выбери другой "
            "способ пополнения.",
            reply_markup=balance_kb(True),
        )
        return
    row = await repo.create_platega_payment(
        session, user_id=user.id, transaction_id=pay["transaction_id"],
        amount_kopeks=amount_kopeks, url=pay["url"],
    )
    await session.commit()
    await send(
        f"💳 Счёт на <b>{fmt_rub(amount_kopeks)}</b> создан "
        f"(действует {platega.INVOICE_TTL_MINUTES} минут).\n\n"
        "Нажми «Перейти к оплате», выбери способ и оплати. Потом вернись сюда "
        "и жми «Я оплатил» — обычно баланс зачисляется за пару секунд. Если "
        "закроешь экран — не страшно, бот сам увидит оплату в течение "
        "~5 минут.",
        reply_markup=platega_invoice_kb(pay["url"], row.id),
    )


@router.callback_query(F.data.startswith(f"{CB_BAL}:pgchk:"))
async def cb_bal_platega_check(call: CallbackQuery, session: AsyncSession) -> None:
    row_id = int(call.data.rsplit(":", 1)[-1])
    row = await repo.get_platega_payment(session, row_id)
    user = await _get_user(session, call)
    if row is None or row.user_id != user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return
    if row.status == "paid":
        await _render_balance(call.message.edit_text, session, user)
        await call.answer("Уже зачислено ✅")
        return
    try:
        status = await platega.get_status(row.transaction_id)
    except platega.PlategaError as exc:
        logger.warning("Platega check failed: {}", exc)
        await call.answer("Платёжка не отвечает, попробуй чуть позже.", show_alert=True)
        return
    if status == "CONFIRMED":
        dep = await billing.apply_paid_platega(session, row)
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
    if status in ("CANCELED", "CHARGEBACKED"):
        row.status = "canceled"
        await session.commit()
        await call.message.edit_text(
            f"⌛ Счёт больше не действует (он живёт "
            f"{platega.INVOICE_TTL_MINUTES} минут). Создай новый.",
            reply_markup=balance_kb(True),
        )
        await call.answer()
        return
    await call.answer(
        "Оплата пока не видна. Если платёж уже отправлен — подожди пару секунд "
        "и жми ещё раз.",
        show_alert=True,
    )


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
            reply_markup=balance_kb(True),
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
            reply_markup=balance_kb(True),
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


def term_label(months: int) -> str:
    """Срок словами — так же, как на кнопках покупки, чтобы юзер узнавал то,
    что выбирал сам."""
    return TERM_LABELS.get(months, f"{months} мес")


def autopay_forecast_line(user) -> str | None:
    """Строка для предупреждения «подписка скоро истечёт»: что именно спишется.

    Юзер должен узнать сумму ДО списания, а не из факта. None — автопродление
    выключено или подписка бессрочная: сказать нечего."""
    if not user.autopay or user.sub_expires_at is None:
        return None
    plan = billing.plan_autopay(user)
    if plan is None:
        # Денег не хватает даже на месяц (либо тариф пустой) — предупреждаем,
        # что пауза всё-таки будет, и называем минимальную сумму.
        if user.sub_max_devices + user.sub_max_bypass < 1:
            return None
        month_price = term_price_kopeks(
            monthly_price_kopeks(user.sub_max_devices, user.sub_max_bypass), 1
        )
        return (
            "♻️ Автопродление включено, но на балансе не хватает даже на месяц "
            f"(нужно {fmt_rub(month_price)}) — подписка встанет на паузу. "
            "Пополни баланс, и она продлится сама."
        )
    months, price, wanted, wanted_price = plan
    if months < wanted:
        return (
            f"♻️ Автопродление: баланса хватит на <b>{term_label(months)}</b> "
            f"({fmt_rub(price)}), а покупал ты на <b>{term_label(wanted)}</b> "
            f"({fmt_rub(wanted_price)}). Пополни ещё "
            f"{fmt_rub(wanted_price - user.balance_kopeks)} — продлим на полный "
            "срок и со скидкой за него."
        )
    return (
        f"♻️ Автопродление: спишем {fmt_rub(price)} за "
        f"<b>{term_label(months)}</b> с баланса, VPN не прервётся."
    )


def autopay_notice(user, res: billing.ChargeResult) -> tuple[str, bool]:
    """Текст уведомления об автопродлении и нужна ли кнопка пополнения.

    Если денег хватило только на срок короче купленного, юзеру говорим об этом
    прямым текстом: какой срок был, какой стал, сколько не хватило. Молча
    выдать месяц вместо года — верный способ получить «а почему у меня
    подписка кончилась через месяц?» в поддержке."""
    shortened = res.months < res.wanted_months
    until = f"до {fmt_msk(res.new_expires_at)} МСК"
    if shortened:
        text = (
            "♻️ Подписка продлена, но <b>на меньший срок</b>, чем ты покупал.\n\n"
            f"• Было куплено на <b>{term_label(res.wanted_months)}</b> — это "
            f"{fmt_rub(res.wanted_price_kopeks)}, на балансе столько не набралось "
            f"(не хватило <b>{fmt_rub(res.missing_kopeks)}</b>).\n"
            f"• Продлили на <b>{term_label(res.months)}</b> за "
            f"{fmt_rub(res.price_kopeks)} — {until}.\n\n"
            "Пополни баланс — в следующий раз продлим на полный срок и со "
            "скидкой за него.\n"
            f"Остаток: {fmt_rub(user.balance_kopeks)}. "
            "Отключить автопродление — в «🎫 Моя подписка»."
        )
    else:
        text = (
            f"♻️ Подписка автоматически продлена на "
            f"<b>{term_label(res.months)}</b> за {fmt_rub(res.price_kopeks)} "
            f"с баланса ({until}).\n"
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
    return text, shortened


async def notify_autopay(user, res: billing.ChargeResult) -> None:
    """Уведомление об автопродлении с баланса. Общая для планировщика и
    мгновенного продления после пополнения; ошибки Telegram глотаем."""
    text, need_topup = autopay_notice(user, res)
    kb = topup_kb() if need_topup else None
    try:
        await bot.send_message(user.tg_id, text, reply_markup=kb)
    except Exception:
        pass


# ── История ──────────────────────────────────────────────────────────────────

_KIND_ICONS = {
    "deposit": "➕", "charge": "🎫", "ref": "🎁", "admin": "🛠", "bonus": "✨",
}


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
            when = fmt_msk(tx.created_at, fmt="%d.%m %H:%M") if tx.created_at else "—"
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


def build_tariff_text(
    user, devices: int, bypass: int, *, switch_days: int | None
) -> str:
    """Экран «⚙️ Тариф»: что за тариф собран и во что он обойдётся.

    Чистая функция: число дней после смены ей ПЕРЕДАЮТ — ровно то же, что
    уходит на подпись кнопки. Считать исход дважды (здесь и в клавиатуре)
    значило бы однажды показать на экране одну цифру, а на кнопке другую.

    Объяснение «как считается цена» уехало в свёрнутую цитату (Блок «Облик»):
    раньше оно занимало абзац на самом экране, и его прокручивали не читая.
    """
    monthly = monthly_price_kopeks(devices, bypass)
    facts = [
        ui.fact("📱", "Устройства", devices),
        ui.fact("⚡", "Резервные подключения", bypass),
        ui.fact("💳", "Цена", f"{fmt_rub(monthly)}/мес"),
        ui.fact("💰", "На балансе", fmt_rub(user.balance_kopeks)),
    ]

    if switch_days is not None:
        was_days = billing.remaining_seconds(user) // 86400
        note = (
            f"Сейчас оплачено дней: <b>{was_days}</b>. Сменишь тариф без "
            f"оплаты — станет <b>{switch_days}</b>: неиспользованное время не "
            "сгорает, а пересчитывается в новый тариф."
        )
    else:
        note = "Настрой количество кнопками − и +, потом выбери срок."

    return ui.screen(
        ui.title("⚙️", "Тариф"),
        lead="Собери тариф под себя — плати только за то, что нужно.",
        facts=facts,
        note=note,
        help=ui.help_block(
            "💡 Как это считается",
            f"{_extend_intro()}\n\n"
            "Чем длиннее срок, тем больше скидка. Купленный срок прибавляется "
            "к твоему остатку целиком — ни дня не теряется.",
        ),
    )


async def _render_tariff(edit, session, user, devices: int, bypass: int) -> None:
    """Собирает и рисует экран тарифа.

    Доступность смены без оплаты выясняем «сухим прогоном» самой смены: те же
    правила и тот же расчёт, что при нажатии, — значит кнопка не может обещать
    того, чего не произойдёт.
    """
    devices, bypass = _clamp_tariff(user, devices, bypass)
    max_dev, max_byp = _tariff_bounds(user)
    preview = await billing.change_tariff(
        session, user, max_devices=devices, max_bypass=bypass, dry_run=True
    )
    switch_days = preview.new_days if preview.ok else None
    await edit(
        build_tariff_text(user, devices, bypass, switch_days=switch_days),
        reply_markup=tariff_kb(
            devices, bypass, _term_price_rows(devices, bypass),
            max_dev, max_byp, switch_days=switch_days,
        ),
    )


@router.callback_query(F.data == f"{CB_BAL}:extend")
async def cb_bal_extend(call: CallbackQuery, session: AsyncSession) -> None:
    user = await _get_user(session, call)
    if user.sub_expires_at is None and not user.is_trial:
        await call.answer("У тебя бессрочная подписка — продлевать нечего 🙂", show_alert=True)
        return
    # Нулевые лимиты — это юзер, который ещё ничего не покупал: показываем ему
    # типовой тариф, а не пустой конструктор.
    await _render_tariff(
        call.message.edit_text, session, user,
        user.sub_max_devices or _START_DEVICES,
        user.sub_max_bypass or _START_BYPASS,
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:ext:"))
async def cb_bal_extend_adjust(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    if len(parts) != 4 or not parts[2].lstrip("-").isdigit() or not parts[3].lstrip("-").isdigit():
        await call.answer()  # кривой callback (форжат только руками) — молча игнор
        return
    user = await _get_user(session, call)
    try:
        await _render_tariff(call.message.edit_text, session, user, int(parts[2]), int(parts[3]))
    except TelegramBadRequest as exc:
        # На границах CB_NOP-заглушки перерисовку не дёргают, но старые
        # сообщения с прежней клавиатурой могут прислать то же состояние.
        if "message is not modified" not in str(exc):
            raise
    await call.answer()


# ── Смена тарифа без оплаты ──────────────────────────────────────────────────

# Почему отказ объясняем словами, а не прячем кнопку: до подтверждения юзер
# успевает изменить состояние (добавить устройство в другом окне), и «кнопка
# просто исчезла» читается как поломка.
_SWITCH_REFUSALS = {
    "trial": (
        "Сейчас идёт пробный период — эти дни подарены, а не куплены, "
        "обменивать их на другой тариф не на что. Выбери срок ниже: "
        "пробные дни при покупке не сгорят."
    ),
    "perpetual": "У тебя бессрочная подписка — менять в ней нечего 🙂",
    "expired": (
        "Подписка закончилась, пересчитывать нечего. Выбери срок ниже — "
        "новый тариф заработает сразу."
    ),
    "same": "Это твой текущий тариф — менять нечего.",
    "empty": "В тарифе должна остаться хотя бы одна позиция.",
}


def _switch_refusal(res) -> str:
    """Человеческое объяснение отказа. Два случая считаются по цифрам юзера,
    поэтому их нет в словаре."""
    if res.reason == "in_use":
        return (
            f"У тебя сейчас занято: {res.used_devices} устр. и "
            f"{res.used_bypass} рез. подключ. — тариф не может быть меньше. "
            "Сначала удали лишнее в «📱 Мои устройства» / "
            "«⚡ Резервное подключение»."
        )
    if res.reason == "too_short":
        return (
            f"На этом тарифе твоего остатка хватит меньше чем на день "
            f"({res.old_days} дн. превратятся в {res.new_days}). "
            "Так подписка обнулится — лучше выбери срок ниже и купи."
        )
    return _SWITCH_REFUSALS.get(res.reason, "Сменить тариф сейчас не получится.")


def _parse_tariff(data: str) -> tuple[int, int] | None:
    """`bal:chg:<dev>:<byp>` → (dev, byp). None — callback форжённый."""
    parts = data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        return None
    return int(parts[2]), int(parts[3])


@router.callback_query(F.data.startswith(f"{CB_BAL}:chg:"))
async def cb_bal_change_ask(call: CallbackQuery, session: AsyncSession) -> None:
    """Спрашиваем подтверждение: смена двигает дату окончания, и обратно её не
    отмотать — пересчёт округляется вниз, «верни как было» вернёт меньше."""
    parsed = _parse_tariff(call.data)
    if parsed is None:
        await call.answer("Что-то не то с тарифом, начни заново.", show_alert=True)
        return
    devices, bypass = parsed
    user = await _get_user(session, call)
    res = await billing.change_tariff(
        session, user, max_devices=devices, max_bypass=bypass, dry_run=True
    )
    if not res.ok:
        await call.answer(_switch_refusal(res), show_alert=True)
        return
    text = ui.screen(
        ui.title("⚙️", "Сменить тариф"),
        lead=(
            f"Новый тариф: <b>{devices}</b> устр. + <b>{bypass}</b> рез. "
            f"подключ., {fmt_rub(monthly_price_kopeks(devices, bypass))}/мес."
        ),
        facts=[
            ui.fact("📅", "Сейчас оплачено", f"{res.old_days} дн."),
            ui.fact("📅", "Станет", f"{res.new_days} дн."),
            ui.fact("💰", "Спишется", "0 ₽"),
        ],
        note=(
            "Деньги не списываются и не возвращаются — меняется только дата "
            "окончания. Отменить смену нельзя: обратный пересчёт вернёт на "
            "день-другой меньше."
        ),
    )
    await call.message.edit_text(text, reply_markup=tariff_confirm_kb(devices, bypass))
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_BAL}:chgok:"))
async def cb_bal_change_apply(call: CallbackQuery, session: AsyncSession) -> None:
    parsed = _parse_tariff(call.data)
    if parsed is None:
        await call.answer("Что-то не то с тарифом, начни заново.", show_alert=True)
        return
    devices, bypass = parsed
    user = await _get_user(session, call)
    res = await billing.change_tariff(
        session, user, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        # Между подтверждением и нажатием состояние могло измениться.
        await session.rollback()
        await call.answer(_switch_refusal(res), show_alert=True)
        return
    await session.commit()
    text = ui.screen(
        ui.title("✅", "Тариф изменён"),
        facts=[
            ui.fact("📱", "Устройства", devices),
            ui.fact("⚡", "Резервные подключения", bypass),
            ui.fact("📅", "Подписка до", f"{fmt_msk(res.new_expires_at)} МСК"),
        ],
        note=f"Осталось дней: <b>{res.new_days}</b>. Новый тариф работает уже сейчас.",
    )
    await call.message.edit_text(text, reply_markup=back_to_menu())
    await call.answer("Готово 🎉")


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
            f"У тебя сейчас активно {used_dev} устр. и {used_byp} рез. "
            "подключ. — тариф не может быть меньше. Сначала удали лишнее в "
            "«📱 Мои устройства» / «⚡ Резервное подключение».",
            show_alert=True,
        )
        return
    # Баланс запоминаем ДО списания: если покупка не пройдёт, session.rollback()
    # погасит загруженного юзера, и чтение его полей полезет в базу — в
    # async-контексте это MissingGreenlet ровно в той ветке, где юзеру надо
    # назвать нехватку (11.08.2026 так падало каждое «Купить» без денег).
    balance_before = user.balance_kopeks
    res = await billing.charge_and_extend(
        session, user, months, max_devices=devices, max_bypass=bypass
    )
    if not res.ok:
        await session.rollback()
        await call.answer(
            f"Не хватает {fmt_rub(res.missing_kopeks)}: цена "
            f"{fmt_rub(res.price_kopeks)}, на балансе "
            f"{fmt_rub(balance_before)}. "
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
    if not user.autopay:
        answer = "Автопродление выключено."
    else:
        # Называем срок и сумму сразу: «включено» без цифр юзер понимает
        # как «спишут месяц», а спишем мы столько, сколько он покупал.
        plan = billing.plan_autopay(user)
        answer = (
            f"Автопродление включено: при истечении спишем "
            f"{fmt_rub(plan[1])} за {term_label(plan[0])} с баланса."
            if plan is not None else
            "Автопродление включено: при истечении продлим подписку с баланса, "
            "если на нём хватит денег."
        )
    await call.answer(answer, show_alert=True)
