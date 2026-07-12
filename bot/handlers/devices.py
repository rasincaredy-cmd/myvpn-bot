"""Устройства и подписка (Блок 9).

Устройство = единица, которую лимитирует подписка; сейчас (1 сервер) это один
WG-пир. Self-service: юзер сам добавляет устройства до лимита подписки, бот
автоматически выдаёт конфиг. Доступы обхода БС привязываются к устройству
отдельно (см. handlers/wdtt.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.keyboards.inline import (
    CB_DEVICE,
    CB_SUB,
    back_to_menu,
    cancel_only,
    device_card_kb,
    devices_list_kb,
    subscription_kb,
)
from bot.services import amnezia
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import DeviceStates
from bot.texts import t
from bot.utils.validators import is_valid_label

# Переиспользуем машинерию создания/отправки пиров.
from bot.handlers.configs import _create_peer_for_user, _send_peer_artifacts

router = Router(name="devices")

_DEVICES_PER_PAGE = 8


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _sub_active(user) -> bool:
    """Подписка активна: срок не задан (бессрочно) или ещё не истёк."""
    if user.sub_expires_at is None:
        return True
    return _as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


def _sub_line(user) -> str:
    if user.sub_expires_at is None:
        return "бессрочно"
    if not _sub_active(user):
        return f"истекла {user.sub_expires_at.strftime('%d.%m.%Y')}"
    return f"до {user.sub_expires_at.strftime('%d.%m.%Y %H:%M')} UTC"


# --- Мои устройства ----------------------------------------------------------

@router.callback_query(F.data.regexp(rf"^{CB_DEVICE}:list(:\d+)?$"))
async def cb_dev_list(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    devices = await repo.list_devices_for_user(session, user.id, active_only=False)
    devices.sort(key=lambda d: (d.status != PeerStatus.ACTIVE, d.id))
    used = sum(1 for d in devices if d.status == PeerStatus.ACTIVE)
    total = len(devices)
    start = page * _DEVICES_PER_PAGE
    page_items = devices[start:start + _DEVICES_PER_PAGE]
    rows = [
        (d.id, "✅" if d.status == PeerStatus.ACTIVE else "🚫", d.label)
        for d in page_items
    ]
    can_add = _sub_active(user) and used < user.sub_max_devices

    head = "📱 <b>Мои устройства</b>"
    if not _sub_active(user):
        head += "\n<i>Подписка истекла — продление у админа.</i>"
    elif not devices:
        head += "\n\nПока пусто. Добавь первое устройство — получишь конфиг."

    await call.message.edit_text(
        head,
        reply_markup=devices_list_kb(
            rows, used, user.sub_max_devices, can_add, page,
            has_prev=page > 0, has_next=start + _DEVICES_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_DEVICE}:add")
async def cb_dev_add(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    if not _sub_active(user):
        await call.answer("Подписка истекла — обратись к админу.", show_alert=True)
        return
    used = await repo.count_active_devices(session, user.id)
    if used >= user.sub_max_devices:
        await call.answer(
            f"Достигнут лимит устройств ({used}/{user.sub_max_devices}).",
            show_alert=True,
        )
        return
    if not await repo.list_ready_servers(session):
        await call.answer("Нет доступных серверов. Попробуй позже.", show_alert=True)
        return
    await state.set_state(DeviceStates.label)
    await call.message.edit_text(t.device_ask_label, reply_markup=cancel_only())
    await call.answer()


@router.message(DeviceStates.label, F.text)
async def step_device_label(message: Message, state: FSMContext, session: AsyncSession) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer("Метка: латиница/цифры/пробел/_-, до 32. Ещё раз:")
        return
    await state.clear()
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    # Повторная проверка лимита/срока (мог измениться, пока вводил метку).
    if not _sub_active(user) or await repo.count_active_devices(session, user.id) >= user.sub_max_devices:
        await message.answer("Лимит устройств или срок подписки не позволяют.", reply_markup=back_to_menu())
        return
    servers = await repo.list_ready_servers(session)
    if not servers:
        await message.answer("Нет доступных серверов.", reply_markup=back_to_menu())
        return
    server = servers[0]

    status_msg = await message.answer("⏳ Создаю устройство...")
    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        # expires_at=None: срок гейтит подписка на уровне устройства (планировщик),
        # а не индивидуальный expires_at пира.
        conf, ip, _ = await _create_peer_for_user(
            session, server, user, label, device_id=device.id, expires_at=None,
        )
        await session.commit()
    except SSHError as exc:
        await session.rollback()
        logger.warning("Device create failed: {}", exc)
        await status_msg.edit_text(f"❌ Не удалось создать устройство: <code>{exc}</code>")
        return
    except Exception:
        await session.rollback()
        logger.exception("Unexpected device create error")
        await status_msg.edit_text(t.error_generic)
        return

    import contextlib
    with contextlib.suppress(Exception):
        await status_msg.delete()
    await _send_peer_artifacts(message.chat.id, server.name, label, conf)
    await message.answer(
        t.device_created.format(label=label), reply_markup=subscription_kb(True)
    )


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:open:"))
async def cb_dev_open(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = await repo.list_peers_for_device(session, device.id)
    accesses = await repo.list_wdtt_for_device(session, device.id)
    active = device.status == PeerStatus.ACTIVE
    text = (
        f"📱 <b>{device.label}</b>\n"
        f"• Статус: <b>{device.status}</b>\n"
        f"• Конфигов: <b>{sum(1 for p in peers if p.status == PeerStatus.ACTIVE)}</b>\n"
        f"• Доступов обхода: <b>{sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)}</b>"
    )
    await call.message.edit_text(
        text,
        reply_markup=device_card_kb(device.id, can_get=active, can_revoke=active),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:send:"))
async def cb_dev_send(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = [p for p in await repo.list_peers_for_device(session, device.id)
             if p.status == PeerStatus.ACTIVE]
    if not peers:
        await call.answer("Нет активных конфигов", show_alert=True)
        return
    for peer in peers:
        server = await repo.get_server(session, peer.server_id)
        if server is None:
            continue
        params = amnezia.AmneziaParams.from_json(server.awg_params_json)
        conf = amnezia.build_peer_conf(
            peer_private_key=decrypt(peer.private_key_enc),
            peer_ip=peer.ip,
            server_public_key=server.server_public_key,
            endpoint=server.server_endpoint,
            params=params,
        )
        await _send_peer_artifacts(call.message.chat.id, server.name, peer.label, conf)
    await call.answer("Готово")


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:revoke:"))
async def cb_dev_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    # Снимаем WG-пиры устройства с серверов (best-effort), обход БС — по паролю.
    peers = [p for p in await repo.list_peers_for_device(session, device.id)
             if p.status == PeerStatus.ACTIVE]
    for peer in peers:
        server = await repo.get_server(session, peer.server_id)
        if server is None:
            continue
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
        except SSHError as exc:
            logger.warning("Device revoke peer ssh error {}: {}", peer.id, exc)
    from bot.services import wdtt as wdtt_svc
    from bot.config import settings
    for acc in [a for a in await repo.list_wdtt_for_device(session, device.id)
                if a.status == PeerStatus.ACTIVE]:
        server = await repo.get_server(session, acc.server_id)
        if server is None:
            continue
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                await wdtt_svc.remove_access(
                    ssh, password=decrypt(acc.password_enc), binary=settings.wdtt_binary_path
                )
        except SSHError as exc:
            logger.warning("Device revoke wdtt ssh error {}: {}", acc.id, exc)
    await repo.revoke_device(session, device.id)
    await session.commit()
    await call.message.edit_text(
        t.device_revoked.format(label=device.label), reply_markup=back_to_menu()
    )
    await call.answer()


# --- Моя подписка ------------------------------------------------------------

@router.callback_query(F.data == f"{CB_SUB}:my")
async def cb_sub_my(call: CallbackQuery, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    used = await repo.count_active_devices(session, user.id)
    text = (
        "🎫 <b>Моя подписка</b>\n"
        f"• Устройства: <b>{used}/{user.sub_max_devices}</b>\n"
        f"• Срок: <b>{_sub_line(user)}</b>"
    )
    if not _sub_active(user):
        text += "\n\n<i>Подписка истекла — доступы отозваны. Напиши админу для продления.</i>"
    await call.message.edit_text(text, reply_markup=subscription_kb(_sub_active(user)))
    await call.answer()
