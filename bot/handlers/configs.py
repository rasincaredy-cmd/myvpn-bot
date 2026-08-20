"""Выдача peer-конфигов: своим, по инвайту, отзыв."""
from __future__ import annotations

from datetime import datetime, timezone

import asyncio
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    AuditAction,
    Invite,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    User,
)
from bot.filters.admin import AdminFilter
from bot.handlers.config_delivery import ask_config_format, build_conf_for_peer
from bot.keyboards.inline import (
    CB_INVITES,
    back_to_menu,
    cancel_only,
    invite_card_kb,
    invites_list_kb,
    pick_server,
    to_server,
)
from bot.loader import bot
from bot.services import amnezia, amnezia_native
from bot.services.crypto import encrypt
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import InviteStates
from bot.texts import t, ui
from bot.utils.timefmt import fmt_msk
from bot.utils.validators import is_valid_label

router = Router(name="configs")

_INVITES_PER_PAGE = 8

# Блокировки на каждый сервер: сериализуют аллокацию IP, чтобы два параллельных
# создания пира (устройство юзера и redeem инвайта) не выбрали один и тот же IP.
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
    log_issue: bool = True,
) -> tuple[Peer, str]:
    """Создаёт peer на сервере и в БД. Возвращает (peer, conf).

    Пир возвращается целиком, а не его поля: вызывающему нужен `peer.id`, чтобы
    предложить юзеру выбрать формат конфига — экран выбора пересобирает конфиг
    по id уже в момент нажатия кнопки.

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

    # Пишем в журнал уже ВНЕ лока: запись в БД не участвует в аллокации IP,
    # держать из-за неё сериализацию сервера незачем.
    #
    # log_issue=False зовёт переезд (services/relocate): он пишет своё событие
    # «конфиг переехал». Иначе на один переезд в истории вышли бы две строки —
    # «выдан конфиг» и «переехал», — и админ, разбирая жалобу, не отличил бы
    # переезд от выдачи нового устройства.
    if log_issue:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=user.tg_id,
            target_user_id=user.id,
            target_type="peer",
            target_id=peer.id,
            details=f"{label} на сервере «{server.name}»",
        )

    # Сервер только что читали из БД в этой же функции — None тут невозможен,
    # проверка на него была бы мёртвым кодом.
    _server, conf = await build_conf_for_peer(session, peer)  # type: ignore[misc]
    return peer, conf


async def provision_device_peers(
    session: AsyncSession, user: User, device: "object"
) -> list[tuple[Server, Peer]]:
    """Создаёт по одному WG-пиру на КАЖДОЙ READY-локации, где у устройства ещё нет
    активного пира (Блок 8: устройство = группа конфигов по странам). Если в локации
    несколько серверов — берём наименее загруженный по активным пирам (Блок
    «Распределение»); упавший сервер не хороним локацию — пробуем следующий.
    Существующие пиры не переезжают: конфиг на руках у клиента привязан к серверу.
    Приватные серверы (Блок «Ревизия») обычным юзерам не выдаются — гейт в
    list_ready_servers(for_user=...). Заполненные по `Server.max_peers` серверы
    пропускаются — потолок действует и здесь, иначе его обходил бы кто угодно
    кнопкой «добавить устройство». Возвращает [(server, peer), ...]."""
    servers = await repo.list_ready_servers(session, for_user=user)
    existing = {
        p.server_id
        for p in await repo.list_peers_for_device(session, device.id)
        if p.status == PeerStatus.ACTIVE
    }
    load = await repo.count_active_peers_by_server(session)
    made: list[tuple[Server, Peer]] = []
    for group in repo.group_by_location(servers).values():
        if any(s.id in existing for s in group):
            continue  # в этой локации у устройства уже есть конфиг
        for server in sorted(group, key=lambda s: load.get(s.id, 0)):
            if not repo.has_free_wg_slot(server, load):
                continue  # сервер упёрся в потолок — юзеру его не предлагаем
            try:
                peer, _conf = await _create_peer_for_user(
                    session, server, user, device.label,
                    device_id=device.id, expires_at=None,
                )
            except SSHError as exc:
                logger.warning("Device {} provision on server {} failed: {}", device.id, server.id, exc)
                continue
            except Exception:
                logger.exception("Device {} provision on server {} crashed", device.id, server.id)
                continue
            load[server.id] = load.get(server.id, 0) + 1
            made.append((server, peer))
            break
    return made


def _split_dns(dns: str | None) -> tuple[str, str]:
    parts = [p.strip() for p in (dns or "1.1.1.1, 1.0.0.1").split(",") if p.strip()]
    return (parts[0] if parts else "1.1.1.1"), (parts[1] if len(parts) > 1 else "")


def config_display_base(server: Server) -> str:
    """Имя конфига для юзера: локация БЕЗ номера сервера («🇳🇱 Нидерланды», а не
    «🇳🇱 Нидерланды 2») — юзеру не важно, какой именно сервер локации ему достался.
    В интерфейсе бота нумерация «Локация N» остаётся (server_labels_map).
    Фолбэк — имя сервера, если локация не задана."""
    return server.location or server.name


async def make_vpn_link(session: AsyncSession, server: Server, label: str, conf: str) -> str:
    """Строит `vpn://`-ссылку с человекочитаемым именем «Локация · метка»."""
    name = f"{config_display_base(server)} · {label}"
    d1, d2 = _split_dns(server.dns)
    return amnezia_native.build_vpn_link(
        conf=conf, name=name, host=server.host, port=server.wg_port, dns1=d1, dns2=d2,
    )


# --- Создание peer админом --------------------------------------------------

router_admin = Router(name="peer_admin")
router_admin.message.filter(AdminFilter())
router_admin.callback_query.filter(AdminFilter())


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
            f"\n• Когда: {fmt_msk(invite.used_at)} МСК"
        )
        can_revoke = False
    elif invite.expires_at and invite.expires_at < now:
        status = "⌛ Истёк"
        extra = f"\n• Истёк: {fmt_msk(invite.expires_at)} МСК"
        can_revoke = True
    else:
        status = "⏳ Активен"
        extra = ""
        can_revoke = True

    text = (
        f"🎟 <b>{invite.label or 'Без метки'}</b>\n"
        f"• Статус: {status}{extra}\n"
        f"• Сервер: <code>{server.name}</code>\n"
        f"• Создан: {fmt_msk(invite.created_at)} МСК"
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
    """Погашение инвайта (Блок «Ревизия» — переведён на подписочную модель).

    Раньше инвайт создавал одиночный пир на ОДНОМ сервере в обход лимитов
    подписки. Теперь это обычное устройство: все READY-локации (кроме приватных
    серверов), лимит sub_max_devices уважается, конфиги приходят с QR и
    vpn://-ссылкой — как при «➕ Добавить устройство». Server_id инвайта остался
    учётным якорем (у какого сервера в админке лежит список инвайтов)."""
    invite = await repo.get_invite(session, token)
    if invite is None or invite.used_at is not None:
        return False

    # Истёкшая подписка: устройство создалось бы и тут же было отозвано тиком
    # планировщика. Не жжём инвайт — пусть сначала продлит.
    exp = user.sub_expires_at
    if exp is not None:
        exp_aware = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        if exp_aware <= datetime.now(timezone.utc):
            await message.answer(
                "🎟 Инвайт принят, но твоя подписка закончилась — сначала продли "
                "её в «🎫 Моя подписка», потом открой ссылку ещё раз.",
                reply_markup=back_to_menu(),
            )
            return True

    # Лимит подписки уважаем и здесь: инвайт — приглашение, а не обход лимитов.
    used = await repo.count_active_devices(session, user.id)
    if used >= user.sub_max_devices:
        await message.answer(
            "🎟 Инвайт принят, но у тебя уже занят весь лимит устройств "
            f"({used}/{user.sub_max_devices}). Освободи слот в «📱 Мои "
            "устройства» и открой ссылку ещё раз.",
            reply_markup=back_to_menu(),
        )
        return True

    if not await repo.list_ready_servers(session, for_user=user):
        return False

    await message.answer(
        t.start_with_invite.format(name=ui.safe(message.from_user.full_name) or "друг")
    )

    label = invite.label or f"tg-{user.tg_id}"
    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        made = await provision_device_peers(session, user, device)
        if not made:
            raise SSHError("не удалось создать конфиг ни на одной локации")
        await repo.mark_invite_used(session, invite, user.tg_id)
        await session.commit()
    except SSHError as exc:
        await session.rollback()
        # Сырой exc юзеру не показываем (техножаргон + может раскрыть host).
        logger.warning("Invite redeem failed: {}", exc)
        await message.answer(
            "⚠️ Не получилось создать конфиг. Попробуй открыть ссылку ещё раз "
            "чуть позже — или напиши в поддержку («🆘 Поддержка» в меню).",
            reply_markup=back_to_menu(),
        )
        # Возвращаем True: токен погасить не успели, но redeem был валидным —
        # не показываем пользователю «инвайт некорректен».
        return True
    except Exception:
        await session.rollback()
        logger.exception("Unexpected invite redeem error")
        await message.answer(t.error_generic, reply_markup=back_to_menu())
        return True

    for _server, peer in made:
        await ask_config_format(message.chat.id, session, peer)
    await message.answer(
        t.invite_config_created.format(label=label),
        reply_markup=back_to_menu(),
    )
    return True


router.include_router(router_admin)
