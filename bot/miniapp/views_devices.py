"""Мини-приложение: устройства, конфиги и резервные подключения.

Всё, что здесь есть, умеет и бот. Разница только в подаче: в приложении конфиг
можно показать картинкой прямо на экране и скопировать ссылку одним касанием, а
в чат он уезжает по кнопке — файлом, картинкой или ссылкой.
"""
from __future__ import annotations

from aiohttp import web
from loguru import logger

from bot.config import settings
from bot.db import repo
from bot.db.models import PeerStatus
from bot.miniapp.http import ApiError, Ctx, authorized, body, int_arg
from bot.miniapp.views_account import sub_active
from bot.services import amnezia, bypass_issue, relocate, teardown
from bot.services.qrgen import conf_to_qr_png
from bot.services.ssh import SSHError
from bot.utils.validators import is_valid_label

# Платформы резервного подключения — источник тот же, что у бота: список
# приложений живёт в одном месте, иначе бот и приложение однажды предложат
# разные программы.
from bot.handlers.wdtt import _PLATFORMS


def _label_arg(data: dict) -> str:
    label = str(data.get("label") or "").strip()
    if not is_valid_label(label):
        raise ApiError(
            "bad_label",
            "Название не подходит: до 32 символов, буквы, цифры, пробелы, "
            "дефис или подчёркивание.",
        )
    return label


async def _own_device(ctx: Ctx, device_id: int):
    device = await repo.get_device(ctx.session, device_id)
    # Чужой номер отвечает ровно как несуществующий: по разнице ответов чужие
    # устройства не должны отличаться от выдуманных.
    if device is None or device.user_id != ctx.user.id:
        raise ApiError("not_found", "Устройство не найдено.", status=404)
    return device


async def _own_peer(ctx: Ctx, peer_id: int):
    peer = await repo.get_peer(ctx.session, peer_id)
    if peer is None or peer.user_id != ctx.user.id:
        raise ApiError("not_found", "Конфиг не найден.", status=404)
    if peer.status != PeerStatus.ACTIVE:
        raise ApiError(
            "revoked", "Конфиг на паузе — продли подписку, и он оживёт сам."
        )
    return peer


def _path_id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except (KeyError, ValueError):
        raise ApiError("bad_body", "Некорректный запрос.") from None


# ── Устройства ───────────────────────────────────────────────────────────────

@authorized()
async def devices(request: web.Request, ctx: Ctx) -> dict:
    session, user = ctx.session, ctx.user
    rows = await repo.list_devices_for_user(session, user.id, active_only=False)
    rows.sort(key=lambda d: (d.status != PeerStatus.ACTIVE, d.id))
    labels = await repo.server_labels_map(session)
    items = []
    for device in rows:
        peers = await repo.list_peers_for_device(session, device.id)
        accesses = await repo.list_wdtt_for_device(session, device.id)
        visible = [
            p for p in relocate.visible_peers(peers) if p.status == PeerStatus.ACTIVE
        ]
        items.append({
            "id": device.id,
            "label": device.label,
            "active": device.status == PeerStatus.ACTIVE,
            "locations": [labels.get(p.server_id, "?") for p in visible],
            "bypass": sum(1 for a in accesses if a.status == PeerStatus.ACTIVE),
            "traffic": amnezia.fmt_bytes(
                sum(p.traffic_used_bytes for p in peers)
                + sum(a.traffic_used_bytes for a in accesses)
            ),
        })
    used = sum(1 for d in rows if d.status == PeerStatus.ACTIVE)
    return {
        "items": items,
        "used": used,
        "max": user.sub_max_devices,
        "can_add": sub_active(user) and used < user.sub_max_devices,
        "sub_active": sub_active(user),
    }


