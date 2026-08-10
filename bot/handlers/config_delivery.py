"""Доставка конфига юзеру: сборка текста и выбор формата.

Раньше бот на каждый конфиг слал три сообщения подряд — файл, картинку QR и
ссылку, — и они занимали весь экран. Теперь юзер сначала выбирает, что ему
нужно, и получает только это.

Здесь же живёт единственная сборка текста конфига из строки пира: она нужна и
экранам выдачи, и кнопкам выбора формата, которые собирают конфиг заново уже
в момент нажатия.
"""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaDocument,
    InputMediaPhoto,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Device, Peer, PeerStatus, Server
from bot.keyboards.inline import CB_CFG, config_format_device_kb, config_format_kb
from bot.loader import bot
from bot.services import amnezia
from bot.services.crypto import decrypt
from bot.services.qrgen import conf_to_qr_png
from bot.texts import t

router = Router(name="config_delivery")


async def build_conf_for_peer(
    session: AsyncSession, peer: Peer
) -> tuple[Server, str] | None:
    """Собирает текст .conf по строке пира. None — сервер пира удалён.

    Конфиг не хранится: он выводится из приватного ключа пира и параметров
    сервера. Поэтому пересобрать его можно в любой момент, и передавать текст
    через callback_data (где 64 байта на всё) не требуется.
    """
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        return None
    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    conf = amnezia.build_peer_conf(
        peer_private_key=decrypt(peer.private_key_enc),
        peer_ip=peer.ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
        dns=server.dns,
    )
    return server, conf


def _safe_filename_base(name: str) -> str:
    """Имя файла без эмодзи и флагов: «🇳🇱 Нидерланды» → «Нидерланды». Amnezia
    при импорте .conf называет конфиг по имени файла, поэтому файл — витрина."""
    cleaned = re.sub(r"[^\w\s.-]", "", name).strip()
    return cleaned or "vpn"


def _conf_filename(server: Server, label: str) -> str:
    from bot.handlers.configs import config_display_base

    base = _safe_filename_base(config_display_base(server))
    return f"{base}-{label}.conf".replace(" ", "_")


async def ask_config_format(
    chat_id: int, session: AsyncSession, peer: Peer
) -> None:
    """Одно сообщение с выбором вместо трёх сообщений подряд.

    Раньше бот вываливал файл, картинку QR и ссылку сразу — на телефоне это
    занимало весь экран, и юзер листал вверх, чтобы понять, что вообще
    произошло. Спрашиваем один раз, шлём только выбранное.
    """
    from bot.handlers.configs import config_display_base

    server = await repo.get_server(session, peer.server_id)
    where = config_display_base(server) if server else "?"
    await bot.send_message(
        chat_id,
        f"📦 <b>Конфиг «{peer.label}» · {where}</b>\n\n"
        "Как тебе его прислать?\n\n"
        "📄 <b>Файлом</b> — универсально: открой файл в AmneziaVPN.\n"
        "📱 <b>QR-кодом</b> — если настраиваешь <b>другое</b> устройство.\n"
        "🔗 <b>Ссылкой</b> — если настраиваешь <b>этот же</b> телефон.",
        reply_markup=config_format_kb(peer.id),
    )


async def ask_config_format_for_device(
    chat_id: int, session: AsyncSession, device: Device, peers: list[Peer]
) -> None:
    """Один вопрос на всё устройство.

    Раньше «получить все» задавало этот вопрос на каждую локацию, и юзер
    отвечал на него столько раз, сколько у него конфигов.
    """
    from bot.handlers.configs import config_display_base

    where = []
    for peer in peers:
        server = await repo.get_server(session, peer.server_id)
        where.append(config_display_base(server) if server else "?")

    await bot.send_message(
        chat_id,
        f"📦 <b>Конфиги «{device.label}»</b> — {len(peers)} шт.\n"
        f"<i>{', '.join(where)}</i>\n\n"
        "Как их прислать?\n\n"
        "📄 <b>Файлами</b> — универсально: открой каждый в AmneziaVPN.\n"
        "📱 <b>QR-кодами</b> — если настраиваешь <b>другое</b> устройство.\n"
        "🔗 <b>Ссылками</b> — если настраиваешь <b>этот же</b> телефон.\n\n"
        "Одной ссылкой сразу все локации не передать — приложение "
        "принимает по одному серверу за раз.",
        reply_markup=config_format_device_kb(device.id),
    )


# Telegram не принимает в одном альбоме больше десяти вложений.
_ALBUM_LIMIT = 10


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def _visible_active_peers(session: AsyncSession, device_id: int) -> list[Peer]:
    """Те же правила, что и у карточки устройства: отозванные не показываем,
    доживающий после переезда конфиг — тоже (в приложении уже нужен новый)."""
    from bot.services import relocate

    peers = relocate.visible_peers(await repo.list_peers_for_device(session, device_id))
    return [p for p in peers if p.status == PeerStatus.ACTIVE]


