from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import CB_CANCEL, CB_MENU, back_to_menu, main_menu
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
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await call.message.edit_text(t.cancelled, reply_markup=main_menu(user.is_admin))
    await call.answer()
