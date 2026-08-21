"""Смена сервера у конфига глазами юзера (Этап C).

Отдельным модулем, а не внутри handlers/devices.py: там своя тема — список
устройств, их создание, удаление и подписка, — а здесь четыре экрана подряд
(какой конфиг → локация → сервер → подтверждение), и в карточке устройств они
бы утонули.

FSM нет: peer_id и server_id едут в callback_data. Значит, права проверяются В
КАЖДОМ хендлере — id подделывается тривиально (урок Этапа B). Ответ на чужой id
дословно совпадает с ответом на несуществующий: по разнице ответов чужой конфиг
не должен отличаться от несуществующего.

Кулдаун «раз в сутки» живёт только здесь: relocate.move_peer его не знает,
потому что админу он не помеха. Проверяем дважды — на входе в выбор локаций и
ещё раз перед самим переездом: между экранами проходит время, и переезд мог
случиться в другом окне бота.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, User
from bot.handlers.config_delivery import ask_config_format
from bot.handlers.devices import _sub_active
from bot.keyboards.inline import (
    CB_DEVICE,
    back_to_devices_kb,
    move_confirm_kb,
    move_pick_config_kb,
    move_pick_location_kb,
    move_pick_server_kb,
)
from bot.services import relocate
from bot.services.ssh import SSHError
from bot.texts import t, ui

router = Router(name="config_move")


async def _own_peer(
    call: CallbackQuery, session: AsyncSession, peer_id: int
) -> tuple[Peer, User] | None:
    """Пир юзера, нажавшего кнопку, — или None с готовым ответом.

    Проверка одна на все экраны: забыть её в одном из четырёх хендлеров —
    ровно та ошибка, которой в Этапе B стоил отдельный разбор.
    """
    peer = await repo.get_peer(session, peer_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if peer is None or user is None or peer.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return None
    if peer.status != PeerStatus.ACTIVE or peer.grace_until is not None:
        await call.answer("Этот конфиг уже нельзя переселить", show_alert=True)
        return None
    if not _sub_active(user):
        await call.answer(
            "Подписка закончилась — сначала продли её в «🎫 Подписка».",
            show_alert=True,
        )
        return None
    return peer, user


def _cooldown_answer(peer: Peer) -> str | None:
    """Текст отказа по кулдауну или None, если переезжать можно."""
    left = relocate.cooldown_left(peer, datetime.now(timezone.utc))
    if left is None:
        return None
    hours = int(left.total_seconds() // 3600)
    when = f"{hours} ч" if hours >= 1 else f"{int(left.total_seconds() // 60)} мин"
    return (
        f"Этот конфиг уже переезжал недавно. Следующий переезд — через {when}: "
        "так серверы не дёргаются каждые пять минут."
    )


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:move:"))
async def cb_move_start(call: CallbackQuery, session: AsyncSession) -> None:
    """Начало: какой конфиг переселяем. Один конфиг — сразу к локациям."""
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return
    if not _sub_active(user):
        await call.answer(
            "Подписка закончилась — сначала продли её в «🎫 Подписка».",
            show_alert=True,
        )
        return

    peers = relocate.visible_peers(await repo.list_peers_for_device(session, device.id))
    if not peers:
        await call.answer("У этого устройства нет активных конфигов", show_alert=True)
        return
    if len(peers) == 1:
        await _render_locations(call, session, peers[0], user, device_id)
        return

    labels = await repo.server_labels_map(session)
    rows = [(p.id, labels.get(p.server_id, "?")) for p in peers]
    await call.message.edit_text(
        "🔀 <b>Смена сервера</b>\n\n"
        "У этого устройства несколько конфигов — по одному на страну. "
        "Выбери, какой переселить:",
        reply_markup=move_pick_config_kb(rows, device_id),
    )
    await call.answer()


async def _render_locations(
    call: CallbackQuery, session: AsyncSession, peer: Peer, user: User, device_id: int
) -> None:
    """Экран локаций. Вынесен, потому что в него ведут два пути: с выбора
    конфига и напрямую, когда конфиг единственный."""
    denied = _cooldown_answer(peer)
    if denied:
        await call.answer(denied, show_alert=True)
        return
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    if not groups:
        await call.answer(
            "Сейчас переезжать некуда: свободных серверов нет. Попробуй позже — "
            "или напиши в «🆘 Поддержка», разберёмся.",
            show_alert=True,
        )
        return
    keys = sorted(groups)
    # Сервер без локации попал бы в кнопки как «#id» — показываем его имя.
    names = [k if not k.startswith("#") else groups[k][0].name for k in keys]
    labels = await repo.server_labels_map(session)
    await call.message.edit_text(
        f"🔀 <b>Куда переселить «{peer.label}»</b>\n\n"
        f"Сейчас конфиг живёт здесь: <b>{ui.safe(labels.get(peer.server_id, '?'))}</b>.\n"
        "Выбери страну:",
        reply_markup=move_pick_location_kb(peer.id, names, device_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvloc:"))
async def cb_move_locations(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    got = await _own_peer(call, session, peer_id)
    if got is None:
        return
    peer, user = got
    await _render_locations(call, session, peer, user, peer.device_id or 0)


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvsrv:"))
async def cb_move_servers(call: CallbackQuery, session: AsyncSession) -> None:
    """Серверы выбранной локации. Список пересобираем заново: пока юзер думал,
    места могли кончиться."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvsrv:")
    peer_id_s, idx_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    keys = sorted(groups)
    idx = int(idx_s)
    if idx >= len(keys):
        await call.answer("Список успел измениться, начни заново.", show_alert=True)
        return
    group = groups[keys[idx]]
    labels = await repo.server_labels_map(session)
    rows = [(s.id, labels.get(s.id, s.name)) for s in group]
    await call.message.edit_text(
        "🔀 <b>Выбери сервер</b>\n\n"
        "Серверы одной страны отличаются только нагрузкой — сверху те, где "
        "свободнее.",
        reply_markup=move_pick_server_kb(peer.id, rows, peer.device_id or 0),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvok:"))
async def cb_move_confirm(call: CallbackQuery, session: AsyncSession) -> None:
    """Экран подтверждения: здесь юзер узнаёт про замену файла ДО переезда."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvok:")
    peer_id_s, server_id_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    server_id = int(server_id_s)
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    allowed = {s.id for group in groups.values() for s in group}
    if server_id not in allowed:
        await call.answer(
            "Этот сервер уже недоступен — выбери другой.", show_alert=True
        )
        return
    labels = await repo.server_labels_map(session)
    await call.message.edit_text(
        t.move_confirm.format(
            label=peer.label,
            where_from=labels.get(peer.server_id, "?"),
            where_to=labels.get(server_id, "?"),
        ),
        reply_markup=move_confirm_kb(peer.id, server_id, peer.device_id or 0),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvgo:"))
async def cb_move_go(call: CallbackQuery, session: AsyncSession) -> None:
    """Собственно переезд. Все проверки повторяются: между подтверждением и
    нажатием прошло время, и место на сервере мог занять кто-то другой."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvgo:")
    peer_id_s, server_id_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    denied = _cooldown_answer(peer)
    if denied:
        await call.answer(denied, show_alert=True)
        return

    server_id = int(server_id_s)
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    target = next(
        (s for group in groups.values() for s in group if s.id == server_id), None
    )
    if target is None:
        await call.answer(
            "Место на этом сервере только что заняли — выбери другой.",
            show_alert=True,
        )
        return

    # Метку и название снимаем ДО переезда: если он упадёт, session.rollback()
    # погасит загруженные объекты, и любое чтение их полей полезет в базу —
    # в async-контексте это MissingGreenlet ровно в аварийной ветке.
    peer_label = peer.label
    target_name = target.name
    labels = await repo.server_labels_map(session)
    where_to = labels.get(target.id, target_name)

    await call.answer("⏳ Переселяю...")
    try:
        new_peer = await relocate.move_peer(
            session, peer, target, owner=user,
            actor_tg_id=user.tg_id, reason="по просьбе юзера",
        )
    except SSHError as exc:
        await session.rollback()
        # Сырой exc юзеру не показываем: пугает и может раскрыть host сервера.
        logger.warning("User peer move failed: {}", exc)
        await call.message.edit_text(
            "⚠️ Не получилось переехать — сервер не ответил. Твой конфиг остался "
            "на прежнем месте и работает. Попробуй ещё раз чуть позже.",
            reply_markup=back_to_devices_kb(),
        )
        return
    except Exception:
        await session.rollback()
        logger.exception("Unexpected user peer move error")
        await call.message.edit_text(t.error_generic, reply_markup=back_to_devices_kb())
        return
    await session.commit()

    await call.message.edit_text(
        t.move_done.format(label=peer_label, where_to=where_to),
        reply_markup=back_to_devices_kb(),
    )
    await ask_config_format(call.message.chat.id, session, new_peer)
