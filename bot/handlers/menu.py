"""Список серверов админа, карточка, peers сервера с управлением, каскадное удаление."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone          # ← новое

from aiogram.fsm.context import FSMContext
from bot.services.crypto import decrypt
from bot.states.install import PeerRenameStates, ServerEditStates
from bot.utils.validators import is_valid_label
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile
from bot.loader import bot as tg_bot
from bot.services.crypto import decrypt
from bot.services.qrgen import conf_to_qr_png
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus, ServerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_ADMIN,
    CB_SERVERS,
    admin_peer_card,
    back_to_menu,
    confirm_delete_server,
    location_choice_kb,
    server_card,
    server_peers_admin,
    server_wdtt_card_kb,
    server_wdtt_list_kb,
    servers_list,
    stats_nav,       # ← новое
    traffic_nav,     # ← новое
)
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.texts import t

router = Router(name="menu")
router.callback_query.filter(AdminFilter())

_PEERS_PER_PAGE = 8


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

    await call.message.edit_text(cleanup_text, reply_markup=back_to_menu())


# --- Peers сервера (admin-панель) --------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:peers:"))
async def cb_server_peers(call: CallbackQuery, session: AsyncSession) -> None:
    """Список всех пиров сервера — включая выданные через инвайт чужим юзерам."""
    # callback: "srv:peers:<id>" (стр. 0) или "srv:peers:<id>:<page>" (навигация)
    parts = call.data.split(":")
    server_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = await repo.list_peers_for_server(session, server.id)
    if not peers:
        await call.message.edit_text(
            f"На <code>{server.name}</code> peer'ов пока нет.",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        await call.answer()
        return

    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    total = len(peers)
    # Активные сверху, затем по id; режем на страницы.
    peers.sort(key=lambda p: (p.status != PeerStatus.ACTIVE, p.id))
    start = page * _PEERS_PER_PAGE
    page_peers = peers[start:start + _PEERS_PER_PAGE]

    await call.message.edit_text(
        f"👥 <b>Peers — {server.name}</b>\n"
        f"Активных: <b>{active}</b> / всего: <b>{total}</b>",
        reply_markup=server_peers_admin(
            page_peers,
            server_id,
            page,
            has_prev=page > 0,
            has_next=start + _PEERS_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:peer:"))
async def cb_admin_peer_open(
    call: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()   # сбрасываем FSM если шли из лимитов
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    owner = await repo.get_user_by_id(session, peer.user_id)
    owner_info = (
        f"@{owner.username}" if owner and owner.username
        else f"id <code>{owner.tg_id}</code>" if owner
        else "неизвестен"
    )
    status_icon = "✅" if peer.status == PeerStatus.ACTIVE else "🚫"

    text = (
        f"👤 <b>{peer.label}</b> {status_icon}\n"
        f"• IP: <code>{peer.ip}</code>\n"
        f"• Статус: <b>{peer.status}</b>\n"
        f"• Владелец: {owner_info}\n"
        f"• Сервер: <code>{server.name}</code>"
    )
    if peer.expires_at:
        text += f"\n• ⏱ Истекает: {peer.expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
    if peer.traffic_limit_bytes:
        text += (
            f"\n• 📊 Трафик: {amnezia.fmt_bytes(peer.traffic_used_bytes)}"
            f" из {amnezia.fmt_bytes(peer.traffic_limit_bytes)}"
        )
    elif peer.traffic_used_bytes:
        text += f"\n• 📊 Трафик: {amnezia.fmt_bytes(peer.traffic_used_bytes)}"

    await call.message.edit_text(
        text,
        reply_markup=admin_peer_card(
            peer.id, server.id, can_revoke=peer.status == PeerStatus.ACTIVE
        ),
    )
    await call.answer()

# --- Переименование пира -----------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_ADMIN}:rename:"))
async def cb_admin_peer_rename(
    call: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(PeerRenameStates.label)
    await state.update_data(peer_id=peer_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_ADMIN}:peer:{peer_id}")
    await call.message.edit_text(
        f"✏️ <b>Переименование</b>\n\n"
        f"Текущая метка: <code>{peer.label}</code>\n\n"
        "Введи новую метку (буквы/цифры/пробел/<code>_-</code>, до 32 символов):",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(PeerRenameStates.label, F.text, AdminFilter())
async def step_peer_rename(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer(
            "Метка: буквы/цифры/пробел/<code>_-</code>, до 32 символов. Ещё раз:"
        )
        return

    data = await state.get_data()
    peer_id = data["peer_id"]
    await state.clear()

    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await message.answer("Пир не найден.")
        return
    peer.label = label
    await session.commit()

    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К пиру", callback_data=f"{CB_ADMIN}:peer:{peer_id}")
    await message.answer(
        f"✅ Метка изменена на <code>{label}</code>.",
        reply_markup=kb.as_markup(),
    )


# --- Трафик пиров -----------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:traffic:"))
async def cb_server_traffic(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Читаю счётчики...")

    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            traffic_list = await amnezia.get_peer_traffic(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    traffic_map = {ti.public_key: ti for ti in traffic_list}
    peers = await repo.list_peers_for_server(session, server_id)
    now_ts = datetime.now(timezone.utc).timestamp()

    lines: list[str] = [f"📊 <b>Трафик — {server.name}</b>\n"]

    if not peers:
        lines.append("Пиров нет.")
    else:
        for peer in peers:
            icon = "✅" if peer.status == PeerStatus.ACTIVE else "🚫"
            ti = traffic_map.get(peer.public_key)

            if ti is None:
                # peer добавлен в БД, но awg его не видит (маловероятно)
                detail = "  нет данных от awg"
            elif ti.last_handshake_ts == 0:
                detail = "  никогда не подключался"
            else:
                delta = int(now_ts - ti.last_handshake_ts)
                if delta < 60:
                    ago = f"{delta} сек"
                elif delta < 3600:
                    ago = f"{delta // 60} мин"
                elif delta < 86400:
                    ago = f"{delta // 3600} ч"
                else:
                    ago = f"{delta // 86400} д"
                # rx сервера = upload пира; tx сервера = download пира
                detail = (
                    f"  ↓ {amnezia.fmt_bytes(ti.tx_bytes)}"
                    f"  ↑ {amnezia.fmt_bytes(ti.rx_bytes)}"
                    f"  🕐 {ago} назад"
                )

            # Накопленный трафик (persisted планировщиком) + ещё не зачтённая
            # текущая дельта — переживает сброс счётчика awg при ребуте.
            acc = peer.traffic_used_bytes
            if ti is not None:
                extra = (ti.rx_bytes + ti.tx_bytes) - peer.traffic_last_raw_bytes
                if extra > 0:
                    acc += extra
            sigma = f"\n  Σ {amnezia.fmt_bytes(acc)}"
            if peer.traffic_limit_bytes:
                sigma += f" / {amnezia.fmt_bytes(peer.traffic_limit_bytes)}"

            lines.append(
                f"{icon} <b>{peer.label}</b> • <code>{peer.ip}</code>\n{detail}{sigma}"
            )

    # Пиры на сервере, о которых БД ничего не знает (ручное добавление и т.п.)
    known_keys = {p.public_key for p in peers}
    orphans = [ti for ti in traffic_list if ti.public_key not in known_keys]
    if orphans:
        lines.append("\n⚠️ <i>Пиры вне БД:</i>")
        for ti in orphans:
            lines.append(f"  <code>{ti.public_key[:24]}…</code>")

    await call.message.edit_text(
        "\n".join(lines), reply_markup=traffic_nav(server_id, has_orphans=bool(orphans))
    )

# --- Состояние сервера -------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:stats:"))
async def cb_server_stats(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Собираю метрики...")

    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            stats = await amnezia.get_server_stats(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    ram_pct  = round(stats.ram_used_mb  / stats.ram_total_mb  * 100) if stats.ram_total_mb  else 0
    disk_pct = round(stats.disk_used_gb / stats.disk_total_gb * 100) if stats.disk_total_gb else 0

    text = (
        f"🖥 <b>Состояние — {server.name}</b>\n\n"
        f"⏱ <b>Uptime:</b> {stats.uptime}\n"
        f"📈 <b>Load avg:</b> {stats.load_1:.2f} / {stats.load_5:.2f} / {stats.load_15:.2f}"
        f"  ({stats.cpu_count} CPU)\n"
        f"🧠 <b>RAM:</b> {stats.ram_used_mb} / {stats.ram_total_mb} MB  ({ram_pct}%)\n"
        f"💾 <b>Диск (/):</b> {stats.disk_used_gb:.1f} / {stats.disk_total_gb:.1f} GB  ({disk_pct}%)"
    )
    await call.message.edit_text(text, reply_markup=stats_nav(server_id))


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:revoke:"))
async def cb_admin_peer_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    """Отзыв пира из admin-панели. Фикс бага: работает для пиров из инвайтов."""
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
    except SSHError as exc:
        # SSH упал, но статус в БД всё равно меняем
        logger.warning("Admin peer revoke ssh error: {}", exc)

    await repo.revoke_peer(session, peer.id)
    await session.commit()

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="« К пирам сервера", callback_data=f"{CB_SERVERS}:peers:{server.id}")
    await call.message.edit_text(
        t.peer_revoked.format(label=peer.label),
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:revive:"))
async def cb_admin_peer_revive(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    await call.answer("⏳ Возобновляю...")
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            await amnezia.add_peer_on_server(ssh, public_key=peer.public_key, peer_ip=peer.ip)
    except SSHError as exc:
        logger.warning("Peer revive ssh error: {}", exc)
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=admin_peer_card(peer.id, server.id, can_revoke=False),
        )
        return

    await repo.revive_peer(session, peer.id)
    await session.commit()
    await call.message.edit_text(
        f"♻️ Peer <code>{peer.label}</code> возобновлён.\n"
        f"IP: <code>{peer.ip}</code> — прежний конфиг снова работает.",
        reply_markup=admin_peer_card(peer.id, server.id, can_revoke=True),
    )


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:delete:"))
async def cb_admin_peer_delete(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return
    if peer.status == PeerStatus.ACTIVE:
        await call.answer("Сначала отзови peer.", show_alert=True)
        return

    label = peer.label
    server_id = server.id
    await repo.delete_peer(session, peer.id)
    await session.commit()

    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К пирам сервера", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    await call.message.edit_text(
        f"🗑 Peer <code>{label}</code> удалён из БД.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()
    

# --- Получить конфиг (admin) -------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_ADMIN}:conf:"))
async def cb_admin_peer_conf(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE:
        await call.answer("Peer отозван", show_alert=True)
        return

    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    priv = decrypt(peer.private_key_enc)
    conf = amnezia.build_peer_conf(
        peer_private_key=priv,
        peer_ip=peer.ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
        dns=server.dns,
    )
    await call.answer("Отправляю...")
    filename = f"{server.name}-{peer.label}.conf".replace(" ", "_")
    await tg_bot.send_document(
        call.message.chat.id,
        document=BufferedInputFile(conf.encode(), filename=filename),
        caption=f"📄 <code>{filename}</code>",
    )
    qr = conf_to_qr_png(conf)
    await tg_bot.send_photo(
        call.message.chat.id,
        photo=BufferedInputFile(qr, filename=f"{filename}.png"),
        caption="📱 QR для AmneziaVPN",
    )


# --- Очистка лишних WG-пиров -------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:cleanup:"))
async def cb_server_cleanup(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Чищу...")
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            traffic_list = await amnezia.get_peer_traffic(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    peers = await repo.list_peers_for_server(session, server_id)
    known_keys = {p.public_key for p in peers}
    orphans = [ti for ti in traffic_list if ti.public_key not in known_keys]

    if not orphans:
        await call.message.edit_text(
            "✅ Лишних пиров нет.", reply_markup=traffic_nav(server_id)
        )
        return

    removed, failed = 0, 0
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            for ti in orphans:
                try:
                    await amnezia.remove_peer_on_server(ssh, public_key=ti.public_key)
                    removed += 1
                except SSHError:
                    failed += 1
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=traffic_nav(server_id),
        )
        return

    result = f"🧹 Удалено лишних пиров: <b>{removed}</b>"
    if failed:
        result += f"\n⚠️ Не удалось: {failed}"
    await call.message.edit_text(result, reply_markup=traffic_nav(server_id))


# Индивидуальные лимиты пира (срок/трафик) убраны: единый гейт — подписка юзера
# (срок + лимит трафика на подписку). См. handlers/admin_panel.py и scheduler.py.


# --- Обходы БС сервера (admin: «обходы как пиры») ----------------------------

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
    await teardown.revoke_bypass(session, access)
    await session.commit()
    await _render_server_wdtt(call, session, server_id)
    await call.answer("Отозвано")