@authorized()
async def device_card(request: web.Request, ctx: Ctx) -> dict:
    """Карточка устройства со списком конфигов по локациям.

    Здесь же дозакидываем недостающие локации, как это делает карточка в боте:
    появилась новая страна — устройство получает в ней конфиг при открытии.
    """
    session, user = ctx.session, ctx.user
    device = await _own_device(ctx, _path_id(request))
    if device.status == PeerStatus.ACTIVE and sub_active(user):
        from bot.handlers.configs import provision_device_peers

        try:
            if await provision_device_peers(session, user, device):
                await session.commit()
        except Exception:
            # Новая локация не доехала — это не повод не показать карточку.
            logger.exception("Мини-приложение: дозаливка локаций упала")

    labels = await repo.server_labels_map(session)
    peers = await repo.list_peers_for_device(session, device.id)
    accesses = await repo.list_wdtt_for_device(session, device.id)
    visible = [
        p for p in relocate.visible_peers(peers) if p.status == PeerStatus.ACTIVE
    ]
    return {
        "id": device.id,
        "label": device.label,
        "active": device.status == PeerStatus.ACTIVE,
        "configs": [
            {
                "peer_id": p.id,
                "location": labels.get(p.server_id, "?"),
                "traffic": amnezia.fmt_bytes(p.traffic_used_bytes),
            }
            for p in visible
        ],
        "bypass": [
            {"id": a.id, "label": a.label, "platform": a.platform or ""}
            for a in accesses if a.status == PeerStatus.ACTIVE
        ],
        "traffic": amnezia.fmt_bytes(
            sum(p.traffic_used_bytes for p in peers)
            + sum(a.traffic_used_bytes for a in accesses)
        ),
    }


@authorized(action=True)
async def device_create(request: web.Request, ctx: Ctx) -> dict:
    """Создаёт устройство и выдаёт ему конфиг в каждой доступной локации.

    Тот же порядок, что в боте: сначала запись, потом серверы, и при неудаче
    откат целиком — устройство без единого конфига занимало бы место в тарифе.
    """
    session, user = ctx.session, ctx.user
    label = _label_arg(await body(request))
    if not sub_active(user):
        raise ApiError("expired", "Подписка закончилась — сначала продли её.")
    used = await repo.count_active_devices(session, user.id)
    if used >= user.sub_max_devices:
        raise ApiError(
            "limit",
            "Все устройства тарифа заняты. Добавь их в тариф — "
            "неиспользованные дни не сгорят, а пересчитаются.",
        )
    if not await repo.list_ready_servers(session, for_user=user):
        raise ApiError("no_servers", "Локации сейчас недоступны — попробуй позже.")

    from bot.handlers.configs import provision_device_peers

    device = await repo.create_device(session, user_id=user.id, label=label)
    try:
        made = await provision_device_peers(session, user, device)
        if not made:
            raise SSHError("не удалось создать конфиг ни на одной локации")
    except SSHError as exc:
        await session.rollback()
        logger.warning("Мини-приложение: устройство не создано: {}", exc)
        raise ApiError(
            "provision",
            "Не получилось создать устройство — что-то сбоит на нашей "
            "стороне. Подожди пару минут и попробуй ещё раз.",
            status=503,
        ) from None
    logger.info("Мини-приложение: юзер {} создал устройство {}", user.id, device.id)
    return {"id": device.id, "label": device.label, "configs": len(made)}


@authorized(action=True)
async def device_rename(request: web.Request, ctx: Ctx) -> dict:
    session = ctx.session
    device = await _own_device(ctx, _path_id(request))
    label = _label_arg(await body(request))
    device.label = label
    # Метки пиров и подключений копируют метку устройства при создании — тянем
    # их следом, чтобы админ-вью и карточки не разъезжались.
    for p in await repo.list_peers_for_device(session, device.id):
        p.label = label
    for a in await repo.list_wdtt_for_device(session, device.id):
        a.label = label
    return {"label": label}


@authorized(action=True)
async def device_delete(request: web.Request, ctx: Ctx) -> dict:
    session, user = ctx.session, ctx.user
    device = await _own_device(ctx, _path_id(request))
    label = device.label
    kept = len(await repo.list_wdtt_for_device(session, device.id))
    await teardown.delete_device(
        session, device,
        actor_tg_id=user.tg_id,
        details=f"Устройство «{label}» удалено из мини-приложения",
    )
    logger.info("Мини-приложение: юзер {} удалил устройство «{}»", user.id, label)
    return {
        "kept_bypass": kept,
        "message": f"Устройство «{label}» удалено.",
    }


