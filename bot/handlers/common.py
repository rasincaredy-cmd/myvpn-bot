from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import (
    CB_CANCEL,
    CB_MENU,
    back_to_menu,
    main_menu,
    notify_settings_kb,
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
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )

    token = (command.args or "").strip()
    from bot.handlers.configs import redeem_invite

    if token:
        ok = await redeem_invite(message, session, user, token)
        if ok:
            return
        await message.answer(t.invite_invalid)

    await _send_main_menu(message, user.is_admin)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await _send_main_menu(message, user.is_admin)


async def _send_main_menu(message: Message, is_admin: bool) -> None:
    text = (t.start_admin if is_admin else t.start_user).format(
        name=message.from_user.full_name or "друг"
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
    state = "включены ✅" if enabled else "выключены 🔕"
    return (
        "🔔 <b>Предупреждения об истечении</b>\n\n"
        f"Сейчас: <b>{state}</b>\n\n"
        "Бот заранее пришлёт сообщение, когда срок действия твоего конфига "
        "подходит к концу — за 24 часа и за 1 час до автоотзыва."
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
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(t.help_text, reply_markup=back_to_menu())
        await event.answer()
    else:
        await event.answer(t.help_text, reply_markup=back_to_menu())


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
    # Если отменяли действие в контексте сервера (создание peer/инвайта/wdtt —
    # в FSM лежит server_id), возвращаем на карточку сервера, а не в главное меню.
    data = await state.get_data()
    await state.clear()
    server_id = data.get("server_id")
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
            await call.message.edit_text(
                text, reply_markup=server_card(server.id, server.wdtt_enabled)
            )
            await call.answer("Отменено")
            return

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(t.cancelled, reply_markup=main_menu(user.is_admin))
    await call.answer()
