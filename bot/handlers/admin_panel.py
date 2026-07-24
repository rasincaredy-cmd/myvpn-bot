"""Админ-панель: глобальная статистика, управление юзерами, рассылка."""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import BalanceTx, Device, Invite, Peer, PeerStatus, Server, ServerStatus, User, WdttAccess
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


# --- Бэкап ------------------------------------------------------------------

@router.callback_query(F.data == f"{CB_PANEL}:backup_now")
async def cb_panel_backup(call: CallbackQuery) -> None:
    """Ручной бэкап: тот же архив, что ночной, — всем админам. Ночной маркер
    не трогаем: регулярный бэкап должен идти своим расписанием."""
    from bot.services import backup as backup_svc

    if not backup_svc.enabled():
        await call.answer(
            "Бэкап выключен: задай BACKUP_PASSWORD в .env и перезапусти бота. "
            "Пароль сохрани и вне сервера!",
            show_alert=True,
        )
        return
    await call.answer("Собираю бэкап…")
    try:
        filename = await backup_svc.send_backup_to_admins()
        logger.info("Manual backup sent: {}", filename)
    except Exception as exc:
        logger.exception("Manual backup failed")
        await tg_bot.send_message(
            call.message.chat.id, f"❌ Бэкап не получился: {exc}"
        )


# --- Статистика -------------------------------------------------------------

@router.callback_query(F.data == f"{CB_PANEL}:stats")
async def cb_panel_stats(call: CallbackQuery, session: AsyncSession) -> None:
    """Статистика под подписочную модель (Блок «Ревизия»): сегменты юзеров как
    в списке (💎🎁💤🔴), устройства/обходы вместо голых пиров, деньги за 30 дней.
    Таблицы маленькие — юзеров грузим целиком и сегментируем той же логикой,
    что и список (repo.user_sub_tier), чтобы цифры не расходились с иконками."""
    users = list((await session.execute(select(User))).scalars())
    users_total = len(users)
    seg = {"paid": 0, "trial": 0, "none": 0}
    blocked = admins = 0
    for u in users:
        if u.is_blocked:
            blocked += 1
            continue
        if u.is_admin:
            admins += 1
            continue
        seg[repo.user_sub_tier(u)] += 1

    async def _cnt(stmt) -> int:
        return (await session.execute(stmt)).scalar() or 0

    servers_total = await _cnt(select(func.count(Server.id)))
    servers_ready = await _cnt(
        select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
    )
    dev_active = await _cnt(
        select(func.count(Device.id)).where(Device.status == PeerStatus.ACTIVE)
    )
    dev_total = await _cnt(select(func.count(Device.id)))
    byp_active = await _cnt(
        select(func.count(WdttAccess.id)).where(WdttAccess.status == PeerStatus.ACTIVE)
    )
    peers_active = await _cnt(
        select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
    )
    invites_pending = await _cnt(
        select(func.count(Invite.id)).where(Invite.used_at.is_(None))
    )
    # Конверсия триал→оплата: сколько юзеров хоть раз ПЛАТИЛИ за подписку
    # (kind='charge' — покупка/автопродление; депозиты и правки админа не в счёт).
    users_paid_ever = await _cnt(
        select(func.count(func.distinct(BalanceTx.user_id)))
        .where(BalanceTx.kind == "charge")
    )
    conv_pct = round(users_paid_ever * 100 / users_total) if users_total else 0
    # Деньги за 30 дней: живые пополнения (Crypto Pay) и списания за подписку.
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    from bot.services.pricing import fmt_rub
    dep_30d = (await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "deposit")
        .where(BalanceTx.created_at >= month_ago)
    )).scalar_one()
    charge_30d = -(await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "charge")
        .where(BalanceTx.created_at >= month_ago)
    )).scalar_one()

    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: <b>{users_total}</b> — "
        f"💎 {seg['paid']} · 🎁 {seg['trial']} · 💤 {seg['none']} · "
        f"🔴 {blocked} · 👑 {admins}\n"
        f"📈 Конверсия: <b>{users_paid_ever}</b> из {users_total} покупали "
        f"подписку ({conv_pct}%)\n"
        f"💰 За 30 дней: пополнений <b>{fmt_rub(dep_30d)}</b>, "
        f"оплат подписки <b>{fmt_rub(charge_30d)}</b>\n\n"
        f"📱 Устройств: <b>{dev_active}</b> активных / {dev_total} всего\n"
        f"🛡 Обходов БС: <b>{byp_active}</b> активных\n"
        f"📄 Конфигов на серверах: <b>{peers_active}</b>\n"
        f"🖥 Серверов: <b>{servers_ready}</b> готовых / {servers_total} всего\n"
        f"🎟 Инвайтов не погашено: <b>{invites_pending}</b>",
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
    if user.is_vip:
        status += " · ⭐ друг"
    from bot.services.pricing import fmt_rub
    return (
        f"👤 <b>{user.full_name or '—'}</b>\n"
        f"• Username: {('@' + user.username) if user.username else '—'}\n"
        f"• Telegram ID: <code>{user.tg_id}</code>\n"
        f"• Статус: {status}\n"
        f"• Устройства: <b>{devices}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{srok}</b>\n"
        f"• Трафик: <b>{trf}</b>\n"
        f"• Баланс: <b>{fmt_rub(user.balance_kopeks)}</b>\n"
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
        reply_markup=user_card_kb(user.id, user.is_blocked, page, is_vip=user.is_vip),
    )
    await call.answer()


