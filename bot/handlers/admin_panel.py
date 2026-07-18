"""Админ-панель: глобальная статистика, управление юзерами, рассылка."""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Invite, Peer, PeerStatus, Server, ServerStatus, User
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_PANEL,
    admin_panel_menu,
    admin_sub_kb,
    admin_user_bypass_card_kb,
    admin_user_device_card_kb,
    admin_user_items_kb,
    back_to_panel,
    broadcast_confirm_kb,
    broadcast_select_kb,
    broadcast_target_kb,
    user_card_kb,
    users_list_kb,
)
from bot.loader import bot as tg_bot
from bot.services import amnezia
from bot.services import revive as revive_svc
from bot.states.install import BroadcastStates, SubAdminStates
from bot.utils.validators import parse_expiry, parse_traffic_limit

from datetime import datetime, timedelta, timezone

router = Router(name="admin_panel")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

_PER_PAGE = 10


# --- Точка входа ------------------------------------------------------------

@router.message(Command("admin"))
@router.callback_query(F.data == f"{CB_PANEL}:main")
async def cmd_admin(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = "👮 <b>Админ-панель</b>"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=admin_panel_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=admin_panel_menu())


# --- Статистика -------------------------------------------------------------

@router.callback_query(F.data == f"{CB_PANEL}:stats")
async def cb_panel_stats(call: CallbackQuery, session: AsyncSession) -> None:
    users_total = (
        await session.execute(select(func.count(User.id)))
    ).scalar() or 0
    users_blocked = (
        await session.execute(
            select(func.count(User.id)).where(User.is_blocked.is_(True))
        )
    ).scalar() or 0
    servers_total = (
        await session.execute(select(func.count(Server.id)))
    ).scalar() or 0
    servers_ready = (
        await session.execute(
            select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
        )
    ).scalar() or 0
    peers_total = (
        await session.execute(select(func.count(Peer.id)))
    ).scalar() or 0
    peers_active = (
        await session.execute(
            select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0
    invites_total = (
        await session.execute(select(func.count(Invite.id)))
    ).scalar() or 0
    invites_pending = (
        await session.execute(
            select(func.count(Invite.id)).where(Invite.used_at.is_(None))
        )
    ).scalar() or 0

    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b>  (🔴 заблокировано: {users_blocked})\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / <b>{servers_total}</b> всего\n"
        f"📄 Peers: <b>{peers_active}</b> активных / <b>{peers_total}</b> всего\n"
        f"🎟 Инвайтов: <b>{invites_pending}</b> непогашенных / <b>{invites_total}</b> всего",
        reply_markup=back_to_panel(),
    )
    await call.answer()


# --- Пользователи -----------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:users:"))
async def cb_panel_users(call: CallbackQuery, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    total = await repo.count_users(session)
    users = await repo.list_all_users(session, offset=page * _PER_PAGE, limit=_PER_PAGE)

    await call.message.edit_text(
        f"👤 <b>Пользователи</b>  (всего: {total})\n"
        f"💎 платная · 🎁 триал · 💤 без подписки · 🔴 бан\n"
        f"Страница {page + 1} из {max(1, -(-total // _PER_PAGE))}",
        reply_markup=users_list_kb(
            users,
            page,
            has_prev=page > 0,
            has_next=(page + 1) * _PER_PAGE < total,
        ),
    )
    await call.answer()


_TIER_LABEL = {
    "paid": "💎 Платная подписка",
    "trial": "🎁 Триал",
    "none": "💤 Без подписки",
}


async def _user_card_text(session: AsyncSession, user) -> str:
    devices = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    tier = repo.user_sub_tier(user)
    if user.sub_expires_at is None:
        srok = "бессрочно"
    else:
        exp = _sub_as_utc(user.sub_expires_at)
        srok = f"{'до' if exp > datetime.now(timezone.utc) else 'истекла'} {user.sub_expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
    trf = amnezia.fmt_traffic_line(
        await repo.sub_traffic_used(session, user), user.sub_traffic_limit_bytes,
        tier == "none",
    )
    status = (
        "🔴 Заблокирован" if user.is_blocked
        else ("👑 Админ" if user.is_admin else _TIER_LABEL[tier])
    )
    return (
        f"👤 <b>{user.full_name or '—'}</b>\n"
        f"• Username: {('@' + user.username) if user.username else '—'}\n"
        f"• Telegram ID: <code>{user.tg_id}</code>\n"
        f"• Статус: {status}\n"
        f"• Устройства: <b>{devices}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{srok}</b>\n"
        f"• Трафик: <b>{trf}</b>\n"
        f"• С нами с: {user.created_at.strftime('%d.%m.%Y')}"
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:user:"))
async def cb_panel_user_open(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.message.edit_text(
        await _user_card_text(session, user),
        reply_markup=user_card_kb(user.id, user.is_blocked, page),
    )
    await call.answer()


# --- Админ: устройства и обходы конкретного юзера ----------------------------

async def _render_user_devices(call, session, user_id: int, page: int) -> None:
    devices = await repo.list_devices_for_user(session, user_id, active_only=False)
    devices.sort(key=lambda d: (d.status != PeerStatus.ACTIVE, d.id))
    rows = [
        (d.id, "✅" if d.status == PeerStatus.ACTIVE else "🚫", d.label)
        for d in devices
    ]
    txt = "📱 <b>Устройства юзера</b>" + ("" if devices else "\n\nПусто.")
    await call.message.edit_text(
        txt, reply_markup=admin_user_items_kb(rows, "udev", user_id, page)
    )


async def _render_user_bypasses(call, session, user_id: int, page: int) -> None:
    labels = await repo.server_labels_map(session)
    accesses = [a for a in await repo.list_wdtt_for_user(session, user_id)
                if a.status == PeerStatus.ACTIVE]
    rows = [(a.id, "🛡", f"{a.label} @ {labels.get(a.server_id, '?')}") for a in accesses]
    txt = "🛡 <b>Обходы юзера</b>" + ("" if accesses else "\n\nПусто.")
    await call.message.edit_text(
        txt, reply_markup=admin_user_items_kb(rows, "ubp", user_id, page)
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:udev:"))
async def cb_panel_user_devices(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    await _render_user_devices(call, session, int(parts[2]), int(parts[3]))
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:udevo:"))
async def cb_panel_user_device_open(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    device_id, user_id, page = int(parts[2]), int(parts[3]), int(parts[4])
    device = await repo.get_device(session, device_id)
    if device is None:
        await call.answer("Не найдено", show_alert=True)
        return
    labels = await repo.server_labels_map(session)
    peers = [p for p in await repo.list_peers_for_device(session, device.id)
             if p.status == PeerStatus.ACTIVE]
    accesses = await repo.list_wdtt_for_device(session, device.id)
    lines = [f"📱 <b>{device.label}</b>", f"• Статус: <b>{device.status}</b>"]
    configs: list = []
    if peers:
        lines.append("• Конфиги по локациям:")
        for p in peers:
            loc = labels.get(p.server_id, "?")
            lines.append(f"   • {loc} — 📊 {amnezia.fmt_bytes(p.traffic_used_bytes)}")
            configs.append((p.id, loc))
    lines.append(f"• Доступов обхода: <b>{sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)}</b>")
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_user_device_card_kb(device.id, user_id, page, configs=configs),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ucfg:"))
async def cb_panel_user_config_send(call: CallbackQuery, session: AsyncSession) -> None:
    """Админ получает конфиг конкретной локации устройства юзера (.conf+QR+vpn://)."""
    parts = call.data.split(":")
    peer_id, user_id, page, device_id = (int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
    peer = await repo.get_peer(session, peer_id)
    if peer is None or peer.status != PeerStatus.ACTIVE:
        await call.answer("Конфиг недоступен", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    from bot.handlers.configs import _send_peer_artifacts, make_vpn_link, config_display_base
    from bot.services.crypto import decrypt
    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    conf = amnezia.build_peer_conf(
        peer_private_key=decrypt(peer.private_key_enc),
        peer_ip=peer.ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
        dns=server.dns,
    )
    await _send_peer_artifacts(
        call.message.chat.id, config_display_base(server), peer.label, conf,
        vpn_link=await make_vpn_link(session, server, peer.label, conf),
    )
    await call.answer("Конфиг отправлен")


@router.callback_query(F.data.startswith(f"{CB_PANEL}:udevx:"))
async def cb_panel_user_device_del(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    device_id, user_id, page = int(parts[2]), int(parts[3]), int(parts[4])
    device = await repo.get_device(session, device_id)
    if device is None:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    label = device.label
    await teardown.delete_device(session, device)
    await session.commit()
    await _render_user_devices(call, session, user_id, page)
    await call.answer(f"Устройство «{label}» удалено")


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ubp:"))
async def cb_panel_user_bypasses(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    await _render_user_bypasses(call, session, int(parts[2]), int(parts[3]))
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ubpo:"))
async def cb_panel_user_bypass_open(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    access_id, user_id, page = int(parts[2]), int(parts[3]), int(parts[4])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    labels = await repo.server_labels_map(session)
    plat = {"android": "Android", "ios": "iOS", "pc": "ПК"}.get(access.platform or "", "—")
    await call.message.edit_text(
        f"🛡 <b>{access.label}</b>\n"
        f"• Платформа: <b>{plat}</b>\n"
        f"• Сервер: <code>{labels.get(access.server_id, '?')}</code>\n"
        f"• Статус: <b>{access.status}</b>\n"
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}",
        reply_markup=admin_user_bypass_card_kb(access.id, user_id, page),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ubpx:"))
async def cb_panel_user_bypass_del(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    access_id, user_id, page = int(parts[2]), int(parts[3]), int(parts[4])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    await teardown.revoke_bypass(session, access)
    await session.commit()
    await _render_user_bypasses(call, session, user_id, page)
    await call.answer("Доступ отозван")


@router.callback_query(
    F.data.startswith(f"{CB_PANEL}:block:") | F.data.startswith(f"{CB_PANEL}:unblock:")
)
async def cb_panel_toggle_block(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    action, user_id, page = parts[1], int(parts[2]), int(parts[3])
    block = action == "block"

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    if user.is_admin:
        await call.answer("Нельзя заблокировать другого админа.", show_alert=True)
        return

    await repo.set_user_blocked(session, user.id, block)
    await session.commit()
    await session.refresh(user)

    await call.message.edit_text(
        await _user_card_text(session, user),
        reply_markup=user_card_kb(user.id, block, page),
    )
    await call.answer("✅ Готово")


# --- Подписка юзера (Блок 9) ------------------------------------------------

def _sub_as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _render_sub_card(call: CallbackQuery, session: AsyncSession, user, page: int) -> None:
    used = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    sub_expired = (
        user.sub_expires_at is not None
        and _sub_as_utc(user.sub_expires_at) <= datetime.now(timezone.utc)
    )
    if user.sub_expires_at is None:
        srok = "бессрочно"
    else:
        srok = f"{'истекла' if sub_expired else 'до'} {user.sub_expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
    trf = amnezia.fmt_traffic_line(await repo.sub_traffic_used(session, user),
                                   user.sub_traffic_limit_bytes, sub_expired)
    await call.message.edit_text(
        f"🎫 <b>Подписка — {user.full_name or user.tg_id}</b>\n"
        f"• Устройства: <b>{used}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{srok}</b>\n"
        f"• Трафик: <b>{trf}</b>",
        reply_markup=admin_sub_kb(user.id, page),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub:"))
async def cb_panel_sub(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await _render_sub_card(call, session, user, page)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_lim:"))
async def cb_panel_sub_lim(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_limit)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text("📱 Введи новый лимит устройств (0–50):")
    await call.answer()


@router.message(SubAdminStates.set_limit, F.text)
async def step_sub_limit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text.strip().isdigit() or not (0 <= int(message.text.strip()) <= 50):
        await message.answer("Нужно число 0–50. Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    await repo.set_subscription(session, data["user_id"], max_devices=int(message.text.strip()))
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    await message.answer(
        f"✅ Лимит устройств: <b>{user.sub_max_devices}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"]),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_bp:"))
async def cb_panel_sub_bp(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_bypass)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text("🛡 Введи лимит доступов обхода БС (0–50):")
    await call.answer()


@router.message(SubAdminStates.set_bypass, F.text)
async def step_sub_bypass(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text.strip().isdigit() or not (0 <= int(message.text.strip()) <= 50):
        await message.answer("Нужно число 0–50. Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    await repo.set_subscription(session, data["user_id"], max_bypass=int(message.text.strip()))
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    await message.answer(
        f"✅ Лимит обхода БС: <b>{user.sub_max_bypass}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"]),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_ext:"))
async def cb_panel_sub_ext(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.extend_days)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "📅 <b>Срок подписки</b>\n\n"
        "Введи дату <code>ДД.ММ.ГГГГ</code> или период <code>Nд</code> (напр. <code>30д</code>).\n"
        "Можно со временем: <code>30д 18:00</code>, <code>31.12.2025 09:30</code>.\n"
        "Без времени: период — от текущего момента, дата — на 23:59 UTC.\n"
        "Отправь <code>-</code>, чтобы сделать бессрочной."
    )
    await call.answer()


@router.message(SubAdminStates.extend_days, F.text)
async def step_sub_extend(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = parse_expiry(message.text.strip())
    if result == "invalid":
        await message.answer(
            "Не понял формат. Примеры: <code>30д</code>, <code>30д 18:00</code>, "
            "<code>31.12.2025</code>, <code>31.12.2025 09:30</code>, <code>-</code>"
        )
        return
    data = await state.get_data()
    await state.clear()
    # Задание срока = выдача платной подписки: снимаем флаг триала, новый период
    # трафика (base := текущая Σ).
    await repo.set_subscription(
        session, data["user_id"],
        expires_at=result, touch_expires=True, reset_traffic_base=True, mark_paid=True,
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    msg = (
        f"✅ Срок установлен: <b>{result.strftime('%d.%m.%Y %H:%M')} UTC</b>"
        if result else "✅ Подписка сделана бессрочной."
    )

    # Ревайв: если у юзера есть отозванные по истечению устройства — возвращаем
    # их к жизни (те же конфиги/ссылки). Новый срок активен ⇒ можно оживлять.
    sub_line = (
        f"до {result.strftime('%d.%m.%Y %H:%M')} UTC" if result else "бессрочная"
    )
    now = datetime.now(timezone.utc)
    if result is None or result > now:
        rv = await revive_svc.revive_devices_for_user(session, user)
        await session.commit()
        if rv.touched:
            msg += f"\n♻️ Восстановлено: устройств <b>{rv.devices_restored}</b>, обходов БС <b>{rv.bypass_restored}</b>."
            if rv.devices_skipped_limit or rv.bypass_skipped_limit:
                msg += (
                    f"\n⚠️ Не влезло в лимиты: устройств {rv.devices_skipped_limit}, "
                    f"обходов {rv.bypass_skipped_limit} (остались отозванными)."
                )
            if rv.errors:
                msg += "\n❌ Не восстановлено: " + "; ".join(rv.errors)
        notify = f"🎉 Подписка продлена ({sub_line})."
        if rv.devices_restored or rv.bypass_restored:
            notify += (
                "\n♻️ Твои устройства восстановлены — прежние конфиги и ссылки "
                "снова работают, ничего перенастраивать не нужно."
            )
        try:
            await tg_bot.send_message(user.tg_id, notify)
        except Exception:
            pass

    await message.answer(msg, reply_markup=admin_sub_kb(user.id, data["page"]))


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_trf:"))
async def cb_panel_sub_trf(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    await state.set_state(SubAdminStates.set_traffic)
    await state.update_data(user_id=int(parts[2]), page=int(parts[3]))
    await call.message.edit_text(
        "📊 Введи лимит трафика на подписку: <code>50GB</code>, <code>500MB</code>, "
        "<code>1TB</code>.\nОтправь <code>-</code>, чтобы снять лимит (безлимит)."
    )
    await call.answer()


@router.message(SubAdminStates.set_traffic, F.text)
async def step_sub_traffic(message: Message, state: FSMContext, session: AsyncSession) -> None:
    result = parse_traffic_limit(message.text.strip())
    if result == "invalid":
        await message.answer("Формат: <code>50GB</code> / <code>500MB</code> / <code>1TB</code> или <code>-</code>. Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    # result: int (байты) или None (снять лимит). Новый лимит → период с нуля.
    await repo.set_subscription(
        session, data["user_id"],
        traffic_limit_bytes=result, touch_traffic_limit=True, reset_traffic_base=True,
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    trf = "безлимит" if result is None else amnezia.fmt_bytes(result)
    await message.answer(
        f"✅ Лимит трафика подписки: <b>{trf}</b>",
        reply_markup=admin_sub_kb(user.id, data["page"]),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_off:"))
async def cb_panel_sub_off(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    now = datetime.now(timezone.utc)
    await repo.set_subscription(session, user_id, expires_at=now, touch_expires=True)
    await session.commit()
    user = await repo.get_user_by_id(session, user_id)
    await _render_sub_card(call, session, user, page)
    await call.answer("Подписка отключена (устройства отзовёт планировщик)")


# --- Рассылка ---------------------------------------------------------------

_BC_TARGET_LABEL = {
    "all": "всем",
    "active": "с активной подпиской",
    "inactive": "без активной подписки",
    "manual": "выбранным вручную",
}
_BC_SEL_PER_PAGE = 8


async def _ask_broadcast_message(call: CallbackQuery, state: FSMContext, target: str) -> None:
    await state.set_state(BroadcastStates.message)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:main")
    await call.message.edit_text(
        f"📢 <b>Рассылка → {_BC_TARGET_LABEL.get(target, target)}</b>\n\n"
        "Пришли сообщение для рассылки — <b>любого типа</b>: текст, фото, видео, "
        "стикер, GIF (можно с подписью). Отправлю его получателям как есть.",
        reply_markup=kb.as_markup(),
    )


async def _render_bc_select(call: CallbackQuery, state: FSMContext, session: AsyncSession, page: int) -> None:
    data = await state.get_data()
    selected = set(data.get("bc_selected", []))
    total = await repo.count_users(session)
    users = await repo.list_all_users(session, offset=page * _BC_SEL_PER_PAGE, limit=_BC_SEL_PER_PAGE)
    rows = []
    for u in users:
        name = (f"@{u.username}" if u.username else None) or u.full_name or f"id{u.tg_id}"
        rows.append((u.id, u.id in selected, name))
    await call.message.edit_text(
        f"✍️ <b>Выбор получателей</b>\nОтмечено: <b>{len(selected)}</b>\n"
        f"Страница {page + 1} из {max(1, -(-total // _BC_SEL_PER_PAGE))}",
        reply_markup=broadcast_select_kb(
            rows, len(selected), page,
            has_prev=page > 0, has_next=(page + 1) * _BC_SEL_PER_PAGE < total,
        ),
    )


@router.callback_query(F.data == f"{CB_PANEL}:broadcast")
async def cb_panel_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.target)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nКому отправляем?",
        reply_markup=broadcast_target_kb(),
    )
    await call.answer()


@router.callback_query(BroadcastStates.target, F.data.startswith(f"{CB_PANEL}:bc_to:"))
async def cb_broadcast_target(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    target = call.data.rsplit(":", 1)[-1]
    await state.update_data(bc_target=target)
    if target == "manual":
        await state.update_data(bc_selected=[])
        await state.set_state(BroadcastStates.select)
        await _render_bc_select(call, state, session, 0)
        await call.answer()
        return
    await _ask_broadcast_message(call, state, target)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data.startswith(f"{CB_PANEL}:bc_sel:"))
async def cb_broadcast_select_toggle(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = call.data.split(":")
    uid, page = int(parts[2]), int(parts[3])
    data = await state.get_data()
    selected = set(data.get("bc_selected", []))
    selected.symmetric_difference_update({uid})  # toggle
    await state.update_data(bc_selected=list(selected))
    await _render_bc_select(call, state, session, page)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data.startswith(f"{CB_PANEL}:bc_selpg:"))
async def cb_broadcast_select_page(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    await _render_bc_select(call, state, session, page)
    await call.answer()


@router.callback_query(BroadcastStates.select, F.data == f"{CB_PANEL}:bc_seldone")
async def cb_broadcast_select_done(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("bc_selected"):
        await call.answer("Никто не выбран", show_alert=True)
        return
    await _ask_broadcast_message(call, state, "manual")
    await call.answer()


@router.message(BroadcastStates.message)
async def step_broadcast_message(message: Message, state: FSMContext) -> None:
    # Запоминаем ссылку на сообщение — разошлём копией (copy_message тянет любой тип).
    await state.update_data(bc_from_chat=message.chat.id, bc_msg_id=message.message_id)
    await state.set_state(BroadcastStates.confirm)
    data = await state.get_data()
    target = data.get("bc_target", "all")
    await message.answer(
        f"📢 <b>Предпросмотр ↑</b>\n\n"
        f"Разослать <b>{_BC_TARGET_LABEL.get(target, target)}</b>?",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(BroadcastStates.confirm, F.data == f"{CB_PANEL}:bc_send")
async def cb_broadcast_send(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    await state.clear()
    target = data.get("bc_target", "all")
    from_chat = data.get("bc_from_chat")
    msg_id = data.get("bc_msg_id")
    if from_chat is None or msg_id is None:
        await call.answer("Нет сообщения для рассылки", show_alert=True)
        return

    await call.message.edit_text("⏳ Рассылаю...")
    await call.answer()

    if target == "manual":
        users = await repo.list_users_by_ids(session, data.get("bc_selected", []))
    else:
        users = await repo.list_broadcast_targets(session, target)
    sent = failed = 0
    for user in users:
        try:
            await tg_bot.copy_message(
                chat_id=user.tg_id, from_chat_id=from_chat, message_id=msg_id
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/s, не словить flood

    await call.message.edit_text(
        f"📢 <b>Рассылка завершена</b> ({_BC_TARGET_LABEL.get(target, target)})\n\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        reply_markup=back_to_panel(),
    )
