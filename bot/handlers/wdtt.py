"""Обход белых списков (wdtt / proxy-turn-vk): выдача/отзыв/просмотр доступов.

v1 — админ-выдача (зеркалит создание peer): владелец включает wdtt на сервере,
создаёт доступ (метка → срок → платформа), бот через SSH зовёт `wdtt-server ctl`
на форкнутом демоне и отдаёт `wdtt://`-ссылку. Юзер видит свои доступы в меню
«🛡 Обход БС». Self-service (для чужих юзеров) — позже, вместе с подписками.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import PeerStatus, ServerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_WDTT,
    back_to_menu,
    cancel_only,
    server_card,
    to_server,
    wdtt_card_kb,
    wdtt_days_kb,
    wdtt_list_kb,
    wdtt_platform_kb,
    wdtt_user_card_kb,
    wdtt_user_list_kb,
)
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt, encrypt
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import WdttStates
from bot.texts import t
from bot.utils.validators import is_valid_label

router = Router(name="wdtt")

_WDTT_PER_PAGE = 8

# platform → (подпись, название приложения для инструкции)
_PLATFORMS = {
    "android": ("Android", "WDTT (Android)"),
    "ios": ("iOS", "vk-turn-proxy (iOS)"),
    "pc": ("ПК", "PWDTT (Windows/Linux/macOS)"),
}


def _mark(status: PeerStatus) -> str:
    return "✅" if status == PeerStatus.ACTIVE else "🚫"


def _fmt_access_text(access, server_name: str) -> str:
    text = (
        f"🛡 <b>{access.label}</b>\n"
        f"• Сервер: <code>{server_name}</code>\n"
        f"• Статус: <b>{access.status}</b>"
    )
    if access.expires_at:
        text += f"\n• ⏱ Истекает: {access.expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
    return text


# ======================= Пользовательская часть =============================

@router.callback_query(F.data.regexp(rf"^{CB_WDTT}:my(:\d+)?$"))
async def cb_wdtt_my(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    accesses = await repo.list_wdtt_for_user(session, user.id)
    if not accesses:
        await call.message.edit_text(t.wdtt_user_empty, reply_markup=back_to_menu())
        await call.answer()
        return

    accesses.sort(key=lambda a: (a.status != PeerStatus.ACTIVE, a.id))
    total = len(accesses)
    start = page * _WDTT_PER_PAGE
    page_items = accesses[start:start + _WDTT_PER_PAGE]

    rows: list[tuple[int, str, str, str]] = []
    for a in page_items:
        srv = await repo.get_server(session, a.server_id)
        rows.append((a.id, _mark(a.status), a.label, srv.name if srv else "?"))
    await call.message.edit_text(
        "🛡 <b>Обход белых списков</b>\nТвои доступы:",
        reply_markup=wdtt_user_list_kb(
            rows, page, has_prev=page > 0, has_next=start + _WDTT_PER_PAGE < total
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_WDTT}:myopen:"))
async def cb_wdtt_my_open(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    srv = await repo.get_server(session, access.server_id)
    await call.message.edit_text(
        _fmt_access_text(access, srv.name if srv else "?"),
        reply_markup=wdtt_user_card_kb(
            access.id, can_get=access.status == PeerStatus.ACTIVE
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_WDTT}:mylink:"))
async def cb_wdtt_my_link(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if access.status != PeerStatus.ACTIVE:
        await call.answer("Доступ отозван", show_alert=True)
        return
    link = decrypt(access.uri_enc)
    await call.message.answer(t.wdtt_link.format(link=link))
    await call.answer("Отправил ссылку")


# ============================ Админская часть ================================

router_admin = Router(name="wdtt_admin")
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())


# --- Тумблер wdtt на сервере -------------------------------------------------

@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:toggle:"))
async def cb_wdtt_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    server.wdtt_enabled = not server.wdtt_enabled
    await session.commit()
    note = ""
    if server.wdtt_enabled and not settings.wdtt_vk_hashes:
        note = " (не задан WDTT_VK_HASHES — выдача работать не будет)"
    await call.message.edit_reply_markup(
        reply_markup=server_card(server_id, server.wdtt_enabled)
    )
    await call.answer(
        ("Обход БС включён" if server.wdtt_enabled else "Обход БС выключен") + note,
        show_alert=bool(note),
    )


# --- Создание доступа (FSM: label → days → platform) -------------------------

@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:new:"))
async def cb_wdtt_new(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if (
        server is None
        or server.owner_tg_id != call.from_user.id
        or server.status != ServerStatus.READY
        or not server.wdtt_enabled
    ):
        await call.answer("Сервер недоступен", show_alert=True)
        return
    if not settings.wdtt_vk_hashes:
        await call.answer(t.wdtt_disabled, show_alert=True)
        return
    await state.set_state(WdttStates.label)
    await state.update_data(server_id=server_id)
    await call.message.edit_text(t.wdtt_ask_label, reply_markup=cancel_only())
    await call.answer()


@router_admin.message(WdttStates.label, F.text)
async def step_wdtt_label(message: Message, state: FSMContext) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer("Метка невалидна (латиница/цифры/пробел/_-, до 32). Ещё раз:")
        return
    await state.update_data(label=label)
    await state.set_state(WdttStates.days)
    await message.answer(t.wdtt_ask_days, reply_markup=wdtt_days_kb())


@router_admin.callback_query(WdttStates.days, F.data.startswith(f"{CB_WDTT}:days:"))
async def step_wdtt_days(call: CallbackQuery, state: FSMContext) -> None:
    days = int(call.data.rsplit(":", 1)[-1])
    await state.update_data(days=days)
    await state.set_state(WdttStates.platform)
    await call.message.edit_text(t.wdtt_ask_platform, reply_markup=wdtt_platform_kb())
    await call.answer()


@router_admin.callback_query(WdttStates.platform, F.data.startswith(f"{CB_WDTT}:plat:"))
async def step_wdtt_platform(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    platform = call.data.rsplit(":", 1)[-1]
    if platform not in _PLATFORMS:
        await call.answer("Неизвестная платформа", show_alert=True)
        return
    data = await state.get_data()
    server_id = data["server_id"]
    label = data["label"]
    days = data["days"]
    await state.clear()

    server = await repo.get_server(session, server_id)
    if server is None or not server.wdtt_enabled:
        await call.message.edit_text("Сервер недоступен.", reply_markup=back_to_menu())
        await call.answer()
        return
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )

    await call.message.edit_text(t.wdtt_creating)
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            res = await wdtt_svc.create_access(
                ssh,
                days=days,
                label=label,
                vk_hashes=settings.wdtt_vk_hashes,
                ports=server.wdtt_ports,
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        logger.warning("wdtt create failed: {}", exc)
        await call.message.edit_text(
            f"❌ Не удалось создать доступ: <code>{exc}</code>",
            reply_markup=to_server(server_id),
        )
        await call.answer()
        return
    except Exception:
        logger.exception("Unexpected wdtt create error")
        await call.message.edit_text(t.error_generic, reply_markup=to_server(server_id))
        await call.answer()
        return

    link = res["link"]
    if platform == "pc":
        link = f"{link}#{label}"
    expires_at = (
        datetime.fromtimestamp(res["expires_at"], tz=timezone.utc)
        if res["expires_at"]
        else None
    )
    # SSH прошёл — сохраняем сразу, чтобы пароль не остался «висеть» без учёта.
    await repo.create_wdtt_access(
        session,
        server_id=server_id,
        user_id=user.id,
        label=label,
        uri_enc=encrypt(link),
        password_enc=encrypt(res["password"]),
        expires_at=expires_at,
    )
    await session.commit()

    _, app_name = _PLATFORMS[platform]
    await call.message.edit_text(
        t.wdtt_created.format(label=label, server=server.name, app=app_name, link=link),
        reply_markup=to_server(server_id),
    )
    await call.answer("Готово")


# --- Список/карточка/отзыв доступов сервера (админ) --------------------------

@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:list:"))
async def cb_wdtt_list(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    server_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    server = await repo.get_server(session, server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Не найдено", show_alert=True)
        return

    accesses = await repo.list_wdtt_for_server(session, server_id)
    accesses.sort(key=lambda a: (a.status != PeerStatus.ACTIVE, a.id))
    active = sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)
    total = len(accesses)
    start = page * _WDTT_PER_PAGE
    page_items = accesses[start:start + _WDTT_PER_PAGE]
    rows = [(a.id, _mark(a.status), a.label) for a in page_items]

    await call.message.edit_text(
        f"🛡 <b>Доступы обхода — {server.name}</b>\n"
        f"Всего: <b>{total}</b> | ✅ Активных: <b>{active}</b>",
        reply_markup=wdtt_list_kb(
            rows, server_id, page,
            has_prev=page > 0, has_next=start + _WDTT_PER_PAGE < total,
        ),
    )
    await call.answer()


@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:open:"))
async def cb_wdtt_open(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, access.server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.edit_text(
        _fmt_access_text(access, server.name),
        reply_markup=wdtt_card_kb(
            access.id, server.id, can_revoke=access.status == PeerStatus.ACTIVE
        ),
    )
    await call.answer()


@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:link:"))
async def cb_wdtt_resend(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, access.server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.answer(t.wdtt_link.format(link=decrypt(access.uri_enc)))
    await call.answer("Отправил ссылку")


@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:revoke:"))
async def cb_wdtt_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    access_id = int(call.data.rsplit(":", 1)[-1])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, access.server_id)
    if server is None or server.owner_tg_id != call.from_user.id:
        await call.answer("Нет доступа", show_alert=True)
        return
    if access.status == PeerStatus.ACTIVE:
        password = decrypt(access.password_enc)
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                await wdtt_svc.remove_access(
                    ssh, password=password, binary=settings.wdtt_binary_path
                )
        except SSHError as exc:
            # Как и с пирами: статус в БД меняем даже при сбое SSH.
            logger.warning("wdtt revoke ssh error: {}", exc)
    await repo.revoke_wdtt_access(session, access.id)
    await session.commit()
    await call.message.edit_text(
        t.wdtt_revoked.format(label=access.label),
        reply_markup=to_server(server.id),
    )
    await call.answer()


router.include_router(router_admin)
