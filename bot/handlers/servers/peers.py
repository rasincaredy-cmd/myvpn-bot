"""Пиры сервера глазами админа: список, карточка, метка, отзыв/возврат, конфиг."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_ADMIN,
    CB_SERVERS,
    admin_peer_card,
    server_card,
    server_peers_admin,
)
from bot.loader import bot as tg_bot
from bot.services import amnezia
from bot.services.crypto import decrypt
from bot.services.qrgen import conf_to_qr_png
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import PeerRenameStates
from bot.texts import t
from bot.utils.validators import is_valid_label

router = Router(name="servers_peers")

_PEERS_PER_PAGE = 8


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


# Индивидуальные лимиты пира (срок/трафик) убраны: единый гейт — подписка юзера
# (срок + лимит трафика на подписку). См. handlers/admin/subscription.py и scheduler.py.
