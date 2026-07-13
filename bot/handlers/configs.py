"""Выдача peer-конфигов: своим, по инвайту, отзыв."""
from __future__ import annotations

from datetime import datetime, timezone

import asyncio
import contextlib
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Invite, Peer, PeerStatus, Server, ServerStatus, User
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_INVITES,
    CB_PEERS,
    back_to_menu,
    cancel_only,
    invite_card_kb,    # ← новое
    invites_list_kb,   # ← новое
    peer_card,
    peers_list,
    pick_server,
    to_server,
)
from bot.loader import bot
from bot.services import amnezia
from bot.services.crypto import decrypt, encrypt
from bot.services.qrgen import conf_to_qr_png
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import InviteStates, PeerStates
from bot.texts import t
from bot.utils.validators import is_valid_label

router = Router(name="configs")

_PEERS_PER_PAGE = 8
_INVITES_PER_PAGE = 8

# Блокировки на каждый сервер: сериализуют аллокацию IP, чтобы два параллельных
# создания пира (админский /newpeer и redeem инвайта) не выбрали один и тот же IP.
_server_ip_locks: dict[int, asyncio.Lock] = {}


def _server_ip_lock(server_id: int) -> asyncio.Lock:
    lock = _server_ip_locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _server_ip_locks[server_id] = lock
    return lock


async def _create_peer_for_user(
    session: AsyncSession,
    server: Server,
    user: User,
    label: str,
    *,
    device_id: int | None = None,
    expires_at: "datetime | None" = None,
) -> tuple[str, str, str]:
    """Создаёт peer на сервере и в БД. Возвращает (conf, ip, label).

    Критическая секция под per-server Lock: пока держим лок, читаем занятые IP
    с сервера (`awg show`), выбираем свободный и добавляем peer. Так два
    параллельных создания на один сервер не займут один IP — второй увидит
    первый уже в выводе `awg show`.
    """
    async with _server_ip_lock(server.id):
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            used = await amnezia.list_used_ips(ssh, server.wg_subnet)
            # Резервируем IP ВСЕХ пиров из БД, включая отозванных: их строка
            # остаётся в БД, а UNIQUE(server_id, ip) не даст переиспользовать IP —
            # иначе INSERT нового пира падает с ошибкой. Отозванный пир держит свой
            # IP, пока его не удалят из БД.
            for p in await repo.list_peers_for_server(session, server.id):
                used.add(p.ip)
            ip = amnezia.next_free_ip(server.wg_subnet, used)
            keys = await amnezia.generate_peer_keys(ssh)

            # Сначала пишем в БД (UniqueConstraint поймает дубль IP), и только
            # потом трогаем сервер — иначе при коллизии остался бы «сирота» на VPS.
            peer = Peer(
                server_id=server.id,
                user_id=user.id,
                device_id=device_id,
                label=label,
                ip=ip,
                public_key=keys.public_key,
                private_key_enc=encrypt(keys.private_key),
                status=PeerStatus.ACTIVE,
                expires_at=expires_at,
            )
            session.add(peer)
            await session.flush()

            await amnezia.add_peer_on_server(ssh, public_key=keys.public_key, peer_ip=ip)

    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    conf = amnezia.build_peer_conf(
        peer_private_key=keys.private_key,
        peer_ip=ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
    )
    return conf, ip, label


async def provision_device_peers(
    session: AsyncSession, user: User, device: "object"
) -> list[tuple[Server, str]]:
    """Создаёт по одному WG-пиру на КАЖДОЙ READY-локации, где у устройства ещё нет
    активного пира (Блок 8: устройство = группа конфигов по странам). Best-effort —
    упавшую локацию пропускаем, дозакинем при следующем открытии устройства.
    Возвращает [(server, conf), ...] для отправки пользователю."""
    servers = await repo.list_ready_servers(session)
    existing = {
        p.server_id
        for p in await repo.list_peers_for_device(session, device.id)
        if p.status == PeerStatus.ACTIVE
    }
    made: list[tuple[Server, str]] = []
    for server in servers:
        if server.id in existing:
            continue
        try:
            conf, _ip, _ = await _create_peer_for_user(
                session, server, user, device.label,
                device_id=device.id, expires_at=None,
            )
        except SSHError as exc:
            logger.warning("Device {} provision on server {} failed: {}", device.id, server.id, exc)
            continue
        except Exception:
            logger.exception("Device {} provision on server {} crashed", device.id, server.id)
            continue
        made.append((server, conf))
    return made


