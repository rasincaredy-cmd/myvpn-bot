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
    device_created_kb,
    devices_list_kb,
    subscription_kb,
)
from bot.services import amnezia
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError
from bot.states.install import DeviceStates
from bot.texts import t
from bot.utils.timefmt import fmt_msk
from bot.utils.validators import is_valid_label

# Переиспользуем машинерию создания/отправки пиров.
from bot.handlers.configs import (
    provision_device_peers,
    _safe_filename_base,
    _send_peer_artifacts,
    make_vpn_link,
    config_display_base,
)

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
        return f"истёк {fmt_msk(user.sub_expires_at, with_time=False)}"
    return f"до {fmt_msk(user.sub_expires_at)} (МСК)"


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
        head += (
            "\n<i>Подписка закончилась — устройства на паузе, конфиги хранятся "
            "30 дней. Продли её в «🎫 Моя подписка» (кнопка ниже) — всё "
            "заработает само, заново ничего настраивать не нужно.</i>"
        )
    elif user.sub_max_devices == 0 and not devices:
        head += (
            "\n\nВ твоём тарифе сейчас нет устройств — только обход БС. "
            "Понадобится VPN — добавь устройства в «🎫 Моя подписка» → "
            "«🔁 Продлить / купить»."
        )
    elif not devices:
        head += (
            "\n\nПока пусто. Устройство — это твой телефон, планшет или "
            "компьютер, на котором будет работать VPN.\n"
            "Жми «➕ Добавить устройство» — пришлю всё нужное для подключения "
            "и подскажу, как настроить."
        )

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
        await call.answer(
            "Подписка закончилась. Продли её в разделе «🎫 Моя подписка» — "
            "устройства оживут сами.",
            show_alert=True,
        )
        return
    used = await repo.count_active_devices(session, user.id)
    if used >= user.sub_max_devices:
        if user.sub_max_devices == 0:
            # Не «(0/0)» — это читается как баг. Объясняем: таков тариф.
            await call.answer(
                "В твоём тарифе нет устройств. Добавить их можно в «🎫 Моя "
                "подписка» → «🔁 Продлить / купить».",
                show_alert=True,
            )
        else:
            await call.answer(
                f"Достигнут лимит устройств ({used}/{user.sub_max_devices}).",
                show_alert=True,
            )
        return
    if not await repo.list_ready_servers(session, for_user=user):
        await call.answer("Локации сейчас недоступны — попробуй чуть позже.", show_alert=True)
        return
    await state.set_state(DeviceStates.label)
    await state.update_data(cancel_to="dev")  # отмена → список устройств
    await call.message.edit_text(t.device_ask_label, reply_markup=cancel_only())
    await call.answer()


