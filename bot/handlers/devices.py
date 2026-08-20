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
    back_to_devices_kb,
    back_to_menu,
    cancel_only,
    device_card_kb,
    device_created_kb,
    devices_list_kb,
    limit_reached_kb,
    subscription_kb,
)
from bot.services import amnezia, relocate
from bot.services.ssh import SSHError
from bot.states.install import DeviceStates
from bot.texts import t, ui
from bot.utils.timefmt import as_utc, fmt_msk
from bot.utils.validators import is_valid_label

# Переиспользуем машинерию создания пиров и единый экран выбора формата.
from bot.handlers.config_delivery import (
    ask_config_format,
    ask_config_format_for_device,
)
from bot.handlers.configs import provision_device_peers

router = Router(name="devices")

_DEVICES_PER_PAGE = 8


def _sub_active(user) -> bool:
    """Подписка активна: срок не задан (бессрочно) или ещё не истёк."""
    if user.sub_expires_at is None:
        return True
    return as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


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
            "\n\nВ твоём тарифе сейчас нет устройств — только резервное "
            "подключение. Понадобится VPN — добавь устройства в «🎫 Моя "
            "подписка» → «🔁 Продлить / купить»."
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
        # Не всплывашка, а экран с выходом (Блок «Тариф»): человек упирался в
        # стену ровно в тот момент, когда готов был платить больше, и ему не
        # предлагали ничего. Теперь у стены есть дверь.
        if user.sub_max_devices == 0:
            # Не «(0/0)» — это читается как баг. Объясняем: таков тариф.
            lead = "В твоём тарифе нет устройств."
        else:
            lead = f"Все устройства тарифа заняты: {used} из {user.sub_max_devices}."
        await call.message.edit_text(
            ui.screen(
                ui.title("📱", "Нужно ещё устройство"),
                lead=lead,
                note=(
                    "Добавь их в тариф — неиспользованные дни не сгорят, они "
                    "пересчитаются под новый тариф. Или освободи место, удалив "
                    "ненужное устройство."
                ),
            ),
            reply_markup=limit_reached_kb(f"{CB_DEVICE}:list"),
        )
        await call.answer()
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
    for _server, peer in made:
        await ask_config_format(message.chat.id, session, peer)
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
            for _server, peer in made:
                await ask_config_format(call.message.chat.id, session, peer)

    peers = await repo.list_peers_for_device(session, device.id)
    accesses = await repo.list_wdtt_for_device(session, device.id)
    # Доживающий после переезда конфиг (Этап C) в списке не показываем: он уже
    # заменён новым в той же локации, и две строки одной страны читались бы как
    # удвоение. Работать он при этом продолжает — сутки на замену файла есть.
    active_peers = relocate.visible_peers(peers)
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
            # Расход по каждому конфигу (Блок «Мелочи 2»): раньше в карточке был
            # только список локаций, и юзер не видел, что куда потратил.
            lines.append(f"   • {loc} — {amnezia.fmt_bytes(p.traffic_used_bytes)}")
            locations.append((p.id, loc))
    active_acc = [a for a in accesses if a.status == PeerStatus.ACTIVE]
    lines.append(f"• Резервных подключений: <b>{len(active_acc)}</b>")
    # Итог по устройству — конфиги плюс обходы, привязанные к нему. Отозванные
    # тоже в сумме: трафик уже потрачен и в лимит подписки он вошёл.
    dev_total = sum(p.traffic_used_bytes for p in peers) + sum(
        a.traffic_used_bytes for a in accesses
    )
    lines.append(f"• 📊 Всего трафика: <b>{amnezia.fmt_bytes(dev_total)}</b>")
    # Кнопка «Сменить сервер» (Этап C): есть что переселять, устройство живо и
    # подписка не кончилась. Кулдаун здесь не смотрим — он живёт на экранах
    # переезда: карточка не должна ходить на каждый конфиг за его сроком.
    can_move = bool(active_peers) and active and _sub_active(user)
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=device_card_kb(
            device.id, can_get=active, can_revoke=active,
            locations=locations, can_move=can_move,
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
    # Третья точка выдачи (Этап C): кнопки на доживающий конфиг карточка больше
    # не рисует, но старое сообщение в чате нажимается — и юзер настроил бы в
    # приложении файл, который через сутки погаснет сам.
    if peer.grace_until is not None:
        await call.answer(
            "Этот конфиг заменён новым — открой карточку устройства.",
            show_alert=True,
        )
        return
    await ask_config_format(call.message.chat.id, session, peer)
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:send:"))
async def cb_dev_send(call: CallbackQuery, session: AsyncSession) -> None:
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    # Та же фильтрация, что в карточке: «получить все» не должно слать
    # доживающий после переезда конфиг — в приложении уже нужен новый.
    peers = relocate.visible_peers(await repo.list_peers_for_device(session, device.id))
    if not peers:
        await call.answer("Нет активных конфигов", show_alert=True)
        return
    # Вопрос про формат задаём один раз на устройство: до 10.08.2026 он
    # приходил на каждую локацию, и юзер отвечал на него столько раз,
    # сколько у него конфигов.
    await ask_config_format_for_device(call.message.chat.id, session, device, peers)
    await call.answer()


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
    # Резервные подключения устройство переживают (решение Влада 4.08) — но
    # молча это выглядит как их пропажа: юзер видел их числом в карточке
    # устройства, а после удаления карточки нет. Говорим, куда они делись.
    kept = len(await repo.list_wdtt_for_device(session, device_id))
    # Журнал пишет сама delete_device, одной транзакцией с удалением: иначе при
    # сбое ниже (например, Telegram не принял edit_text) устройство осталось бы
    # снесённым, а следа в истории юзера не осталось. Врезки здесь больше нет —
    # она бы задвоила событие.
    await teardown.delete_device(
        session, device,
        actor_tg_id=user.tg_id,
        details=f"Устройство «{label}» удалено юзером",
    )
    await session.commit()
    # Удаление необратимо (ревайв невозможен) — фиксируем в лог, кто и что снёс.
    logger.info("User {} deleted device {} ({})", user.id, device_id, label)
    # Возврат в список устройств, а не в меню (Блок «Мелочи 2»).
    text = t.device_revoked.format(label=label)
    if kept:
        text += "\n\n" + t.device_revoked_bypass_kept
    await call.message.edit_text(text, reply_markup=back_to_devices_kb())
    await call.answer()


# --- Моя подписка ------------------------------------------------------------

@router.callback_query(F.data == f"{CB_SUB}:my")
async def cb_sub_my(call: CallbackQuery, session: AsyncSession) -> None:
    """Экран подписки.

    Переверстан 20.08.2026 (Блок «Облик»): было пять буллетов и два абзаца
    курсивом на шестьдесят слов каждый — про триал и про автопродление. Абзацы
    уехали в свёрнутую справку, факты стали строками-иконками.
    """
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    used = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    from bot.config import settings
    from bot.services import billing, cryptopay
    from bot.services.pricing import fmt_rub, monthly_price_kopeks

    active = _sub_active(user)
    can_pay = cryptopay.enabled()
    on_trial = user.is_trial and active and user.sub_expires_at is not None
    perpetual = user.sub_expires_at is None and not user.is_trial

    facts = [
        ui.fact("📱", "Устройства", f"{used} из {user.sub_max_devices}"),
        ui.fact("⚡", "Резервные подключения", f"{bypass} из {user.sub_max_bypass}"),
        ui.fact("📅", "Срок", _sub_line(user)),
        ui.fact(
            "📊", "Трафик",
            amnezia.fmt_traffic_line(
                await repo.sub_traffic_used(session, user),
                user.sub_traffic_limit_bytes,
                expired=not active,
            ),
        ),
        ui.fact("💰", "Баланс", fmt_rub(user.balance_kopeks)),
    ]

    if on_trial:
        lead = f"Идёт бесплатный пробный период — {settings.trial_days} дней."
    elif not active:
        lead = "Подписка закончилась, VPN на паузе."
    elif perpetual:
        lead = "Подписка бессрочная."
    else:
        lead = None

    note = None
    if not active:
        note = (
            "Всё сохранено — заново настраивать ничего не придётся. "
            + ("Жми «🔁 Продлить подписку»: устройства включатся сами."
               if can_pay else
               "Напиши в «🆘 Поддержка» — продлим.")
        )

    # Справка длинная, поэтому свёрнутая: развернёт тот, кому она нужна.
    help_parts = []
    if on_trial:
        help_parts.append(
            "🎁 <b>Что будет после пробного периода</b>\n"
            "VPN просто встанет на паузу — ничего настраивать заново не "
            "придётся, все конфиги сохранятся. Дальше — "
            # Состав назван прямо, поэтому и цена точная, без «от»: «от 120 ₽
            # за 1 устройство + 1 подключение» читалось бы как «бывает и
            # дороже за то же самое».
            f"{fmt_rub(monthly_price_kopeks(1, 1))}/мес за 1 устройство + "
            "1 резервное подключение, есть тарифы и дешевле."
            + (" Продлить можно уже сейчас: оплаченный срок прибавится к "
               "пробному, ни дня не сгорит." if can_pay else "")
        )
    if can_pay and not perpetual:
        # Текст нарочно не зависит от user.autopay: тумблер обновляет только
        # кнопки, и «включено/выключено» в тексте после нажатия начало бы врать.
        # Текущее состояние видно прямо на кнопке «♻️ Автопродление: ВКЛ/выкл».
        help_parts.append(
            "♻️ <b>Автопродление</b>\n"
            "Включено — когда срок закончится, бот сам продлит подписку с "
            "баланса на тот же срок, что ты покупал в прошлый раз, и VPN не "
            "прервётся. Не хватит на полный срок — продлит на меньший и "
            "напишет, сколько не хватило. Не хватит даже на месяц — ничего не "
            "спишется, бот подождёт пополнения. Выключено — VPN встанет на "
            "паузу, пока не продлишь вручную."
        )
        help_parts.append(
            "⚙️ <b>Смена тарифа</b>\n"
            "Менять число устройств и подключений можно в любой момент. "
            "Неиспользованные дни не сгорают: они пересчитываются в новый "
            "тариф. Дороже тариф — дней меньше, дешевле — больше."
        )

    text = ui.screen(
        ui.title("🎫", "Подписка"),
        lead=lead,
        facts=facts,
        note=note,
        help=ui.help_block("💡 Подробности", "\n\n".join(help_parts)) if help_parts else None,
    )

    # Смену тарифа предлагаем только тому, кому есть что менять: у триала дни
    # подарены, у бессрочной менять нечего, у истёкшей пересчитывать нечего.
    can_switch = can_pay and not perpetual and not user.is_trial and active

    await call.message.edit_text(
        text,
        reply_markup=subscription_kb(
            can_pay=can_pay and not perpetual,
            autopay=user.autopay if (can_pay and not perpetual) else None,
            can_switch=can_switch,
        ),
    )
    await call.answer()
