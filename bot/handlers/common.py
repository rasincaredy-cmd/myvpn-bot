from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import (
    CB_CANCEL,
    CB_MENU,
    CB_NOP,
    back_to_menu,
    MenuState,
    main_menu,
    more_menu,
    notify_settings_kb,
    onboarding_hint_kb,
    server_card,
)
from bot.texts import t, ui

router = Router(name="common")



def build_sub_status_line(user) -> str:
    """Строка о подписке для главного меню: раньше за сроком нужно было идти
    в отдельный раздел «Подписка».

    NULL в sub_expires_at — БЕССРОЧНАЯ подписка (грандфазер, спец-юзеры, админ),
    а не отсутствие подписки: так это поле трактует весь остальной код
    (devices._sub_active, wdtt, billing, revive)."""
    expires = user.sub_expires_at
    if expires is None:
        return "🎫 Подписка: <b>бессрочно</b>"
    # SQLite отдаёт naive datetime — сравнивать с aware нельзя.
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expires <= now:
        return "🎫 Подписка: <b>не активна</b>"
    # Полные оставшиеся сутки, но не меньше 1: «0» на работающей подписке
    # читалось бы как «уже отключено».
    #
    # Округление ВВЕРХ обещало бы день, которого нет («5 суток и 1 час» → 6);
    # чистое округление вниз даёт 6 на ровно семи сутках, потому что доли
    # секунды между выдачей и показом уже съедены. Поэтому считаем сутки,
    # отбрасывая доли секунды.
    # Секунда допуска — на дорогу от выдачи подписки до показа экрана: без неё
    # ровно семь выданных суток превращались бы в «6 дней».
    left = expires - now
    days = max(1, int((left.total_seconds() + 1) // 86400))
    return f"🎫 Подписка: <b>активна</b>, осталось дней: <b>{days}</b>"


async def build_menu_state(session: AsyncSession, user) -> MenuState:
    """Состояние, от которого зависит набор кнопок главного меню.

    Считается в одном месте: меню собирается из /start, /menu и возврата
    «‹ Меню», и три копии этой логики разъехались бы на первой же правке.
    """
    from bot.handlers.devices import _sub_active

    return MenuState(
        sub_active=_sub_active(user),
        has_devices=await repo.count_active_devices(session, user.id) > 0,
        is_trial=bool(user.is_trial),
    )


async def send_start_screens(
    message: Message, user, *, is_new: bool, session: AsyncSession
) -> None:
    """Главное меню + подсказка новичку. Вызывается и из /start, и после
    нажатия «Согласен» на экране условий."""
    await _send_main_menu(message, user.is_admin, await build_menu_state(session, user))
    await _send_onboarding_hint(message, is_new=is_new, is_admin=user.is_admin)




# --- /start ------------------------------------------------------------------

@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    # Для рефералки важно знать, был ли юзер в базе ДО этого /start:
    # реферер привязывается только к действительно новым.
    existed = await repo.get_user_by_tg_id(session, message.from_user.id) is not None
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )

    token = (command.args or "").strip()

    # Реф-ссылка t.me/<bot>?start=ref_<user.id> (Блок «Баланс»).
    if token.startswith("ref_"):
        if not existed and user.referrer_id is None:
            ref_raw = token[4:]
            referrer = (
                await repo.get_user_by_id(session, int(ref_raw))
                if ref_raw.isdigit() else None
            )
            if referrer is not None and referrer.id != user.id:
                user.referrer_id = referrer.id
                await session.commit()
                logger.info("Referral: user {} invited by {}", user.id, referrer.id)
        # Гейт и здесь: иначе новый юзер по реф-ссылке получал бы меню, минуя
        # экран условий. Реферер уже привязан выше — согласие его не отменяет.
        await send_start_screens(message, user, is_new=not existed, session=session)
        return

    from bot.handlers.configs import redeem_invite

    if token:
        ok = await redeem_invite(message, session, user, token)
        if ok:
            return
        await message.answer(t.invite_invalid)

    await send_start_screens(message, user, is_new=not existed, session=session)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    existed = await repo.get_user_by_tg_id(session, message.from_user.id) is not None
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await send_start_screens(message, user, is_new=not existed, session=session)


async def _send_onboarding_hint(message: Message, *, is_new: bool, is_admin: bool) -> None:
    """Второе сообщение сразу после ПЕРВОГО /start: триал выдаётся молча, и без
    наводки юзер не знает, что дальше. Кнопка ведёт прямо к добавлению устройства.
    Повторные /start (existed=True) подсказку не шлют — не спамим."""
    if not is_new or is_admin:
        return
    await message.answer(t.onboarding_hint, reply_markup=onboarding_hint_kb())


async def _send_main_menu(
    message: Message, is_admin: bool, state: MenuState
) -> None:
    if is_admin:
        text = t.start_admin.format(name=ui.safe(message.from_user.full_name) or "друг")
    else:
        from bot.config import settings
        from bot.services.pricing import fmt_rub, monthly_price_kopeks

        text = t.start_user.format(
            name=ui.safe(message.from_user.full_name) or "друг",
            trial_days=settings.trial_days,
            trial_devices=settings.trial_devices,
            trial_bypass=settings.trial_bypass,
            trial_gb=settings.trial_traffic_gb,
            # «от» — значит от САМОГО дешёвого тарифа, а это одна позиция
            # (90 ₽). Подставлять сюда типовой 1+1 за 120 ₽ значит обещать
            # «от 120», когда есть тариф за 90.
            base_price=fmt_rub(monthly_price_kopeks(1, 0)),
        )
    await message.answer(text, reply_markup=main_menu(is_admin, state))


# Кнопки-заглушки (числа в конструкторе тарифа, «−»/«+» на границах): без этого
# хендлера Telegram крутил бы на них спиннер до таймаута.
@router.callback_query(F.data == CB_NOP)
async def cb_nop(call: CallbackQuery) -> None:
    await call.answer()


# --- /menu, /help ------------------------------------------------------------

@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await message.answer(
        f"{t.menu_title}\n\n{build_sub_status_line(user)}",
        reply_markup=main_menu(user.is_admin, await build_menu_state(session, user)),
    )


@router.callback_query(F.data == f"{CB_MENU}:open")
async def cb_menu_open(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(
        f"{t.menu_title}\n\n{build_sub_status_line(user)}",
        reply_markup=main_menu(user.is_admin, await build_menu_state(session, user)),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_MENU}:more")
async def cb_menu_more(call: CallbackQuery) -> None:
    """Раздел «Ещё»: настройки, витрина и документы.

    Своего содержимого у экрана нет — только навигация, поэтому текст короткий
    и не пытается пересказать то, что написано на кнопках.
    """
    await call.message.edit_text(t.more_title, reply_markup=more_menu())
    await call.answer()


@router.callback_query(F.data == f"{CB_MENU}:locations")
async def cb_menu_locations(call: CallbackQuery, session: AsyncSession) -> None:
    # Реальные локации сервиса из БД (Блок 8): готовые серверы = доступные страны.
    # Приватные серверы в витрине видят только админы и «друзья».
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    servers = await repo.list_ready_servers(session, for_user=user)
    lines = [t.locations_intro]
    if not servers:
        lines.append("\nПока идёт подготовка — заглядывай позже.")
    else:
        seen: list[str] = []
        for s in servers:
            loc = s.location or s.name  # fallback, если локация не задана
            if loc not in seen:
                seen.append(loc)
        for loc in seen:
            lines.append(f"{loc} — ✅ Доступно")
    lines.append(t.locations_footer)
    await call.message.edit_text("\n".join(lines), reply_markup=back_to_menu())
    await call.answer()


def _notify_text(enabled: bool) -> str:
    # Заголовок дословно повторяет кнопку меню «🔔 Оповещения» — связка
    # «нажал → увидел» без синонимов.
    state = "включены ✅" if enabled else "выключены 🔕"
    return (
        "🔔 <b>Оповещения</b>\n\n"
        f"Сейчас: <b>{state}</b>\n\n"
        "Бот заранее пришлёт сообщение, когда подписка будет заканчиваться — "
        "за 24 часа и за 1 час до отключения устройств."
    )


@router.callback_query(F.data == f"{CB_MENU}:notify")
async def cb_menu_notify(call: CallbackQuery, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(
        _notify_text(user.expiry_warn_enabled),
        reply_markup=notify_settings_kb(user.expiry_warn_enabled),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_MENU}:notify_toggle")
async def cb_menu_notify_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    user.expiry_warn_enabled = not user.expiry_warn_enabled
    await session.commit()
    await call.message.edit_text(
        _notify_text(user.expiry_warn_enabled),
        reply_markup=notify_settings_kb(user.expiry_warn_enabled),
    )
    await call.answer("Готово")


@router.message(Command("help"))
@router.callback_query(F.data == f"{CB_MENU}:help")
async def cmd_help(event: Message | CallbackQuery) -> None:
    from bot.config import settings
    from bot.keyboards.inline import support_intro_kb
    # Прямой контакт — опциональное дополнение к сапорт-чату (если задан в .env).
    contact_block = (
        f"\nНапрямую: {settings.support_contact}" if settings.support_contact else ""
    )
    text = t.help_text.format(contact_block=contact_block)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=support_intro_kb())
        await event.answer()
    else:
        await event.answer(text, reply_markup=support_intro_kb())


# --- /exit, /cancel — отмена любого состояния --------------------------------

@router.message(Command("exit", "cancel"))
async def cmd_exit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer(t.nothing_to_cancel)
        return
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await message.answer(
        t.cancelled,
        reply_markup=main_menu(user.is_admin, await build_menu_state(session, user)),
    )


@router.callback_query(F.data == CB_CANCEL)
async def cb_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Единая отмена: возвращаем ровно туда, откуда пришли (в тот же экран, in-place).

    Приоритет назначения (`cancel_to` кладут сами потоки в FSM-данные):
      • wdtt   → список обхода БС;
      • dev    → список устройств;
      • server_id → карточка сервера (создание peer/инвайта с карточки);
      • panel  → админ-панель (установка VPN, выбор сервера из панели);
      • иначе  → главное меню.
    Рендер делегируем реальным хендлерам, чтобы не дублировать экраны.
    """
    data = await state.get_data()
    await state.clear()
    dest = data.get("cancel_to")
    server_id = data.get("server_id")

    if dest == "wdtt":
        from bot.handlers.wdtt import cb_wdtt_my
        await cb_wdtt_my(call, state, session)
        return
    if dest == "dev":
        from bot.handlers.devices import cb_dev_list
        await cb_dev_list(call, session)
        return
    if dest == "bal":
        from bot.handlers.balance import cb_bal_my
        await cb_bal_my(call, state, session)
        return
    if server_id is not None:
        server = await repo.get_server(session, server_id)
        if server is not None:
            peers = await repo.list_peers_for_server(session, server.id)
            error_block = (
                f"\n<i>Last error:</i> <code>{server.last_error[:200]}</code>"
                if server.last_error
                else ""
            )
            text = t.server_card.format(
                name=server.name,
                host=server.host,
                wg_port=server.wg_port,
                status=server.status,
                peers=len(peers),
                error_block=error_block,
            )
            text += f"\n🌍 Локация: {server.location or '—'}"
            await call.message.edit_text(
                text,
                reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
            )
            await call.answer("Отменено")
            return
    if dest == "panel":
        from bot.handlers.admin import cmd_admin
        await cmd_admin(call, state)
        return

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(
        t.cancelled,
        reply_markup=main_menu(user.is_admin, await build_menu_state(session, user)),
    )
    await call.answer()