@router.message(DeviceStates.label, F.text)
async def step_device_label(message: Message, state: FSMContext, session: AsyncSession) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer(
            "Такое название не подходит. До 32 символов: буквы, цифры, пробелы, "
            "дефис или подчёркивание — например, «Телефон мамы». Попробуй ещё раз:"
        )
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
        await message.answer(
            "Не получилось добавить устройство: достигнут лимит по подписке "
            "или она закончилась. Загляни в «🎫 Моя подписка».",
            reply_markup=back_to_menu(),
        )
        return
    if not await repo.list_ready_servers(session, for_user=user):
        await message.answer(
            "Локации сейчас недоступны — попробуй чуть позже.",
            reply_markup=back_to_menu(),
        )
        return

    status_msg = await message.answer("⏳ Создаю устройство...")
    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        # Устройство = группа конфигов по всем READY-локациям (Блок 8). expires_at=None:
        # срок гейтит подписка на уровне устройства (планировщик), а не пир.
        made = await provision_device_peers(session, user, device)
        if not made:
            raise SSHError("не удалось создать конфиг ни на одной локации")
        await session.commit()
    except SSHError as exc:
        await session.rollback()
        # Сырой exc юзеру не показываем: пугает, может раскрыть host:port
        # сервера и сломать HTML-разметку символом «<».
        logger.warning("Device create failed: {}", exc)
        await status_msg.edit_text(
            "⚠️ Не получилось создать устройство — что-то сбоит на нашей "
            "стороне. Подожди пару минут и попробуй ещё раз. Не помогло — "
            "загляни в «🆘 Поддержка», разберёмся.",
            reply_markup=back_to_menu(),
        )
        return
    except Exception:
        await session.rollback()
        logger.exception("Unexpected device create error")
        await status_msg.edit_text(t.error_generic, reply_markup=back_to_menu())
        return

    import contextlib
    with contextlib.suppress(Exception):
        await status_msg.delete()
    for server, conf in made:
        await _send_peer_artifacts(
            message.chat.id, config_display_base(server), label, conf,
            vpn_link=await make_vpn_link(session, server, label, conf),
        )
    await message.answer(
        t.device_created.format(label=label), reply_markup=device_created_kb()
    )


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:open:"))
async def cb_dev_open(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    active = device.status == PeerStatus.ACTIVE

    # Дозакинуть недостающие локации (Блок 8): если появилась новая страна —
    # устройство получает там конфиг при открытии, и мы сразу его присылаем.
    if active and _sub_active(user):
        made = await provision_device_peers(session, user, device)
        if made:
            await session.commit()
            for server, conf in made:
                await _send_peer_artifacts(
                    call.message.chat.id, config_display_base(server), device.label, conf,
                    vpn_link=await make_vpn_link(session, server, device.label, conf),
                )

    peers = await repo.list_peers_for_device(session, device.id)
    accesses = await repo.list_wdtt_for_device(session, device.id)
    active_peers = [p for p in peers if p.status == PeerStatus.ACTIVE]
    lines = [
        f"📱 <b>{device.label}</b>",
        f"• Статус: <b>{t.STATUS_RU.get(device.status, device.status)}</b>",
    ]
    if not active:
        lines.append(
            "\n⏸ <i>Отключено до продления подписки. Конфиги сохраняются "
            "30 дней и оживут при продлении сами — удалять устройство не нужно.</i>"
        )
    locations: list[tuple[int, str]] = []
    if active_peers:
        labels = await repo.server_labels_map(session)
        lines.append("• Конфиги по локациям:")
        for p in active_peers:
            loc = labels.get(p.server_id, "?")
            lines.append(f"   • {loc}")
            locations.append((p.id, loc))
    lines.append(
        f"• Доступов обхода: <b>{sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)}</b>"
    )
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=device_card_kb(
            device.id, can_get=active, can_revoke=active, locations=locations
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:send1:"))
async def cb_dev_send_one(call: CallbackQuery, session: AsyncSession) -> None:
    """Отправить конфиг одной локации устройства (кнопка на локацию)."""
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if peer is None or user is None or peer.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE:
        await call.answer("Конфиг отозван", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Сервер недоступен", show_alert=True)
        return
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
    await call.answer("Готово")


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
            dns=server.dns,
        )
        await _send_peer_artifacts(
        call.message.chat.id, config_display_base(server), peer.label, conf,
        vpn_link=await make_vpn_link(session, server, peer.label, conf),
    )
    await call.answer("Готово")


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:ren:"))
async def cb_dev_rename(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Переименование своего устройства (Блок «Ревизия»). Только метка в БД:
    конфиги на руках не трогаем, у них имя из локации (config_display_base)."""
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(DeviceStates.rename)
    await state.update_data(device_id=device_id, cancel_to="dev")
    await call.message.edit_text(
        "✏️ <b>Переименование устройства</b>\n\n"
        f"Сейчас: <code>{device.label}</code>\n\n"
        "Введи новое название (до 32 символов: буквы, цифры, пробелы, дефис "
        "или подчёркивание):",
        reply_markup=cancel_only(),
    )
    await call.answer()


@router.message(DeviceStates.rename, F.text)
async def step_device_rename(message: Message, state: FSMContext, session: AsyncSession) -> None:
    label = message.text.strip()
    if not is_valid_label(label):
        await message.answer(
            "Такое название не подходит. До 32 символов: буквы, цифры, пробелы, "
            "дефис или подчёркивание. Попробуй ещё раз:"
        )
        return
    data = await state.get_data()
    await state.clear()
    device = await repo.get_device(session, data["device_id"])
    user = await repo.get_user_by_tg_id(session, message.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await message.answer("Устройство не найдено.", reply_markup=back_to_menu())
        return
    old = device.label
    device.label = label
    # Метки пиров и wdtt-доступов копируют метку устройства при создании —
    # тянем их за собой, чтобы админ-вью и wdtt-карточки не разъезжались.
    for p in await repo.list_peers_for_device(session, device.id):
        p.label = label
    for a in await repo.list_wdtt_for_device(session, device.id):
        a.label = label
    await session.commit()
    logger.info("User {} renamed device {}: {} -> {}", user.id, device.id, old, label)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К устройству", callback_data=f"{CB_DEVICE}:open:{device.id}")
    await message.answer(
        f"✅ Устройство теперь называется <b>{label}</b>.\n"
        "<i>Конфиги на твоих устройствах перенастраивать не нужно — название "
        "меняется только в боте.</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:revoke:"))
async def cb_dev_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    # Снимаем всё с серверов и удаляем устройство из БД целиком (освобождает IP).
    from bot.services import teardown
    label = device.label
    await teardown.delete_device(session, device)
    await session.commit()
    # Удаление необратимо (ревайв невозможен) — фиксируем в лог, кто и что снёс.
    logger.info("User {} deleted device {} ({})", user.id, device_id, label)
    await call.message.edit_text(
        t.device_revoked.format(label=label), reply_markup=back_to_menu()
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
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    trf_line = amnezia.fmt_traffic_line(
        await repo.sub_traffic_used(session, user),
        user.sub_traffic_limit_bytes,
        expired=not _sub_active(user),
    )
    from bot.config import settings
    from bot.services import cryptopay
    from bot.services.pricing import fmt_rub, monthly_price_kopeks

    can_pay = cryptopay.enabled()
    on_trial = user.is_trial and _sub_active(user) and user.sub_expires_at is not None
    title = "🎫 <b>Моя подписка</b>"
    if on_trial:
        title += " — пробный период"
    text = (
        f"{title}\n"
        f"• Устройства: <b>{used}/{user.sub_max_devices}</b>\n"
        f"• Обход БС: <b>{bypass}/{user.sub_max_bypass}</b>\n"
        f"• Срок: <b>{_sub_line(user)}</b>\n"
        f"• Трафик: <b>{trf_line}</b>\n"
        f"• Баланс: <b>{fmt_rub(user.balance_kopeks)}</b>"
    )
    if on_trial:
        # Лимиты триала не дублируем — они уже видны строками выше (и могли
        # быть изменены админом индивидуально).
        text += (
            f"\n\n🎁 <i>Это бесплатный пробный период на {settings.trial_days} "
            "дней. Когда он закончится, VPN просто встанет на паузу — ничего "
            "настраивать заново не придётся, все конфиги сохранятся. Дальше — "
            f"от {fmt_rub(monthly_price_kopeks(1, 1))}/мес (1 устройство + "
            "1 обход БС)."
            + (" Кстати, продлить можно уже сейчас: оплаченный срок прибавится "
               "к пробному, ни дня не сгорит." if can_pay else "")
            + "</i>"
        )
    if not _sub_active(user):
        text += (
            "\n\n<i>Подписка закончилась — VPN на паузе, но всё сохранено: "
            "заново ничего настраивать не придётся. "
            + ("Жми «🔁 Продлить / купить» — устройства включатся сами.</i>" if can_pay
               else "Напиши в поддержку («🆘 Поддержка» в меню) — продлим.</i>")
        )
    # Бессрочным (спец-юзеры/админ) продление и автопродление не показываем.
    perpetual = user.sub_expires_at is None and not user.is_trial
    if can_pay and not perpetual:
        # Текст нарочно не зависит от user.autopay: тумблер обновляет только
        # кнопки, и «включено/выключено» в тексте после нажатия начало бы врать.
        # Текущее состояние видно прямо на кнопке «♻️ Автопродление: ВКЛ/выкл».
        text += (
            "\n\n♻️ <i>Автопродление (кнопка ниже): если включено — когда срок "
            "закончится, бот сам продлит подписку на месяц с баланса, и VPN не "
            "прервётся. Если денег на балансе не хватит, ничего не спишется — "
            "бот подождёт пополнения и продлит сразу после него. Выключено — "
            "VPN просто встанет на паузу, пока не продлишь вручную.</i>"
        )
    await call.message.edit_text(
        text,
        reply_markup=subscription_kb(
            can_pay=can_pay and not perpetual,
            autopay=user.autopay if (can_pay and not perpetual) else None,
        ),
    )
    await call.answer()
