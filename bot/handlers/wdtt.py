"""Обход белых списков (wdtt) — self-service под устройство (Блок 9).

Юзер сам создаёт доступ обхода: выбирает сервер и устройство, к которому доступ
привязывается. Срок доступа = сроку подписки. Отдельный раздел меню «🛡 Обход БС».
Админ только включает/выключает доступность обхода на сервере (тумблер на карточке
сервера) — выдачу делают юзеры.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
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
    pick_server,
    server_card,
    wdtt_platform_kb,
    wdtt_pick_device_kb,
    wdtt_user_card_kb,
    wdtt_user_list_kb,
    wdtt_vk_choice_kb,
)
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt, encrypt
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import WdttStates
from bot.texts import t

router = Router(name="wdtt")

_WDTT_PER_PAGE = 8

# platform → (подпись, название приложения)
_PLATFORMS = {
    "android": ("Android", "WDTT (Android)"),
    "ios": ("iOS", "vk-turn-proxy (iOS)"),
    "pc": ("ПК", "PWDTT (Windows/Linux/macOS)"),
}


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _sub_active(user) -> bool:
    return user.sub_expires_at is None or _as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


def _sub_days_left(user) -> int:
    """Дней до конца подписки для ctl -days; 0 = бессрочно."""
    if user.sub_expires_at is None:
        return 0
    delta = _as_utc(user.sub_expires_at) - datetime.now(timezone.utc)
    return max(1, math.ceil(delta.total_seconds() / 86400))


def _mark(status: PeerStatus) -> str:
    return "✅" if status == PeerStatus.ACTIVE else "🚫"


# ======================= Список доступов юзера ==============================

@router.callback_query(F.data.regexp(rf"^{CB_WDTT}:my(:\d+)?$"))
async def cb_wdtt_my(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    # Отозванных wdtt в БД больше не держим (hard-delete при отзыве), но на всякий
    # случай фильтруем — в списке только живые доступы.
    accesses = [
        a for a in await repo.list_wdtt_for_user(session, user.id)
        if a.status == PeerStatus.ACTIVE
    ]
    total = len(accesses)
    start = page * _WDTT_PER_PAGE
    page_items = accesses[start:start + _WDTT_PER_PAGE]
    labels = await repo.server_labels_map(session)
    rows = []
    for a in page_items:
        plat = _PLATFORMS.get(a.platform, ("", ""))[0] if a.platform else ""
        label = f"{a.label} · {plat}" if plat else a.label
        rows.append((a.id, _mark(a.status), label, labels.get(a.server_id, "?")))

    # Лимит доступов юзер видит в шапке — как у устройств.
    can_create = _sub_active(user) and total < user.sub_max_bypass
    text = (
        "🛡 <b>Обход белых списков</b>\n"
        "Работает там, где обычный VPN режется белыми списками.\n"
        f"\nДоступов: <b>{total}/{user.sub_max_bypass}</b>"
    )
    if not _sub_active(user):
        text += "\n<i>Подписка истекла — создание недоступно.</i>"
    elif not accesses:
        text += "\nПока пусто. Создай доступ под своё устройство."

    await call.message.edit_text(
        text,
        reply_markup=wdtt_user_list_kb(
            rows, can_create=can_create, page=page,
            has_prev=page > 0, has_next=start + _WDTT_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_WDTT}:myopen:"))
async def cb_wdtt_my_open(call: CallbackQuery, session: AsyncSession) -> None:
    access = await repo.get_wdtt_access(session, int(call.data.rsplit(":", 1)[-1]))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    labels = await repo.server_labels_map(session)
    plat = _PLATFORMS.get(access.platform, ("—", ""))[0] if access.platform else "—"
    from bot.services import amnezia
    text = (
        f"🛡 <b>{access.label}</b>\n"
        f"• Устройство/платформа: <b>{plat}</b>\n"
        f"• Локация: <code>{labels.get(access.server_id, '?')}</code>\n"
        f"• Статус: <b>{access.status}</b>\n"
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}"
    )
    if access.expires_at:
        text += f"\n• ⏱ Истекает: {access.expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
    await call.message.edit_text(
        text, reply_markup=wdtt_user_card_kb(access.id, can_get=access.status == PeerStatus.ACTIVE)
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_WDTT}:mylink:"))
async def cb_wdtt_my_link(call: CallbackQuery, session: AsyncSession) -> None:
    access = await repo.get_wdtt_access(session, int(call.data.rsplit(":", 1)[-1]))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if access.status != PeerStatus.ACTIVE:
        await call.answer("Доступ отозван", show_alert=True)
        return
    await call.message.answer(t.wdtt_link.format(link=decrypt(access.uri_enc)))
    await call.answer("Отправил ссылку")


@router.callback_query(F.data.startswith(f"{CB_WDTT}:myrevoke:"))
async def cb_wdtt_my_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    access = await repo.get_wdtt_access(session, int(call.data.rsplit(":", 1)[-1]))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    await teardown.revoke_bypass(session, access)
    await session.commit()
    await call.message.edit_text(
        t.wdtt_revoked.format(label=access.label), reply_markup=back_to_menu()
    )
    await call.answer()
    await call.message.edit_text(
        t.wdtt_revoked.format(label=access.label), reply_markup=back_to_menu()
    )
    await call.answer()


# ======================= Создание доступа (FSM) =============================

async def _ask_device(call: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    devices = await repo.list_devices_for_user(session, user.id, active_only=True)
    if not devices:
        await state.clear()
        await call.message.edit_text(
            "Сначала создай устройство в разделе «📱 Мои устройства».",
            reply_markup=back_to_menu(),
        )
        await call.answer()
        return
    await state.set_state(WdttStates.pick_device)
    await call.message.edit_text(
        t.wdtt_pick_device,
        reply_markup=wdtt_pick_device_kb([(d.id, d.label) for d in devices]),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_WDTT}:new")
async def cb_wdtt_new(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    # Отмена в этом потоке → назад к списку обхода (не в меню/карточку сервера).
    await state.update_data(cancel_to="wdtt")
    if not _sub_active(user):
        await call.answer("Подписка истекла.", show_alert=True)
        return
    if not settings.wdtt_vk_hashes:
        await call.answer(t.wdtt_disabled, show_alert=True)
        return
    used = await repo.count_active_wdtt_for_user(session, user.id)
    if used >= user.sub_max_bypass:
        await call.answer(
            f"Достигнут лимит доступов обхода ({used}/{user.sub_max_bypass}).",
            show_alert=True,
        )
        return
    servers = [
        s for s in await repo.list_ready_servers(session) if s.wdtt_enabled
    ]
    if not servers:
        await call.answer("Обход БС пока не доступен ни на одном сервере.", show_alert=True)
        return
    if len(servers) == 1:
        await state.update_data(server_id=servers[0].id)
        await _ask_device(call, state, session, user)
        return
    await state.set_state(WdttStates.pick_server)
    await call.message.edit_text(
        t.wdtt_pick_server, reply_markup=pick_server(servers, f"{CB_WDTT}:srv")
    )
    await call.answer()


@router.callback_query(WdttStates.pick_server, F.data.startswith(f"{CB_WDTT}:srv:"))
async def cb_wdtt_pick_server(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or not server.wdtt_enabled or server.status != ServerStatus.READY:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    await state.update_data(server_id=server_id)
    await _ask_device(call, state, session, user)


@router.callback_query(WdttStates.pick_device, F.data.startswith(f"{CB_WDTT}:dev:"))
async def cb_wdtt_pick_device(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id or device.status != PeerStatus.ACTIVE:
        await call.answer("Устройство недоступно", show_alert=True)
        return
    await state.update_data(device_id=device_id)
    await state.set_state(WdttStates.vk)
    await call.message.edit_text(t.wdtt_ask_vk, reply_markup=wdtt_vk_choice_kb())
    await call.answer()


def _normalize_vk(raw: str) -> str:
    v = raw.strip()
    for p in ("https://", "http://"):
        if v.startswith(p):
            v = v[len(p):]
    return v.strip().strip("/")


@router.callback_query(WdttStates.vk, F.data == f"{CB_WDTT}:vk:svc")
async def cb_wdtt_vk_svc(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(vk_hash=None)  # None → возьмём ссылку сервиса из конфига
    await state.set_state(WdttStates.platform)
    await call.message.edit_text(t.wdtt_ask_platform, reply_markup=wdtt_platform_kb())
    await call.answer()


@router.callback_query(WdttStates.vk, F.data == f"{CB_WDTT}:vk:own")
async def cb_wdtt_vk_own(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WdttStates.vk_link)
    await call.message.edit_text(t.wdtt_ask_vk_link, reply_markup=cancel_only())
    await call.answer()


@router.message(WdttStates.vk_link, F.text)
async def step_wdtt_vk_link(message: Message, state: FSMContext) -> None:
    v = _normalize_vk(message.text)
    if not v or "vk" not in v.lower():
        await message.answer(
            "Похоже, это не ссылка на звонок VK. Пришли ещё раз (можно без https):"
        )
        return
    await state.update_data(vk_hash=v)
    await state.set_state(WdttStates.platform)
    await message.answer(t.wdtt_ask_platform, reply_markup=wdtt_platform_kb())


@router.callback_query(WdttStates.platform, F.data.startswith(f"{CB_WDTT}:plat:"))
async def cb_wdtt_platform(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    platform = call.data.rsplit(":", 1)[-1]
    if platform not in _PLATFORMS:
        await call.answer("Неизвестная платформа", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    server = await repo.get_server(session, data["server_id"])
    device = await repo.get_device(session, data["device_id"])
    if server is None or device is None or not server.wdtt_enabled:
        await call.message.edit_text("Сервер или устройство недоступны.", reply_markup=back_to_menu())
        await call.answer()
        return

    # Своя VK-ссылка юзера (если выбрал) переопределяет ссылку сервиса из конфига.
    vk_hashes = data.get("vk_hash") or settings.wdtt_vk_hashes
    await call.message.edit_text(t.wdtt_creating)
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            res = await wdtt_svc.create_access(
                ssh,
                days=_sub_days_left(user),
                label=device.label,
                vk_hashes=vk_hashes,
                ports=server.wdtt_ports,
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        logger.warning("wdtt create failed: {}", exc)
        await call.message.edit_text(
            f"❌ Не удалось создать доступ: <code>{exc}</code>", reply_markup=back_to_menu()
        )
        await call.answer()
        return
    except Exception:
        logger.exception("Unexpected wdtt create error")
        await call.message.edit_text(t.error_generic, reply_markup=back_to_menu())
        await call.answer()
        return

    link = res["link"]
    if platform == "pc":
        link = f"{link}#{device.label}"
    await repo.create_wdtt_access(
        session,
        server_id=server.id,
        user_id=user.id,
        device_id=device.id,
        label=device.label,
        uri_enc=encrypt(link),
        password_enc=encrypt(res["password"]),
        expires_at=None,  # срок гейтит подписка на уровне устройства
        platform=platform,
    )
    await session.commit()

    labels = await repo.server_labels_map(session)
    _, app_name = _PLATFORMS[platform]
    await call.message.edit_text(
        t.wdtt_created.format(
            label=device.label, server=labels.get(server.id, server.name),
            app=app_name, link=link,
        ),
        reply_markup=back_to_menu(),
    )
    await call.answer("Готово")


# ============================ Админ: тумблер ================================

router_admin = Router(name="wdtt_admin")
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())


@router_admin.callback_query(F.data.startswith(f"{CB_WDTT}:toggle:"))
async def cb_wdtt_toggle(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
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


router.include_router(router_admin)
