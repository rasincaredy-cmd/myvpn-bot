"""Админ-панель: глобальная статистика, управление юзерами, рассылка."""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Invite, Peer, PeerStatus, Server, ServerStatus, User
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_PANEL,
    admin_panel_menu,
    back_to_panel,
    user_card_kb,
    users_list_kb,
)
from bot.loader import bot as tg_bot
from bot.states.install import BroadcastStates

router = Router(name="admin_panel")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

_PER_PAGE = 10


# --- Точка входа ------------------------------------------------------------

@router.message(Command("admin"))
@router.callback_query(F.data == f"{CB_PANEL}:main")
async def cmd_admin(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = "👮 <b>Админ-панель</b>"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=admin_panel_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=admin_panel_menu())


# --- Статистика -------------------------------------------------------------

@router.callback_query(F.data == f"{CB_PANEL}:stats")
async def cb_panel_stats(call: CallbackQuery, session: AsyncSession) -> None:
    users_total = (
        await session.execute(select(func.count(User.id)))
    ).scalar() or 0
    users_blocked = (
        await session.execute(
            select(func.count(User.id)).where(User.is_blocked.is_(True))
        )
    ).scalar() or 0
    servers_total = (
        await session.execute(select(func.count(Server.id)))
    ).scalar() or 0
    servers_ready = (
        await session.execute(
            select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
        )
    ).scalar() or 0
    peers_total = (
        await session.execute(select(func.count(Peer.id)))
    ).scalar() or 0
    peers_active = (
        await session.execute(
            select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0
    invites_total = (
        await session.execute(select(func.count(Invite.id)))
    ).scalar() or 0
    invites_pending = (
        await session.execute(
            select(func.count(Invite.id)).where(Invite.used_at.is_(None))
        )
    ).scalar() or 0

    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b>  (🔴 заблокировано: {users_blocked})\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / <b>{servers_total}</b> всего\n"
        f"📄 Peers: <b>{peers_active}</b> активных / <b>{peers_total}</b> всего\n"
        f"🎟 Инвайтов: <b>{invites_pending}</b> непогашенных / <b>{invites_total}</b> всего",
        reply_markup=back_to_panel(),
    )
    await call.answer()


# --- Пользователи -----------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:users:"))
async def cb_panel_users(call: CallbackQuery, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    total = await repo.count_users(session)
    users = await repo.list_all_users(session, offset=page * _PER_PAGE, limit=_PER_PAGE)

    await call.message.edit_text(
        f"👤 <b>Пользователи</b>  (всего: {total})\n"
        f"Страница {page + 1} из {max(1, -(-total // _PER_PAGE))}",
        reply_markup=users_list_kb(
            users,
            page,
            has_prev=page > 0,
            has_next=(page + 1) * _PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:user:"))
async def cb_panel_user_open(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return

    peers = await repo.list_peers_for_user(session, user.id)
    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    status = "🔴 Заблокирован" if user.is_blocked else ("👑 Админ" if user.is_admin else "👤 Юзер")

    await call.message.edit_text(
        f"👤 <b>{user.full_name or '—'}</b>\n"
        f"• Username: {('@' + user.username) if user.username else '—'}\n"
        f"• Telegram ID: <code>{user.tg_id}</code>\n"
        f"• Статус: {status}\n"
        f"• Peers: <b>{active}</b> активных / {len(peers)} всего\n"
        f"• С нами с: {user.created_at.strftime('%d.%m.%Y')}",
        reply_markup=user_card_kb(user.id, user.is_blocked, page),
    )
    await call.answer()


@router.callback_query(
    F.data.startswith(f"{CB_PANEL}:block:") | F.data.startswith(f"{CB_PANEL}:unblock:")
)
async def cb_panel_toggle_block(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    action, user_id, page = parts[1], int(parts[2]), int(parts[3])
    block = action == "block"

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    if user.is_admin:
        await call.answer("Нельзя заблокировать другого админа.", show_alert=True)
        return

    await repo.set_user_blocked(session, user.id, block)
    await session.commit()

    peers = await repo.list_peers_for_user(session, user.id)
    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    status = "🔴 Заблокирован" if block else "👤 Юзер"

    await call.message.edit_text(
        f"👤 <b>{user.full_name or '—'}</b>\n"
        f"• Username: {('@' + user.username) if user.username else '—'}\n"
        f"• Telegram ID: <code>{user.tg_id}</code>\n"
        f"• Статус: {status}\n"
        f"• Peers: <b>{active}</b> активных / {len(peers)} всего",
        reply_markup=user_card_kb(user.id, block, page),
    )
    await call.answer("✅ Готово")


# --- Рассылка ---------------------------------------------------------------

@router.callback_query(F.data == f"{CB_PANEL}:broadcast")
async def cb_panel_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.text)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:main")
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправь текст сообщения (поддерживается HTML):",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(BroadcastStates.text, F.text)
async def step_broadcast_text(message: Message, state: FSMContext) -> None:
    await state.update_data(text=message.text)
    await state.set_state(BroadcastStates.confirm)

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Разослать",  callback_data=f"{CB_PANEL}:broadcast_send")
    kb.button(text="✖️ Отмена",     callback_data=f"{CB_PANEL}:main")
    kb.adjust(2)

    await message.answer(
        f"📢 <b>Предпросмотр:</b>\n\n{message.text}\n\n"
        "Разослать всем активным пользователям?",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BroadcastStates.confirm, F.data == f"{CB_PANEL}:broadcast_send")
async def cb_broadcast_send(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    await state.clear()
    text = data.get("text", "")

    await call.message.edit_text("⏳ Рассылаю...")
    await call.answer()

    users = await repo.list_all_users_for_broadcast(session)
    sent = failed = 0
    for user in users:
        try:
            await tg_bot.send_message(user.tg_id, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/s, не словить flood

    await call.message.edit_text(
        f"📢 <b>Рассылка завершена</b>\n\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        reply_markup=back_to_panel(),
    )
