"""Устройства и обходы конкретного юзера глазами админа."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.keyboards.inline import (
    CB_PANEL,
    admin_user_bypass_card_kb,
    admin_user_device_card_kb,
    admin_user_items_kb,
)
from bot.services import amnezia
from bot.texts import t

router = Router(name="admin_user_items")


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
    # Симметрично устройствам (Блок «Мелочи»): показываем и отозванные (🚫),
    # активные — сверху. Иначе счётчик в статистике не сходится с карточкой.
    accesses = await repo.list_wdtt_for_user(session, user_id)
    accesses.sort(key=lambda a: (a.status != PeerStatus.ACTIVE, a.id))
    rows = [
        (a.id, "🛡" if a.status == PeerStatus.ACTIVE else "🚫",
         f"{a.label} @ {labels.get(a.server_id, '?')}")
        for a in accesses
    ]
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
    # Актор — админ из карточки юзера; текст отличаем от юзерского удаления,
    # чтобы по ленте было видно, кто снёс устройство человеку.
    await teardown.delete_device(
        session, device,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        details=f"Устройство «{label}» удалено админом",
    )
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
    # Чья VK-ссылка (Блок «Мелочи 2»): у своей ссылки юзера обход ломается, если
    # он поменял звонок, — поддержке нужно различать эти случаи сразу.
    vk = {True: "🔗 своя юзера", False: "🏢 сервиса"}.get(
        access.vk_own, "—  (до появления флага)"
    )
    await call.message.edit_text(
        f"🛡 <b>{access.label}</b>\n"
        f"• Платформа: <b>{plat}</b>\n"
        f"• VK-ссылка: <b>{vk}</b>\n"
        f"• Сервер: <code>{labels.get(access.server_id, '?')}</code>\n"
        f"• Статус: <b>{access.status}</b>\n"
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}",
        reply_markup=admin_user_bypass_card_kb(
            access.id, user_id, page, is_active=access.status == PeerStatus.ACTIVE
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ubpl:"))
async def cb_panel_user_bypass_link(call: CallbackQuery, session: AsyncSession) -> None:
    """Ссылка обхода БС юзера — админу (Блок «Мелочи»). Симметрично «📄 Конфиг»
    у устройств: поддержке нужно видеть ровно то, что у юзера на руках."""
    access = await repo.get_wdtt_access(session, int(call.data.split(":")[2]))
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    if access.status != PeerStatus.ACTIVE:
        await call.answer("Доступ отозван — ссылка уже не работает", show_alert=True)
        return
    from bot.handlers.wdtt import _PLATFORMS
    from bot.services.crypto import decrypt
    app = _PLATFORMS.get(access.platform, ("", "", None))[1] if access.platform else ""
    app_line = (
        f"Приложение юзера — <b>{app}</b>." if app
        else "Приложение обхода: WDTT — Android, VK Turn Proxy — iOS, PWDTT — ПК."
    )
    await call.message.answer(
        t.wdtt_link.format(link=decrypt(access.uri_enc), app_line=app_line)
    )
    await call.answer("Отправил ссылку")


@router.callback_query(F.data.startswith(f"{CB_PANEL}:ubpx:"))
async def cb_panel_user_bypass_del(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    access_id, user_id, page = int(parts[2]), int(parts[3]), int(parts[4])
    access = await repo.get_wdtt_access(session, access_id)
    if access is None:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    await teardown.revoke_bypass(
        session, access,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        details=f"Обход БС «{access.label}» удалён админом",
    )
    await session.commit()
    await _render_user_bypasses(call, session, user_id, page)
    await call.answer("Доступ отозван")