async def _send_peer_artifacts(
    chat_id: int,
    server_name: str,
    label: str,
    conf: str,
) -> None:
    """Шлёт .conf файлом и QR картинкой."""
    conf_bytes = conf.encode("utf-8")
    filename = f"{server_name}-{label}.conf".replace(" ", "_")
    await bot.send_document(
        chat_id,
        document=BufferedInputFile(conf_bytes, filename=filename),
        caption=f"📄 <code>{filename}</code>",
    )
    qr = conf_to_qr_png(conf)
    await bot.send_photo(
        chat_id,
        photo=BufferedInputFile(qr, filename=f"{filename}.png"),
        caption="📱 QR — отсканируй в приложении AmneziaVPN.",
    )


# --- Список своих конфигов (любой юзер) -------------------------------------

@router.callback_query(F.data.startswith(f"{CB_PEERS}:list"))
async def cb_peer_list(call: CallbackQuery, session: AsyncSession) -> None:
    # callback: "peer:list" (стр. 0) или "peer:list:<page>" (навигация)
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    peers = await repo.list_peers_for_user(session, user.id)
    if not peers:
        await call.message.edit_text(
            "У тебя пока нет конфигов. Жди инвайт от админа или создай сам.",
            reply_markup=back_to_menu(),
        )
        await call.answer()
        return

    # Активные сверху, затем по id; режем на страницы.
    peers.sort(key=lambda p: (p.status != PeerStatus.ACTIVE, p.id))
    total = len(peers)
    start = page * _PEERS_PER_PAGE
    page_peers = peers[start:start + _PEERS_PER_PAGE]

    rows: list[tuple[int, str, str, str]] = []
    for p in page_peers:
        srv = await repo.get_server(session, p.server_id)
        rows.append((p.id, p.label, srv.name if srv else "?", p.status))
    await call.message.edit_text(
        "📁 <b>Твои конфиги</b>",
        reply_markup=peers_list(
            rows,
            page,
            has_prev=page > 0,
            has_next=start + _PEERS_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PEERS}:open:"))
async def cb_peer_open(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if peer is None or user is None or peer.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    srv = await repo.get_server(session, peer.server_id)
    text = (
        f"📄 <b>{peer.label}</b>\n"
        f"• Сервер: <code>{srv.name if srv else '?'}</code>\n"
        f"• IP: <code>{peer.ip}</code>\n"
        f"• Статус: <b>{peer.status}</b>"
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
    is_revoked = peer.status == PeerStatus.REVOKED
    await call.message.edit_text(
        text,
        reply_markup=peer_card(
            peer.id,
            can_revoke=user.is_admin and not is_revoked,
            can_send=not is_revoked,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PEERS}:send:"))
async def cb_peer_send(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if peer is None or user is None or peer.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE:
        await call.answer("Peer отозван", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Сервер удалён", show_alert=True)
        return

    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    priv = decrypt(peer.private_key_enc)
    conf = amnezia.build_peer_conf(
        peer_private_key=priv,
        peer_ip=peer.ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
    )
    await _send_peer_artifacts(call.message.chat.id, server.name, peer.label, conf)
    await call.answer("Готово")


# --- Создание peer админом --------------------------------------------------

router_admin = Router(name="peer_admin")
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())


@router_admin.message(Command("newpeer"))
@router_admin.callback_query(F.data == f"{CB_PEERS}:new")
async def cb_peer_new(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    servers = await repo.list_all_servers(session)
    ready = [s for s in servers if s.status == ServerStatus.READY]
    if not ready:
        await msg.answer("Нет готовых серверов. Сначала установи VPN.", reply_markup=back_to_menu())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    await state.set_state(PeerStates.pick_server)
    await state.update_data(cancel_to="panel")  # отмена на выборе сервера → админка
    text = t.peer_pick_server
    if isinstance(event, CallbackQuery):
        await msg.edit_text(text, reply_markup=pick_server(ready, f"{CB_PEERS}:pick"))
        await event.answer()
    else:
        await msg.answer(text, reply_markup=pick_server(ready, f"{CB_PEERS}:pick"))


@router_admin.callback_query(F.data.startswith(f"{CB_PEERS}:new:"))
async def cb_peer_new_for_server(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Прямой переход «создать peer» с карточки конкретного сервера."""
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.status != ServerStatus.READY:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    await state.set_state(PeerStates.label)
    await state.update_data(server_id=server_id)
    await call.message.edit_text(t.peer_ask_label, reply_markup=cancel_only())
    await call.answer()


@router_admin.callback_query(PeerStates.pick_server, F.data.startswith(f"{CB_PEERS}:pick:"))
async def cb_peer_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.update_data(server_id=server_id)
    await state.set_state(PeerStates.label)
    await call.message.edit_text(t.peer_ask_label, reply_markup=cancel_only())
    await call.answer()


@router_admin.message(PeerStates.label, F.text)
async def step_peer_label(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer(
            "Метка: буквы/цифры/пробел/<code>_-</code>, до 32 символов. Ещё раз:"
        )
        return
    data = await state.get_data()
    server = await repo.get_server(session, data["server_id"])
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await state.clear()

    status_msg = await message.answer("⏳ Создаю peer на сервере...")
    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        conf, ip, _ = await _create_peer_for_user(
            session, server, user, label, device_id=device.id
        )
        await session.commit()
    except SSHError as exc:
        await session.rollback()
        logger.warning("Peer create failed: {}", exc)
        await status_msg.edit_text(f"❌ Не удалось создать peer: <code>{exc}</code>")
        return
    except Exception:
        await session.rollback()
        logger.exception("Unexpected peer create error")
        await status_msg.edit_text(t.error_generic)
        return

    with contextlib.suppress(Exception):
        await status_msg.delete()

    await _send_peer_artifacts(message.chat.id, server.name, label, conf)
    await message.answer(
        t.peer_created.format(server=server.name, label=label, ip=ip),
        reply_markup=to_server(server.id),
    )


# --- Инвайты (одноразовые ссылки для друзей) --------------------------------

@router_admin.message(Command("invite"))
@router_admin.callback_query(F.data == f"{CB_INVITES}:new")
async def cb_invite_new(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    servers = await repo.list_all_servers(session)
    ready = [s for s in servers if s.status == ServerStatus.READY]
    if not ready:
        await msg.answer("Нет готовых серверов.", reply_markup=back_to_menu())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return
    await state.set_state(InviteStates.pick_server)
    await state.update_data(cancel_to="panel")  # отмена на выборе сервера → админка
    text = t.invite_ask_server
    if isinstance(event, CallbackQuery):
        await msg.edit_text(text, reply_markup=pick_server(ready, f"{CB_INVITES}:pick"))
        await event.answer()
    else:
        await msg.answer(text, reply_markup=pick_server(ready, f"{CB_INVITES}:pick"))


@router_admin.callback_query(F.data.startswith(f"{CB_INVITES}:new:"))
async def cb_invite_new_for_server(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None or server.status != ServerStatus.READY:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    await state.set_state(InviteStates.label)
    await state.update_data(server_id=server_id)
    await call.message.edit_text(t.invite_ask_label, reply_markup=cancel_only())
    await call.answer()


@router_admin.callback_query(InviteStates.pick_server, F.data.startswith(f"{CB_INVITES}:pick:"))
async def cb_invite_pick(call: CallbackQuery, state: FSMContext) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    await state.update_data(server_id=server_id)
    await state.set_state(InviteStates.label)
    await call.message.edit_text(t.invite_ask_label, reply_markup=cancel_only())
    await call.answer()


@router_admin.callback_query(F.data.startswith(f"{CB_INVITES}:list:"))
async def cb_invites_list(call: CallbackQuery, session: AsyncSession) -> None:
    # callback: "inv:list:<server_id>" (стр. 0) или "inv:list:<server_id>:<page>"
    parts = call.data.split(":")
    server_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    invites = await repo.list_invites_for_server(session, server_id)
    now = datetime.now(timezone.utc)
    pending = sum(1 for i in invites if i.used_at is None)

    def _icon(inv) -> str:
        if inv.used_at:
            return "✅"
        if inv.expires_at and inv.expires_at < now:
            return "⌛"
        return "⏳"

    # Активные (непогашенные) сверху, затем по id; режем на страницы.
    invites.sort(key=lambda i: (i.used_at is not None, i.id))
    total = len(invites)
    start = page * _INVITES_PER_PAGE
    page_invites = invites[start:start + _INVITES_PER_PAGE]
    rows = [(i.id, _icon(i), i.label or i.token[:8]) for i in page_invites]

    await call.message.edit_text(
        f"🎟 <b>Инвайты — {server.name}</b>\n"
        f"Всего: <b>{total}</b> | "
        f"⏳ Активных: <b>{pending}</b> | "
        f"✅ Использованных: <b>{total - pending}</b>",
        reply_markup=invites_list_kb(
            rows,
            server_id,
            page,
            has_prev=page > 0,
            has_next=start + _INVITES_PER_PAGE < total,
        ),
    )
    await call.answer()


@router_admin.callback_query(F.data.startswith(f"{CB_INVITES}:open:"))
async def cb_invite_open(call: CallbackQuery, session: AsyncSession) -> None:
    invite_id = int(call.data.rsplit(":", 1)[-1])
    invite = await session.get(Invite, invite_id)
    if invite is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, invite.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    if invite.used_at:
        status = "✅ Использован"
        extra = (
            f"\n• Кем: tg_id <code>{invite.used_by_tg_id}</code>"
            f"\n• Когда: {invite.used_at.strftime('%d.%m.%Y %H:%M')}"
        )
        can_revoke = False
    elif invite.expires_at and invite.expires_at < now:
        status = "⌛ Истёк"
        extra = f"\n• Истёк: {invite.expires_at.strftime('%d.%m.%Y %H:%M')}"
        can_revoke = True
    else:
        status = "⏳ Активен"
        extra = ""
        can_revoke = True

    text = (
        f"🎟 <b>{invite.label or 'Без метки'}</b>\n"
        f"• Статус: {status}{extra}\n"
        f"• Сервер: <code>{server.name}</code>\n"
        f"• Создан: {invite.created_at.strftime('%d.%m.%Y %H:%M')}"
    )
    if not invite.used_at:
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={invite.token}"
        text += f"\n• Ссылка: <code>{link}</code>"

    await call.message.edit_text(
        text,
        reply_markup=invite_card_kb(
            invite.id, server.id, can_revoke, used=bool(invite.used_at)
        ),
    )
    await call.answer()


@router_admin.callback_query(F.data.startswith(f"{CB_INVITES}:del:"))
async def cb_invite_delete(call: CallbackQuery, session: AsyncSession) -> None:
    invite_id = int(call.data.rsplit(":", 1)[-1])
    invite = await session.get(Invite, invite_id)
    if invite is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, invite.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return

    # Использованные инвайты тоже можно убрать — из истории (пир выдан отдельно).
    was_used = invite.used_at is not None
    label = invite.label or invite.token[:8]
    server_id = server.id
    await repo.delete_invite(session, invite.id)
    await session.commit()

    # Обновляем список
    invites = await repo.list_invites_for_server(session, server_id)
    now = datetime.now(timezone.utc)
    pending = sum(1 for i in invites if i.used_at is None)

    def _icon(inv) -> str:
        if inv.used_at:
            return "✅"
        if inv.expires_at and inv.expires_at < now:
            return "⌛"
        return "⏳"

    action = "удалён из истории" if was_used else "отозван"
    invites.sort(key=lambda i: (i.used_at is not None, i.id))
    total = len(invites)
    rows = [(i.id, _icon(i), i.label or i.token[:8]) for i in invites[:_INVITES_PER_PAGE]]
    await call.message.edit_text(
        f"🗑 Инвайт <code>{label}</code> {action}.\n\n"
        f"🎟 <b>Инвайты — {server.name}</b>\n"
        f"Всего: <b>{total}</b> | "
        f"⏳ Активных: <b>{pending}</b> | "
        f"✅ Использованных: <b>{total - pending}</b>",
        reply_markup=invites_list_kb(
            rows, server_id, page=0, has_prev=False, has_next=_INVITES_PER_PAGE < total
        ),
    )
    await call.answer()
    

@router_admin.message(InviteStates.label, F.text)
async def step_invite_label(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer("Метка невалидна. Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()

    token = secrets.token_urlsafe(16)
    invite = Invite(
        token=token,
        server_id=data["server_id"],
        issued_by_tg_id=message.from_user.id,
        label=label,
    )
    session.add(invite)
    await session.commit()

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={token}"
    await message.answer(
        t.invite_created.format(link=link),
        reply_markup=to_server(data["server_id"]),
    )


# --- Redeem invite (вызывается из common.cmd_start_deep) --------------------

async def redeem_invite(
    message: Message,
    session: AsyncSession,
    user: User,
    token: str,
) -> bool:
    invite = await repo.get_invite(session, token)
    if invite is None or invite.used_at is not None:
        return False

    server = await repo.get_server(session, invite.server_id)
    if server is None or server.status != ServerStatus.READY:
        return False

    await message.answer(
        t.start_with_invite.format(name=message.from_user.full_name or "друг")
    )

    label = invite.label or f"tg-{user.tg_id}"
    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        conf, ip, _ = await _create_peer_for_user(
            session, server, user, label, device_id=device.id
        )
        await repo.mark_invite_used(session, invite, user.tg_id)
        await session.commit()
    except SSHError as exc:
        await session.rollback()
        logger.warning("Invite redeem failed: {}", exc)
        await message.answer(f"❌ Не удалось создать конфиг: <code>{exc}</code>")
        # Возвращаем True: токен погасить не успели, но redeem был валидным —
        # не показываем пользователю «инвайт некорректен».
        return True
    except Exception:
        await session.rollback()
        logger.exception("Unexpected invite redeem error")
        await message.answer(t.error_generic)
        return True

    await _send_peer_artifacts(message.chat.id, server.name, label, conf)
    await message.answer(
        t.peer_created.format(server=server.name, label=label, ip=ip),
        reply_markup=back_to_menu(),
    )
    return True


# --- Отзыв peer'а (админ) ----------------------------------------------------

@router_admin.callback_query(F.data.startswith(f"{CB_PEERS}:revoke:"))
async def cb_peer_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    # Отзывать пир может только владелец сервера — как в остальных хендлерах.
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
    except SSHError as exc:
        # SSH мог упасть, а статус peer'а в БД всё равно меняем — иначе бот
        # продолжит выдавать его конфиг.
        logger.warning("Peer revoke ssh error: {}", exc)
    await repo.revoke_peer(session, peer.id)
    await session.commit()
    await call.message.edit_text(
        t.peer_revoked.format(label=peer.label), reply_markup=back_to_menu()
    )
    await call.answer()


router.include_router(router_admin)
