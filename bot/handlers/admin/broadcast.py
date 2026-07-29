"""Рассылка: выбор аудитории (в т.ч. поштучно), предпросмотр и отправка копией."""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import (
    CB_PANEL,
    back_to_panel,
    broadcast_confirm_kb,
    broadcast_select_kb,
    broadcast_target_kb,
)
from bot.loader import bot as tg_bot
from bot.states.install import BroadcastStates

router = Router(name="admin_broadcast")

_BC_TARGET_LABEL = {
    "all": "всем",
    "active": "с активной подпиской",
    "inactive": "без активной подписки",
    "manual": "выбранным вручную",
}
_BC_SEL_PER_PAGE = 8


async def _ask_broadcast_message(call: CallbackQuery, state: FSMContext, target: str) -> None:
    await state.set_state(BroadcastStates.message)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:main")
    await call.message.edit_text(
        f"📢 <b>Рассылка → {_BC_TARGET_LABEL.get(target, target)}</b>\n\n"
        "Пришли сообщение для рассылки — <b>любого типа</b>: текст, фото, видео, "
        "стикер, GIF (можно с подписью). Отправлю его получателям как есть.",
        reply_markup=kb.as_markup(),
    )


async def _render_bc_select(call: CallbackQuery, state: FSMContext, session: AsyncSession, page: int) -> None:
    data = await state.get_data()
    selected = set(data.get("bc_selected", []))
    total = await repo.count_users(session)
    users = await repo.list_all_users(session, offset=page * _BC_SEL_PER_PAGE, limit=_BC_SEL_PER_PAGE)
    rows = []
    for u in users:
        name = (f"@{u.username}" if u.username else None) or u.full_name or f"id{u.tg_id}"
        rows.append((u.id, u.id in selected, name))
    await call.message.edit_text(
        f"✍️ <b>Выбор получателей</b>\nОтмечено: <b>{len(selected)}</b>\n"
        f"Страница {page + 1} из {max(1, -(-total // _BC_SEL_PER_PAGE))}",
        reply_markup=broadcast_select_kb(
            rows, len(selected), page,
            has_prev=page > 0, has_next=(page + 1) * _BC_SEL_PER_PAGE < total,
        ),
    )


@router.callback_query(F.data == f"{CB_PANEL}:broadcast")
async def cb_panel_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.target)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nКому отправляем?",
        reply_markup=broadcast_target_kb(),
    )
    await call.answer()


@router.callback_query(BroadcastStates.target, F.data.startswith(f"{CB_PANEL}:bc_to:"))
async def cb_broadcast_target(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    target = call.data.rsplit(":", 1)[-1]
    await state.update_data(bc_target=target)
    if target == "manual":
        await state.update_data(bc_selected=[])
        await state.set_state(BroadcastStates.select)
        await _render_bc_select(call, state, session, 0)
        await call.answer()
        return
    await _ask_broadcast_message(call, state, target)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data.startswith(f"{CB_PANEL}:bc_sel:"))
async def cb_broadcast_select_toggle(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = call.data.split(":")
    uid, page = int(parts[2]), int(parts[3])
    data = await state.get_data()
    selected = set(data.get("bc_selected", []))
    selected.symmetric_difference_update({uid})  # toggle
    await state.update_data(bc_selected=list(selected))
    await _render_bc_select(call, state, session, page)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data.startswith(f"{CB_PANEL}:bc_selpg:"))
async def cb_broadcast_select_page(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    await _render_bc_select(call, state, session, page)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data == f"{CB_PANEL}:bc_seldone")
async def cb_broadcast_select_done(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("bc_selected"):
        await call.answer("Никто не выбран", show_alert=True)
        return
    await _ask_broadcast_message(call, state, "manual")
    await call.answer()


@router.message(BroadcastStates.message)
async def step_broadcast_message(message: Message, state: FSMContext) -> None:
    # Запоминаем ссылку на сообщение — разошлём копией (copy_message тянет любой тип).
    await state.update_data(bc_from_chat=message.chat.id, bc_msg_id=message.message_id)
    await state.set_state(BroadcastStates.confirm)
    data = await state.get_data()
    target = data.get("bc_target", "all")
    await message.answer(
        f"📢 <b>Предпросмотр ↑</b>\n\n"
        f"Разослать <b>{_BC_TARGET_LABEL.get(target, target)}</b>?",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(BroadcastStates.confirm, F.data == f"{CB_PANEL}:bc_send")
async def cb_broadcast_send(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    await state.clear()
    target = data.get("bc_target", "all")
    from_chat = data.get("bc_from_chat")
    msg_id = data.get("bc_msg_id")
    if from_chat is None or msg_id is None:
        await call.answer("Нет сообщения для рассылки", show_alert=True)
        return

    await call.message.edit_text("⏳ Рассылаю...")
    await call.answer()

    if target == "manual":
        users = await repo.list_users_by_ids(session, data.get("bc_selected", []))
    else:
        users = await repo.list_broadcast_targets(session, target)
    sent = failed = 0
    for user in users:
        try:
            await tg_bot.copy_message(
                chat_id=user.tg_id, from_chat_id=from_chat, message_id=msg_id
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/s, не словить flood

    await call.message.edit_text(
        f"📢 <b>Рассылка завершена</b> ({_BC_TARGET_LABEL.get(target, target)})\n\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        reply_markup=back_to_panel(),
    )
