"""Список серверов, карточка и её правки: локация, имя, приватность, DNS, удаление."""
from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import ServerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_SERVERS,
    back_to_menu,
    back_to_servers_kb,
    confirm_delete_server,
    location_choice_kb,
    server_card,
    servers_list,
)
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import ServerEditStates
from bot.texts import t
from bot.utils.validators import is_valid_label

router = Router(name="servers_card")


# --- Список серверов ---------------------------------------------------------

@router.callback_query(F.data == f"{CB_SERVERS}:list")
async def cb_servers_list(call: CallbackQuery, session: AsyncSession) -> None:
    servers = await repo.list_all_servers(session)
    if not servers:
        await call.message.edit_text(t.servers_empty, reply_markup=back_to_menu())
        await call.answer()
        return
    await call.message.edit_text(
        "🖥 <b>Мои серверы</b>",
        reply_markup=servers_list(servers),
    )
    await call.answer()


# --- Карточка сервера --------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:open:"))
async def cb_server_open(
    call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None
) -> None:
    # Сюда ведут «Отмена» из редактирования имени/локации/DNS — сбрасываем FSM,
    # иначе следующее текстовое сообщение админа улетело бы в step-хендлер.
    if state is not None:
        await state.clear()
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
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
    text += f"\n🌐 DNS: <code>{server.dns or '1.1.1.1, 1.0.0.1'}</code>"
    if server.is_private:
        text += "\n🔒 <b>Приватный</b> — конфиги отсюда получают только админы и «друзья» (⭐ в карточке юзера)"
    await call.message.edit_text(
        text, reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private)
    )
    await call.answer()


# --- Локация сервера (Блок 8) ------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:loc:"))
async def cb_server_location(
    call: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.location)
    # Существующие локации — кнопками (см. location_choice_kb): опечатка в тексте
    # плодит две разные локации. Ввод текстом остаётся рабочим.
    known = await repo.list_known_locations(session)
    await state.update_data(server_id=server_id, loc_names=known)
    await call.message.edit_text(
        "🌍 <b>Локация сервера</b>\n\n"
        f"Текущая: {server.location or '—'}\n\n"
        "Выбери из списка или введи текстом — страна с флагом "
        "(напр. <code>🇩🇪 Германия</code>). <code>-</code> — очистить.",
        reply_markup=location_choice_kb(
            known, f"{CB_SERVERS}:locpick",
            cancel_cb=f"{CB_SERVERS}:open:{server_id}",
        ),
    )
    await call.answer()


async def _finish_server_location(
    send, state: FSMContext, session: AsyncSession, location: str | None
) -> None:
    """Общий финал текстового и кнопочного выбора локации: пишем и подтверждаем."""
    data = await state.get_data()
    await state.clear()
    server = await repo.get_server(session, data["server_id"])
    if server is None:
        await send("Сервер не найден.")
        return
    server.location = location
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server.id}")
    await send(f"✅ Локация: {server.location or '—'}", reply_markup=kb.as_markup())