@router.callback_query(F.data.startswith(f"{CB_CFG}:") & F.data.contains(":dev:"))
async def cb_config_format_device(call: CallbackQuery, session: AsyncSession) -> None:
    """Присылает все конфиги устройства пачкой.

    Обработчик стоит ВЫШЕ одиночного: оба ловят префикс `cfg:`, и общий,
    получив `cfg:file:dev:10`, попытался бы разобрать «dev» как номер пира.
    """
    _, kind, _, raw_id = call.data.split(":")
    device = await repo.get_device(session, int(raw_id))
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    is_admin = call.from_user.id in settings.admin_ids
    if device is None or (not is_admin and (user is None or device.user_id != user.id)):
        # Тот же ответ, что и на несуществующее устройство: по разнице
        # чужой номер не должен отличаться от несуществующего.
        await call.answer("Не найдено", show_alert=True)
        return

    peers = await _visible_active_peers(session, device.id)
    if not peers:
        await call.answer("Нет активных конфигов", show_alert=True)
        return

    chat_id = call.message.chat.id
    built: list[tuple[Server, Peer, str]] = []
    for peer in peers:
        pair = await build_conf_for_peer(session, peer)
        if pair is not None:
            built.append((pair[0], peer, pair[1]))
    if not built:
        await call.answer("Серверы недоступны", show_alert=True)
        return

    from bot.handlers.configs import config_display_base, make_vpn_link

    if kind == "link":
        lines = []
        for server, peer, conf in built:
            link = await make_vpn_link(session, server, peer.label, conf)
            lines.append(f"<b>{config_display_base(server)}</b>\n<code>{link}</code>")
        await bot.send_message(
            chat_id,
            "🔗 <b>Ссылки на твои конфиги</b>\n\n" + "\n\n".join(lines) + "\n\n"
            "Нажми на ссылку — она скопируется. В AmneziaVPN жми «＋» и вставь.",
        )
        await call.answer("Отправил")
        return

    media = []
    for server, peer, conf in built:
        where = config_display_base(server)
        if kind == "qr":
            media.append(
                InputMediaPhoto(
                    media=BufferedInputFile(
                        conf_to_qr_png(conf), filename=f"{peer.label}-{server.id}.png"
                    ),
                    caption=where,
                )
            )
        else:
            filename = _conf_filename(server, peer.label)
            media.append(
                InputMediaDocument(
                    media=BufferedInputFile(conf.encode("utf-8"), filename=filename),
                    caption=where,
                )
            )

    # Альбом из одного вложения Telegram не принимает — такой шлём как обычно.
    if len(media) == 1:
        single = media[0]
        if kind == "qr":
            await bot.send_photo(chat_id, photo=single.media, caption=single.caption)
        else:
            await bot.send_document(
                chat_id, document=single.media, caption=single.caption
            )
    else:
        for group in _chunks(media, _ALBUM_LIMIT):
            await bot.send_media_group(chat_id, media=group)

    hint = (
        "📱 Открой AmneziaVPN на <b>другом</b> устройстве → «＋» → "
        "«Сканировать QR-код»."
        if kind == "qr"
        else "📄 Открой AmneziaVPN → «＋» → выбери файл нужной локации."
    )
    await bot.send_message(chat_id, hint)
    await call.answer("Отправил")


@router.callback_query(F.data.startswith(f"{CB_CFG}:"))
async def cb_config_format(call: CallbackQuery, session: AsyncSession) -> None:
    """Присылает конфиг в выбранном виде.

    Права проверяем здесь, а не полагаемся на то, что кнопку видит только
    владелец: peer_id в callback_data подделывается тривиально, и без проверки
    любой юзер вытянул бы чужой конфиг подстановкой чужого номера.
    """
    _, kind, raw_id = call.data.split(":")
    peer = await repo.get_peer(session, int(raw_id))
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    is_admin = call.from_user.id in settings.admin_ids
    if not is_admin and (user is None or peer.user_id != user.id):
        # Тот же текст, что и у несуществующего пира: по разнице ответов чужой
        # id не должен отличаться от несуществующего.
        await call.answer("Не найдено", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE:
        await call.answer(
            "Конфиг отозван — продли подписку, и он оживёт сам.", show_alert=True
        )
        return

    built = await build_conf_for_peer(session, peer)
    if built is None:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    server, conf = built
    chat_id = call.message.chat.id

    if kind == "file":
        filename = _conf_filename(server, peer.label)
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(conf.encode("utf-8"), filename=filename),
            caption=(
                f"📄 <code>{filename}</code> — файл с настройками VPN. "
                "Открой AmneziaVPN → «＋» → выбери этот файл."
            ),
        )
    elif kind == "qr":
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(
                conf_to_qr_png(conf), filename=f"{peer.label}.png"
            ),
            caption=(
                "📱 Открой AmneziaVPN на <b>другом</b> устройстве → «＋» → "
                "«Сканировать QR-код» и наведи камеру на этот экран.\n"
                "<i>Настраиваешь этот же телефон? Возьми «🔗 Ссылкой».</i>"
            ),
        )
    else:
        from bot.handlers.configs import make_vpn_link

        link = await make_vpn_link(session, server, peer.label, conf)
        await bot.send_message(chat_id, t.vpn_link_msg.format(link=link))

    await call.answer("Отправил")
