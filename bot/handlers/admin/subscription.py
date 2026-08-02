"""Карточка подписки юзера: лимиты, срок, баланс, триал, трафик, отключение."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction
from bot.handlers.admin.common import trial_line
from bot.keyboards.inline import (
    CB_PANEL,
    admin_sub_give_kb,
    admin_sub_kb,
    cancel_to_sub_kb,
)
from bot.loader import bot as tg_bot
from bot.services import amnezia
from bot.services import revive as revive_svc
from bot.services.pricing import TERM_LABELS
from bot.states.install import SubAdminStates
from bot.utils.timefmt import as_utc, fmt_msk
from bot.utils.validators import parse_expiry, parse_traffic_limit

router = Router(name="admin_sub")


async def _render_sub_card(call: CallbackQuery, session: AsyncSession, user, page: int) -> None:
    used = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    sub_expired = (
        user.sub_expires_at is not None
        and as_utc(user.sub_expires_at) <= datetime.now(timezone.utc)
    )
    if user.sub_expires_at is None:
        srok = "бессрочно"
    else:
        srok = (
            f"{'истекла' if sub_expired else 'до'} "
            f"{fmt_msk(user.sub_expires_at)} МСК"
        )
    trf = amnezia.fmt_traffic_line(await repo.sub_traffic_used(session, user),
                                   user.sub_traffic_limit_bytes, sub_expired)
    await call.message.edit_text(
        f"🎫 <b>Подписка — {user.full_name or user.tg_id}</b>\n"
        f"• Устройства: <b>{used}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{srok}</b>\n"
        f"• Триал: <b>{trial_line(user)}</b>\n"
        f"• Трафик: <b>{trf}</b>",
        reply_markup=admin_sub_kb(user.id, page, is_trial=user.is_trial),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub:"))
async def cb_panel_sub(
    call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None
) -> None:
    # Сюда же ведёт «✖️ Отмена» из ввода лимитов/срока/трафика/баланса — чистим
    # FSM, иначе следующее сообщение админа улетит в брошенный step-хендлер.
    if state is not None:
        await state.clear()
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await _render_sub_card(call, session, user, page)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_lim:"))
async def cb_panel_sub_lim(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_limit)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "📱 Введи новый лимит устройств (0–50):",
        reply_markup=cancel_to_sub_kb(int(parts[2]), int(parts[3])),
    )
    await call.answer()


@router.message(SubAdminStates.set_limit, F.text)
async def step_sub_limit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text.strip().isdigit() or not (0 <= int(message.text.strip()) <= 50):
        d = await state.get_data()
        await message.answer(
            "Нужно число 0–50. Ещё раз:",
            reply_markup=cancel_to_sub_kb(d["user_id"], d["page"]),
        )
        return
    data = await state.get_data()
    await state.clear()
    await repo.set_subscription(session, data["user_id"], max_devices=int(message.text.strip()))
    user = await repo.get_user_by_id(session, data["user_id"])
    if user is None:
        await message.answer("Юзер уже удалён.")
        return
    # refresh, а не commit: в журнале должен стоять итоговый тариф — тот, что
    # админ видит в карточке, а не только изменённая половина. Раньше свежие
    # значения добывались коммитом, и запись уходила ОТДЕЛЬНОЙ транзакцией —
    # сбой между двумя коммитами оставлял тариф изменённым без следа в истории.
    await session.refresh(user)
    await repo.log_action(
        session, AuditAction.TARIFF_CHANGED,
        actor_tg_id=message.from_user.id,
        actor_is_admin=True,
        target_user_id=user.id,
        details=f"Тариф: устройств {user.sub_max_devices}, обходов {user.sub_max_bypass}",
    )
    await session.commit()
    await message.answer(
        f"✅ Лимит устройств: <b>{user.sub_max_devices}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"], is_trial=user.is_trial),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_bp:"))
async def cb_panel_sub_bp(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_bypass)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "🛡 Введи лимит доступов обхода БС (0–50):",
        reply_markup=cancel_to_sub_kb(int(parts[2]), int(parts[3])),
    )
    await call.answer()


@router.message(SubAdminStates.set_bypass, F.text)
async def step_sub_bypass(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text.strip().isdigit() or not (0 <= int(message.text.strip()) <= 50):
        d = await state.get_data()
        await message.answer(
            "Нужно число 0–50. Ещё раз:",
            reply_markup=cancel_to_sub_kb(d["user_id"], d["page"]),
        )
        return
    data = await state.get_data()
    await state.clear()
    await repo.set_subscription(session, data["user_id"], max_bypass=int(message.text.strip()))
    user = await repo.get_user_by_id(session, data["user_id"])
    if user is None:
        await message.answer("Юзер уже удалён.")
        return
    # Тот же итоговый тариф и та же одна транзакция, что при смене лимита
    # устройств: одно событие на любую правку тарифа, чтобы в ленте читалось
    # «стало столько-то».
    await session.refresh(user)
    await repo.log_action(
        session, AuditAction.TARIFF_CHANGED,
        actor_tg_id=message.from_user.id,
        actor_is_admin=True,
        target_user_id=user.id,
        details=f"Тариф: устройств {user.sub_max_devices}, обходов {user.sub_max_bypass}",
    )
    await session.commit()
    await message.answer(
        f"✅ Лимит обхода БС: <b>{user.sub_max_bypass}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"], is_trial=user.is_trial),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_bal:"))
async def cb_panel_sub_bal(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user = await repo.get_user_by_id(session, int(parts[2]))
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(SubAdminStates.set_balance)
    await state.update_data(user_id=user.id, page=int(parts[3]))
    from bot.services.pricing import fmt_rub
    await call.message.edit_text(
        f"💰 <b>Баланс юзера: {fmt_rub(user.balance_kopeks)}</b>\n\n"
        "Введи изменение в рублях со знаком: <code>+90</code> — начислить "
        "(например, за перевод на карту), <code>-50</code> — списать.",
        reply_markup=cancel_to_sub_kb(user.id, int(parts[3])),
    )
    await call.answer()


@router.message(SubAdminStates.set_balance, F.text)
async def step_sub_balance(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = message.text.strip().replace("₽", "").strip()
    sign = raw[:1]
    if sign not in "+-" or not raw[1:].isdigit() or int(raw[1:]) == 0 or int(raw[1:]) > 1_000_000:
        d = await state.get_data()
        await message.answer(
            "Формат: <code>+90</code> или <code>-50</code> (рубли). Ещё раз:",
            reply_markup=cancel_to_sub_kb(d["user_id"], d["page"]),
        )
        return
    data = await state.get_data()
    await state.clear()
    amount = int(raw[1:]) * 100 * (1 if sign == "+" else -1)
    from bot.services.pricing import fmt_rub
    await repo.add_balance_tx(
        session, data["user_id"], amount, "admin", note="Ручная правка админом"
    )
    # Знак суммы сохраняем как есть: со взятым по модулю списание выглядело бы
    # в ленте пополнением.
    await repo.log_action(
        session, AuditAction.ADMIN_CREDIT,
        actor_tg_id=message.from_user.id,
        actor_is_admin=True,
        target_user_id=data["user_id"],
        amount_kopeks=amount,
        details=(
            f"Админ вручную {'начислил' if amount > 0 else 'списал'} "
            f"{fmt_rub(abs(amount))}"
        ),
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    if user is None:
        await message.answer("Юзер уже удалён — правка баланса отменена.")
        return
    logger.info("Admin balance adjust: user {} {}{} kopeks", user.id, sign, abs(amount))
    try:
        await tg_bot.send_message(
            user.tg_id,
            f"💰 Админ изменил твой баланс: <b>{fmt_rub(amount)}</b>. "
            f"Сейчас на счету: <b>{fmt_rub(user.balance_kopeks)}</b>.",
        )
    except Exception:
        pass
    # Начисление юзеру с истёкшей подпиской и включённым автопродлением —
    # продлеваем сразу, не заставляя ждать тика планировщика (до 5 минут).
    extra = ""
    if amount > 0:
        from bot.handlers.balance import notify_autopay
        from bot.services import billing
        ap = await billing.autopay_if_expired(session, user)
        if ap is not None:
            await session.commit()
            await notify_autopay(user, ap)
            extra = (
                f"\n♻️ Подписка сразу продлена автопродлением на месяц "
                f"(−{fmt_rub(ap.price_kopeks)})."
            )
    await message.answer(
        f"✅ Баланс: <b>{fmt_rub(user.balance_kopeks)}</b>{extra}",
        reply_markup=admin_sub_kb(user.id, data["page"], is_trial=user.is_trial),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_ext:"))
async def cb_panel_sub_ext(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.extend_days)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "📅 <b>Срок подписки</b>\n\n"
        "Введи дату <code>ДД.ММ.ГГГГ</code> или период <code>Nд</code> (напр. <code>30д</code>).\n"
        "Можно со временем: <code>30д 18:00</code>, <code>31.12.2025 09:30</code>.\n"
        "Без времени: период — от текущего момента, дата — на 23:59 МСК.\n"
        "<i>Время везде московское.</i>\n"
        "Отправь <code>-</code>, чтобы сделать бессрочной.",
        reply_markup=cancel_to_sub_kb(int(parts[2]), int(parts[3])),
    )
    await call.answer()


@router.message(SubAdminStates.extend_days, F.text)
async def step_sub_extend(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = parse_expiry(message.text.strip())
    if result == "invalid":
        d = await state.get_data()
        await message.answer(
            "Не понял формат. Примеры: <code>30д</code>, <code>30д 18:00</code>, "
            "<code>31.12.2025</code>, <code>31.12.2025 09:30</code>, <code>-</code>",
            reply_markup=cancel_to_sub_kb(d["user_id"], d["page"]),
        )
        return
    data = await state.get_data()
    await state.clear()
    # Задание срока = выдача платной подписки: снимаем флаг триала, новый период
    # трафика (base := текущая Σ).
    await repo.set_subscription(
        session, data["user_id"],
        expires_at=result, touch_expires=True, reset_traffic_base=True, mark_paid=True,
    )
    # Этот путь выдаёт подписку в обход billing.grant_term (там срок из прайса,
    # здесь — произвольная дата), поэтому запись в журнал делается тут же, иначе
    # такой выдачи в ленте не будет вовсе. Формулировка отличает ручной срок от
    # обычной выдачи тарифа.
    #
    # Дата в прошлом — это не выдача, а отключение: тем же полем ввода админ
    # гасит подписку (ниже по коду устройства отзываются сразу). Писать оба
    # случая кодом SUB_GRANTED значит показывать отключение в ленте как
    # «🎁 Подписка выдана» — админ, разбирая жалобу, прочтёт ровно обратное
    # тому, что произошло.
    # now берётся один раз и используется и здесь, и в ветвлении ревайв/отзыв
    # ниже: два отдельных вызова now() могли бы разойтись на дате «прямо сейчас»
    # и записать в журнал не то, что бот на самом деле сделал.
    now = datetime.now(timezone.utc)
    turning_off = result is not None and result <= now
    await repo.log_action(
        session,
        AuditAction.SUB_REVOKED if turning_off else AuditAction.SUB_GRANTED,
        actor_tg_id=message.from_user.id,
        actor_is_admin=True,
        target_user_id=data["user_id"],
        details=(
            f"Админ отключил подписку: срок задан в прошлом ({fmt_msk(result)} МСК)"
            if turning_off else
            f"Админ вручную задал срок подписки: до {fmt_msk(result)} МСК"
            if result else "Админ вручную сделал подписку бессрочной"
        ),
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    if user is None:
        await message.answer("Юзер уже удалён.")
        return
    msg = (
        f"✅ Срок установлен: <b>{fmt_msk(result)} МСК</b>"
        if result else "✅ Подписка сделана бессрочной."
    )

    # Ревайв: если у юзера есть отозванные по истечению устройства — возвращаем
    # их к жизни (те же конфиги/ссылки). Новый срок активен ⇒ можно оживлять.
    sub_line = (
        f"до {fmt_msk(result)} МСК" if result else "бессрочная"
    )
    if not turning_off:
        rv = await revive_svc.revive_devices_for_user(session, user)
        await session.commit()
        if rv.touched:
            msg += f"\n♻️ Восстановлено: устройств <b>{rv.devices_restored}</b>, обходов БС <b>{rv.bypass_restored}</b>."
            if rv.devices_skipped_limit or rv.bypass_skipped_limit:
                msg += (
                    f"\n⚠️ Не влезло в лимиты: устройств {rv.devices_skipped_limit}, "
                    f"обходов {rv.bypass_skipped_limit} (остались отозванными)."
                )
            if rv.errors:
                msg += "\n❌ Не восстановлено: " + "; ".join(rv.errors)
        notify = f"🎉 Подписка продлена ({sub_line})."
        if rv.devices_restored or rv.bypass_restored:
            notify += (
                "\n♻️ Твои устройства восстановлены — прежние конфиги и ссылки "
                "снова работают, ничего перенастраивать не нужно."
            )
        try:
            await tg_bot.send_message(user.tg_id, notify)
        except Exception:
            pass
    else:
        # Срок задан в прошлом = отключение: конфиги гаснут сразу, не ждём тика.
        # Гасит человек, а не тик планировщика, — актором идёт админ, иначе в
        # ленте это неотличимо от честного истечения срока.
        if await revive_svc.revoke_devices_for_user(
            session, user.id,
            actor_tg_id=message.from_user.id,
            actor_is_admin=True,
            reason="админ задал срок подписки в прошлом",
        ):
            await session.commit()
            msg += "\n🚫 Срок в прошлом — устройства отозваны сразу."
            try:
                await tg_bot.send_message(
                    user.tg_id,
                    "⏱ Подписка истекла — устройства и доступы обхода отключены.\n"
                    "Конфиги сохраняются 30 дней: продлишь подписку — всё оживёт само.",
                )
            except Exception:
                pass

    await message.answer(
        msg, reply_markup=admin_sub_kb(user.id, data["page"], is_trial=user.is_trial)
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_give:"))
async def cb_panel_sub_give(call: CallbackQuery, session: AsyncSession) -> None:
    """Выдать подписку на один из тарифных сроков — то же, что юзер купил бы
    сам, но бесплатно (компенсация, подарок, оплата мимо бота)."""
    from bot.services.pricing import (
        TERM_DISCOUNTS, fmt_rub, monthly_price_kopeks, term_price_kopeks,
    )

    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    if user.sub_max_devices + user.sub_max_bypass < 1:
        await call.answer(
            "У юзера тариф 0 устройств и 0 обходов — сначала задай лимиты, "
            "иначе подписка будет пустой.",
            show_alert=True,
        )
        return
    monthly = monthly_price_kopeks(user.sub_max_devices, user.sub_max_bypass)
    terms = [
        (m, f"{TERM_LABELS[m]} — {fmt_rub(term_price_kopeks(monthly, m))}")
        for m in sorted(TERM_DISCOUNTS)
    ]
    await call.message.edit_text(
        f"🎫 <b>Выдать подписку — {user.full_name or user.tg_id}</b>\n\n"
        f"Тариф юзера: <b>{user.sub_max_devices}</b> устр. + "
        f"<b>{user.sub_max_bypass}</b> обх. = <b>{fmt_rub(monthly)}/мес</b>.\n"
        "Деньги <b>не списываются</b> — суммы на кнопках лишь показывают, "
        "на сколько ты даришь.\n\n"
        "Срок прибавится к остатку (текущая подписка не сгорит), лимит трафика "
        "снимется, отозванные устройства оживут. Дальше автопродление будет "
        "брать этот же срок.",
        reply_markup=admin_sub_give_kb(user.id, page, terms),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_gdo:"))
async def cb_panel_sub_give_do(call: CallbackQuery, session: AsyncSession) -> None:
    from bot.services import billing
    from bot.services.pricing import fmt_rub

    parts = call.data.split(":")
    user_id, page, months = int(parts[2]), int(parts[3]), int(parts[4])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Юзер уже удалён", show_alert=True)
        return
    await call.answer("⏳ Выдаю...")
    try:
        res = await billing.grant_term(
            session, user, months, actor_tg_id=call.from_user.id
        )
    except ValueError:
        await call.answer("Такой срок не продаётся", show_alert=True)
        return
    await session.commit()
    msg = (
        f"🎫 Выдана подписка на <b>{TERM_LABELS.get(months, f'{months} мес')}</b> "
        f"— до <b>{fmt_msk(res.new_expires_at)} МСК</b>.\n"
        f"Списано: <b>{fmt_rub(0)}</b> (выдача админом)."
    )
    rv = res.revive
    if rv is not None and rv.touched:
        msg += (
            f"\n♻️ Восстановлено: устройств <b>{rv.devices_restored}</b>, "
            f"обходов БС <b>{rv.bypass_restored}</b>."
        )
        if rv.errors:
            msg += "\n❌ Не восстановлено: " + "; ".join(rv.errors)
    logger.info("Admin granted {} mo to user {}", months, user.id)
    try:
        await tg_bot.send_message(
            user.tg_id,
            f"🎁 Тебе выдана подписка на <b>{TERM_LABELS.get(months, f'{months} мес')}</b> "
            f"— до {fmt_msk(res.new_expires_at)} МСК.\n"
            "Загляни в «📱 Мои устройства» — всё уже работает."
            + ("\n♻️ Твои устройства восстановлены: прежние конфиги и ссылки "
               "снова действуют." if rv is not None and
               (rv.devices_restored or rv.bypass_restored) else ""),
        )
    except Exception:
        pass
    await call.message.edit_text(
        msg, reply_markup=admin_sub_kb(user.id, page, is_trial=user.is_trial)
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_trl:"))
async def cb_panel_sub_trial(call: CallbackQuery, session: AsyncSession) -> None:
    """Выдать триал заново (Блок «Мелочи 2»): те же условия, что новому юзеру —
    срок, лимит устройств и трафика из конфига, флаг is_trial обратно. Нужно для
    тестов на своём аккаунте и для «дай ещё раз посмотреть» вручную; сам по себе
    триал выдаётся только при регистрации и второй раз не приходит."""
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    expires = datetime.now(timezone.utc) + timedelta(days=settings.trial_days)
    await repo.set_subscription(
        session, user.id,
        max_devices=settings.trial_devices,
        expires_at=expires, touch_expires=True,
        traffic_limit_bytes=(
            settings.trial_traffic_gb * 1024**3 if settings.trial_traffic_gb else None
        ),
        touch_traffic_limit=True,
        reset_traffic_base=True,
        mark_trial=True,
    )
    # Триал вторым заходом — редкое ручное решение админа, и в ленте оно должно
    # отличаться от обычной выдачи тарифа: юзер получает срок бесплатно.
    # Одной транзакцией с самой выдачей, как и остальные админские события.
    await repo.log_action(
        session, AuditAction.SUB_GRANTED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=user_id,
        details=(
            f"Админ выдал триал заново: {settings.trial_days} дн. "
            f"(до {fmt_msk(expires)} МСК), устройств {settings.trial_devices}, "
            f"трафик {settings.trial_traffic_gb or '∞'} ГБ"
        ),
    )
    await session.commit()
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Юзер уже удалён", show_alert=True)
        return
    # Как при продлении срока: отозванные по истечению устройства оживают с
    # прежними конфигами, а не выдаются заново.
    msg = (
        f"🎁 Триал выдан: <b>{settings.trial_days} дн.</b> "
        f"(до {fmt_msk(expires)} МСК), устройств "
        f"<b>{settings.trial_devices}</b>, трафик "
        f"<b>{settings.trial_traffic_gb or '∞'} ГБ</b>."
    )
    rv = await revive_svc.revive_devices_for_user(session, user)
    await session.commit()
    if rv.touched:
        msg += (
            f"\n♻️ Восстановлено: устройств <b>{rv.devices_restored}</b>, "
            f"обходов БС <b>{rv.bypass_restored}</b>."
        )
        if rv.errors:
            msg += "\n❌ Не восстановлено: " + "; ".join(rv.errors)
    logger.info("Admin granted trial to user {} until {}", user.id, expires)
    try:
        await tg_bot.send_message(
            user.tg_id,
            f"🎁 Тебе выдан пробный период на {settings.trial_days} дн. — "
            "загляни в «📱 Мои устройства».",
        )
    except Exception:
        pass
    await call.message.edit_text(
        msg, reply_markup=admin_sub_kb(user.id, page, is_trial=True)
    )
    await call.answer("Триал выдан")


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_trf:"))
async def cb_panel_sub_trf(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_traffic)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "📊 Введи лимит трафика на подписку: <code>50GB</code>, <code>500MB</code>, "
        "<code>1TB</code>.\nОтправь <code>-</code>, чтобы снять лимит (безлимит).",
        reply_markup=cancel_to_sub_kb(int(parts[2]), int(parts[3])),
    )
    await call.answer()


@router.message(SubAdminStates.set_traffic, F.text)
async def step_sub_traffic(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = parse_traffic_limit(message.text.strip())
    if result == "invalid":
        d = await state.get_data()
        await message.answer(
            "Формат: <code>50GB</code> / <code>500MB</code> / <code>1TB</code> "
            "или <code>-</code>. Ещё раз:",
            reply_markup=cancel_to_sub_kb(d["user_id"], d["page"]),
        )
        return
    data = await state.get_data()
    await state.clear()
    # result: int (байты) или None (снять лимит). Новый лимит → период с нуля.
    await repo.set_subscription(
        session, data["user_id"],
        traffic_limit_bytes=result, touch_traffic_limit=True, reset_traffic_base=True,
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    if user is None:
        await message.answer("Юзер уже удалён.")
        return
    trf = "безлимит" if result is None else amnezia.fmt_bytes(result)
    await message.answer(
        f"✅ Лимит трафика подписки: <b>{trf}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"], is_trial=user.is_trial),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_off:"))
async def cb_panel_sub_off(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    now = datetime.now(timezone.utc)
    await repo.set_subscription(session, user_id, expires_at=now, touch_expires=True)
    # Само отключение — отдельным событием от отзыва конфигов ниже: строки про
    # устройства отвечают на «что погасло», а эта — на «кто выключил подписку».
    # Без неё у юзера без единого устройства отключение не оставило бы следа.
    await repo.log_action(
        session, AuditAction.SUB_REVOKED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=user_id,
        details="Админ отключил подписку кнопкой",
    )
    # Конфиги гаснут сразу, а не на тике планировщика (симметрично мгновенному
    # ревайву при продлении). Строки остаются REVOKED — продление всё оживит.
    revoked = await revive_svc.revoke_devices_for_user(
        session, user_id,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        reason="админ отключил подписку",
    )
    await session.commit()
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        # Юзера стёрли, пока карточка висела открытой: отзыв выше уже отработал
        # (он идёт по user_id и переживает владельца), уведомлять некого.
        await call.answer("Юзер уже удалён", show_alert=True)
        return
    if revoked:
        from bot.services.scheduler import REVOKED_RETENTION_DAYS
        try:
            await tg_bot.send_message(
                user.tg_id,
                "⏱ Подписка истекла — устройства и доступы обхода отключены.\n"
                f"Конфиги сохраняются {REVOKED_RETENTION_DAYS} дней: продлишь "
                "подписку — всё оживёт само, перенастраивать не придётся.\n"
                "Продлить: меню → «🎫 Моя подписка» → «🔁 Продлить» "
                "(пополнить баланс — «💰 Баланс»).",
            )
        except Exception:
            pass
    await _render_sub_card(call, session, user, page)
    await call.answer(
        "Подписка отключена, устройства отозваны" if revoked
        else "Подписка отключена (активных устройств не было)"
    )