@router.message(ServerEditStates.location, F.text, AdminFilter())
async def step_server_location(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    await _finish_server_location(
        message.answer, state, session, None if raw == "-" else raw[:64]
    )


@router.callback_query(ServerEditStates.location, F.data.startswith(f"{CB_SERVERS}:locpick:"))
async def cb_server_location_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    choice = call.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    if choice == "new":
        from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
        kb = IKB()
        kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:open:{data['server_id']}")
        await call.message.edit_text(
            "🌍 Введи локацию текстом — страна с флагом "
            "(напр. <code>🇩🇪 Германия</code>). <code>-</code> — очистить.",
            reply_markup=kb.as_markup(),
        )
        await call.answer()
        return
    if choice == "none":
        location = None
    else:
        names = data.get("loc_names") or []
        idx = int(choice)
        if idx >= len(names):
            await call.answer("Список устарел, введи локацию текстом.", show_alert=True)
            return
        location = names[idx]
    await _finish_server_location(call.message.edit_text, state, session, location)
    await call.answer()


# --- Имя сервера (Блок «Ревизия») ---------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:rename:"))
async def cb_server_rename(
    call: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.name)
    await state.update_data(server_id=server_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:open:{server_id}")
    await call.message.edit_text(
        "✏️ <b>Имя сервера</b>\n\n"
        f"Текущее: <code>{server.name}</code>\n\n"
        "Видно только админам (юзеры видят локацию). Введи новое имя "
        "(буквы/цифры/пробел/<code>_-</code>, до 32 символов):",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ServerEditStates.name, F.text, AdminFilter())
async def step_server_rename(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    name = message.text.strip()
    if not is_valid_label(name):
        await message.answer(
            "Имя: буквы/цифры/пробел/<code>_-</code>, до 32 символов. Ещё раз:"
        )
        return
    data = await state.get_data()
    await state.clear()
    server = await repo.get_server(session, data["server_id"])
    if server is None:
        await message.answer("Сервер не найден.")
        return
    server.name = name
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server.id}")
    await message.answer(f"✅ Имя сервера: <code>{name}</code>", reply_markup=kb.as_markup())


# --- Приватность сервера (Блок «Ревизия») --------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:priv:"))
async def cb_server_private_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server.is_private = not server.is_private
    await session.commit()
    await call.answer(
        "🔒 Сервер теперь приватный: новые конфиги/обходы отсюда получат только "
        "админы и «друзья» (⭐). Уже выданные конфиги продолжают работать."
        if server.is_private
        else "🔓 Сервер снова общий — доступен всем юзерам.",
        show_alert=True,
    )
    # Перерисовываем карточку с новым состоянием тумблера.
    await cb_server_open(call, session)


# --- DNS сервера -------------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:dns:"))
async def cb_server_dns(
    call: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.dns)
    await state.update_data(server_id=server_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:open:{server_id}")
    await call.message.edit_text(
        "🌐 <b>DNS для конфигов</b>\n\n"
        f"Текущий: <code>{server.dns or '1.1.1.1, 1.0.0.1'}</code>\n\n"
        "Введи DNS-сервер(ы) через запятую (напр. <code>1.1.1.1, 1.0.0.1</code> "
        "или <code>8.8.8.8</code>). Отправь <code>-</code> — вернуть дефолт.\n"
        "<i>Действует на новые конфиги.</i>",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ServerEditStates.dns, F.text, AdminFilter())
async def step_server_dns(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    data = await state.get_data()
    await state.clear()
    server = await repo.get_server(session, data["server_id"])
    if server is None:
        await message.answer("Сервер не найден.")
        return
    server.dns = None if raw == "-" else raw[:128]
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server.id}")
    await message.answer(
        f"✅ DNS: <code>{server.dns or '1.1.1.1, 1.0.0.1'}</code>",
        reply_markup=kb.as_markup(),
    )


# --- Удаление сервера --------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:del:"))
async def cb_server_del_ask(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.edit_text(
        t.server_delete_confirm.format(name=server.name),
        reply_markup=confirm_delete_server(server.id),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:del_ok:"))
async def cb_server_del_ok(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.message.edit_text(t.server_deleting)
    await call.answer()

    cleanup_text: str
    if server.status in (ServerStatus.READY, ServerStatus.INSTALLING):
        async def progress(step: str) -> None:
            with contextlib.suppress(TelegramBadRequest):
                await call.message.edit_text(t.server_deleting_step.format(step=step))

        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                warnings = await amnezia.uninstall_amneziawg(
                    ssh, wg_port=server.wg_port, progress=progress
                )
        except SSHError as exc:
            logger.warning("Server {} remote cleanup ssh-failed: {}", server.id, exc)
            cleanup_text = t.server_deleted_ssh_failed.format(error=str(exc)[:400])
        except Exception:
            logger.exception("Server {} remote cleanup crashed", server.id)
            cleanup_text = t.server_deleted_ssh_failed.format(error="внутренняя ошибка")
        else:
            cleanup_text = (
                t.server_deleted_with_warnings.format(detail="\n".join(warnings)[:400])
                if warnings
                else t.server_deleted_clean
            )
    else:
        cleanup_text = t.server_deleted_no_remote

    await session.delete(server)
    await session.flush()

    # Сервера больше нет — возвращаем в список серверов, откуда админ и пришёл
    # (Блок «Мелочи 2»), а не в главное меню.
    await call.message.edit_text(cleanup_text, reply_markup=back_to_servers_kb())
