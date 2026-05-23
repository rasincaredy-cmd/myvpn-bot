"""Список серверов админа, карточка, peers, каскадное удаление."""
from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import ServerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_SERVERS,
    back_to_menu,
    confirm_delete_server,
    server_card,
    servers_list,
)
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.texts import t

router = Router(name="menu")
router.callback_query.filter(AdminFilter())


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

    # Best-effort удалённая зачистка: имеет смысл только если бот ходил на сервер.
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


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:peers:"))
async def cb_server_peers(call: CallbackQuery, session: AsyncSession) -> None:
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
    lines = [f"👥 <b>Peers на {server.name}</b>\n"]
    for p in peers:
        mark = "✅" if p.status == "active" else "🚫"
        lines.append(f"{mark} <code>{p.label}</code> — {p.ip} (user_id={p.user_id})")
    await call.message.edit_text(
        "\n".join(lines), reply_markup=server_card(server.id)
    )
    await call.answer()