# --- «Друг» (доступ к приватным серверам, Блок «Ревизия») ---------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:vip:"))
async def cb_panel_toggle_vip(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    user.is_vip = not user.is_vip
    await session.commit()
    await call.answer(
        "⭐ Теперь «друг»: видит и получает конфиги с приватных серверов."
        if user.is_vip else "Больше не «друг»: приватные серверы недоступны "
        "(уже выданные конфиги продолжают работать).",
        show_alert=True,
    )
    await call.message.edit_text(
        await _user_card_text(session, user),
        reply_markup=user_card_kb(user.id, user.is_blocked, page, is_vip=user.is_vip),
    )


# --- Уничтожение юзера (Блок «Ревизия») ---------------------------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:udel:"))
async def cb_panel_user_delete_ask(call: CallbackQuery, session: AsyncSession) -> None:
    from bot.keyboards.inline import user_wipe_confirm_kb

    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Уже удалён", show_alert=True)
        return
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить админа.", show_alert=True)
        return
    devices = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    from bot.services.pricing import fmt_rub
    await call.message.edit_text(
        f"🗑 <b>Стереть юзера {user.full_name or user.tg_id} из БД?</b>\n\n"
        "Будет удалено безвозвратно:\n"
        f"• устройств: <b>{devices}</b>, обходов: <b>{bypass}</b> "
        "(конфиги отзываются с серверов сразу)\n"
        f"• баланс <b>{fmt_rub(user.balance_kopeks)}</b> и вся история операций\n"
        "• история поддержки, неоплаченные счета\n\n"
        "⚠️ Если он снова напишет боту — создастся заново как новый юзер "
        "и ПОЛУЧИТ НОВЫЙ ТРИАЛ. Для наказания используй «🚫 Заблокировать», "
        "удаление — для мусорных/тестовых аккаунтов и «сотрите мои данные».",
        reply_markup=user_wipe_confirm_kb(user.id, page),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:udelc:"))
async def cb_panel_user_delete_confirm(call: CallbackQuery, session: AsyncSession) -> None:
    from bot.services import user_wipe

    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Уже удалён", show_alert=True)
        return
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить админа.", show_alert=True)
        return
    await call.answer("⏳ Стираю...")
    res = await user_wipe.wipe_user(session, user)
    await session.commit()
    lines = [
        f"🗑 Юзер <code>{res.tg_id}</code> стёрт из БД.",
        f"• Конфигов отозвано и снято с серверов: {res.revoked_items}",
        f"• Удалено записей: платежи {res.purged.get('balance_txs', 0)}, "
        f"счета {res.purged.get('invoices', 0)}, "
        f"поддержка {res.purged.get('support_msgs', 0)}",
    ]
    if res.purged.get("referrals_unlinked"):
        lines.append(f"• Отвязано рефералов: {res.purged['referrals_unlinked']}")
    lines.append(
        "<i>Строки конфигов помечены отозванными и доудалятся ретеншном за 30 "
        "дней (SSH-снятие при этом повторится, если сейчас не прошло).</i>"
    )
    await call.message.edit_text(
        "\n".join(lines), reply_markup=back_to_panel()
    )


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
    # user.is_admin в БД синкается только при /start-подобных хендлерах — свежий
    # админ из .env мог ещё не написать боту, проверяем и по settings.
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя заблокировать другого админа.", show_alert=True)
        return

    await repo.set_user_blocked(session, user.id, block)
    await session.commit()
    await session.refresh(user)

    await call.message.edit_text(
        await _user_card_text(session, user),
        reply_markup=user_card_kb(user.id, block, page, is_vip=user.is_vip),
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


@router.callback_query(F.data.startswith(f"{CB_PANEL}:sub_bal:"))
async def cb_panel_sub_bal(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user = await repo.get_user_by_id(session, int(parts[2]))
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(SubAdminStates.set_balance)
    await state.update_data(user_id=user.id, page=int(parts[3]))
    from bot.services.pricing import fmt_rub
    await call.message.edit_text(
        f"💰 <b>Баланс юзера: {fmt_rub(user.balance_kopeks)}</b>\n\n"
        "Введи изменение в рублях со знаком: <code>+90</code> — начислить "
        "(например, за перевод на карту), <code>-50</code> — списать.",
    )
    await call.answer()


@router.message(SubAdminStates.set_balance, F.text)
async def step_sub_balance(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = message.text.strip().replace("₽", "").strip()
    sign = raw[:1]
    if sign not in "+-" or not raw[1:].isdigit() or int(raw[1:]) == 0 or int(raw[1:]) > 1_000_000:
        await message.answer("Формат: <code>+90</code> или <code>-50</code> (рубли). Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    amount = int(raw[1:]) * 100 * (1 if sign == "+" else -1)
    await repo.add_balance_tx(
        session, data["user_id"], amount, "admin", note="Ручная правка админом"
    )
    await session.commit()
    user = await repo.get_user_by_id(session, data["user_id"])
    from bot.services.pricing import fmt_rub
    logger.info("Admin balance adjust: user {} {}{} kopeks", user.id, sign, abs(amount))
    try:
        await tg_bot.send_message(
            user.tg_id,
            f"💰 Админ изменил твой баланс: <b>{fmt_rub(amount)}</b>. "
            f"Сейчас на счету: <b>{fmt_rub(user.balance_kopeks)}</b>.",
        )
    except Exception:
        pass
    # Начисление юзеру с истёкшей подпиской и включённым автопродлением —
    # продлеваем сразу, не заставляя ждать тика планировщика (до 5 минут).
    extra = ""
    if amount > 0:
        from bot.handlers.balance import notify_autopay
        from bot.services import billing
        ap = await billing.autopay_if_expired(session, user)
        if ap is not None:
            await session.commit()
            await notify_autopay(user, ap)
            extra = (
                f"\n♻️ Подписка сразу продлена автопродлением на месяц "
                f"(−{fmt_rub(ap.price_kopeks)})."
            )
    await message.answer(
        f"✅ Баланс: <b>{fmt_rub(user.balance_kopeks)}</b>{extra}",
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
    else:
        # Срок задан в прошлом = отключение: конфиги гаснут сразу, не ждём тика.
        if await revive_svc.revoke_devices_for_user(session, user.id):
            await session.commit()
            msg += "\n🚫 Срок в прошлом — устройства отозваны сразу."
            try:
                await tg_bot.send_message(
                    user.tg_id,
                    "⏱ Подписка истекла — устройства и доступы обхода отключены.\n"
                    "Конфиги сохраняются 30 дней: продлишь подписку — всё оживёт само.",
                )
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
    # Конфиги гаснут сразу, а не на тике планировщика (симметрично мгновенному
    # ревайву при продлении). Строки остаются REVOKED — продление всё оживит.
    revoked = await revive_svc.revoke_devices_for_user(session, user_id)
    await session.commit()
    user = await repo.get_user_by_id(session, user_id)
    if revoked:
        from bot.services.scheduler import REVOKED_RETENTION_DAYS
        try:
            await tg_bot.send_message(
                user.tg_id,
                "⏱ Подписка истекла — устройства и доступы обхода отключены.\n"
                f"Конфиги сохраняются {REVOKED_RETENTION_DAYS} дней: продлишь "
                "подписку — всё оживёт само, перенастраивать не придётся.\n"
                "Продлить: меню → «🎫 Моя подписка» → «🔁 Продлить» "
                "(пополнить баланс — «💰 Баланс»).",
            )
        except Exception:
            pass
    await _render_sub_card(call, session, user, page)
    await call.answer(
        "Подписка отключена, устройства отозваны" if revoked
        else "Подписка отключена (активных устройств не было)"
    )


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
