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
from bot.db.models import PeerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_WDTT,
    back_to_menu,
    cancel_only,
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
from bot.texts import t
from bot.utils.timefmt import fmt_msk

router = Router(name="wdtt")

_WDTT_PER_PAGE = 8

# platform → (подпись, название приложения, URL установки).
# URL пока None: реальных ссылок на скачивание нет — до их появления юзеру
# показываем «пришлём в поддержке» (см. _app_block). Как появятся — вписать
# сюда, текст выдачи подхватит сам.
_PLATFORMS = {
    "android": ("Android", "WDTT (Android)", None),
    "ios": ("iOS", "vk-turn-proxy (iOS)", None),
    "pc": ("ПК", "PWDTT (Windows/Linux/macOS)", None),
}


def _app_block(platform: str) -> str:
    """Строка «где взять приложение» для t.wdtt_created."""
    url = _PLATFORMS.get(platform, ("", "", None))[2]
    if url:
        return url
    return (
        "<i>Ссылку на приложение пришлём в поддержке — жми «🆘 Поддержка» "
        "в меню, ответим быстро.</i>"
    )


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _sub_active(user) -> bool:
    return user.sub_expires_at is None or _as_utc(user.sub_expires_at) > datetime.now(timezone.utc)


async def _wdtt_location_groups(session: AsyncSession):
    """Локация → READY-сервера с включённым обходом и СВОБОДНОЙ ёмкостью
    (wdtt_max_accesses; NULL — безлимит). Заполненные сервера юзеру не предлагаются.
    Возвращает (группы, загрузка по серверам, есть_ли_wdtt_сервера_вообще)."""
    servers = [s for s in await repo.list_ready_servers(session) if s.wdtt_enabled]
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
            "\n<i>Подписка закончилась — добавить обход пока нельзя. Твои "
            "обходы сохраняются 30 дней и оживут при продлении сами.</i>"
        )
    elif not accesses:
        text += "\nПока пусто. Жми «➕ Добавить обход»."

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
        f"• Платформа: <b>{plat}</b>\n"
        f"• 🌍 Локация: <b>{labels.get(access.server_id, '—')}</b>\n"
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
    # Удаление необратимо (ревайв невозможен) — фиксируем в лог.
    logger.info("User {} deleted wdtt access {} ({})", user.id, access.id, access.label)
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
    groups, load, any_wdtt = await _wdtt_location_groups(session)
    if not any_wdtt:
        await call.answer("Обход БС пока недоступен ни в одной локации — попробуй позже.", show_alert=True)
        return
    if not groups:
        await call.answer(
            "Свободные места для обхода закончились — попробуй чуть позже.", show_alert=True
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
    # Свежая выборка: пока юзер думал, ёмкость локации могла закончиться.
    groups, load, _ = await _wdtt_location_groups(session)
    group = groups.get(keys[idx])
    if not group:
        await call.answer("В этой локации не осталось свободных мест — выбери другую.", show_alert=True)
        return
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
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
    device = await repo.get_device(session, data["device_id"])
    if server is None or device is None or not server.wdtt_enabled:
        await call.message.edit_text("Сервер или устройство недоступны.", reply_markup=back_to_menu())
        await call.answer()
        return
    # Ёмкость перепроверяем в момент создания: пока юзер шёл по шагам,
    # последний слот на сервере мог занять кто-то другой.
    if server.wdtt_max_accesses is not None:
        load = await repo.count_active_wdtt_by_server(session)
        if load.get(server.id, 0) >= server.wdtt_max_accesses:
            await call.message.edit_text(
                "Свободные места для обхода только что закончились — "
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
                label=device.label,
                vk_hashes=vk_hashes,
                ports=server.wdtt_ports,
                binary=settings.wdtt_binary_path,
            )
    except SSHError as exc:
        # Сырой exc юзеру не показываем — техножаргон на английском пугает.
        logger.warning("wdtt create failed: {}", exc)
        await call.message.edit_text(
            "😔 Не получилось создать обход — на сервере какая-то заминка.\n"
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
    app_name = _PLATFORMS[platform][1]
    await call.message.edit_text(
        t.wdtt_created.format(
            label=device.label, server=labels.get(server.id, server.name),
            app=app_name, app_block=_app_block(platform), link=link,
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
