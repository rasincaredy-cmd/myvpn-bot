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
from bot.texts import ui
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
from bot.services import amnezia, hardening
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import ServerEditStates
from bot.texts import t, ui
from bot.utils.validators import clean_location, is_valid_label

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
        f"\n<i>Last error:</i> <code>{ui.safe(server.last_error[:200])}</code>"
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
    text += f"\n🌍 Локация: {ui.safe(server.location) or '—'}"
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
        f"Текущая: {ui.safe(server.location) or '—'}\n\n"
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
    # Экранируем: локация — свободный текст, а сохранение уже прошло выше.
    # Сообщение с угловой скобкой Telegram не примет, и админ решит, что смена
    # не сработала, хотя она сработала (аудит 20.08.2026).
    await send(
        f"✅ Локация: {ui.safe(server.location) or '—'}", reply_markup=kb.as_markup()
    )


@router.message(ServerEditStates.location, F.text, AdminFilter())
async def step_server_location(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if raw == "-":
        await _finish_server_location(message.answer, state, session, None)
        return
    location = clean_location(raw)
    if location is None:
        await message.answer(
            "Локация: до 64 символов, без символов <code>&lt; &gt; &amp;</code> "
            "(они ломают экраны бота). Например: <code>🇩🇪 Германия</code>. Ещё раз:"
        )
        return
    await _finish_server_location(message.answer, state, session, location)


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
            cleanup_text = t.server_deleted_ssh_failed.format(error=ui.safe(str(exc)[:400]))
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


# --- Защита сервера (эталон безопасности) -------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:harden:"))
async def cb_server_harden(call: CallbackQuery, session: AsyncSession) -> None:
    """Показать, соответствует ли сервер эталону. Ничего не меняет."""
    server_id = int(call.data.split(":")[2])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    await call.answer("Проверяю...")
    creds = repo.creds_from_server(server)
    try:
        async with SSHClient(creds) as ssh:
            report = await hardening.check(ssh)
    # Important-4: ловим Exception целиком, как это уже делает установка
    # (bot/handlers/install.py). Ошибки asyncssh наследуются напрямую от
    # Exception, а не от OSError: SFTPError (кончилось место в /root,
    # файловая система только для чтения — а проверка первым делом кладёт
    # сценарий через SFTP) и ConnectionLost пролетали мимо перехвата, и
    # админ навсегда оставался с экраном «Проверяю...».
    except Exception as exc:  # noqa: BLE001
        logger.warning("Проверка защиты сервера id={} сорвалась: {}", server_id, exc)
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(
                f"🛡 <b>Защита сервера</b>\n\nНе удалось подключиться: {ui.safe(exc)}",
                reply_markup=back_to_servers_kb(),
            )
        return

    if report.compliant:
        text = "🛡 <b>Защита сервера</b>\n\nСервер соответствует эталону."
    else:
        problems = "\n".join(f"• {p}" for p in report.failed)
        text = (
            "🛡 <b>Защита сервера</b>\n\n"
            f"Найдено несоответствий: {len(report.failed)}\n\n{problems}"
        )

    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB

    kb = IKB()
    if not report.compliant:
        kb.button(
            text="🔧 Привести в порядок",
            callback_data=f"{CB_SERVERS}:hardenrun:{server_id}",
        )
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    # «message is not modified» (повторная проверка с тем же результатом) —
    # не повод ронять обработчик, админ и так видит актуальный текст.
    with contextlib.suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:hardenrun:"))
async def cb_server_harden_run(call: CallbackQuery, session: AsyncSession) -> None:
    """Привести сервер к эталону. Меняет состояние сервера."""
    server_id = int(call.data.split(":")[2])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    await call.answer("Работаю, это займёт пару минут")
    try:
        msg = await call.message.edit_text("🛡 Привожу сервер в порядок...")
    except TelegramBadRequest:
        msg = call.message

    async def progress(text: str) -> None:
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(f"🛡 {text}")

    creds = repo.creds_from_server(server)
    try:
        async with SSHClient(creds) as ssh:
            report = await hardening.harden(
                ssh, session, server_id, wg_port=server.wg_port, progress=progress
            )
    # Important-4: см. комментарий в проверке выше. Здесь это важнее вдвое:
    # приведение к эталону идёт минутами, и обрыв связи посреди него
    # (asyncssh.ConnectionLost) — обычное дело, а не редкость.
    except Exception as exc:  # noqa: BLE001
        logger.warning("Приведение сервера id={} к эталону сорвалось: {}", server_id, exc)
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(
                f"🛡 <b>Защита сервера</b>\n\nСорвалось: {ui.safe(exc)}",
                reply_markup=back_to_servers_kb(),
            )
        return

    if report.compliant:
        text = "🛡 <b>Готово</b>\n\nСервер соответствует эталону."
    else:
        problems = "\n".join(f"• {p}" for p in report.failed)
        text = (
            "🛡 <b>Частично</b>\n\nОсталось несоответствий: "
            f"{len(report.failed)}\n\n{problems}"
        )
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB

    kb = IKB()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(text, reply_markup=kb.as_markup())
