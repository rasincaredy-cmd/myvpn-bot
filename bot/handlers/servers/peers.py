"""Пиры сервера глазами админа: список, карточка, метка, отзыв/возврат, конфиг."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_ADMIN,
    CB_SERVERS,
    admin_peer_card,
    server_peers_admin,
)
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import PeerRenameStates, ServerEditStates
from bot.texts import t
from bot.utils.timefmt import fmt_msk
from bot.utils.validators import is_valid_label

router = Router(name="servers_peers")

_PEERS_PER_PAGE = 8


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:peers:"))
async def cb_server_peers(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Список всех пиров сервера — включая выданные через инвайт чужим юзерам."""
    # callback: "srv:peers:<id>" (стр. 0) или "srv:peers:<id>:<page>" (навигация)
    await state.clear()  # сюда ведёт «Отмена» из редактирования лимита конфигов
    parts = call.data.split(":")
    server_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = await repo.list_peers_for_server(session, server.id)
    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    total = len(peers)
    # Активные сверху, затем по id; режем на страницы.
    peers.sort(key=lambda p: (p.status != PeerStatus.ACTIVE, p.id))
    start = page * _PEERS_PER_PAGE
    page_peers = peers[start:start + _PEERS_PER_PAGE]
    limit = "∞" if server.max_peers is None else str(server.max_peers)

    # Пустой сервер тоже показываем этим экраном, а не карточкой сервера:
    # иначе до кнопки лимита не добраться, пока кому-нибудь не выдан конфиг.
    await call.message.edit_text(
        f"👥 <b>Peers — {server.name}</b>\n"
        f"Активных: <b>{active}</b> / лимит: <b>{limit}</b> / всего: <b>{total}</b>\n"
        "<i>Заполненный сервер юзерам при выдаче и переезде не предлагается.</i>",
        reply_markup=server_peers_admin(
            page_peers,
            server_id,
            page,
            has_prev=page > 0,
            has_next=start + _PEERS_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:plim:"))
async def cb_server_peer_limit(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.peer_limit)
    await state.update_data(server_id=server_id)
    limit = "∞" if server.max_peers is None else str(server.max_peers)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    await call.message.edit_text(
        "✏️ <b>Лимит конфигов на сервере</b>\n\n"
        f"Сейчас: <b>{limit}</b>\n\n"
        "Введи максимум активных конфигов (<code>0</code> — закрыть новую выдачу, "
        "выданные продолжают работать). <code>-</code> — без лимита.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ServerEditStates.peer_limit, F.text, AdminFilter())
async def step_server_peer_limit(
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
    server.max_peers = value
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server.id}")
    await message.answer(
        f"✅ Лимит конфигов: {'∞' if value is None else value}",
        reply_markup=kb.as_markup(),
    )


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
        text += f"\n• ⏱ Истекает: {fmt_msk(peer.expires_at)} МСК"
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


# --- Отзыв / возврат / удаление ----------------------------------------------

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

    await repo.revoke_peer(
        session, peer.id,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        details=f"Пир «{peer.label}» отозван админом из карточки сервера",
    )
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

    # Тот же экран выбора, что у юзера: админ, провалившийся сюда из карточки
    # юзера, не должен получать конфиг по другим правилам, чем сам юзер.
    from bot.handlers.config_delivery import ask_config_format

    await ask_config_format(call.message.chat.id, session, peer)
    await call.answer("Спросил формат")


# Индивидуальные лимиты пира (срок/трафик) убраны: единый гейт — подписка юзера
# (срок + лимит трафика на подписку). См. handlers/admin/subscription.py и scheduler.py.
