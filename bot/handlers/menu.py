"""Список серверов админа, карточка, peers сервера с управлением, каскадное удаление."""
from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
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
    server_card,
    server_peers_admin,
    servers_list,
)
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.texts import t

router = Router(name="menu")
router.callback_query.filter(AdminFilter())


# --- Список серверов ---------------------------------------------------------

@router.callback_query(F.data == f"{CB_SERVERS}:list")
async def cb_servers_list(call: CallbackQuery, session: AsyncSession) -> None:
    servers = await repo.list_servers_for_owner(session, call.from_user.id)
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
async def cb_server_open(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
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
    await call.message.edit_text(text, reply_markup=server_card(server.id))
    await call.answer()


# --- Удаление сервера --------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:del:"))
async def cb_server_del_ask(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
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
    if server is None or server.owner_tg_id != call.from_user.id:
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
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = await repo.list_peers_for_server(session, server.id)
    if not peers:
        await call.message.edit_text(
            f"На <code>{server.name}</code> peer'ов пока нет.",
            reply_markup=server_card(server.id),
        )
        await call.answer()
        return
    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    await call.message.edit_text(
        f"👥 <b>Peers — {server.name}</b>\n"
        f"Активных: <b>{active}</b> / всего: <b>{len(peers)}</b>",
        reply_markup=server_peers_admin(peers, server_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:peer:"))
async def cb_admin_peer_open(call: CallbackQuery, session: AsyncSession) -> None:
    """Карточка пира в admin-просмотре. Работает для пиров любого юзера."""
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Нет доступа", show_alert=True)
        return

    # Получаем инфо о владельце пира (может быть чужой юзер из инвайта)
    owner = await repo.get_user_by_id(session, peer.user_id)
    if owner and owner.username:
        owner_info = f"@{owner.username}"
    elif owner:
        owner_info = f"id <code>{owner.tg_id}</code>"
    else:
        owner_info = "неизвестен"

    status_icon = "✅" if peer.status == PeerStatus.ACTIVE else "🚫"
    text = (
        f"👤 <b>{peer.label}</b> {status_icon}\n"
        f"• IP: <code>{peer.ip}</code>\n"
        f"• Статус: <b>{peer.status}</b>\n"
        f"• Владелец: {owner_info}\n"
        f"• Сервер: <code>{server.name}</code>"
    )
    await call.message.edit_text(
        text,
        reply_markup=admin_peer_card(
            peer.id, server.id, can_revoke=peer.status == PeerStatus.ACTIVE
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_ADMIN}:revoke:"))
async def cb_admin_peer_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    """Отзыв пира из admin-панели. Фикс бага: работает для пиров из инвайтов."""
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
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
