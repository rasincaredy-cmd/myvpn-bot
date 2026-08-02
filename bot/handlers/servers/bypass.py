"""Обходы БС на сервере глазами админа («обходы как пиры»): список, лимит, отзыв."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import CB_SERVERS, server_wdtt_card_kb, server_wdtt_list_kb
from bot.services import amnezia
from bot.states.install import ServerEditStates

router = Router(name="servers_bypass")

_PLAT = {"android": "Android", "ios": "iOS", "pc": "ПК"}


async def _render_server_wdtt(call, session, server_id: int) -> None:
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    accesses = [a for a in await repo.list_wdtt_for_server(session, server_id)
                if a.status == PeerStatus.ACTIVE]
    rows = []
    for a in accesses:
        owner = await repo.get_user_by_id(session, a.user_id)
        who = (f"@{owner.username}" if owner and owner.username
               else f"id{owner.tg_id}") if owner else "?"
        plat = _PLAT.get(a.platform or "", "")
        lbl = a.label + (f" · {plat}" if plat else "") + f" · {who}"
        rows.append((a.id, lbl))
    limit = "∞" if server.wdtt_max_accesses is None else str(server.wdtt_max_accesses)
    await call.message.edit_text(
        f"🛡 <b>Обходы — {server.name}</b>\n"
        f"Активных: <b>{len(accesses)}</b> / лимит: <b>{limit}</b>\n"
        "<i>Заполненный сервер юзерам при создании обхода не предлагается.</i>",
        reply_markup=server_wdtt_list_kb(rows, server_id),
    )


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:wdtt:"))
async def cb_server_wdtt(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    await state.clear()  # сюда ведёт «Отмена» из редактирования лимита обходов
    await _render_server_wdtt(call, session, server_id)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:wlim:"))
async def cb_server_wdtt_limit(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.wdtt_limit)
    await state.update_data(server_id=server_id)
    limit = "∞" if server.wdtt_max_accesses is None else str(server.wdtt_max_accesses)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:wdtt:{server_id}")
    await call.message.edit_text(
        "✏️ <b>Лимит обходов на сервере</b>\n\n"
        f"Сейчас: <b>{limit}</b>\n\n"
        "Введи максимум активных доступов (<code>0</code> — закрыть новую выдачу, "
        "существующие продолжают работать). <code>-</code> — без лимита.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ServerEditStates.wdtt_limit, F.text, AdminFilter())
async def step_server_wdtt_limit(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if raw == "-":
        value = None
    elif raw.isdigit() and int(raw) <= 100_000:
        value = int(raw)
    else:
        await message.answer("Число ≥ 0 или <code>-</code> (без лимита). Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    server = await repo.get_server(session, data["server_id"])
    if server is None:
        await message.answer("Сервер не найден.")
        return
    server.wdtt_max_accesses = value
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К обходам", callback_data=f"{CB_SERVERS}:wdtt:{server.id}")
    await message.answer(
        f"✅ Лимит обходов: {'∞' if value is None else value}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:wopen:"))
async def cb_server_wdtt_open(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    owner = await repo.get_user_by_id(session, access.user_id)
    who = (f"@{owner.username}" if owner and owner.username
           else f"id <code>{owner.tg_id}</code>") if owner else "?"
    plat = _PLAT.get(access.platform or "", "—")
    await call.message.edit_text(
        f"🛡 <b>{access.label}</b>\n"
        f"• Платформа: <b>{plat}</b>\n"
        f"• Владелец: {who}\n"
        f"• Статус: <b>{access.status}</b>\n"
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}",
        reply_markup=server_wdtt_card_kb(access.id, access.server_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:wdel:"))
async def cb_server_wdtt_del(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    access_id, server_id = int(parts[2]), int(parts[3])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    await teardown.revoke_bypass(
        session, access,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        details=f"Обход БС «{access.label}» удалён админом из карточки сервера",
    )
    await session.commit()
    await _render_server_wdtt(call, session, server_id)
    await call.answer("Отозвано")