# ── Конфиги ──────────────────────────────────────────────────────────────────

async def _built(ctx: Ctx, peer):
    from bot.handlers.config_delivery import build_conf_for_peer

    built = await build_conf_for_peer(ctx.session, peer)
    if built is None:
        raise ApiError("no_server", "Сервер конфига недоступен.", status=503)
    return built


@authorized()
async def peer_config(request: web.Request, ctx: Ctx) -> dict:
    """Текст конфига и ссылка для приложения AmneziaVPN.

    Отдаём по TLS и только владельцу: и то и другое — ключи от VPN.
    """
    from bot.handlers.configs import config_display_base_raw, make_vpn_link

    peer = await _own_peer(ctx, _path_id(request))
    server, conf = await _built(ctx, peer)
    return {
        "location": config_display_base_raw(server),
        "label": peer.label,
        "conf": conf,
        "link": await make_vpn_link(ctx.session, server, peer.label, conf),
    }


@authorized()
async def peer_qr(request: web.Request, ctx: Ctx):
    peer = await _own_peer(ctx, _path_id(request))
    _server, conf = await _built(ctx, peer)
    return web.Response(
        body=conf_to_qr_png(conf),
        content_type="image/png",
        # Картинка — это ключ от VPN: ни браузеру, ни промежуточным кешам
        # хранить её не нужно.
        headers={"Cache-Control": "no-store"},
    )


@authorized(action=True)
async def peer_send(request: web.Request, ctx: Ctx) -> dict:
    """Присылает конфиг в чат с ботом — файлом, картинкой или ссылкой.

    Файл нужен именно так: сохранять его прямо из страницы Telegram умеет не
    каждый клиент, а сообщение в чате открывается в AmneziaVPN на любом.
    """
    from aiogram.types import BufferedInputFile

    from bot.handlers.config_delivery import conf_filename
    from bot.handlers.configs import make_vpn_link
    from bot.loader import bot
    from bot.texts import t

    kind = str((await body(request)).get("kind") or "")
    if kind not in {"file", "qr", "link"}:
        raise ApiError("bad_body", "Некорректный запрос.")
    peer = await _own_peer(ctx, _path_id(request))
    server, conf = await _built(ctx, peer)
    chat_id = ctx.user.tg_id

    if kind == "file":
        filename = conf_filename(server, peer.label)
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
                "«Сканировать QR-код» и наведи камеру на этот экран."
            ),
        )
    else:
        link = await make_vpn_link(ctx.session, server, peer.label, conf)
        await bot.send_message(chat_id, t.vpn_link_msg.format(link=link))
    return {"message": "Отправил в чат с ботом."}


# ── Резервное подключение ────────────────────────────────────────────────────

def _platforms() -> list[dict]:
    return [
        {"key": key, "name": name, "app": app}
        for key, (name, app, _install) in _PLATFORMS.items()
    ]


@authorized()
async def bypass_list(request: web.Request, ctx: Ctx) -> dict:
    session, user = ctx.session, ctx.user
    from bot.services.bypass_issue import link_for

    rows = await repo.list_wdtt_for_user(session, user.id)
    rows.sort(key=lambda a: (a.status != PeerStatus.ACTIVE, a.id))
    labels = await repo.server_labels_map(session)
    items = []
    for access in rows:
        items.append({
            "id": access.id,
            "label": access.label,
            "active": access.status == PeerStatus.ACTIVE,
            "platform": access.platform or "",
            "platform_name": _PLATFORMS.get(access.platform or "", ("", "", ""))[0],
            "app": _PLATFORMS.get(access.platform or "", ("", "", ""))[1],
            "location": labels.get(access.server_id, "?"),
            "traffic": amnezia.fmt_bytes(access.traffic_used_bytes),
            "link": await link_for(session, access) if access.status == PeerStatus.ACTIVE else "",
        })
    used = sum(1 for a in rows if a.status == PeerStatus.ACTIVE)
    groups, _load, any_server = await bypass_issue.location_groups(session, user)
    devices_rows = await repo.list_devices_for_user(session, user.id, active_only=True)
    return {
        "items": items,
        "used": used,
        "max": user.sub_max_bypass,
        "can_add": bool(
            settings.wdtt_vk_hashes and sub_active(user)
            and used < user.sub_max_bypass and groups
        ),
        "enabled": bool(settings.wdtt_vk_hashes) and any_server,
        "locations": [
            {"key": key, "name": key if not key.startswith("#") else group[0].name}
            for key, group in groups.items()
        ],
        "devices": [{"id": d.id, "label": d.label} for d in devices_rows],
        "platforms": _platforms(),
    }


