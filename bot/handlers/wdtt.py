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
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.texts import ui
from bot.db import repo
from bot.db.models import AuditAction, PeerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_WDTT,
    back_to_bypasses_kb,
    back_to_menu,
    cancel_only,
    limit_reached_kb,
    pick_location_kb,
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
from bot.texts import t, ui
from bot.utils.timefmt import as_utc, fmt_msk

router = Router(name="wdtt")

_WDTT_PER_PAGE = 8

# platform → (подпись, название приложения, URL установки / инструкция).
_PLATFORMS = {
    "android": (
        "Android", "WDTT (Android)",
        "https://github.com/amurcanov/proxy-turn-vk-android/releases",
    ),
    "ios": (
        "iOS", "VK Turn Proxy (iOS)",
        (
            "Установка через TestFlight:\n"
            "1. Скачай <a href=\"https://apps.apple.com/rs/app/testflight/id899247664\">TestFlight</a> из App Store.\n"
            "2. Открой <a href=\"https://testflight.apple.com/join/ANm6cmDv\">ссылку-приглашение</a> и установи <b>VK Turn Proxy</b>."
        ),
    ),
    "pc": (
        "ПК", "PWDTT (Windows/Linux/macOS)",
        "https://github.com/luminescq/PWDTT/releases",
    ),
}


# Платформы, где в приложении есть тумблер «Режим ссылки»: пока он выключен,
# полей для ссылки на экране нет. Проверено на Android 17.08.2026; про iOS и ПК
# достоверно не известно, поэтому там шаг не показываем — лучше промолчать, чем
# отправить человека искать несуществующую настройку.
_LINK_MODE_PLATFORMS = {"android"}


def _link_mode(platform: str | None) -> bool:
    return platform in _LINK_MODE_PLATFORMS


def _app_block(platform: str) -> str:
    """Строка «где взять приложение» для t.wdtt_created."""
    url = _PLATFORMS.get(platform, ("", "", None))[2]
    if url:
        return url
    return (
        "<i>Ссылку на приложение пришлём в поддержке — жми «🆘 Поддержка» "
        "в меню, ответим быстро.</i>"
    )


async def _link_for(session: AsyncSession, access) -> str:
    """Ссылка доступа с АКТУАЛЬНЫМ адресом сервера из его карточки.

    Ссылка сохраняется один раз при выдаче и после смены IP у хостера держит
    мёртвый адрес. Конфиг VPN такой болезни не знает — он каждый раз собирается
    заново из server.host; здесь делаем то же самое. Одна точка на бота и на
    админку: поддержка обязана видеть ровно ту ссылку, что ушла юзеру."""
    uri = decrypt(access.uri_enc)
    server = await repo.get_server(session, access.server_id)
    return wdtt_svc.link_with_host(uri, server.host) if server else uri


def _sub_active(user) -> bool:
    return user.sub_expires_at is None or as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


async def _wdtt_location_groups(session: AsyncSession, user=None):
    """Локация → READY-сервера с включённым обходом и СВОБОДНОЙ ёмкостью
    (wdtt_max_accesses; NULL — безлимит). Заполненные сервера юзеру не предлагаются,
    приватные — только админам/«друзьям» (гейт в list_ready_servers).
    Возвращает (группы, загрузка по серверам, есть_ли_wdtt_сервера_вообще)."""
    servers = [
        s for s in await repo.list_ready_servers(session, for_user=user)
        if s.wdtt_enabled
    ]
    load = await repo.count_active_wdtt_by_server(session)
    free = [
        s for s in servers
        if s.wdtt_max_accesses is None or load.get(s.id, 0) < s.wdtt_max_accesses
    ]
    return repo.group_by_location(free), load, bool(servers)


def _least_loaded(group, load: dict[int, int]):
    """Наименее загруженный сервер группы — равномерное распределение внутри локации."""
    return min(group, key=lambda s: load.get(s.id, 0))


