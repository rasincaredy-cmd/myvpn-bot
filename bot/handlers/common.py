from __future__ import annotations

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
    back_to_menu,
    main_menu,
    notify_settings_kb,
    onboarding_hint_kb,
    server_card,
)
from bot.texts import t

router = Router(name="common")


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
        await _send_main_menu(message, user.is_admin)
        await _send_onboarding_hint(message, is_new=not existed, is_admin=user.is_admin)
        return

    from bot.handlers.configs import redeem_invite

    if token:
        ok = await redeem_invite(message, session, user, token)
        if ok:
            return
        await message.answer(t.invite_invalid)

    await _send_main_menu(message, user.is_admin)
    await _send_onboarding_hint(message, is_new=not existed, is_admin=user.is_admin)


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
    await _send_main_menu(message, user.is_admin)
    await _send_onboarding_hint(message, is_new=not existed, is_admin=user.is_admin)


async def _send_onboarding_hint(message: Message, *, is_new: bool, is_admin: bool) -> None:
    """Второе сообщение сразу после ПЕРВОГО /start: триал выдаётся молча, и без
    наводки юзер не знает, что дальше. Кнопка ведёт прямо к добавлению устройства.
    Повторные /start (existed=True) подсказку не шлют — не спамим."""
    if not is_new or is_admin:
        return
    await message.answer(t.onboarding_hint, reply_markup=onboarding_hint_kb())


async def _send_main_menu(message: Message, is_admin: bool) -> None:
    if is_admin:
        text = t.start_admin.format(name=message.from_user.full_name or "друг")
    else:
        from bot.config import settings
        from bot.services.pricing import fmt_rub, monthly_price_kopeks

        text = t.start_user.format(
            name=message.from_user.full_name or "друг",
            trial_days=settings.trial_days,
            trial_devices=settings.trial_devices,
            trial_gb=settings.trial_traffic_gb,
            base_price=fmt_rub(monthly_price_kopeks(1, 1)),
        )
    await message.answer(text, reply_markup=main_menu(is_admin))


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
    await message.answer(t.menu_title, reply_markup=main_menu(user.is_admin))


@router.callback_query(F.data == f"{CB_MENU}:open")
async def cb_menu_open(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(t.menu_title, reply_markup=main_menu(user.is_admin))
    await call.answer()


@router.callback_query(F.data == f"{CB_MENU}:locations")
async def cb_menu_locations(call: CallbackQuery, session: AsyncSession) -> None:
    # Реальные локации сервиса из БД (Блок 8): готовые серверы = доступные страны.
    servers = await repo.list_ready_servers(session)
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
    await message.answer(t.cancelled, reply_markup=main_menu(user.is_admin))


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
                text, reply_markup=server_card(server.id, server.wdtt_enabled)
            )
            await call.answer("Отменено")
            return
    if dest == "panel":
        from bot.handlers.admin_panel import cmd_admin
        await cmd_admin(call, state)
        return

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(t.cancelled, reply_markup=main_menu(user.is_admin))
    await call.answer()