def _normalize_vk(raw: str) -> str:
    v = raw.strip()
    for p in ("https://", "http://"):
        if v.startswith(p):
            v = v[len(p):]
    return v.strip().strip("/")


@authorized(action=True)
async def bypass_create(request: web.Request, ctx: Ctx) -> dict:
    session, user = ctx.session, ctx.user
    data = await body(request)
    if not settings.wdtt_vk_hashes:
        raise ApiError("off", "Резервное подключение сейчас недоступно.")
    if not sub_active(user):
        raise ApiError("expired", "Подписка закончилась — сначала продли её.")
    used = await repo.count_active_wdtt_for_user(session, user.id)
    if used >= user.sub_max_bypass:
        raise ApiError(
            "limit",
            "Все резервные подключения тарифа заняты. Добавь их в тариф — "
            "неиспользованные дни не сгорят, а пересчитаются.",
        )
    platform = str(data.get("platform") or "")
    if platform not in _PLATFORMS:
        raise ApiError("bad_body", "Неизвестная платформа.")

    groups, load, _any = await bypass_issue.location_groups(session, user)
    key = str(data.get("location") or "")
    group = groups.get(key)
    if group is None:
        raise ApiError(
            "no_location", "В этой локации не осталось мест — выбери другую."
        )
    server = bypass_issue.least_loaded(group, load)

    device = None
    if data.get("device_id"):
        device = await _own_device(ctx, int_arg(data, "device_id", lo=1, hi=2**31))
        if device.status != PeerStatus.ACTIVE:
            raise ApiError("not_found", "Устройство недоступно.", status=404)

    vk = _normalize_vk(str(data.get("vk") or ""))
    if vk and "vk" not in vk.lower():
        raise ApiError("bad_vk", "Похоже, это не ссылка на звонок VK.")

    try:
        access, link = await bypass_issue.issue(
            session, user,
            server=server, device=device, platform=platform, vk_hashes=vk or None,
        )
    except bypass_issue.NoCapacity:
        raise ApiError(
            "no_location", "Свободные места только что кончились — попробуй позже."
        ) from None
    except SSHError as exc:
        logger.warning("Мини-приложение: подключение не создано: {}", exc)
        raise ApiError(
            "provision",
            "На сервере заминка — попробуй ещё раз через пару минут.",
            status=503,
        ) from None

    labels = await repo.server_labels_map(session)
    return {
        "id": access.id,
        "label": access.label,
        "link": link,
        "location": labels.get(server.id, server.name),
        "app": _PLATFORMS[platform][1],
        "install": _PLATFORMS[platform][2],
    }


@authorized(action=True)
async def bypass_delete(request: web.Request, ctx: Ctx) -> dict:
    session, user = ctx.session, ctx.user
    access = await repo.get_wdtt_access(session, _path_id(request))
    if access is None or access.user_id != user.id:
        raise ApiError("not_found", "Подключение не найдено.", status=404)
    label = access.label
    await teardown.revoke_bypass(
        session, access,
        actor_tg_id=user.tg_id,
        details=f"Обход БС «{label}» удалён из мини-приложения",  # wording: ok — аудит-лог админа
    )
    return {"message": f"Подключение «{label}» удалено."}