def _sub_days_left(user) -> int:
    """Дней до конца подписки для ctl -days; 0 = бессрочно."""
    if user.sub_expires_at is None:
        return 0
    delta = as_utc(user.sub_expires_at) - datetime.now(timezone.utc)
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
    # Показываем и отозванные (🚫): с Блока «Ревайв» они ждут продления подписки
    # и оживут сами — пусть юзер видит, что доступ не пропал. Лимит считаем
    # только по активным.
    accesses = await repo.list_wdtt_for_user(session, user.id)
    accesses.sort(key=lambda a: (a.status != PeerStatus.ACTIVE, a.id))
    active_total = sum(1 for a in accesses if a.status == PeerStatus.ACTIVE)
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
    can_create = _sub_active(user) and active_total < user.sub_max_bypass
    text = t.wdtt_intro.format(used=active_total, limit=user.sub_max_bypass)
    if not _sub_active(user):
        text += (
            "\n<i>Подписка закончилась — добавить резервное подключение пока "
            "нельзя. Доступы сохраняются 30 дней и оживут при продлении сами.</i>"
        )
    elif user.sub_max_bypass == 0 and not accesses:
        text += (
            "\nВ твоём тарифе сейчас нет резервных подключений — добавь их "
            "кнопкой ниже. Неиспользованные дни не сгорят."
        )
    elif not accesses:
        text += "\nПока пусто. Жми «➕ Добавить»."

    # Справка идёт последней и свёрнутой: статусная строка выше должна
    # оставаться на виду, а «как это работает» нужно ровно один раз.
    text += "\n\n" + ui.help_block_raw(t.wdtt_intro_help)

    await call.message.edit_text(
        text,
        reply_markup=wdtt_user_list_kb(
            rows, can_create=can_create, page=page,
            has_prev=page > 0, has_next=start + _WDTT_PER_PAGE < total,
            offer_tariff=user.sub_max_bypass == 0 and _sub_active(user),
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
    # Чья VK-ссылка зашита в доступ (Блок «Мелочи 2»): со своей ссылкой обход
    # перестаёт работать, если юзер создал новый звонок, — пусть видит сразу.
    vk = {True: "своя", False: "сервиса"}.get(access.vk_own)
    text = (
        f"⚡ <b>{access.label}</b>\n"
        f"• Платформа: <b>{plat}</b>\n"
        + (f"• Адрес подключения: <b>{vk}</b>\n" if vk else "")
        + f"• 🌍 Локация: <b>{ui.safe(labels.get(access.server_id, '—'))}</b>\n"
        f"• Статус: <b>{t.STATUS_RU.get(access.status, access.status)}</b>\n"
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}"
    )
    if access.expires_at:
        text += f"\n• ⏱ Действует до: {fmt_msk(access.expires_at, with_time=False)}"
    if access.status != PeerStatus.ACTIVE:
        text += (
            "\n\n⏸ <i>Отключён до продления подписки. Прежняя ссылка оживёт "
            "при продлении сама — удалять доступ не нужно.</i>"
        )
    else:
        # Про отвязку человек должен прочитать ДО того, как решит, что ссылка
        # битая: приложение говорит «неверный пароль», а дело в устройстве.
        text += t.wdtt_unbind_hint
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
    # Имя приложения — по платформе доступа; для старых доступов без платформы
    # остаётся общее перечисление.
    app = _PLATFORMS.get(access.platform, ("", "", None))[1] if access.platform else ""
    app_line = (
        f"Импортируй её в приложение резервного подключения — <b>{app}</b>." if app
        else "Импортируй её в приложение резервного подключения (WDTT — Android, VK Turn Proxy — iOS, PWDTT — ПК)."
    )
    # Адрес подставляем из карточки сервера, а не из того, что записано в
    # ссылке: после смены IP у хостера в базе лежит мёртвый адрес, и юзер
    # получил бы ссылку, которая никуда не ведёт.
    await call.message.answer(
        t.wdtt_link.format(
            link=await _link_for(session, access), app_line=app_line,
            link_mode=t.wdtt_link_mode_short if _link_mode(access.platform) else "",
        )
    )
    await call.answer("Отправил ссылку")


async def _unbind_access(
    session: AsyncSession,
    access,
    *,
    actor_tg_id: int | None,
    actor_is_admin: bool = False,
) -> bool | None:
    """Снимает на сервере обхода привязку доступа к устройству.

    Сервер запоминает первое устройство, подключившееся по ссылке, и остальным
    отвечает отказом — приложение переводит этот отказ как «неверный пароль».
    Пока привязку не снять, человек со новым телефоном (или с другим клиентом
    вместо WDTT) уверен, что ссылка битая, и уходит молча.

    True — привязка была и снята, False — доступ и так был свободен, None —
    сервер не ответил. Одна точка на юзера и на поддержку: путей два, а
    поведение и запись в журнале обязаны быть одни."""
    server = await repo.get_server(session, access.server_id)
    if server is None:
        return None
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            was_bound = await wdtt_svc.unbind_device(
                ssh, password=decrypt(access.password_enc),
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        logger.warning("Wdtt unbind {} ssh err: {}", access.id, exc)
        return None
    await repo.log_action(
        session, AuditAction.WDTT_UNBOUND,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=access.user_id,
        target_type="wdtt",
        target_id=access.id,
        # Отдельная формулировка на «привязки не было»: по журналу должно быть
        # видно, чинили реальную проблему или человек нажал наугад.
        details=(
            f"Обход БС «{access.label}» отвязан от устройства" if was_bound  # wording: ok — аудит-лог админа
            else f"Обход БС «{access.label}»: привязки к устройству не было"  # wording: ok — аудит-лог админа
        ),
    )
    return was_bound


def _unbind_result_text(was_bound: bool | None) -> str:
    if was_bound is None:
        return t.wdtt_unbind_failed
    return t.wdtt_unbound if was_bound else t.wdtt_unbound_already


@router.callback_query(F.data.startswith(f"{CB_WDTT}:myunbind:"))
async def cb_wdtt_my_unbind(call: CallbackQuery, session: AsyncSession) -> None:
    """«Подключаюсь с другого устройства» — юзер сам освобождает своё
    подключение. Без лимита: решение осознанное — привязка перестаёт работать
    как защита от передачи ссылки друзьям, зато человек не отваливается из-за
    чужого сообщения об ошибке в приложении."""
    access = await repo.get_wdtt_access(session, int(call.data.rsplit(":", 1)[-1]))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if access.status != PeerStatus.ACTIVE:
        await call.answer("Доступ отозван", show_alert=True)
        return
    await call.answer("Отвязываю…")
    was_bound = await _unbind_access(session, access, actor_tg_id=user.tg_id)
    await session.commit()
    await call.message.answer(_unbind_result_text(was_bound))


@router.callback_query(F.data.startswith(f"{CB_WDTT}:myrevoke:"))
async def cb_wdtt_my_revoke(call: CallbackQuery, session: AsyncSession) -> None:
    access = await repo.get_wdtt_access(session, int(call.data.rsplit(":", 1)[-1]))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if access is None or user is None or access.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    from bot.services import teardown
    await teardown.revoke_bypass(
        session, access,
        actor_tg_id=user.tg_id,
        details=f"Обход БС «{access.label}» удалён юзером",  # wording: ok — аудит-лог админа
    )
    await session.commit()
    # Удаление необратимо (ревайв невозможен) — фиксируем в лог.
    logger.info("User {} deleted wdtt access {} ({})", user.id, access.id, access.label)
    # Возврат в список обходов, а не в меню (Блок «Мелочи 2»).
    await call.message.edit_text(
        t.wdtt_revoked.format(label=access.label), reply_markup=back_to_bypasses_kb()
    )
    await call.answer()


# ======================= Создание доступа (FSM) =============================

async def _standalone_label(session: AsyncSession, user_id: int) -> str:
    """Имя обхода, выданного без устройства.

    Пустым оно быть не может: уходит на сервер обхода в `ctl add -label`, стоит
    заголовком карточки и подставляется в суффикс ПК-ссылки. Номер берём
    наименьший свободный, а не «сколько всего + 1», — иначе после удаления
    второго из трёх обходов новый снова назвался бы вторым.
    """
    taken = {a.label for a in await repo.list_wdtt_for_user(session, user_id)}
    n = 1
    while f"Резервное подключение {n}" in taken:
        n += 1
    return f"Резервное подключение {n}"


async def _ask_device(call: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    """Шаг «к какому устройству привязать» — если привязывать не к чему, шага нет.

    Устройство для обхода — метка, а не владелец (решение Влада 4.08): тариф
    продаёт устройства и резервные подключения отдельными позициями, и «0
    устройств + 1 подключение» — покупаемый тариф. Прежний тупик «Сначала
    создай устройство» на таком тарифе не проходился вообще: устройство не
    создать, лимит 0, а деньги уже списаны.
    """
    devices = await repo.list_devices_for_user(session, user.id, active_only=True)
    if not devices:
        await state.update_data(device_id=None)
        await state.set_state(WdttStates.vk)
        await call.message.edit_text(t.wdtt_ask_vk, reply_markup=wdtt_vk_choice_kb())
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
        # Экран с выходом, а не всплывашка (Блок «Тариф») — как и на устройствах.
        if user.sub_max_bypass == 0:
            lead = "В твоём тарифе нет резервных подключений."
        else:
            lead = (
                f"Все резервные подключения тарифа заняты: "
                f"{used} из {user.sub_max_bypass}."
            )
        await call.message.edit_text(
            ui.screen(
                ui.title("⚡", "Нужно ещё подключение"),
                lead=lead,
                note=(
                    "Добавь их в тариф — неиспользованные дни не сгорят, они "
                    "пересчитаются под новый тариф."
                ),
            ),
            reply_markup=limit_reached_kb(f"{CB_WDTT}:my"),
        )
        await call.answer()
        return
    groups, load, any_wdtt = await _wdtt_location_groups(session, user)
    if not any_wdtt:
        await call.answer(
            "Резервное подключение пока недоступно ни в одной локации — попробуй позже.",
            show_alert=True,
        )
        return
    if not groups:
        await call.answer(
            "Свободные места закончились — попробуй чуть позже.", show_alert=True
        )
        return
    if len(groups) == 1:
        (group,) = groups.values()
        await state.update_data(server_id=_least_loaded(group, load).id)
        await _ask_device(call, state, session, user)
        return
    keys = list(groups)
    # Сервер без локации попал бы в кнопки как «#id» — показываем его имя.
    names = [k if not k.startswith("#") else groups[k][0].name for k in keys]
    await state.update_data(wdtt_loc_keys=keys)
    await state.set_state(WdttStates.pick_server)
    await call.message.edit_text(
        t.wdtt_pick_server, reply_markup=pick_location_kb(names, f"{CB_WDTT}:loc")
    )
    await call.answer()


@router.callback_query(WdttStates.pick_server, F.data.startswith(f"{CB_WDTT}:loc:"))
async def cb_wdtt_pick_location(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    keys = data.get("wdtt_loc_keys") or []
    idx = int(call.data.rsplit(":", 1)[-1])
    if idx >= len(keys):
        await call.answer("Список устарел, начни заново.", show_alert=True)
        return
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    # Свежая выборка: пока юзер думал, ёмкость локации могла закончиться.
    groups, load, _ = await _wdtt_location_groups(session, user)
    group = groups.get(keys[idx])
    if not group:
        await call.answer("В этой локации не осталось свободных мест — выбери другую.", show_alert=True)
        return
    await state.update_data(server_id=_least_loaded(group, load).id)
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
    # device_id пуст, когда устройств у юзера нет вовсе, — шага выбора не было.
    device_id = data.get("device_id")
    device = await repo.get_device(session, device_id) if device_id is not None else None
    if server is None or not server.wdtt_enabled or (device_id is not None and device is None):
        await call.message.edit_text("Сервер или устройство недоступны.", reply_markup=back_to_menu())
        await call.answer()
        return
    label = device.label if device is not None else await _standalone_label(session, user.id)
    # Ёмкость перепроверяем в момент создания: пока юзер шёл по шагам,
    # последний слот на сервере мог занять кто-то другой.
    if server.wdtt_max_accesses is not None:
        load = await repo.count_active_wdtt_by_server(session)
        if load.get(server.id, 0) >= server.wdtt_max_accesses:
            await call.message.edit_text(
                "Свободные места только что закончились — "
                "попробуй ещё раз чуть позже.",
                reply_markup=back_to_menu(),
            )
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
                label=label,
                vk_hashes=vk_hashes,
                ports=server.wdtt_ports,
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        # Сырой exc юзеру не показываем — техножаргон на английском пугает.
        logger.warning("wdtt create failed: {}", exc)
        await call.message.edit_text(
            "😔 Не получилось создать резервное подключение — на сервере "
            "какая-то заминка.\n"
            "Попробуй ещё раз через пару минут. Если не поможет — жми "
            "«🆘 Поддержка» в меню, разберёмся.",
            reply_markup=back_to_menu(),
        )
        await call.answer()
        return
    except Exception:
        logger.exception("Unexpected wdtt create error")
        await call.message.edit_text(t.error_generic, reply_markup=back_to_menu())
        await call.answer()
        return

    # Адрес в ссылку ставим свой: сервер обхода мог запомнить прежний IP и
    # отдавать его до перезапуска демона (см. wdtt_svc.link_with_host).
    link = wdtt_svc.link_with_host(res["link"], server.host)
    if platform == "pc":
        link = f"{link}#{label}"
    access = await repo.create_wdtt_access(
        session,
        server_id=server.id,
        user_id=user.id,
        device_id=device.id if device is not None else None,
        label=label,
        uri_enc=encrypt(link),
        password_enc=encrypt(res["password"]),
        expires_at=None,  # срок гейтит подписка на уровне устройства
        platform=platform,
        # Своя ссылка юзера или сервисная — поддержке это первый вопрос при
        # разборе «у меня обход не работает».
        vk_own=bool(data.get("vk_hash")),
    )
    # Выдача обхода — такое же событие доступа, как выдача конфига VPN: без неё
    # в истории юзера обход появляется из ниоткуда и исчезает при отзыве.
    # Одной транзакцией с самой выдачей: пароль уже на сервере, и запись не
    # должна потеряться, если ниже упадёт отправка сообщения.
    await repo.log_action(
        session, AuditAction.CONFIG_ISSUED,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        target_type="wdtt",
        target_id=access.id,
        details=f"Обход БС «{label}» на сервере «{server.name}» ({platform})",  # wording: ok — аудит-лог админа
    )
    await session.commit()

    labels = await repo.server_labels_map(session)
    app_name = _PLATFORMS[platform][1]
    await call.message.edit_text(
        t.wdtt_created.format(
            label=label, server=labels.get(server.id, server.name),
            app=app_name, app_block=_app_block(platform), link=link,
            link_mode=t.wdtt_link_mode if _link_mode(platform) else "",
            n="3️⃣" if _link_mode(platform) else "2️⃣",
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
        reply_markup=server_card(server_id, server.wdtt_enabled, server.is_private)
    )
    await call.answer(
        # Админская карточка сервера — формулировки остаются (часть 3 дизайна).
        ("Обход БС включён" if server.wdtt_enabled else "Обход БС выключен") + note,  # wording: ok
        show_alert=bool(note),
    )


router.include_router(router_admin)
